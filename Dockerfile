# Use a slim Python base image
FROM python:3.11

# Set workdir
WORKDIR /app

# Ensure proper log directory exists
RUN mkdir -p /app/logs && \
    chown nobody:nogroup /app/logs && \
    chmod 755 /app/logs

# Install system deps (if any)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Use an entrypoint script
ENTRYPOINT ["./docker-entrypoint.sh"]

