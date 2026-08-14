# Anklang 部署与运行检查

## 1. 安全边界

Anklang 只提供版本化题面查询和健康检查。容器不运行反向代理、LLM 复核、标定或流程采集，也不连接 Fermata。题库与 Urmotiv、Fermata 的数据库相互独立。

生产默认：

- 宿主端口只绑定 `127.0.0.1`；
- 容器使用 UID/GID 10001、只读根文件系统、删除全部 Linux capabilities；
- 只有 `/app/problems-data` 可写；
- 必须配置服务令牌；
- 响应禁止缓存，错误不回显题面、路径、密钥或外部响应。

## 2. 配置

复制 `.env.example` 的字段到 Git 忽略的部署环境文件，按部署平台安全注入。不要用 shell 的 `source` 或 `.` 加载环境文件。

最低生产配置：

```text
ANKLANG_SERVICE_TOKEN=<至少 16 个字符的随机值>
ANKLANG_REQUIRE_SERVICE_TOKEN=true
ANKLANG_LOCAL_DB_PATH=problems-data/local-index.db
```

向量检索需要同时配置：

```text
DASHSCOPE_BASE_URL=<百炼 OpenAI 兼容接口的 base URL>
DASHSCOPE_API_KEY=<密钥>
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
DASHSCOPE_EMBEDDING_DIM=1024
```

URL 或密钥任一缺失时，查询明确返回不可用，增量抓取不写入题目或推进游标。不要把真实配置值复制到终端日志、测试报告或 Git。

运行时增量来源：

```text
ANKLANG_INGEST_ENABLED=true
ANKLANG_INGEST_INTERVAL_SECONDS=3600
```

每轮只拉取来源游标之后的新增或更新记录，直接写入当前 SQLite 索引。查询进程无需重启。

## 3. 构建

先验证 Compose 展开结果，再构建：

```bash
docker compose config -q
docker compose build
```

`compose.yaml` 要求部署环境文件存在，并固定：

- 容器内 `ANKLANG_BIND_HOST=0.0.0.0`；
- 宿主映射 `127.0.0.1:${ANKLANG_PORT:-8730}:8730`；
- `ANKLANG_REQUIRE_SERVICE_TOKEN=true`；
- 停止宽限大于应用退出宽限。

Docker 构建上下文采用默认拒绝策略，只复制 `anklang/`、`ui/` 和 `LICENSE`。不得把 `private/`、本地数据库、测试产物或 Git 元数据加入镜像。

## 4. 启动与观测

```bash
docker compose up -d --build
```

存活检查：

```bash
curl --fail --silent http://127.0.0.1:8730/api/v1/live
```

就绪检查：

```bash
curl --fail --silent http://127.0.0.1:8730/api/v1/ready
```

`live` 只证明进程能响应。`ready` 只检查本地服务状态，不读取题库、不调用 embedding。`health` 可报告本地题目数量、索引元数据和向量状态，但不得返回配置值。

## 5. 查询冒烟

使用部署平台提供的令牌发起一份合成请求；不要把真实题面写进命令历史或日志。v2 响应的顶层字段只能是：

```text
apiVersion, contentHash, checkedAt, completion, candidates
```

候选字段只能是：

```text
source, externalId, title, similarity, url（可选）
```

完整结果、部分结果和不可用结果都不能包含审核、通过、拦截或复核字段。`Cache-Control` 必须为 `no-store`。

## 6. 发布门禁

在仓库根目录运行：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
python3 -m compileall -q anklang ui tests
docker compose config -q
docker build -t anklang:verify .
```

容器部署测试会检查：

- 镜像只含运行文件且以非 root 用户运行；
- 回环绑定、只读根文件系统和唯一可写卷；
- 存活检查不受代理环境影响；
- SIGTERM 有界退出且在途请求不会泄漏；
- 构建上下文不包含私有资料。

最后检查暂存文件和跟踪状态。只能提交当前 Anklang 修改；私有证据、真实题面、题解、测试数据、密钥和模型原始响应必须继续留在 Git 忽略区域。