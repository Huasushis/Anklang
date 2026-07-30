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


def validate_embedding(
    vector: Sequence[float], *, expected_dimensions: int | None = None
) -> list[float]:
    """检查向量能否安全参与存储和计算，并返回普通浮点数列表。

    布尔值在 Python 里属于整数的子类，但它不是向量接口应接受的数字，因此需要单独
    拒绝。空向量、NaN（不是有效数字）、无穷大和维度不符也都视为损坏数据，避免
    它们在后续排序中得到无法解释的分数。
    """
    if isinstance(vector, (str, bytes, bytearray, memoryview)):
        raise ValueError("向量必须是数字列表。")
    if expected_dimensions is not None and (
        isinstance(expected_dimensions, bool)
        or not isinstance(expected_dimensions, int)
        or expected_dimensions <= 0
    ):
        raise ValueError("期望的向量维度必须是正整数。")
    try:
        length = len(vector)
    except (TypeError, OverflowError) as error:
        raise ValueError("向量必须是有长度的数字列表。") from error
    if length == 0:
        raise ValueError("向量不能为空。")
    if expected_dimensions is not None and length != expected_dimensions:
        raise ValueError("向量维度与配置不一致。")

    values: list[float] = []
    try:
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("向量只能包含数字，不能包含布尔值。")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("向量不能包含 NaN（不是有效数字）或无穷大。")
            values.append(number)
    except (TypeError, OverflowError) as error:
        raise ValueError("向量包含无法转换的数字。") from error
    if len(values) != length:
        raise ValueError("向量长度在读取过程中发生变化。")
    return values


def pack_embedding(vector: Sequence[float]) -> bytes:
    """把一个向量打包成 BLOB：固定小端 32 位浮点数序列，与机器字节序无关，
    保证换一台机器读写也不会出错。"""
    values = validate_embedding(vector)
    try:
        blob = struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error) as error:
        raise ValueError("向量里的数字超出 32 位浮点数可存储范围。") from error
    if not all(math.isfinite(value) for value in struct.unpack(f"<{len(values)}f", blob)):
        raise ValueError("向量里的数字超出 32 位浮点数可存储范围。")
    return blob


def unpack_embedding(blob: bytes) -> list[float]:
    """把 BLOB 还原成向量（浮点数列表）。向量维度由 BLOB 长度反推，不需要
    额外存一列"维度"。"""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("向量存储内容必须是字节数据。")
    raw = bytes(blob)
    if not raw or len(raw) % 4 != 0:
        raise ValueError("向量存储内容的长度不正确。")
    count = len(raw) // 4
    try:
        vector = list(struct.unpack(f"<{count}f", raw))
    except struct.error as error:
        raise ValueError("向量存储内容无法读取。") from error
    return validate_embedding(vector)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """计算两个向量的余弦相似度；长度不一致或任一为零向量时返回 0（视为"无法比较"，
    而不是抛异常——调用方大多是在批量循环里打分，不希望一条脏数据打断整体检索）。
    """
    try:
        values_a = validate_embedding(a)
        values_b = validate_embedding(b, expected_dimensions=len(values_a))
    except ValueError:
        return 0.0

    # 先分别缩放到绝对值不超过 1，再计算平方和，避免有效但数值很大的向量在
    # 中间乘法中溢出成 Infinity。
    scale_a = max(abs(value) for value in values_a)
    scale_b = max(abs(value) for value in values_b)
    if scale_a == 0.0 or scale_b == 0.0:
        return 0.0
    scaled_a = [value / scale_a for value in values_a]
    scaled_b = [value / scale_b for value in values_b]
    dot = sum(x * y for x, y in zip(scaled_a, scaled_b))
    norm_a = sum(value * value for value in scaled_a)
    norm_b = sum(value * value for value in scaled_b)
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    similarity = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    if not math.isfinite(similarity):
        return 0.0
    return max(-1.0, min(1.0, similarity))
