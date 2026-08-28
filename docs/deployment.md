# Anklang 部署与运行手册

Anklang 是独立的题面相似检索服务。它有自己的 SQLite（Python 自带的单文件数据库）索引和容器数据卷，不需要 Urmotiv 或 Fermata 才能启动；Urmotiv 通过版本化 HTTP（用于机器通信的协议）查询接口调用它。产品边界、请求字段和候选含义见 [`README.md`](../README.md)。

## 前置条件

- 本机运行：Python 3.11。
- 容器运行：Docker Engine 与 Docker Compose v2。
- 生产 embedding（把文字转换成数字向量）：一个可访问的阿里云百炼 OpenAI 兼容 embedding 接口，以及由部署平台注入的 API 密钥。
- 生产鉴权：至少 16 个字符的 `ANKLANG_SERVICE_TOKEN`。令牌只应由部署平台注入，不应写入仓库、镜像、终端历史或诊断输出。

Anklang 只使用 Python 标准库，不需要安装 SDK。程序不会自动读取 `.env` 文件；请把 [`.env.example`](../.env.example) 中的字段交给进程管理器或部署平台注入。

## 准备配置

Compose 要求 `compose.yaml` 声明的 `env_file` 在启动前已经存在。用密钥管理系统或进程管理器生成该文件，保持其不受版本控制，并只写入所需环境变量。下面的值是占位符，不要原样用于生产：

```text
ANKLANG_SERVICE_TOKEN=<至少16个字符的随机值>
ANKLANG_REQUIRE_SERVICE_TOKEN=true
ANKLANG_LOCAL_DB_PATH=problems-data/local-index.db
DASHSCOPE_BASE_URL=<包含/compatible-mode/v1的HTTPS地址>
DASHSCOPE_API_KEY=<由密钥管理系统注入的值>
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
DASHSCOPE_EMBEDDING_DIM=1024
ANKLANG_INGEST_ENABLED=false
ANKLANG_INGEST_INTERVAL_SECONDS=3600
```

必须同时配置 `DASHSCOPE_BASE_URL` 和 `DASHSCOPE_API_KEY` 才会启用向量查询。缺少任一项时，健康路由仍可用，但 v2 查询会明确返回 `completion.status=unavailable`；增量导入不会写入无向量题目，也不会推进来源游标。

Compose 会覆盖下列容器内设置：

- `ANKLANG_BIND_HOST=0.0.0.0`、`ANKLANG_PORT=8730`；
- `ANKLANG_REQUIRE_SERVICE_TOKEN=true`；
- 容器以 UID/GID `10001:10001` 的非 root 用户运行；
- 根文件系统只读，只有 `/app/problems-data` 数据卷可写。

宿主端口默认映射为 `127.0.0.1:${ANKLANG_PORT:-8730}:8730`。如需更换宿主端口，只改 Compose 插值变量，不要把容器内端口改成其他值。`ANKLANG_STOP_GRACE_PERIOD` 是 Compose 停止宽限，必须大于 `ANKLANG_SHUTDOWN_GRACE_SECONDS`。

## 构建与启动

从 Anklang 仓库根目录执行。第一步会展开 Compose 配置并检查必需的环境文件；不要跳过它。

```bash
docker compose config -q
docker compose build
docker compose up -d --build
```

Compose 构建上下文只允许运行所需的 `anklang/`、`ui/`、示例来源和 `LICENSE`。本地数据库、测试产物、密钥和其他未授权内容不会进入镜像。

查看容器状态：

```bash
docker compose ps
```

停止并移除容器（保留命名数据卷）：

```bash
docker compose down
```

如需删除数据卷，必须先确认其中没有要保留的索引；Anklang 不会替 Urmotiv 或 Fermata 管理其他系统的数据。

## 存活、就绪与健康检查

使用宿主映射端口检查服务：

```bash
curl --fail --silent http://127.0.0.1:8730/api/v1/live
curl --fail --silent http://127.0.0.1:8730/api/v1/ready
curl --fail --silent http://127.0.0.1:8730/api/v1/health
```

三个路由都不调用 embedding 提供方；但健康检查会读取本地索引状态。

- `GET /api/v1/live`：HTTP 200 表示进程能响应。Compose 的容器 healthcheck 也只调用这个路由，并显式绕过代理环境。
- `GET /api/v1/ready`：HTTP 200 且 `ready` 为 `true` 表示进程正在接受查询。收到停止信号后，服务停止接受新查询，此路由返回 HTTP 503 且 `ready` 为 `false`。它不读取题库，不调用后端或网络。
- `GET /api/v1/health`：HTTP 200，返回固定的本地状态字段，包括 `localStoreReady`、`localProblemCount`、`embeddingAvailable`、`vectorIndexReady` 和 `vectorIndexStatus`（以及 `backend`）。异常只会表现为 `degraded`，不会回显密钥、路径、题面或外部响应。

一个就绪服务的存活响应形状为：

```json
{"status":"ok","service":"anklang","apiVersion":"1"}
```

健康响应中的题目数量会随索引变化；下面只展示合成状态：

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

## 查询冒烟

推荐使用 v2 路由：

```text
POST /api/v2/checks/similarity
```

请求必须使用 README 中的严格 JSON 形状，并在生产配置下带有：

```text
Authorization: Bearer <ANKLANG_SERVICE_TOKEN>
Content-Type: application/json
```

可直接复制的合成请求和 `curl` 调用见 [`README.md`](../README.md)。请求中的 `apiVersion` 必须为字符串 `"2"`；不能把生产题面、令牌或外部响应粘贴到 shell 历史或报告。

v2 在服务成功形成结构化结果时返回 HTTP 200；通过 `completion` 区分：

- `complete`：检索完成，可返回零条或多条按相似度降序排列的候选；
- `partial`：仍有可用候选，但部分检索信号失败；调用方按 `retryable` 决定是否重试；
- `unavailable`：不能形成可信结果，`candidates` 必须为空。

候选只允许 `source`、`externalId`、`title`、`similarity` 和可选 `url`；v2 可额外带有来源适配器的有界标量 `metadata`。Anklang 不返回题面摘录、复核结论、审核建议、通过/拦截字段或工作流状态。所有响应均带 `Cache-Control: no-store`。

若需要检查兼容行为，可改用：

```text
POST /api/v1/checks/similarity
```

v1 只在完整检索时返回 HTTP 200；部分或不可用会返回 HTTP 503 固定错误，且不携带候选。鉴权失败、请求非法、服务繁忙等也会返回固定错误对象。

## Urmotiv 单题增量入库

生产环境可用已有 Bearer 服务令牌调用唯一的单题写入路由：

```text
PUT /api/v1/index/problems
Authorization: Bearer <ANKLANG_SERVICE_TOKEN>
Content-Type: application/json
```

鉴权会在读取请求正文前完成。正文必须严格匹配以下可复制的合成 JSON（不要把生产题面、令牌或外部响应粘贴到 shell 历史或报告）：

```json
{
  "apiVersion": "1",
  "requestId": "11111111-1111-4111-8111-111111111111",
  "externalId": "synthetic-urmotiv-1",
  "updatedAt": "2026-08-28T00:00:00.000Z",
  "problem": {
    "title": "合成示例：数组求和",
    "basicStatement": "这是用于接口联调的合成题面：计算数组元素的总和。"
  }
}
```

顶层和 `problem` 都不接受额外字段。`externalId` 非空且最多 200 个 UTF-16 单元，`title` 为 1–200，`basicStatement` 为 1–500,000；`updatedAt` 必须是以 `Z` 结尾的 UTC 时间。成功 HTTP 200 响应严格返回 `apiVersion`、`requestId`、`source`、`externalId`、`contentHash`、`outcome`，其中 `source` 固定为 `urmotiv`，`outcome` 为 `inserted`、`updated` 或 `unchanged`。所有响应带 `Cache-Control: no-store`。

重复提交相同题面和版本返回 `unchanged`；较新的标题变更复用向量，题面变更会重新 embedding 并原子替换。旧版本或同时间冲突版本返回 HTTP 409（`STALE_UPDATE`）；embedding 缺失/失败或索引不可用返回 HTTP 503（`INDEX_UNAVAILABLE`）；正文非法返回 HTTP 400；令牌无效返回 HTTP 401。请求和后台查询共用 `ANKLANG_MAX_IN_FLIGHT_CHECKS` 在途上限。

服务端不会接受调用方提供的 source/namespace、URL、metadata、verdict、workflow state 或 Fermata 字段，也没有删除路由。该路由**使 Urmotiv 适配器能够接入**实时题目，但**不在 Anklang 中实现 Urmotiv 适配器**、授权读取、业务判断或工作流；集成方必须在受控边界中单独提供适配器。Anklang 仍不主动调用 Urmotiv、不共享数据库。

## 实时来源适配器

来源适配器是 Anklang 进程可导入的 `anklang/sources/<name>/` 子包。它只需导出：

```text
SOURCE_NAME: str
fetch_new_problems(since: str | None) -> list[RawProblem]
```
当前仓库只带 `example_static` 合成来源；以 Urmotiv 为数据源的 Anklang 来源适配器（可导入来源子包）未随当前实现提供。部署前，集成方必须实现并随运行环境提供该适配器，并通过获授权的数据读取方式取得题目；否则实时导入仍是未满足的前置条件。Anklang 不会自动发现另一个仓库中的来源适配器，也没有跨服务回调或数据库直连接口。

打开以下设置后，进程按间隔发现来源、读取游标、规范化题面、调用 embedding 并幂等写入 SQLite：

```text
ANKLANG_INGEST_ENABLED=true
ANKLANG_INGEST_INTERVAL_SECONDS=3600
```

在集成方已提供该适配器并具备获授权的数据读取方式的前提下，Urmotiv 的实时新增或更新题目可按上述契约接入；Anklang 负责把提交完成的向量行纳入当前索引。下一次查询立即读取新快照，不需要重启或离线全量重建。一个来源失败不会阻断其他来源；embedding 失败不会推进该来源游标。

这条接入路径不是 Urmotiv 工作流 API。Anklang 不主动调用 Urmotiv、不共享 Urmotiv 数据库，也不拥有问题状态。抄袭/检查信息属于 Urmotiv 问题属性，可由 Urmotiv 的受信任插件添加并由 Fermata 从带版本号的 Urmotiv HTTP 接口读取；不要把这些属性解释为 Anklang 的检索结论。

手工执行一轮导入（适合受控维护窗口）：

```bash
PYTHONPATH=. python3 -m anklang.ingest
```

命令输出只有合成计数和固定类别，不包含题面、路径、密钥或上游异常原文。

## 固定 32/32 证据
受控验收记录使用可复现的 Formal156 题目快照，从 156 条来源记录按固定规则均匀抽取 32 条查询。记录绑定 DashScope `text-embedding-v4`（1024 维）、156 条向量索引、`upstream-v2` 查询模式和已启用的提供方配置，记录到：

- 32 条请求、32 条 HTTP 200 响应；
- 32 条结果的 `completion.status` 均为 `complete`；
- 失败数为 0。

这只证明固定样本下的提供方连通性、版本化响应契约和完整结果形成能力。它不是语义准确率、全量题库质量、同题或抄袭判定、审核结论，也不是 Urmotiv/Fermata 工作流的质量保证。固定证据中的请求正文、题库内容、令牌和完整指纹不随仓库分发。

## 安全与数据边界

- 宿主端口默认只绑定回环地址；需要外部访问时，应在受控网络边界后再代理，不要直接暴露服务。
- 生产必须启用服务令牌；只有查询 POST 路由使用 Bearer 鉴权，存活、就绪和健康路由用于本地探针。
- 日志不记录请求行、题面、外部响应或异常原文；HTTP 错误只返回固定消息。
- SQLite 数据卷只属于 Anklang。Urmotiv、Fermata 和来源适配器不得共享 Anklang 数据库文件。
- `ANKLANG_MINIMUM_SIMILARITY` 只是显示下限，不是重复、抄袭、通过或拦截政策。

## 排查顺序

1. `docker compose config -q` 失败：检查 Compose 声明的环境文件是否存在、布尔值是否为小写 `true`/`false`，以及端口变量是否为合法数字。
2. `/api/v1/live` 失败：检查容器状态、宿主端口映射和进程启动配置。
3. `/api/v1/ready` 返回 503：服务正在停止接收新请求；等待进程退出并由 Compose 重启，或检查停止信号处理。
4. `/api/v1/health` 为 `degraded`：先检查数据卷可写性、SQLite 文件和 embedding 模型/维度是否与索引一致。
5. v2 返回 `unavailable`：检查 `DASHSCOPE_BASE_URL` 与 `DASHSCOPE_API_KEY` 是否同时存在、接口是否返回匹配的模型和维度，以及本地索引是否为 `ready`。不要把它当作“没有相似题”。
6. 增量数量不变：确认 `ANKLANG_INGEST_ENABLED=true`、来源适配器可被导入、更新时间游标有效，并检查 embedding 是否失败；不要通过推进游标来掩盖失败。
