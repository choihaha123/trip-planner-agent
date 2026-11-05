# threads.py - 웹 사용자별 대화 목록 (docs/SPEC.md 2.10)
# 대화 내용(메시지)은 에이전트 체크포인터(storage/checkpoints.sqlite)에 thread_id 단위로 저장되고,
# 여기서는 화면 사이드바에 필요한 목록 정보(소유자·제목·시각)만 storage/web.sqlite 의 threads 표에 둔다.
# 모든 함수는 user_id를 받아 그 사용자의 대화만 다룬다(남의 대화는 없는 것과 같게 처리).
import logging
import re
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from src import config
from src.agent import new_thread_id
from src.web.auth import connect as connect_web_db

logger = logging.getLogger(__name__)


def connect(db_path=None):
    """웹 DB에 연결하고 threads 표가 없으면 만든다."""
    conn = connect_web_db(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS threads (
        thread_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    return conn


def _now() -> str:
    """설정 시간대의 현재 시각 ISO 문자열."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(timespec="seconds")


def make_title(message: str) -> str:
    """첫 질문으로 대화 제목을 만든다: 줄바꿈·연속 공백을 하나로, WEB_TITLE_MAX_CHARS자를 넘으면 자르고 '…'."""
    text = re.sub(r"\s+", " ", message or "").strip()
    limit = config.WEB_TITLE_MAX_CHARS
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def create_thread(user_id: str, db_path=None) -> dict:
    """새 대화를 만들고 {thread_id, title, updated_at}을 돌려준다. 제목은 첫 메시지 때 정한다."""
    thread = {"thread_id": new_thread_id(), "title": "", "updated_at": _now()}
    with closing(connect(db_path)) as conn, conn:
        conn.execute("INSERT INTO threads (thread_id, user_id, title, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                     (thread["thread_id"], user_id, thread["updated_at"], thread["updated_at"]))
    return thread


def list_threads(user_id: str, db_path=None) -> list[dict]:
    """사용자의 대화 목록을 최근 사용 순으로 돌려준다."""
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT thread_id, title, updated_at FROM threads WHERE user_id = ? "
                            "ORDER BY updated_at DESC, created_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_thread(user_id: str, thread_id: str, db_path=None) -> dict | None:
    """사용자의 대화 하나를 돌려준다. 없거나 남의 대화면 None(존재 여부를 드러내지 않는다)."""
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT thread_id, title, updated_at FROM threads WHERE thread_id = ? AND user_id = ?",
                           (thread_id, user_id)).fetchone()
    return dict(row) if row else None


def touch_thread(user_id: str, thread_id: str, first_message: str = "", db_path=None) -> str:
    """메시지를 보낼 때 최근 사용 시각을 갱신하고, 제목이 없으면 첫 메시지로 정한다. 현재 제목을 돌려준다."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute("UPDATE threads SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END "
                     "WHERE thread_id = ? AND user_id = ?", (_now(), make_title(first_message), thread_id, user_id))
        row = conn.execute("SELECT title FROM threads WHERE thread_id = ? AND user_id = ?", (thread_id, user_id)).fetchone()
    return row["title"] if row else ""


def delete_thread(user_id: str, thread_id: str, checkpointer=None, db_path=None) -> bool:
    """사용자의 대화를 목록에서 지우고, checkpointer를 주면 대화 기록(메시지)도 지운다. 지웠으면 True."""
    with closing(connect(db_path)) as conn, conn:
        deleted = conn.execute("DELETE FROM threads WHERE thread_id = ? AND user_id = ?", (thread_id, user_id)).rowcount
    if deleted and checkpointer is not None:
        checkpointer.delete_thread(thread_id)
    if deleted:
        logger.info("웹 대화 삭제", extra={"event": "web_thread_deleted", "data": {"user_id": user_id, "thread_id": thread_id}})
    return bool(deleted)
