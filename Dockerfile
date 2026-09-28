# 私有知识库 — 运行时镜像
# 构建：docker build -t private-kb:0.3 .
# 数据与密钥挂卷，勿打进镜像。

FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    HF_HOME=/app/data/model_cache \
    SENTENCE_TRANSFORMERS_HOME=/app/data/model_cache \
    KB_ENV=production \
    KB_HOST=0.0.0.0 \
    KB_PORT=8000 \
    KB_WORKERS=1

WORKDIR /app

# uv 只参与构建；运行时直接调用固定虚拟环境中的入口，不执行依赖同步。
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 依赖清单先拷，利于层缓存
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY src ./src

# 运行时挂载：/app/data
RUN mkdir -p /app/data/raw /app/data/logs /app/data/locks /app/data/sqlite \
    /app/data/sessions /app/data/chroma_db /app/data/backups /app/data/model_cache \
    && groupadd --system --gid 10001 kb \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin kb \
    && chown -R kb:kb /app/data

VOLUME ["/app/data"]
EXPOSE 8000

# 就绪：生产缺 key 会 503；健康检查不依赖宿主机暴露端口。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD /opt/venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/ready', timeout=3)" || exit 1

USER 10001:10001

CMD ["/opt/venv/bin/mykb", "serve"]
