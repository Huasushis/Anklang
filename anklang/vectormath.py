"""向量的打包/解包与余弦相似度计算。

部署服务器没有 pip/venv，不能装 numpy，所以"向量"就是普通的 Python float 列表，
用标准库 struct 模块序列化成 SQLite 的 BLOB 列，相似度计算也是手写的纯 Python
循环——本地题库检索的量级用不到 numpy 级别的性能。

"余弦相似度"：把两段文字的向量看成空间里的两个箭头，只看它们的夹角（不看长度），
夹角越小（方向越接近）说明两段文字语义越接近，数值范围是 [-1, 1]，1 表示方向完全
相同。这里只负责纯数学计算，clamp 到契约要求的 [0, 1] 区间是候选构造那一层的事。
"""
from __future__ import annotations

import math
import struct
from collections.abc import Sequence


def pack_embedding(vector: Sequence[float]) -> bytes:
    """把一个向量打包成 BLOB：固定小端 32 位浮点数序列，与机器字节序无关，
    保证换一台机器读写也不会出错。"""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_embedding(blob: bytes) -> list[float]:
    """把 BLOB 还原成向量（浮点数列表）。向量维度由 BLOB 长度反推，不需要
    额外存一列"维度"。"""
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """计算两个向量的余弦相似度；长度不一致或任一为零向量时返回 0（视为"无法比较"，
    而不是抛异常——调用方大多是在批量循环里打分，不希望一条脏数据打断整体检索）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
