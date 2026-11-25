#!/bin/bash
# Enhanced Phonos Worker Deployment Script with New NVIDIA API Workers Support
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

# Worker configuration with new NVIDIA API workers
declare -A WORKER_CONFIGS
WORKER_CONFIGS=(
    ["type-0"]="esmfold|phonos-esmfold-worker:ESMFold Worker|single|1|8|true"
    ["type-2"]="alphafold|phonos-alphafold-worker:AlphaFold2 Worker|single|2|16|true"
    ["type-3"]="diffdock|phonos-diffdock-worker:DiffDock Worker|single|1|8|true"
    ["type-5"]="folddock|phonos-folddock-worker:Fold+Dock Worker|single|1|16|true"
    ["type-6"]="metal3d|phonos-metal3d-worker:Metal3D Worker|single|1|8|true"
    ["type-8"]="evo2|phonos-evo2-worker:Evo2 Worker|single|1|4|false"
    ["type-9"]="boltz2|phonos-boltz2-worker:Boltz2 Worker|single|1|12|true"
    ["alphafold2-re"]="phonos-alphafold2-re-worker:AlphaFold2-RE Worker|single|2|8|true"
    ["alphafold2-re-multimer"]="phonos-alphafold2-re-multimer-worker:AlphaFold2-Multimer Worker|single|3|16|true"
    ["boltz2-enhanced"]="phonos-boltz2-enhanced-worker:Boltz2-Enhanced Worker|single|4|12|true"
)

# New worker types for enhanced deployment
NEW_WORKERS=(
    "alphafold2-re"
    "alphafold2-re-multimer" 
    "boltz2-enhanced"
)

# Detect GPU configuration
detect_gpus() {
    if ! command -v nvidia-smi &> /dev/null; then
        echo "0"
        return
    fi
    
    GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
    echo "$GPU_COUNT"
}

# Get available memory (in GB)
get_available_memory() {
    # Check total system memory
    if command -v free &> /dev/null; then
        TOTAL_MEM=$(free -g | awk '/^Mem:/{print $2}')
        echo $((TOTAL_MEM / 1024))  # Convert to GB
    else
        echo "8"  # Default fallback
    fi
}

# Verify prerequisites
check_prerequisites() {
    log_step "Checking prerequisites..."
    
    # Check Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed or not in PATH"
        exit 1
    fi
    
    # Check Docker Compose
    if ! command -v docker-compose &> /dev/null; then
        log_error "Docker Compose is not installed or not in PATH"
        exit 1
    fi
    
    # Check NVIDIA API key
    if [ -z "$NVIDIA_API_KEY" ]; then
        if [ -f "$ROOT_DIR/.env" ]; then
            source "$ROOT_DIR/.env"
        fi
        
        if [ -z "$NVIDIA_API_KEY" ]; then
            log_error "NVIDIA_API_KEY environment variable is not set"
            echo "Please set it in ~/.bashrc or .env file"
            echo "Example: export NVIDIA_API_KEY='nvapi-your-key-here'"
            exit 1
        fi
    fi
    
    log_info "Prerequisites check passed"
}

# Build Docker images for enhanced workers
build_images() {
    log_step "Building Docker images..."
    
    cd "$DEPLOY_DIR"
    
    # Build existing workers
    log_info "Building ESMFold worker image..."
    docker build -t phonos/esmfold-worker:latest "$ROOT_DIR/node1-esm"
    
    log_info "Building AlphaFold2 worker image..."
    docker build -t phonos/alphafold-worker:latest "$ROOT_DIR/phonos-main/phonos_app"
    
    log_info "Building DiffDock worker image..."
    docker build -t phonos/diffdock-worker:latest "$ROOT_DIR/diffdock"
    
    log_info "Building Boltz2 worker image..."
    docker build -t phonos/boltz2-worker:latest "$ROOT_DIR/production"
    
    # Build new workers
    log_info "Building AlphaFold2-RE worker image..."
    if [ -f "$ROOT_DIR/production/alphafold2_re_worker.py" ]; then
        cat > "$DEPLOY_DIR/Dockerfile.alphafold2-re" << 'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY phonos-main/ requirements.txt .
RUN pip install -r requirements.txt
COPY phonos-main /app
WORKDIR /app/production
COPY production/alphafold2_re_worker.py .
COPY production/node_config_*.json .
COPY production/.env* . 2>/dev/null || true
EXPOSE 65433
CMD ["python3", "alphafold2_re_worker.py"]
EOF
        docker build -t phonos/alphafold2-re-worker:latest -f Dockerfile.alphafold2-re .
    fi
    
    log_info "Building AlphaFold2-RE-Multimer worker image..."
    if [ -f "$ROOT_DIR/production/alphafold2_re_multimer_worker.py" ]; then
        cat > "$DEPLOY_DIR/Dockerfile.alphafold2-re-multimer" << 'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY phonos-main/ requirements.txt .
RUN pip install -r requirements.txt
COPY phonos-main /app
WORKDIR /app/production
COPY production/alphafold2_re_multimer_worker.py .
COPY production/node_config_*.json .
COPY production/.env* . 2>/dev/null || true
EXPOSE 65435
CMD ["python3", "alphafold2_re_multimer_worker.py"]
EOF
        docker build -t phonos/alphafold2-re-multimer-worker:latest -f Dockerfile.alphafold2-re-multimer .
    fi
    
    log_info "Building Boltz2-Enhanced worker image..."
    if [ -f "$ROOT_DIR/production/boltz2_enhanced_worker.py" ]; then
        cat > "$DEPLOY_DIR/Dockerfile.boltz2-enhanced" << 'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY phonos-main/ requirements.txt .
RUN pip install -r requirements.txt
COPY phonos-main /app
WORKDIR /app/production
COPY production/boltz2_enhanced_worker.py .
COPY production/node_config_*.json .
COPY production/.env* . 2>/dev/null || true
EXPOSE 65436
CMD ["python3", "boltz2_enhanced_worker.py"]
EOF
        docker build -t phonos/boltz2-enhanced-worker:latest -f Dockerfile.boltz2-enhanced .
    fi
    
    log_info "All Docker images built successfully"
}

# Deploy enhanced workers
deploy_enhanced_workers() {
    MODE=$1
    AVAILABLE_MEMORY=$(get_available_memory)
    
    log_step "Deploying enhanced workers in $MODE mode..."
    
    cd "$DEPLOY_DIR"
    
    # Create enhanced docker-compose file
    cat > docker-compose.enhanced.yml << EOF
version: '3.8'
services:
  # AlphaFold2-RE Worker
  alphafold2-re-worker:
    build:
      context: .
      dockerfile: Dockerfile.alphafold2-re
    image: phonos/alphafold2-re-worker:latest
    container_name: phonos-alphafold2-re-worker
    environment:
      - NVIDIA_API_KEY=\${NVIDIA_API_KEY}
      - REDIS_HOST=\${REDIS_HOST:-localhost}
      - REDIS_PORT=\${REDIS_PORT:-6379}
      - REDIS_DB=\${REDIS_DB:-0}
      - REDIS_PASSWORD=\${REDIS_PASSWORD}
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 8G
          cpus: '4.0'
        reservations:
          memory: 4G
          cpus: '2.0'

  # AlphaFold2-RE-Multimer Worker
  alphafold2-re-multimer-worker:
    build:
      context: .
      dockerfile: Dockerfile.alphafold2-re-multimer
    image: phonos/alphafold2-re-multimer-worker:latest
    container_name: phonos-alphafold2-re-multimer-worker
    environment:
      - NVIDIA_API_KEY=\${NVIDIA_API_KEY}
      - REDIS_HOST=\${REDIS_HOST:-localhost}
      - REDIS_PORT=\${REDIS_PORT:-6379}
      - REDIS_DB=\${REDIS_DB:-0}
      - REDIS_PASSWORD=\${REDIS_PASSWORD}
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 16G
          cpus: '6.0'
        reservations:
          memory: 8G
          cpus: '3.0'

  # Boltz2-Enhanced Worker
  boltz2-enhanced-worker:
    build:
      context: .
      dockerfile: Dockerfile.boltz2-enhanced
    image: phonos/boltz2-enhanced-worker:latest
    container_name: phonos-boltz2-enhanced-worker
    environment:
      - NVIDIA_API_KEY=\${NVIDIA_API_KEY}
      - REDIS_HOST=\${REDIS_HOST:-localhost}
      - REDIS_PORT=\${REDIS_PORT:-6379}
      - REDIS_DB=\${REDIS_DB:-0}
      - REDIS_PASSWORD=\${REDIS_PASSWORD}
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 12G
          cpus: '4.0'
        reservations:
          memory: 6G
          cpus: '2.0'

EOF

    # Deployment based on available resources
    if [ "$AVAILABLE_MEMORY" -ge 32 ]; then
        log_info "High memory system (32GB+) - deploying all enhanced workers"
        docker-compose -f docker-compose.enhanced.yml up -d
        
        echo "Deployed workers:"
        echo "  - AlphaFold2-RE (8GB RAM, 4 CPU)"
        echo "  - AlphaFold2-RE-Multimer (16GB RAM, 6 CPU)" 
        echo "  - Boltz2-Enhanced (12GB RAM, 4 CPU)"
        
    elif [ "$AVAILABLE_MEMORY" -ge 16 ]; then
        log_info "Medium memory system (16-31GB) - deploying select workers"
        echo "Available memory: ${AVAILABLE_MEMORY}GB"
        read -p "Select workers to deploy (space-separated): " WORKER_SELECTION
        
        IFS=',' read -ra WORKERS <<< "$WORKER_SELECTION"
        
        for worker in "${WORKERS[@]}"; do
            worker=$(echo "$worker" | xargs)  # Trim whitespace
            case $worker in
                "alphafold2-re")
                    log_info "Deploying AlphaFold2-RE worker..."
                    docker-compose -f docker-compose.enhanced.yml up -d alphafold2-re-worker
                    ;;
                "alphafold2-re-multimer")
                    log_info "Deploying AlphaFold2-RE-Multimer worker..."
                    docker-compose -f docker-compose.enhanced.yml up -d alphafold2-re-multimer-worker
                    ;;
                "boltz2-enhanced")
                    log_info "Deploying Boltz2-Enhanced worker..."
                    docker-compose -f docker-compose.enhanced.yml up -d boltz2-enhanced-worker
                    ;;
                *)
                    log_warn "Unknown worker: $worker, skipping"
                    ;;
            esac
        done
        
    else
        log_warn "Low memory system (<16GB). Recommended minimum 16GB for enhanced workers"
        echo "Available memory: ${AVAILABLE_MEMORY}GB"
        read -p "Deploy anyway? (y/N): " CONFIRM
        
        if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
            log_info "Deploying with limited resources..."
            # Deploy with reduced resource limits
            sed -i 's/memory: [0-9]*G/memory: 4G/g' docker-compose.enhanced.yml
            sed -i 's/cpus: .[0-9]*/cpus: '\''2.0'\''/g' docker-compose.enhanced.yml
            docker-compose -f docker-compose.enhanced.yml up -d
        else
            log_info "Deployment cancelled"
        fi
    fi
}

# Check worker status
check_enhanced_status() {
    log_step "Checking enhanced worker status..."
    
    echo "Enhanced Workers:"
    echo "=================="
    
    # Check Docker containers
    if command -v docker &> /dev/null; then
        for worker in "${NEW_WORKERS[@]}"; do
            case $worker in
                "alphafold2-re")
                    if docker ps --filter "name=phonos-alphafold2-re-worker" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -q "Up"; then
                        echo "✓ AlphaFold2-RE: Running"
                    else
                        echo "✗ AlphaFold2-RE: Stopped"
                    fi
                    ;;
                "alphafold2-re-multimer")
                    if docker ps --filter "name=phonos-alphafold2-re-multimer-worker" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -q "Up"; then
                        echo "✓ AlphaFold2-RE-Multimer: Running"
                    else
                        echo "✗ AlphaFold2-RE-Multimer: Stopped"
                    fi
                    ;;
                "boltz2-enhanced")
                    if docker ps --filter "name=phonos-boltz2-enhanced-worker" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -q "Up"; then
                        echo "✓ Boltz2-Enhanced: Running"
                    else
                        echo "✗ Boltz2-Enhanced: Stopped"
                    fi
                    ;;
            esac
        done
    else
        log_error "Docker is not available"
    fi
}

# Enhanced deployment mode selection
select_enhanced_deployment() {
    echo ""
    echo "Enhanced Deployment Options:"
    echo "1) High Performance (32GB+ RAM) - All workers"
    echo "2) Standard (16-31GB RAM) - Select workers"
    echo "3) Minimal (<16GB RAM) - Limited workers"
    echo ""
    read -p "Select deployment mode [1-3]: " CHOICE
    
    case $CHOICE in
        1)
            return "high"
            ;;
        2)
            return "standard"
            ;;
        3)
            return "minimal"
            ;;
        *)
            log_warn "Invalid choice, using standard mode"
            return "standard"
            ;;
    esac
}

# Main deployment function with enhanced worker support
deploy_workers() {
    MODE=$1
    
    case $MODE in
        auto)
            DEPLOYMENT_MODE=$(select_enhanced_deployment)
            ;;
        high|standard|minimal)
            DEPLOYMENT_MODE=$MODE
            ;;
        *)
            log_error "Unknown deployment mode: $MODE"
            echo "Valid modes: auto, high, standard, minimal, enhanced"
            exit 1
            ;;
    esac
    
    if [[ "$DEPLOYMENT_MODE" == "high" || "$DEPLOYMENT_MODE" == "standard" || "$DEPLOYMENT_MODE" == "minimal" ]]; then
        deploy_enhanced_workers "$DEPLOYMENT_MODE"
    else
        # Fallback to original deployment for legacy workers
        log_info "Using legacy deployment mode..."
        # Call original deployment logic here
        cd "$DEPLOY_DIR/docker"
        
        GPU_COUNT=$(detect_gpus)
        
        if [ "$GPU_COUNT" -ge 2 ]; then
            COMPOSE_FILE="docker-compose.yml"
        elif [ "$GPU_COUNT" -eq 1 ]; then
            COMPOSE_FILE="docker-compose.single-gpu.yml"
        else
            COMPOSE_FILE="docker-compose.cpu-only.yml"
        fi
        
        # Build images and deploy
        build_images
        docker-compose -f "$COMPOSE_FILE" up -d
        
        echo "Legacy deployment complete with GPU config: $GPU_COUNT"
    fi
}

# Show enhanced help
show_enhanced_help() {
    echo ""
    echo "Enhanced Phonos Worker Deployment with NVIDIA API Workers"
    echo "======================================================"
    echo ""
    echo "New Workers Available:"
    echo "  • AlphaFold2-RE - Enhanced protein structure prediction"
    echo "  • AlphaFold2-RE-Multimer - Multi-protein complex prediction"
    echo "  • Boltz2-Enhanced - Advanced biomolecular prediction with ensemble"
    echo ""
    echo "Deployment Modes:"
    echo "  deploy     - Deploy workers (auto-detects optimal configuration)"
    echo "  deploy high - Deploy all enhanced workers (requires 32GB+ RAM)"
    echo "  deploy standard - Deploy selected workers (requires 16GB+ RAM)"
    echo "  deploy minimal - Deploy with limited resources (<16GB RAM)"
    echo "  status     - Show worker status"
    echo "  stop       - Stop all enhanced workers"
    echo "  logs       - View worker logs"
    echo "  restart    - Restart workers"
    echo ""
    echo "Examples:"
    echo "  $0 deploy                    # Auto-deploy based on resources"
    echo "  $0 deploy standard            # Deploy with worker selection"
    echo "  $0 deploy alphafold2-re  # Deploy specific worker"
    echo "  $0 status                  # Check all workers"
    echo ""
    echo "Environment Variables:"
    echo "  NVIDIA_API_KEY - Your NVIDIA API key"
    echo "  REDIS_HOST - Redis server (default: localhost)"
    echo "  REDIS_PORT - Redis port (default: 6379)"
}

# Enhanced stop function
stop_enhanced_workers() {
    log_step "Stopping enhanced workers..."
    cd "$DEPLOY_DIR"
    
    # Stop enhanced workers
    for worker in "${NEW_WORKERS[@]}"; do
        case $worker in
            "alphafold2-re")
                docker-compose -f docker-compose.enhanced.yml stop alphafold2-re-worker || true
                ;;
            "alphafold2-re-multimer")
                docker-compose -f docker-compose.enhanced.yml stop alphafold2-re-multimer-worker || true
                ;;
            "boltz2-enhanced")
                docker-compose -f docker-compose.enhanced.yml stop boltz2-enhanced-worker || true
                ;;
        esac
    done
    
    log_info "Enhanced workers stopped"
}

# Enhanced logs function
logs_enhanced_workers() {
    WORKER=${2:-all}
    
    for worker in "${NEW_WORKERS[@]}"; do
        if [ "$WORKER" == "all" ] || [ "$WORKER" == "$worker" ]; then
            case $worker in
                "alphafold2-re")
                    docker logs -f --tail 100 phonos-alphafold2-re-worker
                    ;;
                "alphafold2-re-multimer")
                    docker logs -f --tail 100 phonos-alphafold2-re-multimer-worker
                    ;;
                "boltz2-enhanced")
                    docker logs -f --tail 100 phonos-boltz2-enhanced-worker
                    ;;
            esac
        fi
    done
}

# Enhanced restart function
restart_enhanced_workers() {
    log_step "Restarting enhanced workers..."
    stop_enhanced_workers
    sleep 5
    deploy_enhanced_workers standard
}

# Main function
main() {
    # Source environment if available
    if [ -f "$ROOT_DIR/.env" ]; then
        source "$ROOT_DIR/.env"
    fi
    
    COMMAND=${1:-deploy}
    
    case $COMMAND in
        deploy)
            check_prerequisites
            deploy_workers "${2:-auto}"
            check_enhanced_status
            ;;
        status)
            check_enhanced_status
            ;;
        stop)
            stop_enhanced_workers
            ;;
        logs)
            logs_enhanced_workers "${2:-all}"
            ;;
        restart)
            restart_enhanced_workers
            ;;
        help|--help|-h)
            show_enhanced_help
            ;;
        *)
            show_enhanced_help
            echo ""
            log_error "Unknown command: $COMMAND"
            exit 1
            ;;
    esac
}

main "$@"
