# Anklang 当前实现计划与验收边界

状态：当前范围已经收敛。本文件替代此前关于反向代理、LLM 复核、标定、流程采集和审核政策的计划；这些内容已废止，不能作为当前或后续验收要求。

## 1. 产品职责

Anklang 只做原题检索：

```text
查询题面 -> 可选 embedding -> 向量/关键词召回 -> 相似度降序候选
```

返回结果是检索事实，不是产品判断。Anklang 不判断同题、不判断是否可参考、不决定通过或拦截，也不保存提交查询结果。

重复和参考信息由 Urmotiv 管理。Fermata 是独立审题服务，只能读取 Urmotiv 的带版本号 HTTP 接口；Fermata 和 Anklang 不互调、不共享数据库。Anklang 不提供 yuantiji 反向代理运行时。

历史 32/32 标定仅记录已废止的实验路径，不证明当前查询服务的正确性，也不属于当前验收。

## 2. 上游保留与有限改动

上游 [is-my-problem-new](https://github.com/fjzzq2002/is-my-problem-new) 的核心入口和数据流：

1. 接收题面查询；
2. 用 embedding 模型生成查询向量；
3. 对题库向量计算余弦相似度；
4. 按相似度返回候选。

Anklang 的直接对应：

| 上游职责 | Anklang | 差异 |
| --- | --- | --- |
| embedding 客户端 | `anklang/embedding.py` | 改为环境变量配置的百炼 OpenAI 兼容接口 |
| 查询和向量检索 | `anklang/server.py`、`anklang/backends/local_engine.py` | 加版本化机器接口，补关键词召回 |
| 题库索引 | `anklang/store.py` | 改为可增量写入的 SQLite 当前快照 |
| 题目来源 | `anklang/sources/`、`anklang/ingest.py` | 插件发现、独立游标、幂等更新 |

MIT 许可证和上游作者 Ziqian Zhong 的版权声明保留在根目录 `LICENSE`。

## 3. 查询契约

### 请求

`POST /api/v1/checks/similarity` 与 `POST /api/v2/checks/similarity` 接收严格 JSON：

- `apiVersion` 必须与路径一致；
- `requestId` 是规范 UUID；
- `contentHash` 是 64 位小写十六进制；
- `problem` 只包含 `title`、`type`、`tagIds`、`basicStatement`。

### 候选

每条候选必填：

- `source`：来源名；
- `externalId`：来源内稳定题号；
- `title`：题目标题；
- `similarity`：`[0, 1]` 有限数。

`url` 是唯一可选字段。输出不允许出现来源摘录、内部错误、复核字段或流程建议。候选最多 50 条，并按相似度降序。

### 完整性

v1 只在完整查询时返回 200。v2 用 `completion.status` 区分：

- `complete`：配置范围内的查询完整完成；
- `partial`：仍有可用候选，但某条检索信号失败；
- `unavailable`：不能形成候选，候选数组必须为空。

非完整 v2 结果必须使用 `reuse.policy=no-store`。当前所有结果都使用 `no-store`，且 HTTP 发送 `Cache-Control: no-store`。

## 4. embedding 提供方

配置项：

- `DASHSCOPE_BASE_URL`；
- `DASHSCOPE_API_KEY`；
- `DASHSCOPE_EMBEDDING_MODEL`；
- `DASHSCOPE_EMBEDDING_DIM`。

只有 URL 和密钥同时存在才启用向量模式。请求使用 OpenAI 兼容的 `/embeddings` 路径。客户端必须验证：

- 响应大小和 JSON 结构；
- 服务端确认的模型与配置一致；
- 向量数量、顺序、有限数和维度正确；
- 错误信息不包含请求正文、密钥或原始响应。

未配置提供方是正常关键词模式。已配置提供方失败时，查询返回 `partial` 和关键词候选；增量入库仍写入无向量题目，使其立即可做关键词搜索。

## 5. 运行时增量来源

来源包位于 `anklang/sources/<name>/`，导出唯一 `SOURCE_NAME` 和 `fetch_new_problems(since)`。

每轮 `ingest_once()`：

1. 重新发现所有来源包；
2. 读取该来源当前 UTC 时间游标；
3. 校验全部返回记录后选择每个题号的明确最新版本；
4. 规范化题面并计算内容哈希；
5. 按需调用 embedding；
6. 以 `(source, external_id)` 幂等插入或更新；
7. 用比较交换推进游标，防止旧任务覆盖新进度。

同时间不同内容、无法判断先后的版本整组跳过，游标不前进。一个来源失败只增加固定计数，不阻断其他来源，也不输出异常内容。

服务打开 `ANKLANG_INGEST_ENABLED=true` 后，后台线程按 `ANKLANG_INGEST_INTERVAL_SECONDS` 调用同一入口。SQLite 查询不缓存题库快照；写入完成后，现有服务实例的下一次查询立即看到新增或更新题目。

## 6. 部署和隐私

- 默认监听 `127.0.0.1`；容器内监听 `0.0.0.0`，宿主仍只绑定回环地址；
- 生产环境必须要求至少 16 字符的服务令牌；
- 服务限制在途请求、客户端空闲时间和退出宽限；
- 进程不记录请求行、题面、外部响应或异常原文；
- `private/`、SQLite 题库、真实题面、密钥和模型原始响应不进入 Git；
- 测试不发起真实外部请求。

## 7. 验收门禁

全部门禁必须通过：

1. **上游对照**：文档和许可证能定位上游，代码保留查询向量、余弦搜索、候选排序主流程；
2. **查询契约**：v1/v2 都只有查询状态与排名候选，严格拒绝额外输出字段；
3. **embedding**：配置成功路径、响应校验和失败降级测试通过；
4. **增量来源**：新增、更新、立即可搜、幂等、同版本冲突和并发游标测试通过；
5. **静态边界**：运行时代码不存在代理、复核、标定、流程采集或跨服务耦合；
6. **完整验证**：单元测试、编译、Compose 配置、容器构建和容器部署测试通过；
7. **隐私与 Git**：无真实外部请求，无私有内容进入差异或暂存区，提交已推送且跟踪文件干净。