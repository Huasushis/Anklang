# Anklang 独立部署与 v2 迁移

Anklang 必须作为独立服务部署，不连接 Urmotiv 数据库。运行时只需要 Python 3.11+ 标准库；真实
令牌和外部服务密钥由进程管理器注入环境变量，不写入仓库、镜像、命令行记录或日志。环境变量清单
及默认值见根目录 `.env.example`。

## 接口

- `GET /api/v1/live`：无需令牌、固定本地完成的进程存活检查；不读取题库，不访问任何外部服务。
- `GET /api/v1/health`：无需令牌的健康检查，只返回固定状态和安全计数。
- `POST /api/v1/checks/similarity`：旧接口。请求 `apiVersion` 必须为 `"1"`。只有完整检查返回旧版
  200 成功结构；部分完成或不可用固定返回 503。
- `POST /api/v2/checks/similarity`：新接口。请求 `apiVersion` 必须为 `"2"`。可信的完整、部分完成、
  不可用结果均返回严格 200 结构，并通过 `completion` 说明状态。

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

默认监听 `0.0.0.0:8730`。生产环境应由防火墙或反向代理只允许 Urmotiv 和运维健康检查访问；启用
`ANKLANG_SERVICE_TOKEN` 后，两个 similarity 路径都使用同一 Bearer 令牌。不要用 shell 的
`source` 或 `.` 读取真实环境文件。

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
