# services/

Independently-deployable, containerized microservices that the main web app
talks to over the network (HTTP / ZMQ). Each ships its own `Dockerfile` and is
built into a named image; the running app references them by **image name**, so
moving the source here does not affect running containers — only the next build.

| Service | Path | Image | Contract |
|---|---|---|---|
| Notebook kernel | [`services/notebook-kernel`](./notebook-kernel) | `phonos/notebook-kernel:latest` | Jupyter kernel lifecycle + signal protocol (interrupt/restart) used by the Notebook IDE. |
| Wolfram service | [`services/wolfram-service`](./wolfram-service) | `phonos/wolfram-svc:latest` | `POST /evaluate` on :5100 — evaluates Wolfram Language via `wolframscript`. Consumed by `helpers/kernel_manager.py::WolframServiceBackend`. |
| HPC worker | [`services/hpc-worker`](./hpc-worker) | `phonos/hpc-worker:latest` | Outbound-dialing compute worker; long-polls Phonos over HTTPS with a per-cluster token. |

Formerly `production/docker/{notebook-kernel,wolfram-service,hpc-worker}`.

## Build

```bash
# from repo root
docker compose -f docker-compose.notebook.yml build   # kernel + wolfram
```

The compose build contexts point at `./services/...` (updated during the
reorg). Per-cluster secrets (`services/hpc-worker/.cluster*.env`) are gitignored
and provisioned at deploy time.
