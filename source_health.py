"""
MultiSource — 源熔断器 (Phase 4)

当某个外部源连续失败达到阈值，自动跳过一段时间，
避免每次搜索都等满超时再被预算跳过。
"""
import time
from collections import defaultdict


# ============================================================
# 配置
# ============================================================
SOURCE_FAILURE_THRESHOLD = 3    # 连续失败 N 次后熔断
SOURCE_COOLDOWN_SECONDS = 300   # 熔断冷却时间（秒）


class CircuitBreaker:
    """简单的源熔断器，跟踪每个源的连续失败次数。

    状态:
      closed  → 正常查询
      open    → 连续失败 >= FAILURE_THRESHOLD，冷却期内跳过
      half_open → 冷却到期后放行一次探测
    """

    def __init__(self,
                 threshold: int = SOURCE_FAILURE_THRESHOLD,
                 cooldown: int = SOURCE_COOLDOWN_SECONDS):
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures: dict = defaultdict(int)       # source_id → 连续失败次数
        self._opened_at: dict = defaultdict(float)     # source_id → 熔断时间戳

    def should_skip(self, source_id: str) -> bool:
        """当前搜索是否应跳过此源。

        - 连续失败 < threshold → 不跳过 (closed)
        - 连续失败 >= threshold，冷却中 → 跳过 (open)
        - 连续失败 >= threshold，冷却到期 → 不跳过，进入 half_open
        """
        failures = self._failures[source_id]
        if failures < self.threshold:
            return False

        # 冷却检查
        opened_at = self._opened_at.get(source_id, 0)
        if time.time() - opened_at >= self.cooldown:
            # 冷却到期 → half_open，放行一次探测
            self._failures[source_id] = self.threshold - 1  # 回到阈值边缘
            return False

        return True

    def record_success(self, source_id: str):
        """记录一次成功，重置连续失败计数"""
        self._failures[source_id] = 0
        self._opened_at.pop(source_id, None)

    def record_failure(self, source_id: str) -> bool:
        """记录一次失败。返回 True 表示熔断器刚打开（从 closed → open）。"""
        self._failures[source_id] += 1
        failures = self._failures[source_id]

        if failures == self.threshold:
            self._opened_at[source_id] = time.time()
            return True  # 刚触发熔断

        return False

    def status(self, source_id: str) -> str:
        """返回状态字符串，用于日志"""
        failures = self._failures[source_id]
        if failures < self.threshold:
            return "closed"
        if self.should_skip(source_id):
            remaining = int(self.cooldown - (time.time() - self._opened_at.get(source_id, 0)))
            return f"open({remaining}s remaining)"
        return "half_open"
