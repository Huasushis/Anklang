# Anklang

Anklang 是 USTC 算法竞赛协会命题系统 Urmotiv 的配套服务，负责判断一道新题是不是"原题"
（已经在某个公开题库出现过的题目）。

## 定位

Anklang 是完全独立的服务：独立仓库、独立部署、独立数据库，只通过带版本号的 HTTP 接口
（`POST /api/v1/checks/similarity`）与 Urmotiv 通信。Urmotiv 不会把候选题的正文或 Anklang 的
公开题库复制进自己的数据库；Anklang 也不读取、不连接 Urmotiv 的数据库。这样设计是为了让
"抓取和检索公开题库"带来的合规风险、维护成本和模型密钥，都不落在 Urmotiv 主系统上。

## 当前状态

- **阶段 1 已实现并默认启用。** 这是转发模式：把 Urmotiv 的请求交给 yuantiji.ac 检索，
  再把结果整理成双方约定的数据结构。它按内容摘要缓存结果，也可以调用大语言模型复核最相似的
  候选是否为同一道题。阶段 1 已完成真实联调。
- **阶段 2 的本地检索主体已实现，但默认关闭。** 它把题面转换成数字列表，再比较这些数字找出
  含义相近的题目，同时用字面重合补充搜索；在转换服务不可用时会只做字面搜索。当前仓库没有
  正式公开题库数据，因此它还不能替代默认转发模式。
- **阶段 3 的来源接口、导入调度和示例来源已实现。** “来源接口”是让不同题目来源按同一组函数
  提供数据的约定。仓库只包含读取本地示例数据的 `example_static`，不包含真实爬虫；vjudge 来源
  仍因规模、合规、账号和代理维护风险而推迟。

全部代码使用 Python 3.11+ 标准库实现。部署服务器没有 pip/venv，因此不需要、也不应该为了运行
本项目安装 FastAPI、Pydantic、numpy 等第三方包。

### 运行

```sh
# 需要 Python 3.11+，无第三方依赖
# 先由部署平台或进程管理器传入 ANKLANG_SERVICE_TOKEN，再启动：
python3 -m anklang
# 默认监听 8730 端口；GET /api/v1/health、POST /api/v1/checks/similarity
```

在 Urmotiv 管理后台启用"原题相似度检查"插件，把 baseUrl 指向本服务地址、
serviceToken 密钥填成进程收到的同一个令牌即可。`.env.example` 只是一份字段与默认值参考，
程序不会自动读取它。真实密钥应由部署平台或进程管理器传入；不要用 shell 的 `source` 或 `.`
加载密钥文件，特殊字符可能导致命令失败并把密钥回显到终端。

### 使用本地检索与来源导入

把 `ANKLANG_BACKEND` 设为 `local_engine` 后，服务会使用本地 SQLite 文件。SQLite 是 Python
自带的单文件数据库，不需要另起数据库服务。路径、搜索数量、文字转数字服务和定时导入开关见
`.env.example`。所有这些变量同样由部署平台或进程管理器传入。

```sh
# 跑一轮来源导入；当前公开仓库只会发现 example_static 示例来源。
python3 -m anklang.ingest

# 给已经入库、但还没有数字列表的题目补算；需要配置 DASHSCOPE_* 变量。
python3 -m anklang.backfill
```

新增来源时，在 `anklang/sources/<名称>/` 中实现 `SOURCE_NAME` 和
`fetch_new_problems(since)`。自行编写的公开题目抓取代码不能进入公开仓库；应优先使用明确允许
程序访问的官方接口，并在接入前核对来源网站规则和许可证。

### 测试

```sh
python3 -m unittest discover -s tests
```

## 文档索引

- [`AGENTS.md`](AGENTS.md)：开发约定，包括与 Urmotiv 的接口契约、安全红线、开发顺序建议和
  测试要求。第一次接手这个项目，从这份文件开始读。
- [`docs/plan.md`](docs/plan.md)：从零设计时留下的分阶段规划和决策记录。文档顶部说明了当前
  标准库实现与最初技术设想的差异；来源合规、vjudge 暂缓原因和待人工决定事项仍可作为背景。

## 许可证

MIT License，见 [`LICENSE`](LICENSE)。
