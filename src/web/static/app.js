// app.js - 웹 화면 동작 (docs/SPEC.md 2.10)
// 로그인·회원가입, 대화 목록, 저장한 링크·기억 패널, 메시지 전송(SSE 진행 단계), 답변 마크다운 표시를 맡는다. 외부 라이브러리 없이 작성한다.
// 답변 문자열에는 외부 웹 내용이 섞일 수 있으므로, 표시할 때는 항상 HTML을 먼저 이스케이프한 뒤 허용한 형식만 만든다.
"use strict";

const $ = (sel) => document.querySelector(sel);
// running: 답변 중인 대화 id들(서버 목록 기준 + 방금 보낸 것). view: 지금 화면에 붙은 답변 진행 구독(대화 하나).
const state = { threads: [], current: null, running: new Set(), view: null };
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
  detachView();
  state.threads = []; state.current = null; state.running.clear();
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
  /** 내 대화 목록을 다시 불러와 그린다. 답변 중 표시는 서버 기준으로 맞춘다(다른 탭·새로고침 전에 보낸 답변 포함). */
  state.threads = await api("/api/threads");
  state.running = new Set(state.threads.filter((t) => t.running).map((t) => t.thread_id));
  renderThreads();
  updateSendButton();
}

function isRunning(threadId) {
  /** 이 대화가 답변 중인지. */
  return Boolean(threadId) && state.running.has(threadId);
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
    if (isRunning(t.thread_id)) name.classList.add("busy");
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
    if (state.current === thread.thread_id) detachView();
    await api(`/api/threads/${thread.thread_id}`, { method: "DELETE" }).catch((err) => alertInChat(err.detail));
    if (state.current === thread.thread_id) { state.current = null; showWelcome(); }
    await loadThreads();
  });
  box.querySelector(".link").addEventListener("click", () => box.remove());
  li.appendChild(box);
}

async function selectThread(threadId) {
  /** 대화를 열어 지난 메시지를 보여 준다. 답변 중이면 그 진행을 처음부터 다시 받아(/stream) 이어 보여 준다. */
  closeSidebar();
  detachView();
  state.current = threadId;
  renderThreads();
  updateSendButton();
  const box = $("#messages");
  box.replaceChildren();
  try {
    const data = await api(`/api/threads/${threadId}/messages`);
    if (state.current !== threadId) return;  // 불러오는 사이 다른 대화를 골랐다
    $("#thread-title").textContent = data.thread.title || "새 대화";
    if (!data.messages.length && !data.running) showWelcome(true);
    for (const m of data.messages) addMessage(m.role === "user" ? "user" : "bot", m.content, m.itinerary || null);
    if (data.running) {
      state.running.add(threadId);
      renderThreads();
      updateSendButton();
      watchRun(threadId, (signal) => fetch(`/api/threads/${threadId}/stream`, { credentials: "same-origin", signal }));
    }
  } catch (err) {
    alertInChat(err.detail);
  }
  scrollToBottom();
}

function showWelcome(keepThread = false) {
  /** 빈 화면(인사말·예시)을 보여 준다. keepThread가 아니면 '새 대화' 상태로 돌아간다. */
  if (!keepThread) {
    detachView();
    state.current = null; renderThreads(); updateSendButton(); $("#thread-title").textContent = "새 대화";
  }
  $("#messages").replaceChildren(WELCOME);
  WELCOME.hidden = false;
}

// ---------- 오른쪽 패널(저장한 링크·기억): 한 번에 하나만 연다 ----------
const PANELS = {
  links: { panel: "#links-panel", button: "#open-links", load: () => loadLinks(), focus: "#link-input" },
  memory: { panel: "#memory-panel", button: "#open-memory", load: () => loadMemory(), focus: "#memory-panel" },  // 입력칸은 '수정'·'추가'를 눌러야 열려 패널 자체에 포커스
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
  /** 기억 항목을 설정 순서대로 그린다. 값이 있으면 값·수정·삭제, 없으면 '아직 없어요'·추가 (F1: 패널에서 직접 추가·수정). */
  const list = $("#memory-list");
  list.replaceChildren();
  $("#memory-empty").hidden = items.some((item) => item.value);
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "memory-item" + (item.value ? "" : " empty");
    li.innerHTML = `<div class="info"><span class="label"></span><span class="value"></span></div>
      <div class="actions"><button class="edit link small" type="button"></button></div>`;
    li.querySelector(".label").textContent = item.label + (item.manual ? " · 직접 입력" : "");
    li.querySelector(".value").textContent = item.value || "아직 없어요";
    const edit = li.querySelector(".edit");
    edit.textContent = item.value ? "수정" : "추가";
    edit.setAttribute("aria-label", `${item.label} 기억 ${item.value ? "수정" : "추가"}`);
    edit.addEventListener("click", () => editMemory(li, item));
    if (item.value) {
      const del = document.createElement("button");
      del.className = "del";
      del.type = "button";
      del.textContent = "×";
      del.setAttribute("aria-label", `${item.label} 기억 삭제`);
      del.addEventListener("click", () => askDeleteMemory(li, item));
      li.querySelector(".actions").appendChild(del);
    }
    list.appendChild(li);
  }
}

function editMemory(li, item) {
  /** 기억 항목을 그 자리에서 고치는 입력칸(글자 수 표시·저장·취소)을 연다. 저장은 PUT /api/memory/{항목}. */
  if (li.querySelector(".memory-edit")) return;
  li.querySelector(".confirm")?.remove();
  const form = document.createElement("form");
  form.className = "memory-edit";
  form.innerHTML = `<textarea rows="2"></textarea>
    <div class="row"><span class="count muted small"></span><button class="link small" type="button">취소</button><button class="primary" type="submit">저장</button></div>`;
  const input = form.querySelector("textarea");
  const count = form.querySelector(".count");
  input.maxLength = item.max_chars;
  input.value = item.value;
  input.setAttribute("aria-label", `${item.label} 내용`);
  const update = () => { count.textContent = `${input.value.length}/${item.max_chars}`; };
  input.addEventListener("input", update);
  update();
  form.querySelector(".link").addEventListener("click", () => form.remove());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const res = await api(`/api/memory/${encodeURIComponent(item.field)}`, { method: "PUT", body: JSON.stringify({ value: input.value }) });
      renderMemory(res.memory);
      $("#memory-result").innerHTML = `<p class="ok">${escapeHtml(item.label)} ${res.changed ? "기억을 저장했어요. 다음 답변부터 반영돼요." : "기억은 바뀐 내용이 없어요."}</p>`;
    } catch (err) {
      if (err.status === 401) return logout();
      showMemoryError(err.detail);
    }
  });
  li.appendChild(form);
  input.focus();
}

function askDeleteMemory(li, item) {
  /** 기억 삭제 전 확인을 목록 안에서 받는다. */
  if (li.querySelector(".confirm")) return;
  li.querySelector(".memory-edit")?.remove();
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
function addMessage(role, text, itinerary = null) {
  /** 말풍선 하나를 추가한다. 사용자 메시지는 글자 그대로(링크만), 답변은 마크다운 + 프로그램 안내 분리(일정표는 카드). */
  WELCOME.hidden = true;
  const msg = document.createElement("div");
  msg.className = `msg ${role}${itinerary ? " has-itin" : ""}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "bot") bubble.innerHTML = renderAnswer(text, itinerary);
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

async function sendMessage() {
  /** 메시지를 보내 이 대화의 답변을 시작하고 진행을 보여 준다. 대화가 없으면 새로 만든다.
      답변은 서버가 대화마다 따로 돌리므로, 다른 대화의 답변을 기다리는 중에도 보낼 수 있다. 이 대화가 답변 중이면 보내지 않는다. */
  const input = $("#input");
  const content = input.value.trim();
  if (!content || isRunning(state.current)) return;
  try {
    if (!state.current) {
      const t = await api("/api/threads", { method: "POST" });
      state.current = t.thread_id;
      state.threads.unshift(t);
    }
  } catch (err) {
    return alertInChat(err.detail);
  }
  const threadId = state.current;
  state.running.add(threadId);
  renderThreads();
  updateSendButton();
  input.value = "";
  autoGrow();
  addMessage("user", content);
  await watchRun(threadId, (signal) => fetch(`/api/threads/${threadId}/messages`, {
    method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }),
  }));
}

function detachView() {
  /** 화면에 붙은 답변 진행 구독을 끊는다(다른 대화로 옮길 때). 서버의 답변은 계속되고, 돌아오면 /stream으로 다시 붙는다. */
  if (!state.view) return;
  state.view.controller.abort();
  state.view.progress.stop();
  state.view = null;
}

async function watchRun(threadId, request) {
  /** 답변 진행(SSE)을 지금 화면에 그린다: 진행 카드, 글자 초안, 끝(done 답변·error 오류·stopped 멈춤 안내).
      request(signal)는 보내기(POST /messages) 또는 다시 받기(GET /stream). 다시 받기는 처음 이벤트부터 재생된다. */
  detachView();
  const controller = new AbortController();
  const progress = createProgress();
  const draft = createDraft(threadId);
  state.view = { threadId, controller, progress };
  scrollToBottom();
  const finish = (role, text, itinerary = null) => {  // 진행 카드·초안을 걷고 결과를 그린다
    progress.stop();
    progress.msg.remove();
    draft.clear();
    if (role && state.current === threadId) { addMessage(role, text, itinerary); scrollToBottom(); }
  };
  try {
    const res = await request(controller.signal);
    if (!res.ok) {
      let detail = "메시지를 보내지 못했습니다.";
      try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
      if (res.status === 401) return logout();
      if (res.status === 404 && state.current === threadId) { finish(); return selectThread(threadId); }  // 그새 끝났다
      return finish("error", detail);
    }
    await readEvents(res, (event, data) => {
      if (data.title && state.current === threadId) $("#thread-title").textContent = data.title;
      if (event === "progress") progress.add(data.stage);
      else if (event === "delta") draft.append(data.text);
      else if (event === "draft_reset") draft.clear();
      else if (event === "done") finish("bot", data.answer, data.itinerary || null);
      else if (event === "error") finish("error", data.message);
      else if (event === "stopped") finish("notice", data.message);
    });
  } catch (err) {
    if (controller.signal.aborted) { draft.clear(); return; }  // 다른 대화로 옮겼다: 서버 답변은 계속된다
    finish("error", "답변 진행을 받지 못했습니다. 대화를 다시 열면 이어서 볼 수 있어요.");
  } finally {
    if (state.view?.controller === controller) state.view = null;
    progress.stop();
    if (!controller.signal.aborted) {
      state.running.delete(threadId);
      await loadThreads().catch(() => {});
      reloadOpenPanel();  // 대화 중 저장된 링크·기억이 있을 수 있으므로 열려 있는 패널을 맞춘다
    }
  }
}

async function stopCurrent() {
  /** 지금 대화의 답변을 멈춘다. 서버가 다음 단계에서 멈추고 stopped 이벤트를 보내면 안내가 그려진다. */
  const threadId = state.current;
  $("#send").disabled = true;
  $("#send").textContent = "멈추는 중…";
  await api(`/api/threads/${threadId}/stop`, { method: "POST" }).catch((err) => alertInChat(err.detail));
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

function updateSendButton() {
  /** 지금 대화가 답변 중이면 보내기 버튼을 '중지'로 바꾼다(입력은 계속 할 수 있다). 다른 대화가 답변 중인 것과는 상관없다. */
  const running = isRunning(state.current);
  $("#send").disabled = false;
  $("#send").textContent = running ? "중지" : "보내기";
  $("#send").classList.toggle("stop", running);
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

function renderAnswer(text, itinerary = null) {
  /** 답변 전체를 HTML로 만든다: 프로그램 안내를 떼어 따로 그리고, 본문은 마크다운으로.
      일정표(itinerary 카드 데이터가 있으면)는 카드로 그리고 원래 줄글은 '글로 보기'에 접어 둔다. (R5-5) */
  const { body, notices } = splitNotices(text);
  if (itinerary) return renderItinerary(itinerary, body) + renderNotices(notices);
  return `<div class="md">${renderMarkdown(body)}</div>${renderNotices(notices)}`;
}

// ---------- 일정표 카드 (R5-5): 표시 판정·금액은 서버(파이썬)가 정리해 보내고, 여기서는 그리기만 한다 ----------
const CHANGE_LABELS = { changed: "바뀜", added: "새로 추가" };
const VERDICT_CLASS = { within: "ok", near: "near", over: "over" };

function sourceLink(src, label = "출처") {
  /** 출처 {title, url, saved}를 작은 링크로. 저장 자료면 📌 배지를 앞에 붙인다. */
  if (!src || !/^https?:\/\//.test(src.url)) return "";
  const pin = src.saved ? `<span class="pin">📌 내가 저장한 자료 · ${escapeHtml(src.saved)} 저장</span>` : "";
  return `${pin}<a href="${escapeHtml(src.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}: ${escapeHtml(src.title)}</a>`;
}

function renderItinerary(it, body) {
  /** 일정표 카드: 머리(제목·기간·인원·출발지·예산 판정), 바뀐 점, 가정한 조건, 일차 탭 + 타임라인, 예산 카드, 참고, 출처, 글로 보기. */
  const esc = escapeHtml;
  const days = it.days || [];
  const short = (d) => (d || "").slice(5);  // '2026-11-13(금)' → '11-13(금)'
  const period = days.length && days[0].date ? `${short(days[0].date).replace("-", "/")} ~ ${short(days[days.length - 1].date).replace("-", "/")}` : `${it.nights}박${it.nights + 1}일`;
  const b = it.budget;
  const verdict = b && b.verdict ? `<span class="verdict ${VERDICT_CLASS[b.verdict]}">${esc(b.verdict_label)}</span>` : "";
  const changeText = (c) => c.kind === "changed" ? `${esc(c.before)} → ${esc(c.after)}` : c.kind === "added" ? `추가 — ${esc(c.after)}` : `빠짐 — ${esc(c.before)}`;
  const changes = it.changes && it.changes.length
    ? `<section class="itin-changes"><div class="itin-label">바뀐 점</div><ul>${it.changes.map((c) => `<li><b>${c.day}일차 ${esc(c.time)}</b> ${changeText(c)}</li>`).join("")}</ul></section>` : "";
  const assumptions = it.assumptions && it.assumptions.length ? `<p class="itin-assume">가정한 조건 · ${it.assumptions.map(esc).join(", ")}</p>` : "";
  const tabs = days.map((d, i) => `<button type="button" class="itin-tab${i ? "" : " on"}" role="tab" aria-selected="${!i}" data-day="${d.day}">
      ${d.day}일차${d.date ? `<small>${esc(short(d.date).replace("-", "/"))}</small>` : ""}</button>`).join("");
  const panels = days.map((d, i) => {
    const stops = d.items.map((s) => {
      const leg = s.move ? `<li class="leg">이동 · ${esc(s.move)}</li>` : "";
      const badge = s.change ? `<span class="chg">${CHANGE_LABELS[s.change]}</span>` : "";
      // 장소 출처가 요금 출처와 같은 글이면 한 번만 보여 준다(📌 배지 중복 방지)
      const sameSource = s.source && s.admission && s.admission.source && s.admission.source.url === s.source.url;
      // 모델이 식사 비용도 입장료 칸에 넣는 경우가 있어 카드는 중립적인 '요금'으로 부른다
      const fee = s.admission ? `<div class="stop-fee"><span class="chip">요금 ${esc(s.admission.text)}</span>${s.admission.unsourced
        ? `<span class="warn">출처 없음, 사용자 확인 필요</span>` : sameSource ? "" : sourceLink(s.admission.source, "요금 출처")}</div>` : "";
      return `${leg}<li class="stop${s.change ? " changed" : ""}"><div class="stop-time">${esc(s.time)}</div><div class="stop-body">
        <div class="stop-place">${esc(s.place)}${badge}</div>${s.activity ? `<div class="stop-act">${esc(s.activity)}</div>` : ""}
        ${fee}${s.source ? `<div class="stop-src">${sourceLink(s.source)}</div>` : ""}</div></li>`;
    }).join("");
    return `<div class="itin-day" role="tabpanel" data-day="${d.day}"${i ? " hidden" : ""}>
      <div class="itin-day-title">${d.day}일차${d.date ? ` · ${esc(short(d.date).replace("-", "/"))}` : ""}</div><ol class="timeline">${stops}</ol>
      ${d.cost ? `<div class="day-cost">이 날 비용(대략) <b>${esc(d.cost)}</b></div>` : ""}</div>`;
  }).join("");
  const budget = b ? `<section class="itin-budget"><div class="itin-label">예산 요약 · 대략치 ${verdict}</div>
      <div class="b-total">총 ${esc(b.total)}</div>
      <div class="muted small">1인당 ${esc(b.per_person)}${b.budget ? ` · 예산 ${esc(b.budget)}` : ""}${b.whole_trip ? ` · 일차 무관(항공 등) ${esc(b.whole_trip)}` : ""}</div>
      ${b.rows.length ? `<ul class="b-rows">${b.rows.map((r) => `<li><span>${esc(r.name)} <small>${esc(r.unit)}${r.day ? ` · ${r.day}일차` : ""}</small></span>
        <span class="amt">${esc(r.total)}</span><small class="basis">${r.source ? sourceLink(r.source) : esc(r.basis)}</small></li>`).join("")}</ul>` : ""}</section>` : "";
  const adjust = b && b.verdict === "over" && it.adjustments.length
    ? `<section class="itin-notes"><div class="itin-label">예산 조정안</div><ul>${it.adjustments.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></section>` : "";
  const notes = it.notes.length ? `<section class="itin-notes"><div class="itin-label">참고</div><ul>${it.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></section>` : "";
  const sources = it.sources.length ? `<details class="itin-sources"><summary>출처 ${it.sources.length}개</summary><ul>${it.sources.map((s) =>
      `<li>${sourceLink(s, s.saved ? "저장 자료" : "출처")}</li>`).join("")}</ul></details>` : "";
  return `<article class="itin" data-title="${esc(it.title)}">
    <header class="itin-head"><div><div class="itin-title">${esc(it.title)}</div>
      <div class="itin-meta">${esc(period)} · ${esc(it.people)}명${it.origin ? ` · ${esc(it.origin)} 출발` : ""}</div></div>
      <button type="button" class="itin-save">이미지로 저장</button></header>
    ${changes}${assumptions}
    <div class="itin-tabs" role="tablist">${tabs}</div>${panels}
    ${budget}${adjust}${notes}${sources}
    <details class="itin-text"><summary>글로 보기</summary><div class="md">${renderMarkdown(body)}</div></details>
    <div class="itin-export-foot">여행 플래너에서 만든 일정 · 금액은 대략치이며 출처 없는 값은 확인이 필요합니다</div>
  </article>`;
}

function onItineraryClick(event) {
  /** 일정표 카드의 버튼(메시지 영역에서 한 번만 받는다): 일차 탭은 그 일차만 보여 주고, '이미지로 저장'은 PNG를 내려받는다. */
  const save = event.target.closest(".itin-save");
  if (save) return saveItineraryImage(save);
  const tab = event.target.closest(".itin-tab");
  if (!tab) return;
  const card = tab.closest(".itin");
  card.querySelectorAll(".itin-tab").forEach((t) => { t.classList.toggle("on", t === tab); t.setAttribute("aria-selected", String(t === tab)); });
  card.querySelectorAll(".itin-day").forEach((p) => { p.hidden = p.dataset.day !== tab.dataset.day; });
}

// ---------- 일정표 이미지 저장 (R5-6): html2canvas(2026-11-06 도입 승인, CDN)를 처음 저장할 때만 불러온다 ----------
const HTML2CANVAS_URL = "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js";
let html2canvasLoading = null;

function loadHtml2canvas() {
  /** html2canvas를 한 번만 불러온다. 실패하면 다음에 다시 시도할 수 있게 비운다. */
  if (window.html2canvas) return Promise.resolve(window.html2canvas);
  html2canvasLoading = html2canvasLoading || new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = HTML2CANVAS_URL;
    script.onload = () => resolve(window.html2canvas);
    script.onerror = () => { html2canvasLoading = null; reject(new Error("html2canvas 불러오기 실패")); };
    document.head.appendChild(script);
  });
  return html2canvasLoading;
}

async function saveItineraryImage(button) {
  /** 카드를 복제해 화면 밖에서 '내보내기 모양'(모든 일차 펼침, 탭·버튼·글로 보기·출처 목록 숨김)으로 바꾼 뒤 PNG로 내려받는다. */
  const card = button.closest(".itin");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "이미지 만드는 중…";
  const clone = card.cloneNode(true);
  clone.classList.add("export");
  clone.querySelectorAll(".itin-day").forEach((p) => { p.hidden = false; });
  const holder = document.createElement("div");
  holder.className = "itin-export-holder";
  holder.appendChild(clone);
  document.body.appendChild(holder);
  try {
    const html2canvas = await loadHtml2canvas();
    const canvas = await html2canvas(clone, { scale: 2, backgroundColor: "#FFFFFF", useCORS: true, logging: false });
    const link = document.createElement("a");
    link.href = canvas.toDataURL("image/png");
    link.download = `${(card.dataset.title || "여행 일정").replace(/[\\/:*?"<>|]/g, " ").trim()}.png`;
    link.click();
  } catch (err) {
    alertInChat("일정표 이미지를 만들지 못했습니다. 인터넷 연결을 확인하고 잠시 후 다시 눌러 주세요.");
  } finally {
    holder.remove();
    button.disabled = false;
    button.textContent = label;
  }
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
  $("#messages").addEventListener("click", onItineraryClick);
  $("#composer").addEventListener("submit", (e) => {  // 버튼: 답변 중이면 중지, 아니면 보내기 (Enter는 보내기만)
    e.preventDefault();
    if (isRunning(state.current)) stopCurrent(); else sendMessage();
  });
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
