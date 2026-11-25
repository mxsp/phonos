# Phonos Deployment Guide

## Architecture Overview

```
GitHub (push to main)
  → GitHub Actions CI/CD (.github/workflows/main.yml)
    → Lint (flake8)
    → Test (pytest tests/test_main.py)
    → Deploy via SSH to production server
      → git pull
      → update deps if changed
      → rebuild frontend if changed
      → docker compose up -d --build
```

**Production server:** `<YOUR_SERVER_IP>` (GCP VM)
**User:** `phonos`
**App directory:** `/opt/phonos`
**Docker Compose:** `/opt/phonos/phonos-main/docker-compose.yml`

## Services (Docker Compose)

| Service  | Container         | Port  | Description                    |
|----------|-------------------|-------|--------------------------------|
| postgres | phonos-postgres   | 5432  | PostgreSQL 16 database         |
| redis    | phonos-redis      | 6379  | Redis 7 message broker + cache |
| web      | phonos-web        | 5002  | Flask app (maps to 5000)       |
| mrs      | phonos-mrs        | -     | Management & Routing Service   |

## GitHub Secrets Required

These must be set in the GitHub repo under Settings → Secrets → Actions:

| Secret           | Value                           | Description              |
|------------------|---------------------------------|--------------------------|
| `DEPLOY_HOST`    | `<YOUR_SERVER_IP>`                | Server IP                |
| `DEPLOY_USER`    | `phonos`                  | SSH username             |
| `DEPLOY_SSH_KEY` | (contents of ~/.ssh/deploy_key) | Private SSH key for auth |

## Manual Deploy Commands

```bash
# Full deploy (pull + deps + build + restart)
./deploy/deploy.sh

# Just pull latest code
./deploy/deploy.sh --pull-only

# Just restart docker services
./deploy/deploy.sh --restart

# Check status
./deploy/deploy.sh --status
```

## For Future Agents

### How to modify the deploy pipeline
1. Edit `.github/workflows/main.yml` for CI/CD changes
2. Edit `deploy/deploy.sh` for server-side deploy logic
3. Both files are self-contained and well-commented

### How to add a new service
1. Add service to `phonos-main/docker-compose.yml`
2. If it needs env vars from `.env`, add them to the `environment:` block
3. The deploy script will auto-rebuild via `docker compose up -d --build`

### How to add new tests
1. Add test files to `tests/` directory
2. The CI runs `pytest tests/test_main.py` - to add more test files, update the workflow
3. Tests that need GPU (like `test_node5_esm2.py`) should NOT be added to CI

### How to skip tests and force deploy
1. Go to GitHub Actions → "Phonos CI/CD" → "Run workflow"
2. Set "Skip tests" to "true"
3. Click "Run workflow"

### Key files
- `.github/workflows/main.yml` - CI/CD pipeline definition
- `deploy/deploy.sh` - Server-side deployment script
- `deploy/DEPLOY.md` - This file
- `phonos-main/docker-compose.yml` - Docker service definitions
- `phonos-main/Dockerfile` - App container build
- `phonos-main/requirements.txt` - Python dependencies
- `.gitignore` - Files excluded from git
- `.env` - Environment secrets (NEVER committed)

### Troubleshooting
- **Deploy fails at SSH step:** Check that `DEPLOY_SSH_KEY` secret is set and the key is authorized on the server
- **Tests fail in CI:** Run `pytest tests/test_main.py -v` locally to reproduce
- **Docker build fails:** Check `phonos-main/Dockerfile` and run `docker compose build` locally
- **Service won't start:** Check logs with `docker compose logs <service>` in `phonos-main/`
