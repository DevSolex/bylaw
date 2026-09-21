FROM python:3.12-slim

# Create and use a non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy and install dependencies first (layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source
COPY . .

# Ensure the data directory exists and is owned by appuser
RUN mkdir -p /app/data/runs && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Health check using Python stdlib (slim image has no curl)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        r = urllib.request.urlopen('http://localhost:8000/healthz', timeout=8); \
        sys.exit(0 if r.status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
