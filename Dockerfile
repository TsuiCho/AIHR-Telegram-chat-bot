FROM python:3.11-alpine

# Устанавливаем системные зависимости
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    postgresql-dev \
    zlib-dev \
    jpeg-dev \
    openjpeg-dev \
    tiff-dev \
    freetype-dev

# Создаем пользователя
RUN adduser -D -h /app -s /bin/sh botuser
WORKDIR /app

# Копируем зависимости
COPY --chown=botuser:botuser requirements.txt ./

# Устанавливаем Python-пакеты
RUN pip install --no-cache-dir \
    aiogram==3.0.0b7 \
    asyncpg==0.28.0 \
    pdfplumber==0.10.0 \
    python-docx==0.8.11 \
    httpx==0.25.0 \
    python-dotenv==1.0.0

# Копируем исходный код
COPY --chown=botuser:botuser . .

USER botuser
CMD ["python", "aihr.py"]