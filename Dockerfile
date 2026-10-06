# PDF Editor for hosting (Render, Koyeb, Fly.io, any Docker host).
FROM python:3.12-slim

# Fonts for writing text on Linux (Liberation = metric-compatible Arial/Times/Courier).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-liberation fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# Hosted mode: private documents per browser, memory limits, per-visitor AI keys.
ENV PDFEDITOR_HOSTED=1 \
    PDFEDITOR_WORKSPACE=/tmp/workspace \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PORT=10000
EXPOSE 10000

# One worker process (documents are kept in its memory), a few threads.
# MALLOC_ARENA_MAX above keeps memory from fragmenting across threads.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:$PORT app:app"]
