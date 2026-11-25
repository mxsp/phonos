#!/bin/bash
# Phonos Kubernetes Worker Deployment Script
# Deploy workers to a Kubernetes cluster with GPU support

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$(dirname "$SCRIPT_DIR")/kubernetes"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

# Check prerequisites
check_prerequisites() {
    log_step "Checking prerequisites..."

    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl is not installed"
        exit 1
    fi

    if ! kubectl cluster-info &> /dev/null; then
        log_error "Cannot connect to Kubernetes cluster"
        exit 1
    fi

    # Check for kustomize (optional but recommended)
    if command -v kustomize &> /dev/null; then
        USE_KUSTOMIZE=true
        log_info "Using kustomize for deployment"
    else
        USE_KUSTOMIZE=false
        log_warn "kustomize not found, using kubectl apply directly"
    fi

    log_info "Prerequisites check passed"
}

# Configure secrets interactively
configure_secrets() {
    log_step "Configuring secrets..."

    SECRET_FILE="$K8S_DIR/secret.yaml"

    echo ""
    echo "You need to configure the following secrets (from your main server):"
    echo ""

    read -p "Enter REDIS_PASSWORD: " -s REDIS_PASSWORD
    echo ""
    read -p "Enter ENCRYPTION_KEY: " -s ENCRYPTION_KEY
    echo ""

    if [ -z "$REDIS_PASSWORD" ] || [ -z "$ENCRYPTION_KEY" ]; then
        log_error "Both REDIS_PASSWORD and ENCRYPTION_KEY are required"
        exit 1
    fi

    # Update secret file
    sed -i "s/your-redis-password-from-main-server/$REDIS_PASSWORD/g" "$SECRET_FILE"
    sed -i "s/your-encryption-key-from-main-server/$ENCRYPTION_KEY/g" "$SECRET_FILE"

    log_info "Secrets configured"
}

# Deploy to Kubernetes
deploy() {
    log_step "Deploying to Kubernetes..."

    cd "$K8S_DIR"

    if [ "$USE_KUSTOMIZE" = true ]; then
        kustomize build . | kubectl apply -f -
    else
        kubectl apply -f namespace.yaml
        kubectl apply -f configmap.yaml
        kubectl apply -f secret.yaml
        kubectl apply -f esmfold-deployment.yaml
        kubectl apply -f diffdock-deployment.yaml
    fi

    log_info "Deployment submitted to Kubernetes"
}

# Check deployment status
check_status() {
    log_step "Checking deployment status..."

    echo ""
    echo "Namespace: phonos-workers"
    echo ""

    echo "Pods:"
    kubectl get pods -n phonos-workers -o wide
    echo ""

    echo "Deployments:"
    kubectl get deployments -n phonos-workers
    echo ""

    echo "PVCs:"
    kubectl get pvc -n phonos-workers
    echo ""
}

# View logs
view_logs() {
    WORKER=${1:-esmfold}
    log_step "Viewing logs for $WORKER-worker..."

    kubectl logs -f -n phonos-workers -l "app.kubernetes.io/component=$WORKER-worker" --tail=100
}

# Delete deployment
delete_deployment() {
    log_step "Deleting Kubernetes deployment..."

    read -p "Are you sure you want to delete the deployment? [y/N] " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        cd "$K8S_DIR"

        if [ "$USE_KUSTOMIZE" = true ]; then
            kustomize build . | kubectl delete -f -
        else
            kubectl delete -f diffdock-deployment.yaml --ignore-not-found
            kubectl delete -f esmfold-deployment.yaml --ignore-not-found
            kubectl delete -f secret.yaml --ignore-not-found
            kubectl delete -f configmap.yaml --ignore-not-found
            kubectl delete -f namespace.yaml --ignore-not-found
        fi

        log_info "Deployment deleted"
    else
        log_info "Deletion cancelled"
    fi
}

# Main
main() {
    echo ""
    echo "========================================"
    echo "  Phonos Kubernetes Deployment Script"
    echo "========================================"
    echo ""

    ACTION=${1:-deploy}

    case $ACTION in
        deploy)
            check_prerequisites
            configure_secrets
            deploy
            sleep 5
            check_status
            ;;

        status)
            check_status
            ;;

        logs)
            view_logs "${2:-esmfold}"
            ;;

        delete)
            check_prerequisites
            delete_deployment
            ;;

        *)
            echo "Usage: $0 [deploy|status|logs|delete]"
            echo ""
            echo "Actions:"
            echo "  deploy  - Deploy workers to Kubernetes"
            echo "  status  - Check deployment status"
            echo "  logs    - View worker logs (logs esmfold|diffdock)"
            echo "  delete  - Delete deployment"
            exit 1
            ;;
    esac
}

main "$@"
