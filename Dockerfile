FROM python:3.11-slim

# Установка системных зависимостей (вручную для лучшей совместимости)
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libglib2.0-0 \
    fonts-liberation \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем requirements.txt отдельно для эффективного кеширования слоев
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка Playwright (Chromium) - пропускаем установку зависимостей (сделано выше)
RUN playwright install chromium

# Копирование остального кода приложения
COPY . .

# Создание необходимых директорий для результатов
RUN mkdir -p Parser_Results Exporter_Results

CMD ["python", "main.py"]
