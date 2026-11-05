# tracing.py - 실행 추적 미들웨어 (docs/SPEC.md 1.2, 1.5)
# 매 턴 LLM 호출·도구 호출이 끝날 때마다 한 줄씩 기록한다. 무엇을 남길지만 정하고,
# 저장 위치·형식(logs/trace.jsonl, JSON Lines)은 src/config.py 의 setup_logging 이 정한다.
# 참고: ref/local_tracer.py (콜백 방식) → 여기서는 create_agent 미들웨어로 구현
import json
import logging
import time

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config

from src import config

logger = logging.getLogger(config.TRACE_LOGGER_NAME)


def _clip(value) -> str:
    """본문을 설정 길이(TRACE_TEXT_LIMIT)로 자른 문자열로 만든다."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return value[: config.TRACE_TEXT_LIMIT]


def _text(message) -> str:
    """메시지 content(문자열 또는 블록 리스트)에서 텍스트만 모은다."""
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


def _ids(runtime) -> dict:
    """현재 호출의 thread_id 와 user_id 를 찾는다. 못 찾으면 빈 값."""
    try:
        thread_id = get_config().get("configurable", {}).get("thread_id", "")
    except RuntimeError:
        thread_id = ""
    user_id = getattr(getattr(runtime, "context", None), "user_id", "")
    return {"thread_id": thread_id, "user_id": user_id}


def _model_name(model) -> str:
    """모델 객체에서 '모델ID@리전' 이름을 만든다."""
    model_id = getattr(model, "model_id", None) or getattr(model, "model", None) or type(model).__name__
    region = getattr(model, "region_name", None)
    return f"{model_id}@{region}" if region else str(model_id)


def _write(event: str, data: dict) -> None:
    """추적 기록 한 줄을 남긴다."""
    logger.info(event, extra={"event": event, "data": data})


class TracingMiddleware(AgentMiddleware):
    """LLM 호출과 도구 호출을 logs/trace.jsonl 에 한 줄씩 기록하는 미들웨어.
    대체 모델 미들웨어보다 안쪽에 두어, 시도한 모델마다 따로 기록되게 한다."""

    def wrap_model_call(self, request, handler):
        """모델 호출 하나의 모델 이름·입력·출력·요청한 도구·토큰 수·소요 시간을 기록한다."""
        data = {**_ids(request.runtime), "model": _model_name(request.model),
                "input": _clip(_text(request.messages[-1])) if request.messages else ""}
        start = time.perf_counter()
        try:
            response = handler(request)
        except Exception as e:
            _write("model_call", {**data, "status": "error", "error": _clip(str(e)),
                                  "latency_s": round(time.perf_counter() - start, 2)})
            raise
        message = response.result[-1] if getattr(response, "result", None) else response
        usage = getattr(message, "usage_metadata", None) or {}
        _write("model_call", {
            **data, "status": "ok",
            "output": _clip(_text(message)),
            "tool_calls": [{"name": tc["name"], "args": _clip(tc["args"])} for tc in getattr(message, "tool_calls", [])],
            "tokens": {"input": usage.get("input_tokens"), "output": usage.get("output_tokens")},
            "latency_s": round(time.perf_counter() - start, 2),
        })
        return response

    def wrap_tool_call(self, request, handler):
        """도구 호출 하나의 도구 이름·인자·결과·소요 시간을 기록한다."""
        call = request.tool_call
        data = {**_ids(request.runtime), "tool": call["name"], "args": _clip(call["args"])}
        start = time.perf_counter()
        try:
            result = handler(request)
        except Exception as e:
            _write("tool_call", {**data, "status": "error", "error": _clip(str(e)),
                                 "latency_s": round(time.perf_counter() - start, 2)})
            raise
        _write("tool_call", {**data, "status": "ok", "output": _clip(_text(result)),
                             "latency_s": round(time.perf_counter() - start, 2)})
        return result
