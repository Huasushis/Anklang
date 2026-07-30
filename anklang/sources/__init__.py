"""源插件框架：每个"来源"（source）是 anklang/sources/ 下的一个子包，只要实现约定
的两个符号（SOURCE_NAME、fetch_new_problems），就能被框架自动发现并纳入抓取入库
流程，不需要在别处手工注册。

约定（对应 docs/plan.md 4.1 节）：
  SOURCE_NAME: str
      这个来源的标识，写入 problems 表的 source 列，同时也是 ingest_cursor 表按
      来源隔离游标的 key。必须在全部来源里唯一。
  fetch_new_problems(since: str | None) -> list[RawProblem]
      抓取"自 since 之后"的新题或更新题；since 为 None 表示第一次抓取（是全量还是
      来源自己定义的起点，由来源自己决定）。since 的具体格式由每个来源自己决定并
      自己解读——框架把它当成不透明的游标，只负责原样存取，不解析它的内容。有的
      来源可能用时间戳、有的可能用分页游标或递增 ID，这由来源自己决定，框架不做
      任何假设。

框架不对来源的抓取方式做任何假设（HTTP 请求、读本地文件、调官方 API 都可以），
只负责：
  1. 用 pkgutil 发现 anklang/sources/ 下所有实现了上述约定的子包；
  2. 调用每个来源的 fetch_new_problems，拿到 RawProblem 列表；
  3. 规范化题面、计算内容哈希、（若 embedding 服务可用）计算向量、按
     (source, external_id) 去重后写入 ProblemStore；
  4. 推进该来源的增量游标。
第 2-4 步的编排逻辑在 anklang/ingest.py，不在这个文件里——这里只放"发现"逻辑和
双方共用的 RawProblem 数据结构。

每个来源自己的中间数据（分页游标、去重指纹、限流状态、账号凭据等）只应该存在自己
的子目录或自己在 ingest_cursor 表里的那一行，不与其他来源共享，这个隔离原则和
Urmotiv 自己的插件规范（"插件如需保存数据，必须声明独立数据库命名空间"）是同一
治理思路，虽然 Anklang 是独立服务，沿用同一套思路方便未来维护者理解。
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class RawProblem:
    """源插件抓取到的一道原始题目，尚未规范化、尚未计算向量——这两步由框架
    （anklang/ingest.py）统一处理，源插件不需要自己做。"""

    external_id: str
    title: str
    statement: str
    url: str | None = None
    updated_at: str | None = None
    """该题目自己的更新时间戳，字符串格式由来源自己定义（例如 ISO 8601 日期），
    框架用它推进 since 游标（取本次抓取到的最大值）。不需要增量能力的来源可以不
    设置（留 None）——此时框架不会推进游标，每次都会重新抓一遍全量，但按
    (source, external_id) 去重仍然保证不会重复入库，只是会有更多"已存在，跳过"
    的判断开销。"""
    raw_ref: str | None = None
    """指向原始抓取产物存放位置的引用（例如本地缓存文件路径、原始响应的某个 ID），
    便于排查问题；不代表要长期保留大文件，也不会被写入 ProblemStore。"""


def discover_source_modules() -> list[ModuleType]:
    """发现 anklang/sources/ 下所有实现了源插件约定的子包，按 SOURCE_NAME 排序返回，
    保证多次调用的顺序稳定。"""
    package = importlib.import_module("anklang.sources")
    modules: list[ModuleType] = []
    for module_info in pkgutil.iter_modules(package.__path__):
        if not module_info.ispkg:
            continue
        module = importlib.import_module(f"anklang.sources.{module_info.name}")
        if hasattr(module, "SOURCE_NAME") and hasattr(module, "fetch_new_problems"):
            modules.append(module)
    modules.sort(key=lambda module: getattr(module, "SOURCE_NAME"))
    return modules
