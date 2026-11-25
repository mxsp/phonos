#!/bin/bash
#
# Deploy File System Security & Performance Improvements
# Run this script to apply all migrations and verify the deployment
#
# Usage: ./apply_file_system_improvements.sh [--skip-backup] [--dry-run]
#

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHONOS_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
PRODUCTION_DIR="$PHONOS_DIR/production"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse arguments
SKIP_BACKUP=false
DRY_RUN=false

for arg in "$@"; do
    case $arg in
        --skip-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: $0 [--skip-backup] [--dry-run]"
            exit 1
            ;;
    esac
done

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}File System Improvements Deployment${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Check if we're in dry-run mode
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}⚠ DRY RUN MODE - No changes will be made${NC}"
    echo ""
fi

# Step 1: Database Backup
if [ "$SKIP_BACKUP" = false ] && [ "$DRY_RUN" = false ]; then
    echo -e "${YELLOW}[1/7] Creating database backup...${NC}"

    BACKUP_FILE="$PRODUCTION_DIR/backups/phonos_backup_$(date +%Y%m%d_%H%M%S).sql"
    mkdir -p "$PRODUCTION_DIR/backups"

    if [ -z "$DB_USER" ]; then
        read -p "Enter PostgreSQL username: " DB_USER
    fi

    if [ -z "$DB_NAME" ]; then
        DB_NAME="phonos_db"
    fi

    echo "Creating backup: $BACKUP_FILE"
    PGPASSWORD="${DB_PASSWORD}" pg_dump -U "$DB_USER" "$DB_NAME" > "$BACKUP_FILE"

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Backup created successfully${NC}"
    else
        echo -e "${RED}✗ Backup failed${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}[1/7] Skipping database backup${NC}"
fi
echo ""

# Step 2: Apply Database Migrations
echo -e "${YELLOW}[2/7] Applying database migrations...${NC}"

if [ "$DRY_RUN" = false ]; then
    # Migration 002: File ownership validation
    echo "  Applying migration 002 (file ownership validation)..."
    PGPASSWORD="${DB_PASSWORD}" psql -U "$DB_USER" -d "$DB_NAME" -f "$PRODUCTION_DIR/migrations/002_file_ownership_validation.sql"

    # Migration 003: Job file validation
    echo "  Applying migration 003 (job file validation)..."
    PGPASSWORD="${DB_PASSWORD}" psql -U "$DB_USER" -d "$DB_NAME" -f "$PRODUCTION_DIR/migrations/003_job_file_validation.sql"

    # Migration 004: Performance indexes
    echo "  Applying migration 004 (performance indexes)..."
    PGPASSWORD="${DB_PASSWORD}" psql -U "$DB_USER" -d "$DB_NAME" -f "$PRODUCTION_DIR/migrations/004_file_performance_indexes.sql"

    echo -e "${GREEN}✓ All migrations applied${NC}"
else
    echo -e "  ${BLUE}Would apply:${NC}"
    echo "    - 002_file_ownership_validation.sql"
    echo "    - 003_job_file_validation.sql"
    echo "    - 004_file_performance_indexes.sql"
fi
echo ""

# Step 3: Verify Indexes
echo -e "${YELLOW}[3/7] Verifying database indexes...${NC}"

if [ "$DRY_RUN" = false ]; then
    PGPASSWORD="${DB_PASSWORD}" psql -U "$DB_USER" -d "$DB_NAME" -c "
    SELECT indexname, tablename
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename IN ('files', 'jobs', 'projects')
      AND indexname LIKE 'idx_%'
    ORDER BY tablename, indexname;
    " | head -20

    echo -e "${GREEN}✓ Indexes verified${NC}"
else
    echo "  Would verify indexes in database"
fi
echo ""

# Step 4: Check Application Files
echo -e "${YELLOW}[4/7] Checking application files...${NC}"

FILES_TO_CHECK=(
    "phonos_app/helpers/projects.py"
    "phonos_app/helpers/paths.py"
    "phonos_app/helpers/team_migration.py"
    "phonos_app/routes/api_files.py"
    "production/cleanup_orphaned_files.py"
    "static/js/structure_viewer_enhanced.js"
)

for file in "${FILES_TO_CHECK[@]}"; do
    if [ -f "$PHONOS_DIR/$file" ]; then
        echo -e "  ${GREEN}✓${NC} $file"
    else
        echo -e "  ${RED}✗${NC} $file (missing)"
    fi
done
echo ""

# Step 5: Run Orphaned File Check
echo -e "${YELLOW}[5/7] Checking for orphaned files...${NC}"

if [ "$DRY_RUN" = false ]; then
    if [ -f "$PRODUCTION_DIR/cleanup_orphaned_files.py" ]; then
        python3 "$PRODUCTION_DIR/cleanup_orphaned_files.py" --dry-run | tail -20
    else
        echo "  Cleanup script not found, skipping"
    fi
else
    echo "  Would run: python3 cleanup_orphaned_files.py --dry-run"
fi
echo ""

# Step 6: Restart Application
echo -e "${YELLOW}[6/7] Restarting application...${NC}"

if [ "$DRY_RUN" = false ]; then
    if systemctl is-active --quiet phonos; then
        echo "  Restarting phonos service..."
        sudo systemctl restart phonos
        echo -e "${GREEN}✓ Service restarted${NC}"
    elif systemctl is-active --quiet gunicorn; then
        echo "  Restarting gunicorn service..."
        sudo systemctl restart gunicorn
        echo -e "${GREEN}✓ Service restarted${NC}"
    else
        echo -e "${YELLOW}⚠ No systemd service found, restart manually${NC}"
    fi
else
    echo "  Would restart application service"
fi
echo ""

# Step 7: Run Tests
echo -e "${YELLOW}[7/7] Running tests...${NC}"

if [ "$DRY_RUN" = false ]; then
    if [ -d "$PHONOS_DIR/tests" ]; then
        echo "  Running security tests..."
        python3 -m pytest "$PHONOS_DIR/tests/test_file_security.py" -v --tb=short || true

        echo ""
        echo "  Running integration tests..."
        python3 -m pytest "$PHONOS_DIR/tests/test_file_integration.py" -v --tb=short || true
    else
        echo -e "${YELLOW}⚠ Tests directory not found, skipping${NC}"
    fi
else
    echo "  Would run pytest test suite"
fi
echo ""

# Summary
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Deployment Summary${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}This was a DRY RUN. No changes were made.${NC}"
    echo ""
    echo "To apply changes, run:"
    echo "  $0"
    echo ""
else
    echo -e "${GREEN}✓ Database migrations applied${NC}"
    echo -e "${GREEN}✓ Application restarted${NC}"
    echo -e "${GREEN}✓ Deployment complete!${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Monitor logs: sudo journalctl -u phonos -f"
    echo "  2. Check cache stats: python3 -c 'from phonos_app.helpers.paths import get_path_cache_stats; print(get_path_cache_stats())'"
    echo "  3. Review summary: cat FILE_SYSTEM_IMPROVEMENTS_SUMMARY.md"
    echo ""
    echo "If issues occur, see rollback instructions in:"
    echo "  FILE_SYSTEM_IMPROVEMENTS_SUMMARY.md"
fi
echo ""
