# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

XHenry (also called "Suzana" in README/DB filename) is a dashboard for monitoring X (Twitter) accounts grouped into folders: account health (search ban / ghost ban / suspended), engagement metrics, best-time-to-post suggestions, Telegram alerts, and a Grok (xAI) "will this post get restricted?" reviewer. FastAPI + SQLAlchemy backend, single-page vanilla JS frontend. All user-facing strings, comments and docstrings are in Brazilian Portuguese — keep new ones in Portuguese.

## Commands

```bash
python -m pip install -r requirements.txt
copy .env.example .env
python -m uvicorn main:app --host 127.0.0.1 --port 8000   # add --reload for dev
```

There are no tests, linter, or build step. OpenAPI docs at `/docs` unless `APP_ENV=production`.

## Configuration

`.env` is loaded by a hand-rolled parser in `app/database.py` (`load_env_file`, uses `setdefault`, so real env vars win). Key vars:
- `DATABASE_URL` — Postgres in production (Render/VPS); `postgres://`/`postgresql://` are rewritten to `postgresql+psycopg://`. Unset → SQLite at `data/suzana.db`.
- `X_BEARER_TOKEN` — needed only for `/api/check*`; without it the dashboard still loads and checks return 503.
- `SECRET_KEY` — required for `/api/register` and `/api/login` (HMAC-signed tokens).
- `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, `XAI_API_KEY`, `XAI_MODEL` — fallbacks; the `app_settings` row (id=1, edited via `/api/settings`) takes precedence.

## Architecture

- `main.py` — app factory. Lifespan runs `Base.metadata.create_all`, then `ensure_user_auth_columns()` and `ensure_owner()`. **There is no migration tool**: new columns on existing tables must be added by hand via `ALTER TABLE` (add them to `ensure_user_auth_columns` in `app/services.py`, which runs at startup). `create_all` will not alter existing tables.
- `app/models.py` — all tables (`users`, `folders`, `x_accounts`, `health_checks`, `daily_metrics`, `folder_daily_metrics`, `tweet_samples`, `sync_runs`, `app_settings`, `alerts_sent`, `day_baselines`). `import app.models` in `main.py` is what registers them on the metadata.
- `app/services.py` — nearly all business logic; routers are thin. Key flows:
  - **Health check** (`check_handle` → `collect_health` → `derive_status` → `persist_account`): calls X API v2 (user lookup, user timeline, recent search) and maps HTTP statuses to a `HealthStatus`. Writes a `HealthCheck`, a profile snapshot into `DailyMetric`, a `SyncRun`, rebuilds the `FolderDailyMetric` rollup, then sends a Telegram ban alert on status transition (deduped via `alerts_sent.dedupe_key`).
  - **Dashboard** (`dashboard`, `account_payload`, `build_suggestions`): reads only the DB (`tweet_samples`, `daily_metrics`). It never calls the X API.
  - Tweet metrics are **not collected by this app** — they are written into `tweet_samples`/`daily_metrics` by an external `robo.py` running on a VPS (not in this repo). `upsert_tweet`/`recompute_days` exist here but aren't wired to any route.
- `app/routers/` — `accounts.py` (folders, accounts, check, dashboard), `auth.py` (register/login/me, custom HS256 JWT + PBKDF2 hashing implemented manually despite `pyjwt`/`passlib` in requirements), `checks.py` (settings, Telegram test, Grok post review), `pages.py` (serves `/`, `/health`, plus temporary debug/maintenance routes).
- Auth is only enforced on `/api/me*` and `/api/change-password` (via `current_user`). All folder/account/dashboard/settings routes are unauthenticated and operate on a single implicit owner user (`local@suzana`).
- Dates/times: stored in UTC; SQLite returns naive datetimes, so wrap with `as_utc()` before converting. Day bucketing uses `America/Sao_Paulo` (`DEFAULT_TZ`). Periods: `today, yesterday, 7d, 30d, 3m, 1y, total`.

## Frontend

`/` serves `templates/index.html` as a static `FileResponse` (not Jinja-rendered, despite `Jinja2Templates` being configured). **All frontend JS lives in `static/js/app.js`**, loaded at the end of `<body>` as `/static/js/app.js?v=N`; bump `?v=` in index.html whenever app.js changes (`/static` is cacheable, index.html is `no-store`). The only inline script left is the `tailwind.config` block in `<head>`, which must run before the Tailwind CDN processes classes. Uses Tailwind CDN, Chart.js and Cropper.js from CDNs plus `static/css/styles.css`. The login token is kept in `localStorage["access_token"]` and sent as a Bearer header by the `api()` helper.

## Notes

- README says the `backend/` folder is an older skeleton and must not be used (it is not present in the repo, but `.gitignore` still references it).
- `pages.py` still has a temporary read-only `/debug-metrics` route; don't build on it.
