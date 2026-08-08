#!/bin/bash
# Entrypoint script for Railway deployment
# Runs database migrations and initial setup before starting the bot

set -e

echo "🚀 Starting Railway entrypoint..."

# Wait for PostgreSQL to be ready (if DATABASE_URL is set)
if [ -n "$DATABASE_URL" ]; then
    echo "⏳ Waiting for PostgreSQL..."
    
    # Extract host and port from DATABASE_URL
    # Format: postgresql://user:pass@host:port/dbname
    PG_HOST=$(echo $DATABASE_URL | sed -E 's/.*@([^:]+):.*/\1/')
    PG_PORT=$(echo $DATABASE_URL | sed -E 's/.*:([0-9]+)\/.*/\1/')
    
    # Use pg_isready or simple netcat check
    for i in {1..30}; do
        if nc -z "$PG_HOST" "$PG_PORT" 2>/dev/null; then
            echo "✅ PostgreSQL is ready!"
            break
        fi
        echo "   Waiting... ($i/30)"
        sleep 1
    done
fi

# Run database migrations (init schema)
if [ -n "$DATABASE_URL" ]; then
    echo "🔧 Running database migrations..."
    python -c "
import asyncio
import sys
sys.path.insert(0, '.')
from database import get_database

async def run_migrations():
    db = await get_database()
    if db:
        print('✅ Database connected and schema initialized')
    else:
        print('⚠️  No PostgreSQL configured - running in stateless mode')

asyncio.run(run_migrations())
"
    
    if [ $? -eq 0 ]; then
        echo "✅ Migrations completed successfully"
    else
        echo "❌ Migration failed"
        exit 1
    fi
else
    echo "⚠️  No DATABASE_URL - skipping migrations"
fi

# Verify critical environment variables
echo "🔍 Verifying environment..."
if [ -z "$BOT_TOKEN" ]; then
    echo "❌ BOT_TOKEN not set!"
    exit 1
fi
echo "✅ BOT_TOKEN: ${BOT_TOKEN:0:10}..."

if [ -z "$ADMIN_ID" ]; then
    echo "❌ ADMIN_ID not set!"
    exit 1
fi
echo "✅ ADMIN_ID: $ADMIN_ID"

# Print configuration summary
echo "📋 Configuration:"
echo "   PORT: ${PORT:-8080}"
echo "   DATABASE_URL: ${DATABASE_URL:+SET}"
echo "   REDIS_URL: ${REDIS_URL:+SET}"
echo "   GENIUS_API_TOKEN: ${GENIUS_API_TOKEN:+SET}"
echo "   Railway service: ${RAILWAY_SERVICE_NAME:-local}"

echo "🎯 Starting bot..."
exec python3 bot.py