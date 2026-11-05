// app.js - 웹 화면 동작 (docs/SPEC.md 2.10)
// 로그인·회원가입, 대화 목록, 저장한 링크·기억 패널, 메시지 전송(SSE 진행 단계), 답변 마크다운 표시를 맡는다. 외부 라이브러리 없이 작성한다.
// 답변 문자열에는 외부 웹 내용이 섞일 수 있으므로, 표시할 때는 항상 HTML을 먼저 이스케이프한 뒤 허용한 형식만 만든다.
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { threads: [], current: null, busyThread: null };
let WELCOME = null;  // 빈 화면 요소. 메시지 영역을 비워도 다시 쓸 수 있게 처음에 잡아 둔다.

// ---------- API ----------
async function api(path, options = {}) {
  /** JSON API를 호출한다. 실패하면 {status, detail}을 던진다. */
  const res = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) {
    let detail = "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
    try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
    throw { status: res.status, detail };
  }
  return res.json();
}

// ---------- 로그인·회원가입 ----------
let signupMode = false;

function showAuth() {
  /** 로그인 화면을 보여 준다. */
  $("#app-view").hidden = true;
  $("#auth-view").hidden = false;
  $("#auth-email").focus();
}

function setAuthMode(signup) {
  /** 로그인 / 회원가입 모드를 바꾼다. */
  signupMode = signup;
  $("#auth-submit").textContent = signup ? "가입하고 시작하기" : "로그인";
  $("#auth-toggle").textContent = signup ? "이미 계정이 있나요? 로그인" : "처음이신가요? 회원가입";
  $("#auth-sub").textContent = signup ? "이메일과 비밀번호만 있으면 바로 시작할 수 있어요." : "다시 오셨네요. 로그인하고 여행 계획을 이어가세요.";
  $("#auth-hint").hidden = !signup;
  $("#auth-password").autocomplete = signup ? "new-password" : "current-password";
  $("#auth-error").hidden = true;
}

async function submitAuth(event) {
  /** 로그인 또는 회원가입을 보내고, 성공하면 앱 화면으로 간다. */
  event.preventDefault();
  const body = JSON.stringify({ email: $("#auth-email").value, password: $("#auth-password").value });
  $("#auth-submit").disabled = true;
  try {
    const me = await api(signupMode ? "/api/signup" : "/api/login", { method: "POST", body });
    $("#auth-password").value = "";
    await showApp(me.email);
  } catch (err) {
    $("#auth-error").textContent = err.detail;
    $("#auth-error").hidden = false;
  } finally {
    $("#auth-submit").disabled = false;
  }
}

async function logout() {
  /** 로그아웃하고 로그인 화면으로 돌아간다. */
  await api("/api/logout", { method: "POST" }).catch(() => {});
  state.threads = []; state.current = null;
  closePanels();
  $("#link-list").replaceChildren();
  showLinkResult("");
  $("#memory-list").replaceChildren();
  $("#memory-result").replaceChildren();
  setAuthMode(false);
  showAuth();
}

// ---------- 대화 목록 ----------
async function showApp(email) {
  /** 앱 화면을 열고 대화 목록을 불러온다. */
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  $("#user-email").textContent = email;
  await loadThreads();
  showWelcome();
  $("#input").focus();
}

async function loadThreads() {
  /** 내 대화 목록을 다시 불러와 그린다. */
  state.threads = await api("/api/threads");
  renderThreads();
}

function renderThreads() {
  /** 사이드바 대화 목록을 그린다(제목 첫 글자 아이콘, 선택 표시, 답변 중 표시, 삭제 확인). */
  const list = $("#thread-list");
  list.replaceChildren();
  $("#thread-empty").hidden = state.threads.length > 0;
  for (const t of state.threads) {
    const li = document.createElement("li");
    li.className = "thread" + (t.thread_id === state.current ? " on" : "");
    const title = t.title || "새 대화";
    li.innerHTML = `<span class="ico" aria-hidden="true"></span><span class="name"></span>
      <button class="del" type="button" aria-label="대화 삭제">×</button>`;
    li.querySelector(".ico").textContent = title.trim().charAt(0) || "✈";
    const name = li.querySelector(".name");
    name.textContent = title;
    name.title = title;
    if (t.thread_id === state.busyThread) name.classList.add("busy");
    li.addEventListener("click", () => selectThread(t.thread_id));
    li.querySelector(".del").addEventListener("click", (e) => { e.stopPropagation(); askDelete(li, t); });
    list.appendChild(li);
  }
}

function askDelete(li, thread) {
  /** 대화 삭제 전 확인을 목록 안에서 받는다(되돌릴 수 없음: 대화 기록까지 지워진다). */
  if (li.querySelector(".confirm")) return;
  const box = document.createElement("div");
  box.className = "confirm";
  box.innerHTML = `<span>이 대화를 지울까요?</span><button class="danger" type="button">삭제</button><button class="link small" type="button">취소</button>`;
  box.addEventListener("click", (e) => e.stopPropagation());
  box.querySelector(".danger").addEventListener("click", async () => {
    await api(`/api/threads/${thread.thread_id}`, { method: "DELETE" }).catch((err) => alertInChat(err.detail));
    if (state.current === thread.thread_id) { state.current = null; showWelcome(); }
    await loadThreads();
  });
  box.querySelector(".link").addEventListener("click", () => box.remove());
  li.appendChild(box);
}

async function selectThread(threadId) {
  /** 대화를 열어 지난 메시지를 보여 준다. */
  closeSidebar();
  state.current = threadId;
  renderThreads();
  const box = $("#messages");
  box.replaceChildren();
  try {
    const data = await api(`/api/threads/${threadId}/messages`);
    $("#thread-title").textContent = data.thread.title || "새 대화";
    if (!data.messages.length) showWelcome(true);
    for (const m of data.messages) addMessage(m.role === "user" ? "user" : "bot", m.content);
    if (state.busyThread === threadId) addProgressNote();
  } catch (err) {
    alertInChat(err.detail);
  }
  scrollToBottom();
}

function showWelcome(keepThread = false) {
  /** 빈 화면(인사말·예시)을 보여 준다. keepThread가 아니면 '새 대화' 상태로 돌아간다. */
  if (!keepThread) { state.current = null; renderThreads(); $("#thread-title").textContent = "새 대화"; }
  $("#messages").replaceChildren(WELCOME);
  WELCOME.hidden = false;
}

// ---------- 오른쪽 패널(저장한 링크·기억): 한 번에 하나만 연다 ----------
const PANELS = {
  links: { panel: "#links-panel", button: "#open-links", load: () => loadLinks(), focus: "#link-input" },
  memory: { panel: "#memory-panel", button: "#open-memory", load: () => loadMemory(), focus: "#memory-panel" },  // 기억 패널은 입력칸이 없어 패널 자체에 포커스
};

function openPanel(name) {
  /** 패널 하나를 열고(다른 패널은 닫음) 내용을 새로 불러온다. */
  closeSidebar();
  closePanels();
  const p = PANELS[name];
  $(p.panel).hidden = false;
  $(p.button).setAttribute("aria-expanded", "true");
  $(p.focus).focus();
  p.load();
}

function closePanels() {
  /** 열린 패널을 모두 닫는다. */
  for (const p of Object.values(PANELS)) {
    $(p.panel).hidden = true;
    $(p.button).setAttribute("aria-expanded", "false");
  }
}

function togglePanel(name) {
  /** 상단 버튼: 닫혀 있으면 열고, 열려 있으면 닫는다. */
  if ($(PANELS[name].panel).hidden) openPanel(name); else closePanels();
}

function reloadOpenPanel() {
  /** 열려 있는 패널의 내용을 다시 불러온다(대화 중 저장된 링크·기억을 반영). */
  for (const p of Object.values(PANELS)) if (!$(p.panel).hidden) p.load();
}

// ---------- 저장한 링크 패널 ----------

async function loadLinks() {
  /** 저장한 링크 목록을 불러와 그린다. */
  try { renderLinks(await api("/api/links")); }
  catch (err) { if (err.status === 401) return logout(); showLinkResult(`<p class="error">${escapeHtml(err.detail)}</p>`); }
}

function renderLinks(links) {
  /** 저장한 링크 목록(제목·URL·저장일·삭제)을 그린다. 최근 저장한 것이 위로 오게 뒤집어 보여 준다. */
  const list = $("#link-list");
  list.replaceChildren();
  $("#link-empty").hidden = links.length > 0;
  for (const link of [...links].reverse()) {
    const li = document.createElement("li");
    li.className = "saved-link";
    li.innerHTML = `<div class="info"><a target="_blank" rel="noopener noreferrer"></a><span class="url muted small"></span></div>
      <span class="day muted small"></span><button class="del" type="button" aria-label="이 링크 삭제">×</button>`;
    const a = li.querySelector("a");
    a.href = /^https?:\/\//.test(link.url) ? link.url : "#";
    a.textContent = link.title || link.url;
    li.querySelector(".url").textContent = link.url;
    li.querySelector(".day").textContent = link.saved_at ? `${link.saved_at.slice(5).replace("-", "/")} 저장` : "";
    li.querySelector(".del").addEventListener("click", () => askDeleteLink(li, link));
    list.appendChild(li);
  }
}

function askDeleteLink(li, link) {
  /** 링크 삭제 전 확인을 목록 안에서 받는다(그 링크의 저장 내용 전체가 지워져 답변 근거로 쓰지 않게 된다). */
  if (li.querySelector(".confirm")) return;
  const box = document.createElement("div");
  box.className = "confirm";
  box.innerHTML = `<span>이 링크를 지울까요? 앞으로 답변 근거로 쓰지 않아요.</span><button class="danger" type="button">삭제</button><button class="link small" type="button">취소</button>`;
  box.querySelector(".danger").addEventListener("click", async () => {
    try {
      const data = await api(`/api/links?url=${encodeURIComponent(link.url)}`, { method: "DELETE" });
      renderLinks(data.links);
    } catch (err) {
      showLinkResult(`<p class="error">${escapeHtml(err.detail)}</p>`);
      loadLinks();
    }
  });
  box.querySelector(".link").addEventListener("click", () => box.remove());
  li.appendChild(box);
}

function showLinkResult(html) {
  /** 링크 추가 결과(또는 오류)를 입력칸 아래에 보여 준다. */
  $("#link-result").innerHTML = html;
}

async function addLinks(event) {
  /** 붙여넣은 링크를 저장한다. 결과는 대화의 '링크 처리 결과'와 같은 모양(✓ 저장함 / ✕ 못 씀과 이유)으로 보여 준다. */
  event.preventDefault();
  const input = $("#link-input");
  const text = input.value.trim();
  if (!text) return;
  const button = $("#link-submit");
  button.disabled = true;
  button.textContent = "확인 중…";
  showLinkResult(`<p class="muted small">링크를 읽고 여행 글인지 확인하는 중이에요. 링크마다 몇 초~십여 초 걸려요.</p>`);
  try {
    const data = await api("/api/links", { method: "POST", body: JSON.stringify({ text }) });
    showLinkResult(renderNotices(splitNotices(data.notice).notices));
    renderLinks(data.links);
    if (data.saved) input.value = "";
  } catch (err) {
    if (err.status === 401) return logout();
    showLinkResult(`<p class="error">${escapeHtml(err.detail)}</p>`);
  } finally {
    button.disabled = false;
    button.textContent = "저장";
  }
}

// ---------- 기억 패널 ----------
async function loadMemory() {
  /** 장기 기억 목록을 불러와 그린다. */
  $("#memory-result").replaceChildren();
  try { renderMemory(await api("/api/memory")); }
  catch (err) { if (err.status === 401) return logout(); showMemoryError(err.detail); }
}

function renderMemory(items) {
  /** 기억 항목(표시 이름·값·삭제)을 설정 순서대로 그린다. */
  const list = $("#memory-list");
  list.replaceChildren();
  $("#memory-empty").hidden = items.length > 0;
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "memory-item";
    li.innerHTML = `<div class="info"><span class="label"></span><span class="value"></span></div>
      <button class="del" type="button">×</button>`;
    li.querySelector(".label").textContent = item.label;
    li.querySelector(".value").textContent = item.value;
    li.querySelector(".del").setAttribute("aria-label", `${item.label} 기억 삭제`);
    li.querySelector(".del").addEventListener("click", () => askDeleteMemory(li, item));
    list.appendChild(li);
  }
}

function askDeleteMemory(li, item) {
  /** 기억 삭제 전 확인을 목록 안에서 받는다. */
  if (li.querySelector(".confirm")) return;
  const box = document.createElement("div");
  box.className = "confirm";
  box.innerHTML = `<span>이 기억을 지울까요? 앞으로 대화에서 쓰지 않아요.</span><button class="danger" type="button">삭제</button><button class="link small" type="button">취소</button>`;
  box.querySelector(".danger").addEventListener("click", async () => {
    try {
      renderMemory((await api(`/api/memory/${encodeURIComponent(item.field)}`, { method: "DELETE" })).memory);
    } catch (err) {
      showMemoryError(err.detail);
      loadMemory();
    }
  });
  box.querySelector(".link").addEventListener("click", () => box.remove());
  li.appendChild(box);
}

function showMemoryError(text) {
  /** 기억 패널에 오류 안내를 보여 준다. */
  $("#memory-result").innerHTML = `<p class="error">${escapeHtml(text)}</p>`;
}

// ---------- 메시지 ----------
function addMessage(role, text) {
  /** 말풍선 하나를 추가한다. 사용자 메시지는 글자 그대로(링크만), 답변은 마크다운 + 프로그램 안내 분리. */
  WELCOME.hidden = true;
  const msg = document.createElement("div");
  msg.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "bot") bubble.innerHTML = renderAnswer(text);
  else if (role === "user") bubble.innerHTML = linkify(escapeHtml(text));
  else bubble.textContent = text;
  msg.appendChild(bubble);
  $("#messages").appendChild(msg);
  return msg;
}

function alertInChat(text) {
  /** 오류 안내를 채팅 영역에 보여 준다(브라우저 alert 대신). */
  addMessage("error", text);
  scrollToBottom();
}

function addProgressNote() {
  /** 다른 화면에서 보낸 답변이 아직 진행 중임을 알린다. */
  const msg = document.createElement("div");
  msg.className = "msg bot";
  msg.innerHTML = `<div class="progress"><span class="muted">이 대화의 답변을 만드는 중이에요. 끝나면 자동으로 보여 드릴게요.</span></div>`;
  $("#messages").appendChild(msg);
}

function scrollToBottom() {
  /** 메시지 영역을 맨 아래로 내린다. */
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

const LONG_STAGES = ["일정표"];  // 오래 걸리는 단계: 대기 안내를 덧붙인다

function createProgress() {
  /** 진행 단계 카드(출발→도착 경로선, 단계 체크리스트, 경과 초)를 만들고 갱신 함수를 돌려준다. */
  const msg = document.createElement("div");
  msg.className = "msg bot";
  msg.innerHTML = `<div class="progress" role="status"><div class="route" aria-hidden="true">출발 <span class="line"></span> 도착</div>
    <ul class="steps"></ul><div class="elapsed"></div></div>`;
  $("#messages").appendChild(msg);
  const steps = msg.querySelector(".steps");
  const elapsed = msg.querySelector(".elapsed");
  const started = Date.now();
  let longHint = "";
  const tick = () => { elapsed.textContent = `${Math.floor((Date.now() - started) / 1000)}초 지남${longHint}`; };
  tick();
  const timer = setInterval(tick, 1000);
  return {
    msg,
    add(stage) {
      const last = steps.lastElementChild;
      if (last && last.textContent === stage) return;
      if (last && last.classList.contains("wait")) last.remove();   // '기다리는 중'은 실제 단계가 오면 지운다
      steps.querySelectorAll(".now").forEach((li) => li.classList.remove("now"));
      const li = document.createElement("li");
      li.textContent = stage;
      li.className = stage.includes("기다리는 중") ? "now wait" : "now";
      steps.appendChild(li);
      if (LONG_STAGES.some((s) => stage.includes(s))) longHint = " · 일정표는 보통 1~2분 걸려요";
      tick();
      scrollToBottom();
    },
    stop() { clearInterval(timer); },
  };
}

function createDraft(threadId) {
  /** 답변 초안 말풍선. 서버가 보내는 글자(delta)를 이어 붙여 보여 주고, 최종 답변(done)이 오면 지운다.
      최종본은 서버가 링크 안내·📌·가격 출처를 덧붙인 것이라 초안과 다를 수 있어 바꿔 끼운다. 다시 그리기는 프레임당 한 번. */
  let text = "", msg = null, pending = false;
  const paint = () => {
    pending = false;
    if (!msg || state.current !== threadId) return;
    msg.querySelector(".bubble").innerHTML = `<div class="md">${renderMarkdown(text)}</div>`;
    scrollToBottom();
  };
  return {
    append(piece) {
      text += piece;
      if (state.current !== threadId) return;
      if (!msg) {
        msg = document.createElement("div");
        msg.className = "msg bot draft";
        msg.innerHTML = '<div class="bubble" aria-busy="true"></div>';
        $("#messages").appendChild(msg);
      }
      if (!pending) { pending = true; requestAnimationFrame(paint); }
    },
    clear() { text = ""; if (msg) { msg.remove(); msg = null; } },
  };
}

async function sendMessage(event) {
  /** 메시지를 보내고 SSE로 진행 단계·답변을 받는다. 대화가 없으면 새로 만든다. */
  event?.preventDefault();
  const input = $("#input");
  const content = input.value.trim();
  if (!content || state.busyThread) return;
  setBusy(true);
  try {
    if (!state.current) {
      const t = await api("/api/threads", { method: "POST" });
      state.current = t.thread_id;
      state.threads.unshift(t);
    }
  } catch (err) {
    setBusy(false);
    return alertInChat(err.detail);
  }
  const threadId = state.current;
  state.busyThread = threadId;
  renderThreads();
  input.value = "";
  autoGrow();
  addMessage("user", content);
  const progress = createProgress();
  let shown = false;  // 이 대화 화면에 답변(또는 오류)을 그렸는지
  const draft = createDraft(threadId);
  scrollToBottom();
  try {
    const res = await fetch(`/api/threads/${threadId}/messages`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }),
    });
    if (!res.ok) {
      let detail = "메시지를 보내지 못했습니다.";
      try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
      if (res.status === 401) return logout();
      throw new Error(detail);
    }
    await readEvents(res, (event, data) => {
      if (data.title && state.current === threadId) $("#thread-title").textContent = data.title;
      if (event === "progress") progress.add(data.stage);
      else if (event === "delta") draft.append(data.text);
      else if (event === "draft_reset") draft.clear();
      else if (event === "done" || event === "error") {
        progress.stop();
        draft.clear();
        if (state.current === threadId) {
          progress.msg.remove();
          addMessage(event === "done" ? "bot" : "error", event === "done" ? data.answer : data.message);
          shown = true;
          scrollToBottom();
        }
      }
    });
  } catch (err) {
    progress.stop();
    progress.msg.remove();
    draft.clear();
    if (state.current === threadId) { alertInChat(err.message || "답변을 받지 못했습니다. 같은 질문을 다시 보내 주세요."); shown = true; }
  } finally {
    progress.stop();
    state.busyThread = null;
    setBusy(false);
    await loadThreads().catch(() => {});
    reloadOpenPanel();  // 대화 중 저장된 링크·기억이 있을 수 있으므로 열려 있는 패널을 맞춘다
    // 답변을 기다리는 사이 다른 대화를 보다가 돌아왔으면, 저장된 기록으로 다시 그려 답변을 보여 준다
    if (state.current === threadId && !shown) selectThread(threadId);
  }
}

async function readEvents(res, onEvent) {
  /** fetch 응답 본문(SSE)을 읽어 이벤트마다 onEvent(event, data)를 부른다. (EventSource는 POST를 못 보내 직접 읽는다) */
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

function setBusy(busy) {
  /** 답변을 기다리는 동안 보내기 버튼만 막는다(입력은 계속 할 수 있다). */
  $("#send").disabled = busy;
  $("#send").textContent = busy ? "답변 중…" : "보내기";
}

// ---------- 답변 표시: 프로그램 안내 분리 + 마크다운 ----------
// 프로그램 안내 문구는 src/middleware.py·src/memory.py 의 상수와 같아야 한다.
const NOTICE_LINK = "※ 링크 처리 결과";
const NOTICE_LINK_OLD = "※ 참고하지 못한 링크";
const NOTICE_MEMORY = "※ 기억했어요:";
const NOTICE_SAVED_MISSING = "※ 저장하신 자료 중";
const PIN_PATTERN = /📌 내가 저장한 자료(?: \((\d{2}\/\d{2}) 저장\))?/g;

function escapeHtml(text) {
  /** HTML 특수문자를 이스케이프한다(답변 속 외부 웹 내용이 태그로 실행되지 않게). */
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function splitNotices(text) {
  /** 답변을 본문과 프로그램 안내(링크 처리 결과 블록, 기억 알림, 저장 자료 없음)로 나눈다. */
  const body = [], notices = [];
  let block = null;
  for (const line of String(text).split("\n")) {
    const t = line.trim().replace(/^\*\*|\*\*$/g, "");
    if (t.startsWith(NOTICE_LINK) || t.startsWith(NOTICE_LINK_OLD)) {
      block = { kind: "links", label: t.replace(/[:：]\s*$/, ""), items: [] };
      notices.push(block);
    } else if (block && /^[-*•]\s/.test(t)) {
      block.items.push(t.replace(/^[-*•]\s+/, ""));
    } else if (t.startsWith(NOTICE_MEMORY)) {
      block = null;
      notices.push({ kind: "memory", text: t });
    } else if (t.startsWith(NOTICE_SAVED_MISSING)) {
      block = null;
      notices.push({ kind: "saved-missing", text: t });
    } else {
      block = null;
      body.push(line);
    }
  }
  return { body: body.join("\n").trim(), notices };
}

function renderNotices(notices) {
  /** 프로그램 안내를 본문과 다른 블록으로 그린다. ✔ 저장함은 초록, ✖ 못 씀은 빨강. */
  if (!notices.length) return "";
  const html = notices.map((n) => {
    if (n.kind === "links") {
      // ✔·✖는 Windows 등에서 컬러 이모지로 그려져 의미색이 사라지므로, 이모지가 없는 ✓·✕로 바꿔 색을 입힌다
      const items = n.items.map((item) => {
        const safe = linkify(escapeHtml(item))
          .replace(/^✔️?\s*([^:]+):/, '<span class="ok">✓ $1</span>:')
          .replace(/^✖️?\s*못 씀:/, '<span class="fail">✕ 못 씀</span>:');
        return `<li>${safe}</li>`;
      }).join("");
      return `<div class="notice"><span class="label">링크 처리 결과</span><ul>${items}</ul></div>`;
    }
    const label = n.kind === "memory" ? "기억" : "저장 자료";
    const text = n.text.replace(/^※\s*/, "");
    return `<div class="notice"><span class="label">${label}</span>${linkify(escapeHtml(text))}</div>`;
  }).join("");
  return `<div class="notices">${html}</div>`;
}

function inline(text) {
  /** 이스케이프된 한 줄에 굵게·링크·맨 URL·📌 배지를 입힌다. */
  let out = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = linkify(out);
  out = out.replace(PIN_PATTERN, (_, day) => `<span class="pin">📌 내가 저장한 자료${day ? ` · ${day} 저장` : ""}</span>`);
  return out;
}

function linkify(escaped) {
  /** 이스케이프된 문자열의 [제목](URL)과 맨 URL을 링크로 바꾼다(http/https만, 새 탭). */
  const anchor = (url, label) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  const parts = escaped.split(/(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g);
  return parts.map((part) => {
    const md = part.match(/^\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)$/);
    if (md) return anchor(md[2], md[1]);
    return part.replace(/https?:\/\/[^\s<>()]+[^\s<>().,!?]/g, (url) => anchor(url, url));
  }).join("");
}

function renderAnswer(text) {
  /** 답변 전체를 HTML로 만든다: 프로그램 안내를 떼어 따로 그리고, 본문은 마크다운으로. */
  const { body, notices } = splitNotices(text);
  return `<div class="md">${renderMarkdown(body)}</div>${renderNotices(notices)}`;
}

function renderMarkdown(text) {
  /** 답변에 쓰이는 마크다운만 그린다: 제목, 목록(들여쓴 하위 줄 보존), 표, 구분선, 문단·줄바꿈.
      일정표의 '  이동:/출처:/입장료:/▶ 비용' 같은 들여쓴 줄은 앞 항목 아래 줄로 남긴다. */
  const lines = escapeHtml(text).split("\n");
  const out = [];
  let list = null, para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) { flushPara(); closeList(); continue; }
    // 표: '|'로 시작하는 줄이 이어지고 둘째 줄이 구분선일 때만
    if (line.startsWith("|") && i + 1 < lines.length && /^\|?\s*:?-{2,}/.test(lines[i + 1].trim())) {
      flushPara(); closeList();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(line).map((c) => `<th>${c}</th>`).join("");
      i += 1;
      const rows = [];
      while (i + 1 < lines.length && lines[i + 1].trim().startsWith("|")) { i += 1; rows.push(`<tr>${cells(lines[i]).map((c) => `<td>${c}</td>`).join("")}</tr>`); }
      out.push(`<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { flushPara(); closeList(); out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); continue; }
    if (/^(-{3,}|\*{3,})$/.test(line)) { flushPara(); closeList(); out.push("<hr>"); continue; }
    const bullet = raw.match(/^(\s{0,1})([-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      flushPara();
      const kind = /\d/.test(bullet[2]) ? "ol" : "ul";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(bullet[3])}`);
      // 들여쓴 하위 줄(이동:/출처:/입장료:/▶ 비용, 중첩 목록)을 이 항목 아래 줄로 붙인다
      while (i + 1 < lines.length && /^\s{2,}\S/.test(lines[i + 1])) {
        i += 1;
        const sub = lines[i].trim();
        const cls = /^(출처|입장료 출처)\s*:/.test(sub) ? "sub src" : sub.startsWith("▶") ? "sub cost" : "sub";
        out.push(`<div class="${cls}">${inline(sub.replace(/^[-*•]\s+/, "· "))}</div>`);
      }
      out.push("</li>");
      continue;
    }
    closeList();
    if (/^출처\s*:/.test(line)) { flushPara(); out.push(`<p class="src">${inline(line)}</p>`); continue; }
    para.push(line);
  }
  flushPara(); closeList();
  return out.join("");
}

// ---------- 화면 조작 ----------
function autoGrow() {
  /** 입력창 높이를 내용에 맞춘다. */
  const el = $("#input");
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
}

function openSidebar() { $("#sidebar").classList.add("open"); $("#scrim").hidden = false; }
function closeSidebar() { $("#sidebar").classList.remove("open"); $("#scrim").hidden = true; }

document.addEventListener("DOMContentLoaded", async () => {
  WELCOME = $("#welcome");
  $("#auth-form").addEventListener("submit", submitAuth);
  $("#auth-toggle").addEventListener("click", () => setAuthMode(!signupMode));
  $("#logout").addEventListener("click", logout);
  $("#new-thread").addEventListener("click", () => { closeSidebar(); showWelcome(); $("#input").focus(); });
  $("#composer").addEventListener("submit", sendMessage);
  $("#input").addEventListener("input", autoGrow);
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
  });
  document.querySelectorAll(".ticket").forEach((b) => b.addEventListener("click", () => {
    $("#input").value = b.textContent; autoGrow(); $("#input").focus();
  }));
  $("#menu").addEventListener("click", openSidebar);
  $("#scrim").addEventListener("click", closeSidebar);
  $("#open-links").addEventListener("click", () => togglePanel("links"));
  $("#open-memory").addEventListener("click", () => togglePanel("memory"));
  $("#close-links").addEventListener("click", closePanels);
  $("#close-memory").addEventListener("click", closePanels);
  $("#link-form").addEventListener("submit", addLinks);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanels(); });
  try {
    const me = await api("/api/me");
    await showApp(me.email);
  } catch (_) {
    setAuthMode(false);
    showAuth();
  }
});
