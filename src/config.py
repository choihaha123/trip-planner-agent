# config.py - 설정값, .env 읽기, 로그 설정 (docs/SPEC.md 1.5)
# 다른 파일은 설정값을 여기서 가져다 쓴다. 비밀 값은 .env 에만 둔다.
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ---------- 모델 (Amazon Bedrock) ----------
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
BEDROCK_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
MODEL_TEMPERATURE = 0
# 기본 모델이 토큰 한도(ThrottlingException)에 걸리면 순서대로 시도할 대체 모델. "모델ID@리전"을 쉼표로 구분.
# 한도는 모델·리전·프로필 조합별이라, 같은 모델을 다른 리전/global 프로필로 먼저 시도한다.
BEDROCK_FALLBACK_MODELS = [
    item.strip() for item in os.getenv(
        "BEDROCK_FALLBACK_MODELS",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0@us-west-2,"
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0@us-east-1,"
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0@ap-northeast-2,"
        "us.anthropic.claude-sonnet-4-6@us-east-1,"
        "us.anthropic.claude-haiku-4-5-20251001-v1:0@us-east-1",
    ).split(",") if item.strip()
]
BEDROCK_MAX_RETRIES = int(os.getenv("BEDROCK_MAX_RETRIES", "1"))  # 모델 하나당 재시도 횟수 (넘으면 다음 모델로)

# ---------- 사용자·대화 기록 (SPEC 1.3, 2.7) ----------
DEFAULT_USER_ID = "default"
RESERVED_USER_PREFIX = "__"    # 시스템 예약 user_id 접두어 (예: 기본 KB "__kb__")
CHECKPOINT_DB = PROJECT_ROOT / os.getenv("CHECKPOINT_DB", "storage/checkpoints.sqlite")

# ---------- RAG 저장소 (SPEC 1.4, 2.7) ----------
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
CHROMA_DIR = PROJECT_ROOT / os.getenv("CHROMA_DIR", "storage/chroma")
KB_COLLECTION = "travel_kb"            # 기본 KB 컬렉션 (공용, 읽기 전용)
USER_COLLECTION = "user_pages"         # 사용자 수집 컬렉션 (user_id로 분리)
KB_USER_ID = "__kb__"                  # 기본 KB 청크의 user_id 표시값 (예약값)
KB_DATA_DIR = PROJECT_ROOT / "data" / "kb"  # 기본 KB 원본 문서 폴더 (src/preprocess/build_kb.py가 읽음)
CHUNK_SIZE = 800                       # 청크 최대 글자 수
CHUNK_OVERLAP = 100                    # 청크 간 겹치는 글자 수
RAG_TOP_K = 8                          # 검색 후보 청크 수 (관련성 판정으로 걸러지므로 넉넉히)
EMBED_MAX_WORKERS = 8                  # 청크 임베딩 동시 호출 수 (Titan은 한 호출에 한 텍스트)

# ---------- 평가 (docs/EVALUATION.md 5, 6) ----------
EVAL_JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0@us-east-1")  # '~' 항목 채점
EVAL_PASS_RATE = 0.8           # 전체 합격 비율 (guardrail은 전부 통과해야 함)
EVAL_FIXTURE_PORT = 8765       # 평가용 테스트 페이지(evaluation/fixtures) 로컬 웹서버 포트

# ---------- 날짜 처리 (SPEC 2.4) ----------
TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Seoul")

# ---------- 기능 규칙 (SPEC 1.5) ----------
RELEVANCE_THRESHOLD = 0.5      # URL 관련도 임계값 (이상이면 통과)
BUDGET_TOLERANCE = 0.10        # 예산 근접 허용폭 (±10%)
DEFAULT_PARTY_SIZE = 1         # 정보 부족 시 기본 인원
DEFAULT_ORIGIN = "인천"         # 정보 부족 시 기본 출발지
BASE_CURRENCY = "KRW"          # 기준 통화
PEOPLE_PER_ROOM = 2            # 객실당 인원

# ---------- 외부 API provider 선택 (SPEC 1.3, 2.4, 2.5) ----------
WEB_SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "tavily")
WEATHER_PROVIDER = os.getenv("WEATHER_PROVIDER", "open-meteo")
EXCHANGE_PROVIDER = os.getenv("EXCHANGE_PROVIDER", "open-er-api")
WEB_SEARCH_MAX_RESULTS = 5     # 웹 검색 결과 수 (Tavily 키는 .env 의 TAVILY_API_KEY)
NO_SOURCE_LABEL = "출처 없음, 사용자 확인 필요"  # URL 출처 없는 LLM 일반지식 표시 (SERVICE 2.6)
HTTP_TIMEOUT_SECONDS = 10      # 날씨·환율 API 호출 타임아웃 (HTTP 클라이언트: httpx)
EXCHANGE_CACHE_SECONDS = 3600  # 환율 캐시 유지 시간 (1시간)
# ---------- URL 수집 한계 (SERVICE 2.1, OPEN_ISSUES #3) ----------
MAX_URLS_PER_MESSAGE = 5       # 한 메시지에서 수집할 최대 URL 수 (초과분은 건너뛰고 사용자에게 알림, URL은 동시 처리)
COLLECT_TIMEOUT_SECONDS = 15   # 페이지 수집 타임아웃
COLLECT_MAX_CHARS = 50_000     # 본문 최대 글자 수 (넘으면 앞부분만 저장)
COLLECT_MIN_CHARS = 200        # 본문 최소 글자 수 (미만이면 '본문 없음' 수집 실패)
# 관련도 판정 (SERVICE 2.1, 2.8): 빠른 모델로 본문 앞부분만 판정. 한도 초과 시 대체 모델("모델ID@리전")을 순서대로 시도
RELEVANCE_MODEL = os.getenv("RELEVANCE_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0@us-east-1")
RELEVANCE_FALLBACK_MODELS = [
    item.strip() for item in os.getenv(
        "RELEVANCE_FALLBACK_MODELS",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0@us-west-2,"
        "global.anthropic.claude-haiku-4-5-20251001-v1:0@us-east-1",
    ).split(",") if item.strip()
]
RELEVANCE_INPUT_CHARS = 3000   # 판정 LLM에 넣을 본문 앞부분 글자 수
COLLECT_USER_AGENT = os.getenv("COLLECT_USER_AGENT", "Mozilla/5.0 (compatible; miniPJT-travel-agent)")
os.environ.setdefault("USER_AGENT", COLLECT_USER_AGENT)  # WebBaseLoader가 읽는 값

WEATHER_FORECAST_DAYS = 16     # 예보 가능 범위: 현지 오늘 포함 16일 (Open-Meteo 한계, SERVICE 2.4 '약 2주')
WEATHER_DEFAULT_DAYS = 7       # 날짜를 주지 않으면 오늘부터 며칠을 조회할지

# ---------- 로그 (logs/app.jsonl, JSON Lines) ----------
LOG_FILE = PROJECT_ROOT / os.getenv("LOG_FILE", "logs/app.jsonl")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
TRACE_LOG_FILE = PROJECT_ROOT / os.getenv("TRACE_LOG_FILE", "logs/trace.jsonl")  # 실행 추적(src/tracing.py) 전용
TRACE_LOGGER_NAME = "src.tracing"
TRACE_TEXT_LIMIT = 200         # 추적 기록에 남길 입력·출력 본문 최대 글자 수


class JsonLineFormatter(logging.Formatter):
    """로그 레코드 하나를 JSON 한 줄로 만든다. extra 로 넘긴 event·data 필드도 함께 담는다."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "data"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """루트 로거에 JSON Lines 파일 핸들러를 붙인다. 여러 번 불러도 한 번만 설정한다."""
    root = logging.getLogger()
    if any(getattr(h, "_app_handler", False) for h in root.handlers):
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(JsonLineFormatter())
    handler._app_handler = True
    root.addHandler(handler)
    root.setLevel(LOG_LEVEL)

    # 실행 추적은 별도 파일에만 남기고 app 로그로 올려 보내지 않는다.
    trace_logger = logging.getLogger(TRACE_LOGGER_NAME)
    trace_logger.propagate = False
    trace_logger.setLevel(logging.INFO)
    TRACE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    trace_handler = logging.FileHandler(TRACE_LOG_FILE, encoding="utf-8")
    trace_handler.setFormatter(JsonLineFormatter())
    trace_logger.addHandler(trace_handler)

    # 외부 라이브러리의 상세 로그는 줄인다. (모델 호출 기록은 실행 추적이 대신한다)
    for noisy in ("botocore", "urllib3", "httpx", "httpcore", "langchain_aws"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
