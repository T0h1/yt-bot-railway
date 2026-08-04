# Multi-stage Dockerfile for Railway deployment
# Builder stage - install dependencies and build
FROM python:3.11-slim AS builder

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies to /install directory
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime stage - minimal distroless-like image
FROM python:3.11-slim AS runtime

# Install only runtime dependencies (ffmpeg + libpq for asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /bin/bash appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=appuser:appuser . .

# Create download directory
RUN mkdir -p /app/media_downloads && chown appuser:appuser /app/media_downloads

# Switch to non-root user
USER appuser

# Expose port (Railway sets PORT env var)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import requests; requests.get('http://localhost:8080/health', timeout=5)" || exit 1

# Run the bot
CMD ["python3", "bot.py"]