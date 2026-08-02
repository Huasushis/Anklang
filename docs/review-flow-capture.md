# Fermata 审题流程的 Anklang v2 可信采集

本页说明怎样为 Fermata 的审题流程标定采集 Anklang v2 原始请求和原始响应。这里的“可信”不是说
远程题库一定正确，而是说采集程序能证明：固定版本的代码对清单中的每个请求只调用一次固定接口，
只把严格、完整的 200 响应纳入结果，并且批次中途没有悄悄重试、漏项或切换后端状态。

采集器不会自动生成题面请求，也不会判断材料是否可以发送给外部服务。操作员必须先完成材料授权和
脱敏检查，再在 Git 已忽略的私有目录中人工建立请求文件与 manifest。题面、原始响应、令牌和实际
服务地址只留在这个私有目录，不会出现在证明文件、终端摘要或固定错误码中。

## 使用前提

- 只能从无未提交文件的 Anklang 提交运行。采集器把当前 40 位 Git 提交和四个固定依赖文件的摘要
  写进证明；代码未跟踪、工作区有改动或运行中代码身份变化都会拒绝完成。
- 私有工作目录及其 `runs/` 子目录权限必须是 `0700`；manifest 和请求文件必须是当前用户所有的
  普通文件，权限为 `0400` 或 `0600`，且不得是符号链接。
- 私有工作目录必须位于 Anklang 仓库内，并由 `.gitignore` 明确忽略。manifest 和请求文件必须直接
  放在工作目录下，不能用路径跳转引用别处。
- 当前版本只接受 `reverse_proxy`。远程语料无法由采集器独立重建，因此证明会如实标成
  `remote_corpus_unverifiable`。`local_engine` 需要另外实现并固定一套 SQLite 快照验证器后才能
  生成 `reproducible_snapshot` 证据；操作员自报几个哈希不足以证明本地语料可重现。
- 服务令牌只从 `ANKLANG_SERVICE_TOKEN` 读取，至少 16 个字符。不要用 `source` 或 `.` 加载环境文件。

## 私有 manifest

下面只给字段形状；所有摘要、UUID、地址和请求文件都要由操作员依据本次真实运行填写。`cases`
可有 1 到 2000 项，`expectedCaseCount` 必须与其实际长度相同，不固定为某个标定集规模。

```json
{
  "schemaVersion": 1,
  "artifactKind": "anklang_review_flow_v2_capture_manifest",
  "captureId": "capture-0123456789abcdef",
  "expectedCaseCount": 1,
  "endpoint": "https://service.example/api/v2/checks/similarity",
  "timeoutMs": 300000,
  "externalStatementTransferConfirmed": true,
  "runtimeDeclaration": {
    "backend": "reverse_proxy",
    "searchK": 8,
    "minimumSimilarity": 0.5,
    "blockThreshold": 0.9,
    "similarityBlockEnabled": false,
    "cacheTtlSeconds": 3600,
    "llmReviewEnabled": false,
    "llmModel": null,
    "llmReviewTopN": null,
    "llmEndpointSha256": null,
    "reverseProxy": {
      "useRerank": false,
      "upstreamEndpointSha256": "64 位小写十六进制摘要"
    },
    "localEngine": null,
    "corpus": {
      "evidenceKind": "remote_corpus_unverifiable",
      "serviceOriginSha256": "与 upstreamEndpointSha256 相同的摘要",
      "declarationSha256": "远程语料操作员声明的摘要"
    }
  },
  "cases": [
    {
      "caseId": "case-synthetic-1",
      "request": {
        "fileName": "request-0001.json",
        "sha256": "request-0001.json 完整原始字节的 SHA-256"
      }
    }
  ]
}
```

每个请求必须是严格的 Anklang v2 JSON，并有批次内唯一的 `requestId`、文件名和完整原始字节摘要。
采集器发送文件原始字节，不会重新序列化题面；因此 manifest 绑定的就是实际发送内容。

`runtimeDeclaration` 是操作员声明，不是服务器自证。端点、上游、可选 LLM 地址只填 SHA-256，
不得填明文地址或密钥。启用 LLM 时，`llmModel`、`llmReviewTopN` 和 `llmEndpointSha256` 必须同时
填写；禁用时三者必须都是 `null`。

## 运行和恢复

```sh
ANKLANG_SERVICE_TOKEN='由私有环境注入的令牌' \
python3 scripts/capture-review-flow-calibration.py \
  --workspace private/review-flow-capture \
  --manifest private/review-flow-capture/manifest.json
```

采集器会先请求一次同源的固定 `GET /api/v1/health`，然后按 manifest 顺序向固定
`POST /api/v2/checks/similarity` 各发送一次原始请求，最后再请求一次 health。三类请求都使用同一
Bearer 令牌，不跟随重定向，也没有自动重试。每次外部调用前都会先把 `active` 状态同步到磁盘；
如果进程在响应安全落盘前退出，恢复时该调用固定记为取消，绝不猜测服务端是否收到并重发。

```sh
ANKLANG_SERVICE_TOKEN='由私有环境注入的令牌' \
python3 scripts/capture-review-flow-calibration.py \
  --workspace private/review-flow-capture \
  --manifest private/review-flow-capture/manifest.json \
  --resume
```

恢复只继续尚未开始的请求。已经完成但结果不完整、HTTP 非 200、响应不合法、响应缺失或中断的样本
都会使整批保持不完整。health 调用一旦进入不确定状态也不会重试；这时应保留当前运行目录作为失败
证据，使用新的 `captureId` 重新开始整批，而不是覆盖原目录。

## 只读复验

完成采集后、交给 Fermata bridge 前，应使用同一份已提交代码执行一次离线复验。三项 verifier 身份
必须来自准备接收证据的一方所固定的 Anklang 代码身份，不能从待验证 attestation 自己抄写：

```sh
python3 scripts/capture-review-flow-calibration.py verify-capture \
  --workspace private/review-flow-capture \
  --manifest private/review-flow-capture/manifest.json \
  --verifier-code-version '40 位提交号' \
  --verifier-runner-sha256 '采集入口文件摘要' \
  --verifier-dependency-code-sha256 '四个固定依赖文件的组合摘要' \
  > private/review-flow-capture/verified-attestation.json
```

`verify-capture` 不读取令牌，不联网、不重试，也不创建、替换或修复任何文件。它要求 Git 工作区和
固定四个依赖都与 expected 身份一致，并在复验前后再次核对代码身份。随后依次重读 manifest 和原始
request、完整且没有 active/invalidated 状态的 checkpoint、两份 health 正文及 HTTP 元数据、每题
request/response 与 response-meta、attestation 和完成标记；响应会重新执行严格 v2、内容摘要和完整
状态校验，前后 health 仍须规范等值，完成标记必须逐字节等于确定性重建结果。

成功时标准输出只包含原先保存的 attestation 原始字节。失败时标准输出为空，标准错误只输出一个固定
错误码；复验失败不能通过重写文件或补做网络请求修复，只能保留失败证据并按规则重新采集。

## 完整性门禁与输出

health 必须是 HTTP 200、`application/json`、`Cache-Control: no-store`，并包含
`status=ok`、`service=anklang`、`apiVersion=1` 以及与操作员声明相同的后端。前后响应会先按 JSON
对象键排序并去除无意义空白，再要求规范内容完全一致；`upstreamReady`、题量、索引状态或任何其他
字段在批次中变化都会拒绝完成。两份原始 health 字节及其 HTTP 状态、内容类型、缓存头元数据都分别
以 `0600` 排他保存；恢复时会重新核对，不会只看 JSON 正文中的 `status`。

后端的 `configurationSha256` 是以下安全结构的规范 JSON 摘要，不是服务器对操作员声明真实性的
签名：

```text
protocol + declarationSource
+ runtimeDeclaration + runtimeDeclarationSha256
+ captureInput.manifestSha256
+ health.beforeSha256 + health.afterSha256
+ health.beforeCanonicalSha256 + health.afterCanonicalSha256
+ health.beforeMetadataSha256 + health.afterMetadataSha256
+ health.beforeBackend + health.afterBackend
+ health.beforeStatus + health.afterStatus
+ health.backendEqual + health.statusEqual + health.responseEqual
```

单个 v2 响应只有同时满足 HTTP 200、JSON、`no-store`、严格 v2 契约、`contentHash` 与请求一致、
`completion.status=complete` 才算完整。请求和响应快照都用 `0600`、排他创建，已有同名文件时不会
覆盖。每次收到 HTTP 响应还会排他保存一份仅含状态码、内容类型、缓存头和正文摘要的元数据；构建
或恢复批次时会从这份元数据和原始响应重新运行严格 v2 校验，并要求结果与 checkpoint 逐字段一致，
不会直接相信 checkpoint 中的 `complete`。运行目录还保存权限相同的 checkpoint 和两份 health
原始响应。

只有全部样本完整、前后 health 一致、manifest 未变化、代码仍是启动时的干净提交，才会生成：

- `attestation.json`：Fermata 可校验的代码、配置摘要、语料声明和逐样本请求/响应摘要；
- `REVIEW_FLOW_ANKLANG_CAPTURE_COMPLETE`：最后一步排他创建的完成标记，绑定 attestation 与整组
  样本摘要。

任何失败、取消、缺失或不完整响应都不会生成这两个文件。完成标记存在才表示这批资料可以交给
Fermata bridge；不能根据进程退出码、已有部分响应或人工观察自行补标。
