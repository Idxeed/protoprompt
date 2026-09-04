# Возобновление задачи *(experimental)*

`TaskResumePlanner` — намеренно узкий host-only adapter для возобновления
одной задачи из долговременной Ledger-памяти. Он построен поверх [bounded
Ledger recall](ledger-recall.md), но не является workflow engine, checkpoint-ом
агента либо browser/task-control-plane API. У reference Ollama-приложения есть
отдельный local-only demonstration host — [локальное task-resume демо для
Ollama](ollama-task-resume-demo.md); он не расширяет этот core-контракт.

Adapter выбирает только host-confirmed typed `TaskEpisode` из одного
task-specific scope, запечатывает выбор как durable Ledger checkpoint и
собирает его в один ограниченный provider request. Все capabilities, которые
создают, допускают, связывают, запечатывают и возобновляют задачу, остаются у
host-а.

!!! warning "Experimental host boundary"

    Не передавайте модели, model tool или недоверенному клиенту Ledger,
    `MemoryWriter`, admission gate, planner, `task_ref`, descriptor либо
    checkpoint ID. Этот API предназначен для trusted application code.

## Что связывает контракт

Host выпускает opaque `task_ref` (identifier не длиннее 128 символов и без
whitespace) и выводит backend scope так:

```python
task_scope = task_resume_scope(parent_scope, task_ref=task_ref)
```

Derived scope сохраняет tenant и user родителя, получает
`kind="task_resume"` и включает в backend thread namespace как opaque
correlation marker родителя, так и task reference. Поэтому одинаковый
`task_ref` в другом parent thread или kind не может cross-read или resume эту
задачу. У `parent_scope` должны быть непустые tenant и user.

Host обязан использовать этот **точно такой же** derived scope и для
`MemoryWriter`/`LedgerRecallPlanner`, и для `TokenBudgetedContextBuilder`.
`TaskResumePlanner` проверяет это при создании и отвергает mismatched или
widened boundary. Recall planner и request builder также обязаны использовать
один и тот же **экземпляр counter**: выбор checkpoint-а, reduced reference
lane и итоговый provider receipt — единый accounting contract.

Каждая выбранная запись должна быть canonical JSON `TaskEpisode`:

- обязательны `task_ref`, `goal`, `completed_action_refs` и `outcome`;
- `next_action` и `lesson` — семантически опциональные bounded reference
  data, но их canonical JSON keys обязательны и при отсутствии имеют `null`;
- malformed JSON, duplicate/unknown fields, unsupported schema, неверный
  payload kind и несовпадающий task reference отклоняются fail-closed.

`TaskProcedure` существует как отдельный typed data object, но этот adapter
его **не** выбирает. В v0.17 нет dependency graph, ordering или
conflict-resolution semantics для procedure.

## Admission и policy выбора

Episode попадает в этот lane только после того, как trusted host code создаёт
`host_assertion` candidate с `asserted=True`, выполняет review и явно
подтверждает решение `allow`. Строгая recall policy зафиксирована так:

- только `MemoryKind.EPISODE`;
- только `MemoryOrigin.HOST_ASSERTION`;
- immutable admission audit;
- minimum confidence `0.75`.

Конструктор отвергает policy, расширяющую любое из этих правил. Origin label
или typed JSON сами по себе не дают authority и не являются admission decision.

## Минимальная host-интеграция

`ledger`, `store` и `embedding_client` ниже уже сконфигурированы и принадлежат
host-у. Host выбирает и защищает checkpoint secret и task mapping в durable
state.

```python
from protoprompt import ContextInput
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    TaskEpisode,
    TaskOutcome,
    TaskResumePlanner,
    task_resume_scope,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter

parent_scope = MemoryScope(
    tenant="acme",
    user="alice",
    thread="chat:17",
    kind="chat",
)
task_ref = "task:deploy-42"                 # host-minted opaque identifier
descriptor = "Безопасно возобновить проверенный deployment."
task_scope = task_resume_scope(parent_scope, task_ref=task_ref)

writer = MemoryWriter(ledger, scope=task_scope, actor="task-host")
episode = TaskEpisode(
    task_ref=task_ref,
    goal="Возобновить проверенный deployment.",
    completed_action_refs=("action:prepare", "artifact:manifest"),
    outcome=TaskOutcome.INTERRUPTED,
    next_action="Проверить host-owned status deployment.",
)
gate = MemoryReviewGate(
    writer,
    origin=MemoryOrigin.HOST_ASSERTION,
    policy=MemoryAdmissionPolicy(
        policy_id="task-resume-episodes-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
        allowed_kinds=(MemoryKind.EPISODE,),
        minimum_confidence=0.75,
    ),
)
candidate = gate.ingress(
    kind=MemoryKind.EPISODE,
    source_ref="host:deploy-42",
    evidence_refs=("artifact:manifest",),
    confidence=0.9,
    asserted=True,
).submit(episode.to_json())
gate.confirm(gate.review(candidate.record_id), event_id="admission:deploy-42")

counter = RegexTokenCounter()
recall = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.task_resume_safe_default(),
    counter=counter,
    checkpoint_secret=HOST_CHECKPOINT_SECRET,
)
builder = TokenBudgetedContextBuilder(
    store,
    embedding_client,
    counter=counter,
    max_tokens=8_000,
    scope=task_scope,
)
resume = TaskResumePlanner(
    builder,
    recall,
    parent_scope=parent_scope,
    task_ref=task_ref,
    task_descriptor=descriptor,
)

checkpoint = resume.seal_checkpoint(
    checkpoint_id="checkpoint:deploy-42:v1",
    token_budget=600,
    byte_budget=32_768,
)

# Сохраняйте mapping в host-owned durable state, вне Ledger receipt.
host_task_state = {
    "task_ref": task_ref,
    "descriptor": descriptor,
    "checkpoint_id": checkpoint.checkpoint_id,
}
```

Descriptor фиксируется при создании adapter-а. Он намеренно не хранится в
Ledger checkpoint, а `seal_checkpoint()` и `compose_checkpoint()` не принимают
replacement descriptor.

Для следующего запроса host восстанавливает из своего mapping те же parent
scope, task scope, policy, token-counter identity, checkpoint secret и
descriptor. Затем он снова создаёт adapter и собирает host-owned checkpoint ID:

```python
request = await resume.compose_checkpoint(
    checkpoint_id=host_task_state["checkpoint_id"],
    inp=ContextInput(
        query="Что говорит текущий PDF об окне deployment?",
        system_prompt="Следуй правилам безопасности host-а.",
        include_rag=True,
        include_session=False,
    ),
    user_message="Кратко изложи окно deployment.",
)
messages = request.render_messages()  # host сразу отправляет их provider-у
```

Frozen `task_descriptor` используется только для recall и
checkpoint-integrity работы adapter-а. `ContextInput.query` остаётся query
текущего запроса, включая live RAG retrieval. Поэтому текущий вопрос может
касаться PDF, не перебиндивая незаметно task selection checkpoint-а.

## Provider-safe projection

Raw выбранный `TaskEpisode` остаётся только у host-а. Перед сборкой provider
request `TaskResumePlanner` проверяет его относительно task boundary и
проецирует в фиксированную форму `TaskEpisodeReference`:

```json
{
  "schema_version": 1,
  "type": "protoprompt.task-episode-reference",
  "kind": "episode",
  "goal": "...",
  "completed_action_count": 2,
  "outcome": "interrupted",
  "next_action": "...",
  "lesson": "..."
}
```

Provider lane имеет fixed envelope и содержит только эти reduced fields.
`task_ref`, отдельные `completed_action_refs`, descriptor, checkpoint ID,
scope, record ID, source/evidence references и host checkpoint secret
структурно отсутствуют. Aggregate count сохраняет информацию о прогрессе, но
не раскрывает action identifiers. Текстовые поля всё равно являются
**недоверенными reference data**, а не tool instructions или authority.

`compose_checkpoint()` возвращает opaque `TaskResumeReferenceRequest`, а не
generic serializable context object. Отправляйте `request.render_messages()`
только из trusted host code. Не сериализуйте capability через dataclass/web
framework и не возвращайте его из web route. Для совместимости
`render_ledger_data()` теперь возвращает ту же reduced projection, что и
`render_reference_data()`; raw Ledger episode он не возвращает никогда.

## Fresh validation и recovery

Каждая композиция проходит durable checkpoint через public resume path,
проверяет, что continuation reference совпадает с `task_ref` adapter-а,
повторно декодирует каждую выбранную запись как подходящий `TaskEpisode` и
выполняет финальную Ledger validation перед возвратом request. Если supporting
record был forgotten, retracted, superseded, expired, erased или получил новую
revision, resume завершается fail-closed. Host должен оценить новое состояние и
запечатать новый checkpoint; нельзя отправлять старый сохранённый request после
erasure или lifecycle change.

`checkpoint_id` и `task_ref` — opaque host metadata. Это не client/model
routing API, а descriptor нельзя восстановить только из checkpoint-а. Durable
mapping host-а имеет вид:

```text
{ task_ref, descriptor, checkpoint_id }
```

## Receipts и работа с данными

`TaskResumePlanner.explain()`, checkpoint receipts и composed-request receipts
content-free. Они не раскрывают task references, descriptor, scope,
checkpoint identity, record IDs, source/evidence references или Ledger
payloads. Transient `TaskResumeReferenceRequest` содержит только
provider-safe projection вместе с private host internals; держите его у host-а
и отправляйте сразу, не превращая в durable state.

## Явные non-goals

Этот experimental adapter не предоставляет общий Ollama/browser control plane.
Опциональное local demo намеренно получает seed только от host-а и не имеет
task-management route. Adapter также не предоставляет:

- automatic extraction, admission, confirmation или task handoff;
- procedure execution, dependency/conflict semantics или workflow planning;
- tool execution, authority, side effects или exactly-once guarantees;
- provider-conversation snapshot либо workflow/agent checkpoint;
- infinite memory, unlimited context window или automatic long-term retention.

Считайте текст episode недоверенными reference data. За реальные действия и
решение о новом checkpoint-е отвечает host, а не planner и не модель.
