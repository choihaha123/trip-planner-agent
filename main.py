# main.py - CLI 진입점 (docs/SPEC.md 1.2, 1.3)
# 사용법: python main.py [--user <user_id>] [--thread <thread_id>]
#         python main.py --reset <user_id>   (수집 데이터 초기화)
# 대화 중 명령: /new 새 대화, /exit 종료
import argparse
import logging
import sys

from src.agent import (
    ThreadAccessError,
    ask,
    build_agent,
    check_thread_access,
    new_thread_id,
    open_checkpointer,
    validate_user_id,
)
from src.config import DEFAULT_USER_ID, setup_logging
from src.retriever import reset_user

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """CLI 인자를 읽는다."""
    parser = argparse.ArgumentParser(description="여행 계획 에이전트 CLI")
    parser.add_argument("--user", default=DEFAULT_USER_ID, help="사용자 ID (기본 default)")
    parser.add_argument("--thread", help="이어갈 대화 thread_id")
    parser.add_argument("--reset", metavar="USER_ID", help="해당 user_id의 수집 데이터를 초기화하고 종료 (기본 KB는 유지)")
    return parser.parse_args()


def reset_command(user_id: str) -> int:
    """--reset: user_id의 사용자 수집 데이터를 지우고 결과를 출력한다."""
    try:
        user_id = validate_user_id(user_id)
    except ValueError as e:
        print(f"오류: {e}")
        return 1
    deleted = reset_user(user_id)
    print(f"'{user_id}'의 수집 데이터를 초기화했습니다. (청크 {deleted}개 삭제, 기본 KB는 유지)")
    return 0


def chat_loop(agent, user_id: str, thread_id: str) -> None:
    """사용자 입력을 받아 에이전트 답변을 출력하는 대화 루프."""
    print(f"[user: {user_id}] [thread: {thread_id}]  (/new 새 대화, /exit 종료)")
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
        try:
            print(ask(agent, message, thread_id, user_id))
        except Exception:
            logger.exception("에이전트 호출 실패", extra={"event": "agent_error", "data": {"thread_id": thread_id}})
            print("답변 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
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
