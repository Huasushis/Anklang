"""OpenAI 兼容的最小 Chat 客户端（仅用标准库）。

只实现 Anklang 复核需要的能力：请求一次 chat/completions，读取 message.content，
尽力从中抽出一个 JSON 对象。密钥只在请求头里使用，不写日志。
"""
from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from typing import Any

_MAX_RESPONSE_BYTES = 2_000_000
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(self, base_url: str, api_key: str, opener: Any | None = None) -> None:
        self._url = f"{base_url}/chat/completions"
        self._api_key = api_key
        self._opener = opener or urllib.request.urlopen

    def complete_json(
        self, model: str, system: str, user: str, timeout_seconds: float
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "model": model,
                "temperature": 0.0,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
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
            with self._opener(request, timeout=timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as error:
            raise LlmError("LLM 请求失败。") from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise LlmError("LLM 响应过大。")
        try:
            payload = json.loads(raw.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            RecursionError,
        ) as error:
            raise LlmError("LLM 响应结构不符合预期。") from error
        if not isinstance(content, str):
            raise LlmError("LLM 响应内容不是文本。")
        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise LlmError("LLM 响应里没有 JSON 对象。")
        try:
            parsed = json.loads(match.group(0))
        except (ValueError, RecursionError) as error:
            raise LlmError("LLM 响应里的 JSON 无法解析。") from error
        if not isinstance(parsed, dict):
            raise LlmError("LLM 响应的 JSON 不是对象。")
        return parsed
