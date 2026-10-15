# docker-log-bridge: a tiny read-only HTTP API that reads container logs from one
# or more Docker hosts (same `url|Label` list Dozzle uses) and serves a compact,
# LLM-ready report for Home Assistant's ai_task.generate_data.
FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge/ ./bridge/

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -qO- "http://localhost:${PORT}/api/health" >/dev/null 2>&1 || exit 1

CMD ["python", "-m", "bridge.app"]
