# Phonos Worker Deployment Guide

This guide covers deploying Phonos worker nodes (ESMFold and DiffDock) using Docker Compose or Kubernetes.

## Overview

Phonos uses distributed worker nodes for ML inference:
- **ESMFold Worker (type-0)**: Protein structure prediction
- **DiffDock Worker (type-3)**: Molecular docking

### GPU Configuration

Workers are configured for optimal GPU usage:
- **ESMFold**: GPU 0 (default)
- **DiffDock**: GPU 1 (default)

This allows both workers to run simultaneously on a dual-GPU system.

## Quick Start

### Docker Compose (Recommended)

```bash
# 1. Configure environment
cd deploy/workers/docker
cp .env.example .env
# Edit .env with your Redis and encryption settings

# 2. Deploy workers (auto-detects GPU configuration)
../scripts/deploy-workers.sh deploy
```

### Kubernetes

```bash
# 1. Update secrets in kubernetes/secret.yaml
# 2. Deploy
../scripts/deploy-kubernetes.sh deploy
```

## Deployment Options

### Docker Compose

Three deployment modes are available:

| Mode | File | Description |
|------|------|-------------|
| **Dual GPU** | `docker-compose.yml` | ESMFold on GPU 0, DiffDock on GPU 1 |
| **Single GPU** | `docker-compose.single-gpu.yml` | Both workers share GPU 0 |
| **CPU Only** | `docker-compose.cpu-only.yml` | No GPU required |

#### Commands

```bash
# Auto-deploy (detects GPU configuration)
./deploy-workers.sh deploy

# Force specific mode
./deploy-workers.sh deploy dual
./deploy-workers.sh deploy single
./deploy-workers.sh deploy cpu

# Stop workers
./deploy-workers.sh stop

# Check status
./deploy-workers.sh status

# View logs
./deploy-workers.sh logs
./deploy-workers.sh logs esmfold
./deploy-workers.sh logs diffdock
```

### Kubernetes

Kubernetes manifests support GPU scheduling via NVIDIA device plugin.

#### Prerequisites

- Kubernetes cluster with GPU nodes
- NVIDIA device plugin installed
- kubectl configured

#### Components

| File | Description |
|------|-------------|
| `namespace.yaml` | phonos-workers namespace |
| `configmap.yaml` | Shared configuration |
| `secret.yaml` | Redis/encryption secrets |
| `esmfold-deployment.yaml` | ESMFold worker + PVC |
| `diffdock-deployment.yaml` | DiffDock worker + PVCs |
| `kustomization.yaml` | Kustomize configuration |

#### Commands

```bash
# Deploy
./deploy-kubernetes.sh deploy

# Check status
./deploy-kubernetes.sh status

# View logs
./deploy-kubernetes.sh logs esmfold
./deploy-kubernetes.sh logs diffdock

# Delete deployment
./deploy-kubernetes.sh delete
```

## Configuration

### Required Settings

These settings **must match** your main Phonos server:

| Variable | Description |
|----------|-------------|
| `REDIS_HOST` | Main server hostname |
| `REDIS_PORT` | Redis port (default: 6379) |
| `REDIS_PASSWORD` | Redis password |
| `ENCRYPTION_KEY` | AES encryption key |

### GPU Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `PREFERRED_GPU` | Preferred GPU index | ESMFold: 0, DiffDock: 1 |
| `CUDA_VISIBLE_DEVICES` | Visible GPU devices | Auto-configured |

### Performance Tuning

| Variable | Description | Default |
|----------|-------------|---------|
| `OMP_NUM_THREADS` | OpenMP threads | 8 |
| `MKL_NUM_THREADS` | MKL threads | 6 |
| `JOB_TIMEOUT` | Job timeout (seconds) | ESMFold: 600, DiffDock: 1200 |

## Architecture

```
┌─────────────────────────────────────┐
│         Main Phonos Server          │
│  ┌─────────────────────────────┐    │
│  │   Redis (Message Broker)    │    │
│  └─────────────────────────────┘    │
└─────────────────────────────────────┘
            ↑           ↑
     GPU 0  │           │  GPU 1
┌───────────┴───┐   ┌───┴───────────┐
│ ESMFold Worker│   │DiffDock Worker│
│   (type-0)    │   │   (type-3)    │
└───────────────┘   └───────────────┘
```

## GPU Assignment Logic

Workers use a GPU-first approach with intelligent fallback:

1. **Check environment**: `CUDA_VISIBLE_DEVICES` or `PREFERRED_GPU`
2. **Detect available GPUs**: Query nvidia-smi for VRAM
3. **Select GPU**:
   - ESMFold prefers GPU 0
   - DiffDock prefers GPU 1 (with 8GB+ VRAM check)
4. **Fallback to CPU**: If no suitable GPU found

### Single GPU System

On single-GPU systems:
- Both workers can share GPU 0
- Jobs are processed sequentially
- GPU memory is released between jobs

### CPU-Only System

For CPU-only deployment:
- Set `CUDA_VISIBLE_DEVICES=""` or `PREFERRED_GPU=cpu`
- Increase job timeouts (CPU is 10-50x slower)
- Increase thread counts for better CPU utilization

## Troubleshooting

### Worker not connecting to Redis

1. Verify `REDIS_HOST` points to main server (not localhost)
2. Check firewall allows outbound to port 6379
3. Test connectivity:
   ```bash
   redis-cli -h <REDIS_HOST> -p 6379 -a <REDIS_PASSWORD> ping
   ```

### Encryption errors

- `ENCRYPTION_KEY` **must be identical** to main server
- Copy the exact value from main server's `.env`
- Do not generate a new key

### GPU not detected

1. Check NVIDIA drivers: `nvidia-smi`
2. Verify Docker NVIDIA runtime: `docker run --gpus all nvidia/cuda:11.0-base nvidia-smi`
3. Check container GPU access in logs

### Out of memory errors

- Reduce `samples_per_complex` for DiffDock
- Use CPU mode for large sequences
- Check for GPU memory leaks in logs

### Kubernetes GPU scheduling

1. Ensure NVIDIA device plugin is installed:
   ```bash
   kubectl get pods -n kube-system | grep nvidia
   ```
2. Check node labels:
   ```bash
   kubectl get nodes -l nvidia.com/gpu=true
   ```

## Security Notes

- Never commit `.env` files with real credentials
- Use Kubernetes Secrets for production
- Enable Redis TLS for secure connections
- Rotate encryption keys periodically

## Scaling

### Horizontal Scaling

Deploy multiple worker instances with unique IDs:

```yaml
# In .env or ConfigMap
WORKER_ID=node-type-0-worker-1  # Unique per instance
```

### Auto-scaling (Kubernetes)

Use Horizontal Pod Autoscaler based on queue depth:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: esmfold-worker-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: esmfold-worker
  minReplicas: 1
  maxReplicas: 10
```

## Monitoring

### Health Checks

Workers send heartbeats every 30 seconds to:
- `pubsub:worker_heartbeats` (Redis pub/sub)
- `worker_health:<worker_id>` (Redis hash)

### Logs

```bash
# Docker
docker logs -f phonos-esmfold-worker

# Kubernetes
kubectl logs -f -n phonos-workers -l app.kubernetes.io/component=esmfold-worker
```

### Metrics

Key metrics to monitor:
- Job processing time
- GPU memory usage
- Queue depth
- Heartbeat intervals

## File Structure

```
deploy/workers/
├── docker/
│   ├── docker-compose.yml          # Dual GPU deployment
│   ├── docker-compose.single-gpu.yml
│   ├── docker-compose.cpu-only.yml
│   └── .env.example
├── kubernetes/
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── secret.yaml
│   ├── esmfold-deployment.yaml
│   ├── diffdock-deployment.yaml
│   └── kustomization.yaml
├── scripts/
│   ├── deploy-workers.sh           # Docker deployment script
│   └── deploy-kubernetes.sh        # K8s deployment script
└── README.md
```

## Support

For issues or questions:
- Check worker logs for errors
- Verify Redis connectivity
- Ensure encryption keys match
- Review GPU configuration
