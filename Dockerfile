FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=600 \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.8.* \
 && useradd --create-home --uid 10001 worker

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY templates ./templates
# The rule files (`skills/`) are resolved next to the package; without them every job is refused at its pin.
COPY skills ./skills
# `--extra graph` adds the Neo4j driver (used only when WORKER_GRAPH=on). DEV_DEPS=1 adds pytest for the test profile.
ARG DEV_DEPS=0
RUN uv sync --frozen --extra graph $([ "$DEV_DEPS" = "1" ] && echo "" || echo "--no-dev") \
 && mkdir /data /anchors && chown worker /data /anchors

USER worker
ENV WORKER_STATE_DIR=/data \
    WORKER_LOG_FORMAT=json
VOLUME ["/data"]
EXPOSE 8765

# Liveness only: the process answers. Readiness (/readyz) is for the proxy and for `depends_on`.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/livez', timeout=4)"

# The console must listen on 0.0.0.0 inside the container; publish it to localhost only (see compose file).
CMD ["onboarding-worker", "serve", "--host", "0.0.0.0", "--port", "8765", "--state-dir", "/data"]
