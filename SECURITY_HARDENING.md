# Data-at-Rest Hardening — 2026-06-11

Scope: protect user data sitting on the drive / in the DB. Assessed first, then applied
only what is safe and necessary (no encryption that would break login, name search, or
Stripe invoicing).

## Posture found (already good — left as-is)
- Passwords: bcrypt (`user_credentials.password` BYTEA). ✅
- API keys: SHA-256 hashed (`api_keys.key_hash`, `user_model_api_keys`). ✅
- Worker payloads / user-model creds / IoT / developer-options: already AES-256-GCM
  encrypted via `production/encryption_util.py` (key rotation aware). ✅
- 2FA in use is **email OTP** (session-only, never stored). The `two_factor_auth.secret_key`
  TOTP column is **dead code** (no read/write) — nothing sensitive at rest there.
- Postgres is **localhost:5432** (loopback) → no network-TLS gap; at-rest = this host's disk.

## Applied now ✅

### 1. Filesystem lockdown (LIVE — no restart needed)
World-readable (`755`) data dirs → owner-only:
- `user_uploads/`, `job_outputs/`, `data/`, `logs/` → dirs `700`, files `600`.
- `static/` deliberately left `755` (served directly by nginx as user `nginx`; app assets only).
- `.env`, `.env.deployment`, `production/node_private_key.pem` already `600` (verified).

Any other local account on the box can no longer read users' uploads, job outputs, or logs.

### 2. Durable owner-only file creation (needs reload to activate)
`os.umask(0o077)` added at process start in `wsgi.py` and `phonos_app/app.py`, so every
file/dir the app creates from now on defaults to `0600`/`0700`. (gunicorn binds TCP
`127.0.0.1:5000`, not a unix socket, so the umask does not affect the nginx↔gunicorn path.)

### 3. Webhook HMAC secrets encrypted at rest (needs reload + migration)
`api_webhooks.secret_key` was the one reversible **secret** stored in plaintext.
- New helper `phonos_app/helpers/field_crypto.py`: `encrypt_field`/`decrypt_field`
  (text-safe `enc:v1:<base64>` token over AES-256-GCM, **transparent legacy-plaintext
  passthrough** on read → zero breakage for existing rows).
- `phonos_app/helpers/webhooks.py`: secret encrypted on create, decrypted only when signing
  the outgoing payload. The one-time plaintext disclosure to the user on creation is unchanged.
- Verified: ciphertext hides the plaintext, roundtrip works, legacy rows still sign.

## YOUR ACTIONS (I could not / should not do these automatically)

1. **Reload the web app** to activate the umask + webhook encryption code (zero-downtime):
   ```
   kill -HUP <gunicorn-master-pid>      # e.g. graceful worker cycle
   # or:  sudo systemctl reload phonos.service   /   restart phonos.service
   ```
   (The live-service reload was blocked by the safety guard since it wasn't explicitly requested.)

2. **Migrate existing plaintext webhook secrets** (idempotent, run once):
   ```
   python3.11 scripts/encrypt_webhook_secrets.py
   ```

3. **Encrypt the disk / DB volume** — the real "DB encryption at rest". Postgres is local,
   so enable full-volume encryption (AWS EBS encryption on the data volume, or LUKS/dm-crypt),
   and/or restrict the Postgres data dir perms. App-level field encryption does NOT substitute
   for this.

4. **ROTATE `ENCRYPTION_KEY` and `SECRET_KEY`** — these live in `.env`, which was committed to
   git history (see committed-secrets note). All app-level encryption above is only as strong as
   `ENCRYPTION_KEY` secrecy. Rotate the key (the module supports `ENCRYPTION_KEY_OLD` +
   `ENCRYPTION_KEY_VERSION` for online rotation), rotate `SECRET_KEY`, and purge history.

5. **Bind gunicorn to `127.0.0.1`** instead of `0.0.0.0:5000` so the app isn't reachable on the
   public IP bypassing nginx/TLS (`gunicorn_config.py` / the systemd ExecStart `--bind`).

6. **Encrypt backups** (DB dumps / file backups) wherever they land.

## Deliberately NOT done (would break the app)
- **`users.legal_name`, `users.address`, `user_credentials.email` NOT encrypted.** They are used
  for name search (`phonos_app/helpers/search.py`), Stripe tax invoicing (`production/stripe_service.py`),
  team/assignment display, and login lookup. In-place encryption would break those. If column-level
  PII encryption is required, do it as a dedicated project with deterministic encryption / a blind
  index for searchable fields — or rely on the disk-level encryption in action #3.

> Not legal advice / not a formal pentest. This raises the at-rest bar; a full review should still
> cover key management, backups, and infra controls.
