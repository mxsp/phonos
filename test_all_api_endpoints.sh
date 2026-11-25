#!/bin/bash
# Test all API endpoints called by the API tokens view

echo "Testing All API Tokens View Endpoints..."
echo "========================================="
echo

BASE_URL="http://localhost:5000"

# Test each endpoint
endpoints=(
    "/api/keys"
    "/api/keys/stats"
    "/api/webhooks"
    "/api/worker-keys"
    "/api/external-queue/status"
)

for endpoint in "${endpoints[@]}"; do
    echo "Testing: $endpoint"
    response=$(curl -s -w "\nHTTP_CODE:%{http_code}" "$BASE_URL$endpoint")
    http_code=$(echo "$response" | grep "HTTP_CODE" | cut -d: -f2)

    if [ "$http_code" = "401" ]; then
        echo "   ✓ Endpoint exists (401 Unauthorized - expected for auth-required endpoint)"
    elif [ "$http_code" = "404" ]; then
        echo "   ✗ ERROR: Endpoint not found (404)"
    elif [ "$http_code" = "500" ]; then
        echo "   ⚠ Server error (500)"
    else
        echo "   ✓ Endpoint accessible (HTTP $http_code)"
    fi
    echo
done

echo "========================================="
echo "Test complete! All endpoints should return 401 (auth required)."
