# test_mcp.py - MCP 서버 단위 테스트 (docs/SPEC.md 2.11, TASKS Step 14)
# 실행: python -m unittest discover tests
# mcp.Client에 서버 인스턴스를 넘겨 같은 프로세스에서 연결한다. 에이전트는 가짜 모델로 만든 실제 그래프를 쓴다(Bedrock 호출 없음).
import unittest
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from mcp import Client

from src import config, mcp_server
from src.agent import AgentContext, get_thread_owner, run_config


class FakeModel(GenericFakeChatModel):
    """정해 둔 답을 차례로 내는 가짜 채팅 모델(bind_tools는 자신을 돌려줌)."""

    def bind_tools(self, tools, **kwargs):
        """도구 목록은 쓰지 않는다."""
        return self


def text(result) -> str:
    """도구 호출 결과의 글자."""
    return result.content[0].text


class TestMcpServer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """가짜 모델 에이전트를 서버에 끼우고, MCP 사용자 ID를 시험용 값으로 고정한다."""
        self.saver = InMemorySaver()
        self.agent = create_agent(model=FakeModel(messages=iter([AIMessage("오코노미야키를 추천해요"),
                                                                 AIMessage("타코야키도 좋아요")])),
                                  tools=[], checkpointer=self.saver, context_schema=AgentContext)
        for patcher in (patch.object(mcp_server, "get_agent", return_value=(self.agent, self.saver)),
                        patch.object(config, "MCP_USER_ID", "mcp_test")):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(self, name: str, args: dict) -> str:
        """서버에 같은 프로세스로 연결해 도구를 부르고 결과 글자를 돌려준다."""
        async with Client(mcp_server.server) as client:
            return text(await client.call_tool(name, args))

    async def test_tool_list(self):
        """에이전트 도구 하나와 보조 계산 도구 넷을 노출한다(혼합 방식)."""
        async with Client(mcp_server.server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
        self.assertEqual(names, {"ask_trip_agent", "calculate_trip_budget", "convert_currency",
                                 "resolve_trip_dates", "get_weather_forecast"})

    async def test_new_and_continued_conversation(self):
        """대화 ID 없이 물으면 새 대화로 답하고 끝에 대화 ID를 붙인다. 그 ID로 이어 물으면 같은 대화에 쌓이고, 소유자는 MCP 사용자다."""
        first = await self.call("ask_trip_agent", {"message": "오사카 저녁 메뉴 추천해줘"})
        self.assertIn("오코노미야키를 추천해요", first)
        thread_id = first.split("대화 ID: ")[1].split(" ")[0]
        self.assertEqual(get_thread_owner(self.saver, thread_id), "mcp_test")   # 사용자 ID는 서버 설정으로 고정
        second = await self.call("ask_trip_agent", {"message": "또 뭐 있어?", "conversation_id": thread_id})
        self.assertIn("타코야키도 좋아요", second)
        self.assertIn(f"대화 ID: {thread_id}", second)
        messages = self.agent.get_state(run_config(thread_id, "mcp_test")).values["messages"]
        self.assertEqual([m.type for m in messages], ["human", "ai", "human", "ai"])

    async def test_other_users_conversation_rejected(self):
        """다른 사용자(CLI 등)가 만든 대화나 없는 대화 ID는 같은 문구로 거부한다."""
        self.agent.invoke({"messages": [{"role": "user", "content": "안녕"}]}, run_config("alice-thread", "alice"),
                          context=AgentContext(user_id="alice"))
        for thread_id in ("alice-thread", "no-such-thread"):
            answer = await self.call("ask_trip_agent", {"message": "이어서", "conversation_id": thread_id})
            self.assertIn(f"대화 '{thread_id}'를 열 수 없습니다", answer)

    async def test_empty_or_long_message_rejected(self):
        """빈 메시지와 너무 긴 메시지는 에이전트를 부르지 않고 거부한다."""
        self.assertIn("비어 있습니다", await self.call("ask_trip_agent", {"message": "  "}))
        long = "가" * (config.MCP_MESSAGE_MAX_CHARS + 1)
        self.assertIn(f"{config.MCP_MESSAGE_MAX_CHARS}자까지", await self.call("ask_trip_agent", {"message": long}))

    async def test_budget_tool_is_python_calculation(self):
        """예산 도구는 파이썬 계산 결과를 그대로 돌려주고, 확인할 수 없는 '출처' 근거는 추정치로 내린다."""
        items = [{"name": "숙박", "category": "lodging", "min_amount": 100000, "max_amount": 100000, "currency": "KRW",
                  "unit": "per_room_per_night", "basis": "user_input"},
                 {"name": "항공", "category": "flight", "min_amount": 300000, "max_amount": 300000, "currency": "KRW",
                  "unit": "per_person", "basis": "source", "source": "r123456"}]
        answer = await self.call("calculate_trip_budget", {"items": items, "people": 1, "nights": 2, "budget": 460000})
        self.assertIn("일행 총액 500,000 KRW", answer)
        self.assertIn("예산 근접", answer)                                     # 500,000 ≤ 460,000×1.1 = 506,000
        self.assertIn("항공 [1인 1회] 300,000 KRW × 1 = 300,000 KRW (근거: LLM 추정치", answer)

    async def test_dates_tool(self):
        """날짜 도구는 CLI와 같은 파이썬 날짜 계산을 쓴다."""
        answer = await self.call("resolve_trip_dates", {"days_from_today": 1, "nights": 1})
        self.assertIn("여행 기간:", answer)
        self.assertIn("1박2일", answer)


if __name__ == "__main__":
    unittest.main()
