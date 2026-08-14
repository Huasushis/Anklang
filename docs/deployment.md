# Anklang 独立部署与 v2 迁移

Anklang 必须作为独立服务部署，不连接 Urmotiv 数据库。运行时只需要 Python 3.11+ 标准库；真实
令牌和外部服务密钥由进程管理器注入环境变量，不写入仓库、镜像、命令行记录或日志。环境变量清单
及默认值见根目录 `.env.example`。

## 接口

- `GET /api/v1/live`：无需令牌、固定本地完成的进程存活检查；不读取题库，不访问任何外部服务。
- `GET /api/v1/ready`：无需令牌、提供方无关的就绪检查；只验证本地服务状态，不调用任何后端、
  不发起任何网络请求，也不读取题库，返回 `Cache-Control: no-store`。与 `/live`（仅存活）和
  `/api/v1/health`（透传上游后端状态）语义区分。
- `GET /api/v1/health`：无需令牌的健康检查，只返回固定状态和安全计数。
- `POST /api/v1/checks/similarity`：旧接口。请求 `apiVersion` 必须为 `"1"`。只有完整检查返回旧版
  200 成功结构；部分完成或不可用固定返回 503。
- `POST /api/v2/checks/similarity`：新接口。请求 `apiVersion` 必须为 `"2"`。可信的完整、部分完成、
  不可用结果均返回严格 200 结构，并通过 `completion` 说明状态。

所有成功和错误响应都会带 `Cache-Control: no-store`。若部署时通过构建参数注入了 `ANKLANG_REVISION`
（如 `docker compose build --build-arg ANKLANG_REVISION=$(git rev-parse --short HEAD)`），则每个
响应还会带 `X-Anklang-Revision` 头，供发布观测区分部署版本；留空则不输出。该值只允许字母、数字、
点、下划线和连字符，不泄露路径或密钥。Compose 的 `build.args` 注入的值会写入镜像 `ENV`，作为
可靠默认值；`environment:` 和随附的 `private/anklang.env` 默认都不定义该变量，因此直接复制
`.env.example`（其中 `ANKLANG_REVISION` 处于注释状态）不会用空值覆盖构建注入。只有当操作者
确实需要在运行时覆盖构建注入的修订时，才在 `private/anklang.env` 中取消注释并填写非空值
`ANKLANG_REVISION=...`，此时 `env_file` 优先于镜像 `ENV`。

路径和正文版本不匹配时固定返回 400。Anklang 的全部 HTTP 响应都带
`Cache-Control: no-store`。反向代理不得覆盖或删除这个响应头，也不得自行缓存请求或响应正文。

v2 的完成状态如下：

| `completion.status` | 含义 | 候选与拦截 | `reuse` |
| --- | --- | --- | --- |
| `complete` | 检索及已配置复核全部完成 | 可以为空；按已校准策略判定 | 可能 `allowed`，也可能 `no-store` |
| `partial` | 某一路检索或复核未完成，可能仍有可信候选 | 不能只按相似度拦截；本次成功的模型复核明确同题时才可拦截 | 固定 `no-store` |
| `unavailable` | 无法形成可信候选 | 固定空候选且不拦截 | 固定 `no-store` |

`completion.reasonCode` 和 `retryable` 是固定机器字段；可选 `retryAfterSeconds` 只在适合重试时出现。
完整分支固定为 `reasonCode="complete"`、`retryable=false`。`reuse.policy="allowed"` 只会出现在完整
结果中，`expiresAt` 晚于 `checkedAt` 且最多相差七天。缓存命中会原样保留首次计算的 `checkedAt`
和绝对 `expiresAt`，不会因为读取而延长有效期。

v2 的顶层和各分支是严格结构，多字段或少字段都不合法：

```jsonc
{
  "apiVersion": "2",
  "contentHash": "<原样回显的 64 位小写十六进制摘要>",
  "checkedAt": "2026-08-01T00:00:00.000Z",
  "completion": {
    "status": "partial",
    "reasonCode": "search_partial",
    "retryable": true,
    "retryAfterSeconds": 30 // 仅 retryable=true 时可选，1..86400
  },
  "candidates": [
    {
      "source": "来源",
      "externalId": "来源内编号",
      "title": "标题",
      "similarity": 0.95,
      "url": "https://example.invalid/problem", // 可选
      "sameProblemSuggestion": true,             // 可选
      "explanation": "固定说明"                 // 可选
    }
  ],
  "recommendation": {
    "blockSubmission": false,
    "message": "固定说明"
  },
  "reuse": {"policy": "no-store"}
}
```

`completion` 的精确分支为：

```jsonc
{"status":"complete","reasonCode":"complete","retryable":false}
{"status":"partial","reasonCode":"<非完整原因>","retryable":true,"retryAfterSeconds":30}
{"status":"unavailable","reasonCode":"<非完整原因>","retryable":false}
```

后两个分支的 `retryAfterSeconds` 都是可选字段，但只有 `retryable=true` 时允许出现。九个非完整原因
只有：`search_timeout`、`search_rate_limited`、`search_backend_unavailable`、
`search_backend_invalid`、`search_partial`、`review_unavailable`、`service_unavailable`、
`service_invalid_response`、`internal_error`。

`reuse` 的精确分支只有：

```jsonc
{"policy":"allowed","expiresAt":"2026-08-01T01:00:00.000Z"}
{"policy":"no-store"}
```

`allowed` 只允许用于 `complete`；`partial` 和 `unavailable` 固定使用 `no-store`。`unavailable` 固定
空候选且 `blockSubmission=false`。`partial` 不能只凭相似度阈值设为拦截；只有本次成功且可信的
模型复核为候选写入 `sameProblemSuggestion=true` 时才可以。只有 `complete` 的空候选数组表示完整
检查后没有候选。

## 启动与验证

```sh
python3 -m anklang
```

默认只监听 `127.0.0.1:8730`。非容器部署通常保持这个默认值，再由同机反向代理访问；只有明确的
容器网络需要把 `ANKLANG_BIND_HOST` 设为 `0.0.0.0`，同时仍应让宿主映射只绑定回环地址。生产环境
必须设置 `ANKLANG_REQUIRE_SERVICE_TOKEN=true` 和至少 16 字符的 `ANKLANG_SERVICE_TOKEN`；两个
similarity 路径使用同一 Bearer 令牌。不要用 shell 的 `source` 或 `.` 读取真实环境文件。

生产运行边界由以下变量控制，启动时严格校验；越界或拼写错误会直接拒绝启动：

| 变量 | 默认值 | 允许范围 | 用途 |
| --- | ---: | ---: | --- |
| `ANKLANG_MAX_IN_FLIGHT_CHECKS` | 16 | 1..256 | 同时进入鉴权、正文读取和后端检索的查重数 |
| `ANKLANG_CLIENT_IDLE_TIMEOUT_SECONDS` | 15 | 1..300 秒 | 客户端连续不发送正文数据的最长时间 |
| `ANKLANG_SHUTDOWN_GRACE_SECONDS` | 30 | 1..300 秒 | 收到 `SIGTERM`/`SIGINT` 后等待在途请求的最长时间 |

达到并发上限或服务正在退出时，v1/v2 查重都会在读取正文、调用检索或模型前固定返回 503
`SERVICE_BUSY`，并带 `Retry-After: 1` 与 `Cache-Control: no-store`。收到退出信号后服务停止接收新
查重，关闭监听套接字，并在宽限期内等待已经开始的请求；到期后请求线程不会继续阻止进程退出。
正文读取连续无数据超时固定返回 408 `CLIENT_TIMEOUT`，这些错误都不包含题面、密钥或异常原文。

## 独立容器部署

`Dockerfile` 基于 Python 3.11 slim，只复制 `anklang/` 运行包和许可证；构建上下文采用默认拒绝清单，
不会把 `.env`、私有目录、题库、缓存、数据库、报告、测试或 Git 元数据复制进镜像层。镜像中的服务
使用固定非 root 用户。`compose.yaml` 进一步启用只读根文件系统、移除全部 Linux capabilities
（进程的额外系统权限）、禁止获取新权限并限制进程数；只有 `/app/problems-data` 命名卷和小型
`/tmp` 临时文件系统可写。

1. 创建被 Git 整体忽略的 `private/` 目录，把 `.env.example` 复制成权限仅当前用户可读写的
   `private/anklang.env`；至少配置一个长度不小于 16 的 `ANKLANG_SERVICE_TOKEN`：

   ```sh
   install -d -m 700 private
   cp .env.example private/anklang.env
   chmod 600 private/anklang.env
   ```

   Compose 会直接读取它，不要 `source`，也不要把展开配置后的输出写入日志。
   复制进来的 `ANKLANG_REVISION` 默认处于注释状态：除非取消注释并填写非空值，
   否则不会覆盖镜像构建时注入的修订标识（见上文接口说明）。
2. 运行 `docker compose --env-file private/anklang.env up --build -d`。容器内显式监听
   `0.0.0.0:8730`，宿主端口固定映射到
   `127.0.0.1:${ANKLANG_PORT:-8730}`，不会直接监听所有宿主网卡。
3. 用 `GET http://127.0.0.1:8730/api/v1/live` 检查进程存活，用 `/api/v1/ready` 检查本地就绪，再用 `/api/v1/health` 检查后端就绪。
   Docker 健康检查只调用 `/live`，不会因监控探针触发任何外部请求。
4. 停止时 Compose 默认给 45 秒，应用内部默认给 30 秒；前者必须始终大于后者。若在
   `private/anklang.env` 修改 `ANKLANG_SHUTDOWN_GRACE_SECONDS`，也要把
   `ANKLANG_STOP_GRACE_PERIOD` 设为更大的秒数值（例如 `75s`），Compose 会把两者分别传给应用和
   容器运行时。

生产数据只放在 `anklang-problems-data` 卷。备份或替换本地索引时按 README 的非破坏性重建流程
处理，不把宿主私有目录整体复制进镜像，也不把 Anklang 连接到 Urmotiv 的数据库。

### 作为 Urmotiv 的可选 profile 运行

Urmotiv 同级仓库的 `compose.yaml` 已提供默认关闭的 `anklang` profile。它使用 Anklang 正式镜像
边界和同一个 `Anklang/private/anklang.env`，固定容器端口 8730、非 root 用户、只读根文件系统、
`cap_drop: ALL`、`no-new-privileges`、进程数限制、小型 `/tmp`、独立数据卷、`/api/v1/live`
健康检查以及 45 秒容器停止宽限。宿主只绑定 `127.0.0.1:8730`。

在 Urmotiv 仓库运行：

```sh
docker compose --env-file <Urmotiv 私有主环境文件> --profile anklang up -d anklang
```

Urmotiv 插件的 `baseUrl` 使用 `http://anklang:8730`，服务令牌与
`Anklang/private/anklang.env` 中的值一致。Anklang 令牌、yuantiji 地址、DashScope 和可选复核模型
密钥都不得复制进 Urmotiv 主环境文件。默认 profile 不会创建 Anklang 容器；也不要与独立 Compose
同时占用同一宿主端口。

部署前在服务器仓库中运行：

```sh
python3 -m compileall -q anklang tests
python3 -m unittest discover -s tests
git diff --check
```

测试只使用合成题面和假外部服务，不会真实联网。部署后先核对健康检查，再用人工编写的合成请求分别
验证 v1 完整成功、v1 非完整 503、v2 三种完成状态以及所有响应的 `Cache-Control: no-store`。

## 从 v1 迁移到 v2

1. 先部署同时提供 v1/v2 的 Anklang；旧调用方继续使用 v1，不会收到新增字段。
2. 在调用方独立加入严格 v2 schema 和三分支处理。只有 `complete` 的空候选表示“完整检索后没有
   候选”；`partial`/`unavailable` 绝不能解释为通过。
3. 调用方若采用业务层复用，只接受 `reuse.policy="allowed"`，同时核对 `contentHash`、`checkedAt`
   和未过期的 `expiresAt`；`no-store` 不得保存。
4. 用合成故障验证超时、429、畸形上游、模型复核失败和服务不可用，再把调用路径切到 v2。
5. 观察期内保留 v1 以便回滚调用方；回滚只切换路径和请求版本，不复制数据库或清空 Anklang 缓存。

每次修改检索配置、模型、阈值或本地索引后，服务生成的内部缓存身份都会变化。只缓存完整结果；
缓存键保存的是规范化请求和配置的摘要，不保存题面原文，但内存仍应只存在于 Anklang 进程中。
