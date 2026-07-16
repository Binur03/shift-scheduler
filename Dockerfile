# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Prevent Python from writing pyc files and buffering stdout/stderr.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Install dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY . .

# Cloud Run sends traffic to $PORT (defaults to 8080).
EXPOSE 8080

# Run with Gunicorn. Cloud Run scales by instance, so a small number of
# workers/threads per instance is appropriate. `app:app` refers to the
# module-level Flask app created by create_app() in app.py.
CMD exec gunicorn \
    --bind :$PORT \
    --workers 2 \
    --threads 8 \
    --timeout 0 \
    app:app
