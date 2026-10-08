FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md requirements-lock.txt /app/
COPY src /app/src
COPY schemas /app/schemas
COPY configs /app/configs
COPY prompts /app/prompts
COPY registry /app/registry
COPY skills /app/skills
COPY scripts /app/scripts
COPY deploy /app/deploy
RUN python -m pip install -r requirements-lock.txt \
    && python -m pip install --no-deps "." \
    && python scripts/validate_schemas.py \
    && python -m compileall -q src scripts
RUN groupadd --gid 10001 ecmonitor \
    && useradd --uid 10001 --gid ecmonitor --no-create-home ecmonitor \
    && mkdir -p /data /sources \
    && chown ecmonitor:ecmonitor /data
USER 10001:10001
ENV ECMONITOR_DATA_ROOT=/data ECMONITOR_SOURCE_ROOT=/sources ECMONITOR_PROJECT_ROOT=/app

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python deploy/healthcheck.py

CMD ["ecmonitor-workflow", "worker"]
