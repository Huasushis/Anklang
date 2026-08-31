# Anklang 开发约定

## 产品边界

Anklang 是公开项目 [is-my-problem-new](https://github.com/fjzzq2002/is-my-problem-new) 的小型直接改编。运行入口直接保留自上游 v2 提交 [`72e309bd`](https://github.com/fjzzq2002/is-my-problem-new/blob/72e309bdcea2669bc3f476bea6fa81b1f21e788a/ui/server.py) 的 `ui/server.py`；上游作者 Ziqian Zhong 的 MIT 版权声明必须保留在 `LICENSE` 中。

本地检索维护一条数据流：题面查询 → 向量化 → `cosine_all` → 相似度降序 → `collapse` → `mkrow` 候选。允许的扩展只有：

1. 用环境变量配置阿里云百炼兼容的 embedding（把文字转为向量）接口；
2. 来源插件在服务运行期间增量添加或更新可检索题目，不离线全量重建，不重启查询服务；
3. 用带版本号的 HTTP 查询接口供 Urmotiv 调用；
4. 把 yuantiji 公共搜索作为独立可选来源，并支持 `yuantiji`、`local`、`hybrid` 三种运行模式。

Anklang 不判断候选是否同题或可作参考，不给通过、拦截、审核建议，不调用 LLM 做复核，不做准确率标定或流程采集。yuantiji 适配器只发送当前查询题面并投影公开候选，不转发 Urmotiv 权限、题解、账号、审题意见或 Fermata 数据。重复/参考信息属于 Urmotiv；Fermata 只能通过带版本号的 Urmotiv HTTP 接口读取，不得与 Anklang 运行时互调或共享数据库。

只修改本仓库。不得为兼容旧范围恢复已删除的代理、复核、标定、缓存或采集路径。

## 当前结构

- `ui/server.py`：保留上游运行入口、`cosine_all`、`collapse`、`mkrow` 和检索调用顺序，并组装本地适配器。
- `anklang/embedding.py`：OpenAI 兼容的百炼 embedding 客户端；校验模型、维度、顺序和响应大小。
- `anklang/store.py`：仅保存 SQLite 当前题目行、向量和来源游标；查询每次读取当前行。
- `anklang/sources/`、`anklang/ingest.py`：来源发现、规范时间游标、幂等写入、更新冲突保护。
- `anklang/contracts.py`、`anklang/http_api.py`：严格的查询入/候选出契约和 HTTP 适配器。

## 接口不变量

- `POST /api/v1/checks/similarity`：完整查询成功才返回 200；结果字段严格为 `apiVersion`、`contentHash`、`checkedAt`、`candidates`。
- `POST /api/v2/checks/similarity`：始终显式返回 `completion`；结果仍只包含检索状态及候选，不包含产品判断。
- 候选字段严格为 `source`、`externalId`、`title`、`similarity`，可选 `url`；v2 还可返回有界 `metadata`、`statement` 和 `statementTruncated`，供受信任界面展开核对。
- 候选按 `similarity` 降序；服务端只应用显示下限，不形成阈值政策。
- HTTP 响应发送 `Cache-Control: no-store`，但运行时和契约中不得存在结果缓存、复用策略或代次状态。
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

embedding 提供方只由管理接口在运行期供给（进程内存，重启后回到未配置）；`DASHSCOPE_*` 等环境变量不能激活它。提供方未配置、被清除或失败时，查询与增量入库必须明确返回不可用；增量任务不得写入无向量题目或推进游标。SQLite 中记录的模型和维度必须与当前提供方一致；身份冲突不得覆盖现有向量。

测试不得发起真实外部请求。embedding 与 yuantiji 测试使用注入的 opener；HTTP 测试只连接回环地址。

## 验证

在 `Anklang/` 运行：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
python3 -m compileall -q anklang ui tests
docker compose config -q
docker build -t anklang:verify .
```

受影响测试先跑，最后再跑完整套件和容器部署测试。提交前检查暂存清单；不得提交 `private/`、题库数据库、密钥、题面、模型原始响应或无关仓库改动。
