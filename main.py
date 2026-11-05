# main.py - CLI 진입점 (docs/SPEC.md 1.2, 1.3)
# 사용법: python main.py [--user <user_id>] [--thread <thread_id>]
#         python main.py --reset <user_id>   (저장한 링크 모두 지우기)
# 대화 중 명령: /new 새 대화, /saved 저장한 링크 목록, /forget <번호> 링크 지우기,
#               /memory 기억 보기, /forget-memory <번호> 기억 지우기, /exit 종료 (답변 중 Ctrl+C는 그 답변만 취소)
import argparse
import logging
import re
import sys

from src.agent import (
    ThreadAccessError,
    ask,
    build_agent,
    check_thread_access,
    is_throttled,
    new_thread_id,
    open_checkpointer,
    validate_user_id,
)
from src.config import DEFAULT_USER_ID, MEMORY_FIELDS, setup_logging
from src.memory import delete_memory, get_memory, reset_memory
from src.middleware import extract_urls
from src.retriever import delete_user_source, list_user_pages, reset_user

logger = logging.getLogger(__name__)

# 시작 화면·--help에 보여 줄 링크 저장 기능 안내 (UX 평가: 이 기능을 알 방법이 없었다)
LINK_TIP = "여행 글 링크를 붙여넣으면 저장해 두었다가, 나중 대화에서도 그 글을 근거로 답합니다."
COMMANDS_HELP = ("/new 새 대화, /saved 저장한 링크 목록, /forget <번호> 링크 지우기, "
                 "/memory 기억 보기, /forget-memory <번호> 기억 지우기, /exit 종료")


def parse_args() -> argparse.Namespace:
    """CLI 인자를 읽는다."""
    parser = argparse.ArgumentParser(
        description=f"여행 계획 에이전트 CLI. {LINK_TIP}",
        epilog=f"대화 중 명령: {COMMANDS_HELP}. 답변 중 Ctrl+C는 그 답변만 취소합니다.")
    parser.add_argument("--user", default=DEFAULT_USER_ID, help="사용자 ID (기본 default)")
    parser.add_argument("--thread", help="이어갈 대화 thread_id")
    parser.add_argument("--reset", metavar="USER_ID",
                        help="해당 user_id가 저장한 링크와 기억을 모두 지우고 종료 (기본 자료·대화 기록은 유지)")
    return parser.parse_args()


def reset_command(user_id: str) -> int:
    """--reset: user_id가 저장한 링크와 장기 기억을 함께 지우고 결과를 출력한다. (SERVICE 2.7, 2.9)"""
    try:
        user_id = validate_user_id(user_id)
    except ValueError as e:
        print(f"오류: {e}")
        return 1
    pages, _ = reset_user(user_id)
    memories = reset_memory(user_id)
    if not pages and not memories:
        print(f"'{user_id}'이(가) 저장한 링크와 기억이 없습니다.")
    else:
        print(f"'{user_id}'이(가) 저장한 링크 {pages}개와 기억 {memories}개를 지웠습니다. (기본 여행 자료는 그대로입니다)")
    return 0


def memory_items(user_id: str) -> list[tuple[str, str]]:
    """현재 user_id의 기억을 (항목 키, 값) 목록으로 돌려준다. 순서는 설정 목록(MEMORY_FIELDS) 순서라 /memory 번호와 같다."""
    return list(get_memory(user_id).items())


def memory_command(user_id: str) -> str:
    """/memory: 현재 user_id의 장기 기억을 번호·항목·값으로 보여 준다. LLM을 거치지 않는다. (SERVICE 2.9)"""
    items = memory_items(user_id)
    if not items:
        return "기억하고 있는 정보가 없습니다. 이름·여행 선호·답변 스타일을 말씀해 주시면 기억해 둡니다."
    lines = [f"기억하고 있는 정보 {len(items)}개  (지우려면 /forget-memory <번호>)"]
    lines += [f" {i}. {MEMORY_FIELDS[field]}: {value}" for i, (field, value) in enumerate(items, 1)]
    return "\n".join(lines)


def forget_memory_command(user_id: str, arg: str) -> str:
    """/forget-memory <번호 또는 항목 이름>: 그 기억 항목을 현재 user_id에서만 지운다. 예: /forget-memory 2, /forget-memory 여행 선호"""
    items = memory_items(user_id)
    if not items:
        return "기억하고 있는 정보가 없습니다."
    if arg.isdigit():
        matches = [items[int(arg) - 1][0]] if 1 <= int(arg) <= len(items) else []
    else:  # 항목 키(name) 또는 표시 이름의 일부('이름', '선호' 등). 정확히 한 항목에 맞을 때만 지운다.
        key = re.sub(r"[\s·]", "", arg)
        matches = [f for f, _ in items if key and (key == f or key in re.sub(r"[\s·]", "", MEMORY_FIELDS[f]))]
    if len(matches) != 1:
        numbers = "1" if len(items) == 1 else f"1~{len(items)}"
        return f"지울 기억의 번호나 항목 이름을 확인해 주세요(가능한 번호: {numbers}). 예: /forget-memory 1 (목록은 /memory)"
    field = matches[0]
    delete_memory(user_id, field)
    return f"'{MEMORY_FIELDS[field]}' 기억을 지웠습니다. 이제 답변에 쓰지 않습니다."


def error_message(error: Exception) -> str:
    """답변 실패 시 사용자에게 보여 줄 안내를 원인별로 고른다. (SPEC 1.3)
    기다리면 풀리는 경우(모델 한도 초과)에만 '잠시 후'를 안내하고, 그 밖에는 다시 보내거나 새 대화를 시작하도록 안내한다."""
    if is_throttled(error):
        return "지금 요청이 많아 답변하지 못했습니다. 잠시 후 같은 질문을 다시 보내 주세요."
    return "답변 중 오류가 발생했습니다. 같은 질문을 다시 보내 보시고, 계속 안 되면 /new로 새 대화를 시작해 주세요."


def saved_command(user_id: str) -> str:
    """/saved: 현재 user_id가 저장한 링크를 번호·제목·URL·저장일로 보여 준다. LLM을 거치지 않는다. (SERVICE 2.1)"""
    pages = list_user_pages(user_id)
    if not pages:
        return "저장한 링크가 없습니다. 여행 글 링크를 붙여넣으면 저장됩니다."
    lines = [f"저장한 링크 {len(pages)}개  (지우려면 /forget <번호>)"]
    for i, p in enumerate(pages, 1):
        day = p["collected_at"][5:10].replace("-", "/")  # '2026-11-05T…' → '11/05'
        lines.append(f" {i}. {p['title'] or p['source_url']} ({p['source_url']})" + (f"  {day} 저장" if day else ""))
    return "\n".join(lines)


def forget_command(user_id: str, arg: str) -> str:
    """/forget <번호>: /saved 목록의 그 번호 링크를 현재 user_id 자료에서만 지운다. 번호는 저장한 순서라 /saved와 같다."""
    pages = list_user_pages(user_id)
    if not arg.isdigit() or not 1 <= int(arg) <= len(pages):
        numbers = "1" if len(pages) == 1 else f"1~{len(pages)}"
        return (f"지울 링크 번호를 확인해 주세요(가능한 번호: {numbers}). 예: /forget 1 (목록은 /saved)" if pages
                else "저장한 링크가 없습니다.")
    page = pages[int(arg) - 1]
    delete_user_source(user_id, page["source_url"])
    return f"'{page['title'] or page['source_url']}' 링크를 지웠습니다. 이제 답변 근거로 쓰지 않습니다."


def chat_loop(agent, user_id: str, thread_id: str) -> None:
    """사용자 입력을 받아 에이전트 답변을 출력하는 대화 루프. 답변 중 Ctrl+C는 그 답변만 취소하고 대화를 이어 간다."""
    print(f"[user: {user_id}] [thread: {thread_id}]  ({COMMANDS_HELP})")
    print(f"팁: {LINK_TIP}")
    while True:
        try:
            message = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue
        if message == "/exit":
            break
        if message == "/new":
            thread_id = new_thread_id()
            print(f"새 대화를 시작합니다. [thread: {thread_id}]")
            continue
        if message == "/saved":
            print(saved_command(user_id))
            continue
        if message == "/forget" or message.startswith("/forget "):
            print(forget_command(user_id, message[len("/forget"):].strip()))
            continue
        if message == "/memory":
            print(memory_command(user_id))
            continue
        if message == "/forget-memory" or message.startswith("/forget-memory "):
            print(forget_memory_command(user_id, message[len("/forget-memory"):].strip()))
            continue
        urls, _ = extract_urls(message)
        if urls:  # 링크 수집·판정은 몇 초 걸리므로 기다리는 이유를 먼저 보여 준다.
            print(f"링크 {len(urls)}개 확인 중…", flush=True)
        try:
            print(ask(agent, message, thread_id, user_id))
        except KeyboardInterrupt:
            logger.info("답변 취소(Ctrl+C)", extra={"event": "answer_cancelled", "data": {"thread_id": thread_id}})
            print("\n답변을 취소했습니다. 다른 질문을 입력하거나 /exit로 종료하세요.")
        except Exception as e:
            logger.exception("에이전트 호출 실패", extra={"event": "agent_error", "data": {
                "thread_id": thread_id, "throttled": is_throttled(e)}})
            print(error_message(e))
    print(f"대화를 마칩니다. 이어가려면: python main.py --user {user_id} --thread {thread_id}")


def main() -> int:
    """인자를 검사하고 에이전트를 만든 뒤 대화 루프를 시작한다."""
    setup_logging()
    args = parse_args()
    if args.reset:
        return reset_command(args.reset)
    try:
        user_id = validate_user_id(args.user)
    except ValueError as e:
        print(f"오류: {e}")
        return 1

    checkpointer = open_checkpointer()
    if args.thread:
        try:
            check_thread_access(checkpointer, args.thread, user_id)
        except ThreadAccessError as e:
            logger.warning("thread 접근 거부", extra={"event": "thread_access_denied",
                                                     "data": {"thread_id": args.thread, "user_id": user_id}})
            print(f"오류: {e}")
            return 1
        thread_id = args.thread
    else:
        thread_id = new_thread_id()

    agent = build_agent(checkpointer)
    chat_loop(agent, user_id, thread_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
