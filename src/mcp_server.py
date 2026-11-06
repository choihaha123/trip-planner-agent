# mcp_server.py - MCP 서버 진입점 (docs/SERVICE.md 2.11, docs/SPEC.md 2.11, TASKS Step 14)
# Claude Desktop 같은 MCP 클라이언트가 자식 프로세스로 띄워 stdio로 대화한다(로컬 실행).
# 실행: python src/mcp_server.py   (또는 프로젝트 루트에서 python -m src.mcp_server)
# 노출 도구(혼합): ask_trip_agent(에이전트를 통째로 써 CLI와 같은 답) + 보조 계산 도구 4개(파이썬 계산, Bedrock 미사용).
# 표준 출력은 MCP 메시지 전용이므로 print를 쓰지 않고, 로그는 config.setup_logging의 파일(logs/)로만 남긴다.
import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Literal

if __package__ in (None, ""):  # 파일로 직접 실행(python src/mcp_server.py)해도 src 패키지를 찾게 한다
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402

from src import config  # noqa: E402
from src.agent import (AgentContext, ThreadAccessError, build_agent, check_thread_access, get_text,  # noqa: E402
                       is_throttled, new_thread_id, open_checkpointer, run_config, validate_user_id)
from src.tools import (BudgetError, BudgetItem, ExchangeRateError, compute_budget, get_exchange_rate,  # noqa: E402
                       get_weather, render_budget, resolve_dates)
from src.web.app import progress_stages  # noqa: E402  (웹과 같은 진행 단계 문구)

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "여행 계획·조언 에이전트입니다. 여행 일정표, 예산, 날씨·환율, 여행지·교통·맛집 질문, 여행 글 링크 저장·참고는 "
    "ask_trip_agent에 사용자의 말을 그대로 넘기세요. 돌려받은 답(출처 '제목 + URL', 일정표 형식, '출처 없음' 표시, "
    "링크 처리 결과)은 바꾸거나 요약하지 말고 그대로 보여 주세요. 같은 여행을 이어서 물을 때는 답 끝의 대화 ID를 "
    "conversation_id로 넘기세요. 사용자가 금액·날짜만 간단히 계산해 달라고 할 때만 보조 도구"
    "(calculate_trip_budget, convert_currency, resolve_trip_dates, get_weather_forecast)를 쓰세요."
)
server = MCPServer(config.MCP_SERVER_NAME, instructions=INSTRUCTIONS)

# 에이전트는 프로세스에서 하나만 만들고(처음 부를 때), 대화 기록은 CLI·웹과 같은 DB를 쓴다.
_agent = None
_checkpointer = None
_agent_lock = threading.Lock()


def get_agent():
    """MCP에서 쓸 (에이전트, 체크포인터)를 돌려준다. 처음 부를 때 한 번만 만든다."""
    global _agent, _checkpointer
    with _agent_lock:
        if _agent is None:
            _checkpointer = open_checkpointer()
            _agent = build_agent(_checkpointer)
        return _agent, _checkpointer


def conversation_footer(thread_id: str) -> str:
    """답 끝에 붙이는 대화 ID 안내(클라이언트 모델이 다음 질문에 넘기게)."""
    return f"\n\n---\n대화 ID: {thread_id} (같은 여행을 이어서 물으려면 conversation_id에 이 값을 넣으세요)"


def agent_error_message(error: Exception) -> str:
    """에이전트 실패를 원인별로 안내한다(CLI의 /new 대신 MCP에 맞는 다음 행동)."""
    if is_throttled(error):
        return "지금 요청이 많아 답변하지 못했습니다. 잠시 후 같은 질문을 다시 보내 주세요."
    return "답변 중 오류가 발생했습니다. 같은 질문을 다시 보내 보시고, 계속 안 되면 conversation_id 없이 새 대화로 물어봐 주세요."


@server.tool()
async def ask_trip_agent(message: str, ctx: Context, conversation_id: str = "") -> str:
    """여행 계획·조언 에이전트에게 질문한다. 일정표, 예산, 날씨·환율, 여행지·교통·맛집 질문, 여행 글 링크 저장·참고에 쓴다.
    답에는 출처('제목 + URL')와 일정표 형식, 가격의 '출처 없음' 표시, 링크 처리 결과가 들어 있으니 바꾸지 말고 그대로 보여 준다.
    사용자의 말을 그대로 message로 넘긴다(링크도 그대로 넣으면 판정 후 저장한다).

    Args:
        message: 사용자의 질문이나 요청 원문
        conversation_id: 이어서 물을 때 이전 답 끝의 대화 ID. 새 대화면 비운다
    """
    text = (message or "").strip()
    if not text:
        return "질문이 비어 있습니다. 여행 질문이나 요청을 보내 주세요."
    if len(text) > config.MCP_MESSAGE_MAX_CHARS:
        return f"질문은 {config.MCP_MESSAGE_MAX_CHARS}자까지 보낼 수 있습니다."
    user_id = config.MCP_USER_ID  # 서버 설정으로 고정(도구 인자로 바꿀 수 없음, SERVICE 2.8)
    agent, checkpointer = get_agent()
    thread_id = (conversation_id or "").strip()
    if thread_id:
        try:
            check_thread_access(checkpointer, thread_id, user_id)
        except ThreadAccessError:
            return (f"대화 '{thread_id}'를 열 수 없습니다. 이 연결에서 받은 대화 ID인지 확인하거나, "
                    "conversation_id 없이 새 대화로 물어봐 주세요.")
    else:
        thread_id = new_thread_id()

    loop = asyncio.get_running_loop()
    stages: asyncio.Queue = asyncio.Queue()
    config_ = run_config(thread_id, user_id)

    def run():
        """에이전트를 단계별로 실행하며 진행 단계를 큐로 보내고, 턴 끝 처리가 반영된 최종 메시지를 돌려준다."""
        for update in agent.stream({"messages": [{"role": "user", "content": text}]}, config_,
                                   context=AgentContext(user_id=user_id, channel="mcp"), stream_mode="updates"):
            for stage in progress_stages(update):
                loop.call_soon_threadsafe(stages.put_nowait, stage)
        return agent.get_state(config_).values["messages"][-1]

    task = asyncio.ensure_future(asyncio.to_thread(run))
    step = 0
    # 일정표처럼 오래 걸리는 답도 클라이언트가 진행 상황을 받도록, 단계가 오거나 잠시 조용할 때마다 진행 알림을 보낸다.
    while not task.done():
        try:
            stage = await asyncio.wait_for(stages.get(), timeout=config.WEB_AGENT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            stage = "답변을 만드는 중"
        if task.done() and stage == "답변을 만드는 중":
            break
        step += 1
        await ctx.report_progress(step, message=stage)
    try:
        final = await task
    except Exception as e:
        logger.exception("MCP 답변 실패", extra={"event": "mcp_agent_error", "data": {"thread_id": thread_id}})
        return agent_error_message(e)
    logger.info("MCP 답변", extra={"event": "mcp_answer", "data": {"thread_id": thread_id, "user_id": user_id}})
    return get_text(final) + conversation_footer(thread_id)


# ---------- 보조 계산 도구: CLI와 같은 파이썬 함수, Bedrock을 쓰지 않는다 ----------
@server.tool()
def calculate_trip_budget(items: list[BudgetItem], people: int, nights: int, budget: float | None = None,
                          budget_basis: Literal["total", "per_person"] = "total", currency: str = config.BASE_CURRENCY) -> str:
    """여행 예산을 파이썬으로 정확히 계산한다(항목별·일자별 합계, 일행 총액·1인당, 예산 안/근접/초과 판정).
    금액·단위가 정해진 단순 계산에만 쓴다. 일정표와 함께 짜려면 ask_trip_agent를 쓴다.
    비용 근거(basis)는 user_input(사용자가 준 값) 또는 llm_estimate(추정치)로 넣는다. 숙박은 unit=per_room_per_night(1박 단가).

    Args:
        items: 비용 항목 목록 (이름·분류·최소/최대 금액·통화·단위·근거)
        people: 인원 수
        nights: 박 수 (당일치기는 0)
        budget: 예산 금액 (없으면 비움, 판정 생략)
        budget_basis: 예산 기준 total=일행 총액, per_person=1인
        currency: 표시 통화 (기본 KRW)
    """
    # 검색 출처 ID를 풀 대화 상태가 없으므로 '출처' 근거는 확인할 수 없어 추정치로 내린다(SPEC 2.11)
    items = [i.model_copy(update={"basis": "llm_estimate", "source": ""}) if i.basis == "source" else i for i in items]
    try:
        return render_budget(compute_budget(items, people, nights, budget, budget_basis, currency), for_user=True)
    except (BudgetError, ExchangeRateError) as e:
        return f"예산 계산 실패: {e}"


@server.tool()
def convert_currency(from_currency: str, to_currency: str = config.BASE_CURRENCY, amount: float = 1.0) -> str:
    """실시간 참고 환율로 금액을 환산한다(하루 1회 갱신되는 대략치). 답에 결과의 출처를 함께 보여 준다.

    Args:
        from_currency: 바꿀 통화 코드 (예: JPY, USD, EUR)
        to_currency: 바꿀 대상 통화 코드 (기본 KRW)
        amount: 환산할 금액 (기본 1)
    """
    return get_exchange_rate.func(from_currency, to_currency, amount)


@server.tool()
def resolve_trip_dates(weeks_from_now: int | None = None, weekday: str = "", days_from_today: int | None = None,
                       date: str = "", nights: int | None = None) -> str:
    """상대 날짜('다음 주 토요일', '3일 뒤')와 여행 기간을 실제 날짜로 계산한다. 한 주는 월~일. 기준 표현은 하나만 준다.

    Args:
        weeks_from_now: 몇 주 뒤의 주인지 (이번 주=0, 다음 주=1) — weekday와 함께
        weekday: mon~sun 또는 weekend (비우면 그 주 전체)
        days_from_today: 오늘로부터 며칠 뒤인지 (내일=1)
        date: 절대 날짜 YYYY-MM-DD 또는 MM-DD
        nights: 박 수 (주면 귀국일과 일차별 날짜도 계산)
    """
    return resolve_dates.func(weeks_from_now, weekday, days_from_today, date, nights)


@server.tool()
def get_weather_forecast(city: str, start_date: str = "", end_date: str = "", country_code: str = "") -> str:
    """도시의 일별 날씨 예보(현지 기준 약 16일까지)를 조회한다. 상대 날짜는 resolve_trip_dates로 먼저 날짜를 구한다.
    답에 결과의 출처 'Open-Meteo'를 함께 보여 준다.

    Args:
        city: 영문 도시명 (예: Osaka, Kyoto)
        start_date: 시작 날짜 YYYY-MM-DD (비우면 오늘)
        end_date: 끝 날짜 YYYY-MM-DD (비우면 시작일부터 7일)
        country_code: ISO 국가 코드 2글자 (선택, 예: JP)
    """
    return get_weather.func(city, start_date, end_date, country_code)


def main() -> None:
    """사용자 ID를 검사하고 stdio로 MCP 서버를 실행한다."""
    config.setup_logging()
    validate_user_id(config.MCP_USER_ID)
    logger.info("MCP 서버 시작", extra={"event": "mcp_start", "data": {"user_id": config.MCP_USER_ID}})
    server.run("stdio")


if __name__ == "__main__":
    main()
