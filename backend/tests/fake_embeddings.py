"""确定性桩 embedding：字符袋哈希，相似度来自字符重叠度。

任何测试不得调用真实 embedding 服务（工单 0009）；此桩让向量库与语义搜索
离线可测——共享字符越多的文本，余弦相似度越高（"记账"能命中含"记账"的条目）。
满足与生产 OpenAICompatibleEmbedder 相同的最小约定：`embed(text) -> list[float]`。
"""

import zlib


class FakeEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        vec = [0.0] * self.dim
        for ch in text:
            vec[zlib.crc32(ch.encode("utf-8")) % self.dim] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            return [1.0] + [0.0] * (self.dim - 1)
        return [v / norm for v in vec]
