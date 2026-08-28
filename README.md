# Anklang

Anklang 是一个可独立部署的题面相似检索服务：输入一道算法题的题面，服务把题面向量化（embedding，把文字转换成固定长度的数字向量），再返回按余弦相似度（比较两个向量方向接近程度的分数）排序的候选题目。它是公开项目 [is-my-problem-new](https://github.com/fjzzq2002/is-my-problem-new) v2 的小型直接改编，机器调用入口是版本化的 HTTP API。

Anklang 不需要 Urmotiv 或 Fermata 才能启动；它使用自己的 SQLite 索引（SQLite 是 Python 自带的单文件数据库）。Urmotiv 可以把 Anklang 当作独立的检索后端，两个系统不共享数据库。

## 用途与边界

Anklang 只回答“哪些已收录题目与这道题相似”，返回检索候选和检索完成状态。它不作产品判断：

- 不判断候选是否同题，也不判断候选是否可以参考；
- 不负责审核、通过、拦截、工作流或问题状态；
- 不调用 LLM（大语言模型）复核，不做代理，不保存查询结果，也不提供结果缓存；
- 不替 Urmotiv 保存或解释业务属性。

共享边界必须保持清楚：Urmotiv 插件可以把抄袭/检查信息作为 **Urmotiv 的问题属性** 添加，供 Fermata 读取；Fermata 通过带版本号的 Urmotiv HTTP 接口读取这些属性。Anklang 只返回题目候选，不拥有这些属性、审核结论、工作流状态或跨服务数据库，也不提供让 Fermata 与 Anklang 运行时互调的接口。

## 与上游的对应关系

上游运行入口固定在 v2 提交 [`72e309bd`](https://github.com/fjzzq2002/is-my-problem-new/blob/72e309bdcea2669bc3f476bea6fa81b1f21e788a) 的 `ui/server.py`。Anklang 保留入口和检索主链：

```text
查询题面 -> 向量化 -> cosine_all -> 相似度降序 -> collapse -> mkrow -> 候选
```

`cosine_all` 计算每个当前题目的余弦相似度；排序后，`collapse` 按 `(source, externalId)` 去重；`mkrow` 生成公开候选字段。上游作者 Ziqian Zhong 的 MIT 版权声明与 Anklang 声明见 [`LICENSE`](LICENSE)。

Anklang 只增加部署所需的四类能力：

| 能力 | 实现 | 说明 |
| --- | --- | --- |
| embedding 提供方 | `anklang/embedding.py` | 使用环境变量配置的阿里云百炼（DashScope）OpenAI 兼容 `/embeddings` 接口；查询和入库使用同一模型与维度。 |
| 当前题库 | `anklang/store.py` | 把上游内存题库换成可独立持久化的 SQLite 当前题目行和向量；每次查询读取当前快照。 |
| 增量来源 | `anklang/sources/`、`anklang/ingest.py` | 自动发现来源插件，按来源维护时间游标，并幂等插入或更新题目。 |
| 机器接口 | `anklang/http_api.py` | 为 Urmotiv 提供严格、带版本号的查询和本地健康检查。 |

上游的网页、查询改写、重排、统计、查询向量缓存和 OJ 筛选不在 Anklang 当前范围内。候选分数是排序信号，不是“重复”阈值政策。

## 数据来源、embedding 与实时增量

Anklang 不内置真实题库。`anklang/sources/example_static/` 只有本仓库编写的合成样例；生产题目由来源插件提供。embedding 提供方也不随镜像打包：必须由部署者配置百炼兼容地址、密钥、模型和维度。

运行时的两条数据流如下：

```text
来源插件 --新增/更新题目--> ingest_once --规范化、哈希、embedding--> SQLite 当前行
                                                                        |
查询题面 --同一 embedding 提供方--> cosine_all --降序--> collapse --mkrow-+
                                                                        |
                                                                        +--> 版本化候选响应
```

### embedding 提供方

`DASHSCOPE_BASE_URL` 必须是完整的 HTTP/HTTPS 地址，并包含百炼 OpenAI 兼容接口的 `/compatible-mode/v1` 前缀；客户端向 `{DASHSCOPE_BASE_URL}/embeddings` 发送 `model`、`input` 和 `dimensions`，使用 `DASHSCOPE_API_KEY` 作为 Bearer 令牌。默认模型是 `text-embedding-v4`，默认维度是 `1024`。

查询前，服务会确认 SQLite 中的向量模型和维度与当前配置一致。以下任一情况都会让 v2 明确返回 `unavailable`，而不是伪装成完整的空结果：

- `DASHSCOPE_BASE_URL` 或 `DASHSCOPE_API_KEY` 缺失；
- 提供方请求失败或返回结构、数量、顺序、维度不符合约定；
- 本地向量索引为空、损坏或与当前模型身份冲突。

增量入库时 embedding 失败不会写入没有向量的题目，也不会推进来源游标；下一轮会再次尝试。

### 来源插件契约

服务启动后每轮都会重新发现 `anklang/sources/` 下的子包。一个来源插件只需导出下列两个符号：

```text
SOURCE_NAME: str
fetch_new_problems(since: str | None) -> list[RawProblem]
```
本仓库只内置 `example_static` 合成来源，没有内置的 Urmotiv 题库连接器。实际接入时，部署者必须把 Urmotiv 插件作为可被 Anklang 导入的来源子包随运行环境提供；不存在未实现的跨服务回调或数据库直连替代方案。

`RawProblem` 的字段如下：

| 字段 | 要求 |
| --- | --- |
| `external_id` | 来源内稳定题号；与 `SOURCE_NAME` 组成稳定主键。 |
| `title`、`statement` | 非空题目标题和题面。题面会由框架统一规范化并计算 `contentHash`。 |
| `url` | 可选的 HTTPS 或 HTTP 题目链接。 |
| `updated_at` | 可选的毫秒精度 UTC 时间，例如 `2026-01-01T00:00:00.000Z`。提供时用于选择较新版本和推进游标。 |
| `metadata` | 可选的公开标量元数据；只在 v2 候选中传递，不参加题面哈希、向量或相似度。 |

`since` 是该来源上次成功推进的 UTC 时间游标；首次调用为 `null`。来源插件负责从自己的数据源读取增量，Anklang 负责校验、规范化、调用 embedding、幂等写入和游标比较交换。来源名必须全局唯一；同一 `(source, external_id)` 的不明确冲突会整组跳过，不会覆盖已有较新内容。

Urmotiv 的实时新增题目应通过可被 Anklang 导入的来源插件接入：打开 `ANKLANG_INGEST_ENABLED=true` 后，服务按 `ANKLANG_INGEST_INTERVAL_SECONDS` 周期调用同一抓取入口。写入提交后，现有服务实例的下一次查询直接读取新快照，不需要重启或离线全量重建。一个来源失败不会阻断其他来源。

这不是跨服务工作流接口：Anklang 不主动调用 Urmotiv，也不读取 Urmotiv 数据库。插件必须自行遵守 Urmotiv 对来源数据的授权、脱敏和生命周期要求。

## 独立部署

### 运行前准备

- 本机运行需要 Python 3.11；运行代码只使用标准库。
- 容器运行需要 Docker Engine 和 Docker Compose v2。
- 为进程注入 [`.env.example`](.env.example) 中的变量。程序不会自动读取任何 `.env` 文件；不要把密钥写进命令历史、镜像或版本库。
- 生产环境必须设置至少 16 个字符的 `ANKLANG_SERVICE_TOKEN`，并启用 `ANKLANG_REQUIRE_SERVICE_TOKEN=true`。Compose 会强制启用此要求。

### 本机启动

从仓库根目录执行：

```bash
export PYTHONPATH=.
python3 -m anklang
```

默认监听 `127.0.0.1:8730`。本机测试可以保持服务令牌为空；一旦设置了 `ANKLANG_SERVICE_TOKEN`，查询请求必须带 `Authorization: Bearer <令牌>`。健康检查不需要令牌。配置字段、默认值和范围见 [`.env.example`](.env.example)。

### Compose 启动

Compose 使用容器内 `0.0.0.0:8730`，但宿主端口默认只绑定 `127.0.0.1:8730`。先按部署平台的密钥注入方式准备 `compose.yaml` 声明的环境文件，再执行：

```bash
docker compose config -q
docker compose build
docker compose up -d --build
```

容器以 UID/GID `10001:10001` 的非 root 用户运行，根文件系统只读，删除全部 Linux capabilities，只有 `/app/problems-data` 数据卷可写。停止服务：

```bash
docker compose down
```

更完整的发布前检查、端口变更方式和故障处理见 [`docs/deployment.md`](docs/deployment.md)。

## 存活、就绪与健康

三个 GET 路由都不访问 embedding 提供方：

```bash
curl --fail --silent http://127.0.0.1:8730/api/v1/live
curl --fail --silent http://127.0.0.1:8730/api/v1/ready
curl --fail --silent http://127.0.0.1:8730/api/v1/health
```

- `/api/v1/live` 返回 HTTP 200 表示进程能响应：

  ```json
  {"status":"ok","service":"anklang","apiVersion":"1"}
  ```

- `/api/v1/ready` 返回 HTTP 200 且 `ready: true` 表示服务仍接受查询；停止接收请求后返回 HTTP 503 且 `ready: false`。它只检查本地运行状态，不读取题库，也不调用后端或网络。
- `/api/v1/health` 返回 HTTP 200 和本地状态，例如题目数量、索引状态以及 embedding 是否已配置。它不返回密钥或异常原文：

  ```json
  {
    "status": "ok",
    "service": "anklang",
    "apiVersion": "1",
    "backend": "upstream-v2",
    "localStoreReady": true,
    "localProblemCount": 5,
    "embeddingAvailable": true,
    "vectorIndexReady": true,
    "vectorIndexStatus": "ready"
  }
  ```

`health.status` 为 `degraded` 时先检查本地数据库、模型/维度配置和向量索引；健康路由本身仍会返回结构化状态。

## 版本化查询契约

Anklang 暴露两个 POST 路由：

- `/api/v1/checks/similarity`：兼容接口，只在完整检索时返回 HTTP 200；部分或不可用时返回 HTTP 503 固定错误。
- `/api/v2/checks/similarity`：推荐接口。只要服务形成结构化结果，就以 HTTP 200 返回，并在 `completion` 中明确是完整、部分还是不可用。

查询请求必须是严格 JSON 对象，顶层只能有 `apiVersion`、`requestId`、`contentHash`、`problem` 四个字段。`apiVersion` 必须与 URL 中的版本一致；`requestId` 是规范 UUID；`contentHash` 是调用方计算的 64 位小写十六进制字符串。`problem` 只能有 `title`、`type`、`tagIds`、`basicStatement`：

- `type` 只能是 `traditional`、`interactive` 或 `submit_answer`；
- `title` 长度为 1–200；`tagIds` 有 1–30 个字符串，每个长度不超过 120；
- `basicStatement` 长度为 1–500,000；长度按 JavaScript UTF-16 字符串单元计算。

下面是可直接解析的合成请求示例；示例题面不是生产题库内容：

```json
{
  "apiVersion": "2",
  "requestId": "00000000-0000-4000-8000-000000000001",
  "contentHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "problem": {
    "title": "合成示例：数组求和",
    "type": "traditional",
    "tagIds": ["array", "sum"],
    "basicStatement": "给定一个整数数组，计算并输出所有元素的总和。"
  }
}
```

使用服务令牌的 v2 调用示例：

```bash
curl --fail --silent \
  -H "Authorization: Bearer ${ANKLANG_SERVICE_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-raw '{"apiVersion":"2","requestId":"00000000-0000-4000-8000-000000000001","contentHash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","problem":{"title":"合成示例：数组求和","type":"traditional","tagIds":["array","sum"],"basicStatement":"给定一个整数数组，计算并输出所有元素的总和。"}}' \
  http://127.0.0.1:8730/api/v2/checks/similarity
```

v2 完整结果的顶层字段严格为 `apiVersion`、`contentHash`、`checkedAt`、`completion`、`candidates`。候选最多 50 条，按 `similarity` 降序排列；服务只应用 `ANKLANG_MINIMUM_SIMILARITY` 作为显示下限，不把它解释成“重复”或“通过”阈值。每条候选必填 `source`、`externalId`、`title`、`similarity`，可选 `url`；v2 还可以带来源插件提供的有界 `metadata`。示例：

```json
{
  "apiVersion": "2",
  "contentHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "checkedAt": "2026-08-28T00:00:00.000Z",
  "completion": {
    "status": "complete",
    "reasonCode": "complete",
    "retryable": false
  },
  "candidates": [
    {
      "source": "example_static",
      "externalId": "demo-1001",
      "title": "数组元素之和",
      "similarity": 0.82,
      "url": "https://example.invalid/problems/demo-1001"
    }
  ]
}
```

`completion` 的含义：

| `status` | 含义 | 候选数组 |
| --- | --- | --- |
| `complete` | 当前配置下检索完整完成；`reasonCode` 固定为 `complete`，`retryable` 为 `false`。 | 可以为空或包含候选。 |
| `partial` | 仍形成了一部分可用候选，但某个检索信号失败。 | 可以包含候选。 |
| `unavailable` | 无法形成可信的检索结果，例如 embedding 未配置或本地索引不可用。 | 必须为空。 |

非完整状态的 `reasonCode` 会说明固定的超时、限流、后端不可用/无效、服务不可用或内部错误类别；`retryable` 为 `true` 时调用方可以重试，若存在 `retryAfterSeconds`，其范围为 1–86,400 秒。v2 的“不可用”不是完整的空结果。所有响应均发送 `Cache-Control: no-store`，服务端不保存查询结果或复用策略。

v1 成功响应只包含 `apiVersion`、`contentHash`、`checkedAt`、`candidates`，不包含 `completion` 或 v2 元数据。无论 v1 还是 v2，响应都不会包含题面摘录、复核结论、审核建议、通过/拦截字段或工作流状态。鉴权失败、请求契约错误、服务繁忙等 HTTP 错误也只返回固定错误对象，不回显题面、路径、密钥或外部响应。

## 固定 32/32 证据的正确解释
仓库关联的受控验收记录使用一个可复现的 Formal156 题目快照，从 156 条来源记录中按固定规则均匀抽取 32 条查询。记录绑定 DashScope `text-embedding-v4`（1024 维）、156 条向量索引、`upstream-v2` 查询模式和已启用的提供方配置，观察到：

- 请求 32 条，HTTP 200 响应 32 条；
- `completion.status=complete` 的结果 32 条；
- 失败 0 条。

这是一份固定样本的提供方连通性、版本化响应契约和“完整结果可形成”证据，不是 32/32 的语义准确率，也不是全量题库质量、同题判断、抄袭判断、审核结果或工作流质量保证。请求正文、题库内容、令牌和完整指纹不随本仓库文档分发。

## 配置速查

所有布尔值只能写小写 `true` 或 `false`；空值使用代码中的默认值。

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `ANKLANG_BIND_HOST` | `127.0.0.1` | 监听地址；容器内由 Compose 覆盖为 `0.0.0.0`。 |
| `ANKLANG_PORT` | `8730` | 服务端口；Compose 宿主映射可用同名变量修改。 |
| `ANKLANG_SERVICE_TOKEN` | 空 | 若非空，查询必须带 Bearer 令牌。 |
| `ANKLANG_REQUIRE_SERVICE_TOKEN` | `false` | 为 `true` 时缺少或短于 16 字符的令牌会拒绝启动。 |
| `ANKLANG_MAX_IN_FLIGHT_CHECKS` | `16` | 在途查询上限，范围 1–256。 |
| `ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS` | `15` | 客户端连续无数据时限，范围 1–300 秒。 |
| `ANKLANG_SHUTDOWN_GRACE_SECONDS` | `30` | 收到 SIGTERM/SIGINT 后等待在途查询的最长时间，范围 1–300 秒。 |
| `ANKLANG_SEARCH_K` | `8` | 内部返回候选数，范围 1–20；接口仍最多输出 50 条。 |
| `ANKLANG_MINIMUM_SIMILARITY` | `0.5` | 显示下限，范围 0–1；不是业务判定阈值。 |
| `ANKLANG_LOCAL_DB_PATH` | `problems-data/local-index.db` | SQLite 文件路径；相对启动目录。 |
| `DASHSCOPE_BASE_URL` | 空 | 百炼 OpenAI 兼容地址，需含 `/compatible-mode/v1`。 |
| `DASHSCOPE_API_KEY` | 空 | 百炼 embedding 令牌；只读入内存。 |
| `DASHSCOPE_EMBEDDING_MODEL` | `text-embedding-v4` | 向量模型标识。 |
| `DASHSCOPE_EMBEDDING_DIM` | `1024` | 向量维度，范围 1–4096。 |
| `ANKLANG_INGEST_ENABLED` | `false` | 是否在进程内启用来源插件增量抓取。 |
| `ANKLANG_INGEST_INTERVAL_SECONDS` | `3600` | 增量抓取间隔，范围 60–86,400 秒。 |
| `ANKLANG_STOP_GRACE_PERIOD` | `45s` | 仅供 Compose 使用的容器停止宽限；应大于应用停止宽限。 |

实现边界、字段不变量和维护约束见 [`docs/plan.md`](docs/plan.md)。
