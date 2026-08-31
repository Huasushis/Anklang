# Anklang

Anklang 是独立部署的题面相似检索服务，提供版本化 HTTP 接口。

<a id="toc"></a>
## Table of Contents（目录）

- [1. Background（背景）](#background)
- [2. Prerequisites（前提）](#prerequisites)
- [3. Install（安装）](#install)
- [4. Start（启动）](#start)
- [5. Health（存活与健康）](#health)
- [6. Usage（使用）](#usage)
- [7. API（接口）](#api)
- [8. Configuration（配置）](#configuration)
- [9. Operations and Security（运维与安全）](#operations-and-security)
- [10. Testing（测试）](#testing)
- [11. Support（支持）](#support)
- [12. Contributing（贡献）](#contributing)
- [13. Maintainers（维护者）](#maintainers)
- [14. License（许可）](#license)

<a id="background"></a>
## 1. Background（背景）

Anklang 只做题面检索：把题面向量化（embedding，即转换为固定长度的数字向量），计算余弦相似度，按相似度排序、去重并返回候选。它不判断候选是否同题或可作参考，也不产生通过、拦截、审核、抄袭或工作流结论。

检索主链保持上游 [`is-my-problem-new` v2 的 `ui/server.py`](https://github.com/fjzzq2002/is-my-problem-new/blob/72e309bdcea2669bc3f476bea6fa81b1f21e788a) 的顺序：

```text
题面 -> embedding -> cosine_all -> 相似度降序 -> collapse -> mkrow -> 候选
```

Anklang 是独立进程、独立 SQLite 索引和独立凭据边界：

- Anklang 不连接 Urmotiv 的数据库，也不读取 Fermata 的数据库或运行时进程。
- Urmotiv 可以通过版本化 HTTP 接口查询 Anklang；若要把 Urmotiv 新题实时写入索引，集成方必须另行提供经过授权的适配器。
- Fermata 只能从 Urmotiv 读取受信任的业务属性；Anklang 不保存这些属性，也不提供 Fermata 与 Anklang 的运行时互调。
- Anklang 不提供代理转发或结果缓存；管理员可以在运行时选择 `yuantiji`、`local` 或 `hybrid`。
  `yuantiji` 直接调用其公开搜索 API，只发送当前查询题面；`local`/`hybrid` 才把题目行和向量落在本地 SQLite，并调用运行期配置的 OpenAI 兼容 embedding HTTP 提供方。
  三种来源彼此独立，Anklang 仍不执行 LLM 复核或业务裁决。

上游作者的 MIT 版权声明保留在 [`LICENSE`](LICENSE)。实现边界和维护不变量见 [`docs/plan.md`](docs/plan.md)。

<a id="prerequisites"></a>
## 2. Prerequisites（前提）

- Python 3.11 或更高版本；运行时只使用 Python 标准库。
- 本机运行需要可写的 SQLite 数据目录；容器运行需要 Docker Engine 与 Docker Compose v2。
- 生产环境需要一个至少 16 个字符的 `ANKLANG_SERVICE_TOKEN`，并将 `ANKLANG_REQUIRE_SERVICE_TOKEN` 设为 `true`。
- 选择 `local` 或 `hybrid` 时需要一个可访问的 OpenAI 兼容 embedding 提供方；选择 `yuantiji` 时不需要它。Anklang 不在环境变量或镜像中保存提供方密钥，提供方由管理接口在运行期配置。
- 如果启用实时入库，还需要由集成方提供来源适配器和获授权的数据读取方式；仓库内的 `example_static` 仅是合成来源。

配置字段和默认值见 [`.env.example`](.env.example)。程序不会自动读取 `.env` 文件；不要把令牌或提供方密钥写入仓库、镜像、命令历史或日志。

<a id="install"></a>
## 3. Install（安装）

本机运行不需要安装第三方 Python 包。推荐在仓库根目录创建隔离环境（可选）：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 --version
```

容器安装路径使用仓库提供的 [`Dockerfile`](Dockerfile)：

```bash
docker compose config -q
docker compose build
```

生产 Compose 配置要求 `private/anklang.env` 由部署平台或密钥管理器提供。该文件不应进入 Git，也不要用 shell `source` 读取它。

<a id="start"></a>
## 4. Start（启动）

### 本机启动

从仓库根目录执行；`PYTHONPATH` 让保留的 `ui.server` 入口和 `anklang` 包都能被发现：

```bash
PYTHONPATH=. python3 -m anklang
```

默认监听 `127.0.0.1:8730`。设置 `ANKLANG_BIND_HOST` 和 `ANKLANG_PORT` 可改变监听地址和端口。`yuantiji` 模式无需 embedding；`local`/`hybrid` 没有配置 embedding 提供方时，进程仍可启动并提供存活/就绪/健康路由，但涉及本地索引的查询和入库会明确返回不可用。

### Compose 启动

先按部署平台的方式准备只含环境变量的 `private/anklang.env`，再从仓库根目录执行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 anklang
docker compose down
```

容器内监听 `0.0.0.0:8730`，宿主默认只绑定 `127.0.0.1:8730`。容器以非 root 用户运行，根文件系统只读，只有 `/app/problems-data` 数据卷可写。完整发布前检查见 [`docs/deployment.md`](docs/deployment.md)。

<a id="health"></a>
## 5. Health（存活与健康）

三个 GET 路由都不会发起 embedding 或 yuantiji 网络请求：

```bash
curl --fail --silent http://127.0.0.1:8730/api/v1/live
curl --fail --silent http://127.0.0.1:8730/api/v1/ready
curl --fail --silent http://127.0.0.1:8730/api/v1/health
```

- `/api/v1/live` 返回 HTTP 200 表示 HTTP 进程能响应。
- `/api/v1/ready` 返回 HTTP 200 且 `ready: true` 表示进程仍接受请求；停止接收请求时返回 HTTP 503 和 `ready: false`。它不读题库，不访问 embedding 或网络。
- `/api/v1/health` 返回本地索引、题目数量和 embedding 配置状态。`status: "degraded"` 是本地状态提示，不是语义质量结论。

健康路由不需要服务令牌；生产部署仍应要求查询、入库和管理路由使用 `Authorization: Bearer <ANKLANG_SERVICE_TOKEN>`。

<a id="usage"></a>
## 6. Usage（使用）

### 查询

查询方计算 `contentHash` 并提交严格 JSON。下面是只含合成题面的 v2 示例；示例内容不代表生产题库：

```bash
curl --fail --silent \
  -H "Authorization: Bearer ${ANKLANG_SERVICE_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-raw '{"apiVersion":"2","requestId":"00000000-0000-4000-8000-000000000001","contentHash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","problem":{"title":"合成示例：数组求和","type":"traditional","tagIds":["array"],"basicStatement":"给定一个整数数组，计算并输出元素总和。"}}' \
  http://127.0.0.1:8730/api/v2/checks/similarity
```

v2 的 `completion.status` 是 `complete`、`partial` 或 `unavailable`。不可用时，候选数组必须为空；Anklang 不把不可用伪装成“没有相似题”。

### 接入 Urmotiv 实时题目

集成方可以把授权读取到的单题通过 `PUT /api/v1/index/problems` 写入 Anklang 自己的索引，也可以实现 `anklang/sources/` 下的来源适配器并打开 `ANKLANG_INGEST_ENABLED=true`。两种路径都不共享数据库，不允许把 Urmotiv 的权限、审核意见或工作流字段写进 Anklang。

### 配置检索来源

检索来源是独立于本地向量索引的运行时设置。默认生产配置为 `yuantiji`；切换为 `local` 或 `hybrid` 后，Anklang 只在本地保存自己的索引，不会把 Urmotiv 的权限、审核状态或 Fermata 属性写入其中：

```text
PUT /api/v1/admin/search-sources
{"mode":"yuantiji","yuantijiBaseUrl":"https://yuantiji.ac","yuantijiRerank":false}
```

使用 `POST /api/v1/admin/search-sources/test` 做主动连通性测试；它会发送固定合成题面验证真实搜索接口。`GET /api/v1/admin/search-sources` 只读当前配置，不会因公共服务变慢而阻塞健康探针或管理页。

### 配置 embedding

提供方配置只在进程内存中存在，重启后回到未配置。配置、查询、入库共用 Anklang 服务令牌；提供方的 `apiKey` 是另一项独立的外部服务凭据，只接受管理请求，永远不会出现在 GET 响应、健康状态、日志或 Git 中：

```text
PUT /api/v1/admin/embedding-provider
{
  "protocol": "openai",
  "baseUrl": "https://embedding.example.invalid/compatible-mode/v1",
  "apiKey": "由密钥管理器注入的提供方密钥",
  "model": "text-embedding-v4",
  "dimension": 1024
}
```

`protocol` 当前固定为 `openai`，表示请求 `POST {baseUrl}/embeddings`。`baseUrl` 必须是没有账号、密码、查询参数或片段的 HTTP/HTTPS 地址。首次配置、模型/维度/地址变化或旧索引缺少提供方身份时，服务会按小批次重建全部本地向量；重建期间保留旧向量但暂停本地检索，状态可从 embedding 状态中的 `rebuild` 读取。提供方请求失败、响应不符合契约、模型或维度与本地索引冲突时，查询和入库会失败关闭。

<a id="api"></a>
## 7. API（接口）

所有 JSON 响应都带 `Cache-Control: no-store`。错误只返回固定错误类别，不回显题面、路径、密钥、外部响应或异常原文。

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/live` | 无 | HTTP 进程存活。 |
| `GET` | `/api/v1/ready` | 无 | 是否仍接受请求。 |
| `GET` | `/api/v1/health` | 无 | 本地索引和 embedding 状态。 |
| `POST` | `/api/v1/checks/similarity` | 服务令牌 | 完整检索成功才返回 200；非完整状态返回 503。 |
| `POST` | `/api/v2/checks/similarity` | 服务令牌 | 返回带 `completion` 的版本化检索结果。 |
| `PUT` | `/api/v1/index/problems` | 服务令牌 | Urmotiv 适配器的单题增量入库。 |
| `GET` | `/api/v1/admin/embedding-provider` | 服务令牌 | 读取提供方公开状态，不含密钥。 |
| `PUT` | `/api/v1/admin/embedding-provider` | 服务令牌 | 在运行期设置提供方。 |
| `DELETE` | `/api/v1/admin/embedding-provider` | 服务令牌 | 等待在途向量化结束后清除提供方。 |
| `GET` | `/api/v1/admin/search-sources` | 服务令牌 | 读取来源选择，不主动探测公共服务。 |
| `PUT` | `/api/v1/admin/search-sources` | 服务令牌 | 选择 `yuantiji`、`local` 或 `hybrid`。 |
| `POST` | `/api/v1/admin/search-sources/test` | 服务令牌 | 主动测试 yuantiji（local 模式返回无需测试）。 |
| `POST` | `/api/v1/admin/embedding-provider/test` | 服务令牌 | 用固定合成文本测试 OpenAI 兼容接口，不保存设置。 |

### 查询请求和响应

查询顶层只能有 `apiVersion`、`requestId`、`contentHash`、`problem`；`problem` 只能有 `title`、`type`、`tagIds`、`basicStatement`。版本必须与路径一致，`requestId` 是规范 UUID，`contentHash` 是 64 位小写十六进制字符串。

v1 成功响应严格包含 `apiVersion`、`contentHash`、`checkedAt`、`candidates`，只在完整检索时返回 HTTP 200。v2 成功响应还包含：

```json
{
  "apiVersion": "2",
  "contentHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "checkedAt": "2030-01-01T00:00:00.000Z",
  "completion": {
    "status": "complete",
    "reasonCode": "complete",
    "retryable": false
  },
  "candidates": []
}
```

候选最多 50 条，按 `similarity` 降序；每条必填 `source`、`externalId`、`title`、`similarity`，可选 `url`。v2 可以带来源适配器提供的有界公开 `metadata` 和最多 32,000 个 UTF-16 单元的 `statement`（过长时附 `statementTruncated: true`），供上游界面展开核对。`ANKLANG_MINIMUM_SIMILARITY` 只是显示下限，不是重复、抄袭、通过或拦截阈值。

### 单题入库

`PUT /api/v1/index/problems` 的请求顶层只能有 `apiVersion`、`requestId`、`externalId`、`updatedAt`、`problem`，其中 `problem` 只能有 `title`、`basicStatement`。服务端先鉴权再读取正文，固定使用 `urmotiv` 命名空间，不提供删除路由。

成功响应严格包含 `apiVersion`、`requestId`、`source`、`externalId`、`contentHash`、`outcome`；`outcome` 为 `inserted`、`updated` 或 `unchanged`。旧版本或同时间冲突返回固定 `STALE_UPDATE`，embedding 或索引不可用返回 `INDEX_UNAVAILABLE`。相同请求可安全重放，但 Anklang 不会因不确定的网络结果自行重复业务写入。

### embedding 管理

`PUT` 请求接受 `protocol`（当前为 `openai`）、`baseUrl`、`apiKey`、`model`、`dimension`；`POST .../test` 只用固定合成文本测试，不保存密钥。`GET` 只返回 `configured`、`baseUrl`、`model`、`dimension` 和必要时的 `rebuild` 状态；`DELETE` 清除内存中的提供方。管理接口没有独立的 Urmotiv、Fermata 或 yuantiji 凭据，调用者必须使用 Anklang 的服务令牌。

字段范围、固定错误码和严格契约以 [`docs/plan.md`](docs/plan.md) 与 `anklang/contracts.py` 为准。

<a id="configuration"></a>
## 8. Configuration（配置）

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `ANKLANG_BIND_HOST` | `127.0.0.1` | 监听地址；Compose 在容器内覆盖为 `0.0.0.0`。 |
| `ANKLANG_PORT` | `8730` | 监听端口。 |
| `ANKLANG_SERVICE_TOKEN` | 空 | 查询、入库和管理接口的 Bearer 令牌。 |
| `ANKLANG_REQUIRE_SERVICE_TOKEN` | `false` | 设为 `true` 时要求至少 16 个字符的服务令牌。 |
| `ANKLANG_MAX_IN_FLIGHT_CHECKS` | `16` | 在途查询上限，范围 1–256。 |
| `ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS` | `15` | 客户端连续无数据时限，范围 1–300 秒。 |
| `ANKLANG_SHUTDOWN_GRACE_SECONDS` | `30` | 应用停止时等待在途查询的最长时间。 |
| `ANKLANG_SEARCH_K` | `8` | 内部检索候选数，范围 1–20。 |
| `ANKLANG_MINIMUM_SIMILARITY` | `0.5` | 候选显示下限，范围 0–1；不是业务阈值。 |
| `ANKLANG_SEARCH_MODE` | `yuantiji` | 默认来源：`yuantiji`、`local` 或 `hybrid`。 |
| `YUANTIJI_BASE_URL` | `https://yuantiji.ac` | yuantiji 公共搜索 API 根地址。 |
| `YUANTIJI_RERANK` | `false` | 是否请求 yuantiji 重排。 |
| `ANKLANG_LOCAL_DB_PATH` | `problems-data/local-index.db` | SQLite 文件路径，相对于启动目录。 |
| `ANKLANG_INGEST_ENABLED` | `false` | 是否开启来源适配器增量抓取。 |
| `ANKLANG_INGEST_INTERVAL_SECONDS` | `3600` | 增量抓取间隔，范围 60–86,400 秒。 |

`DASHSCOPE_*` 等环境变量不会配置或激活 Anklang embedding 提供方。提供方必须通过管理 API 注入，进程重启后需要重新配置；`ANKLANG_SEARCH_MODE=yuantiji` 不需要这些变量。

<a id="operations-and-security"></a>
## 9. Operations and Security（运维与安全）

### 运维

- 使用 `/api/v1/live` 做 liveness（存活）探针，使用 `/api/v1/ready` 做就绪探针；不要用它们推断语义准确率。
- 观察 `/api/v1/health` 的本地状态和容器日志；不要把请求正文、令牌或外部响应写入日志。
- 关闭时先停止接收新查询，再在 `ANKLANG_SHUTDOWN_GRACE_SECONDS` 内等待在途操作；Compose 的停止宽限应更长。
- 生产题库由受控来源适配器提供。来源失败不得阻断其他来源；失败或无向量题目不得推进游标。
- SQLite 数据卷应按部署平台备份。不要复制、提交或通过聊天分享题面和数据库内容。

### 安全边界

- 生产必须设置 `ANKLANG_REQUIRE_SERVICE_TOKEN=true`，并用至少 16 个字符的服务令牌保护查询、入库和 embedding 管理。
- Bearer 令牌是 Anklang 自己的服务凭据；它不等于 Urmotiv 机器人令牌、Fermata 管理令牌或 yuantiji 凭据。Anklang 不读取这些凭据。
- embedding `apiKey` 仅在配置调用和进程内存中存在，响应、日志和仓库中都不能出现。
- 服务端在读取请求正文前鉴权；严格限制请求大小、字段和字符串范围；响应不缓存，也不回显题面和外部错误。
- Anklang 只返回检索候选。相似度不是业务判定，任何重复或审核政策必须在 Urmotiv 等上游系统实现。
- 增量入库的更新冲突、模型/维度冲突和不可用提供方都失败关闭；没有隐式覆盖或删除接口。

<a id="testing"></a>
## 10. Testing（测试）

测试使用合成数据、注入的 embedding 响应和回环 HTTP，不发起真实外部请求。提交前从仓库根目录运行：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
python3 -m compileall -q anklang ui tests
docker compose config -q
docker build -t anklang:verify .
```

不要把测试通过写成语义准确率通过。32/32 一类的受控连通性或契约记录，只能证明固定输入下的接口与完整结果形成，不是全量题库质量、重复判断或审核质量保证。

<a id="support"></a>
## 11. Support（支持）

请在 [GitHub Issues](https://github.com/Huasushis/Anklang/issues) 报告可复现问题。报告版本、运行模式、固定错误码和不含敏感内容的健康状态；不要附题面、题库数据库、令牌、embedding 响应或日志中的密钥。

<a id="contributing"></a>
## 12. Contributing（贡献）

1. 从 `main` 创建主题分支，并保持提交只覆盖一个可审阅的行为变化。
2. 不改变上游本地检索调用顺序，不新增代理转发、结果缓存或 LLM 复核路径；yuantiji 仅作为明确可选的独立来源。
3. 涉及 HTTP、权限、来源、索引或密钥边界时，补充无权、失败关闭和边界测试。
4. 运行 [Testing（测试）](#testing) 中的命令，并在提交前确认没有私有数据、题面、数据库或密钥。
5. 提交说明应包含行为、验证命令和已知限制；不要把接口运行性写成语义准确率。

<a id="maintainers"></a>
## 13. Maintainers（维护者）

- [Huasushis](https://github.com/Huasushis)

<a id="license"></a>
## 14. License（许可）

本项目采用 MIT License。上游 `is-my-problem-new` 的版权声明保留在 [`LICENSE`](LICENSE)。

SPDX-License-Identifier: MIT
