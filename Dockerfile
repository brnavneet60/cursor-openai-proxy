# syntax=docker/dockerfile:1
# Cursor OpenAI Proxy — FastAPI + Cursor SDK local (default) + Agent CLI fallback.
FROM python:3.12-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates bash \
  && rm -rf /var/lib/apt/lists/* \
  && useradd -m -u 10001 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
  && chown -R appuser:appuser /app

USER appuser
ENV HOME=/home/appuser
ENV PATH="/home/appuser/.local/bin:/usr/local/bin:${PATH}"

# Official Cursor Agent CLI — kept for CURSOR_BRIDGE_BACKEND=cli fallback
# https://cursor.com/docs/cli/installation
RUN curl -fsSL https://cursor.com/install | bash \
  && agent --version \
  && cursor-sdk-bridge --help >/dev/null

COPY --chown=appuser:appuser server.py sdk_runtime.py .

ENV HOST=0.0.0.0
ENV PORT=8765
ENV CURSOR_BRIDGE_MODE=ask
ENV CURSOR_BRIDGE_CHAT_ONLY=true
ENV CURSOR_BRIDGE_BACKEND=sdk
ENV CURSOR_AGENT_BIN=agent

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3)"

CMD ["python", "server.py"]
