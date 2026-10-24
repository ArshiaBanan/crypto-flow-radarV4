# v3.5 SaaS Identity, Workspace & Entitlements

v3.5 adds the first real multi-user SaaS layer to the Crypto Flow Radar Web Terminal.

## Delivered

- Registration and login with PBKDF2-HMAC-SHA256 password hashing.
- HttpOnly, SameSite session cookie with configurable Secure flag.
- Server-side session expiry and revocation.
- CSRF protection for all state-changing SaaS endpoints.
- Per-user private Watchlists.
- Plan-based limits for watchlist count and symbols per list.
- Per-user alert score threshold and direction preference.
- Browser / Telegram notification preferences with plan entitlement enforcement.
- Audit trail for identity and workspace mutations.
- Responsive Workspace UI integrated into the existing terminal.

## API

`GET /v35/status`

`POST /v35/auth/register`

`POST /v35/auth/login`

`POST /v35/auth/logout`

`GET /v35/auth/me`

`GET /v35/watchlists`

`POST /v35/watchlists`

`PATCH /v35/watchlists/{watchlist_id}`

`DELETE /v35/watchlists/{watchlist_id}`

`GET /v35/preferences`

`PUT /v35/preferences`

## Commercial boundary

Plan definitions are entitlement metadata. v3.5 intentionally does not accept card payments or expose client-side paid-plan escalation. A payment provider must call a server-side webhook before paid entitlements are considered purchased.

## Production hardening still required

For a true multi-tenant public launch, add TLS, secure cookies, login rate limiting, email verification, password reset, account recovery, provider webhooks, subscription state reconciliation, and central session/rate-limit storage (Redis/PostgreSQL). The current implementation uses the project's existing SQLite auxiliary store for SaaS state, so a PostgreSQL migration of the SaaS tables should be completed before a scaled multi-instance launch.
