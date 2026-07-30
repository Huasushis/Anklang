"""示例源插件：从本仓库自带的一个本地 JSON 文件（problems.json）读取几道构造出来的
题目。

这个来源不抓取任何真实网络内容，纯粹用来端到端验证"源插件 -> 框架规范化/计算向量
-> 入库 -> 本地检索"这条链路是否打通，同时给之后要接入真实来源（例如某个 OJ 的
官方 API）的人一份可以照着写的样例——教程见仓库 README 里"新增一个源插件"一节。

problems.json 是自己编写的示例题目（不是任何真实题库的原文，也不影射任何真实存在
的题目），格式：
  [
    {
      "external_id": "...",
      "title": "...",
      "statement": "...",
      "url": "...",
      "updated_at": "2026-01-01T00:00:00.000Z"
                                   # 规范 UTC 时间，可直接比较先后
    },
    ...
  ]
"""
from __future__ import annotations

import json
from pathlib import Path

from anklang.sources import RawProblem

SOURCE_NAME = "example_static"

_DATA_PATH = Path(__file__).with_name("problems.json")


def fetch_new_problems(since: str | None) -> list[RawProblem]:
    """读取 problems.json；若给定 since，则只返回 updated_at 大于它的题目。

    示例数据的 updated_at 是统一精度的 UTC 时间，字符串顺序等于时间顺序。
    """
    problems = _load_all()
    if since is None:
        return problems
    return [problem for problem in problems if (problem.updated_at or "") > since]


def _load_all() -> list[RawProblem]:
    with _DATA_PATH.open("r", encoding="utf-8") as handle:
        raw_items = json.load(handle)
    problems: list[RawProblem] = []
    for item in raw_items:
        problems.append(
            RawProblem(
                external_id=item["external_id"],
                title=item["title"],
                statement=item["statement"],
                url=item.get("url"),
                updated_at=item.get("updated_at"),
            )
        )
    return problems
