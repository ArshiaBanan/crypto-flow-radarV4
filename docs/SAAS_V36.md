# Crypto Flow Radar v3.6 — SaaS Identity, Verification & Subscription Layer

## Purpose

v3.6 converts the previous private Workspace foundation into a subscription-ready SaaS identity layer. It adds:

- username + password accounts;
- Gmail/email ownership verification with a 6-digit code;
- 15-minute verification-code expiry;
- 6-attempt limit per code;
- 60-second resend cooldown enforced server-side;
- password reset by email code;
- professional profile fields: username, display name, timezone, phone, personal Telegram chat ID;
- Free / Pro / Legendary / Elite entitlements;
- server-side signal quotas and signal-kind gating;
- personal signal inbox;
- email / Telegram / SMS routing adapters for entitled users;
- signed subscription webhooks;
- subscription ledger and audit trail.

## Plan policy implemented

| Plan | Signals | Tracking | Signal classes | Notifications | Intelligence |
|---|---:|---:|---|---|---|
| Free | 1 / week | 5 slots | Standard | In-app | Basic |
| Pro | 10 / day | 25 slots | Standard | Email 24/7 | Core external context |
| Legendary | 30 / day | 100 slots | Standard + To The Moon | Email 24/7 | Advanced whale tools + early context |
| Elite | 100 / day | 250 slots | Standard + To The Moon + Stealth + Early | Email + personal Telegram + SMS | Advanced whales + priority delivery + API |

These are product entitlements, not claims about signal profitability. Numeric limits are configuration choices and should be reviewed against infrastructure cost and user behavior before commercial launch.

## Email verification flow

1. User registers with `username`, email and password.
2. Server creates an unverified account and stores only a hash of the 6-digit code.
3. The verification code is sent through the configured Gmail SMTP sender.
4. The UI starts a 60-second countdown immediately after the send attempt.
5. `Resend` is disabled during the countdown. The backend independently rejects early resend attempts with HTTP `429`.
6. Code expires after 15 minutes and locks after six failed attempts.
7. Login is rejected until `email_verified=true`.

### Gmail SMTP configuration

Use a dedicated sending Gmail account for the product rather than a personal mailbox. Google currently says that App Passwords require 2-Step Verification and that App Passwords are intended for cases where the application does not support Sign in with Google. Do not put the normal Gmail account password into `V36_SMTP_PASSWORD`.

Example:

```env
V36_EMAIL_MODE=smtp
V36_SMTP_HOST=smtp.gmail.com
V36_SMTP_PORT=587
V36_SMTP_USERNAME=alerts@example.com
V36_SMTP_PASSWORD=<google-app-password>
V36_SMTP_FROM_EMAIL=alerts@example.com
```

For local development, use:

```env
V36_EMAIL_MODE=console
```

In console mode, the verification code is logged and may be returned by the development API response. Never enable console mode in production.

## APIs

### Identity

```text
POST /v36/auth/register
POST /v36/auth/verify-email
POST /v36/auth/resend-verification
POST /v36/auth/login
POST /v36/auth/logout
GET  /v36/auth/me
POST /v36/auth/forgot-password
POST /v36/auth/reset-password
```

### Profile / workspace

```text
GET  /v36/profile
PUT  /v36/profile
GET  /v36/watchlists
POST /v36/watchlists
PATCH /v36/watchlists/{watchlist_id}
DELETE /v36/watchlists/{watchlist_id}
GET  /v36/preferences
PUT  /v36/preferences
GET  /v36/usage
GET  /v36/my-alerts
```

### Subscription / delivery

```text
GET  /v36/plans
GET  /v36/subscription
POST /v36/signal/reserve
POST /v36/billing/webhook
```

`/v36/billing/webhook` is provider-neutral and uses HMAC SHA-256 over the raw request body. A payment provider integration such as Stripe can be attached later by mapping its verified webhook events into the CFR subscription event schema.

## Alert routing

The v3.6 router is connected to the v2.0 production alert pipeline. For each `ALERT` event, eligible verified users are evaluated against:

- plan signal quota;
- signal class (`STANDARD`, `TO_THE_MOON`, `STEALTH`, `EARLY`);
- notification preferences;
- channel entitlement;
- personal Telegram chat ID;
- E.164 phone number for SMS.

A signal is quota-counted once per user even when it is delivered through several channels.

External delivery adapters require credentials. The system does not pretend that an unconfigured provider delivered a message.

## Production requirements

Before public launch:

- move SaaS state to PostgreSQL alongside the existing production storage strategy;
- store SMTP, billing, Telegram and SMS credentials in a secret manager;
- set `V36_COOKIE_SECURE=true` behind HTTPS;
- use a dedicated domain and transactional mail identity;
- add SPF, DKIM and DMARC for the sending domain;
- connect a verified payment provider webhook;
- add rate limiting / abuse controls at the edge;
- add per-user delivery retry/backoff and queueing rather than blocking the market-event loop;
- verify SMS and Telegram providers with real sandbox accounts;
- add account deletion / privacy export workflows and final commercial terms.

## Important boundary

The SaaS layer controls who may receive which class of signals and through which channels. It does not create a claim that a `To The Moon`, `Stealth` or `Early` signal will be profitable. Those labels remain product classifications whose definitions must be backed by the research/validation pipeline.
