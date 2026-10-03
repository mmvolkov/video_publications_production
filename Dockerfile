FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Официальный Claude Code CLI — пишет сценарии по подписке Claude (AI_ENGINE=claude-code).
# Не нужен — соберите с --build-arg INSTALL_CLAUDE_CODE=0.
ARG INSTALL_CLAUDE_CODE=1
ENV PATH="/root/.local/bin:${PATH}" \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1
RUN if [ "$INSTALL_CLAUDE_CODE" = "1" ]; then \
        curl -fsSL https://claude.ai/install.sh | bash && claude --version; \
    fi

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
