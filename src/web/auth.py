# auth.py - 웹 회원가입·로그인·세션 (docs/SPEC.md 2.10)
# 계정은 storage/web.sqlite 의 users 표, 로그인 상태는 sessions 표에 저장한다.
# 비밀번호는 표준 라이브러리 hashlib.scrypt 로 해시하고, 세션은 무작위 토큰(secrets)을 HttpOnly 쿠키로 주고받는다.
# 에이전트 user_id 는 이메일이 아니라 가입 때 만든 내부 ID(u_ + 무작위)다.
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src import config

logger = logging.getLogger(__name__)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOGIN_FAILED = "이메일 또는 비밀번호가 맞지 않습니다."  # 이메일 존재 여부를 드러내지 않는 같은 문구


class AuthError(Exception):
    """회원가입·로그인 실패. 메시지는 사용자에게 그대로 보여 줄 문구다."""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """웹 DB에 연결하고 표가 없으면 만든다. 호출마다 새 연결을 쓰므로 여러 요청이 동시에 써도 안전하다."""
    path = Path(db_path or config.WEB_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id),
            expires_at TEXT NOT NULL);
    """)
    return conn


def _now() -> datetime:
    """설정 시간대의 현재 시각."""
    return datetime.now(ZoneInfo(config.TIMEZONE))


def normalize_email(email: str) -> str:
    """이메일을 비교용으로 정리한다(앞뒤 공백 제거, 소문자). 중복 판정은 이 값으로 한다."""
    return (email or "").strip().lower()


def hash_password(password: str, salt: bytes | None = None) -> str:
    """비밀번호를 scrypt 로 해시해 'scrypt$n$r$p$salt$hash' 문자열로 돌려준다. 원문은 저장하지 않는다."""
    salt = salt or secrets.token_bytes(16)
    p = config.PASSWORD_SCRYPT
    digest = hashlib.scrypt(password.encode(), salt=salt, n=p["n"], r=p["r"], p=p["p"], dklen=32)
    return f"scrypt${p['n']}${p['r']}${p['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """비밀번호가 저장된 해시와 맞는지 확인한다(저장 당시 비용 인자로 다시 계산, 시간 일정 비교)."""
    try:
        _, n, r, p, salt, digest = stored.split("$")
        again = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(again.hex(), digest)


def signup(email: str, password: str, db_path=None) -> str:
    """계정을 만들고 내부 user_id를 돌려준다. 이메일 형식·중복, 비밀번호 길이를 검사한다."""
    email = normalize_email(email)
    if not _EMAIL.match(email):
        raise AuthError("이메일 형식이 올바르지 않습니다.")
    if len(password or "") < config.PASSWORD_MIN_LENGTH:
        raise AuthError(f"비밀번호는 {config.PASSWORD_MIN_LENGTH}자 이상이어야 합니다.")
    user_id = f"u_{secrets.token_hex(6)}"
    with closing(connect(db_path)) as conn, conn:
        try:
            conn.execute("INSERT INTO users (user_id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                         (user_id, email, hash_password(password), _now().isoformat(timespec="seconds")))
        except sqlite3.IntegrityError:
            raise AuthError("이미 가입된 이메일입니다.") from None
    logger.info("회원가입", extra={"event": "web_signup", "data": {"user_id": user_id}})
    return user_id


def _token_hash(token: str) -> str:
    """세션 토큰은 DB에 해시로만 저장한다(DB가 새도 토큰을 쓸 수 없게)."""
    return hashlib.sha256(token.encode()).hexdigest()


def login(email: str, password: str, db_path=None) -> str:
    """이메일·비밀번호가 맞으면 새 세션 토큰을 만들어 돌려준다. 틀리면 같은 문구의 AuthError."""
    with closing(connect(db_path)) as conn, conn:
        row = conn.execute("SELECT user_id, password_hash FROM users WHERE email = ?",
                           (normalize_email(email),)).fetchone()
        if row is None or not verify_password(password or "", row["password_hash"]):
            logger.info("로그인 실패", extra={"event": "web_login_failed", "data": {}})
            raise AuthError(LOGIN_FAILED)
        token = secrets.token_urlsafe(32)
        expires = _now() + timedelta(days=config.WEB_SESSION_DAYS)
        conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     (_token_hash(token), row["user_id"], expires.isoformat(timespec="seconds")))
    logger.info("로그인", extra={"event": "web_login", "data": {"user_id": row["user_id"]}})
    return token


def user_from_token(token: str | None, db_path=None) -> dict | None:
    """세션 토큰으로 로그인한 사용자({user_id, email})를 찾는다. 없거나 만료면 None(만료 세션은 지운다)."""
    if not token:
        return None
    with closing(connect(db_path)) as conn, conn:
        row = conn.execute("SELECT s.user_id, s.expires_at, u.email FROM sessions s JOIN users u USING (user_id) "
                           "WHERE s.token_hash = ?", (_token_hash(token),)).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < _now():
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            return None
    return {"user_id": row["user_id"], "email": row["email"]}


def logout(token: str | None, db_path=None) -> None:
    """세션을 지운다. 없는 토큰이어도 오류 없이 넘어간다."""
    if token:
        with closing(connect(db_path)) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
