FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Официальный Claude Code CLI — пишет сценарии по подписке Claude (AI_ENGINE=claude-code).
# Не нужен — соберите с --build-arg INSTALL_CLAUDE_CODE=0. Если установщик недоступен
# (например, Claude не работает в регионе сервера), образ собирается без CLI — сайт остаётся рабочим.
ARG INSTALL_CLAUDE_CODE=1
ENV PATH="/root/.local/bin:${PATH}" \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1
RUN if [ "$INSTALL_CLAUDE_CODE" = "1" ]; then \
        if curl -fsSL -o /tmp/claude-install.sh https://claude.ai/install.sh \
           && head -n 1 /tmp/claude-install.sh | grep -q '^#!' \
           && bash /tmp/claude-install.sh && claude --version; then \
            echo "Claude Code установлен"; \
        else \
            echo "ВНИМАНИЕ: Claude Code не установлен (установщик недоступен) — сценарии по подписке выключены"; \
        fi; \
        rm -f /tmp/claude-install.sh; \
    fi

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
