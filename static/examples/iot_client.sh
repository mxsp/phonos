#!/bin/bash
# Phonos IoT Client - Shell/cURL example
# Requires: openssl, python3 (for JSON formatting)
#
# Usage:
#   ./iot_client.sh --api-key YOUR_KEY --device-id sensor-001 --project-id 1
#   ./iot_client.sh --api-key YOUR_KEY --device-id sensor-001 --project-id 1 --loop 5

set -e

# Default values
ENDPOINT="https://phonos.fyreflysystems.de"
DEVICE_TYPE="generic"
LOOP=1
INTERVAL=5

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --api-key) API_KEY="$2"; shift 2 ;;
        --endpoint) ENDPOINT="$2"; shift 2 ;;
        --device-id) DEVICE_ID="$2"; shift 2 ;;
        --project-id) PROJECT_ID="$2"; shift 2 ;;
        --device-type) DEVICE_TYPE="$2"; shift 2 ;;
        --loop) LOOP="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Validate required arguments
if [[ -z "$API_KEY" || -z "$DEVICE_ID" || -z "$PROJECT_ID" ]]; then
    echo "Usage: $0 --api-key KEY --device-id ID --project-id ID [options]"
    echo "Options:"
    echo "  --endpoint URL      Phonos server URL (default: https://phonos.fyreflysystems.de)"
    echo "  --device-type TYPE  temperature_sensor, bioreactor, or generic (default: generic)"
    echo "  --loop N            Number of data sends (default: 1)"
    echo "  --interval SEC      Seconds between sends (default: 5)"
    exit 1
fi

# Function to generate data based on device type
generate_data() {
    case "$DEVICE_TYPE" in
        temperature_sensor)
            python3 -c "
import random, json
print(json.dumps({
    'temperature': round(random.uniform(18, 28), 2),
    'humidity': round(random.uniform(30, 70), 2),
    'pressure': round(random.uniform(1000, 1025), 1)
}, sort_keys=True))"
            ;;
        bioreactor)
            python3 -c "
import random, json
print(json.dumps({
    'agitation_rpm': random.randint(80, 250),
    'dissolved_oxygen': round(random.uniform(4, 10), 2),
    'ph': round(random.uniform(6.5, 7.5), 2),
    'temperature': round(random.uniform(35, 38), 2),
    'volume_liters': round(random.uniform(3, 10), 2)
}, sort_keys=True))"
            ;;
        *)
            python3 -c "
import random, json
print(json.dumps({
    'counter': random.randint(0, 10000),
    'status': random.choice(['ok', 'warning', 'normal', 'active']),
    'value1': round(random.uniform(0, 100), 2),
    'value2': round(random.uniform(0, 100), 2)
}, sort_keys=True))"
            ;;
    esac
}

# Function to send data
send_data() {
    local DATA="$1"
    local TIMESTAMP=$(date +%s)

    # Build full payload with sorted keys
    PAYLOAD=$(python3 -c "
import json
data = $DATA
print(json.dumps({
    'data': data,
    'device_id': '$DEVICE_ID',
    'project_id': $PROJECT_ID,
    'timestamp': $TIMESTAMP
}, sort_keys=True))")

    # Generate HMAC-SHA256 signature
    MESSAGE="${TIMESTAMP}.${PAYLOAD}"
    SIGNATURE=$(echo -n "${MESSAGE}" | openssl dgst -sha256 -hmac "${API_KEY}" | cut -d' ' -f2)

    # Send request
    curl -s -X POST "${ENDPOINT}/api/iot/v1/data?api_key=${API_KEY}" \
        -H "Content-Type: application/json" \
        -H "X-Phonos-Timestamp: ${TIMESTAMP}" \
        -H "X-Phonos-Signature: ${SIGNATURE}" \
        -d "${PAYLOAD}"
}

echo "Phonos IoT Client - Shell"
echo "Device: $DEVICE_ID ($DEVICE_TYPE)"
echo "Project: $PROJECT_ID"
echo "Endpoint: $ENDPOINT"
echo ""

for ((i=1; i<=LOOP; i++)); do
    DATA=$(generate_data)
    echo "[$i/$LOOP] Sending: $DATA"
    RESULT=$(send_data "$DATA")
    echo "Result: $RESULT"

    if [[ $i -lt $LOOP ]]; then
        sleep "$INTERVAL"
    fi
done

echo ""
echo "Done!"
