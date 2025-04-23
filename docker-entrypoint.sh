#!/usr/bin/env bash
set -e
cd /app
exec gunicorn --bind 0.0.0.0:8000 \
     --workers 4 \
     --threads 2 \
     --log-level info \
     app:app
