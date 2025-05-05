#!/usr/bin/env bash
set -e

# Create logs directory if not exists
mkdir -p /app/logs

# Set proper permissions
chown -R nobody:nogroup /app/logs
chmod -R 755 /app/logs

# Run application
exec gunicorn --bind 0.0.0.0:8000 \
     --workers 4 \
     --threads 2 \
     --log-level info \
     --access-logfile - \
     --error-logfile - \
     --preload \
     app:app