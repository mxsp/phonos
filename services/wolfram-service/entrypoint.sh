#!/bin/bash
# Wolfram Service Entrypoint
# Activates Wolfram Engine on startup if credentials are provided,
# then runs the HTTP API server.

set -e

# Activate Wolfram Engine if credentials provided
if [ -n "$WOLFRAM_USERNAME" ] && [ -n "$WOLFRAM_PASSWORD" ]; then
    echo "Activating Wolfram Engine with username/password..."
    wolframscript -activate -username "$WOLFRAM_USERNAME" -password "$WOLFRAM_PASSWORD" -verbose 2>&1
    echo "Activation complete."
elif [ -n "$WOLFRAM_LICENSE" ]; then
    echo "Activating Wolfram Engine with license key..."
    wolframscript -activate -entitlement "$WOLFRAM_LICENSE" -verbose 2>&1
    echo "Activation complete."
else
    echo "WARNING: No Wolfram credentials provided. Set WOLFRAM_USERNAME+WOLFRAM_PASSWORD or WOLFRAM_LICENSE."
fi

# Execute the CMD (the API server)
exec "$@"
