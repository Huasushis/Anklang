# Anklang

Anklang 是公开项目 [is-my-problem-new](https://github.com/fjzzq2002/is-my-problem-new) 的小型直接改编：输入一道算法题的题面，返回按相似度排序的已知题目候选。

上游作者为 Ziqian Zhong，许可证为 MIT；原版权声明和本项目声明都保存在 [`LICENSE`](LICENSE)。

## 与上游的对应关系

上游核心流程是“题面 → embedding（把文字转成向量）→ 余弦相似度检索 → 候选排序”。Anklang 保留该流程：

- 上游 `src/embedder.py` / v2 embedding 调用对应 `anklang/embedding.py`；
- 上游 `src/ui.py` / v2 `ui/server.py` 的查询和向量搜索对应 `anklang/server.py` 与 `anklang/backends/local_engine.py`；
- 上游静态题库索引改为 `anklang/store.py` 的 SQLite 当前快照；
- 新增 `anklang/sources/` 与 `anklang/ingest.py`，让来源数据增量进入同一索引。

改动范围只包括可配置的百炼 embedding 提供方、运行时增量来源插件和供 Urmotiv 调用的版本化 HTTP 查询接口。没有 LLM 复核、通过/拦截政策、准确率标定、流程采集、yuantiji 代理或结果缓存。

## 数据流

```text
来源插件 ──增量题目/更新时间──> ingest_once ──幂等写入──> SQLite 当前索引
                                                               │
查询题面 ──可选百炼 embedding──> 向量 + 关键词召回 ──排序──────┘
                                                               │
                                                               └─> 候选列表
```

新增或更新题目写入后，下一个查询从当前 SQLite 快照读取，服务无需重启，也不需要离线全量重建。

## HTTP 查询接口

- `POST /api/v1/checks/similarity`：兼容的完整查询接口；查询不完整时返回固定 503。
- `POST /api/v2/checks/similarity`：返回完整、部分完成或不可用状态。
- `GET /api/v1/live`：进程存活。
- `GET /api/v1/ready`：本地就绪状态，不调用后端或网络。
- `GET /api/v1/health`：本地索引和 embedding 可用状态，不返回密钥。

v1 成功响应严格为：

```json
{
  "apiVersion": "1",
  "contentHash": "64 位小写十六进制",
  "checkedAt": "2026-08-14T00:00:00.000Z",
  "candidates": [
    {
      "source": "example",
      "externalId": "problem-1",
      "title": "示例标题",
      "similarity": 0.82,
      "url": "https://example.invalid/problem-1"
    }
  ]
}
```

v2 只增加检索完整性和复用状态：

```json
{
  "apiVersion": "2",
  "contentHash": "64 位小写十六进制",
  "checkedAt": "2026-08-14T00:00:00.000Z",
  "completion": {
    "status": "complete",
    "reasonCode": "complete",
    "retryable": false
  },
  "candidates": [],
  "reuse": {"policy": "no-store"}
}
```

接口只返回检索候选。是否重复、是否可作参考以及后续流程均由 Urmotiv 决定。

## 运行

本机只使用 Python 3.11 标准库：

```bash
export PYTHONPATH=.
python3 -m anklang
```

程序不自动读取 `.env`。不要用 shell 的 `source` 或 `.` 加载真实环境文件。配置字段见 [`.env.example`](.env.example)。

关键配置：

- `ANKLANG_BIND_HOST`、`ANKLANG_PORT`：监听地址和端口；
- `ANKLANG_SERVICE_TOKEN`、`ANKLANG_REQUIRE_SERVICE_TOKEN`：服务鉴权；
- `ANKLANG_LOCAL_DB_PATH`：SQLite 索引路径；
- `ANKLANG_SEARCH_K`、`ANKLANG_MINIMUM_SIMILARITY`：候选数量和显示下限；
- `DASHSCOPE_BASE_URL`、`DASHSCOPE_API_KEY`、`DASHSCOPE_EMBEDDING_MODEL`、`DASHSCOPE_EMBEDDING_DIM`：OpenAI 兼容的百炼 embedding；
- `ANKLANG_INGEST_ENABLED`、`ANKLANG_INGEST_INTERVAL_SECONDS`：运行时增量抓取。

没有 embedding 配置时，服务正常使用关键词召回。已配置提供方调用失败时，v2 返回 `partial` 和仍可用的关键词候选；不会把失败说成完整空结果。

## 来源插件

在 `anklang/sources/<name>/` 添加包，并提供：

```text
SOURCE_NAME: str
fetch_new_problems(since: str | None) -> list[RawProblem]
```

`since` 是该来源上次成功推进的 UTC 时间游标。来源返回新增或更新题目；框架负责字段校验、题面规范化、内容哈希、可选 embedding、`(source, external_id)` 幂等写入和游标比较交换。完整约束见 [`docs/plan.md`](docs/plan.md)。

手工执行一轮：

```bash
PYTHONPATH=. python3 -m anklang.ingest
```

生产服务设置 `ANKLANG_INGEST_ENABLED=true` 后在进程内周期执行同一入口。

## 容器部署

```bash
docker compose config -q
docker compose build
```

Compose 只把 `127.0.0.1:${ANKLANG_PORT:-8730}` 暴露到宿主机，并以非 root、只读根文件系统运行。部署步骤和检查见 [`docs/deployment.md`](docs/deployment.md)。

## 验证

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
python3 -m compileall -q anklang tests
docker compose config -q
docker build -t anklang:verify .
```

测试只使用合成数据、注入的 embedding 响应和回环 HTTP，不发起真实外部请求。旧的 32/32 标定记录属于已废止范围的历史实验，不是 Anklang 当前实现或验收证据。