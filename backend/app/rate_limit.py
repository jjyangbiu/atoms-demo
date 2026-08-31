"""生成限流（工单 0011）：每用户每小时生成上限 + 全局并发上限。

给真实 LLM API 额度装护栏，纯内存计量（不新增表）：
- 每用户滑动窗口：窗口固定为 1 小时，窗口内接受的生成数达上限则拒绝；
- 全局并发：进行中的生成（自接受起至流结束，含同项目排队等待）占一个名额，
  占满即拒绝新生成，不影响已在进行中的生成。

进程重启后窗口清空、名额归零，与「实时额度护栏」语义一致（同 _generation_locks 口径）。
时钟经构造参数注入，测试以可控时钟验证窗口边界。
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Literal

WINDOW_SECONDS = 3600.0
# 全局并发占满时无法预知何时有名额释放，给一个短的固定建议等待
GLOBAL_BUSY_RETRY_AFTER = 30.0

RejectReason = Literal["user_hourly", "global_concurrency"]


class RateLimitRejected(Exception):
    """限流拒绝：reason ∈ user_hourly | global_concurrency，retry_after 为建议等待秒数。"""

    def __init__(self, reason: RejectReason, retry_after: float):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


class GenerationLimiter:
    """生成限流器：accept/release 成对使用；限额 <= 0 表示该项不限。"""

    def __init__(
        self,
        per_user_hourly: int,
        max_concurrent: int,
        clock: Callable[[], float] = time.time,
    ):
        self.per_user_hourly = per_user_hourly
        self.max_concurrent = max_concurrent
        self.clock = clock
        self._windows: dict[int, deque[float]] = {}
        self._active = 0
        self._lock = threading.Lock()

    def accept(self, user_id: int) -> None:
        """接受一次生成：先检查后记账，任一限额超限都不产生副作用。"""
        with self._lock:
            now = self.clock()
            window = self._windows.setdefault(user_id, deque())
            while window and now - window[0] >= WINDOW_SECONDS:
                window.popleft()
            if self.per_user_hourly > 0 and len(window) >= self.per_user_hourly:
                retry_after = window[0] + WINDOW_SECONDS - now
                raise RateLimitRejected("user_hourly", max(1.0, math.ceil(retry_after)))
            if self.max_concurrent > 0 and self._active >= self.max_concurrent:
                raise RateLimitRejected("global_concurrency", GLOBAL_BUSY_RETRY_AFTER)
            window.append(now)
            self._active += 1

    def release(self) -> None:
        """生成结束（成功/失败/断流）时释放全局名额。"""
        with self._lock:
            self._active = max(0, self._active - 1)
