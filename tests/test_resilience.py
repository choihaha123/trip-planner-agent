# test_resilience.py - 장애 내성 단위 테스트 (docs/SPEC.md 2.5, 2.8 / TASKS Phase 5-2 T1)
# 실행: python -m unittest discover tests
# Bedrock·Tavily는 호출하지 않는다. 지식베이스 검색·웹 검색은 가짜 함수로 바꿔 검사한다.
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt

from src import tools
from src.agent import AgentContext, make_tool_error_middleware, repair_orphan_tool_calls, repair_tool_history
from src import middleware
from src.collector import CollectResult
from src.middleware import (SYSTEM_FAILURE_MESSAGE, URL_NOTICE_HEADER, JudgeOutcome, UrlPreprocessMiddleware,
                            build_url_notice, hide_url_notices, process_url, strip_url_notice)
from src.tracing import TracingMiddleware

ACCESS_DENIED = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "InvokeModel")
WEB_RESULT = '<reference id="1" title="웹 문서" url="https://example.com">센소지 06:00 개방</reference>'


def fake_runtime(messages=None) -> SimpleNamespace:
    """도구에 넘길 가짜 ToolRuntime(user_id context와 대화 state만 가진다)."""
    return SimpleNamespace(context=SimpleNamespace(user_id="t_user"), state={"messages": messages or []})


class TestKnowledgeSearchFailure(unittest.TestCase):
    def test_search_knowledge_returns_none_on_error(self):
        """지식베이스 검색이 예외를 내면 예외를 올리지 않고 None을 돌려준다."""
        with patch.object(tools.retriever, "search", side_effect=ACCESS_DENIED):
            self.assertIsNone(tools.search_knowledge("센소지 운영시간", "t_user"))

    def test_search_travel_info_falls_back_to_web(self):
        """검색 실패 시 search_travel_info는 웹 검색 결과를 '확인하지 못해' 머리말과 함께 돌려준다."""
        with patch.object(tools.retriever, "search", side_effect=ACCESS_DENIED), \
                patch.object(tools, "run_web_search", return_value=WEB_RESULT):
            result = tools.search_travel_info.func("센소지 운영시간", fake_runtime())
        self.assertIn("저장 자료·기본 자료를 확인하지 못해", result)
        self.assertIn(WEB_RESULT, result)

    def test_search_travel_info_both_failed(self):
        """지식베이스 검색과 웹 검색이 모두 실패해도 예외 없이 '출처 없음' 안내를 돌려준다."""
        with patch.object(tools.retriever, "search", side_effect=ACCESS_DENIED), \
                patch.object(tools, "run_web_search", return_value=None):
            result = tools.search_travel_info.func("센소지 운영시간", fake_runtime())
        self.assertIn("확인하지 못해", result)
        self.assertIn(tools.WEB_SEARCH_FAILED, result)

    def test_no_relevant_result_keeps_old_header(self):
        """검색은 됐지만 관련 자료가 없으면 기존처럼 '관련 자료가 없어' 머리말을 쓴다(실패와 구분)."""
        with patch.object(tools.retriever, "search", return_value=[]), \
                patch.object(tools, "run_web_search", return_value=WEB_RESULT):
            result = tools.search_travel_info.func("센소지 운영시간", fake_runtime())
        self.assertIn("지식베이스에 관련 자료가 없어", result)

    def test_web_search_survives_knowledge_failure(self):
        """web_search가 먼저 불려 지식베이스를 확인하다 실패해도 웹 검색 결과는 돌려준다."""
        with patch.object(tools.retriever, "search", side_effect=ACCESS_DENIED), \
                patch.object(tools, "run_web_search", return_value=WEB_RESULT):
            result = tools.web_search.func("센소지 운영시간", fake_runtime())
        self.assertIn("[웹 검색 결과]", result)
        self.assertNotIn("먼저 확인한 지식베이스", result)


class FakeToolModel(GenericFakeChatModel):
    """정해 둔 답을 차례로 내는 가짜 채팅 모델. create_agent가 부르는 bind_tools는 그대로 자신을 돌려준다."""

    def bind_tools(self, tools, **kwargs):
        """도구 목록은 쓰지 않는다."""
        return self


@tool
def broken_tool(query: str) -> str:
    """항상 AccessDenied 예외를 내는 테스트용 도구."""
    raise ACCESS_DENIED


def tool_request(name: str = "broken_tool") -> SimpleNamespace:
    """미들웨어에 넘길 가짜 도구 호출 요청."""
    return SimpleNamespace(tool=None, tool_call={"name": name, "id": "call_1", "args": {}})


class TestToolErrorMiddleware(unittest.TestCase):
    def test_exception_becomes_error_tool_message(self):
        """도구 예외는 같은 tool_call_id의 ToolMessage(status=error)가 되고, 내부 정보 대신 오류 종류만 담는다."""
        def handler(_):
            raise ACCESS_DENIED
        result = make_tool_error_middleware().wrap_tool_call(tool_request(), handler)
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual((result.tool_call_id, result.status), ("call_1", "error"))
        self.assertIn("AccessDeniedException", result.content)
        self.assertNotIn("not authorized", result.content)

    def test_graph_control_signal_passes_through(self):
        """LangGraph 제어 신호(interrupt)는 오류 결과로 바꾸지 않고 그대로 올린다."""
        def handler(_):
            raise GraphInterrupt()
        with self.assertRaises(GraphInterrupt):
            make_tool_error_middleware().wrap_tool_call(tool_request(), handler)

    def test_thread_survives_tool_failure(self):
        """도구가 실패한 턴도 답변이 나오고, 대화 기록에 도구 호출과 결과 짝이 남아 다음 턴이 이어진다. (P1 재현)"""
        model = FakeToolModel(messages=iter([
            AIMessage(content="", tool_calls=[{"name": "broken_tool", "args": {"query": "q"}, "id": "call_1"}]),
            AIMessage(content="검색을 못 해 일반 지식으로 답합니다."),
            AIMessage(content="두 번째 턴 답변"),
        ]))
        agent = create_agent(model=model, tools=[broken_tool], checkpointer=InMemorySaver(),
                             middleware=[make_tool_error_middleware(), TracingMiddleware()])
        run = {"configurable": {"thread_id": "t1"}}
        first = agent.invoke({"messages": [{"role": "user", "content": "첫 질문"}]}, run)
        self.assertEqual(first["messages"][-1].content, "검색을 못 해 일반 지식으로 답합니다.")
        tool_results = [m for m in first["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual([(m.tool_call_id, m.status) for m in tool_results], [("call_1", "error")])
        second = agent.invoke({"messages": [{"role": "user", "content": "둘째 질문"}]}, run)
        self.assertEqual(second["messages"][-1].content, "두 번째 턴 답변")


class StrictFakeToolModel(FakeToolModel):
    """Bedrock처럼, 결과 없는 도구 호출이 입력에 있으면 요청을 거부하는 가짜 모델."""

    def _generate(self, messages, *args, **kwargs):
        """입력의 도구 호출마다 결과가 있는지 검사한 뒤 정해 둔 답을 낸다."""
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        for m in messages:
            for tc in getattr(m, "tool_calls", None) or []:
                if tc["id"] not in answered:
                    raise ValueError("ValidationException: tool_use ids were found without tool_result")
        return super()._generate(messages, *args, **kwargs)


def orphan_call(call_id: str = "call_1") -> AIMessage:
    """도구 호출 하나를 담은 AI 메시지."""
    return AIMessage(content="", tool_calls=[{"name": "broken_tool", "args": {"query": "q"}, "id": call_id}])


class TestRepairToolHistory(unittest.TestCase):
    def test_inserts_result_right_after_orphan_call(self):
        """결과 없는 도구 호출 바로 뒤(다음 사람 메시지 앞)에 오류 결과를 넣는다."""
        messages = [HumanMessage("첫 질문"), orphan_call(), HumanMessage("둘째 질문")]
        repaired = repair_tool_history(messages)
        self.assertEqual([m.type for m in repaired], ["human", "ai", "tool", "human"])
        self.assertEqual((repaired[2].tool_call_id, repaired[2].status), ("call_1", "error"))

    def test_keeps_existing_results_and_fills_only_missing(self):
        """도구 호출 두 개 중 하나만 결과가 있으면, 기존 결과 뒤에 빠진 것만 넣는다."""
        ai = AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "c1"}, {"name": "b", "args": {}, "id": "c2"}])
        messages = [HumanMessage("q"), ai, ToolMessage("ok", tool_call_id="c1"), HumanMessage("q2")]
        repaired = repair_tool_history(messages)
        self.assertEqual([getattr(m, "tool_call_id", m.type) for m in repaired], ["human", "ai", "c1", "c2", "human"])

    def test_returns_same_list_when_nothing_missing(self):
        """짝이 모두 맞으면 같은 목록을 그대로 돌려준다(요청을 바꾸지 않는다)."""
        messages = [HumanMessage("q"), orphan_call(), ToolMessage("ok", tool_call_id="call_1"), AIMessage("답")]
        self.assertIs(repair_tool_history(messages), messages)

    def test_broken_thread_recovers(self):
        """도구 오류 처리 없이 깨진 thread도, 보정 미들웨어가 있는 에이전트로 이어서 쓰면 답이 나온다. (기존 깨진 thread 복구)"""
        saver, run = InMemorySaver(), {"configurable": {"thread_id": "broken"}}
        breaker = create_agent(model=StrictFakeToolModel(messages=iter([orphan_call()])), tools=[broken_tool],
                               checkpointer=saver)
        with self.assertRaises(ClientError):
            breaker.invoke({"messages": [{"role": "user", "content": "첫 질문"}]}, run)

        def resume(middleware):
            agent = create_agent(model=StrictFakeToolModel(messages=iter([AIMessage("복구된 답변")])),
                                 tools=[broken_tool], checkpointer=saver, middleware=middleware)
            return agent.invoke({"messages": [{"role": "user", "content": "둘째 질문"}]}, run)

        with self.assertRaises(ValueError):
            resume([])  # 보정이 없으면 예전처럼 계속 거부된다.
        self.assertEqual(resume([repair_orphan_tool_calls])["messages"][-1].content, "복구된 답변")


NOTICE = f"{URL_NOTICE_HEADER}\n- https://docs.python.org/3/: 여행 관련 내용이 아니라고 판단해 참고하지 않았습니다"


class CapturingFakeModel(FakeToolModel):
    """받은 입력 메시지를 기록하는 가짜 모델."""

    def _generate(self, messages, *args, **kwargs):
        """입력을 inputs에 남기고 정해 둔 답을 낸다."""
        self.__dict__.setdefault("inputs", []).append(messages)
        return super()._generate(messages, *args, **kwargs)


class TestHideUrlNotice(unittest.TestCase):
    def test_strip_cuts_notice_block(self):
        """안내 블록 첫 줄부터 끝까지 떼고 본문만 남긴다. 블록이 없으면 그대로."""
        self.assertEqual(strip_url_notice(f"본문 답변\n\n{NOTICE}"), "본문 답변")
        self.assertEqual(strip_url_notice("본문 답변"), "본문 답변")

    def test_strip_keeps_text_after_block(self):
        """LLM이 본문 중간에 쓴 블록(머리줄 꾸밈 포함)만 지우고, 블록 뒤의 출처 등 본문은 남긴다."""
        text = ("센소지는 6시에 엽니다.\n\n**※ 참고하지 못한 링크**\n- https://docs.python.org/3/: 처리 중 오류가 나 참고하지 않았습니다\n\n"
                "출처: [센소지](https://example.com)")
        self.assertEqual(strip_url_notice(text), "센소지는 6시에 엽니다.\n\n출처: [센소지](https://example.com)")

    def test_hides_only_ai_notices(self):
        """AI 답변의 블록만 빼고, 사람 메시지는 건드리지 않는다. 뺄 것이 없으면 같은 목록."""
        messages = [HumanMessage(f"이거 봐 {URL_NOTICE_HEADER}"), AIMessage(f"답\n\n{NOTICE}")]
        cleaned = hide_url_notices(messages)
        self.assertEqual([m.content for m in cleaned], [messages[0].content, "답"])
        plain = [HumanMessage("q"), AIMessage("답")]
        self.assertIs(hide_url_notices(plain), plain)

    def test_hides_notice_on_itinerary_result(self):
        """일정표 답변은 render_itinerary 도구 결과 끝에 블록이 붙으므로 거기서도 뺀다."""
        itinerary = ToolMessage(f"[1일차] 센소지\n\n{NOTICE}", tool_call_id="c1", name="render_itinerary")
        self.assertEqual(hide_url_notices([itinerary])[0].content, "[1일차] 센소지")

    def test_block_content_keeps_tool_use(self):
        """블록 리스트 답변은 텍스트 블록만 자르고 도구 호출 블록은 남긴다."""
        tool_block = {"type": "tool_use", "id": "c1", "name": "web_search", "input": {}}
        ai = AIMessage(content=[{"type": "text", "text": f"찾아볼게요\n\n{NOTICE}"}, tool_block])
        cleaned = hide_url_notices([ai])[0]
        self.assertEqual(cleaned.content, [{"type": "text", "text": "찾아볼게요"}, tool_block])

    def test_model_input_hidden_but_history_kept(self):
        """모델 입력에서는 지난 답변의 블록이 빠지고, 저장된 대화 기록에는 사용자가 본 그대로 남는다."""
        model = CapturingFakeModel(messages=iter([AIMessage("이번 답변")]))
        agent = create_agent(model=model, tools=[], checkpointer=InMemorySaver(), context_schema=AgentContext,
                             middleware=[UrlPreprocessMiddleware()])
        history = [HumanMessage("첫 질문"), AIMessage(f"지난 답변\n\n{NOTICE}"), HumanMessage("둘째 질문")]
        result = agent.invoke({"messages": history}, {"configurable": {"thread_id": "n1"}},
                              context=AgentContext(user_id="t_user"))
        self.assertNotIn(URL_NOTICE_HEADER, str(model.inputs[0]))
        self.assertIn(URL_NOTICE_HEADER, result["messages"][1].content)


class TestUrlNoticeOnce(unittest.TestCase):
    def after_agent(self, answer: str, notice: str):
        """URL 전처리 미들웨어의 턴 끝 처리를 답변 하나에 적용한다."""
        state = {"messages": [HumanMessage("q"), AIMessage(answer, id="a1")], "url_notice": notice}
        return UrlPreprocessMiddleware().after_agent(state, None)

    def test_llm_block_replaced_by_program_block(self):
        """LLM이 본문에 쓴 블록(지어낸 이유)은 지우고, 프로그램 판정 블록 하나만 남긴다."""
        fake = f"답변 본문\n\n{URL_NOTICE_HEADER}\n- https://docs.python.org/3/: 처리 중 오류가 나 참고하지 않았습니다"
        result = self.after_agent(fake, NOTICE)["messages"][0]
        self.assertEqual(result.id, "a1")
        self.assertEqual(result.content.count(URL_NOTICE_HEADER), 1)
        self.assertNotIn("처리 중 오류", result.content)
        self.assertIn("여행 관련 내용이 아니라고 판단해", result.content)

    def test_llm_block_removed_without_program_notice(self):
        """이번 턴에 참고하지 못한 링크가 없는데 LLM이 블록을 쓰면 지운다."""
        fake = f"답변 본문\n\n{URL_NOTICE_HEADER}\n- https://a.com: 처리 중 오류가 나 참고하지 않았습니다"
        self.assertEqual(self.after_agent(fake, "")["messages"][0].content, "답변 본문")

    def test_nothing_to_do(self):
        """블록도 안내도 없으면 답변을 바꾸지 않는다."""
        self.assertIsNone(self.after_agent("답변 본문", ""))


PAGE = CollectResult(url="https://blog.example.com/1", ok=True, title="센소지 후기", text="센소지 " * 100)
ACCEPTED = JudgeOutcome("accepted", 0.9, "여행 글")


class TestSystemFailureNotice(unittest.TestCase):
    def process(self, **patches):
        """수집은 PAGE로 고정하고, 저장소·판정 함수를 바꿔 끼워 process_url을 실행한다."""
        with patch.object(middleware, "collect", return_value=PAGE), \
                patch.object(middleware, "judge_page", patches.get("judge", lambda *_: ACCEPTED)), \
                patch.object(middleware.retriever, "stored_content_hash", patches.get("stored_hash", lambda *_: None)), \
                patch.object(middleware.retriever, "save_user_page", patches.get("save", lambda *_: "saved")):
            return process_url(PAGE.url, "t_user")

    def test_save_error_is_save_failed(self):
        """저장(임베딩 포함) 예외는 링크 문제가 아니라 save_failed로 분류한다."""
        def fail(*_):
            raise ACCESS_DENIED
        self.assertEqual(self.process(save=fail).status, "save_failed")

    def test_hash_check_error_is_save_failed(self):
        """저장 전 중복 확인(저장소 조회) 예외도 save_failed로 분류한다."""
        def fail(*_):
            raise RuntimeError("chroma locked")
        self.assertEqual(self.process(stored_hash=fail).status, "save_failed")

    def test_normal_save_still_works(self):
        """저장소가 정상이면 기존처럼 saved."""
        self.assertEqual(self.process().status, "saved")

    def test_system_failures_use_our_side_message(self):
        """저장 실패·판정 실패는 '링크에는 문제가 없다'는 문구, 관련도 미달은 기존 거절 문구로 안내한다."""
        outcomes = [middleware.UrlOutcome("https://a.com", "save_failed"), middleware.UrlOutcome("https://b.com", "judge_failed"),
                    middleware.UrlOutcome("https://c.com", "rejected_low_score")]
        notice = build_url_notice(outcomes, [])
        self.assertEqual(notice.count(SYSTEM_FAILURE_MESSAGE), 2)
        self.assertIn("✖ 못 씀: https://c.com — 여행 관련 내용이 아니라고 판단해", notice)


class TestNextActionNotice(unittest.TestCase):
    def notice(self, *outcomes, skipped=()):
        """결과 목록으로 안내 블록을 만든다."""
        return build_url_notice(list(outcomes), list(skipped))

    def test_reason_followed_by_next_action(self):
        """무관 링크·동영상은 기존 이유 뒤에 다음 행동 안내가 붙는다(기존 이유 문구는 그대로 남는다)."""
        text = self.notice(middleware.UrlOutcome("https://a.com", "rejected_low_score"),
                           middleware.UrlOutcome("https://youtu.be/x", "collect_failed",
                                                 failure_message="동영상 페이지라 내용을 읽을 수 없습니다", failure="video"))
        self.assertIn("✖ 못 씀: https://a.com — 여행 관련 내용이 아니라고 판단해 참고하지 않았습니다. "
                      "여행 블로그나 관광 안내 페이지 링크를 보내 주세요", text)
        self.assertIn("동영상 페이지라 내용을 읽을 수 없습니다. 영상 설명이나 자막 내용을 글로 붙여 주시면 참고할게요", text)

    def test_no_hint_for_instruction_and_system_failure(self):
        """지시문 의심은 우회 방법을 알려 주지 않고, 시스템 장애 문구에는 안내를 덧붙이지 않는다."""
        text = self.notice(middleware.UrlOutcome("https://a.com", "rejected_instruction"),
                           middleware.UrlOutcome("https://b.com", "save_failed"))
        self.assertIn("안전을 위해 참고하지 않았습니다\n", text)
        self.assertTrue(text.endswith(SYSTEM_FAILURE_MESSAGE))

    def test_skipped_links_say_send_next(self):
        """개수 초과는 '다음 메시지로 보내 주세요'를 안내한다."""
        text = self.notice(skipped=["https://x.com"])
        self.assertIn("5개까지만", text)
        self.assertIn("다음 메시지로 보내 주세요: https://x.com", text)

    def test_every_collect_failure_code_known(self):
        """다음 행동 안내 키는 모두 실제 상태나 수집 실패 코드여야 한다(오타 방지)."""
        from src.collector import FAILURE_MESSAGES
        known = set(FAILURE_MESSAGES) | set(middleware.NOT_USED_MESSAGES)
        self.assertLessEqual(set(middleware.NEXT_ACTIONS), known)


class TestUrlNotesRule(unittest.TestCase):
    def test_rule_included_without_notice_header(self):
        """LLM 안내문에는 본문에서 링크 처리 결과를 말하지 말라는 규칙이 들어가고, 흉내 낼 블록 머리줄은 넣지 않는다."""
        notes = middleware.build_url_notes([middleware.UrlOutcome("https://a.com", "rejected_low_score")], [])
        self.assertIn(middleware.URL_NOTES_ANSWER_RULE, notes)
        self.assertNotIn(URL_NOTICE_HEADER, notes)


class NoCallModel(FakeToolModel):
    """호출되면 실패하는 가짜 모델(LLM을 부르지 않아야 하는 경로 검사용)."""

    def _generate(self, *args, **kwargs):
        """호출 자체를 실패로 본다."""
        raise AssertionError("모델이 호출되면 안 된다")


class TestSaveTransparency(unittest.TestCase):
    def test_notice_lists_saved_links_too(self):
        """링크 처리 결과 블록에 저장한 링크(✔ 제목)와 못 쓴 링크(✖)가 함께 나온다. (T3-1)"""
        text = build_url_notice([middleware.UrlOutcome("https://a.com", "saved", "센소지 후기"),
                                 middleware.UrlOutcome("https://b.com", "unchanged", "시부야 스카이"),
                                 middleware.UrlOutcome("https://c.com", "rejected_low_score")], [])
        self.assertTrue(text.startswith(URL_NOTICE_HEADER))
        self.assertIn("✔ 저장함: 센소지 후기 (https://a.com)", text)
        self.assertIn("✔ 이미 저장됨(내용 같음): 시부야 스카이 (https://b.com)", text)
        self.assertIn("✖ 못 씀: https://c.com", text)

    def test_is_url_only(self):
        """URL과 '저장해 줘' 같은 말뿐이면 링크만 보낸 메시지, 질문이 섞이면 아니다. (T3-2)"""
        for text in ("https://a.com", "이거 저장해줘 https://a.com", "https://a.com https://b.com 링크 저장 부탁해요!"):
            self.assertTrue(middleware.is_url_only(text), text)
        for text in ("이 블로그 참고해서 센소지 코스 추천해줘 https://a.com", "오사카 갈 때 참고하려고 저장해둬 https://a.com"):
            self.assertFalse(middleware.is_url_only(text), text)

    def test_url_only_message_answered_without_llm(self):
        """링크만 보낸 메시지는 LLM을 부르지 않고 저장 결과 답과 처리 결과 블록을 돌려준다. (T3-2)"""
        agent = create_agent(model=NoCallModel(messages=iter([])), tools=[], checkpointer=InMemorySaver(),
                             context_schema=AgentContext, middleware=[UrlPreprocessMiddleware()])
        saved = middleware.UrlOutcome("https://blog.example.com/1", "saved", "센소지 후기")
        with patch.object(middleware, "process_url", return_value=saved):
            result = agent.invoke({"messages": [HumanMessage("이거 저장해줘 https://blog.example.com/1")]},
                                  {"configurable": {"thread_id": "u1"}}, context=AgentContext(user_id="t_user"))
        answer = result["messages"][-1].content
        self.assertIn("링크 1개를 저장해 두었습니다", answer)
        self.assertIn("✔ 저장함: 센소지 후기", answer)

    def test_saved_material_missing(self):
        """저장 자료를 언급했는데 이번 턴 도구 결과에 저장 자료 출처가 없으면 True, 있으면 False. (T3-3)"""
        ask = HumanMessage("저번에 저장한 거 기반으로 도쿄 하루 코스 짜줘")
        web = ToolMessage('<reference id="1" title="웹" url="https://w.com" source="web">…</reference>', tool_call_id="c1",
                          name="search_travel_info")
        saved = ToolMessage('<reference id="1" title="내 글" url="https://s.com" source="saved">…</reference>', tool_call_id="c2",
                            name="search_travel_info")
        self.assertTrue(middleware.saved_material_missing([ask, web, AIMessage("답")]))
        # 검색하지 않은 턴(목록 보기 같은 기능 문의)에는 붙이지 않는다.
        self.assertFalse(middleware.saved_material_missing([HumanMessage("내가 저장한 링크 목록 보여줘"), AIMessage("미지원")]))
        self.assertFalse(middleware.saved_material_missing([ask, saved, AIMessage("답")]))
        self.assertFalse(middleware.saved_material_missing([HumanMessage("도쿄 하루 코스 짜줘"), web, AIMessage("답")]))

    def test_after_agent_adds_saved_missing_notice(self):
        """저장 자료 없음 안내는 답변 끝에 한 번 붙고, 다음 턴 모델 입력에서는 빠진다. (T3-3)"""
        web = ToolMessage('<reference id="1" title="웹" url="https://w.com" source="web">…</reference>', tool_call_id="c1",
                          name="web_search")
        state = {"messages": [HumanMessage("저장해 둔 거 기반으로 맛집 알려줘"), web, AIMessage("맛집 답변", id="a1")]}
        answer = UrlPreprocessMiddleware().after_agent(state, None)["messages"][0].content
        self.assertEqual(answer, f"맛집 답변\n\n{middleware.SAVED_MISSING_NOTICE}")
        self.assertEqual(hide_url_notices([AIMessage(answer)])[0].content, "맛집 답변")

    def test_reference_source_attribute(self):
        """검색 결과 태그에 출처 구분(saved·kb·web)이 붙는다. (T3-4)"""
        from langchain_core.documents import Document
        docs = [Document("KB 본문", metadata={"user_id": "__kb__", "title": "KB", "source_url": "https://kb"}),
                Document("내 글", metadata={"user_id": "t_user", "title": "내 글", "source_url": "https://s"})]
        refs = tools.format_references(docs)
        self.assertIn('url="https://kb" source="kb"', refs)
        self.assertIn('url="https://s" source="saved"', refs)

    def test_legacy_notice_still_hidden(self):
        """T3 이전 이름('※ 참고하지 못한 링크')의 블록도 지난 기록 정리 때 인식해 뺀다."""
        old = AIMessage(f"답\n\n{middleware.LEGACY_URL_NOTICE_HEADER}\n- https://a.com: 처리 중 오류가 나 참고하지 않았습니다")
        self.assertEqual(hide_url_notices([old])[0].content, "답")


class FakeStore:
    """retriever 사용자 컬렉션 대신 쓰는 가짜 저장소(get·delete만)."""

    def __init__(self, metadatas):
        """청크별 메타데이터 목록으로 만든다."""
        self.metadatas, self.deleted = metadatas, []

    def get(self, where, include):
        """user_id가 같은 청크의 id와 메타데이터를 돌려준다."""
        hits = [(str(i), m) for i, m in enumerate(self.metadatas) if m["user_id"] == where["user_id"]]
        return {"ids": [i for i, _ in hits], "metadatas": [m for _, m in hits]}

    def delete(self, ids):
        """지운 id를 기록한다."""
        self.deleted.extend(ids)


class TestCliMessages(unittest.TestCase):
    def run_chat(self, inputs, ask_result=None, ask_error=None):
        """입력 목록으로 대화 루프를 돌리고 화면 출력을 돌려준다."""
        import io
        import main
        out = io.StringIO()
        with patch("builtins.input", side_effect=inputs), patch("sys.stdout", out), \
                patch.object(main, "ask", side_effect=ask_error, return_value=ask_result):
            main.chat_loop(None, "t_user", "t1")
        return out.getvalue()

    def test_reset_counts_links_not_chunks(self):
        """--reset은 지운 청크가 아니라 저장한 링크(고유 URL) 수를 센다. (T4-3)"""
        from src import retriever
        store = FakeStore([{"user_id": "t_user", "source_url": "https://a"}, {"user_id": "t_user", "source_url": "https://a"},
                           {"user_id": "t_user", "source_url": "https://b"}, {"user_id": "other", "source_url": "https://c"}])
        with patch.object(retriever, "get_store", return_value=store):
            self.assertEqual(retriever.reset_user("t_user"), (2, 3))
        self.assertEqual(store.deleted, ["0", "1", "2"])

    def test_reset_message_uses_plain_words(self):
        """--reset 안내에는 '청크'·'KB' 같은 개발 용어 없이 링크 수만 보여 준다. (T4-3)"""
        import io
        import main
        for result, memories, expected in (((2, 7), 1, "저장한 링크 2개와 기억 1개를 지웠습니다"),
                                           ((0, 0), 0, "저장한 링크와 기억이 없습니다")):
            out = io.StringIO()
            with patch.object(main, "reset_user", return_value=result), patch.object(main, "reset_memory", return_value=memories), \
                    patch("sys.stdout", out):
                main.reset_command("t_user")
            self.assertIn(expected, out.getvalue())
            self.assertNotIn("청크", out.getvalue())

    def test_start_screen_mentions_link_saving(self):
        """시작 화면에 링크 저장 기능 안내가 나온다. (T4-1)"""
        self.assertIn("링크를 붙여넣으면 저장해 두었다가", self.run_chat(["/exit"]))

    def test_link_progress_shown(self):
        """링크가 있는 메시지는 답변 전에 '링크 N개 확인 중…'을 보여 준다. (T4-2)"""
        output = self.run_chat(["이거 봐 https://a.com https://b.com", "/exit"], ask_result="FAKE_ANSWER")
        self.assertLess(output.index("링크 2개 확인 중"), output.index("FAKE_ANSWER"))

    def test_ctrl_c_cancels_only_answer(self):
        """답변 중 Ctrl+C는 그 답변만 취소하고 대화 루프는 이어 간다. (T4-5)"""
        output = self.run_chat(["질문", "/exit"], ask_error=KeyboardInterrupt)
        self.assertIn("답변을 취소했습니다", output)
        self.assertIn("대화를 마칩니다", output)

    def test_thread_denied_message_explains(self):
        """남의·없는 thread 거부 문구는 같고, 이유와 다음 행동을 알려 준다. (T4-4)"""
        from src.agent import ThreadAccessError, check_thread_access
        with patch("src.agent.get_thread_owner", return_value="someone"), self.assertRaises(ThreadAccessError) as owned:
            check_thread_access(None, "t9", "t_user")
        with patch("src.agent.get_thread_owner", return_value=None), self.assertRaises(ThreadAccessError) as missing:
            check_thread_access(None, "t9", "t_user")
        self.assertEqual(str(owned.exception), str(missing.exception))
        self.assertIn("본인이 만든 대화만 열 수 있습니다", str(owned.exception))


PAGES = [{"source_url": "https://blog.naver.com/a", "title": "센소지 후기", "collected_at": "2026-11-05T10:00:00+09:00"},
         {"source_url": "https://www.gotokyo.org/b", "title": "시부야 스카이", "collected_at": "2026-11-06T09:00:00+09:00"}]


class TestSavedCommands(unittest.TestCase):
    def test_list_user_pages_groups_chunks(self):
        """청크 여러 개인 링크도 한 항목으로 묶고, 저장한 순서로 정렬하며, 남의 자료는 빼고 돌려준다. (T6-1)"""
        from src import retriever
        store = FakeStore([
            {"user_id": "t_user", "source_url": "https://b", "title": "B", "collected_at": "2026-11-06T09:00:00"},
            {"user_id": "t_user", "source_url": "https://a", "title": "A", "collected_at": "2026-11-05T09:00:00"},
            {"user_id": "t_user", "source_url": "https://a", "title": "A", "collected_at": "2026-11-05T09:00:00"},
            {"user_id": "other", "source_url": "https://c", "title": "C", "collected_at": "2026-11-04T09:00:00"}])
        with patch.object(retriever, "get_store", return_value=store):
            pages = retriever.list_user_pages("t_user")
        self.assertEqual([p["source_url"] for p in pages], ["https://a", "https://b"])

    def test_saved_lists_numbered_pages(self):
        """/saved는 번호·제목·URL·저장일을 보여 주고, 저장한 게 없으면 그렇게 안내한다. (T6-1)"""
        import main
        with patch.object(main, "list_user_pages", return_value=PAGES):
            text = main.saved_command("t_user")
        self.assertIn(" 1. 센소지 후기 (https://blog.naver.com/a)  11/05 저장", text)
        self.assertIn(" 2. 시부야 스카이 (https://www.gotokyo.org/b)  11/06 저장", text)
        with patch.object(main, "list_user_pages", return_value=[]):
            self.assertIn("저장한 링크가 없습니다", main.saved_command("t_user"))

    def test_forget_deletes_only_that_page_of_this_user(self):
        """/forget 2는 /saved 목록 2번 링크를 현재 사용자 자료에서만 지운다. (T6-1)"""
        import main
        with patch.object(main, "list_user_pages", return_value=PAGES), \
                patch.object(main, "delete_user_source", return_value=3) as delete:
            text = main.forget_command("t_user", "2")
        delete.assert_called_once_with("t_user", "https://www.gotokyo.org/b")
        self.assertIn("'시부야 스카이' 링크를 지웠습니다", text)

    def test_forget_rejects_bad_number(self):
        """번호가 없거나 범위 밖이면 지우지 않고 사용법을 안내한다. (T6-1)"""
        import main
        for arg in ("", "0", "3", "abc"):
            with patch.object(main, "list_user_pages", return_value=PAGES), \
                    patch.object(main, "delete_user_source") as delete:
                self.assertIn("가능한 번호: 1~2", main.forget_command("t_user", arg))
            delete.assert_not_called()

    def test_commands_do_not_call_llm(self):
        """/saved·/forget은 에이전트(LLM)를 부르지 않는다. (T6-1)"""
        import io
        import main
        out = io.StringIO()
        with patch("builtins.input", side_effect=["/saved", "/forget 1", "/exit"]), patch("sys.stdout", out), \
                patch.object(main, "ask", side_effect=AssertionError("LLM 호출 금지")), \
                patch.object(main, "list_user_pages", return_value=PAGES), patch.object(main, "delete_user_source"):
            main.chat_loop(None, "t_user", "t1")
        self.assertIn("저장한 링크 2개", out.getvalue())
        self.assertIn("'센소지 후기' 링크를 지웠습니다", out.getvalue())


class TestSavedSourceMark(unittest.TestCase):
    def search_result(self):
        """저장 자료 1개 + 기본 KB 1개 + 웹 1개가 섞인 이번 턴 검색 결과."""
        from langchain_core.documents import Document
        docs = [Document("센소지 6시 개방", metadata={"user_id": "t_user", "title": "센소지 후기",
                                                   "source_url": "https://blog.naver.com/a",
                                                   "collected_at": "2026-11-05T10:00:00+09:00"}),
                Document("아사쿠사 개요", metadata={"user_id": "__kb__", "title": "KB", "source_url": "https://kb.example/asakusa",
                                                "collected_at": "2026-11-01T00:00:00+09:00"})]
        web = tools.wrap_reference(3, "웹 문서", "https://web.example/x", "…", source=tools.SOURCE_WEB)
        return ToolMessage(tools.format_references(docs) + "\n\n" + web, tool_call_id="c1", name="search_travel_info")

    def test_collects_only_saved_references(self):
        """이번 턴 검색 결과에서 저장 자료(source=saved)의 URL과 저장 시각만 모은다. (T6-2)"""
        saved = middleware.saved_references_this_turn([HumanMessage("q"), self.search_result()])
        self.assertEqual(saved, {"https://blog.naver.com/a": "2026-11-05T10:00:00+09:00"})

    def test_marks_saved_link_only(self):
        """저장 자료 링크(마크다운·맨 URL)에만 표시가 붙고, 웹·기본 자료와 더 긴 다른 URL에는 붙지 않는다. (T6-2)"""
        saved = {"https://blog.naver.com/a": "2026-11-05T10:00:00+09:00"}
        text = ("출처:\n- [센소지 후기](https://blog.naver.com/a)\n- [웹 문서](https://web.example/x)\n"
                "참고 https://blog.naver.com/a. 다른 글 https://blog.naver.com/a/2")
        marked = middleware.mark_saved_sources(text, saved)
        self.assertIn("- 📌 내가 저장한 자료 (11/05 저장) [센소지 후기](https://blog.naver.com/a)", marked)
        self.assertIn("- [웹 문서](https://web.example/x)", marked)
        self.assertIn("참고 📌 내가 저장한 자료 (11/05 저장) https://blog.naver.com/a.", marked)
        self.assertIn("다른 글 https://blog.naver.com/a/2", marked)

    def test_after_agent_marks_and_fixes_fake_marks(self):
        """턴 끝에 저장 자료 출처를 표시하고, LLM이 웹 출처에 붙인 가짜 표시는 지운다. 다음 턴 모델 입력에서는 표시가 빠진다. (T6-2)"""
        answer = ("센소지는 6시에 엽니다.\n출처: [센소지 후기](https://blog.naver.com/a), "
                  "📌 내가 저장한 자료 [웹 문서](https://web.example/x)")
        state = {"messages": [HumanMessage("센소지 몇 시에 열어?"), self.search_result(), AIMessage(answer, id="a1")]}
        result = UrlPreprocessMiddleware().after_agent(state, None)["messages"][0].content
        self.assertIn("📌 내가 저장한 자료 (11/05 저장) [센소지 후기](https://blog.naver.com/a)", result)
        self.assertIn(", [웹 문서](https://web.example/x)", result)
        self.assertEqual(result.count("📌"), 1)
        hidden = hide_url_notices([AIMessage(result)])[0].content
        self.assertNotIn("📌", hidden)
        self.assertIn("[센소지 후기](https://blog.naver.com/a)", hidden)

    def test_no_change_without_saved_or_notice(self):
        """저장 자료도 안내도 없으면 답변을 바꾸지 않는다."""
        state = {"messages": [HumanMessage("q"), AIMessage("[웹](https://web.example/x)", id="a1")]}
        self.assertIsNone(UrlPreprocessMiddleware().after_agent(state, None))


class TestHistoryTrim(unittest.TestCase):
    def history(self):
        """지난 두 턴(검색 후 답변, 일정표가 최종 답변인 턴) + 지금 턴(검색 중)."""
        call = lambda cid, name, args: AIMessage("", tool_calls=[{"name": name, "args": args, "id": cid}])
        return [
            HumanMessage("교토 단풍 명소"), call("c1", "search_travel_info", {"query": "교토 단풍"}),
            ToolMessage("<reference>긴 검색 결과…</reference>" * 50, tool_call_id="c1", name="search_travel_info"),
            AIMessage("도후쿠지를 추천해요"),
            HumanMessage("일정 짜줘"), call("c2", "render_itinerary", {"days": ["아주 긴 일정 JSON"] * 50}),
            ToolMessage("# 교토 일정표 본문", tool_call_id="c2", name="render_itinerary"),
            HumanMessage("고마워"), call("c3", "search_travel_info", {"query": "지금 턴"}),
            ToolMessage("지금 턴 검색 결과", tool_call_id="c3", name="search_travel_info"),
        ]

    def test_clears_only_previous_turn_tool_payloads(self):
        """지난 턴 검색 결과·도구 인자만 줄이고, 지금 턴·답변 본문·최종 답변인 일정표는 그대로 둔다. (응답 속도 개선 H)"""
        from src.agent import PREVIOUS_TURN_TOOL_RESULT, ClearPreviousTurnTools
        messages = self.history()
        ClearPreviousTurnTools().apply(messages)
        self.assertEqual(messages[2].content, PREVIOUS_TURN_TOOL_RESULT)       # 지난 턴 검색 결과
        self.assertEqual(messages[1].tool_calls[0]["args"], {})                 # 지난 턴 도구 인자
        self.assertEqual(messages[5].tool_calls[0]["args"], {})                 # 일정표 JSON 인자
        self.assertEqual(messages[6].content, "# 교토 일정표 본문")              # 최종 답변인 일정표는 보존
        self.assertEqual(messages[3].content, "도후쿠지를 추천해요")
        self.assertEqual(messages[8].tool_calls[0]["args"], {"query": "지금 턴"})
        self.assertEqual(messages[9].content, "지금 턴 검색 결과")
        self.assertEqual(len(messages), 10)                                     # 메시지 수(도구 호출·결과 짝) 유지

    def test_single_turn_untouched(self):
        """첫 턴(지난 턴 없음)은 아무것도 바꾸지 않는다."""
        from src.agent import ClearPreviousTurnTools
        messages = self.history()[:3]
        ClearPreviousTurnTools().apply(messages)
        self.assertEqual(messages[1].tool_calls[0]["args"], {"query": "교토 단풍"})

    def test_model_input_trimmed_but_history_kept(self):
        """그래프에서 모델 입력은 줄고, 저장된 대화 기록은 원래 검색 결과를 그대로 갖는다."""
        from src.agent import PREVIOUS_TURN_TOOL_RESULT, make_history_trim_middleware
        model = CapturingFakeModel(messages=iter([AIMessage("천만에요")]))
        agent = create_agent(model=model, tools=[], checkpointer=InMemorySaver(), middleware=[make_history_trim_middleware()])
        result = agent.invoke({"messages": self.history()[:7] + [HumanMessage("고마워")]}, {"configurable": {"thread_id": "h1"}})
        sent = model.inputs[0]
        self.assertIn(PREVIOUS_TURN_TOOL_RESULT, [m.content for m in sent])
        self.assertTrue(result["messages"][2].content.startswith("<reference>긴 검색 결과"))


class TestSourceIds(unittest.TestCase):
    """일정표·예산의 출처를 ID로 받고 파이썬이 제목·URL을 채운다. (응답 속도 개선 B)"""

    def setUp(self):
        """검색 결과 두 건이 든 대화 기록과 그 출처 ID."""
        self.url_a, self.url_b = "https://kyoto.travel/ko/getting-around/", "https://www.shokoku-ji.jp/ko/kinkakuji"
        refs = tools.wrap_reference(1, "교토 교통편", self.url_a, "…") + tools.wrap_reference(1, "킨카쿠지", self.url_b, "…")
        self.messages = [HumanMessage("일정 짜줘"), ToolMessage(refs, tool_call_id="c1", name="search_travel_info")]
        self.index = tools.reference_index(self.messages)
        self.id_a, self.id_b = tools.reference_id(self.url_a), tools.reference_id(self.url_b)

    def test_id_is_stable_per_url(self):
        """같은 URL은 몇 번째 검색이든 같은 ID, 다른 URL은 다른 ID다(검색마다 1부터 매기던 번호의 충돌 해소)."""
        self.assertEqual(tools.reference_id(self.url_a), tools.reference_id(self.url_a))
        self.assertNotEqual(self.id_a, self.id_b)
        self.assertEqual(self.index[self.id_a].title, "교토 교통편")

    def test_resolve_source_variants(self):
        """ID·URL 직접 기재는 풀고, 모르는 ID는 None(출처 없음)."""
        self.assertEqual(tools.resolve_source(self.id_b, self.index).url, self.url_b)
        self.assertEqual(tools.resolve_source(f"킨카쿠지 ({self.url_b})", self.index).title, "킨카쿠지")
        self.assertIsNone(tools.resolve_source("r000000", self.index))

    def test_itinerary_renders_title_and_url_from_ids(self):
        """일정표는 ID만 받아도 '출처: 제목 (URL)'로 그리고, 모르는 ID의 입장료에는 출처 없음 표시를 붙인다."""
        days = [tools.DayPlan(day=1, items=[
            tools.ScheduleItem(time="오전", place="교토역", source=self.id_a),
            tools.ScheduleItem(time="오후", place="킨카쿠지",
                               admission=tools.Admission(min_amount=500, max_amount=500, currency="JPY", source=self.id_b)),
            tools.ScheduleItem(time="저녁", place="기온",
                               admission=tools.Admission(min_amount=0, max_amount=1000, currency="JPY", source="r000000"))])]
        text = tools.build_itinerary_text("교토 당일", "교토", 0, 1, "서울", days, source_index=self.index)
        self.assertIn(f"출처: 교토 교통편 ({self.url_a})", text)
        self.assertIn(f"입장료 출처: 킨카쿠지 ({self.url_b})", text)
        gion = text[text.index("기온"):]
        self.assertIn("(출처 없음, 사용자 확인 필요)", gion.splitlines()[1])   # 모르는 ID의 입장료 줄
        self.assertNotIn(self.id_a, text)

    def test_budget_sources_resolved_or_downgraded(self):
        """예산 항목의 출처 ID는 '제목 URL'로 바꾸고, 풀 수 없으면 LLM 추정치로 표시한다."""
        item = lambda src: tools.BudgetItem(name="입장료", min_amount=500, max_amount=500, currency="JPY",
                                            unit="per_person", basis="source", source=src)
        ok, unknown = tools.resolve_budget_sources([item(self.id_b), item("r000000")], self.index)
        self.assertEqual((ok.basis, ok.source), ("source", f"킨카쿠지 {self.url_b}"))
        self.assertEqual((unknown.basis, unknown.source), ("llm_estimate", ""))


class TestItinerarySearchCap(unittest.TestCase):
    """일정표 요청의 검색 라운드 상한. (응답 속도 개선 C)"""

    def turn(self, rounds: int, confirmed: bool = True) -> list:
        """일정 요청 턴: check_trip_info 결과 뒤에 검색 라운드 rounds번."""
        msgs = [HumanMessage("교토 5박6일 일정 짜줘"),
                AIMessage("", tool_calls=[{"name": "check_trip_info", "args": {}, "id": "t0"}]),
                ToolMessage("[확인 완료] 아래 조건으로 일정을 만드세요." if confirmed else "[확인 필요] …",
                            tool_call_id="t0", name="check_trip_info")]
        for i in range(rounds):
            msgs += [AIMessage("", tool_calls=[{"name": "search_travel_info", "args": {}, "id": f"s{i}"},
                                               {"name": "get_weather", "args": {}, "id": f"w{i}"}]),
                     ToolMessage("결과", tool_call_id=f"s{i}", name="search_travel_info"),
                     ToolMessage("날씨", tool_call_id=f"w{i}", name="get_weather")]
        return msgs

    def call(self, messages):
        """limit_itinerary_searches를 가짜 요청에 적용하고 넘어간 tool_choice를 돌려준다."""
        from src.agent import limit_itinerary_searches
        seen = {}
        request = SimpleNamespace(messages=messages, tool_choice=None,
                                  override=lambda **kw: SimpleNamespace(messages=messages, **kw))
        limit_itinerary_searches.wrap_model_call(request, lambda r: seen.setdefault("choice", r.tool_choice))
        return seen["choice"]

    def test_forces_render_after_cap(self):
        """[확인 완료] 뒤 검색 라운드가 상한에 이르면 다음 호출은 render_itinerary로 강제한다."""
        from src import config as cfg
        self.assertIsNone(self.call(self.turn(cfg.ITINERARY_MAX_SEARCH_ROUNDS - 1)))
        self.assertEqual(self.call(self.turn(cfg.ITINERARY_MAX_SEARCH_ROUNDS)), "render_itinerary")

    def test_not_applied_outside_itinerary(self):
        """일정표 작성 단계가 아니면(확인 필요, 일반 질문) 검색이 많아도 강제하지 않는다."""
        self.assertIsNone(self.call(self.turn(5, confirmed=False)))
        general = [HumanMessage("교토 맛집"), *self.turn(5)[3:]]
        self.assertIsNone(self.call(general))


class TestPreferenceQuestions(unittest.TestCase):
    """일정 전에 취향(꼭 갈 곳·여행 스타일·관심사)을 한 번 함께 묻는다. (SERVICE 2.2, 2026-11-05)"""

    def evaluate(self, asked=frozenset(), prefs=None, delegate=False, origin="김포"):
        """필수 항목 중 출발지만 바꿀 수 있는 교토 5박6일 판정."""
        return tools.evaluate_trip_info("교토", 5, 2, origin, 2000000, "total", set(asked), prefs, delegate)

    def test_asks_preferences_once_even_when_required_complete(self):
        """필수 항목이 다 있어도 처음에는 취향을 묻는다."""
        result = self.evaluate()
        self.assertEqual((result["status"], result["ask"]), ("ask", list(tools.PREFERENCE_ITEMS)))

    def test_preferences_asked_together_with_required(self):
        """빠진 필수 항목과 취향은 한 번에 묶어 묻는다."""
        result = self.evaluate(origin="")
        self.assertEqual(result["ask"], ["origin", *tools.PREFERENCE_ITEMS])
        text = tools.render_trip_info(result)
        self.assertIn("물어볼 항목: 출발지", text)
        self.assertIn("취향 질문", text)

    def test_unanswered_preferences_are_decided_not_asked_again(self):
        """한 번 물었는데 일부만 답하면, 답한 것은 확정 조건, 나머지는 다시 묻지 않고 에이전트가 정한다."""
        result = self.evaluate(asked=tools.PREFERENCE_ITEMS, prefs={"must_visit": "기요미즈데라"})
        self.assertEqual(result["status"], "ready")
        self.assertIn("기요미즈데라", result["confirmed"]["must_visit"])
        self.assertEqual(set(result["assumed"]), {"pace", "interests"})
        text = tools.render_trip_info(result)
        self.assertNotIn("가정한 조건", text)                  # 취향 가정은 일정표 맨 앞 '가정한 조건'에 넣지 않는다
        self.assertIn("취향은 에이전트가 정함", text)

    def test_delegated_preferences_skip_question(self):
        """'나머지는 알아서 해줘'로 맡기면 묻지 않고 바로 진행한다."""
        self.assertEqual(self.evaluate(delegate=True)["status"], "ready")

    def test_known_preferences_not_asked(self):
        """대화·기억으로 이미 아는 취향은 묻지 않는다."""
        prefs = {"must_visit": "없음", "pace": "여유롭게", "interests": "맛집"}
        self.assertEqual(self.evaluate(prefs=prefs)["status"], "ready")


class TestBudgetBeforeItinerary(unittest.TestCase):
    """예산 초과 처리 완화(15% 이내 초과는 조정안 없이 진행)와 일정표 전 예산 확인. (응답 속도 개선 K, SERVICE 2.3)"""

    def budget_result(self, total: int, budget: int = 1_000_000) -> dict:
        """일행 총액이 total(KRW)인 당일 일정 예산 계산 결과."""
        item = tools.BudgetItem(name="숙박", category="lodging", min_amount=total, max_amount=total, currency="KRW",
                                unit="group", basis="user_input")
        return tools.compute_budget([item], 1, 0, budget, "total", "KRW")

    def render(self, total: int, adjustments=None) -> str:
        """당일 일정표를 예산 결과와 함께 그린다."""
        days = [tools.DayPlan(day=1, items=[tools.ScheduleItem(time="오전", place="교토역")])]
        return tools.build_itinerary_text("교토 당일", "교토", 0, 1, "서울", days, budget_result=self.budget_result(total),
                                          adjustments=adjustments)

    def test_threshold(self):
        """'초과'라도 초과 폭이 15% 이하면 조정안이 필요 없고, 15%를 넘으면 필요하다."""
        self.assertFalse(tools.needs_adjustments(self.budget_result(1_150_000)["judgement"]))
        self.assertTrue(tools.needs_adjustments(self.budget_result(1_160_000)["judgement"]))
        self.assertFalse(tools.needs_adjustments(self.budget_result(900_000)["judgement"]))

    def test_small_overrun_renders_without_adjustments(self):
        """12% 초과는 조정안 없이 일정표를 내고 초과 금액은 그대로 알린다. 20% 초과는 조정안을 요구한다."""
        text = self.render(1_120_000)
        self.assertIn("초과", text)
        self.assertNotIn("## 예산 조정안", text)
        with self.assertRaises(ValueError):
            self.render(1_200_000)
        self.assertIn("## 예산 조정안", self.render(1_200_000, adjustments=["숙박 등급을 낮춘다"]))

    def turn(self, with_budget: bool, budget_checked: bool = False, rounds: int = 1) -> list:
        """일정 작성 단계 턴: 확인 완료(예산 유무) → 검색 rounds번 (→ 예산 계산)."""
        conditions = "목적지 교토, 2박3일, 인원 1명" + (", 예산 1,000,000" if with_budget else "")
        msgs = [HumanMessage("교토 일정 짜줘"),
                AIMessage("", tool_calls=[{"name": "check_trip_info", "args": {}, "id": "t0"}]),
                ToolMessage(f"[확인 완료] 아래 조건으로 일정을 만드세요.\n- 확정 조건: {conditions}",
                            tool_call_id="t0", name="check_trip_info")]
        for i in range(rounds):
            msgs += [AIMessage("", tool_calls=[{"name": "search_travel_info", "args": {}, "id": f"s{i}"}]),
                     ToolMessage("결과", tool_call_id=f"s{i}", name="search_travel_info")]
        if budget_checked:
            msgs += [AIMessage("", tool_calls=[{"name": "calculate_budget", "args": {}, "id": "b0"}]),
                     ToolMessage("예산 판정: 예산 안", tool_call_id="b0", name="calculate_budget")]
        return msgs

    def choice(self, messages):
        """limit_itinerary_searches가 넘긴 tool_choice."""
        from src.agent import limit_itinerary_searches
        seen = {}
        request = SimpleNamespace(messages=messages, tool_choice=None,
                                  override=lambda **kw: SimpleNamespace(messages=messages, **kw))
        limit_itinerary_searches.wrap_model_call(request, lambda r: seen.setdefault("c", r.tool_choice))
        return seen["c"]

    def test_budget_checked_before_itinerary(self):
        """예산이 있으면 검색 한 라운드 뒤 calculate_budget을 먼저 부르게 하고, 이미 확인했으면 강제하지 않는다.
        예산이 없으면 강제하지 않는다."""
        self.assertEqual(self.choice(self.turn(with_budget=True)), "calculate_budget")
        self.assertIsNone(self.choice(self.turn(with_budget=True, budget_checked=True)))
        self.assertIsNone(self.choice(self.turn(with_budget=False)))
        self.assertIsNone(self.choice(self.turn(with_budget=True, rounds=0)))   # 검색 전에는 강제하지 않는다


class TestAgentModelTimeout(unittest.TestCase):
    def test_long_read_timeout_and_no_retry(self):
        """답변 모델은 긴 일정표 생성을 위해 응답 대기 한도를 늘리고(기본 60초 → 설정값), 시간 초과를 같은 모델로 재시도하지 않는다.
        대체 모델도 같은 설정이다. (응답 속도 개선 A)"""
        from src import config as cfg
        from src.agent import make_fallback_llms, make_llm
        for llm in [make_llm(), *(m for _, m in make_fallback_llms())]:
            client_config = llm.client._client_config
            self.assertEqual(client_config.read_timeout, cfg.BEDROCK_READ_TIMEOUT)
            self.assertEqual(client_config.retries["total_max_attempts"], cfg.BEDROCK_AGENT_MAX_RETRIES + 1)
        self.assertGreater(cfg.BEDROCK_READ_TIMEOUT, 60)


class TestCliErrorMessage(unittest.TestCase):
    def test_throttled_says_wait(self):
        """모델 한도 초과만 '잠시 후'를 안내한다."""
        from main import error_message
        throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "Too many tokens"}}, "Converse")
        self.assertIn("잠시 후", error_message(throttled))

    def test_other_errors_suggest_new_chat(self):
        """그 밖의 오류는 기다리라고 하지 않고, 다시 보내거나 /new로 새 대화를 시작하도록 안내한다."""
        from main import error_message
        for error in (ACCESS_DENIED, ValueError("ValidationException")):
            message = error_message(error)
            self.assertIn("/new", message)
            self.assertNotIn("잠시 후", message)


if __name__ == "__main__":
    unittest.main()
