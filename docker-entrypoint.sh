#!/usr/bin/env bash
# Start the M3U auto‑reloader in background, then launch Gunicorn

# Kick off the auto‑reload thread
python - << 'PYCODE'
from app import auto_reload_m3u, checker, parse_m3u_files
import threading

# Ensure checker is using the latest config on start
checker.config = parse_m3u_files("input/")
threading.Thread(target=auto_reload_m3u, daemon=True).start()
PYCODE

# Start Gunicorn with 4 workers
exec gunicorn --bind 0.0.0.0:8000 \
    --workers 4 \
    --threads 2 \
    --log-level info \
    app:app

