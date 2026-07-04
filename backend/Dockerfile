FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Run via a wrapper that calls eventlet.monkey_patch() first,
# before any other imports. This eliminates the greenlet patching warning.
CMD ["python", "-c", "import eventlet; eventlet.monkey_patch(); exec(open('app.py').read())"]
