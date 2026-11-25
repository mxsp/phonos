# Wolfram Language Execution Service
# Lightweight HTTP API wrapping wolframscript with Wolfram Cloud authentication

FROM wolframresearch/wolframengine:latest

LABEL maintainer="Phonos Platform"
LABEL description="Wolfram Language execution service for Phonos Notebook"

# Install Python for the API server
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-requests \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create app directory
WORKDIR /app

# Copy server, error parser, and entrypoint
COPY server.py /app/server.py
COPY wolfram_errors.py /app/wolfram_errors.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Switch back to wolframengine user
USER wolframengine

# Entrypoint: activates Wolfram Engine if credentials provided, then runs server
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "/app/server.py"]

EXPOSE 5100
