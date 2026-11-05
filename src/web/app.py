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
from src.middleware import SAVED_STATUSES, build_url_notice, extract_urls, process_urls
from src.retriever import delete_user_source, list_user_pages
from src.web import auth, threads

logger = logging.getLogger(__name__)

# 에이전트는 프로세스에서 하나만 만들고, 호출은 한 번에 하나씩 처리한다(대화 기록·벡터DB·기억 저장소가
# CLI 한 명 사용을 전제로 만들어져 있어, 동시 호출 안전성을 점검하기 전까지 잠금으로 순서를 지킨다).
_agent = None
_checkpointer = None
_agent_init_lock = threading.Lock()
AGENT_LOCK = threading.Lock()

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


def history_messages(messages: list) -> list[dict]:
    """체크포인트 메시지를 화면용 [{role, content}]으로 바꾼다. 턴마다 사용자 메시지와 그 턴의 마지막 메시지(최종 답변,
    일정표면 render_itinerary 결과)만 보여 주고, 중간의 도구 호출·결과는 뺀다."""
    shown, turn_last = [], None
    for m in messages:
        if getattr(m, "type", "") == "human":
            if turn_last is not None:
                shown.append({"role": "assistant", "content": get_text(turn_last)})
            shown.append({"role": "user", "content": get_text(m)})
            turn_last = None
        else:
            turn_last = m
    if turn_last is not None:
        shown.append({"role": "assistant", "content": get_text(turn_last)})
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
    """내 대화 목록(최근 사용 순)."""
    return threads.list_threads(user["user_id"])


@app.post("/api/threads")
def create_thread(user: dict = Depends(current_user)) -> dict:
    """새 대화를 만든다."""
    return threads.create_thread(user["user_id"])


@app.delete("/api/threads/{thread_id}")
def delete_thread(thread_id: str, user: dict = Depends(current_user)) -> dict:
    """내 대화를 목록과 대화 기록에서 지운다."""
    owned_thread(user, thread_id)
    _, checkpointer = get_agent()
    with AGENT_LOCK:  # 답변 중인 대화를 지우다 기록이 엇갈리지 않게 에이전트 호출과 순서를 맞춘다
        threads.delete_thread(user["user_id"], thread_id, checkpointer)
    return {"ok": True}


@app.get("/api/threads/{thread_id}/messages")
def thread_messages(thread_id: str, user: dict = Depends(current_user)) -> dict:
    """대화의 지난 메시지(화면용)."""
    thread = owned_thread(user, thread_id)
    agent, _ = get_agent()
    state = agent.get_state(run_config(thread_id, user["user_id"]))
    return {"thread": thread, "messages": history_messages((state.values or {}).get("messages", []))}


@app.post("/api/threads/{thread_id}/messages")
def send_message(thread_id: str, body: MessageIn, user: dict = Depends(current_user)) -> StreamingResponse:
    """메시지를 보내고 진행 단계(progress), 답변 초안 글자(delta·draft_reset), 최종 답변(done) 또는 오류(error)를 SSE로 돌려준다."""
    owned_thread(user, thread_id)
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="메시지가 비어 있습니다.")
    if len(content) > config.WEB_MESSAGE_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"메시지는 {config.WEB_MESSAGE_MAX_CHARS}자까지 보낼 수 있습니다.")
    user_id = user["user_id"]
    agent, _ = get_agent()

    def events():
        """잠금을 얻을 때까지 '대기 중'을 보내고, 에이전트를 단계별로 실행하며 진행 단계와 답변 글자를 보낸 뒤 최종 답변을 보낸다.
        글자(delta)는 답변 모델(model 노드)이 만드는 텍스트만이다(판정 모델·도구 인자 제외). 최종 답변은 턴 끝 처리
        (링크 안내·📌·가격 출처·기억 알림)가 반영된 저장 상태라 초안과 다를 수 있어, 화면은 done이 오면 초안을 바꿔 끼운다."""
        while not AGENT_LOCK.acquire(timeout=config.WEB_AGENT_WAIT_SECONDS):
            yield sse("progress", {"stage": "다른 요청을 처리하는 중이라 잠시 기다리는 중"})
        try:
            title = threads.touch_thread(user_id, thread_id, content)
            urls, _ = extract_urls(content)
            yield sse("progress", {"stage": f"링크 {len(urls)}개 확인 중" if urls else "질문 확인 중", "title": title})
            config_ = run_config(thread_id, user_id)
            writing = False  # 이번 모델 호출에서 글자를 보내기 시작했는지
            for mode, item in agent.stream({"messages": [{"role": "user", "content": content}]}, config_,
                                           context=AgentContext(user_id=user_id, channel="web"),
                                           stream_mode=["updates", "messages"]):
                if mode == "messages":
                    chunk, meta = item
                    text = draft_text(chunk) if meta.get("langgraph_node") == "model" else ""
                    if text:
                        if not writing:
                            writing = True
                            yield sse("progress", {"stage": "답변 작성 중"})
                        yield sse("delta", {"text": text})
                    continue
                if calls_tools(item):  # 도구를 부르기 전의 짧은 글('찾아볼게요')은 답변이 아니므로 초안에서 지운다
                    if writing:
                        yield sse("draft_reset", {})
                    writing = False
                for stage in progress_stages(item):
                    if stage != "답변 정리 중":  # 글자 스트리밍 중이면 '답변 작성 중'이 이미 표시된다
                        yield sse("progress", {"stage": stage})
            final = agent.get_state(config_).values["messages"][-1]
            yield sse("done", {"answer": get_text(final), "title": title})
        except Exception as e:
            logger.exception("웹 답변 실패", extra={"event": "web_agent_error", "data": {"thread_id": thread_id}})
            yield sse("error", {"message": error_message(e)})
        finally:
            AGENT_LOCK.release()

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


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
