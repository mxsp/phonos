# Dockerfile for Node1-ESM Worker
# ESMFold protein structure prediction worker node

FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY node1-esm.py .
COPY config.py .
COPY shared/ shared/

# Create necessary directories
RUN mkdir -p /app/output /app/cache

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV WORKER_TYPE=type-0
ENV WORKER_ID=publisher-node-type-0

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import redis; r=redis.Redis(host='${REDIS_HOST}', port=${REDIS_PORT}); r.ping()" || exit 1

# Run the worker
CMD ["python", "node1-esm.py"]
