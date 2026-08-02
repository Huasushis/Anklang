"""Anklang：阶段 1（yuantiji.ac 反向代理，生产默认）+ 阶段 2/3 本地检索引擎与
源插件框架主体（默认不启用，面向未来自建题库）。

实现说明：部署服务器没有 pip/venv，因此全部用 Python 标准库实现（http.server、
urllib、json、hashlib、sqlite3、struct 等），不引入 docs/plan.md 最初设想的
FastAPI/Pydantic/numpy 等第三方依赖——这是运行环境的硬约束，不是没有条件采用更好的
工具，接手人扩展时也要遵守这一点。

模块边界：
  config / contracts / cache / yuantiji / review / server   阶段 1，反向代理主体。
  backends                检索后端抽象（SearchBackend），reverse_proxy 与
                           local_engine 两种实现按 ANKLANG_BACKEND 切换。
  store / vectormath / embedding / text_normalize            阶段 2，本地题库的
                           存储、向量数学、百炼 embedding 客户端、题面规范化。
  sources / ingest / backfill                                阶段 3，源插件框架、
                           抓取入库调度、embedding 补算命令。
"""

__all__ = [
    "config",
    "contracts",
    "cache",
    "yuantiji",
    "review",
    "server",
    "backends",
    "store",
    "vectormath",
    "embedding",
    "text_normalize",
    "sources",
    "ingest",
    "backfill",
    "review_flow_capture",
]
