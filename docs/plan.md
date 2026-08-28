# Anklang 实现说明与维护边界

本文档记录当前可部署实现的职责、数据契约和验收边界。Anklang 是公开项目 [is-my-problem-new](https://github.com/fjzzq2002/is-my-problem-new) v2 的小型直接改编，不是 Urmotiv 或 Fermata 的工作流服务。

## 产品职责

Anklang 只做原题候选检索：

```text
查询题面 -> embedding -> cosine_all -> 相似度降序 -> collapse -> mkrow -> 候选
```

embedding（向量化）把文字转换为固定长度的数字向量；`cosine_all` 计算向量方向的相似度；`collapse` 按来源和稳定题号去重；`mkrow` 形成公开候选。返回值是检索事实，不是产品判断。

Anklang 不判断候选是否同题或可作参考，不决定通过、拦截、审核或后续流程，不调用 LLM 复核，不提供 yuantiji 代理，不保存查询结果或结果缓存，也不保存提交查询的业务状态。

跨服务边界如下：Urmotiv 的受信任插件可以把抄袭/检查信息作为 Urmotiv 的问题属性添加，供 Fermata 消费；Fermata 通过带版本号的 Urmotiv HTTP 接口读取这些属性。Anklang 不拥有这些属性、工作流状态或审核结论，不读取 Urmotiv/Fermata 数据库，也不提供两个服务运行时互调的接口。

## 上游保留与有限改动

上游 v2 运行入口和对应提交是 [`ui/server.py`](https://github.com/fjzzq2002/is-my-problem-new/blob/72e309bdcea2669bc3f476bea6fa81b1f21e788a/ui/server.py)。上游作者 Ziqian Zhong 的 MIT 版权声明保留在 [`LICENSE`](../LICENSE)。

| 上游路径或职责 | Anklang 实现 | 允许的差异 |
| --- | --- | --- |
| `ui/server.py` 运行入口、`cosine_all`、排序、`collapse`、`mkrow` | `ui/server.py` | 保留调用顺序；候选字段收窄为机器接口契约。 |
| embedding | `anklang/embedding.py` | 改用配置的百炼 OpenAI 兼容 `/embeddings` 接口，校验模型、维度、顺序、数量和响应大小。 |
| 内存题库 | `anklang/store.py` | 使用独立 SQLite 当前题目行和向量；查询每次读取当前快照。 |
| 题目更新 | `anklang/sources/`、`anklang/ingest.py` | 自动发现来源适配器，按来源维护 UTC 游标，增量幂等插入或更新。 |
| HTTP 适配 | `anklang/http_api.py` | 提供严格的 v1/v2 查询、存活、就绪和健康路由。 |

网页、查询改写、重排、统计、OJ 筛选、查询向量缓存、LLM 复核、流程采集和跨服务代理不属于当前实现。

## 数据与索引不变量

- 生产题库不随镜像提供。来源适配器提交 `RawProblem` 后，框架规范化题面并计算内容哈希，再使用配置的 embedding 写入 SQLite。
- 查询和入库必须使用同一个模型标识与维度；SQLite 中的索引身份不匹配时拒绝形成候选。
- `(source, external_id)` 是稳定主键。题目更新时间使用带毫秒的 UTC `Z` 字符串；同一题号出现无法判断先后的不同版本时整组跳过，并且不推进游标。
- 运行时增量写入完成后，下一个查询直接读取新快照；不需要重启服务或离线全量重建。
- embedding 未配置、提供方失败、向量无效或索引不可用时，v2 使用 `unavailable`，候选必须为空；入库任务不写无向量题目，也不推进失败来源的游标。
- 查询响应使用 `Cache-Control: no-store`。运行时没有结果缓存、复用策略、过期时间或索引代次状态。

## 来源适配器契约

服务每轮重新发现 `anklang/sources/` 下的来源子包。每个来源适配器包只导出：

```text
SOURCE_NAME: str
fetch_new_problems(since: str | None) -> list[RawProblem]
```
当前仓库只内置 `example_static` 合成来源；以 Urmotiv 为数据源的 Anklang 来源适配器（可导入来源子包）未包含在当前实现中。集成方必须实现并随 Anklang 运行环境提供该适配器，并通过获授权的数据读取方式取得题目；这里没有跨仓库自动发现、HTTP 回调或数据库直连。

`SOURCE_NAME` 必须全局唯一。`since` 是该来源上次成功推进的 UTC 时间；`null` 表示首次读取。每条 `RawProblem` 包含稳定 `external_id`、`title`、`statement`，并可包含安全 HTTP `url`、`updated_at` 和有界标量 `metadata`。`metadata` 只用于公开候选展示，不参加题面哈希、向量或相似度。

在集成方已提供该适配器并具备获授权的数据读取方式的前提下，Urmotiv 的实时新增题目按此契约接入。设置 `ANKLANG_INGEST_ENABLED=true` 后，后台按 `ANKLANG_INGEST_INTERVAL_SECONDS` 周期调用该适配器；一个来源失败不阻断其他来源。Anklang 负责校验、规范化、embedding、幂等写入和游标比较交换，不负责来源授权、业务属性或工作流状态。

## HTTP 契约

### 请求

`POST /api/v1/checks/similarity` 和 `POST /api/v2/checks/similarity` 均要求严格 JSON：

下面是可直接解析的合成请求形状：

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

`apiVersion` 必须与路径一致；`requestId` 必须是规范 UUID；`contentHash` 必须匹配 64 位小写十六进制。`problem` 只能有 `title`、`type`、`tagIds`、`basicStatement`，顶层也不允许额外字段。长度按 JavaScript UTF-16 字符串单元计算。完整范围和调用命令见 [`README.md`](../README.md)。


### 响应

v1 成功响应严格为 `apiVersion`、`contentHash`、`checkedAt`、`candidates`。它只在检索完整时返回 HTTP 200；部分或不可用返回 HTTP 503。

v2 成功形成的响应严格为：

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
  "candidates": []
}
```

`completion.status` 为 `complete`、`partial` 或 `unavailable`。完整结果的原因码固定为 `complete` 且不可重试；部分结果可以携带候选；不可用结果的候选数组必须为空。非完整结果可带固定原因码和可选的 `retryAfterSeconds`（1–86,400 秒）。

候选最多 50 条，按 `similarity` 降序；每条必填 `source`、`externalId`、`title`、`similarity`，可选 `url`。v2 可额外带来源适配器提供的有界 `metadata`，v1 不带该字段。`ANKLANG_MINIMUM_SIMILARITY` 只是显示下限，不形成重复、抄袭、通过或拦截政策。

HTTP 层不向外发送题面、来源摘录、模型原始响应、复核字段、审核建议或工作流属性。错误消息是固定文本，响应不缓存。

## 部署不变量

- 本机默认监听 `127.0.0.1`；Compose 容器内监听 `0.0.0.0`，宿主仍只映射到回环地址。
- 生产必须启用 `ANKLANG_REQUIRE_SERVICE_TOKEN=true` 并提供至少 16 个字符的 `ANKLANG_SERVICE_TOKEN`。存活、就绪和健康路由供本地探针使用。
- 容器以非 root 用户运行，根文件系统只读，只有 `/app/problems-data` 数据卷可写。
- `GET /api/v1/live` 只表示进程可响应；`GET /api/v1/ready` 只表示进程仍接受请求，不读取题库、不访问 embedding 或网络；`GET /api/v1/health` 返回本地索引和 embedding 配置状态，不返回密钥。
- 关闭时服务先停止接收新查询，再在 `ANKLANG_SHUTDOWN_GRACE_SECONDS` 内等待在途查询；Compose 停止宽限应更长。

部署命令和排查顺序见 [`docs/deployment.md`](deployment.md)。

## 固定 32/32 证据的范围
受控验收记录绑定一个可复现的 Formal156 题目快照，从 156 条来源记录按固定规则均匀抽取 32 条查询。记录绑定 DashScope `text-embedding-v4`（1024 维）、156 条向量索引、`upstream-v2` 查询模式和已启用的提供方配置，记录为 32/32 HTTP 200、32/32 `completion.status=complete`、失败 0 条。

这只证明固定样本下的提供方连通性、响应契约和完整结果形成能力，不证明语义准确率、全量题库质量、同题/抄袭判断、审核结论或工作流质量。请求正文、题库内容、令牌和完整指纹不进入仓库文档。

## 可验证入口

变更后应至少验证：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
python3 -m compileall -q anklang ui tests
docker compose config -q
docker build -t anklang:verify .
```

测试使用合成数据、注入的 embedding 响应和回环 HTTP，不发起真实外部请求；生产证据不应把真实题面、模型原始响应或令牌带入 Git。
