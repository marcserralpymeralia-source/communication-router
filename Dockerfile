FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY backend /app/backend
COPY api /app/api
COPY Dockerfile ./Dockerfile
COPY vercel.json ./vercel.json
COPY Procfile ./Procfile
COPY docker-compose.yml ./docker-compose.yml
COPY docs ./docs
COPY README.md ./README.md
COPY ARCHITECTURE.md ./ARCHITECTURE.md
COPY CLIENTE_NUEVO.md ./CLIENTE_NUEVO.md
COPY ESCALAR_CLIENTES.md ./ESCALAR_CLIENTES.md

WORKDIR /app/backend

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
