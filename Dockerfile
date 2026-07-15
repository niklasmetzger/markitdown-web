# syntax=docker/dockerfile:1.7
# Multi-stage build: keeps image small and avoids shipping the build toolchain.

FROM python:3.13-slim AS base

# System deps: tesseract (OCR) and a few common media codecs that markitdown probes for.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
        ffmpeg \
        libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Layer caching: deps first, code second
COPY requirements.txt ./
COPY packages/ ./packages/

# Install webapp deps + markitdown (editable, from local source)
RUN pip install -r requirements.txt

# Copy the webapp code
COPY webapp/ ./webapp/

# Non-root user
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000

WORKDIR /app/webapp

# Healthcheck (uses the /health endpoint)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
