# Security Policy

## Reporting a Vulnerability
Please do **not** open public GitHub issues for security vulnerabilities.
Report security issues privately via GitHub Security Advisories at:
https://github.com/mxsp/phonos/security/advisories/new

Include reproduction steps, affected version/commit, and impact if known.

## Deployment Security Defaults
- **Playground mode** (`phonos playground` / `--mode playground`) runs a pre-seeded local evaluation sandbox on port `5040`. Never expose Playground to the public internet.
- **Production mode** (`phonos production` / `--mode production` / `PHONOS_ENV=production`) enforces strong `SECRET_KEY` and `ENCRYPTION_KEY` secrets (auto-generated with `0600` permissions on first local launch), disables debug surfaces, and restricts network access to private/loopback subnets by default (`PHONOS_LOCALNET_ONLY=1`).
- **Reverse Proxy**: `ProxyFix` (`X-Forwarded-For` / `X-Forwarded-Proto`) is disabled by default (`PHONOS_TRUST_PROXY=0`) so clients cannot spoof IP lockouts or rate limits. Enable `PHONOS_TRUST_PROXY=1` only when Phonos sits behind a trusted TLS-terminating reverse proxy (e.g., Caddy or Nginx) that strips client-supplied forwarding headers.
- **API Keys**: Model provider keys (`NVIDIA_API_KEY`, Copilot LLM keys, and custom model endpoint keys) are sealed at rest using authenticated encryption (`AES-GCM` / `Fernet`) and are never returned in plaintext by any API endpoint.
