# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm

# Установка базовых системных зависимостей
RUN apt-get update && apt-get install -y \
    ffmpeg \
    shadowsocks-libev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем uv из официального образа
COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /uvx /bin/

# Копируем lock-файлы отдельно для эффективного кеширования слоев
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Установка Playwright и системных зависимостей для браузеров (Chromium и Firefox)
RUN uv run playwright install --with-deps chromium firefox

# Предварительная загрузка модели Whisper (base модель, вшиваем в образ)
RUN uv run python -c 'from faster_whisper import WhisperModel; WhisperModel("base", device="cpu", compute_type="int8", download_root="/app/models/whisper")'

# Копирование остального кода приложения
COPY . .

# Создание необходимых директорий для результатов
RUN mkdir -p Parser_Results Exporter_Results tmp_gql_dumps

# Настройка PYTHONPATH для корректного импорта модулей
ENV PYTHONPATH=/app

CMD ["uv", "run", "python", "main.py"]
