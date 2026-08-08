# Multi-stage Dockerfile for Railway deployment
# Builder stage - install dependencies and build
FROM python:3.11-slim AS builder

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libpq-dev \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies to /install directory
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime stage - minimal distroless-like image
FROM python:3.11-slim AS runtime

# Install only runtime dependencies (ffmpeg + libpq for asyncpg + netcat for DB wait)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq5 \
    unzip \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Install deno for yt-dlp YouTube n-challenge solving
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    chmod +x /usr/local/bin/deno && \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /bin/bash appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=appuser:appuser . .

# Copy entrypoint script
COPY --chown=appuser:appuser entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create download directories
RUN mkdir -p /app/media_downloads /app/cookie_data /tmp/yt-dlp-cache \
    && chown appuser:appuser /app/media_downloads /app/cookie_data /tmp/yt-dlp-cache

# Set XDG_CACHE_HOME so yt-dlp doesn't try to write to /app/.cache (root-owned)
ENV XDG_CACHE_HOME=/tmp/yt-dlp-cache

# Switch to non-root user
USER appuser

# Expose port (Railway sets PORT env var)
EXPOSE 8080

# Health check - use the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=5)" || exit 1

# Run the bot via entrypoint
ENTRYPOINT ["/entrypoint.sh"]