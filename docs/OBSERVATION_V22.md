# Crypto Flow Radar v2.2 — Real-Market Observation & Immutable Research Ledger

## هدف
v2.2 برای جمع‌آوری شواهد point-in-time از بازار زنده ساخته شده است. هر snapshot همراه با featureها، scoreها، context مدل، config hash و hash-chain ذخیره می‌شود.

## جداول
- `observation_sessions_v22`: session، نسخه کد، محیط و config hash
- `market_observations_v22`: snapshot اصلی بازار و موتور تصمیم
- `observation_events_v22`: رویدادهای append-only مثل `ALERT_CREATED` و `PAPER_RESOLVED`

## Integrity
هر snapshot یک `payload_hash` و `chain_hash` دارد. `chain_hash = SHA256(prev_chain_hash | payload_hash)` است و برای هر symbol داخل هر session مستقل نگه داشته می‌شود.

## اصل point-in-time
هیچ future label یا outcome داخل snapshot اولیه ذخیره نمی‌شود. outcomeها بعداً به‌عنوان event مجزا ثبت می‌شوند تا dataset پژوهشی دچار leakage نشود.

## وضعیت v2.2
این لایه برای observation/research است و به‌تنهایی تضمین‌کننده سودآوری نیست. داده‌ی واقعی باید در طول زمان جمع شود و سپس v2.1 validation روی cohortهای واقعی اجرا شود.
