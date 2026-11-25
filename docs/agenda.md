# External Worker Integration — Agenda

## Security Model: Subdomain-Based Identity

Every external worker **must** have a valid `*.fyreflysystems.de` subdomain.
This is the **sole identity anchor** for external workers connecting to the
main app (`phonos.fyreflysystems.de`). No worker is trusted without a
verified subdomain.

```
  External Worker                    Phonos Main App
  ────────────────                   ────────────────

  worker-abc.fyreflysystems.de        phonos.fyreflysystems.de
         │                                    │
         │  1. DNS resolves subdomain         │
         │     → worker-abc.fyreflysystems.de │
         │     → matches *.fyreflysystems.de  │
         │                                    │
         │  2. TLS handshake                  │
         │     → valid cert for subdomain     │
         │                                    │
         │  3. Registration request           │
         │     worker_id=worker-abc           │
         │     subdomain=worker-abc...        │
         │     public_key=<RSA>               │
         │                                    │
         │  4. MRS verifies:                  │
         │     • subdomain → DNS resolves     │
         │     • subdomain matches worker_id  │
         │     • *.fyreflysystems.de suffix   │
         │     • public key matches           │
         │                                    │
         │  5. Approved → assigned shard queue│
```

## Rationale

- **DNS as trust root** — subdomain ownership proves control of the worker.
- **No shared secrets** — each worker has its own subdomain + key pair.
- **Revocable** — remove DNS record to decommission a worker.
- **Auditable** — every worker's subdomain is logged in registration history.
- **Sovereign** — workers live on customer-owned infrastructure; we only verify
  their DNS identity.

## Implementation Phases

### Phase 1: Main-App Groundwork (this session)

| What | Where |
|------|-------|
| Subdomain verification utility | `phonos_app/helpers/subdomain_verify.py` |
| Add `remote_ssh` backend type | `config/runtimes.schema.json`, `runtime_config.py` |
| Subdomain enforcement in worker registration | `production/mrs_service/registration.py` |
| Subdomain-based authorization | `production/mrs_service/authorization.py` |
| Worker identity with subdomain | `production/node_utils.py` |

### Phase 2: Worker-Side Registration (next session)

| What | Where |
|------|-------|
| Worker registers via its subdomain | Worker `node_utils.py` registration flow |
| Worker proves subdomain ownership | TLS client cert or DNS challenge |
| Heartbeat includes subdomain | Worker health check payload |

### Phase 3: Live Implementation (future)

| What | Where |
|------|-------|
| WireGuard tunnel live | `cloud_provisioner.py` → wg subprocess calls |
| File sync over tunnel | `cloud_provisioner.py` → rsync subprocess |
| RemoteSSHBackend live | `kernel_manager.py` → paramiko SSH calls |
| Cloud provisioner live | `cloud_provisioner.py` → AWS/GCP SDK calls |
| Kicker shows external workers | `production/kicker.py` |

## Subdomain Validation Rules

1. Subdomain must end with `.fyreflysystems.de`
2. Subdomain must resolve via DNS (A/AAAA record)
3. Reverse DNS (PTR) must match the forward name
4. Worker ID must match the subdomain label:
   - `worker-abc.fyreflysystems.de` → worker_id = `worker-abc`
   - Subdomain label must be a valid DNS label (63 chars max, alphanumeric + hyphens)
5. TLS certificate must be valid for the subdomain (SNI check)
6. Only subdomains under `fyreflysystems.de` are trusted — bare `fyreflysystems.de`
   (the main app domain) is NOT valid for external workers

## Worker Registration Flow

```
Worker                              MRS (Main App)
──────                              ──────────────

1. Generate RSA key pair
2. Build identity:
   worker_id = <subdomain-label>
   subdomain = <worker_id>.fyreflysystems.de
3. Send registration request
   ─────────────────────────────────► 4. Validate subdomain:
                                         • DNS resolve
                                         • *.fyreflysystems.de suffix
                                         • label matches worker_id
                                      5. Store pending approval in DB
                                      6. Notify admin (kicker/log)
                                      7. Admin approves → worker active
   ◄────────────────────────────────── 8. Confirmation with org_id
9. Begin heartbeat on subdomain
```

## DNS Record Convention

External worker subdomains follow a strict naming scheme:

```
<worker-identifier>.fyreflysystems.de

where <worker-identifier>:
  - max 63 characters
  - lowercase alphanumeric + hyphens only
  - must not start or end with hyphen
  - should describe the worker purpose
```

## Testing

- Unit tests for subdomain validation patterns
- Integration test: register worker with valid subdomain
- Integration test: reject worker with invalid subdomain
- Integration test: reject bare `fyreflysystems.de`
- Integration test: reject worker with wrong label/subdomain mismatch

## Security Considerations

- **DNS spoofing**: mitigated by requiring consistent forward + reverse DNS
- **Subdomain takeover**: prevented by requiring public key proof alongside subdomain
- **Replay attacks**: mitigated by nonce + timestamp in registration messages
- **Worker impersonation**: prevented by subdomain + RSA key pair binding
