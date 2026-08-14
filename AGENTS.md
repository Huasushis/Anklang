# Anklang 开发约定

## 产品边界

Anklang 是公开项目 [is-my-problem-new](https://github.com/fjzzq2002/is-my-problem-new) 的小型直接改编。上游作者 Ziqian Zhong 的 MIT 版权声明必须保留在 `LICENSE` 中。

只维护一条数据流：题面查询 → 向量化与关键词检索 → 按相似度排序的候选。允许的本地扩展只有：

1. 用环境变量配置阿里云百炼兼容的 embedding（把文字转为向量）接口；
2. 来源插件在服务运行期间增量添加或更新可检索题目，不离线全量重建，不重启查询服务；
3. 用带版本号的 HTTP 查询接口供 Urmotiv 调用。

Anklang 不判断候选是否同题或可作参考，不给通过、拦截、审核建议，不调用 LLM 做复核，不做准确率标定或流程采集，不代理 yuantiji。重复/参考信息属于 Urmotiv；Fermata 只能通过带版本号的 Urmotiv HTTP 接口读取，不得与 Anklang 运行时互调或共享数据库。

只修改本仓库。不得为兼容旧范围恢复已删除的代理、复核、标定、缓存或采集路径。

## 当前结构

- `anklang/embedding.py`：OpenAI 兼容的百炼 embedding 客户端；校验模型、维度、顺序和响应大小。
- `anklang/backends/local_engine.py`：沿用上游“查询向量 → 余弦相似度 → 排序候选”的主流程，并补关键词召回。
- `anklang/store.py`：SQLite 单文件索引；查询每次读取当前快照，因此增量写入后立即可见。
- `anklang/sources/`、`anklang/ingest.py`：来源发现、规范时间游标、幂等写入、更新冲突保护。
- `anklang/contracts.py`、`anklang/server.py`：严格的查询入/候选出契约和 HTTP 服务。

## 接口不变量

- `POST /api/v1/checks/similarity`：完整查询成功才返回 200；结果字段严格为 `apiVersion`、`contentHash`、`checkedAt`、`candidates`。
- `POST /api/v2/checks/similarity`：始终显式返回 `completion` 和 `reuse`；结果仍只包含检索状态及候选，不包含产品判断。
- 候选字段严格为 `source`、`externalId`、`title`、`similarity`，可选 `url`。
- 候选按 `similarity` 降序；服务端只应用显示下限，不形成阈值政策。
- v2 非完整结果必须 `reuse.policy=no-store`；所有 HTTP 响应都发送 `Cache-Control: no-store`。
- 外部服务错误、题面、路径、密钥和原始响应不得进入 HTTP、日志或测试报告。

## 来源插件不变量

每个 `anklang/sources/<name>/` 包只导出：

```python
SOURCE_NAME: str
fetch_new_problems(since: str | None) -> list[RawProblem]
```

`SOURCE_NAME` 全局唯一。`updated_at` 使用带毫秒的 UTC `Z` 时间。`(source, external_id)` 是稳定主键。同时间但内容不同的重复版本必须整组跳过且不推进游标；较旧任务不得覆盖新游标。单个来源失败不能阻断其他来源。

运行时增量抓取由 `ANKLANG_INGEST_ENABLED=true` 开启；每轮重新发现来源并调用同一个 `ingest_once()`。题目写入后，下一个查询直接读取新快照，无需重建或重启。

## embedding 不变量

只有同时配置 `DASHSCOPE_BASE_URL` 和 `DASHSCOPE_API_KEY` 才启用向量检索；否则是正常的关键词模式。已配置提供方失败时返回明确的部分结果，不能伪装成完整空结果。索引元数据必须绑定模型和维度；身份冲突不得覆盖现有向量。

测试不得发起真实外部请求。embedding 测试使用注入的 opener；HTTP 测试只连接回环地址。

## 验证

在 `Anklang/` 运行：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
python3 -m compileall -q anklang tests
docker compose config -q
docker build -t anklang:verify .
```

受影响测试先跑，最后再跑完整套件和容器部署测试。提交前检查暂存清单；不得提交 `private/`、题库数据库、密钥、题面、模型原始响应或无关仓库改动。