# Phonos Notebook Kernel Container
# Python 3.11 Jupyter kernel with scientific computing packages

FROM python:3.11-slim-bookworm

LABEL maintainer="Phonos Platform"
LABEL description="Jupyter kernel container for Phonos Notebook IDE"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    curl \
    git \
    wget \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libjpeg-dev \
    libpng-dev \
    libfreetype6-dev \
    pkg-config \
    r-base \
    r-base-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Rust compiler
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

# Install Python packages
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# Install Phonos IoT SDK
COPY phonos_iot /tmp/phonos_iot/phonos_iot
COPY setup.py /tmp/phonos_iot/setup.py
RUN pip install --no-cache-dir /tmp/phonos_iot && \
    rm -rf /tmp/phonos_iot

# Install Phonos SDK for AI Models
COPY phonos /tmp/phonos/phonos
COPY phonos/setup.py /tmp/phonos/setup.py
COPY phonos/README.md /tmp/phonos/README.md
RUN pip install --no-cache-dir /tmp/phonos && \
    rm -rf /tmp/phonos

# Create non-root user for security and install Rust for the user
RUN useradd -m -s /bin/bash -u 1000 notebook && \
    mkdir -p /workspace /project /home/notebook/.local /home/notebook/.jupyter && \
    chown -R notebook:notebook /workspace /project /home/notebook

# Install Rust for notebook user
USER notebook
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/home/notebook/.cargo/bin:${PATH}"

# Switch back to root for remaining setup
USER root

# Configure Jupyter
RUN mkdir -p /etc/jupyter && \
    echo "c.NotebookApp.token = ''" > /etc/jupyter/jupyter_notebook_config.py && \
    echo "c.NotebookApp.password = ''" >> /etc/jupyter/jupyter_notebook_config.py

# Install kernel for user
USER notebook
WORKDIR /workspace

RUN python -m ipykernel install --user --name python3 --display-name "Python 3.11" && \
    python -m ipykernel install --user --name quantum --display-name "Python 3.11 Quantum"

# Copy kernel gateway script
COPY --chown=notebook:notebook kernel_gateway.py /home/notebook/kernel_gateway.py
COPY --chown=notebook:notebook startup.py /home/notebook/startup.py

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/home/notebook
ENV JUPYTER_RUNTIME_DIR=/home/notebook/.local/share/jupyter/runtime
ENV PHONOS_API_URL=http://172.18.0.1:5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Expose ZMQ ports (dynamically assigned, but useful for documentation)
EXPOSE 5555-5560

CMD ["python", "/home/notebook/kernel_gateway.py"]
