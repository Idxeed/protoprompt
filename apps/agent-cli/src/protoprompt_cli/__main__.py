"""Точка входа: ``pp-agent [путь] [-p промпт] [--backend …]``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from protoprompt import RegexTokenCounter, SqliteStore
from protoprompt.agent import WorkingMemory
from protoprompt.agent.scorer import ScorerWeights

from protoprompt_cli import persistence
from protoprompt_cli.config import load_config, user_config_path
from protoprompt_cli.core import AgentCore
from protoprompt_cli.factory import make_llm, resolve_models
from protoprompt_cli.repl import Repl
from protoprompt_cli.startup import choose_project
from protoprompt_cli.terminal import escape_terminal_text
from protoprompt_cli.tools import PERM_DENY, ToolRunner
from protoprompt_cli import __version__


class _SafeArgumentParser(argparse.ArgumentParser):
    """Keep parser errors from becoming a terminal-control bypass.

    ``argparse`` reflects unrecognised command-line arguments in errors.  The
    normal CLI contract treats them as local input, but escaping here keeps
    this final stdout/stderr route consistent with model and tool output.
    """

    def _print_message(self, message, file=None):
        if message:
            message = escape_terminal_text(message)
        super()._print_message(message, file)


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="pp-agent")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("path", nargs="?", default=None,
                        help="каталог проекта (по умолчанию — cwd)")
    parser.add_argument("-p", "--print", dest="prompt", default=None,
                        help="неинтерактивный прогон: один промпт в stdout")
    parser.add_argument("--output-format", default="text",
                        choices=["text", "json"],
                        help="формат вывода для -p (text | json)")
    parser.add_argument("--session", default=None,
                        help="имя сессии (по умолчанию: default)")
    parser.add_argument("--resume", dest="resume_session", default=None,
                        help="продолжить указанную сессию")
    parser.add_argument("--continue", dest="continue_session",
                        action="store_true", help="продолжить последнюю сессию")
    parser.add_argument("--plan", action="store_true",
                        help="стартовать в режиме планирования")
    parser.add_argument("--stream", dest="stream", action="store_true",
                        default=None, help="включить стриминг")
    parser.add_argument("--no-stream", dest="stream", action="store_false",
                        help="выключить стриминг в REPL")
    parser.add_argument("--backend", default=None,
                        help="LLM-бэкенд: ollama | openai | httpx")
    parser.add_argument("--config", default=None,
                        help="явно доверенный путь к config.toml")
    parser.add_argument("--budget", type=int, default=None,
                        help="токен-бюджет памяти")
    parser.add_argument("--request-max-tokens", type=int, default=None,
                        help="жёсткий лимит всего запроса к модели")
    parser.add_argument("--output-reserve", type=int, default=None,
                        help="резерв и лимит выходных токенов модели")
    parser.add_argument("--trace", action="store_true",
                        help="включить трейс памяти сразу")
    parser.add_argument("--no-menu", action="store_true",
                        help="не показывать меню выбора проекта")
    return parser


async def _run(args: argparse.Namespace) -> int:
    root = persistence.find_root(args.path or Path.cwd())
    identity = persistence.capture_project_identity(root)
    root = identity.root
    # A repository can contain arbitrary TOML.  Only an explicit --config or
    # a per-user state path may choose providers and non-secret endpoints;
    # credentials always come from the fixed PP_* environment contract.
    config_path = args.config or user_config_path(identity)
    cfg = load_config(config_path)
    if args.backend:
        cfg["llm"]["backend"] = args.backend
    if args.budget:
        cfg["memory"]["max_tokens"] = args.budget
    if args.request_max_tokens is not None:
        cfg["agent"]["request_max_tokens"] = args.request_max_tokens
    if args.output_reserve is not None:
        cfg["agent"]["output_reserve_tokens"] = args.output_reserve
    session = args.resume_session or args.session or persistence.DEFAULT_SESSION

    chat_model, embed_model = resolve_models(cfg)
    llm = make_llm(cfg)
    persistence.ensure_state_dir(identity)
    store = SqliteStore(str(persistence.cold_db_path(identity)))
    memory_cfg = cfg["memory"]
    mem = WorkingMemory(
        store=store,
        llm=llm,
        counter=RegexTokenCounter(),
        max_tokens=int(memory_cfg["max_tokens"]),
        namespace=identity.namespace,
        embed_model=embed_model,
        recall_cooldown_steps=int(memory_cfg["recall_cooldown_steps"]),
        dedup_threshold=float(memory_cfg["dedup_threshold"]),
        max_pinned_tokens=int(
            memory_cfg["max_tokens"] * memory_cfg["max_pinned_ratio"]
        ),
        weights=ScorerWeights(ref_half_life=20),
    )

    loaded_perms = (
        persistence.load_json(persistence.perms_json_path(identity), {}) or {}
    )
    # A durable grant must never become authority automatically in a later
    # process: there is no portable atomic way to bind a pathname-based tool
    # action to the original directory object for its whole lifetime.  Keep
    # durable denials (safe restriction), while every allow is session-only.
    perms = (
        {name: mode for name, mode in loaded_perms.items() if mode == PERM_DENY}
        if isinstance(loaded_perms, dict)
        else {}
    )
    tools = ToolRunner(root, perms=perms, project_identity=identity)
    core = AgentCore(
        mem, llm, tools,
        system_prompt=cfg["agent"]["system_prompt"],
        chat_model=chat_model,
        max_iterations=int(cfg["agent"]["max_iterations"]),
        tail_size=int(cfg["agent"]["tail"]),
        request_max_tokens=int(cfg["agent"]["request_max_tokens"]),
        output_reserve_tokens=int(cfg["agent"]["output_reserve_tokens"]),
    )
    core.plan_mode = args.plan

    persistence.load_session(mem, identity, session)
    if args.continue_session and not args.resume_session and not args.session:
        latest = persistence.latest_session(identity)
        if latest:
            if persistence.load_session(mem, identity, latest):
                session = latest

    if args.prompt is not None:
        if args.output_format == "json" and args.stream:
            raise ValueError("--stream нельзя использовать вместе с --output-format json")
        if args.trace:
            from protoprompt_cli.repl import Tracer

            # Trace labels can contain model/tool-derived summaries.  Keep
            # batch mode behind the same terminal boundary as REPL output.
            mem._trace = Tracer(
                lambda line: sys.stdout.write(escape_terminal_text(line) + "\n"),
                mem,
            )
        stream_cb = None
        if args.stream:
            stream_cb = lambda token: sys.stdout.write(escape_terminal_text(token))
        result = await core.turn(args.prompt, stream_cb=stream_cb)
        if args.output_format == "json":
            payload = {
                "reply": result.reply,
                "plan": result.plan,
                "iterations": result.iterations,
                "actions_run": result.actions_run,
                "streamed": result.streamed,
                "usage": core.usage,
            }
            print(escape_terminal_text(json.dumps(payload, ensure_ascii=False, indent=2)))
        elif result.reply and not result.streamed:
            print(escape_terminal_text(result.reply))
        identity.assert_current(root)
        persistence.save_session(mem, identity, session)
        return 0

    repl = Repl(
        core,
        mem,
        tools,
        root=root,
        project_identity=identity,
        cfg=cfg,
        session=session,
        stream=True if args.stream is None else args.stream,
    )
    if args.trace:
        repl._set_trace(True)
    await repl.run()
    identity.assert_current(root)
    persistence.save_session(mem, identity, session)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if (
            args.path is None
            and args.prompt is None
            and not args.no_menu
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        ):
            selected = choose_project(Path.cwd())
            if selected is None:
                return 0
            args.path = str(selected)
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        print(f"pp-agent: error: {escape_terminal_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
