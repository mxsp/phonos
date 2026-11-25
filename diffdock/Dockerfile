# Dockerfile for DiffDock Worker
# Molecular docking worker node

FROM continuumio/miniconda3:latest

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY node3-diffdock.py .
COPY config.py .
COPY shared/ shared/

# Copy DiffDock setup script
COPY setup_diffdock.sh .
RUN chmod +x setup_diffdock.sh

# Create necessary directories
RUN mkdir -p /app/output /app/cache /opt/DiffDock

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV WORKER_TYPE=type-3
ENV WORKER_ID=publisher-node-type-3
ENV DIFFDOCK_INSTALL_DIR=/opt/DiffDock
ENV DIFFDOCK_CONDA_ENV=diffdock

# Note: DiffDock installation should be done during deployment
# or in a derived image. Run setup_diffdock.sh to install DiffDock.

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import redis; r=redis.Redis(host='${REDIS_HOST}', port=${REDIS_PORT}); r.ping()" || exit 1

# Run the worker
CMD ["python", "node3-diffdock.py"]
