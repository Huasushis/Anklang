"""Anklang：is-my-problem-new（MIT，Copyright (c) 2023 Ziqian Zhong）的最小直接改编。

改用阿里云百炼（DashScope）做文本向量，支持源插件实时入库，只暴露查询入/结果出
的查重接口。不包含 yuantiji 反向代理、LLM 复核、标定或结果缓存。

保留上游的核心数据流：题面查询 -> 向量化 -> 余弦相似度检索 -> 相似候选排序。
为 Urmotiv 集成只增加标准库 HTTP 接口、SQLite 增量索引与来源插件：

  config / contracts / server          配置、查询契约、HTTP 服务。
  embedding / vectormath               可配置向量客户端与余弦相似度。
  backends.local_engine / store         向量与关键词混合检索、SQLite 索引。
  sources / ingest                     来源插件发现、增量写入与游标。
"""

__all__ = [
    "config",
    "contracts",
    "server",
    "backends",
    "store",
    "vectormath",
    "embedding",
    "text_normalize",
    "sources",
    "ingest",
]
