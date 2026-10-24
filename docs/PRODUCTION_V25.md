# Crypto Flow Radar v2.5 — Production Hardening & Reliability

## هدف
v2.5 لایه‌ی Reliability را روی providerهای بیرونی و REST داخلی اضافه می‌کند، بدون اینکه داده‌ی نامعتبر یا provider failure وارد scoring شود.

## اجزای اصلی
- HTTP timeout قابل تنظیم
- Retry محدود با exponential backoff
- احترام به `Retry-After` برای rate-limitهای provider
- Circuit breaker با حالت‌های `CLOSED / OPEN / HALF_OPEN`
- سقف اندازه‌ی response برای جلوگیری از payloadهای ناخواسته
- JSON/schema shape validation در transport و collectorها
- محدودیت concurrency و request pacing
- request fingerprint به‌عنوان `X-CFR-Request-ID` برای trace/idempotency tracking؛ این header تضمین idempotency سمت provider نیست
- provider health registry مشترک در سطح process
- degradation policy: providerهای fail شده از consensus کنار گذاشته می‌شوند و نتیجه‌ی مصنوعی ساخته نمی‌شود
- admin mutation fail-closed در صورت نبودن `ADMIN_API_KEY`
- secretها فقط از environment/settings خوانده می‌شوند و در responseهای health/status برگردانده نمی‌شوند

## رفتار failure
1. خطای transient مثل 429/5xx تا سقف attempts retry می‌شود.
2. خطاهای متوالی circuit را باز می‌کنند.
3. circuit بعد از cooldown وارد `HALF_OPEN` می‌شود.
4. یک موفقیت circuit را می‌بندد؛ failure دوباره circuit را باز نگه می‌دارد.
5. در زمان `OPEN` هیچ request جدیدی به آن provider فرستاده نمی‌شود.
6. consensus در صورت نبودن provider، با familyهای باقی‌مانده کار می‌کند؛ اگر شواهد مستقل کافی نباشد، eligibility فعال نمی‌شود.
7. هیچ stale payload به‌صورت silent جایگزین داده‌ی زنده نمی‌شود.

## متغیرهای تنظیمات
- `V25_HTTP_TIMEOUT_SECONDS`
- `V25_HTTP_MAX_ATTEMPTS`
- `V25_BACKOFF_BASE_SECONDS`
- `V25_BACKOFF_MAX_SECONDS`
- `V25_CIRCUIT_FAILURE_THRESHOLD`
- `V25_CIRCUIT_OPEN_SECONDS`
- `V25_MAX_RESPONSE_BYTES`
- `V25_MAX_CONCURRENCY`
- `V25_MIN_REQUEST_INTERVAL_MS`
- `V25_FAIL_CLOSED_EXTERNAL`

## API
- `GET /v25/resilience/status`
- `POST /v25/resilience/reset` با `X-Admin-Key`
- `GET /health` اکنون state providerها را نیز گزارش می‌کند.

## Scope
این مرحله reliability را مهندسی می‌کند؛ اثبات edge معاملاتی، سودآوری یا availability واقعی providerها نیازمند اجرای زنده و سنجش بیرونی است.
