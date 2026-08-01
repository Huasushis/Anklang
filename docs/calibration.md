# 相似度标定与证据保存

本页说明怎样衡量 Anklang 找原题是否准确。这里的“标定”是拿一批已经由人确认答案的题目做检查，
计算召回率和误报率，再判断某个相似度下限是否有足够证据。标定程序只产生证据，**不会修改线上配置、
不会打开自动拦截，也不会调用 LLM 复核**。

当前本地检索还没有正式公开语料，因此本工具首先用于建立可恢复、不可覆盖的实验流程。没有完整报告
以前，不能把“程序测试通过”写成“原题判断准确”，也不能打开
`ANKLANG_SIMILARITY_BLOCK_ENABLED`。

## 1. 私有目录

所有题面、人工答案、逐条候选和检查点都放在仓库已经忽略的目录：

```text
problems-data/calibration/
├── dataset.json                 # 人工标注数据，0600
├── corpus.json                  # 语料来源、许可与版本清单，0600
├── corpus-snapshot.db           # 清单绑定的实际语料快照，0600；文件名可自定
└── runs/
    └── <唯一实验标签>/          # 0700
        ├── run.lock             # 同标签独占锁，0600
        ├── checkpoint.json      # 可恢复的逐条私有证据，0600
        └── report.json          # 不含题面和候选明细的安全汇总，0600
```

先创建仅当前用户可访问的目录，不要把真实材料放进 Git：

```sh
install -d -m 700 problems-data/calibration
```

输入文件必须是当前用户拥有的普通文件，权限为 `0400` 或 `0600`，不能是符号链接。工作目录和实验
目录必须是 `0700`。程序会拒绝权限过宽、位于工作目录外或经符号链接指向别处的输入。

## 2. 人工标注数据

数据集是一个 JSON 对象。下面只展示自编占位材料，不能把真实题面复制进文档：

```json
{
  "schemaVersion": "1",
  "datasetId": "private-calibration-v1",
  "cases": [
    {
      "caseId": "cal-positive-001",
      "split": "calibration",
      "statement": "人工自编或许可证允许使用的题面文本",
      "expectedDuplicateCandidates": [
        {"source": "public-source", "externalId": "problem-001"}
      ]
    },
    {
      "caseId": "cal-negative-001",
      "split": "calibration",
      "statement": "另一份人工自编题面文本",
      "expectedDuplicateCandidates": []
    }
  ]
}
```

- `caseId` 是不含题名的稳定编号。
- `calibration` 用来选择候选阈值；`holdout` 是事先隔离的留出集，只用来验证选择结果。
- `expectedDuplicateCandidates` 非空表示正例；候选身份由 `source` 和 `externalId` 两项共同组成，
  两个来源即使使用同一编号也不是同一道题。空数组表示
  人工确认的反例。
- 两个分组都必须同时有正例和反例，避免 `0/0` 被误判为通过。
- 同一题面不能重复出现，也不能跨分组出现。两个分组不能共享人工确认的候选编号，避免用已经见过的
  答案验证自己。
- 数据结构故意没有题名、链接、题解、作者、账号和审核意见字段。

历史 USTC 私有题目如果用于标定，只能作为这里的私有查询样本，不能变成本地公开语料。用户允许把
私有题面和题解发送给当前配置的外部模型，并不改变 Git、日志、报告和错误信息的保密要求。本工具不
调用 LLM 复核；反向代理检索和已配置的外部 embedding（把文字转换成数字向量的服务）仍会收到题面。
即使已经取得总体同意，每一次这类运行也必须显式添加 `--allow-external-statements`，让许可进入本次
`backendHash`，不能依赖默认配置静默外发。

## 3. 语料清单

每次实验还要绑定一份语料清单：

```json
{
  "schemaVersion": "1",
  "corpusId": "licensed-public-corpus-v1",
  "problemCount": 100,
  "sources": [
    {
      "sourceId": "documented-public-source",
      "revision": "immutable-source-revision",
      "license": "人工核对后的许可证说明",
      "licenseReviewed": true,
      "provenance": "人工保存的来源与取得方式记录",
      "contentSha256": "64 位小写 sha256"
    }
  ],
  "artifact": {
    "kind": "anklang-sqlite-v1",
    "fileName": "corpus-snapshot.db",
    "contentSha256": "实际 SQLite 文件的 64 位小写 sha256",
    "problemCount": 100,
    "embeddingRows": 100,
    "embeddingModel": "text-embedding-v4",
    "embeddingDimensions": 1024,
    "indexBuildRevision": "负责构建索引的代码版本"
  }
}
```

清单本身不会证明抓取行为合法；`licenseReviewed: true` 表示已经有人核对网站规则、许可证、取得方式和
留存范围。自行编写的公开题目爬虫仍不能进入公开仓库，vjudge 仍按 `docs/plan.md` 暂缓。优先使用
许可证明确的现成数据或允许程序访问的官方接口。

`artifact` 必须指向工作目录正下方的实际普通文件，程序会流式计算文件摘要，并把“清单摘要 + 实际
快照摘要”共同记为 `corpusHash`。运行结束前还会再算一次，期间变化就只保留检查点、不发布报告。
这类检查点会被永久标为失效；即使后来放回原字节也不能恢复。源码在运行期间变化或实验目录被替换时
采用同样处理。修改来源、版本、数量、许可记录或实际快照后，旧检查点都不能继续，必须创建新标签。
`sources` 中的
`contentSha256` 是来源留档摘要；`artifact.contentSha256` 才是本次后端实际使用的快照摘要。

本地模式只允许把 `ANKLANG_LOCAL_DB_PATH` 指向这份 SQLite 快照，并用只读、不可变方式钉住已核对
摘要的文件描述符；不会建表、迁移、写 WAL 或改数据库。程序会核对 `problems` 的行数、非空向量数和
每条向量的实际维度。无论当前是否已经补齐向量，正式本地索引都必须有由 `ProblemStore` 生成、
随题目写入在同一事务维护的机器可读表：

```sql
CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

该表必须恰好登记 `schema_version`、`embedding_model`、`embedding_dimensions`、
`corpus_revision`、`index_build_revision`、`problem_count`、`embedding_rows` 七项，不能手工添加扩展键。
程序会从实际 `(source, external_id, content_hash)` 集合复算 `corpus_revision`，并重新统计两类行数；
含向量时，模型、维度和构建版本还必须与清单一致。同维但模型不同也不兼容。缺少、冲突或无法复算
这些证据仍可跑出聚合指标，但 `thresholdEvidenceEligible` 会是 `false`，不会推荐阈值。不要为让旧
报告变成可用而手填这张表；旧库已有未知向量时，应保留旧文件并按 README 的新数据库路径流程重建。
没有向量的正式关键词索引由程序登记固定的“未使用向量”模型状态和零维度；清单仍按前述格式把三项
向量构建信息写成 `null`，校准器会核对数据库确实没有向量，而不会把该固定状态冒充实际模型。
零向量却登记了真实向量模型，或只有部分题目带向量，都属于未完成的向量索引：只读标定会与线上
检索一样把样本记为失败，并把阈值证据判为不合格；不能通过关闭向量客户端把它冒充完整关键词索引。
`corpus_revision` 的每行摘要使用域分离、长度前缀规范编码的 SHA-256，再逐字节异或成与顺序无关的
集合承诺；题目身份唯一约束和 `problem_count` 一并核对。算法版本属于 `index_build_revision`，任何
算法调整都必须提升构建版本并重建快照，不能静默接受旧值。
反向代理无法证明远端实际使用了哪份不可变语料，因此 `artifact.kind` 使用 `remote-snapshot` 只作运行
留档；这类报告同样永远不推荐阈值。

## 4. 运行

程序使用当前环境配置的检索后端，但绕过 `anklang.review`，所以不会进行 LLM 复核，也不会读取或修改
线上拦截开关：

```sh
python3 -m anklang.calibrate \
  --workspace problems-data/calibration \
  --dataset problems-data/calibration/dataset.json \
  --corpus-manifest problems-data/calibration/corpus.json \
  --label public-baseline-20260801
```

这条不带外发许可的形式只适用于本地、未配置外部 embedding 的运行；若当前仍是默认反向代理，程序会
明确拒绝，不会静默发送题面。

环境变量由进程管理器或当前进程安全传入；不要 `source` 或 `.` 加载 `.env`。

- `ANKLANG_BACKEND=local_engine` 会查询当前本地 SQLite 题库。
- `ANKLANG_BACKEND=reverse_proxy` 会把数据集题面发送给配置的 yuantiji 服务。它由个人维护，正式实验前
  应先确认用途和调用量，并控制数据范围；命令缺少 `--allow-external-statements` 时会在构造后端前
  拒绝运行。单元测试不会发起这种请求。
- 已配置的本地 embedding 服务会收到查询题面。它失败后本地后端虽然可以退回字面检索，但标定程序会
  把这种降级结果记为失败，不能算完整样本；这类运行也必须添加同一个显式许可参数。

例如，明确允许当前这一次外发后才能运行：

```sh
python3 -m anklang.calibrate \
  --workspace problems-data/calibration \
  --dataset problems-data/calibration/dataset.json \
  --corpus-manifest problems-data/calibration/corpus.json \
  --label external-baseline-20260801 \
  --allow-external-statements
```

默认记录 Recall@1、Recall@5、Recall@8，并在预先登记的一组阈值上检查结果。需要改变指标时必须在
运行前通过参数登记；这些设置会进入 `configHash`：

```sh
python3 -m anklang.calibrate \
  --workspace problems-data/calibration \
  --dataset problems-data/calibration/dataset.json \
  --corpus-manifest problems-data/calibration/corpus.json \
  --label public-candidate-20260801 \
  --top-k 1 --top-k 5 --top-k 8 \
  --selection-k 8 \
  --candidate-threshold 0.80 \
  --candidate-threshold 0.85 \
  --candidate-threshold 0.90 \
  --minimum-recall 0.95 \
  --maximum-false-block-rate 0.05
```

列表参数必须严格递增且不能重复。K 最大为 50，命令行登记的最大 K 还不能超过线上
`ANKLANG_SEARCH_K`，阈值选择使用的 K 必须恰好等于它；实际后端始终按该线上 K 检索，再由证据层
计算较小 K 的辅助 Recall@K。候选显示下限也必须等于线上 `ANKLANG_MINIMUM_SIMILARITY`，待评估拦截
阈值不能低于这个下限。不要跑完以后根据结果临时补一个有利阈值；那会改变 `configHash`，应使用新
标签重新运行。

## 5. 五类绑定与恢复

检查点和报告都保存以下摘要：

- `datasetHash`：人工标注数据原始文件；
- `configHash`：Recall@K、显示下限、待评估阈值和验收目标；
- `codeHash`：当前 `anklang/**/*.py` 源码；
- `backendHash`：影响检索的非秘密后端设置；地址只登记摘要，密钥不登记；
- `corpusHash`：语料来源清单和实际语料快照内容。

程序用独占锁防止两个进程同时运行同一标签。每条检索前先把 `activeCase` 同步落盘，返回终态后再原子
更新。`Ctrl+C`、进程退出或落盘失败时，服务端是否已经完成无法可靠判断；恢复会把该 `activeCase`
固定记为 `cancelled`，**不会自动重发**，随后继续其他样本。因此这份报告必然不完整；若确实需要重试
该样本，必须使用新标签重跑整个实验，不能覆盖原证据。使用完全相同的文件、代码和配置恢复：

```sh
python3 -m anklang.calibrate \
  --workspace problems-data/calibration \
  --dataset problems-data/calibration/dataset.json \
  --corpus-manifest problems-data/calibration/corpus.json \
  --label public-baseline-20260801 \
  --resume
```

若后端需要外发题面，恢复命令也必须再次添加 `--allow-external-statements`；该布尔值属于后端绑定。

五类摘要任一变化都会拒绝恢复。已经生成 `report.json` 的标签永远不能覆盖或恢复，无论报告完整与否；
下一次实验必须使用新标签。检查点采用临时文件、同步落盘后替换，报告使用“不存在才发布”的原子操作。

## 6. 完整性和指标

每个样本只有以下终态：

- `success`：后端完整返回且候选结构合法；
- `error`：后端异常、降级或候选损坏；
- `missing`：没有得到样本结果；
- `skipped`：调用方跳过；
- `cancelled`：调用方取消。

只有全部样本都是 `success`，报告的 `complete` 才为 `true`。任何异常、缺失、跳过或取消都会同时产生：

- `complete: false`；
- `metrics: null`；
- `thresholdRecommendation: null`。

报告中的指标含义：

- `recallAtK`：正例中，人工确认候选出现在前 K 项的比例；
- `falsePositiveRateAtDisplayThreshold`：反例中，在显示下限以上仍出现候选的比例；
- `recallAtSelectionKByThreshold`：同时考虑候选名次与待评估阈值后的正例召回率；
- `falseBlockRateByThreshold`：反例会被对应阈值错误拦截的比例；
- `latencyMs`：成功调用耗时的样本数、平均值、中位数、95 分位和最大值。

阈值先只在 `calibration` 中选择，再到 `holdout` 验证。每个分组至少需要 100 个正例和 100 个反例；
不能用少量样本的点估计推荐阈值。两边都使用 95% Wilson 置信区间：召回率取保守的下界，假拦截率取
保守的上界，并同时达到登记目标后才会给出 `thresholdRecommendation`。此外，实际语料和向量来源必须
可验证，即 `thresholdEvidenceEligible: true`。完整报告也可能因为证据规模、语料绑定或指标不达标而
不给建议。程序只报告建议，不写 `.env`、不修改 `ANKLANG_BLOCK_THRESHOLD`，也不打开自动拦截。

## 7. 安全报告与对比规则

`report.json` 只包含：实验标签、五类摘要、设置、样本与终态计数、聚合耗时、聚合质量指标和阈值建议。
它不包含：

- 题面、题名、链接或题解；
- 样本编号和候选编号；
- 候选列表、逐条分数或模型原始回答；
- 外部异常文字、私有路径、地址或密钥。

`checkpoint.json` 是私有恢复证据，会保存样本编号、候选编号和分数，但同样不保存题面、标题、链接、
候选说明、模型原话或异常文字。两类文件当前都保持 `0600` 且不进入 Git。以后若要提交安全汇总，应先
另行人工审阅，不要直接提交整个运行目录。

调整语料、向量模型、检索步骤、提示词或阈值前，先用唯一标签完成旧方案基线；候选方案使用另一个标签。
完整和不完整报告都保留，不能删除失败证据，也不能拿不完整报告宣布提升。
