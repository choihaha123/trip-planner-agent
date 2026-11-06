# runs.py - 웹 대화별 백그라운드 답변 실행 관리 (docs/SPEC.md 2.10, TASKS Phase 7 R4)
# 답변은 대화(thread)마다 백그라운드 스레드에서 끝까지 실행하고, 진행 이벤트(진행 단계·글자·최종 답변)를 실행 객체에 쌓는다.
# 화면(SSE)은 쌓인 이벤트를 구독만 하므로, 연결이 끊겨도(새로고침·탭 닫기) 답변·링크 안내·기억 저장은 끝까지 된다.
# 다시 열면 처음부터 이벤트를 재생해 이어 본다. 같은 대화는 한 번에 하나만, 서버 전체는 동시에 WEB_MAX_CONCURRENT_RUNS개까지 돈다.
# 에이전트는 모르고, app.py가 넘긴 '한 턴 실행 함수'(work)를 돌린다.
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

from src import config

logger = logging.getLogger(__name__)

STOPPED_MESSAGE = "답변을 멈췄습니다. 이어서 질문하시면 됩니다."
WAITING_STAGE = "다른 요청이 많아 잠시 기다리는 중"


class RunBusy(Exception):
    """이 대화는 이미 답변 중이다."""


@dataclass
class Run:
    """대화 하나의 진행 중인 답변. events는 (이벤트 이름, 데이터)를 순서대로 쌓기만 한다."""
    thread_id: str
    user_id: str
    content: str  # 이번에 보낸 사용자 메시지 (지난 메시지 화면에서 진행 중인 턴을 가리는 데 쓴다)
    events: list[tuple[str, dict]] = field(default_factory=list)
    done: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    cond: threading.Condition = field(default_factory=threading.Condition)
    worker: threading.Thread | None = None

    def emit(self, event: str, data: dict) -> None:
        """이벤트 하나를 쌓고 구독자를 깨운다."""
        with self.cond:
            self.events.append((event, data))
            self.cond.notify_all()

    def finish(self) -> None:
        """실행이 끝났음을 알린다(구독자는 남은 이벤트를 다 받은 뒤 끝난다)."""
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def subscribe(self) -> Iterator[tuple[str, dict] | None]:
        """처음 이벤트부터 끝까지 내보낸다. 새 이벤트가 WEB_AGENT_WAIT_SECONDS 동안 없으면 None(연결 유지 신호)."""
        index = 0
        while True:
            with self.cond:
                if index >= len(self.events) and not self.done:
                    self.cond.wait(timeout=config.WEB_AGENT_WAIT_SECONDS)
                batch, index = self.events[index:], len(self.events)
                finished = self.done and index >= len(self.events)
            yield from batch
            if finished:
                return
            if not batch:
                yield None


_runs: dict[str, Run] = {}
_registry_lock = threading.Lock()  # _runs 목록을 보호한다(같은 대화 중복 실행 방지)
_slots = threading.BoundedSemaphore(config.WEB_MAX_CONCURRENT_RUNS)  # 서버 전체 동시 답변 수


def get_run(thread_id: str) -> Run | None:
    """진행 중인 답변(없으면 None)."""
    with _registry_lock:
        return _runs.get(thread_id)


def running_ids() -> set[str]:
    """지금 답변 중인 대화 id들."""
    with _registry_lock:
        return set(_runs)


def start_run(thread_id: str, user_id: str, content: str, work: Callable[[Run], None]) -> Run:
    """대화의 답변을 백그라운드 스레드로 시작한다. 이미 답변 중이면 RunBusy.
    실행 자리가 날 때까지 '잠시 기다리는 중'을 보내며 기다리고, 기다리는 중에 중지하면 바로 끝낸다.
    work(run)는 run.emit으로 이벤트를 보내고 run.cancel을 확인해 멈춘다. 끝나면 목록에서 빼고 구독자에게 알린다."""
    with _registry_lock:
        if thread_id in _runs:
            raise RunBusy(thread_id)
        run = _runs[thread_id] = Run(thread_id, user_id, content)

    def target() -> None:
        """자리를 얻어 work를 실행하고, 무슨 일이 있어도 목록에서 빼고 끝을 알린다."""
        try:
            while not _slots.acquire(timeout=config.WEB_AGENT_WAIT_SECONDS):
                if run.cancel.is_set():
                    run.emit("stopped", {"message": STOPPED_MESSAGE})
                    return
                run.emit("progress", {"stage": WAITING_STAGE})
            try:
                work(run)
            finally:
                _slots.release()
        except Exception:  # work가 처리하지 못한 예외: 구독자가 끝없이 기다리지 않게 오류로 끝낸다
            logger.exception("웹 답변 실행 실패", extra={"event": "web_run_crashed", "data": {"thread_id": thread_id}})
            run.emit("error", {"message": "답변을 만들지 못했습니다. 같은 질문을 다시 보내 주세요."})
        finally:
            with _registry_lock:
                _runs.pop(thread_id, None)
            run.finish()

    run.worker = threading.Thread(target=target, name=f"web-run-{thread_id}", daemon=True)
    run.worker.start()
    return run


def stop_run(thread_id: str) -> bool:
    """답변 중이면 멈춤 신호를 보낸다(다음 이벤트에서 멈춘다). 보냈으면 True."""
    run = get_run(thread_id)
    if run is None:
        return False
    run.cancel.set()
    logger.info("웹 답변 중지 요청", extra={"event": "web_run_stop", "data": {"thread_id": thread_id}})
    return True


def wait_finished(thread_id: str, timeout: float) -> bool:
    """답변이 끝날 때까지 최대 timeout초 기다린다. 끝났거나 원래 없었으면 True."""
    run = get_run(thread_id)
    if run is None or run.worker is None:
        return True
    run.worker.join(timeout)
    return not run.worker.is_alive()
