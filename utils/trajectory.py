"""可复用的轨迹记录辅助函数。"""
from __future__ import annotations

from typing import Any


def record_call(
    store: Any,
    sample_id: int,
    ordinal: int,
    kind: str,
    result: Any,
    error: str | None = None,
) -> None:
    """通过统一的 TrajectoryStore 接口持久化一次模型调用。"""
    store.record_call(
        sample_id,
        call_ordinal=ordinal,
        attempt=result.attempts if result else 0,
        kind=kind,
        status="error" if error else "ok",
        request_params=result.request_params if result else {},
        result=result,
        error=error,
    )
