const state = { conversationId: null, streaming: false, oldestMessageId: null, hasOlder: false };
const $ = (selector) => document.querySelector(selector);
const messages = $("#messages");
const conversationList = $("#conversations");
const documentList = $("#documents");
const composer = $("#composer");
const textarea = $("#message");
const send = $("#send");

function scrollBottom() { messages.scrollTop = messages.scrollHeight; }
function titleFor(text) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > 58 ? `${clean.slice(0, 57)}…` : clean || "Новый диалог";
}
function setStatus(text, online = null) {
  const marker = $("#status");
  marker.classList.toggle("online", online === true);
  marker.classList.toggle("offline", online === false);
  $("#status-text").textContent = text;
}
function clearNode(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function renderEmpty(copy = "Загрузите PDF — ответ будет опираться на найденные фрагменты.") {
  clearNode(messages);
  const empty = document.createElement("div"); empty.className = "empty";
  const orb = document.createElement("div"); orb.className = "orb";
  const heading = document.createElement("h2"); heading.textContent = "О чём поговорим?";
  const paragraph = document.createElement("p"); paragraph.textContent = copy;
  empty.append(orb, heading, paragraph); messages.append(empty);
}
function messageElement(role, content = "") {
  const fragment = $("#message-template").content.cloneNode(true);
  const article = fragment.querySelector(".message");
  article.classList.add(role);
  article.querySelector(".avatar").textContent = role === "user" ? "ВЫ" : "P";
  const bubble = article.querySelector(".bubble"); bubble.textContent = content;
  return { article, bubble };
}
function renderMessage(role, content = "") {
  messages.querySelector(".empty")?.remove();
  const { article, bubble } = messageElement(role, content);
  messages.append(article); scrollBottom();
  return { article, bubble };
}
function historyButton() {
  const button = document.createElement("button");
  button.type = "button"; button.className = "load-history";
  button.textContent = "Показать более ранние сообщения";
  button.addEventListener("click", loadOlderMessages);
  return button;
}
async function loadOlderMessages() {
  if (!state.conversationId || !state.hasOlder || !state.oldestMessageId || state.streaming) return;
  const control = messages.querySelector(".load-history");
  if (control) { control.disabled = true; control.textContent = "Загрузка…"; }
  try {
    const payload = await request(`/api/conversations/${encodeURIComponent(state.conversationId)}/messages?before_id=${state.oldestMessageId}`);
    const previousHeight = messages.scrollHeight;
    control?.remove();
    const fragment = document.createDocumentFragment();
    payload.messages.forEach((item) => fragment.append(messageElement(item.role, item.content).article));
    messages.prepend(fragment);
    state.oldestMessageId = payload.messages[0]?.id ?? state.oldestMessageId;
    state.hasOlder = Boolean(payload.has_more);
    if (state.hasOlder) messages.prepend(historyButton());
    messages.scrollTop += messages.scrollHeight - previousHeight;
  } catch (error) {
    if (control) { control.disabled = false; control.textContent = `Ошибка: ${error.message}`; messages.prepend(control); }
  }
}
async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Ошибка ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}
function iconButton(label, symbol, handler) {
  const button = document.createElement("button");
  button.type = "button"; button.className = "icon-button";
  button.ariaLabel = label; button.title = label; button.textContent = symbol;
  button.addEventListener("click", (event) => { event.stopPropagation(); handler(); });
  return button;
}
function renderSources(items) {
  clearNode($("#sources"));
  if (!items.length) return;
  const label = document.createElement("span"); label.textContent = "Источники:";
  $("#sources").append(label);
  items.forEach((item) => {
    const source = document.createElement("span"); source.className = "source";
    source.title = `${item.name} · score ${item.score}`; source.textContent = item.name;
    $("#sources").append(source);
  });
}
function renderContext(payload) {
  const receipt = payload.receipt;
  $("#context-status").textContent = [
    `Контекст: ${receipt.input_tokens}/${receipt.max_tokens} токенов`,
    `резерв ответа ${receipt.output_reserve_tokens}`,
    `PDF ${payload.rag_block_count}`,
    `память ${payload.memory_block_count}`,
  ].join(" · ");
}

async function loadRuntimeMode() {
  const health = await request("/api/health");
  const remote = health.mode === "remote";
  document.body.classList.toggle("remote-mode", remote);
  $("#context-subtitle").textContent = remote
    ? "PDF‑RAG и память · удалённая Ollama"
    : "PDF‑RAG и память с ограниченным контекстом";
  $("#privacy-note").textContent = remote
    ? "Внимание: текст сообщений и PDF отправляются в настроенную удалённую Ollama."
    : "Enter — отправить · Shift + Enter — новая строка · Всё хранится локально";
}

async function loadConversations() {
  const items = await request("/api/conversations");
  clearNode(conversationList);
  items.forEach((item) => {
    const row = document.createElement("div"); row.className = "conversation-row";
    const open = document.createElement("button"); open.type = "button";
    open.className = `conversation ${item.id === state.conversationId ? "active" : ""}`;
    open.textContent = item.title; open.title = `${item.title} · ${item.message_count} сообщений`;
    open.addEventListener("click", () => openConversation(item.id, item.title));
    row.append(open, iconButton("Удалить диалог", "×", async () => {
      if (!window.confirm(`Удалить диалог «${item.title}» и его память?`)) return;
      await request(`/api/conversations/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      if (state.conversationId === item.id) { state.conversationId = null; renderEmpty(); $("#chat-title").textContent = "Новый диалог"; }
      await loadConversations();
    }));
    conversationList.append(row);
  });
}
async function loadDocuments() {
  const items = await request("/api/documents");
  clearNode(documentList);
  if (!items.length) {
    const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "Пока пусто";
    documentList.append(empty); return;
  }
  items.forEach((item) => {
    const row = document.createElement("div"); row.className = "document";
    const type = document.createElement("span"); type.className = "document-type"; type.textContent = "PDF";
    const copy = document.createElement("div"); copy.className = "document-copy"; copy.title = item.name;
    const name = document.createElement("span"); name.textContent = item.name;
    const meta = document.createElement("small"); meta.textContent = `${item.chunks} фрагм.`;
    copy.append(name, meta);
    row.append(type, copy, iconButton("Удалить PDF", "×", async () => {
      if (!window.confirm(`Удалить «${item.name}» из базы знаний?`)) return;
      await request(`/api/documents/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      await loadDocuments();
    }));
    documentList.append(row);
  });
}
async function newConversation() {
  const item = await request("/api/conversations", { method: "POST" });
  await openConversation(item.id, item.title); await loadConversations();
}
async function openConversation(id, title = "Новый диалог") {
  if (state.streaming) return;
  state.conversationId = id; $("#chat-title").textContent = title;
  const payload = await request(`/api/conversations/${encodeURIComponent(id)}/messages`);
  state.oldestMessageId = payload.messages[0]?.id ?? null;
  state.hasOlder = Boolean(payload.has_more);
  renderEmpty("Память этого диалога сохранится локально.");
  if (payload.messages.length) {
    clearNode(messages);
    payload.messages.forEach((item) => renderMessage(item.role, item.content));
    if (state.hasOlder) messages.prepend(historyButton());
  }
  await loadConversations();
}
async function loadModels() {
  try {
    const { models } = await request("/api/models");
    models.forEach((name) => {
      const option = document.createElement("option"); option.value = name; option.textContent = name;
      $("#model").append(option);
    });
    setStatus("Ollama онлайн", true);
  } catch { setStatus("Ollama недоступна", false); }
}
function parseSseFrames(buffer, handle) {
  const frames = buffer.split("\n\n"); const rest = frames.pop();
  frames.forEach((frame) => {
    const event = frame.match(/^event: (.+)$/m)?.[1];
    const raw = frame.match(/^data: (.+)$/m)?.[1];
    if (!event || raw === undefined) return;
    try { handle(event, JSON.parse(raw)); } catch { /* malformed frame: ignore */ }
  });
  return rest;
}
async function streamChat(text) {
  state.streaming = true; send.disabled = true; textarea.disabled = true;
  clearNode($("#sources")); $("#context-status").textContent = "Собираем ограниченный контекст…";
  const user = renderMessage("user", text); const answer = renderMessage("assistant"); let aggregate = "";
  try {
    const response = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: state.conversationId, message: text, model: $("#model").value }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({})); throw new Error(payload.detail || "Ошибка запроса");
    }
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = parseSseFrames(buffer, (event, payload) => {
        if (event === "token") { aggregate += payload; answer.bubble.textContent = aggregate; scrollBottom(); }
        if (event === "sources") renderSources(payload);
        if (event === "context") renderContext(payload);
        if (event === "error") { answer.bubble.textContent = payload.message || "Ошибка Ollama"; }
      });
    }
  } catch (error) {
    answer.bubble.textContent = `Ошибка: ${error.message}`;
    $("#context-status").textContent = "Контекст не был отправлен модели.";
  } finally {
    state.streaming = false; send.disabled = false; textarea.disabled = false; textarea.focus();
    await loadConversations();
  }
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault(); const text = textarea.value.trim();
  if (!text || state.streaming) return;
  if (!state.conversationId) await newConversation();
  textarea.value = ""; textarea.style.height = "auto";
  $("#chat-title").textContent = titleFor(text);
  await streamChat(text);
});
textarea.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); }
});
textarea.addEventListener("input", () => {
  textarea.style.height = "auto"; textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
});
$("#new-chat").addEventListener("click", newConversation);
$("#pdf-input").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const label = document.querySelector(".upload"); label.textContent = "Загрузка…"; $("#upload-note").textContent = "";
  try {
    const form = new FormData(); form.append("file", file);
    const result = await request("/api/documents", { method: "POST", body: form });
    $("#upload-note").textContent = `Готово: ${result.chunks} фрагм.`; await loadDocuments();
  } catch (error) { $("#upload-note").textContent = `Ошибка: ${error.message}`; }
  finally { event.target.value = ""; label.textContent = "↑ Загрузить PDF"; }
});
(async () => {
  try {
    await Promise.all([loadConversations(), loadDocuments(), loadModels(), loadRuntimeMode()]);
    const first = conversationList.querySelector(".conversation");
    if (first) first.click(); else await newConversation();
  } catch (error) { setStatus(`Ошибка интерфейса: ${error.message}`, false); }
})();
