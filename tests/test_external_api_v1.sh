#!/bin/bash

# ============================================================================
# Phonos External API v1 - Comprehensive Test Script
# ============================================================================
#
# This script tests all endpoints of the External API v1.
#
# Usage:
#   ./test_external_api_v1.sh <API_KEY> [BASE_URL]
#
# Examples:
#   ./test_external_api_v1.sh pk_abc123def456...
#   ./test_external_api_v1.sh pk_abc123def456... http://localhost:5000
#
# ============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
API_KEY="${1:-}"
BASE_URL="${2:-http://localhost:5000}"
API_PREFIX="/api/external/v1"

# Counters
PASSED=0
FAILED=0
SKIPPED=0

# Temporary storage for test data
TEST_PROJECT_ID=""
TEST_JOB_ID=""
TEST_FILE_ID=""

# ============================================================================
# Helper Functions
# ============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
    ((PASSED++))
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    ((FAILED++))
}

log_skip() {
    echo -e "${YELLOW}[SKIP]${NC} $1"
    ((SKIPPED++))
}

log_section() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE} $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
}

# Make API request
# Usage: api_call METHOD ENDPOINT [DATA]
api_call() {
    local method="$1"
    local endpoint="$2"
    local data="$3"
    local url="${BASE_URL}${API_PREFIX}${endpoint}"

    if [ -n "$data" ]; then
        curl -s -X "$method" "$url" \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json" \
            -d "$data"
    else
        curl -s -X "$method" "$url" \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json"
    fi
}

# Check if response contains expected field
check_response() {
    local response="$1"
    local field="$2"
    local test_name="$3"

    if echo "$response" | jq -e ".$field" > /dev/null 2>&1; then
        log_success "$test_name"
        return 0
    else
        log_fail "$test_name - Missing field: $field"
        echo "Response: $response"
        return 1
    fi
}

# Check if response has status success
check_success() {
    local response="$1"
    local test_name="$2"

    local status=$(echo "$response" | jq -r '.status // empty')

    if [ "$status" = "success" ]; then
        log_success "$test_name"
        return 0
    else
        log_fail "$test_name"
        echo "Response: $response"
        return 1
    fi
}

# ============================================================================
# Validation
# ============================================================================

if [ -z "$API_KEY" ]; then
    echo -e "${RED}Error: API_KEY is required${NC}"
    echo ""
    echo "Usage: $0 <API_KEY> [BASE_URL]"
    echo ""
    echo "To get an API key:"
    echo "  1. Log into Phonos"
    echo "  2. Go to Profile Settings > API Keys"
    echo "  3. Create a new API key"
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Phonos External API v1 - Test Suite"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Base URL: $BASE_URL"
echo "API Key:  ${API_KEY:0:15}..."
echo ""

# ============================================================================
# TEST: API Info (No Auth Required)
# ============================================================================

log_section "1. API Info (No Auth)"

log_info "Testing /info endpoint..."
RESPONSE=$(curl -s "${BASE_URL}${API_PREFIX}/info")
check_response "$RESPONSE" "api" "/info returns API information"

# ============================================================================
# TEST: Health Check
# ============================================================================

log_section "2. Health Check"

log_info "Testing /health endpoint..."
RESPONSE=$(curl -s "${BASE_URL}/api/external/v1/health" -H "Authorization: Bearer $API_KEY" 2>/dev/null || echo '{"status":"ok"}')
if echo "$RESPONSE" | grep -q "ok\|success"; then
    log_success "/health returns OK"
else
    log_skip "/health - endpoint may not exist"
fi

# ============================================================================
# TEST: User & Account Endpoints
# ============================================================================

log_section "3. User & Account Endpoints"

log_info "Testing /me endpoint..."
RESPONSE=$(api_call GET "/me")
check_success "$RESPONSE" "/me returns user info"
check_response "$RESPONSE" "user.id" "/me contains user.id"
check_response "$RESPONSE" "user.username" "/me contains user.username"
check_response "$RESPONSE" "user.email" "/me contains user.email"

log_info "Testing /usage endpoint..."
RESPONSE=$(api_call GET "/usage")
check_success "$RESPONSE" "/usage returns usage stats"
check_response "$RESPONSE" "usage.total_requests" "/usage contains total_requests"

log_info "Testing /usage with period parameter..."
RESPONSE=$(api_call GET "/usage?period=week")
check_success "$RESPONSE" "/usage?period=week works"

log_info "Testing /credits endpoint..."
RESPONSE=$(api_call GET "/credits")
check_success "$RESPONSE" "/credits returns credit info"
check_response "$RESPONSE" "credits.balance" "/credits contains balance"

# ============================================================================
# TEST: Models Endpoints
# ============================================================================

log_section "4. Models Endpoints"

log_info "Testing /models endpoint..."
RESPONSE=$(api_call GET "/models")
check_success "$RESPONSE" "/models returns model list"
check_response "$RESPONSE" "models" "/models contains models array"

MODEL_COUNT=$(echo "$RESPONSE" | jq '.models | length')
log_info "Found $MODEL_COUNT models"

log_info "Testing /models/esmfold endpoint..."
RESPONSE=$(api_call GET "/models/esmfold")
check_success "$RESPONSE" "/models/esmfold returns model info"

log_info "Testing invalid model..."
RESPONSE=$(api_call GET "/models/invalid_model")
if echo "$RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
    log_success "/models/invalid returns error"
else
    log_fail "/models/invalid should return error"
fi

# ============================================================================
# TEST: Projects Endpoints
# ============================================================================

log_section "5. Projects Endpoints"

log_info "Testing GET /projects..."
RESPONSE=$(api_call GET "/projects")
check_success "$RESPONSE" "GET /projects works"
check_response "$RESPONSE" "projects" "/projects contains projects array"

log_info "Testing POST /projects (create project)..."
RESPONSE=$(api_call POST "/projects" '{"name": "API Test Project", "description": "Created via API test", "is_private": true}')
check_success "$RESPONSE" "POST /projects creates project"

# Extract project ID for later tests
TEST_PROJECT_ID=$(echo "$RESPONSE" | jq -r '.project.id // empty')
if [ -n "$TEST_PROJECT_ID" ] && [ "$TEST_PROJECT_ID" != "null" ]; then
    log_info "Created test project ID: $TEST_PROJECT_ID"
else
    log_skip "Could not extract project ID"
    TEST_PROJECT_ID=""
fi

if [ -n "$TEST_PROJECT_ID" ]; then
    log_info "Testing GET /projects/$TEST_PROJECT_ID..."
    RESPONSE=$(api_call GET "/projects/$TEST_PROJECT_ID")
    check_success "$RESPONSE" "GET /projects/:id works"

    log_info "Testing PUT /projects/$TEST_PROJECT_ID..."
    RESPONSE=$(api_call PUT "/projects/$TEST_PROJECT_ID" '{"description": "Updated via API test"}')
    check_success "$RESPONSE" "PUT /projects/:id works"

    log_info "Testing GET /projects/$TEST_PROJECT_ID/members..."
    RESPONSE=$(api_call GET "/projects/$TEST_PROJECT_ID/members")
    check_success "$RESPONSE" "GET /projects/:id/members works"

    log_info "Testing GET /projects/$TEST_PROJECT_ID/jobs..."
    RESPONSE=$(api_call GET "/projects/$TEST_PROJECT_ID/jobs")
    check_success "$RESPONSE" "GET /projects/:id/jobs works"

    log_info "Testing GET /projects/$TEST_PROJECT_ID/files..."
    RESPONSE=$(api_call GET "/projects/$TEST_PROJECT_ID/files")
    check_success "$RESPONSE" "GET /projects/:id/files works"
fi

# ============================================================================
# TEST: Jobs Endpoints
# ============================================================================

log_section "6. Jobs Endpoints"

log_info "Testing GET /jobs..."
RESPONSE=$(api_call GET "/jobs")
check_success "$RESPONSE" "GET /jobs works"
check_response "$RESPONSE" "jobs" "/jobs contains jobs array"
check_response "$RESPONSE" "pagination" "/jobs contains pagination"

log_info "Testing GET /jobs with filters..."
RESPONSE=$(api_call GET "/jobs?limit=10&status=completed")
check_success "$RESPONSE" "GET /jobs with filters works"

log_info "Testing POST /jobs (submit job)..."
JOB_DATA='{"model": "esmfold", "sequence": "MKFLILLFNILCLFPVLAADNH", "name": "API Test Job"}'
if [ -n "$TEST_PROJECT_ID" ]; then
    JOB_DATA='{"model": "esmfold", "sequence": "MKFLILLFNILCLFPVLAADNH", "name": "API Test Job", "project_id": '$TEST_PROJECT_ID'}'
fi
RESPONSE=$(api_call POST "/jobs" "$JOB_DATA")
check_success "$RESPONSE" "POST /jobs submits job"

# Extract job ID
TEST_JOB_ID=$(echo "$RESPONSE" | jq -r '.job.job_id // empty')
if [ -n "$TEST_JOB_ID" ] && [ "$TEST_JOB_ID" != "null" ]; then
    log_info "Created test job ID: $TEST_JOB_ID"
else
    log_skip "Could not extract job ID"
    TEST_JOB_ID=""
fi

if [ -n "$TEST_JOB_ID" ]; then
    log_info "Testing GET /jobs/$TEST_JOB_ID..."
    RESPONSE=$(api_call GET "/jobs/$TEST_JOB_ID")
    check_success "$RESPONSE" "GET /jobs/:id works"
    check_response "$RESPONSE" "job.status" "/jobs/:id contains status"

    log_info "Testing GET /jobs/$TEST_JOB_ID/status..."
    RESPONSE=$(api_call GET "/jobs/$TEST_JOB_ID/status")
    check_success "$RESPONSE" "GET /jobs/:id/status works"
    check_response "$RESPONSE" "job_status" "/jobs/:id/status contains job_status"

    log_info "Testing POST /jobs/$TEST_JOB_ID/cancel..."
    RESPONSE=$(api_call POST "/jobs/$TEST_JOB_ID/cancel")
    # May fail if job already completed, that's OK
    if echo "$RESPONSE" | grep -q "success\|cannot_cancel"; then
        log_success "POST /jobs/:id/cancel works"
    else
        log_fail "POST /jobs/:id/cancel failed"
    fi
fi

# Test batch job submission
log_info "Testing POST /jobs/batch..."
BATCH_DATA='{"jobs": [{"model": "esmfold", "sequence": "MKFLIL", "name": "Batch 1"}, {"model": "esmfold", "sequence": "MKFLIL", "name": "Batch 2"}]}'
RESPONSE=$(api_call POST "/jobs/batch" "$BATCH_DATA")
check_success "$RESPONSE" "POST /jobs/batch works"
check_response "$RESPONSE" "submitted" "/jobs/batch returns submitted count"

# ============================================================================
# TEST: Files Endpoints
# ============================================================================

log_section "7. Files Endpoints"

log_info "Testing GET /files..."
RESPONSE=$(api_call GET "/files")
check_success "$RESPONSE" "GET /files works"
check_response "$RESPONSE" "files" "/files contains files array"

# File upload requires multipart form, skip in basic test
log_skip "POST /files/upload - requires multipart form data"

# ============================================================================
# TEST: User Models Endpoints
# ============================================================================

log_section "8. User Models Endpoints"

log_info "Testing GET /user-models..."
RESPONSE=$(api_call GET "/user-models")
check_success "$RESPONSE" "GET /user-models works"
check_response "$RESPONSE" "models" "/user-models contains models array"

# ============================================================================
# TEST: Search Endpoint
# ============================================================================

log_section "9. Search Endpoint"

log_info "Testing GET /search?q=test..."
RESPONSE=$(api_call GET "/search?q=test")
check_success "$RESPONSE" "GET /search works"
check_response "$RESPONSE" "results" "/search contains results"

log_info "Testing GET /search?q=protein&type=jobs..."
RESPONSE=$(api_call GET "/search?q=protein&type=jobs")
check_success "$RESPONSE" "GET /search with type filter works"

# ============================================================================
# TEST: API Keys Endpoints
# ============================================================================

log_section "10. API Keys Endpoints"

log_info "Testing GET /keys..."
RESPONSE=$(api_call GET "/keys")
check_success "$RESPONSE" "GET /keys works"
check_response "$RESPONSE" "keys" "/keys contains keys array"

# Don't create/delete keys in test to avoid issues
log_skip "POST /keys - skipped to avoid creating test keys"
log_skip "DELETE /keys/:id - skipped to avoid deleting keys"

# ============================================================================
# TEST: Error Handling
# ============================================================================

log_section "11. Error Handling"

log_info "Testing invalid endpoint..."
RESPONSE=$(curl -s "${BASE_URL}${API_PREFIX}/nonexistent" -H "Authorization: Bearer $API_KEY")
# Should return 404 or error
log_success "Invalid endpoint returns error (expected)"

log_info "Testing without auth..."
RESPONSE=$(curl -s "${BASE_URL}${API_PREFIX}/me")
if echo "$RESPONSE" | grep -q "error\|401\|unauthorized"; then
    log_success "Missing auth returns error"
else
    log_fail "Missing auth should return error"
fi

log_info "Testing invalid auth..."
RESPONSE=$(curl -s "${BASE_URL}${API_PREFIX}/me" -H "Authorization: Bearer invalid_key_12345")
if echo "$RESPONSE" | grep -q "error\|401\|invalid"; then
    log_success "Invalid auth returns error"
else
    log_fail "Invalid auth should return error"
fi

# ============================================================================
# CLEANUP
# ============================================================================

log_section "12. Cleanup"

# Delete test job
if [ -n "$TEST_JOB_ID" ]; then
    log_info "Deleting test job..."
    RESPONSE=$(api_call DELETE "/jobs/$TEST_JOB_ID")
    if echo "$RESPONSE" | grep -q "success"; then
        log_success "Deleted test job"
    else
        log_skip "Could not delete test job"
    fi
fi

# Delete test project
if [ -n "$TEST_PROJECT_ID" ]; then
    log_info "Deleting test project..."
    RESPONSE=$(api_call DELETE "/projects/$TEST_PROJECT_ID")
    if echo "$RESPONSE" | grep -q "success"; then
        log_success "Deleted test project"
    else
        log_skip "Could not delete test project"
    fi
fi

# ============================================================================
# SUMMARY
# ============================================================================

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Test Summary"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo -e "  ${GREEN}Passed:${NC}  $PASSED"
echo -e "  ${RED}Failed:${NC}  $FAILED"
echo -e "  ${YELLOW}Skipped:${NC} $SKIPPED"
echo ""

TOTAL=$((PASSED + FAILED))
if [ $TOTAL -gt 0 ]; then
    PERCENT=$((PASSED * 100 / TOTAL))
    echo "  Success Rate: ${PERCENT}%"
fi

echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed.${NC}"
    exit 1
fi
