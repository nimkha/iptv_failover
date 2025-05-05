#!/usr/bin/env bash
set -e

# Create logs directory
mkdir -p /app/logs
chmod 755 /app/logs

# Run with preload
exec gunicorn --bind 0.0.0.0:8000 \
     --workers 4 \
     --threads 2 \
     --log-level info \
     --access-logfile - \
     --error-logfile - \
     --preload \
     app:app