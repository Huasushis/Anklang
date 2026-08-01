"""检索后端抽象：把"怎么找相似题目"这件事从 AnklangService 里抽出来，做成可以
互相替换的统一接口。目前有两种实现：

  - reverse_proxy.ReverseProxyBackend  转发给 yuantiji.ac（阶段 1，生产环境默认）。
  - local_engine.LocalEngineBackend    本地题库的向量 + 关键词混合检索（阶段 2，
                                        面向未来自建题库场景，默认不启用）。

由 anklang/config.py 的 ANKLANG_BACKEND 配置项决定 anklang/server.py 实例化哪一个；
两者对上层（AnklangService、anklang.review.evaluate）暴露完全一样的接口，互相替换
不需要改动查重判定逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class BackendError(RuntimeError):
    """检索后端不可用或调用失败。server 捕获这个异常，统一转换成"降级但合法"的
    响应（候选为空、message 说明情况），不让 Urmotiv 一侧收到无法解释的失败——
    呼应 docs/plan.md 2.2 节"设计含义"里对降级优先于报错的要求。
    """


@dataclass(frozen=True)
class BackendSearchResult:
    """一次后端检索的内部结果，不会直接作为 HTTP 响应发送。

    degraded 表示本次检索有一部分没有完成，例如本地文字转数字服务失败后只做了
    关键词检索。候选只供后端内部诊断测试使用；服务层必须返回固定的“检索未完成”
    结果，不能据此自动放行、拒绝、调用模型复核或写入缓存。
    """

    candidates: list[dict[str, Any]]
    degraded: bool
    # 本地索引成功结果所属的机器身份摘要；远程后端保持 None。服务层只会在
    # 当前 O(1) 门禁仍返回同一摘要时缓存，避免跨语料或跨模型复用旧判断。
    cache_identity: str | None = None


@runtime_checkable
class SearchBackend(Protocol):
    """所有检索后端必须实现的统一接口。

    search() 返回 BackendSearchResult，其中每个候选 dict 至少包含 source /
    externalId / title / similarity，可以再带 url / explanation —— 字段名和含义与
    anklang.contracts.build_result 期望的候选结构一致。anklang.review.evaluate 会
    在此基础上做相似度阈值判定和可选的 LLM 复核，两种后端不需要、也不应该重复
    实现这部分判定逻辑。
    """

    def search(self, query_text: str, k: int) -> BackendSearchResult:
        """用 query_text（题面文本）检索最相似的最多 k 条候选，不要求调用方再排序。
        遇到自身不可用的情况（上游服务挂了、本地存储读取失败等）应该抛出
        BackendError，而不是返回空列表悄悄吞掉——"完全没有候选"和"这次没查成功"
        对上层的处理方式不同（后者要在 message 里如实说明）。
        """
        ...

    def describe_health(self) -> dict[str, Any]:
        """返回这个后端自身的健康信息（供 /api/v1/health 展示），绝不包含密钥。
        这个方法本身不应该抛出异常——后端自己的调用失败应该被内部捕获，体现成
        状态字段（例如 upstreamReady=False），而不是让健康检查端点本身出错。
        """
        ...
