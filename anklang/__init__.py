"""Anklang: a bounded adaptation of is-my-problem-new v2.

Runtime search starts in the preserved upstream path ``ui/server.py`` and keeps its
query-vector -> cosine -> descending-order -> collapse -> row-projection flow. The Anklang
package supplies only the configured provider, incremental rows, source cursors, and strict
Urmotiv HTTP adapter.
"""

__all__ = [
    "config",
    "contracts",
    "http_api",
    "backends",
    "store",
    "vectormath",
    "embedding",
    "text_normalize",
    "sources",
    "ingest",
]
