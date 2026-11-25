# Trusted Tester Program

A controlled path into Phonos: people **join** as applicants, an admin **approves** them as
Trusted Testers, and testers later **move on to regular access**.

```
sign-up ──▶ applicant ──apply──▶ pending ──approve──▶ tester ──graduate──▶ regular
                                    └──reject──▶ rejected (may apply again)
                                                  tester ──revoke──▶ applicant
```

## Switching it on

| `TRUSTED_TESTER_GATE` | New accounts start as | Effect |
|---|---|---|
| `false` (default) | `regular` | Opt-in early-access track: regular users may apply from the dashboard ("Trusted Tester Program"). Nobody is blocked. |
| `true` | `applicant` | Applicants only reach `/trusted-tester`, sign-in/out, account recovery, legal pages and static files until approved. Pages redirect to `/trusted-tester`; APIs answer `403 {"code": "trusted_tester_pending"}`. |

Existing accounts are always `regular` (column default), so turning the gate on never locks
anyone out. Admins are never gated. Set the variable in the VM `.env` and restart `phonos-web`.

## Data

- `users.access_tier`: `applicant` | `tester` | `regular` (default `regular`).
- `trusted_tester_applications`: one row per user with motivation, institution, use case,
  status (`pending` | `approved` | `rejected` | `graduated` | `revoked`), reviewer, review note
  and timestamps.

Both self-apply on boot via `production/database.py` (`_create_schema`);
`production/migrations/023_trusted_tester_program.sql` documents the same schema.

## Endpoints

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET / POST | `/trusted-tester` | signed-in user | Program page and application form (server-rendered, works while gated) |
| GET | `/api/trusted-tester/status` | signed-in user | Tier, application and whether the gate is on |
| POST | `/api/trusted-tester/apply` | signed-in user | `{motivation (≥20 chars), institution?, use_case?}` |
| GET | `/api/admin/trusted-testers?status=` | admin | List applications |
| POST | `/api/admin/trusted-testers/<id>/<approve\|reject\|graduate\|revoke>` | admin | Review, optional `{note}` |

Every review sends the user an in-app notification and an email; admins get a notification for
each new application. The admin panel (dashboard → Admin Panel) has a "Trusted Tester
applications" card with the review buttons.

Tier rules on review: approving an applicant makes them `tester`; graduating always sets
`regular`; revoking a `tester` returns them to `applicant`. Regular users who joined through the
opt-in track keep `regular` whatever happens to their application.

Code: `phonos_app/helpers/trusted_tester.py` (tiers, gate), `phonos_app/routes/api_trusted_tester.py`
(routes), `templates/trusted_tester.html`, tests in `tests/test_trusted_tester.py`.
