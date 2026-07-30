"""阿里云百炼（DashScope）文本向量（embedding）客户端。

"Embedding"（向量化）：把一段文字转换成一串固定长度的浮点数（向量），语义越接近的
文本，向量在数学上就越接近，从而可以用向量距离衡量"两段文字讲的是不是同一件事"
（具体的距离计算见 anklang.vectormath.cosine_similarity）。

只用标准库 urllib 实现——部署服务器没有 pip/venv，不能装官方 SDK。

接口（已实测可用）：
  POST {base_url}/embeddings
  请求体 {"model": "text-embedding-v4", "input": "文本" 或 ["文本1", "文本2", ...],
          "dimensions": 1024}
  鉴权   Authorization: Bearer <DASHSCOPE_API_KEY>
  响应体 {"data": [{"embedding": [...]}, ...], "model": ...}
  其中 base_url 已包含 DashScope 的 "/compatible-mode/v1" 后缀（由部署环境的
  DASHSCOPE_BASE_URL 配置项提供，本模块不假设具体地区域名）。

同步接口限制（docs/plan.md 3.4 节）：一次最多 10 条文本、单条最多 8192 Token。这里
只封装"一次一条或几条"的同步调用（超过 10 条自动分批串行请求），不实现面向几十万题
一次性建库的批处理（异步）接口——后者是未来需要重建全量索引时才用得上的独立工具，
不在本次框架搭建范围内。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .vectormath import validate_embedding

_MAX_RESPONSE_BYTES = 8_000_000
_MAX_BATCH_SIZE = 10


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float = 30.0,
        opener: Any | None = None,
    ) -> None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError("向量维度必须是正整数。")
        self._url = f"{base_url}/embeddings"
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout = timeout_seconds
        self._opener = opener or urllib.request.urlopen

    def embed_one(self, text: str) -> list[float]:
        """算一条文本的向量。"""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """按最多 10 条一批调用同步接口，返回与输入等长、顺序一致的向量列表。"""
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _MAX_BATCH_SIZE):
            chunk = texts[start : start + _MAX_BATCH_SIZE]
            vectors.extend(self._embed_chunk(chunk))
        return vectors

    def _embed_chunk(self, chunk: list[str]) -> list[list[float]]:
        body = json.dumps(
            {
                "model": self._model,
                "input": chunk if len(chunk) > 1 else chunk[0],
                "dimensions": self._dimensions,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except EmbeddingError:
            raise
        except Exception as error:
            # 这里刻意捕获所有异常再包装成 EmbeddingError：调用方（本地检索后端）只会
            # 捕获 EmbeddingError 来降级成关键词召回，任何漏出去的异常类型都会让整个
            # 查重请求失败，而不是退化成"没有向量也能用"。
            raise EmbeddingError("调用百炼 embedding 接口失败。") from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise EmbeddingError("百炼 embedding 响应过大。")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise EmbeddingError("百炼 embedding 响应不是有效 JSON。") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(chunk):
            raise EmbeddingError("百炼 embedding 响应条数与请求不一致。")

        # 有些 OpenAI 兼容的 embedding 接口会在每条结果里带 index 字段标明原始顺序；
        # 没有这个字段时按响应数组的原始顺序处理。两种情况都要能正确处理。
        has_indices = ["index" in item for item in data if isinstance(item, dict)]
        if has_indices and any(has_indices) and not all(has_indices):
            raise EmbeddingError("百炼 embedding 响应不能只给部分结果提供 index。")
        indexed: list[tuple[int, list[float]]] = []
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingError("百炼 embedding 响应格式不正确。")
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingError("百炼 embedding 响应缺少向量数据。")
            try:
                vector = validate_embedding(
                    embedding, expected_dimensions=self._dimensions
                )
            except ValueError as error:
                raise EmbeddingError("百炼 embedding 响应里的向量不合法。") from error

            # 接口允许省略 index，此时按响应数组位置处理；只要给出了 index，就不能
            # 用布尔值、字符串或小数冒充整数，也不能靠回退逻辑掩盖错误。
            if "index" in item:
                index = item["index"]
                if isinstance(index, bool) or not isinstance(index, int):
                    raise EmbeddingError("百炼 embedding 响应里的 index 不是整数。")
            else:
                index = position
            indexed.append((index, vector))

        indices = [index for index, _ in indexed]
        if len(set(indices)) != len(indices) or set(indices) != set(range(len(chunk))):
            raise EmbeddingError("百炼 embedding 响应里的 index 不完整或有重复。")
        indexed.sort(key=lambda pair: pair[0])
        return [vector for _, vector in indexed]
