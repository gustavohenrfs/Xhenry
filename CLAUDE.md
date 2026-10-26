# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

XHenry (formerly "Suzana", still the SQLite filename `data/suzana.db` and the owner email `local@suzana`) is a dashboard for monitoring X (Twitter) accounts grouped into folders: account health (search ban / ghost ban / suspended), engagement metrics, best-time-to-post suggestions, Telegram alerts, and a Grok (xAI) "will this post get restricted?" reviewer. FastAPI + SQLAlchemy backend, single-page vanilla JS frontend. All user-facing strings, comments and docstrings are in Brazilian Portuguese — keep new ones in Portuguese.

## Commands

```bash
python -m pip install -r requirements.txt
copy .env.example .env
python -m uvicorn main:app --host 127.0.0.1 --port 8000   # add --reload for dev
```

Tests: `python -m pip install -r requirements-dev.txt` then `python -m pytest -q` (Cloaker, auth/route protection, password reset, dashboard, health check, archiving, Telegram and the watchdog; `tests/conftest.py` points `DATABASE_URL` at a throwaway SQLite and sets `WATCHDOG=off` before importing `app.*`; the X API and Telegram are monkeypatched, never called). No linter or build step. On Python 3.15 `uvicorn[standard]` fails to build `pyyaml` without MSVC; install plain `uvicorn` locally. OpenAPI docs at `/docs` unless `APP_ENV=production`.

## Configuration

`.env` is loaded by a hand-rolled parser in `app/database.py` (`load_env_file`, uses `setdefault`, so real env vars win). Key vars:
- `DATABASE_URL` — Postgres in production (Render/VPS); `postgres://`/`postgresql://` are rewritten to `postgresql+psycopg://`. Unset → SQLite at `data/suzana.db`.
- `X_BEARER_TOKEN` — needed only for `/api/check*`; without it the dashboard still loads and checks return 503.
- `SECRET_KEY` — required for `/api/login` (HMAC-signed tokens).
- `ALLOW_REGISTRATION` — `/api/register` returns 403 unless this is `true` (missing = closed). Turn it on only to create an account.
- `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, `XAI_API_KEY`, `XAI_MODEL` — fallbacks; the `app_settings` row (id=1, edited via `/api/settings`) takes precedence.
- `PUBLIC_URL` — base of the password-reset link sent to Telegram (falls back to the request URL, which is wrong behind nginx).

## Architecture

- `main.py` — app factory. Lifespan runs `Base.metadata.create_all`, then `ensure_user_auth_columns()` and `ensure_owner()`. **There is no migration tool**: new columns on existing tables must be added by hand via `ALTER TABLE` (add them to `ensure_user_auth_columns` in `app/services.py`, which runs at startup). `create_all` will not alter existing tables.
- `app/models.py` — all tables (`users`, `folders`, `x_accounts`, `health_checks`, `daily_metrics`, `folder_daily_metrics`, `tweet_samples`, `sync_runs`, `app_settings`, `alerts_sent`, `day_baselines`). `import app.models` in `main.py` is what registers them on the metadata.
- `app/services.py` — nearly all business logic; routers are thin. Key flows:
  - **Health check** (`check_handle` → `collect_health` → `derive_status` → `persist_account`): calls X API v2 (user lookup, user timeline, recent search) and maps HTTP statuses to a `HealthStatus`. Writes a `HealthCheck`, a profile snapshot into `DailyMetric`, a `SyncRun`, rebuilds the `FolderDailyMetric` rollup, then sends a Telegram ban alert on status transition (deduped via `alerts_sent.dedupe_key`).
  - **Dashboard** (`dashboard`, `account_payload`, `build_suggestions`): reads only the DB (`tweet_samples`, `daily_metrics`). It never calls the X API. Metrics shown are only Views, Curtidas, Retweets, Replies and Seguidores; best-time suggestions rank hours by average `view_count`. The `engagement_count`/`engagement_rate` columns are legacy: kept on the tables so `robo.py` inserts don't break, but nothing in the app reads or writes them.
  - Tweet metrics are **not collected by this app** — they are written into `tweet_samples`/`daily_metrics` by an external `robo.py` running on a VPS (not in this repo). `upsert_tweet`/`recompute_days` exist here but aren't wired to any route.
- `app/watchdog.py` — background task started in `main.py`'s lifespan (off with `WATCHDOG=off`; `tests/conftest.py` sets it off). Every 60 s: sends "Robô parado" when the last robo heartbeat (`sync_runs` row with `kind=tweets`, `account_id` NULL, status ok/partial) is older than `ROBO_SILENCE_MIN` (15); stays quiet if no heartbeat ever existed. With `AUTO_CHECK_MINUTES` > 0 it runs `check_handle` on stale accounts, which is what fires the existing Ghost/Search Ban alert (`dispatch_alerts`); off by default because it spends X API quota.
- `robo.py` (VPS, not in this repo; last committed copy is in git history before `c84e0ee`) writes `tweet_samples`/`daily_metrics`, one heartbeat per run in `sync_runs`, and sends viral, views-drop (today vs yesterday, from 23h) and collection-error alerts, deduped via `alerts_sent`.
- `app/cloaker.py` — pure module (no FastAPI) behind the Cloaker tab: `build_variants` makes N image/video variants with distinct sha256 (few-px crop + resize back, tone shift, ±1 noise, re-encode; metadata always stripped, optional fresh EXIF/creation_time). Image output format comes from the file content, not the declared type (`detect_image_ext`), and an embedded ICC profile is converted to sRGB before being dropped. Video uses the ffmpeg binary bundled by `imageio-ffmpeg`. `CloakerError` messages are user-facing Portuguese and become HTTP 400. Exposed by `app/routers/cloaker.py` (`POST /api/cloaker`, requires login, returns a zip + `X-Cloaker-Manifest` header). Video jobs stay on disk in a per-request dir under `data/tmp` (removed by a `BackgroundTask` after the response), run one at a time (`_VIDEO_SLOTS`, 429 when busy) and share a `VIDEO_JOB_BUDGET` of 1800 s; images are capped at 40 MP. Video limits ("Vídeo acima de 100 MB", up to 20 copies: `MAX_VIDEO_BYTES` 100 MB, `MAX_COPIES_VIDEO` 20) are duplicated as `CLOAKER_MAX_VIDEO_*` in app.js plus hint text in index.html — change all three together. On the VPS keep nginx `client_max_body_size` above the byte limit (110M) and `proxy_read_timeout` above the budget (1860s). Progress bar: the tab sends a random `job_id`, `build_variants(on_progress=...)` feeds an in-memory dict in the router, and the tab polls `GET /api/cloaker/progress/{job_id}` — this only works with a single uvicorn worker.
- `app/routers/` — `accounts.py` (folders, accounts, check, dashboard), `auth.py` (register/login/me, custom HS256 JWT + PBKDF2 hashing implemented by hand, no JWT/hash library), `checks.py` (settings, Telegram test, Grok post review), `pages.py` (serves `/`, `/health` and legacy `/avatars/{file}`).
- Auth (`current_user`, usually as `dependencies=[Depends(current_user)]` on the decorator) is enforced on every write or cost-incurring route: `/api/me*`, `/api/change-password`, `/api/settings` (GET/PUT), `/api/telegram/test`, `/api/posts/review`, folder/account POST/PATCH/DELETE, `/api/check*`, `/api/cloaker` (and its `/progress/{job_id}`). Read-only routes stay public: `/api/config`, `/api/dashboard`, `/api/suggestions`, `GET /api/folders`, `/api/refresh-all`; so are `/api/login`, `/api/register` (gated by `ALLOW_REGISTRATION`) and `/api/password-reset/{request,confirm}`. `tests/test_auth_routes.py` pins this list — update it when adding routes. Logging in only gates access: folders, accounts and settings are not scoped per user, all data hangs off a single implicit owner (`local@suzana`), so any account that can log in can edit everything.
- Archiving accounts: `PATCH /api/accounts/{id}` with `is_archived` sets the flag and `archived_at` (column added at startup by `ensure_user_auth_columns`). Archived accounts are left out by `selected_accounts` (dashboard, KPIs, suggestions, `/api/check-all`), `accounts_for` (single check), the folder counts, the watchdog auto-check and `robo.py`'s account query; their rows in `tweet_samples`/`daily_metrics`/`health_checks` stay. `GET /api/accounts/archived` (public, read-only) returns them with all-time totals and the daily views/followers history for the Arquivadas tab.
- Password reset (`app/routers/auth.py`): "Esqueceu a senha?" posts the email to `/api/password-reset/request`, which sends `PUBLIC_URL/#reset=<token>` to the configured Telegram chat (same answer whether the email exists or not, 60 s in-memory cooldown per email). The token is signed with a key derived from `SECRET_KEY` (not interchangeable with login tokens), lasts 15 min and embeds a fingerprint of `senha_hash`, so it stops working once the password changes. No DB table. Login tokens are stateless, so a reset does not log out other sessions.
- Dates/times: stored in UTC; SQLite returns naive datetimes, so wrap with `as_utc()` before converting. Day bucketing uses `America/Sao_Paulo` (`DEFAULT_TZ`). Periods: `today, yesterday, 7d, 30d, 3m, 1y, total`.

## Operations (VPS)

`scripts/backup.sh` is the daily backup (cron): `pg_dump -Fc` verified with `pg_restore --list`, a tar.gz of the legacy `data/avatars`, retention by `BACKUP_KEEP_DAYS` (14) in `BACKUP_DIR` (`/var/backups/xhenry`), and a Telegram message naming the failed step (token from env/.env, falling back to `app_settings`). It reads only the keys it needs from `.env` (never sources it). `.gitattributes` keeps `*.sh` LF.

## Frontend

`/` serves `templates/index.html` as a static `FileResponse` (no template engine). **All frontend JS lives in `static/js/app.js`**, loaded at the end of `<body>` as `/static/js/app.js?v=N`; bump `?v=` in index.html whenever app.js changes (`/static` is cacheable, index.html is `no-store`). The only inline script left is the `tailwind.config` block in `<head>`, which must run before the Tailwind CDN processes classes. Uses Tailwind CDN, Chart.js and Cropper.js from CDNs plus `static/css/styles.css`. The login token is kept in `localStorage["access_token"]` and sent as a Bearer header by the `api()` helper.

## Notes

- `.gitignore` still references an old `backend/` skeleton folder that is no longer in the repo.
