"""进程内存中的 embedding 提供方注册表。

提供方配置只存在于进程内存：重启后必然回到未配置状态，不写入 SQLite、文件或环境
变量，也不会创建另一套加密密钥。Urmotiv 的加密插件存储是唯一事实来源，每次重启后
由它重新供给提供方。环境变量（包括其他服务使用的通用键名）永远不能激活向量能力。

并发约定：所有对当前客户端的实际调用都必须先 ``acquire()`` 拿到客户端、结束后必须
``release()``。``configure()`` 和 ``clear()`` 先阻止新的获取，再等待已经在途的操作
结束，然后才替换或移除客户端。这保证 ``clear()`` 返回后，无论是已在途还是之后的
调用，都不可能再使用被清除的密钥。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from .embedding import EmbeddingClient
from .store import EmbeddingIndexSpec


@dataclass(frozen=True)
class ProviderConfig:
    """一次提供方配置：包含只在进程内使用的密钥运行时值，绝不回显。"""

    base_url: str
    api_key: str
    model: str
    dimension: int


class ProviderRegistry:
    """持有当前内存提供方的线程安全注册表。"""

    def __init__(self, initial: Any | None = None) -> None:
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._client = initial
        self._in_flight = 0
        self._base_url: str | None = None
        self._model: str | None = None
        self._dimension: int | None = None
        if initial is not None:
            self._model = getattr(initial, "model", None)
            self._dimension = getattr(initial, "dimensions", None)

    def acquire(self) -> Any | None:
        """返回当前客户端并计数；无提供方时返回 None（调用方必须显式不可用）。"""

        with self._lock:
            if self._client is None:
                return None
            self._in_flight += 1
            return self._client

    def release(self) -> None:
        """结束一次使用；计数归零时唤醒等待中的配置或清除操作。"""

        with self._lock:
            if self._in_flight <= 0:
                raise RuntimeError("在途提供方使用计数不一致。")
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.notify_all()

    def configure(self, config: ProviderConfig, *, opener: Any | None = None) -> None:
        """原子替换为新的提供方：先阻止新获取，等待在途操作结束后再安装。"""

        client = EmbeddingClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            dimensions=config.dimension,
            opener=opener,
        )
        with self._lock:
            self._client = None
            while self._in_flight > 0:
                self._idle.wait()
            self._base_url = config.base_url
            self._model = config.model
            self._dimension = config.dimension
            self._client = client

    def clear(self) -> None:
        """立即阻止新获取，并同步等待在途操作全部结束后再返回。"""

        with self._lock:
            self._client = None
            while self._in_flight > 0:
                self._idle.wait()
            self._base_url = None
            self._model = None
            self._dimension = None

    def status(self) -> tuple[bool, str | None, str | None, int | None]:
        """返回 (configured, baseUrl, model, dimension)，永不包含密钥。"""

        with self._lock:
            return (
                self._client is not None,
                self._base_url,
                self._model,
                self._dimension,
            )

    def spec(self) -> EmbeddingIndexSpec | None:
        """当前提供方的索引身份；未配置时返回 None。"""

        with self._lock:
            if self._client is None or self._model is None or self._dimension is None:
                return None
            return EmbeddingIndexSpec(self._model, self._dimension)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight
