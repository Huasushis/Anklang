# Anklang 实施规划

- 版本：0.1（规划稿，仓库尚未有实现代码）
- 日期：2026-07-26

本文档面向从零开始实现 Anklang 的接手人，配合仓库根目录的 `AGENTS.md`（开发约定与安全红线）和
`README.md`（项目定位与文档索引）一起阅读。三份文档共同保证：不看 Urmotiv 主仓库的任何其他材料，
也能把 Anklang 从空仓库做到可用。

Anklang 要对接的上游、参考项目和契约来源如下，后文会反复引用：

- **Urmotiv**：USTC 算法竞赛协会的私有题库系统，本仓库的唯一"客户"。它已经实现了调用 Anklang 的
  客户端代码（`plugins/anklang/src/index.ts`），本规划的接口约定完全以这份已实现代码为准。
- **is-my-problem-new**（GitHub：`fjzzq2002/is-my-problem-new`，别名"原题机"）：一个开源的原题检索
  项目，Anklang 阶段 2、3 的技术路线主要参考它。仓库有 `main` 和 `v2` 两个分支，公开部署实例
  `yuantiji.ac` 目前运行的是 `v2` 分支的代码（已通过实测确认，见阶段 1）。
- **yuantiji.ac**：`is-my-problem-new` 的公开部署实例，由原作者个人自费维护。阶段 1 会把它当作
  反向代理的后端。

---

## 1. 技术选型

**结论：Anklang 使用 Python（3.11 及以上），Web 框架用 FastAPI，数据校验用 Pydantic v2。不使用
TypeScript。** 下面说明理由，不做模棱两可的对比。

### 1.1 对比

| 维度 | TypeScript（与 Urmotiv 一致） | Python（与 is-my-problem-new 一致） |
| --- | --- | --- |
| 与 Urmotiv 共享代码/类型 | Urmotiv 是 pnpm 工作区，包内可以共享 `zod` 类型；但 Anklang 是**独立服务**，只通过 HTTP JSON 通信，不进同一个工作区，这个优势用不上 | 无共享类型，但契约本来就该用"双方各自校验同一份 JSON 样例"的方式保证，与语言无关 |
| 复用 is-my-problem-new 的代码 | 需要把语料清洗、embedding 流水线、PDF/OCR 解析、去重逻辑全部从 Python 重写成 TypeScript，工作量大、容易引入新 bug，且重写"别人已经踩过坑"的部分收益很低 | 阶段 2、3 最有价值、最难写对的部分（语料库构建、embedding、检索）可以直接参考甚至复用（在遵守 MIT 许可证的前提下，见第 5 节），只需要适配 Urmotiv 的契约这一层 |
| 向量检索 / PDF/OCR 生态 | Node 生态在这两块明显弱于 Python | `faiss`、`numpy`、`PyMuPDF`、`pdfplumber` 等库成熟、文档充分 |
| 团队熟悉度 | 团队已经在 Urmotiv 上用 TypeScript | 竞赛圈（算法竞赛/CP 社区）对 Python 的普及度不低于 TypeScript，不构成额外的贡献门槛 |
| 部署形态 | 需要 Node 运行时 | 需要 Python 运行时；两者在 Docker 里复杂度相当 |

阶段 1（反向代理）本身用哪种语言都很简单，但阶段 1 的选择不应该只看阶段 1——如果阶段 1 用
TypeScript、阶段 2 又要重写成 Python 来复用参考项目代码，等于中途换语言，得不偿失。所以从阶段 1
起就定为 Python。

### 1.2 具体约定

- **语言**：Python 3.11+。
- **Web 框架**：FastAPI + Uvicorn（与 `is-my-problem-new` v2 的 `ui/server.py` 思路一致，单进程
  即可跑通阶段 1）。
- **数据校验**：Pydantic v2，为 Urmotiv 契约中的每个 JSON 结构建一份对应模型（详见第 2.4 节）。
- **包管理**：建议 `uv`（速度快、自带锁文件）；如果接手人更熟悉传统方式，`pip` + `requirements.txt`
  也可以，但必须锁定版本号，保证可复现安装。是否使用 `uv` 由实现者决定，本规划不强制。
- **代码格式/静态检查**：建议 `ruff`（同时做 lint 和 format），减少额外工具依赖。
- **测试框架**：`pytest`。

### 1.3 契约保证方式（没有共享类型怎么办）

Anklang 与 Urmotiv 之间不共享代码仓库，也就不能像 Urmotiv 内部那样用同一份 `zod` 类型两边同时校验。
替代做法：

1. 在 Anklang 仓库的测试目录里保存一份**固定 JSON 样例**（fixture），内容是符合 Urmotiv
   `anklangRequestSchema` / `anklangResultSchema` 的真实例子（从 `plugins/anklang` 的测试用例copy
   过来即可，见第 2.4 节）。
2. Anklang 自己的 Pydantic 模型必须校验通过这些样例；任何字段名、类型、取值范围的差异都会被测试
   捕获。
3. 如果 Urmotiv 一侧的契约发生不兼容变化（`apiVersion` 升级），需要手工同步一份新的 fixture，不能
   假设两边会自动保持一致——这正是"不共享类型"必须付出的代价，用测试补回来。

---

## 2. 阶段 1：yuantiji.ac 反向代理插件（优先，最小可用）

### 2.1 目标

几天内做出一个能被 Urmotiv 实际调用、满足契约的 Anklang：把 Urmotiv 的查重请求转发给公开服务
yuantiji.ac，把结果翻译成 Urmotiv 要的格式，加一层可选的 LLM 复核（LLM 复核：让一个大语言模型
读题面和候选题，判断"这是不是同一道题"，作为比单纯相似度数字更可靠的判断依据）。这一阶段**不**
自建题库、**不**做 embedding、**不**碰 vjudge。

### 2.2 与 Urmotiv 的契约（完整搬运，必须逐字段满足）

以下内容来自 Urmotiv 仓库 `plugins/anklang/src/index.ts` 中已经实现的客户端代码，是 Anklang
必须满足的服务端契约，不是建议。

**接口**：`POST {baseUrl}/api/v1/checks/similarity`

**请求头**（Urmotiv 客户端固定发送）：

```
Accept: application/json
Content-Type: application/json
X-Urmotiv-API-Version: 1
Authorization: Bearer <token>   # 仅当 Urmotiv 一侧配置了令牌时才会带；Anklang 若没配置校验，必须允许缺省该头
```

**请求体**（对应 `anklangRequestSchema`）：

```jsonc
{
  "apiVersion": "1",              // 必须字面量 "1"
  "requestId": "<uuid>",          // 每次请求唯一，响应不需要回传它
  "contentHash": "<64位小写十六进制>",  // 正则 ^[a-f0-9]{64}$，只当作不透明缓存键使用，不需要知道怎么算出来的
  "problem": {
    "title": "string，1-200 字符",
    "type": "traditional | interactive | submit_answer",
    "tagIds": ["至少 1 个、最多 30 个标签字符串，每个 1-120 字符"],
    "basicStatement": "Markdown 文本，1-500000 字符"
  }
}
```

Urmotiv **只发送**查重必需的信息：题目名称、类型、知识点标签、基础题面、内容摘要。**不会发送**
作者身份、学号、邮箱、基础题解、完整题解、测试数据、附件或任何审核意见。Anklang 的实现不能假设
将来会拿到更多字段，也不需要更多字段。

**响应体**（对应 `anklangResultSchema`，Urmotiv 用 `zod` 的 `.strict()` 模式校验——**多一个字段、
少一个字段都会导致整次检查失败**，这是最容易踩的坑，务必让实现和测试都覆盖"字段一个不多一个不少"）：

```jsonc
{
  "apiVersion": "1",                 // 必须字面量 "1"
  "contentHash": "<原样回显请求里的 contentHash>",  // 不一致会被 Urmotiv 客户端直接拒绝
  "checkedAt": "2026-07-26T00:00:00.000Z",  // 必须是 UTC、以字面量 "Z" 结尾的 ISO 8601；
                                             // Urmotiv 用 zod 的 datetime() 默认选项校验，
                                             // 不接受 +08:00 这类带时区偏移的写法
  "candidates": [                    // 最多 50 条
    {
      "source": "string，1-80 字符",         // 建议：候选题所在的 OJ 名称，如 "Codeforces"
      "externalId": "string，1-200 字符",     // 建议：yuantiji 的 uid，形如 "来源/题号"
      "title": "string，1-200 字符",
      "url": "合法 URL，可省略",
      "similarity": 0.82,                    // 0 到 1 之间的有限数，不能是 NaN/Infinity/负数
      "sameProblemSuggestion": true,          // 可选：LLM 复核结论，没复核就不要发这个字段
      "explanation": "string，1-2000 字符，可选"  // 可选：LLM 复核的理由
    }
  ],
  "recommendation": {
    "blockSubmission": false,
    "message": "string，1-2000 字符"          // 必填，即使不拦截也要给出人类可读的说明
  }
}
```

**响应体积上限 2MB**（Urmotiv 客户端按流式读取并在超限时直接中止），50 条候选、每条候选的字段
长度上限都很小，正常实现不会超限，但如果未来往候选里塞了题面原文之类的大字段，会触发这个限制，
所以从一开始就不要把候选题的正文塞进响应。

**Urmotiv 客户端的校验行为**（决定 Anklang 出错时会发生什么）：

- 非 2xx 响应：视为请求失败，交给 Urmotiv 侧配置的 `failureBehavior`（`block` 或 `continue`，
  默认 `block`，也就是**默认会阻止提交**）处理。
- 2xx 但 JSON 解析失败、或不满足 `anklangResultSchema`、或 `contentHash` 对不上：同样视为失败。
- 整个请求受 Urmotiv 侧配置的 `timeoutMs` 限制（默认 30000 毫秒，范围 1000-120000）。超时后
  Urmotiv 直接放弃这次调用，Anklang 即使之后算完了也没用。

**设计含义**：因为默认行为是"失败就阻止提交"，而 yuantiji.ac 是第三方公开服务、没有 SLA（服务等级
承诺），Anklang **应当优先返回"降级但合法"的 200 响应**（例如 candidates 为空、message 说明"本次
未能完成外部检索，建议人工复核"），而不是让请求整体失败——只有请求本身不合法（鉴权失败、
`apiVersion` 不对、JSON 解不出来）时才返回 4xx/5xx。这样 Urmotiv 侧至少能看到一句人话说明，而不是
一次不明所以的失败。

**鉴权**：`Authorization: Bearer <token>` 是否校验由 Anklang 自己的配置决定（见 2.6 节
`ANKLANG_API_TOKEN`）；配置了就必须校验（建议用常数时间比较，避免时间侧信道泄露 token），没配置
就不检查这个头。

**Anklang 自己的健康检查端点**（不属于 Urmotiv 契约，是运维需要）：建议额外提供
`GET /api/v1/health`，返回自身状态（例如是否能连通 yuantiji.ac、LLM 复核是否启用），供 Docker
健康检查和人工排查使用。这个端点与 Urmotiv 客户端无关，不要和上面的契约混淆。

### 2.3 yuantiji.ac 接口侦查结果

以下内容来自两类证据：(a) 直接用 `curl` 请求 `yuantiji.ac` 得到的真实响应（已实测，非猜测）；
(b) 阅读该站点首页返回的内联 JavaScript 源码逆向得到的请求/响应字段（源码可见，但站方未发布正式
接口文档，也没有版本号承诺）。**这不是一个官方稳定 API，只是当前可观察到的真实行为**，实现前后
都需要重新核对。

#### 2.3.1 `GET /api/health`（已实测，2026-07-26）

真实响应示例：

```json
{
  "ok": true,
  "backend": "cloud",
  "emb": "gemini",
  "problems": 254940,
  "rerank": true,
  "rewrite": true,
  "models": {
    "embed": "google/gemini-embedding-001",
    "rewrite": "google/gemma-3-12b-it",
    "rerank": "Qwen/Qwen3-Reranker-8B"
  }
}
```

响应头显示 `Server: uvicorn`、`Via: 1.1 Caddy`，与 `is-my-problem-new` v2 分支 README 描述的
"FastAPI 单文件服务 + Caddy 反代"架构完全吻合，确认线上跑的就是 v2 分支的代码。

#### 2.3.2 `POST /api/search`（来自前端源码逆向，未做真实调用验证）

前端发起请求的代码（简化摘录自首页内联 `<script>`）：

```js
const r = await fetch("/api/search", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    query: q.value.trim(),   // 题面文本
    k: 100,                  // 期望返回条数
    rewrite: true,           // 是否先用 LLM 重写/简化题面再检索
    skip_short: true,        // 是否跳过过短的题面
    sources: undefined,      // 可选：限定 OJ 来源列表
    rerank: false            // 是否用 Qwen3-Reranker 对前 100 名重新排序
  })
});
// 422：query 超过 16000 字符，或参数不合法
```

从结果渲染逻辑反推出的 `results[]` 每项字段：

| 字段 | 含义 |
| --- | --- |
| `uid` | 形如 `"来源/题号"` 的字符串，在 yuantiji 内保证唯一 |
| `title` | 题目标题 |
| `url` | 指向原始 OJ 的完整链接 |
| `src` | OJ 名称，如 `"Codeforces"` |
| `cos` | 余弦相似度（embedding 检索得到的相似度分数，0 到 1 附近，理论上可能出现很小的负值） |
| `rr` | 重排分数，只有请求时 `rerank: true` 才会有 |
| `base_rank` | 重排前的原始名次，用于展示"名次变化" |
| `also` | 数组，`{url, uid}`，表示这道题在其他 OJ 上的重复项 |
| `original` / `t0` / `t1` | 原始题面与两个版本的 LLM 重写文本，用于展示片段 |

响应还包含 `rewrites`（重写文本）、`timing`（各阶段耗时）、`cost`（本次调用花费）、`src_counts`
（按 OJ 统计的匹配数，用于前端的筛选面板）。

该站点没有 `robots.txt`（请求返回 404），没有看到显式的速率限制响应头，`OPTIONS /api/search`
返回 405（没有为浏览器跨域配置预检），说明这个接口是设计给同源网页前端用的，不是对外发布的
服务器对服务器 API。

#### 2.3.3 候选字段到 Anklang 契约的映射

| Anklang `candidates[]` 字段 | 取值来源 | 说明 |
| --- | --- | --- |
| `source` | yuantiji `d.src` | 直接使用 OJ 名称 |
| `externalId` | yuantiji `d.uid` | 已经是"来源/题号"格式，天然唯一 |
| `title` | yuantiji `d.title` | 裁剪到 200 字符 |
| `url` | yuantiji `d.url` | 先校验是合法 URL，不合法就不发这个字段（它是 optional） |
| `similarity` | 优先 `d.rr`（如果请求时开了 `rerank`），否则 `d.cos` | 必须 clamp 到 `[0, 1]` 区间再发送 |
| `sameProblemSuggestion` | LLM 复核结果 | 只有进入复核的前 N 条候选才有；未复核的候选不要发这个字段 |
| `explanation` | LLM 复核结果 | 同上，裁剪到 2000 字符 |

`also`（同一题在其他 OJ 上的重复项）**不在 Anklang 契约字段范围内**，是否要利用这条信息（例如平铺
成额外候选，或合并进 `explanation` 文字里）是产品判断，留给接手人决定，见第 6 节的待确认清单。

### 2.4 需上线前确认的点

1. **`/api/search` 的字段需要一次真实调用二次确认。** 上面的字段来自阅读公开前端源码，不是官方
   文档，可能已经过时或将来变化。实现前必须真实调用一次（用简短、无敏感内容的题面文本）核对字段
   名和结构，并且实现要对"字段缺失/类型不对"保持容错（跳过该候选，不要整体报错）。
2. **是否需要联系 yuantiji.ac 维护者。** 该站点由 `@TLE`（GitHub: fjzzq2002）个人自费维护，页面
   "关于"面板明确写了服务器成本自付、接受捐赠，且没有发布面向程序化调用的服务条款。把它当作
   Urmotiv 的常驻查重后端，意味着 Urmotiv 每次提交都会消耗对方的真实 LLM/embedding 调用费用。
   **建议在正式接入前通过其 GitHub 或页面提到的 QQ 群联系维护者，说明用途和预期调用量**，这既是
   避免被当作滥用流量封禁的现实需要，也是同为竞赛社区项目之间的基本尊重。这是需要人决策的事情，
   不是技术问题，本规划不能替接手人做这个决定。
3. **相似度阈值不能凭空定。** `cos`/`rr` 的数值分布目前没有已知基准（上游消融实验只报告了召回率
   指标，没有给出面向"多高的 cos 算作雷同"的标注数据）。本规划的默认设计是**把是否拦截的判断交给
   LLM 复核的结论，而不是原始相似度数字**（见 2.6 节），避免拍脑袋定一个阈值。如果确实需要"纯相似度
   自动拦截"作为无 LLM 场景的兜底，必须先用人工标注的已知重复题对做校准，默认关闭。
4. **`also` 字段怎么用是产品判断**，见上一节末尾，需要接手人拍板。
5. **合规判断**：yuantiji.ac 对 vjudge/AtCoder/QOJ 等内容的展示是否处于灰色地带，是上游项目自己
   的合规问题；Anklang 作为下游调用方只做"链接 + 相似度 + 摘要片段"展示，不转存对方全文，风险应
   可控，但建议协会内部如有法务/顾问流程，还是过一遍。

### 2.5 目录结构

```text
Anklang/
  AGENTS.md
  README.md
  .gitignore
  .env.example              # 阶段 1 起提供，只列字段名，不含真实值
  pyproject.toml            # 或 requirements.txt，阶段 1 实现时创建
  compose.yaml              # 阶段 1 实现时创建，可独立 docker compose up 验证
  docs/
    plan.md                 # 本文档
  app/
    main.py                 # FastAPI 入口，挂载 /api/v1/checks/similarity 和 /api/v1/health
    config.py               # 环境变量读取与校验（建议 pydantic-settings）
    schemas.py              # anklangRequestSchema / anklangResultSchema 对应的 Pydantic 模型
    auth.py                 # Bearer token 校验
    cache.py                # 按 contentHash 的结果缓存
    sources/
      yuantiji/
        client.py           # 封装 GET /api/health、POST /api/search，含超时与重试
        mapper.py           # yuantiji 结果 -> Anklang candidates 映射（2.3.3 节的表）
    recheck/
      llm_client.py         # OpenAI 兼容 Chat Completions 封装
      prompts.py            # 复核用的提示词模板
  tests/
    fixtures/
      urmotiv_request_sample.json   # 从 plugins/anklang/test/anklang.test.ts copy
      urmotiv_result_sample.json
      yuantiji_search_sample.json   # 真实调用一次后固化下来的样例（脱敏，不含敏感内容）
    test_contract.py        # 校验响应永远满足 anklangResultSchema
    test_yuantiji_mapper.py
    test_llm_recheck.py
    test_cache.py
    test_auth.py
```

### 2.6 请求处理流程与模块职责

1. **校验请求**：`apiVersion` 字面量、必填字段是否存在、鉴权头（若配置了 `ANKLANG_API_TOKEN`）。
   不合法直接 4xx。
2. **查缓存**（`cache.py`）：按 `contentHash` 查 Anklang 自己的本地缓存。命中且未过期：直接返回，
   但**建议把 `checkedAt` 刷新为本次实际返回的时间**（不是首次生成的时间），因为这个字段语义上
   表示"这次检查完成的时间"；是否刷新最终由实现者决定，两种做法都要在代码注释里说明选择的理由。
3. **未命中缓存**：调用 `sources/yuantiji/client.py`：
   - 先看 `/api/health` 的缓存结果（不必每次都查健康检查，可以按分钟级缓存），决定是否请求
     `rerank: true`。
   - 若 `problem.basicStatement` 超过 yuantiji 的 16000 字符查询上限，需要先截断（建议保留题面
     开头到接近上限处，因为大多数信息量集中在描述和输入输出格式部分）。
   - 调用 `POST /api/search`，超时时间独立配置（见 2.7 节 `YUANTIJI_TIMEOUT_MS`），不能占满
     Urmotiv 侧给的整个预算。
4. **映射候选**（`sources/yuantiji/mapper.py`）：按 2.3.3 节的表转换字段，按 `similarity` 降序
   排列，裁剪到 50 条以内。
5. **LLM 复核**（`recheck/llm_client.py`，若 `RECHECK_ENABLED=true`）：对排序后最靠前的 N 个
   候选（默认 5，见 2.7 节）**并发**调用 LLM，写回 `sameProblemSuggestion` / `explanation`。
6. **计算 `recommendation`**：
   - `blockSubmission = true`，当且仅当被复核的候选中有任意一条 `sameProblem == true`；未启用
     复核时默认不自动拦截（除非显式开启并校准过的纯相似度阈值，见 2.4 节第 3 点）。
   - `message`：拦截时给出"在 {source} 发现疑似同一道题：《{title}》，模型判断：{explanation}"
     一类的人话说明；不拦截时给出候选数量和最高相似度的摘要。
7. **写入缓存**，TTL 建议略短于 Urmotiv 侧默认的 1440 分钟（例如 720 分钟），避免"上游数据已经
   更新，但两层缓存都还在用旧结果"叠加太久。
8. **返回 200**，响应体严格符合 `anklangResultSchema`（字段一个不多一个不少）。
9. **任何步骤出错**（上游超时/限流/解析失败/LLM 失败）：不要让整个请求 500。按 2.2 节末尾的
   "设计含义"降级处理，只有请求本身不合法才返回 4xx。

### 2.7 LLM 复核设计

- **配置**（面向"OpenAI 兼容"接口，即遵循 OpenAI Chat Completions 请求/响应格式的服务，覆盖
  OpenAI、DeepSeek、Moonshot/Kimi、阿里云百炼兼容模式、自建 vLLM/Ollama 等）：
  `RECHECK_ENABLED`、`RECHECK_BASE_URL`、`RECHECK_API_KEY`、`RECHECK_MODEL`、
  `RECHECK_TOP_N`（默认 5）、`RECHECK_TIMEOUT_MS`（默认 8000，且必须并发发出，不能串行等待）、
  `RECHECK_MAX_RETRIES`（默认 1）。
- **提示词固定模板**，要求模型只输出严格 JSON：`{"sameProblem": boolean, "explanation": string}`。
  优先使用目标模型支持的结构化输出（`response_format` 为 `json_object` 或 `json_schema`）；解析
  失败时该候选的 `sameProblemSuggestion`/`explanation` 留空（不发送这两个字段），不影响其他候选
  和主流程。
- **成本与预算**：`RECHECK_TOP_N=5` 时一次查重最多触发 5 次并发 LLM 调用；必须能通过
  `RECHECK_ENABLED=false` 整体关掉，给低预算部署留活口。
- **绝不在日志里记录完整题面**：调试日志只能包含 `contentHash`、候选的 `externalId`、是否解析
  成功、耗时，不能包含 `basicStatement` 或候选题面原文（呼应 `AGENTS.md` 的安全红线）。

### 2.8 缓存设计

- 阶段 1 建议用 **SQLite 单文件**（`cache/anklang-cache.db`），表结构大致为
  `(content_hash TEXT PRIMARY KEY, result_json TEXT, checked_at TEXT, expires_at TEXT)`。
- 不用 Redis 的理由：阶段 1 单实例部署已经够用，少一个外部依赖更容易独立运行和测试；如果将来
  多副本部署需要共享缓存，再评估接入 Redis（Urmotiv 自己的 `compose.yaml` 里已经有一个 Redis
  容器，如果部署时选择让 Anklang 复用同一个 Redis 实例，需要用独立的 key 前缀区分，这个决定留到
  部署阶段，不在阶段 1 纠结）。

### 2.9 配置项清单（`.env`）

```dotenv
# Anklang 自身
ANKLANG_API_TOKEN=              # 可选；配置后校验 Authorization: Bearer <token>
ANKLANG_HTTP_PORT=8090

# yuantiji.ac 反向代理
YUANTIJI_BASE_URL=https://yuantiji.ac
YUANTIJI_TIMEOUT_MS=15000
YUANTIJI_USER_AGENT=Anklang/0.1 (contact: 填写联系方式，方便对方在异常时联系到我们)

# LLM 复核（OpenAI 兼容）
RECHECK_ENABLED=false
RECHECK_BASE_URL=
RECHECK_API_KEY=
RECHECK_MODEL=
RECHECK_TOP_N=5
RECHECK_TIMEOUT_MS=8000
RECHECK_MAX_RETRIES=1

# 缓存
CACHE_PATH=cache/anklang-cache.db
CACHE_DEFAULT_TTL_MINUTES=720

# 日志
LOG_LEVEL=info
```

以上只列字段名、默认值和用途说明，**不包含真实密钥**；真实值只放在部署环境自己的 `.env`
（已被 `.gitignore` 排除），与 Urmotiv 的 `.env.example` 惯例一致。

### 2.10 Docker 部署

- Anklang 自己在仓库根目录提供一份 `compose.yaml`（阶段 1 实现时创建），一个 `anklang` 服务，
  基于 `python:3.11-slim` 构建，暴露 `ANKLANG_HTTP_PORT`，健康检查请求自己的 `/api/v1/health`
  （不是 yuantiji.ac 的）。这样接手人可以在不依赖 Urmotiv 仓库的情况下单独 `docker compose up`
  验证 Anklang。

- **与 Urmotiv compose 的衔接**：Urmotiv 的 `compose.yaml`（`E:\Huasushis\program\Urmotiv\compose.yaml`）
  目前只有 `postgres`、`redis`、`minio`、`migrate`、`api`、`worker`、`web` 七个服务，还没有
  Anklang / Fermata 的 profile（Docker Compose 的 "profile" 是一种给服务打标签、默认不启动、
  只有显式指定 `--profile <名字>` 才会启动该服务的机制）。这符合 Urmotiv `docs/spec.md` 第 12 节
  "Docker Compose 提供基础服务与可选的 Anklang、Fermata 组合配置"这个既定计划，但**目前尚未实现**。

  给 Urmotiv 侧接手人的建议片段（**这段改动属于 Urmotiv 仓库，不在 Anklang 仓库执行**，这里只给
  参考，方便复制）：

  ```yaml
  # 建议追加到 Urmotiv 的 compose.yaml 的 services 下（仅供参考，不在本仓库生效）
  anklang:
    profiles: ["anklang"]
    build:
      context: ../Anklang
    restart: unless-stopped
    environment:
      ANKLANG_API_TOKEN: ${ANKLANG_API_TOKEN:?请在私有环境文件中设置 ANKLANG_API_TOKEN}
      YUANTIJI_BASE_URL: ${YUANTIJI_BASE_URL:-https://yuantiji.ac}
    ports:
      - "127.0.0.1:${ANKLANG_HTTP_PORT:-8090}:8090"
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/api/v1/health').status==200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 10s
  ```

  Urmotiv 侧 `plugins/anklang` 插件设置里的 `baseUrl` 配置为这个内部服务地址即可
  （例如 `http://anklang:8090`，与 Docker Compose 内部服务发现的命名一致）。

### 2.11 测试清单

- **契约测试**：固定 fixture 校验 Anklang 的输出永远满足 `anklangResultSchema`（字段一个不多一个
  不少、`apiVersion` 字面量、`contentHash` 正则、`candidates` ≤ 50、响应体积 ≤ 2MB、`checkedAt`
  是以 `Z` 结尾的 UTC 时间）。
- **yuantiji 映射测试**：给定一份固化的 `/api/search` 示例响应，断言 candidates 映射正确、排序
  正确、裁剪正确、`similarity` 已 clamp 到 `[0,1]`。
- **降级路径测试**：yuantiji 超时/500/返回畸形 JSON 时，Anklang 返回的是 200 + 空 candidates +
  合理 `message`，而不是让整个请求失败。
- **鉴权测试**：无 token、错误 token、正确 token 三种情况。
- **缓存测试**：命中、未命中、过期三种情况。
- **LLM 复核测试**：mock 掉真实 API，验证 JSON 解析成功/失败两条路径都不影响主流程；
  `RECHECK_ENABLED=false` 时完全不发起调用。
- 以上测试都**不产生真实网络调用**（mock yuantiji 与 LLM）。可以另外准备一个标记为
  `@pytest.mark.external`、默认跳过、需要人手动触发的集成测试，用于真实核对 yuantiji.ac 的字段
  （对应 2.4 节第 1 点），运行时必须克制调用频率。

---

## 3. 阶段 2：本地检索引擎

### 3.1 目标

不再依赖 yuantiji.ac，自建题库索引与向量检索（"向量检索"：把每道题的文字转换成一串数字组成的
"向量"，两道题越相似，它们的向量在数学上就越"靠近"，通过比较向量距离找出相似题目）。这一阶段
是阶段 1 的补充而不是替代——两个来源的结果可以合并展示，`source` 字段区分 `"yuantiji"` 和自建
来源的名字，阶段 1 的反向代理仍可以作为兜底或对照。

### 3.2 存储选型：SQLite 起步，Postgres 是明确的升级路径

| 维度 | SQLite（阶段 2 默认） | Postgres（+ pgvector 插件） |
| --- | --- | --- |
| 部署复杂度 | 单文件，无需独立数据库服务，容易独立运行和测试 | 需要一个数据库服务进程 |
| 与 Urmotiv 的关系 | 完全独立，符合"不共享数据库"的定位 | 即使用 Postgres，也必须是 Anklang 自己独立的实例/数据库，绝不能连到 Urmotiv 的 Postgres |
| 量级 | 到几十万题级别（与 yuantiji.ac 当前 25.5 万题同量级）足够 | 更大规模、更高并发写入时更合适 |
| 并发写入 | 单写者友好，多进程并发写容易冲突 | 原生支持多连接并发写入 |

**结论**：阶段 2 默认用 SQLite，元数据（标题、来源、链接、规范化后的题面文本）和向量数据都可以先
放在本地文件里，符合 `is-my-problem-new` 本身"single-file server"的思路，也让 Anklang 保持
"可以整个目录打包带走"的简单形态。触发迁移到 Postgres + pgvector 的信号（满足任一条即应重新评估）：

1. 阶段 3 引入多个源插件后，出现频繁的并发写入冲突；
2. 向量索引大到单机内存放不下；
3. 需要多副本同时提供读服务。

### 3.3 元数据结构草案

```text
problems 表：
  uid              主键，形如 "来源/题号"
  source           来源标识（yuantiji / 某个源插件名）
  external_id      来源内部的题目编号
  title
  url
  statement_normalized   规范化后的题面文本（用于展示片段和关键词检索）
  statement_kind         规范化来源（html / pdf_text / ocr / attachment）
  content_sha256         去重用的原文哈希
  fetched_at
```

向量数据可以另存为旁路的向量索引文件，不必都塞进 SQLite 的一张表里。

### 3.4 Embedding：阿里云百炼（DashScope）text-embedding

- **模型**：`text-embedding-v4`，支持自定义向量维度。
- **价格**（2026-07 查得，**上线前请以
  [阿里云百炼模型价格页](https://help.aliyun.com/zh/model-studio/model-pricing) 为准，价格会变**）：
  同步接口约 ¥0.0006/千 Token 输入，无输出费用；批量（异步）接口约 ¥0.0007/千 Token，价格与同步
  接口基本一致。
- **批量策略**：
  - **同步接口**：一次最多 10 条文本、单条最多 8192 Token，适合"新题实时入库"这种小批量场景
    （对应阶段 3 的增量抓取）。
  - **批处理（异步）接口**：一次最多 10 万行、单行最多 2048 Token、文件不超过 200MB；同一账号
    最多 3 个批任务同时跑、最多排队 50 个、提交速率限制 1 次/秒；结果在 24 小时内可取，逾期自动
    删除，需要及时下载。适合"第一次建库/重建索引"这种一次性处理几十万题的场景。
  - **成本估算示例**（仅示例，不是承诺）：假设收录 30 万题，规范化后每题平均约 500 Token，
    总量 1.5 亿 Token，按 ¥0.0006/千 Token 计约 ¥90。如果参照上游项目的做法给每道题生成多个
    LLM 重写版本再分别 embedding（见下一条），这个数字要相应乘倍。
  - 阿里云百炼通常对新账号提供免费额度（查得为 90 天内 2000 万 Token，**具体额度以百炼官方账户
    页面为准**），适合先跑通流程再评估正式成本。
- **是否需要"多版本重写再 embedding"**：`is-my-problem-new` 的做法是给每道题生成 4 个 LLM 重写
  版本（2 种模板 × 2 种采样温度），分别 embedding 后取向量质心，用来抹平"同一道题不同措辞"的
  差异。这个做法有效，但会让 embedding 成本乘以 4，且需要额外一层重写用的 LLM 调用成本。
  **建议阶段 2 先只做"直接 embedding 规范化后的原始题面"，不做多重写版本**，用一个小规模的
  标注测试集（人工确认的已知重复题对）量化对比"加多重写版本"能带来多少召回率提升，值得再决定是否
  上马这个复杂度。这是第 6 节里明确列出的待确认项，不能在没有数据的情况下直接照抄上游的做法。

### 3.5 相似度计算：向量 + 关键词混合

- **向量召回**（embedding 余弦相似度）作为主要召回手段。
- **关键词/全文检索**（可以用 SQLite 自带的 FTS5 全文检索扩展，或简单的 BM25 算法：一种衡量
  关键词匹配程度的经典算法）作为补充，用来捕捉"整段题面几乎逐字照抄，但因为翻译或轻微改写导致
  向量距离没那么近"的情况——这种"字面高度重合"的信号有时比语义向量更直接。
- **混合策略建议**：分别取向量召回的 Top-K1 和关键词召回的 Top-K2，取并集去重后，统一交给阶段 1
  已经实现的 LLM 复核环节做最终判断，**不要自己再设计一套复杂的分数融合公式**——这样阶段 1、2、3
  可以共用同一套"复核决定拦截与否"的逻辑，不必为每个来源重新发明一遍。

### 3.6 题面规范化：PDF → 文本解析规划

- 参照上游 `gen/` 流程的处理优先级：HTML 结构化提取 > PDF 文本层解析（可用 `PyMuPDF`）> 图片
  OCR（光学字符识别：把图片里的文字识别成机器可读文本）> 附件文档解析。每一步都要有"输出是否
  像正常文本"的检测（控制字符、编码异常、内容过短），不可靠就自动降级到下一优先级，或标记为
  "无法自动处理，需要人工"。
- 题面中的数学公式（LaTeX/图片公式）在 OCR 之后容易出现乱码，上游为此做了专门的公式 OCR 处理。
  阶段 2 可以先接受"公式识别质量较低，退回展示原始图片链接"这种妥协，不必一开始就复刻上游全部
  工程量。

### 3.7 目录结构（在阶段 1 基础上新增）

```text
  app/
    sources/
      local_index/
        store.py          # SQLite 元数据 + 向量文件读写
        embed_client.py   # 阿里云百炼 text-embedding 封装（同步 + 批量两种模式）
        search.py         # 向量 + 关键词混合检索
        normalize.py      # HTML/PDF/OCR 规范化管线
  problems-data/           # 本地题库正文与索引文件所在目录，整体 .gitignore
    corpus/
    embeddings/
    cache/
```

### 3.8 配置项新增

```dotenv
LOCAL_INDEX_ENABLED=false
DASHSCOPE_API_KEY=
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
DASHSCOPE_EMBEDDING_DIM=1024
PROBLEMS_DATA_ROOT=problems-data
FTS_ENABLED=true
```

### 3.9 测试清单

- `normalize.py` 对已知格式样本（HTML、PDF 文本层、纯文本）的规范化输出做快照测试，**测试样例必须
  是自己编写的、无版权问题的示例文本，不能用真实抓取到的第三方题面作为测试快照**（这一条尤其
  重要，避免版权风险混进仓库的测试数据）。
- embedding 客户端：mock 掉 DashScope 调用，测试批量分片逻辑（超过 10 条自动分批、超过单条 Token
  上限的处理）、重试与限流退避。
- 混合检索：构造小型已知语料，验证向量召回、关键词召回、去重合并逻辑。
- 存储演进说明：即使阶段 2 不是最终形态，也要在文档里写清楚"如果以后换成 Postgres，现有 SQLite
  数据怎么迁移"的粗略预案，不需要现在就写迁移代码。

---

## 4. 阶段 3：源插件体系

### 4.1 设计

- `sources/<name>/` 每个目录对应一个题目来源，统一接口约定（以下是接口约定，不是需要现在实现的
  代码）：
  - `fetch_new_problems(since: datetime) -> Iterable[NormalizedProblem]`
  - `NormalizedProblem` 至少包含：`uid`、`title`、`url`、`source`、`statement`、
    `statement_kind`、`fetched_at`、`raw_ref`（指向原始抓取产物存放位置，便于排查问题，不代表
    要长期保留大文件）。
  - `since` 参数由每个源自己决定怎么使用（有的源有更新时间戳，有的只能整表扫一遍做 diff），
    接口只约定输入输出，不约定内部实现方式。
- 每个源插件的中间数据（分页游标、去重指纹、限流状态等）只存在自己的子目录/自己的数据库表
  命名空间里，不与其他源共享——这个原则和 Urmotiv 自己的插件规范（`docs/plugins.md` 第 5 节
  "插件如需保存数据，必须声明独立数据库命名空间"）是同一治理思路，虽然 Anklang 是独立服务，
  沿用同一套思路方便未来维护者理解，不需要发明新规则。

### 4.2 vjudge 源：为什么暂缓

- **规模**：vjudge 聚合了约 25.5 万题（即 yuantiji.ac 当前索引规模），自建同等规模的抓取与存储
  是重量级工程，不是"顺手加一个源"的量级。
- **上游项目明确不提供 vjudge 数据和爬虫脚本，官方说法是"版权原因"**——这是一个明确信号：即使
  有能力抓取，也有人认为直接分发这类数据或工具本身存在版权顾虑。Anklang 复刻这件事需要独立
  评估，不能因为"上游内部显然做得到"就假设"我们做也没问题"。
- **真正要做时必须规划的成本项**（不是危言耸听，是接手人必须正视的现实）：
  1. vjudge 本身是"聚合"，背后是几十个不同的 OJ，每个 OJ 的页面结构、登录方式、频率限制都不同，
     爬虫要针对每个源分别维护；
  2. 很多 OJ 需要注册账号才能看到完整题面，规模化抓取意味着需要批量建号的"注册机"，这类行为对
     多数 OJ 的服务条款而言是灰色地带甚至被明确禁止；
  3. 单 IP 大量请求会被封锁，规模化通常牵涉 IP 代理池，这进一步增加了"看起来像滥用"的观感和实际
     的运营成本；
  4. 抓取内容如果涉及托管或再分发，版权风险比"只运行一个内部使用的相似度索引"更高；即便只是
     内部使用，留存来源网站内容的合理使用边界也需要法律判断，不是工程判断；
  5. 数据体量（25 万余题，含图片/PDF 附件）带来的存储和长期维护成本不小。
- **真正实施时的要点建议**（留给未来真正做这件事的人）：
  - 优先接入有官方公开 API 或明确允许程序化访问的来源（例如 Codeforces 提供官方 API 可以拿到
    题目列表和部分元数据），把"透明、被允许的直连来源"作为第一优先级，而不是重做一个 vjudge
    规模的通用聚合爬虫；
  - 每接入一个新来源都要单独评估其服务条款，写进该来源自己目录下的 README，不能笼统假设
    "抓取方式都一样";
  - 需要账号池/代理池的来源，账号池凭据全部走 `.env`/密钥管理，绝不入库；自行编写的抓取代码
    不进公开仓库（详见 `AGENTS.md` 的安全红线）；
  - 存储和抓取速率必须显式限流、可配置暂停，避免被目标站点封禁。
- **结论**：阶段 3 先实现源插件的接口约定与至少一个"温和"的示例来源（走官方 API、而非爬虫），
  vjudge 规模的通用聚合抓取作为独立的、需要单独立项评估的未来工作，**不纳入本规划的验收范围**。

### 4.3 目录结构

```text
  sources/
    codeforces_api/        # 示例：走官方 API 的温和来源，仅用于验证接口约定，不代表已完整实现
      fetch.py
      README.md            # 该来源自己的服务条款说明、限速策略
    vjudge/                 # 占位，暂不实现
      README.md            # 说明暂缓原因（即本节内容）与真正实施前的前置条件
```

### 4.4 配置与测试清单

- 每个来源自己的开关（`SOURCE_<NAME>_ENABLED`）、限速参数、凭据变量名（只列名字，不含真实值）。
- 测试：每个来源的 `fetch_new_problems` 针对录制好的样例响应（fixture，不是真实抓取）做单元测试；
  增量游标的幂等性测试（重复调用不重复入库）。

---

## 5. 许可证合规

- **Anklang 仓库本身**：MIT License，`Copyright (c) 2026 Huasushis`（已核实，见仓库根目录
  `LICENSE` 文件，与本规划撰写时的仓库内容一致）。
- **上游参考项目 `is-my-problem-new`**：MIT License，`Copyright (c) 2023 Ziqian Zhong`
  （已核实，`main` 与 `v2` 两个分支都是 MIT）。MIT 是一种非常宽松的开源许可证：允许自由使用、
  复制、修改、合并、出版、分发、再授权乃至出售，**唯一强制要求是"在软件的全部副本或主要部分中
  保留上述版权声明和本许可声明"**，并且软件按"原样"提供、不附带任何担保。
  - UI 使用的 IBM Plex Sans/Mono 字体另遵循 SIL Open Font License 1.1（仅当 Anklang 也做一个
    面向最终用户的独立网页界面、并且直接复用这套字体资源时才相关；如果 Anklang 不做独立 UI，
    可以暂不涉及这一条）。
- **复用或改编 `is-my-problem-new` 代码时必须**：
  1. 在被复用代码所在目录保留一份其 LICENSE 文件副本（例如
     `app/sources/local_index/THIRD_PARTY_LICENSE`），并在对应文件头部注明
     "改编自 is-my-problem-new（MIT），Copyright (c) 2023 Ziqian Zhong"；
  2. 不移除、不掩盖原始版权声明；
  3. MIT 允许修改和闭源使用，保留声明是唯一强制要求，但**不代表上游作者同意被关联到 Anklang
     项目**——如果要公开提及"参考了某开源项目"，用事实性描述，不暗示合作或背书关系。
- **不能视为"复用"的部分**：
  - vjudge 数据集本身，上游明确不提供，Anklang 也不能想办法从别处获取后当作"复用上游"处理；
  - 上游代码里调用 Together/Voyage/Gemini 等特定商业 API 的部分可以参考实现思路，但 API 凭据、
    账号都必须是 Anklang 自己申请的配置，不能假设可以蹭上游的额度或密钥。

---

## 6. 需上线前确认的问题汇总

前面各阶段分散提到的"需要人决策、无法只靠查资料确认"的事项，汇总在这里方便一次看完：

1. **`POST /api/search` 的请求/响应字段需要一次真实调用二次确认**（第 2.4 节第 1 点）——本规划
   给出的字段来自阅读公开前端源码，不是官方文档。
2. **是否需要联系 yuantiji.ac 维护者取得使用许可、告知预期调用量**（第 2.4 节第 2 点）——这是
   对个人自费维护的第三方服务的基本尊重，也是避免被封禁的现实需要。
3. **相似度阈值与是否启用"纯相似度自动拦截"需要标注数据校准，默认不开启**（第 2.4 节第 3 点、
   第 3.4 节末尾）。
4. **yuantiji 的 `also`（同题多平台重复）字段怎么利用是产品判断**（第 2.3.3 节末尾），需要接手人
   拍板是否平铺为独立候选、合并进说明文字，还是暂不处理。
5. **阶段 2 是否需要"多版本 LLM 重写再 embedding"，需要用小规模标注集测试召回率提升是否值得
   对应的成本倍增**（第 3.4 节）。
6. **阶段 3 vjudge 源是否/何时真正实施，需要协会内部先做法律与运营评估，不是纯技术决定**
   （第 4.2 节）。
7. **Anklang 与 Urmotiv compose 的最终衔接方式**（profile、`include`，还是完全独立部署）由
   Urmotiv 仓库维护者决定，本文档只给出参考片段（第 2.10 节）。
8. **缓存命中时是否刷新 `checkedAt`**（第 2.6 节第 2 步）——本规划给出建议但不强制。
