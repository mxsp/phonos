# Phonos HPC Worker — containerized compute node
# Runs semi-externally on any machine with Docker; dials OUT to the Phonos
# API (no inbound ports). Setup on a new machine is two commands:
#
#   docker build -t phonos/hpc-worker:latest .
#   docker run -d --restart unless-stopped \
#     -e PHONOS_HPC_URL=https://your-phonos-host \
#     -e PHONOS_HPC_TOKEN=phc_... \
#     phonos/hpc-worker:latest

FROM python:3.11-slim-bookworm

LABEL maintainer="Phonos Platform"
LABEL description="Phonos HPC compute worker (numpy/scipy/pandas)"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

RUN useradd -m -s /bin/bash -u 1000 hpc
USER hpc
WORKDIR /app

COPY --chown=hpc:hpc worker.py task_runner.py /app/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "/app/worker.py"]
