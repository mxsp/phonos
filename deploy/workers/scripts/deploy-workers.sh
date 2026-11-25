#!/bin/bash
# Phonos Worker Deployment Script
# Automatically detects GPU configuration and deploys workers appropriately

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
ROOT_DIR="$(dirname "$(dirname "$DEPLOY_DIR")")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

# Detect GPU configuration
detect_gpus() {
    if ! command -v nvidia-smi &> /dev/null; then
        echo "0"
        return
    fi

    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
    echo "$GPU_COUNT"
}

# Verify prerequisites
check_prerequisites() {
    log_step "Checking prerequisites..."

    # Check Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        exit 1
    fi

    # Check Docker Compose
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        log_error "Docker Compose is not installed. Please install Docker Compose first."
        exit 1
    fi

    # Check for .env file
    if [ ! -f "$DEPLOY_DIR/docker/.env" ]; then
        if [ -f "$DEPLOY_DIR/docker/.env.example" ]; then
            log_warn ".env file not found. Copying from .env.example..."
            cp "$DEPLOY_DIR/docker/.env.example" "$DEPLOY_DIR/docker/.env"
            log_warn "Please edit $DEPLOY_DIR/docker/.env with your configuration"
            exit 1
        else
            log_error ".env file not found and no .env.example available"
            exit 1
        fi
    fi

    # Verify required environment variables
    source "$DEPLOY_DIR/docker/.env"

    if [ -z "$REDIS_HOST" ] || [ "$REDIS_HOST" = "your-redis-password-from-main-server" ]; then
        log_error "REDIS_HOST not configured in .env"
        exit 1
    fi

    if [ -z "$REDIS_PASSWORD" ] || [ "$REDIS_PASSWORD" = "your-redis-password-from-main-server" ]; then
        log_error "REDIS_PASSWORD not configured in .env"
        exit 1
    fi

    if [ -z "$ENCRYPTION_KEY" ] || [ "$ENCRYPTION_KEY" = "your-encryption-key-from-main-server" ]; then
        log_error "ENCRYPTION_KEY not configured in .env"
        exit 1
    fi

    log_info "Prerequisites check passed"
}

# Select deployment mode
select_deployment_mode() {
    GPU_COUNT=$(detect_gpus)

    if [ "$GPU_COUNT" -ge 2 ]; then
        log_info "Detected $GPU_COUNT GPUs - using dual GPU configuration"
        echo "dual"
    elif [ "$GPU_COUNT" -eq 1 ]; then
        log_info "Detected 1 GPU - using single GPU configuration"
        echo "single"
    else
        log_warn "No GPU detected - using CPU-only configuration"
        echo "cpu"
    fi
}

# Build Docker images
build_images() {
    log_step "Building Docker images..."

    cd "$DEPLOY_DIR/docker"

    # Build ESMFold worker
    log_info "Building ESMFold worker image..."
    docker build -t phonos/esmfold-worker:latest "$ROOT_DIR/node1-esm"

    # Build DiffDock worker
    log_info "Building DiffDock worker image..."
    docker build -t phonos/diffdock-worker:latest "$ROOT_DIR/diffdock"

    log_info "Docker images built successfully"
}

# Deploy workers
deploy_workers() {
    MODE=$1

    log_step "Deploying workers in $MODE mode..."

    cd "$DEPLOY_DIR/docker"

    case $MODE in
        dual)
            COMPOSE_FILE="docker-compose.yml"
            ;;
        single)
            COMPOSE_FILE="docker-compose.single-gpu.yml"
            ;;
        cpu)
            COMPOSE_FILE="docker-compose.cpu-only.yml"
            ;;
        *)
            log_error "Unknown deployment mode: $MODE"
            exit 1
            ;;
    esac

    # Stop existing workers
    log_info "Stopping existing workers..."
    docker-compose -f "$COMPOSE_FILE" down 2>/dev/null || true

    # Start workers
    log_info "Starting workers..."
    docker-compose -f "$COMPOSE_FILE" up -d

    log_info "Workers deployed successfully"
}

# Check worker status
check_status() {
    log_step "Checking worker status..."

    cd "$DEPLOY_DIR/docker"

    echo ""
    docker ps --filter "name=phonos-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    echo ""

    # Check container logs for errors
    for container in phonos-esmfold-worker phonos-diffdock-worker; do
        if docker ps -q -f name=$container | grep -q .; then
            ERRORS=$(docker logs --tail 10 $container 2>&1 | grep -i error || true)
            if [ -n "$ERRORS" ]; then
                log_warn "Errors in $container:"
                echo "$ERRORS"
            fi
        fi
    done
}

# Main execution
main() {
    echo ""
    echo "========================================"
    echo "   Phonos Worker Deployment Script"
    echo "========================================"
    echo ""

    # Parse arguments
    ACTION=${1:-deploy}
    MODE=${2:-auto}

    case $ACTION in
        deploy)
            check_prerequisites

            if [ "$MODE" = "auto" ]; then
                MODE=$(select_deployment_mode)
            fi

            build_images
            deploy_workers "$MODE"
            check_status

            echo ""
            log_info "Deployment complete!"
            echo ""
            echo "GPU Configuration:"
            if [ "$MODE" = "dual" ]; then
                echo "  - ESMFold: GPU 0"
                echo "  - DiffDock: GPU 1"
            elif [ "$MODE" = "single" ]; then
                echo "  - Both workers: GPU 0"
            else
                echo "  - Both workers: CPU only"
            fi
            echo ""
            echo "View logs:"
            echo "  docker logs -f phonos-esmfold-worker"
            echo "  docker logs -f phonos-diffdock-worker"
            ;;

        stop)
            log_step "Stopping workers..."
            cd "$DEPLOY_DIR/docker"
            docker-compose down 2>/dev/null || true
            docker-compose -f docker-compose.single-gpu.yml down 2>/dev/null || true
            docker-compose -f docker-compose.cpu-only.yml down 2>/dev/null || true
            log_info "Workers stopped"
            ;;

        status)
            check_status
            ;;

        logs)
            WORKER=${2:-all}
            if [ "$WORKER" = "all" ]; then
                docker logs -f --tail 100 phonos-esmfold-worker &
                docker logs -f --tail 100 phonos-diffdock-worker
            else
                docker logs -f --tail 100 "phonos-$WORKER-worker"
            fi
            ;;

        *)
            echo "Usage: $0 [deploy|stop|status|logs] [dual|single|cpu|auto]"
            echo ""
            echo "Actions:"
            echo "  deploy  - Deploy workers (default)"
            echo "  stop    - Stop all workers"
            echo "  status  - Check worker status"
            echo "  logs    - View worker logs"
            echo ""
            echo "Modes:"
            echo "  auto    - Auto-detect GPU configuration (default)"
            echo "  dual    - Use dual GPU (ESMFold=GPU0, DiffDock=GPU1)"
            echo "  single  - Use single GPU for both workers"
            echo "  cpu     - CPU-only mode"
            exit 1
            ;;
    esac
}

main "$@"
