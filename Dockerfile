FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 部署修订标识由构建流水线注入（如 git rev-parse --short HEAD），运行时只从
# 环境变量读取，不做任何 Git 访问；未注入时保持未设置，服务端不输出该响应头。
ARG ANKLANG_REVISION=
ENV ANKLANG_REVISION=${ANKLANG_REVISION}

WORKDIR /app

RUN groupadd --system --gid 10001 anklang \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin anklang \
    && mkdir -p /app/problems-data \
    && chown 10001:10001 /app/problems-data

COPY --chown=10001:10001 anklang /app/anklang
COPY --chown=10001:10001 LICENSE /app/LICENSE

USER 10001:10001

EXPOSE 8730

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=4 \
    CMD ["python3", "-c", "import os,urllib.request; port=os.environ.get('ANKLANG_PORT', '8730'); opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); response=opener.open(f'http://127.0.0.1:{port}/api/v1/live', timeout=2); raise SystemExit(0 if response.status == 200 else 1)"]

CMD ["python3", "-m", "anklang"]
