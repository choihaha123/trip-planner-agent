# app.py - 웹 FastAPI 앱 (docs/SPEC.md 2.10)
# 실행: uvicorn src.web.app:app  (프로젝트 루트에서)
# 계정·세션은 src/web/auth.py, 대화 목록은 src/web/threads.py, 에이전트 코어는 src/agent.py,
# 저장 링크는 src/retriever.py·src/middleware.py, 장기 기억은 src/memory.py 를 CLI와 같이 그대로 쓴다.
# 로그인한 계정의 내부 ID가 에이전트 user_id 이며, 모든 API는 쿠키의 세션으로 사용자를 정하고 요청 내용으로 바꿀 수 없다.
import json
import logging
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from main import error_message
from src import config
from src.agent import AgentContext, build_agent, get_text, open_checkpointer, run_config
from src.memory import delete_memory, get_memory
from src.middleware import SAVED_STATUSES, build_url_notice, extract_urls, process_urls, saved_references_this_turn
from src.tools import itinerary_view
from src.retriever import delete_user_source, list_user_pages
from src.web import auth, runs, threads

logger = logging.getLogger(__name__)

# 에이전트는 프로세스에서 하나만 만들고 여러 대화가 함께 쓴다. 답변은 대화마다 백그라운드로 돌린다(runs.py, R4).
# (2026-11-06 점검: 체크포인터·기억 저장소는 내부 잠금, 모델·미들웨어는 요청별 상태가 없어 대화 단위 동시 실행이 안전하다)
_agent = None
_checkpointer = None
_agent_init_lock = threading.Lock()

# 진행 단계 표시: 모델이 부른 도구 → 화면 문구 (SERVICE 2.10)
TOOL_STAGES = {
    "search_travel_info": "자료 검색 중", "web_search": "웹 검색 중", "get_weather": "날씨 확인 중",
    "get_exchange_rate": "환율 확인 중", "calculate_budget": "예산 계산 중", "resolve_dates": "날짜 계산 중",
    "check_trip_info": "여행 정보 확인 중", "render_itinerary": "일정표 작성 중",
}


def get_agent():
    """웹에서 쓸 (에이전트, 체크포인터)를 돌려준다. 처음 부를 때 한 번만 만든다. 대화 기록은 CLI와 같은 DB를 쓴다."""
    global _agent, _checkpointer
    with _agent_init_lock:
        if _agent is None:
            _checkpointer = open_checkpointer()
            _agent = build_agent(_checkpointer)
        return _agent, _checkpointer

config.setup_logging()
app = FastAPI(title="여행 계획 에이전트")

# 화면: 빌드 도구 없는 HTML/CSS/JS를 같은 앱이 제공한다 (SERVICE 2.10)
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """웹 화면(한 페이지 앱)을 돌려준다. 로그인 여부는 화면이 /api/me로 확인한다."""
    return FileResponse(STATIC_DIR / "index.html")


class Credentials(BaseModel):
    """회원가입·로그인 입력."""
    email: str
    password: str


def current_user(request: Request) -> dict:
    """쿠키의 세션 토큰으로 로그인한 사용자를 찾는다. 없거나 만료면 401."""
    user = auth.user_from_token(request.cookies.get(config.WEB_SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    """세션 토큰을 HttpOnly 쿠키로 내려준다(자바스크립트에서 읽을 수 없게, 같은 사이트 요청에만)."""
    response.set_cookie(config.WEB_SESSION_COOKIE, token, max_age=config.WEB_SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=config.WEB_COOKIE_SECURE)


@app.post("/api/signup")
def signup(body: Credentials, response: Response) -> dict:
    """회원가입 후 바로 로그인 상태로 만든다."""
    try:
        auth.signup(body.email, body.password)
        token = auth.login(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    _set_session_cookie(response, token)
    return {"email": auth.normalize_email(body.email)}


@app.post("/api/login")
def login(body: Credentials, response: Response) -> dict:
    """로그인. 실패하면 이메일 존재 여부와 상관없이 같은 문구로 401."""
    try:
        token = auth.login(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from None
    _set_session_cookie(response, token)
    return {"email": auth.normalize_email(body.email)}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    """세션을 지우고 쿠키를 없앤다."""
    auth.logout(request.cookies.get(config.WEB_SESSION_COOKIE))
    response.delete_cookie(config.WEB_SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    """로그인한 사용자의 이메일. 내부 user_id는 화면에 내보내지 않는다."""
    return {"email": user["email"]}


# ---------- 대화 (SERVICE 2.10) ----------
class MessageIn(BaseModel):
    """보낼 메시지."""
    content: str


def owned_thread(user: dict, thread_id: str) -> dict:
    """로그인한 사용자의 대화를 돌려준다. 없거나 남의 대화면 같은 404(존재 여부를 드러내지 않음)."""
    thread = threads.get_thread(user["user_id"], thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
    return thread


def answer_view(message, messages: list) -> dict:
    """턴의 최종 답변 하나를 화면용 {role, content[, itinerary]}로 만든다. 일정표(render_itinerary 결과)면 카드용 데이터를 붙인다
    (R5-5: 📌는 대화 전체 검색 결과의 저장 자료 기준). 줄글(content)은 '글로 보기'·복사용으로 그대로 둔다."""
    view = {"role": "assistant", "content": get_text(message)}
    if getattr(message, "name", "") == "render_itinerary":
        card = itinerary_view(getattr(message, "artifact", None), saved_references_this_turn(messages, whole_conversation=True))
        if card:
            view["itinerary"] = card
    return view


def history_messages(messages: list) -> list[dict]:
    """체크포인트 메시지를 화면용 [{role, content[, itinerary]}]으로 바꾼다. 턴마다 사용자 메시지와 그 턴의 마지막 메시지(최종 답변,
    일정표면 render_itinerary 결과)만 보여 주고, 중간의 도구 호출·결과는 뺀다."""
    shown, turn_last = [], None
    for m in messages:
        if getattr(m, "type", "") == "human":
            if turn_last is not None:
                shown.append(answer_view(turn_last, messages))
            shown.append({"role": "user", "content": get_text(m)})
            turn_last = None
        else:
            turn_last = m
    if turn_last is not None:
        shown.append(answer_view(turn_last, messages))
    return shown


def progress_stages(update: dict) -> list[str]:
    """stream 이벤트 하나(노드 → 상태 변경)에서 화면에 보여 줄 진행 단계를 뽑는다."""
    stages = []
    for node, change in (update or {}).items():
        if node != "model" or not isinstance(change, dict):
            continue
        for m in change.get("messages", []):
            for call in getattr(m, "tool_calls", None) or []:
                stages.append(TOOL_STAGES.get(call["name"], "자료 확인 중"))
            if not getattr(m, "tool_calls", None):
                stages.append("답변 정리 중")
    return list(dict.fromkeys(stages))  # 순서 유지하며 중복 제거


def draft_text(chunk) -> str:
    """스트리밍 조각(AIMessageChunk)에서 화면에 보여 줄 글자만 뽑는다. 도구 호출 인자(JSON) 조각은 뺀다."""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def calls_tools(update: dict) -> bool:
    """stream 이벤트가 '답변 모델이 도구를 부르기로 한 것'인지(그 전에 흘러나온 글자는 초안에서 지운다)."""
    change = (update or {}).get("model")
    return isinstance(change, dict) and any(getattr(m, "tool_calls", None) for m in change.get("messages", []))


def sse(event: str, data: dict) -> str:
    """서버 전송 이벤트(SSE) 한 건의 문자열."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/threads")
def list_threads(user: dict = Depends(current_user)) -> list[dict]:
    """내 대화 목록(최근 사용 순). running은 지금 답변 중인 대화(화면이 다시 열 때 이어 본다)."""
    running = runs.running_ids()
    return [{**t, "running": t["thread_id"] in running} for t in threads.list_threads(user["user_id"])]


@app.post("/api/threads")
def create_thread(user: dict = Depends(current_user)) -> dict:
    """새 대화를 만든다."""
    return threads.create_thread(user["user_id"])


@app.delete("/api/threads/{thread_id}")
def delete_thread(thread_id: str, user: dict = Depends(current_user)) -> dict:
    """내 대화를 목록과 대화 기록에서 지운다. 답변 중이면 먼저 멈추고 끝나기를 기다린다
    (지운 뒤에 진행 중이던 실행이 기록을 다시 쓰지 않게). 제때 멈추지 않으면 409."""
    owned_thread(user, thread_id)
    _, checkpointer = get_agent()
    runs.stop_run(thread_id)
    if not runs.wait_finished(thread_id, config.WEB_DELETE_WAIT_SECONDS):
        raise HTTPException(status_code=409, detail="답변을 멈추는 중입니다. 잠시 후 다시 지워 주세요.")
    threads.delete_thread(user["user_id"], thread_id, checkpointer)
    return {"ok": True}


@app.get("/api/threads/{thread_id}/messages")
def thread_messages(thread_id: str, user: dict = Depends(current_user)) -> dict:
    """대화의 지난 메시지(화면용). 답변 중이면 이번 사용자 메시지까지만 보여 준다(진행 중인 도구 단계는 /stream으로)."""
    thread = owned_thread(user, thread_id)
    agent, _ = get_agent()
    messages = (agent.get_state(run_config(thread_id, user["user_id"])).values or {}).get("messages", [])
    run = runs.get_run(thread_id)
    if run is not None:
        last_human = max((i for i, m in enumerate(messages) if getattr(m, "type", "") == "human"), default=-1)
        if last_human >= 0 and get_text(messages[last_human]) == run.content:
            messages = messages[:last_human + 1]
    return {"thread": thread, "messages": history_messages(messages), "running": run is not None}


@app.post("/api/threads/{thread_id}/messages")
def send_message(thread_id: str, body: MessageIn, user: dict = Depends(current_user)) -> StreamingResponse:
    """메시지를 보내 이 대화의 답변을 백그라운드로 시작하고, 그 진행을 SSE로 돌려준다(run_events).
    이벤트: progress(진행 단계), delta·draft_reset(답변 초안 글자), 끝은 done(최종 답변)·error·stopped 중 하나.
    연결이 끊겨도 답변은 끝까지 가고 /stream으로 다시 볼 수 있다. 이 대화가 이미 답변 중이면 409."""
    owned_thread(user, thread_id)
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="메시지가 비어 있습니다.")
    if len(content) > config.WEB_MESSAGE_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"메시지는 {config.WEB_MESSAGE_MAX_CHARS}자까지 보낼 수 있습니다.")
    user_id = user["user_id"]
    agent, _ = get_agent()

    def work(run: runs.Run) -> None:
        """에이전트를 단계별로 실행하며 진행 단계와 답변 글자를 쌓고, 끝나면 최종 답변을 쌓는다. 멈춤 신호가 오면 다음 이벤트에서 멈춘다.
        글자(delta)는 답변 모델(model 노드)이 만드는 텍스트만이다(판정 모델·도구 인자 제외). 최종 답변은 턴 끝 처리
        (링크 안내·📌·가격 출처·기억 알림)가 반영된 저장 상태라 초안과 다를 수 있어, 화면은 done이 오면 초안을 바꿔 끼운다.
        중간에 멈춘 턴의 결과 없는 도구 호출은 다음 턴에 보정된다(T1-3)."""
        try:
            title = threads.touch_thread(user_id, thread_id, content)
            urls, _ = extract_urls(content)
            run.emit("progress", {"stage": f"링크 {len(urls)}개 확인 중" if urls else "질문 확인 중", "title": title})
            config_ = run_config(thread_id, user_id)
            writing = False  # 이번 모델 호출에서 글자를 보내기 시작했는지
            stream = agent.stream({"messages": [{"role": "user", "content": content}]}, config_,
                                  context=AgentContext(user_id=user_id, channel="web"), stream_mode=["updates", "messages"])
            try:
                for mode, item in stream:
                    if run.cancel.is_set():
                        run.emit("stopped", {"message": runs.STOPPED_MESSAGE, "title": title})
                        return
                    if mode == "messages":
                        chunk, meta = item
                        text = draft_text(chunk) if meta.get("langgraph_node") == "model" else ""
                        if text:
                            if not writing:
                                writing = True
                                run.emit("progress", {"stage": "답변 작성 중"})
                            run.emit("delta", {"text": text})
                        continue
                    if calls_tools(item):  # 도구를 부르기 전의 짧은 글('찾아볼게요')은 답변이 아니므로 초안에서 지운다
                        if writing:
                            run.emit("draft_reset", {})
                        writing = False
                    for stage in progress_stages(item):
                        if stage != "답변 정리 중":  # 글자 스트리밍 중이면 '답변 작성 중'이 이미 표시된다
                            run.emit("progress", {"stage": stage})
            finally:
                stream.close()  # 멈췄을 때 그래프 실행을 바로 정리한다
            messages = agent.get_state(config_).values["messages"]
            final = answer_view(messages[-1], messages)
            run.emit("done", {"answer": final["content"], "title": title,
                              **({"itinerary": final["itinerary"]} if "itinerary" in final else {})})
        except Exception as e:
            logger.exception("웹 답변 실패", extra={"event": "web_agent_error", "data": {"thread_id": thread_id}})
            run.emit("error", {"message": error_message(e)})

    try:
        run = runs.start_run(thread_id, user_id, content, work)
    except runs.RunBusy:
        raise HTTPException(status_code=409, detail="이 대화는 아직 답변 중입니다. 끝나거나 멈춘 뒤 보내 주세요.") from None
    return run_events(run)


def run_events(run: runs.Run) -> StreamingResponse:
    """실행의 이벤트를 처음부터 SSE로 내보낸다. 오래 조용하면 연결 유지 주석을 보낸다. 끊겨도 실행에는 영향이 없다."""
    def body():
        for item in run.subscribe():
            yield ": keepalive\n\n" if item is None else sse(*item)
    return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/threads/{thread_id}/stream")
def follow_run(thread_id: str, user: dict = Depends(current_user)) -> StreamingResponse:
    """답변 중인 대화의 진행을 처음부터 다시 받는다(새로고침·다른 대화를 보다 돌아왔을 때). 답변 중이 아니면 404."""
    owned_thread(user, thread_id)
    run = runs.get_run(thread_id)
    if run is None:
        raise HTTPException(status_code=404, detail="진행 중인 답변이 없습니다.")
    return run_events(run)


@app.post("/api/threads/{thread_id}/stop")
def stop_answer(thread_id: str, user: dict = Depends(current_user)) -> dict:
    """답변을 멈춘다(다음 단계에서 멈추고 stopped 이벤트를 보낸다). 답변 중이 아니었으면 stopped=False."""
    owned_thread(user, thread_id)
    return {"stopped": runs.stop_run(thread_id)}


# ---------- 저장한 링크 (SERVICE 2.10 링크 패널) ----------
# 대화와 같은 처리(process_urls: 수집 → 판정 → 저장)와 같은 안내 문구(build_url_notice)를 쓴다.
# Chroma 연결은 프로세스에서 하나를 같이 쓰고 대화 중 URL도 여러 스레드로 저장하므로 에이전트 잠금은 걸지 않는다.
class LinksIn(BaseModel):
    """링크 패널에 붙여넣은 글(URL 하나 이상)."""
    text: str


def link_items(user_id: str) -> list[dict]:
    """화면용 저장 링크 목록(저장한 순서): url, title, saved_at(YYYY-MM-DD)."""
    return [{"url": p["source_url"], "title": p["title"], "saved_at": p["collected_at"][:10]}
            for p in list_user_pages(user_id)]


@app.get("/api/links")
def list_links(user: dict = Depends(current_user)) -> list[dict]:
    """내가 저장한 링크 목록."""
    return link_items(user["user_id"])


@app.post("/api/links")
def add_links(body: LinksIn, user: dict = Depends(current_user)) -> dict:
    """붙여넣은 글의 URL을 저장하고 '링크 처리 결과' 안내문(대화와 같은 문구)과 저장 후 목록을 돌려준다."""
    text = (body.text or "").strip()
    if len(text) > config.WEB_MESSAGE_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"한 번에 {config.WEB_MESSAGE_MAX_CHARS}자까지 넣을 수 있습니다.")
    urls, skipped = extract_urls(text)
    if not urls:
        raise HTTPException(status_code=400, detail="http:// 또는 https://로 시작하는 링크를 넣어 주세요.")
    outcomes = process_urls(urls, user["user_id"])
    return {"notice": build_url_notice(outcomes, skipped),
            "saved": sum(o.status in SAVED_STATUSES for o in outcomes),
            "links": link_items(user["user_id"])}


@app.delete("/api/links")
def delete_link(url: str, user: dict = Depends(current_user)) -> dict:
    """내가 저장한 링크 하나의 저장 내용 전체를 지운다. 내 목록에 없는 URL이면 404(남의 자료는 애초에 대상이 아님)."""
    if not any(p["source_url"] == url for p in list_user_pages(user["user_id"])):
        raise HTTPException(status_code=404, detail="저장한 링크를 찾을 수 없습니다.")
    delete_user_source(user["user_id"], url)
    return {"ok": True, "links": link_items(user["user_id"])}


# ---------- 장기 기억 (SERVICE 2.10 기억 패널, CLI /memory·/forget-memory와 같음) ----------
# 기억 저장소는 프로세스에서 연결 하나를 같이 쓰고(SqliteStore, 스레드 공유 허용) 에이전트도 같은 저장소에 쓰므로 잠금은 걸지 않는다.
def memory_entries(user_id: str) -> list[dict]:
    """화면용 기억 목록: field(항목 키), label(표시 이름), value. 순서는 설정 목록(MEMORY_FIELDS) 순서."""
    return [{"field": k, "label": config.MEMORY_FIELDS[k], "value": v} for k, v in get_memory(user_id).items()]


@app.get("/api/memory")
def list_memory(user: dict = Depends(current_user)) -> list[dict]:
    """내 장기 기억 목록."""
    return memory_entries(user["user_id"])


@app.delete("/api/memory/{field}")
def forget_memory(field: str, user: dict = Depends(current_user)) -> dict:
    """내 기억 항목 하나를 지운다. 없는 항목(또는 기억 항목이 아닌 이름)이면 404."""
    if field not in config.MEMORY_FIELDS or not delete_memory(user["user_id"], field):
        raise HTTPException(status_code=404, detail="기억한 정보를 찾을 수 없습니다.")
    return {"ok": True, "memory": memory_entries(user["user_id"])}
