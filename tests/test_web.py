# test_web.py - 웹 API 단위 테스트 (docs/SPEC.md 2.10 / TASKS Step 15)
# 실행: python -m unittest discover tests
# 실제 storage/web.sqlite 대신 임시 폴더의 DB를 쓴다. Bedrock은 호출하지 않는다.
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import json

from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from src import config, memory
from src.agent import AgentContext, system_prompt_with_date
from src.middleware import UrlOutcome
from src.web import app as web_app
from src.web import auth, threads
from src.web.app import app


class WebTestCase(unittest.TestCase):
    def setUp(self):
        """테스트마다 빈 임시 웹 DB를 쓴다."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(config, "WEB_DB", Path(tmp.name) / "web.sqlite")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)

    def signup(self, email="a@example.com", password="password1", client=None):
        """회원가입 요청을 보낸다."""
        return (client or self.client).post("/api/signup", json={"email": email, "password": password})


class TestPassword(unittest.TestCase):
    def test_hash_and_verify(self):
        """비밀번호는 원문이 아닌 해시로 저장되고, 맞는 비밀번호만 통과한다. 같은 비밀번호도 해시가 매번 다르다. (Step 15-1)"""
        stored = auth.hash_password("password1")
        self.assertNotIn("password1", stored)
        self.assertTrue(auth.verify_password("password1", stored))
        self.assertFalse(auth.verify_password("password2", stored))
        self.assertNotEqual(stored, auth.hash_password("password1"))
        self.assertFalse(auth.verify_password("password1", "망가진 값"))


class TestAuthApi(WebTestCase):
    def test_signup_logs_in(self):
        """회원가입하면 바로 로그인되어 /api/me가 이메일을 돌려준다. 내부 user_id는 내보내지 않는다. (Step 15-1)"""
        self.assertEqual(self.signup(" A@Example.com ").status_code, 200)
        me = self.client.get("/api/me").json()
        self.assertEqual(me, {"email": "a@example.com"})

    def test_duplicate_email_rejected(self):
        """이메일은 대소문자·공백을 무시하고 중복될 수 없다."""
        self.signup("a@example.com")
        res = self.signup("A@EXAMPLE.COM ", client=TestClient(app))
        self.assertEqual((res.status_code, res.json()["detail"]), (400, "이미 가입된 이메일입니다."))

    def test_signup_validation(self):
        """이메일 형식과 비밀번호 최소 길이를 검사한다."""
        self.assertEqual(self.signup("not-an-email").json()["detail"], "이메일 형식이 올바르지 않습니다.")
        self.assertIn("8자 이상", self.signup("b@example.com", "short").json()["detail"])

    def test_login_failure_same_message(self):
        """틀린 비밀번호와 없는 이메일은 같은 문구로 401을 돌려준다(이메일 존재 여부를 숨김)."""
        self.signup("a@example.com")
        client = TestClient(app)
        wrong_pw = client.post("/api/login", json={"email": "a@example.com", "password": "wrong-pass"})
        no_user = client.post("/api/login", json={"email": "x@example.com", "password": "password1"})
        self.assertEqual((wrong_pw.status_code, no_user.status_code), (401, 401))
        self.assertEqual(wrong_pw.json()["detail"], no_user.json()["detail"])

    def test_login_logout_flow(self):
        """로그인하면 세션 쿠키(HttpOnly)가 생기고, 로그아웃하면 /api/me가 401이 된다."""
        self.signup("a@example.com")
        client = TestClient(app)
        self.assertEqual(client.get("/api/me").status_code, 401)
        res = client.post("/api/login", json={"email": "a@example.com", "password": "password1"})
        self.assertIn("httponly", res.headers["set-cookie"].lower())
        self.assertEqual(client.get("/api/me").status_code, 200)
        client.post("/api/logout")
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_session_token_not_stored_raw(self):
        """세션 토큰은 DB에 해시로만 저장된다."""
        self.signup("a@example.com")
        token = self.client.cookies.get(config.WEB_SESSION_COOKIE)
        with closing(auth.connect()) as conn:
            stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")]
        self.assertNotIn(token, stored)
        self.assertEqual(len(stored), 1)

    def test_expired_session_rejected(self):
        """만료된 세션은 거부되고 지워진다."""
        self.signup("a@example.com")
        with patch.object(auth, "_now", return_value=auth._now() + timedelta(days=config.WEB_SESSION_DAYS + 1)):
            self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_users_get_distinct_internal_ids(self):
        """계정마다 다른 내부 user_id(u_…)가 생기고 이메일과 다르다."""
        a = auth.signup("a@example.com", "password1")
        b = auth.signup("b@example.com", "password1")
        self.assertTrue(a.startswith("u_") and b.startswith("u_"))
        self.assertNotEqual(a, b)


class FakeModel(GenericFakeChatModel):
    """정해 둔 답을 차례로 내는 가짜 채팅 모델(bind_tools는 자신을 돌려줌).
    웹은 글자 스트리밍을 쓰므로, 스트리밍할 때도 도구 호출을 잃지 않게 조각(tool_call_chunks)으로 내보낸다."""

    def bind_tools(self, tools, **kwargs):
        """도구 목록은 쓰지 않는다."""
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """정해 둔 답 하나를 글자 조각(어절 단위)과 도구 호출 조각으로 내보낸다."""
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk
        message = next(self.messages)
        pieces = [w + " " for w in message.content.split(" ")] if message.content else []
        if pieces:
            pieces[-1] = pieces[-1].rstrip()
        for piece in pieces:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk
        if message.tool_calls:
            calls = [{"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                     for i, c in enumerate(message.tool_calls)]
            chunk = ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=calls))
            if run_manager:
                run_manager.on_llm_new_token("", chunk=chunk)
            yield chunk


@tool
def search_travel_info(query: str) -> str:
    """테스트용 검색 도구."""
    return '<reference id="1" title="오사카 음식" url="https://example.com">오코노미야키</reference>'


def turn(answer: str) -> list:
    """검색 도구를 한 번 부르고 답하는 한 턴의 모델 응답."""
    return [AIMessage("", tool_calls=[{"name": "search_travel_info", "args": {"query": "q"}, "id": f"c-{answer}"}]),
            AIMessage(answer)]


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """SSE 응답 본문을 (event, data) 목록으로 바꾼다."""
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


class ChatTestCase(WebTestCase):
    def setUp(self):
        """가짜 모델로 만든 실제 에이전트 그래프를 웹 앱에 끼워 넣고, 두 계정 클라이언트를 만든다."""
        super().setUp()
        self.answers = [*turn("오코노미야키를 추천해요"), *turn("타코야키도 좋아요")]
        self.saver = InMemorySaver()
        self.agent = create_agent(model=FakeModel(messages=iter(self.answers)), tools=[search_travel_info],
                                  checkpointer=self.saver, context_schema=AgentContext)
        patcher = patch.object(web_app, "get_agent", return_value=(self.agent, self.saver))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.signup("a@example.com")
        self.bob = TestClient(app)
        self.signup("b@example.com", client=self.bob)

    def send(self, thread_id: str, content: str, client=None):
        """메시지를 보내고 SSE 이벤트 목록을 돌려준다."""
        res = (client or self.client).post(f"/api/threads/{thread_id}/messages", json={"content": content})
        return res, (parse_sse(res.text) if res.status_code == 200 else [])


class TestThreadsApi(ChatTestCase):
    def test_create_list_and_isolation(self):
        """대화를 만들면 내 목록에만 보이고, 남의 대화는 조회·전송·삭제 모두 404다. (Step 15-2)"""
        thread_id = self.client.post("/api/threads").json()["thread_id"]
        self.assertEqual([t["thread_id"] for t in self.client.get("/api/threads").json()], [thread_id])
        self.assertEqual(self.bob.get("/api/threads").json(), [])
        self.assertEqual(self.bob.get(f"/api/threads/{thread_id}/messages").status_code, 404)
        self.assertEqual(self.send(thread_id, "안녕", client=self.bob)[0].status_code, 404)
        self.assertEqual(self.bob.delete(f"/api/threads/{thread_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/threads/nosuch/messages").status_code, 404)

    def test_send_streams_progress_then_answer(self):
        """메시지를 보내면 진행 단계(질문 확인 → 자료 검색 → 답변 작성), 답변 글자(delta), 최종 답변(done)이 SSE로 오고,
        제목이 첫 질문으로 정해진다. (응답 속도 개선: 스트리밍)"""
        thread_id = self.client.post("/api/threads").json()["thread_id"]
        res, events = self.send(thread_id, "오사카 저녁 메뉴 하나 추천해줘")
        self.assertTrue(res.headers["content-type"].startswith("text/event-stream"))
        stages = [d["stage"] for e, d in events if e == "progress"]
        self.assertEqual(stages, ["질문 확인 중", "자료 검색 중", "답변 작성 중"])
        deltas = [d["text"] for e, d in events if e == "delta"]
        self.assertGreater(len(deltas), 1)                                  # 여러 조각으로 나뉘어 온다
        self.assertEqual("".join(deltas), "오코노미야키를 추천해요")
        self.assertEqual(events[-1], ("done", {"answer": "오코노미야키를 추천해요", "title": "오사카 저녁 메뉴 하나 추천해줘"}))
        self.assertEqual(self.client.get("/api/threads").json()[0]["title"], "오사카 저녁 메뉴 하나 추천해줘")

    def test_text_before_tool_call_is_reset(self):
        """도구를 부르기 전에 흘러나온 짧은 글('찾아볼게요')은 draft_reset으로 초안에서 지운다."""
        self.answers[0] = AIMessage("찾아볼게요", tool_calls=self.answers[0].tool_calls)
        thread_id = self.client.post("/api/threads").json()["thread_id"]
        _, events = self.send(thread_id, "오사카 저녁 메뉴")
        kinds = [e for e, _ in events]
        self.assertLess(kinds.index("draft_reset"), kinds.index("done"))
        after_reset = [d["text"] for e, d in events[kinds.index("draft_reset"):] if e == "delta"]
        self.assertEqual("".join(after_reset), "오코노미야키를 추천해요")

    def test_history_hides_tool_steps(self):
        """지난 메시지는 사용자 질문과 최종 답변만 보여 주고 도구 호출·결과는 뺀다. 두 번째 메시지는 제목을 바꾸지 않는다."""
        thread_id = self.client.post("/api/threads").json()["thread_id"]
        self.send(thread_id, "오사카 저녁 메뉴 추천해줘")
        self.send(thread_id, "다른 것도")
        body = self.client.get(f"/api/threads/{thread_id}/messages").json()
        self.assertEqual(body["messages"], [
            {"role": "user", "content": "오사카 저녁 메뉴 추천해줘"}, {"role": "assistant", "content": "오코노미야키를 추천해요"},
            {"role": "user", "content": "다른 것도"}, {"role": "assistant", "content": "타코야키도 좋아요"}])
        self.assertEqual(body["thread"]["title"], "오사카 저녁 메뉴 추천해줘")

    def test_message_validation(self):
        """빈 메시지와 너무 긴 메시지는 400."""
        thread_id = self.client.post("/api/threads").json()["thread_id"]
        self.assertEqual(self.send(thread_id, "   ")[0].status_code, 400)
        self.assertEqual(self.send(thread_id, "가" * (config.WEB_MESSAGE_MAX_CHARS + 1))[0].status_code, 400)

    def test_error_event_and_lock_released(self):
        """에이전트가 실패하면 error 이벤트로 원인별 안내를 보내고, 잠금이 풀려 다음 메시지는 정상 처리된다."""
        thread_id = self.client.post("/api/threads").json()["thread_id"]
        with patch.object(self.agent, "stream", side_effect=RuntimeError("boom")):
            _, events = self.send(thread_id, "안녕")
        self.assertEqual(events[-1][0], "error")
        self.assertIn("/new", events[-1][1]["message"])
        self.assertFalse(web_app.AGENT_LOCK.locked())
        self.assertEqual(self.send(thread_id, "오사카 저녁 메뉴")[1][-1][0], "done")

    def test_delete_removes_list_and_history(self):
        """대화를 지우면 목록과 대화 기록(체크포인트) 모두에서 사라진다."""
        thread_id = self.client.post("/api/threads").json()["thread_id"]
        self.send(thread_id, "오사카 저녁 메뉴")
        self.assertEqual(self.client.delete(f"/api/threads/{thread_id}").json(), {"ok": True})
        self.assertEqual(self.client.get("/api/threads").json(), [])
        self.assertIsNone(self.saver.get_tuple({"configurable": {"thread_id": thread_id}}))

    def test_requires_login(self):
        """로그인하지 않으면 대화 API는 401."""
        self.assertEqual(TestClient(app).get("/api/threads").status_code, 401)

    def test_serves_screen_files(self):
        """'/'는 화면 HTML, /static/ 아래는 CSS·JS를 돌려준다(로그인 없이도 화면은 열리고, 로그인 여부는 화면이 확인). (Step 15-3)"""
        client = TestClient(app)
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('<script src="/static/app.js">', page.text)
        self.assertIn("javascript", client.get("/static/app.js").headers["content-type"])
        self.assertIn("text/css", client.get("/static/style.css").headers["content-type"])

    def test_make_title(self):
        """제목은 공백을 정리하고 설정 길이를 넘으면 자른다."""
        self.assertEqual(threads.make_title("오사카\n  저녁"), "오사카 저녁")
        long = threads.make_title("가" * 50)
        self.assertEqual(len(long), config.WEB_TITLE_MAX_CHARS + 1)
        self.assertTrue(long.endswith("…"))


class TestLinksApi(WebTestCase):
    """링크 패널 API (Step 15-4). 저장소(Chroma)·수집·판정 대신 user_id별 메모리 저장소를 쓴다."""

    def setUp(self):
        super().setUp()
        self.store = {}  # user_id → [{source_url, title, collected_at}]

        def fake_process(urls, user_id):
            outcomes = []
            for url in urls:
                if "news" in url:
                    outcomes.append(UrlOutcome(url, "rejected_low_score", "뉴스"))
                    continue
                self.store.setdefault(user_id, []).append(
                    {"source_url": url, "title": f"여행 글 {url[-1]}", "collected_at": "2026-11-05T10:00:00+09:00"})
                outcomes.append(UrlOutcome(url, "saved", f"여행 글 {url[-1]}"))
            return outcomes

        def fake_delete(user_id, url):
            self.store[user_id] = [p for p in self.store.get(user_id, []) if p["source_url"] != url]

        for name, fake in (("process_urls", fake_process), ("delete_user_source", fake_delete),
                           ("list_user_pages", lambda user_id: list(self.store.get(user_id, [])))):
            patcher = patch.object(web_app, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.signup("a@example.com")
        self.bob = TestClient(app)
        self.signup("b@example.com", client=self.bob)

    def test_add_returns_notice_and_list(self):
        """붙여넣은 글의 URL을 처리해 대화와 같은 '링크 처리 결과'(✔ 저장함 / ✖ 못 씀과 이유)와 목록을 돌려준다."""
        res = self.client.post("/api/links", json={"text": "https://blog.example.com/a\nhttps://news.example.com/n"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["notice"].startswith("※ 링크 처리 결과"))
        self.assertIn("✔ 저장함: 여행 글 a (https://blog.example.com/a)", data["notice"])
        self.assertIn("✖ 못 씀: https://news.example.com/n — 여행 관련 내용이 아니라고", data["notice"])
        self.assertEqual(data["saved"], 1)
        self.assertEqual(data["links"], [{"url": "https://blog.example.com/a", "title": "여행 글 a", "saved_at": "2026-11-05"}])
        self.assertEqual(self.client.get("/api/links").json(), data["links"])

    def test_add_validation(self):
        """URL이 없거나 너무 길면 400이고 처리하지 않는다. 로그인하지 않으면 401."""
        self.assertEqual(self.client.post("/api/links", json={"text": "오사카 블로그"}).status_code, 400)
        self.assertEqual(self.client.post("/api/links", json={"text": "https://a.com/" + "x" * config.WEB_MESSAGE_MAX_CHARS}).status_code, 400)
        web_app.process_urls.assert_not_called()
        self.assertEqual(TestClient(app).get("/api/links").status_code, 401)

    def test_isolation_and_delete(self):
        """남의 링크는 목록에 보이지 않고 지울 수도 없다(404). 내 링크는 지우면 목록에서 빠진다."""
        self.client.post("/api/links", json={"text": "https://blog.example.com/a https://blog.example.com/b"})
        self.assertEqual(self.bob.get("/api/links").json(), [])
        self.assertEqual(self.bob.delete("/api/links", params={"url": "https://blog.example.com/a"}).status_code, 404)
        self.assertEqual(len(self.client.get("/api/links").json()), 2)
        res = self.client.delete("/api/links", params={"url": "https://blog.example.com/a"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual([l["url"] for l in res.json()["links"]], ["https://blog.example.com/b"])
        self.assertEqual(self.client.delete("/api/links", params={"url": "https://blog.example.com/a"}).status_code, 404)


class TestMemoryApi(WebTestCase):
    """기억 패널 API (Step 15-4 ②). 실제 storage/memory.sqlite 대신 임시(:memory:) 저장소를 쓴다."""

    def setUp(self):
        super().setUp()
        store = memory.open_memory_store(":memory:")
        patcher = patch.object(memory, "get_memory_store", return_value=store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.signup("a@example.com")
        self.bob = TestClient(app)
        self.signup("b@example.com", client=self.bob)
        self.alice_id = auth.user_from_token(self.client.cookies.get(config.WEB_SESSION_COOKIE))["user_id"]

    def test_list_and_delete(self):
        """내 기억이 설정 순서대로(항목 키·표시 이름·값) 보이고, 항목 하나를 지우면 목록에서 빠진다. 없는 항목은 404."""
        memory.save_memory(self.alice_id, "answer_style", "짧게")
        memory.save_memory(self.alice_id, "name", "민수")
        self.assertEqual(self.client.get("/api/memory").json(), [
            {"field": "name", "label": "이름·호칭", "value": "민수"},
            {"field": "answer_style", "label": "답변 스타일", "value": "짧게"}])
        res = self.client.delete("/api/memory/name")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([m["field"] for m in res.json()["memory"]], ["answer_style"])
        self.assertEqual(self.client.delete("/api/memory/name").status_code, 404)
        self.assertEqual(self.client.delete("/api/memory/no_such_field").status_code, 404)

    def test_isolation(self):
        """남의 기억은 보이지 않고 지울 수도 없다(404). 로그인하지 않으면 401."""
        memory.save_memory(self.alice_id, "name", "민수")
        self.assertEqual(self.bob.get("/api/memory").json(), [])
        self.assertEqual(self.bob.delete("/api/memory/name").status_code, 404)
        self.assertEqual(len(self.client.get("/api/memory").json()), 1)
        self.assertEqual(TestClient(app).get("/api/memory").status_code, 401)


class CaptureModel(FakeModel):
    """받은 시스템 프롬프트를 기록하는 가짜 모델."""
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """첫 메시지(시스템 프롬프트)를 기록하고 정해 둔 답을 낸다."""
        self.seen.append(messages[0].content)
        return super()._generate(messages, stop, run_manager, **kwargs)


class TestSavedLinksGuide(unittest.TestCase):
    def test_guide_depends_on_channel(self):
        """저장 링크·기억 관리 안내는 CLI면 /saved·/memory 명령, 웹이면 '저장한 링크'·'기억' 버튼이다. (Step 15-4)"""
        cases = (("cli", "'/saved'", "'저장한 링크' 버튼"), ("web", "'저장한 링크' 버튼", "/saved"),
                 ("cli", "'/memory'", "'기억' 버튼"), ("web", "'기억' 버튼", "/memory"))
        for channel, expected, unexpected in cases:
            model = CaptureModel(messages=iter([AIMessage("네")]))
            model.seen = []
            agent = create_agent(model=model, tools=[], middleware=[system_prompt_with_date], context_schema=AgentContext)
            agent.invoke({"messages": [{"role": "user", "content": "저장한 링크 보여줘"}]},
                         context=AgentContext(user_id="", channel=channel))
            self.assertIn(expected, model.seen[0])
            self.assertNotIn(unexpected, model.seen[0])
            self.assertNotIn("{saved_links_guide}", model.seen[0])
            self.assertNotIn("{memory_guide}", model.seen[0])

    def test_memory_notice_hint_depends_on_channel(self):
        """기억 알림 끝의 '보기' 안내도 CLI는 /memory, 웹은 '기억' 버튼이다. 화면이 알림으로 인식하는 시작 문구는 같다."""
        self.assertTrue(memory.build_memory_notice({"name": "민수"}).endswith("(보기: /memory)"))
        web = memory.build_memory_notice({"name": "민수"}, "web")
        self.assertTrue(web.startswith(memory.MEMORY_NOTICE_PREFIX))
        self.assertIn("'기억' 버튼", web)
        self.assertNotIn("/memory", web)


if __name__ == "__main__":
    unittest.main()
