FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Минимальные системные зависимости (g++ нужен для hnswlib внутри chromadb)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# uv — для stdio MCP-серверов (command:"uvx", например mcp-server-fetch)
RUN pip install --no-cache-dir uv

COPY . .

RUN mkdir -p data logs skills/system skills/custom skills/external
RUN chmod -R 755 data logs skills

CMD ["python", "-m", "app.bot"]
