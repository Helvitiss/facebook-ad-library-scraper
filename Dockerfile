FROM python:3.11-slim-bookworm

# Установка базовых системных зависимостей
RUN apt-get update && apt-get install -y \
    ffmpeg \
    shadowsocks-libev \
    curl \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libasound2 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем requirements.txt отдельно для эффективного кеширования слоев
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка Playwright и системных зависимостей для браузеров (Chromium и Firefox)
RUN playwright install --with-deps chromium firefox

# Предварительная загрузка модели Whisper (base модель, вшиваем в образ)
RUN python -c 'from faster_whisper import WhisperModel; WhisperModel("base", device="cpu", compute_type="int8", download_root="/app/models/whisper")'

# Копирование остального кода приложения
COPY . .

# Создание необходимых директорий для результатов
RUN mkdir -p Parser_Results Exporter_Results tmp_gql_dumps

# Настройка PYTHONPATH для корректного импорта модулей
ENV PYTHONPATH=/app

CMD ["python", "main.py"]
