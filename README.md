# Parkd-In — Developer Reference

## What the software does

Parkd-In is a web app that shows a map of Camden, London with street segments colour-coded by estimated parking availability: green (likely free), yellow (uncertain), red (likely full). Users can tap a segment to see a probability score and submit reports ("I parked here", "I left a space", "nothing free here") which feed back into the predictions.

**How the prediction works**

The app uses a *heuristic engine* — a set of hand-crafted rules, not machine learning. It scores each street segment using:

| Factor | What it measures |
|---|---|
| Capacity | How many parking bays are on that street |
| Time of day | Whether a *CPZ* (Controlled Parking Zone — a residents-only or pay-and-display area) is currently active |
| Traffic speed | Recent TfL sensor readings for nearby A-roads |
| Crowd reports | Recent "parked/left/searching" events submitted by users |

The output is a probability between 0 and 1 (displayed as 0–100%) per street segment, recalculated and published as map tiles.

**What map tiles are**

Map tiles are small binary files (`.pbf` format) that Mapbox GL JS — the mapping library used in the browser — downloads and renders as coloured lines on the map. They are pre-generated rather than calculated live per request, which makes the map fast. Each tile covers a geographic area at a fixed zoom level. The app uses zoom level 14, which gives street-level detail, and covers Camden with 30 tiles.

**The two operating modes**

| Mode | What runs | Who it's for |
|---|---|---|
| `LOCAL_MODE=true` | FastAPI backend + tiles from disk, no database | Development, Vercel deployment |
| `LOCAL_MODE=false` | Full stack: Supabase database, Redis queue, R2 tile CDN | Live production |

---

## Architecture overview

```
Browser (Mapbox GL JS)
  │
  ├── GET /tiles/{z}/{x}/{y}.pbf ──→ Vercel CDN (static .pbf files in public/tiles/)
  │
  └── GET/POST /api/v1/* ──────────→ FastAPI (Python)
                                          │
                          ┌───────────────┼───────────────┐
                          ▼               ▼               ▼
                     Supabase        Upstash Redis    Cloudflare R2
                  (PostgreSQL DB     (temporary       (tile CDN for
                   with PostGIS      queue + cache)    production)
                   spatial queries)
```

**PostGIS** is a spatial extension to PostgreSQL that understands geographic shapes — it lets the database answer questions like "which parking bays are within 30 metres of this road segment".

**APScheduler** is a background job runner embedded inside the FastAPI process. It runs three recurring tasks while the server is up:
- Every 1 minute: drain the Upstash event queue into the database
- Every 5 minutes: recalculate probabilities and generate new tiles
- Every 15 minutes: poll TfL for live traffic speeds
- Daily at 06:00 UTC: download updated enforcement (penalty charge notice) data

In `LOCAL_MODE` the scheduler is disabled entirely.

---

## What you need before starting

### Accounts and services

| Service | Purpose | Free tier | URL |
|---|---|---|---|
| **Supabase** | PostgreSQL database with PostGIS spatial queries | 500MB storage, 2 projects | https://supabase.com |
| **Upstash** | Redis — temporary queue and segment probability cache | 10,000 commands/day | https://upstash.com |
| **Cloudflare R2** | Stores and serves map tile files | 10GB storage, 1M reads/month | https://cloudflare.com |
| **Mapbox** | Renders the map in the browser | 50,000 map loads/month | https://mapbox.com |
| **Vercel** | Hosts the frontend and API | Unlimited static, 100GB bandwidth | https://vercel.com |
| **GitHub** | Stores the code and runs the daily tile refresh | Free | https://github.com |
| **TfL Unified API** | Live traffic speed data for Camden A-roads | Free with registration | https://api-portal.tfl.gov.uk |

### Provisioning Supabase

1. Create a new project at https://supabase.com/dashboard
2. Note your project's **database password** (set during creation)
3. Go to **Project Settings → Database**
4. Under **Connection string**, select **URI** and copy the string that uses port **6543** (labelled "Connection pooling"). It looks like:
   `postgresql://postgres.xxxx:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:6543/postgres`
5. Append `?pgbouncer=true` to the end of that URL

> **Why port 6543 and not 5432?** Port 6543 goes through PgBouncer, Supabase's connection pooler, which limits how many simultaneous database connections are used. The direct port 5432 can hit connection limits quickly under load. Always use 6543.

> **Why `?pgbouncer=true`?** This tells SQLAlchemy to disable features that are incompatible with connection pooling, like prepared statements.

6. Enable the PostGIS extension: go to **Database → Extensions**, search for `postgis`, enable it.

### Provisioning Upstash

1. Create a new Redis database at https://upstash.com
2. Copy the **REST URL** and **REST Token** from the database page

### Provisioning Cloudflare R2

1. In your Cloudflare dashboard, go to **R2 Object Storage → Create bucket**
2. Name it (e.g. `parkd-in`)
3. Go to **R2 → Manage R2 API tokens → Create API token** with "Object Read & Write" permission
4. Note: **Account ID**, **Access Key ID**, **Secret Access Key**
5. Go to the bucket → **Settings → Public access** → enable it and copy the **Public bucket URL**

### Provisioning Mapbox

1. Log in at https://mapbox.com
2. Go to **Tokens** and copy your **Default public token** (starts with `pk.`)

### Provisioning TfL API

1. Register at https://api-portal.tfl.gov.uk
2. Create an application subscription and copy the **API key**

---

## Local setup

### 1. Install Python

Download Python 3.11 from https://python.org/downloads. During installation, check **"Add Python to PATH"**.

Verify:
```
python --version
```
Expected output: `Python 3.11.x`

### 2. Get the code

```
git clone https://github.com/iamnickthegeek/parkd-in.git
cd parkd-in/predictive_parking
```

### 3. Create a virtual environment

A virtual environment is an isolated copy of Python that keeps this project's dependencies separate from everything else on your computer.

```
python -m venv venv
```

Activate it:
- **Windows:** `venv\Scripts\activate`
- **Mac/Linux:** `source venv/bin/activate`

You should see `(venv)` at the start of your terminal prompt. Run this activation command every time you open a new terminal for this project.

### 4. Install dependencies

```
pip install -r backend/requirements.txt
pip install pyarrow mapbox-vector-tile
```

### 5. Configure environment variables

Copy the example below into a file named `.env` in the `predictive_parking/` folder. Fill in your real values.

```
# See the full .env reference section below
LOCAL_MODE=true
MAPBOX_API_KEY=pk.your_key_here
...
```

See the **Complete .env reference** section for every variable.

### 6. Run database migrations (production mode only)

*Skip this step if `LOCAL_MODE=true`.*

A *migration* is a script that creates or updates database tables to match what the code expects.

```
set PYTHONPATH=backend
alembic upgrade head
```

### 7. Load road data into the database (production mode only)

*Skip this step if `LOCAL_MODE=true`.*

This downloads Camden's road network from OpenStreetMap (~2 minutes) and parking bay data from Camden Open Data, then inserts them into the database.

```
set PYTHONPATH=backend
python ingestion/run_ingestion.py
```

Expected output ends with:
```
Upserted segments: 8512 inserted, 0 updated
Bay upsert complete: 8205 inserted, 0 updated, ...
```

---

## Running the application

### Local mode (no database required)

**Step 1 — Generate tiles** (one-off, takes ~5 seconds):
```
set PYTHONPATH=backend
python generate_tiles_local.py
```
Output: `Done: 19 tiles written` and files created in `public/tiles/`.

**Step 2 — Start the backend:**
```
set PYTHONPATH=backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
`uvicorn` is the web server. `--reload` means it automatically restarts when you change a `.py` file.

**Step 3 — Start the frontend:**
```
cd frontend
python -m http.server 3000
```

Open http://localhost:3000 in your browser.

### Production mode (with database)

Same steps as above, but with `LOCAL_MODE=false` in `.env`. The backend will connect to Supabase, start the scheduler, and tiles will be uploaded to R2 instead of written to disk.

To generate and upload tiles to R2:
```
set PYTHONPATH=backend
python generate_tiles.py
```

---

## Full command reference

| Command | What it does |
|---|---|
| `python generate_tiles_local.py` | Generate map tiles from the local OSM cache, write to `public/tiles/`. No database needed. |
| `python generate_tiles.py` | Generate tiles using Supabase data, upload to Cloudflare R2. Requires `LOCAL_MODE=false` and a working database. |
| `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` | Start the API server. Must set `PYTHONPATH=backend` first. |
| `python -m http.server 3000` | Serve the frontend at http://localhost:3000. Run from `frontend/`. |
| `alembic upgrade head` | Apply all pending database migrations (create/alter tables). Run once after cloning, and after any schema change. |
| `alembic downgrade base` | Undo all migrations (drops all tables). Destructive — use only to reset a dev database. |
| `python ingestion/run_ingestion.py` | Download and import OSM roads, parking bays, and CPZ zones into the database. Takes 5–15 minutes on first run. |
| `vercel deploy --scope iamnickthegeeks-projects --no-wait` | Deploy to Vercel manually. Usually not needed — pushes to GitHub trigger automatic deployment. |
| `git push origin HEAD` | Push code to GitHub, triggering an automatic Vercel deployment. |

### Keepalive ping (run periodically to prevent Supabase free-tier auto-pause)

Run from `predictive_parking/` with the virtual environment active:

```
python -c "import psycopg2.extras; psycopg2.extras.HstoreAdapter.get_oids = staticmethod(lambda c: None); import sys; sys.path.insert(0,'backend'); from dotenv import load_dotenv; load_dotenv('.env'); import os; from sqlalchemy import create_engine, text; from sqlalchemy.pool import NullPool; url = os.environ['SUPABASE_DATABASE_URL'].split('?')[0]; e = create_engine(url, poolclass=NullPool, connect_args={'sslmode':'disable','connect_timeout':10}); c = e.connect(); print('DB rows:', c.execute(text('SELECT COUNT(*) FROM streetsegment')).scalar()); c.close(); e.dispose()"
```

---

## Complete .env reference

Create this file at `predictive_parking/.env`. Never commit it to git.

```ini
# ── Mode ─────────────────────────────────────────────────────────────────────
# true  = no database, no Redis, no R2. Tiles served from disk.
#         Use this for local development and on Vercel.
# false = full production stack. Requires all services below.
LOCAL_MODE=true

# ── Mapbox ───────────────────────────────────────────────────────────────────
# Public token from https://mapbox.com → Tokens.
# Safe to expose in frontend code (it's public by design).
MAPBOX_API_KEY=pk.eyJ1...

# ── Supabase (PostgreSQL database) ───────────────────────────────────────────
# Connection string from Supabase dashboard → Project Settings → Database.
# Use port 6543 (PgBouncer), not 5432 (direct). Append ?pgbouncer=true.
SUPABASE_DATABASE_URL=postgresql://postgres.YOURREF:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:6543/postgres?pgbouncer=true

# ── Upstash (Redis cache and queue) ──────────────────────────────────────────
# From Upstash dashboard → your Redis database → REST API section.
UPSTASH_REDIS_REST_URL=https://YOUR-INSTANCE.upstash.io
UPSTASH_REDIS_REST_TOKEN=AXXXabcdef...

# ── Cloudflare R2 (tile file storage) ────────────────────────────────────────
# Account ID: Cloudflare dashboard → right sidebar.
R2_ACCOUNT_ID=abc123...

# API token: R2 → Manage R2 API Tokens → Create API token (Read + Write).
R2_ACCESS_KEY_ID=your_access_key
R2_SECRET_ACCESS_KEY=your_secret_key

# Bucket name exactly as created in R2.
R2_BUCKET_NAME=parkd-in

# Public URL: bucket → Settings → Public access → URL.
R2_PUBLIC_URL=https://pub-XXXX.r2.dev

# S3-compatible endpoint: https://ACCOUNT_ID.r2.cloudflarestorage.com
R2_ENDPOINT_URL=https://abc123.r2.cloudflarestorage.com

# ── Security ─────────────────────────────────────────────────────────────────
# A random string used to sign JWT tokens (authentication cookies).
# Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=your_random_64_char_hex_string

# ── Monitoring ───────────────────────────────────────────────────────────────
# Optional. Error tracking from https://sentry.io.
# Leave blank to disable.
SENTRY_DSN=https://xxxx@xxxx.ingest.sentry.io/xxxx

# ── External data sources ────────────────────────────────────────────────────
# TfL API key from https://api-portal.tfl.gov.uk
TFL_API_KEY=your_tfl_api_key

# These URLs are public and do not need changing.
CAMDEN_DATA_URL=https://opendata.camden.gov.uk
LONDON_DATASTORE_URL=https://data.london.gov.uk

# ── General ──────────────────────────────────────────────────────────────────
ENVIRONMENT=development
CORS_ORIGINS=http://localhost:3000
```

---

## API endpoints

All endpoints are prefixed `/api/v1`.

| Method | Path | Auth required | Description |
|---|---|---|---|
| `GET` | `/health` | No | Service status, database connectivity, row counts |
| `POST` | `/auth/anonymous` | No | Returns a short-lived JWT token for the session |
| `POST` | `/parking/event` | Yes (JWT) | Submit a crowd report (PARKED / LEFT / SEARCHING) |
| `GET` | `/parking/probability` | No | Parking probability for segments near a lat/lon |
| `GET` | `/parking/best_nearby` | No | Top-ranked nearby segments |
| `GET` | `/parking/segment/{id}` | No | Full detail for one street segment |
| `GET` | `/api/v1/tiles/{z}/{x}/{y}.pbf` | No | Serve a map tile (disk in LOCAL_MODE, R2 in production) |

**Example — get probability near a point:**
```
GET /api/v1/parking/probability?lat=51.536&lon=-0.142&radius=200&window_min=10
```

---

## Troubleshooting

### Map loads but no coloured street segments appear

**Cause A — Tiles are not generated yet.**
Run `python generate_tiles_local.py` and confirm it prints `Done: 19 tiles written`. Then hard-refresh the browser (Ctrl+Shift+R).

**Cause B — The tile URL is wrong.**
Open browser developer tools (F12) → Network tab → reload the page → filter for `.pbf`. If requests are returning 404, the tile files are missing from `public/tiles/`. Re-run the tile generator.

**Cause C — Mapbox property name mismatch.**
The local tiles use a `color` property (e.g. `"00AA00"`). If you see segments in a single colour or black, check that `public/map.js` uses `["concat", "#", ["get", "color"]]` as the line colour expression, not `["get", "color_int"]`.

### `FATAL: Authentication error` when connecting to Supabase

**Cause A — Project is paused.** Supabase pauses free-tier projects after 7 days of no activity. Go to https://supabase.com/dashboard → find your project → click "Restore project". Wait 60 seconds.

**Cause B — Wrong port.** Ensure the URL uses port `6543`, not `5432`.

**Cause C — SSL conflict.** The connection must use `sslmode=disable` in the code's connect arguments. This is already set in `backend/app/db/database.py`. Do not change it.

### `SSL connection has been closed unexpectedly`

This happens on direct port 5432 connections. Switch to port 6543 (PgBouncer).

### Tile generation times out

The `generate_tiles.py` script (production mode) runs heavy PostGIS spatial queries over 23,000 road segments. If it times out:
- Check Supabase is not paused
- The `SET statement_timeout = '300000'` (5 minutes) is already applied before the query loop in `backend/app/core/engine.py`
- If still failing after 5 minutes, the Supabase project may be under resource pressure — try again during off-peak hours

### `ModuleNotFoundError` on startup

The virtual environment is not active, or dependencies are not installed.
```
venv\Scripts\activate        # Windows
pip install -r backend/requirements.txt
```

### Scheduler fires on every uvicorn restart

`LOCAL_MODE=true` disables the scheduler. If `LOCAL_MODE=false`, the scheduler starts on every uvicorn process startup, which is expected behaviour.

### GitHub Actions tile refresh fails

Go to your GitHub repository → **Actions** tab → click the failed run → expand the failing step to read the error message.

Common causes:
- Missing `cache/camden_osm.parquet` in the repo (re-commit it)
- `generate_tiles_local.py` dependency not installed in the Action (check `requirements-vercel.txt` contains `geopandas`, `mapbox-vector-tile`, `pyarrow`, `shapely`, `pyproj`)
- Workflow does not have write permission: go to **Settings → Actions → General → Workflow permissions** → set to "Read and write permissions"

---

## Security checklist

**Never commit or share:**
- `.env` file (already in `.gitignore`)
- `SECRET_KEY` — used to sign user tokens; anyone with it can forge authentication
- `SUPABASE_DATABASE_URL` — contains the database password; full read/write access to all data
- `R2_SECRET_ACCESS_KEY` — allows deleting all tile files
- `UPSTASH_REDIS_REST_TOKEN` — allows reading all cached probabilities and the event queue

**Safe to share or expose publicly:**
- `MAPBOX_API_KEY` — this is a *public* token by design; Mapbox expects it to be in browser code. Restrict its allowed URLs in the Mapbox dashboard to your domain.
- `R2_PUBLIC_URL` — this is the public CDN address for tile files; it is intentionally public

**Other precautions:**
- Rotate `SECRET_KEY` if you suspect it has been exposed. All existing user sessions will be invalidated.
- The `.vercelignore` file excludes `.env` from Vercel uploads. Set environment variables in the Vercel dashboard (Settings → Environment Variables) rather than shipping them in the deploy.
- Do not use port 5432 (direct Postgres) in production — it bypasses connection limits and can exhaust Supabase's free-tier connection cap.

---

## Deployment

### Overview

| Component | Where it runs | Trigger |
|---|---|---|
| Frontend (HTML/CSS/JS) | Vercel CDN | `git push` to GitHub |
| Map tiles (.pbf files) | Vercel CDN | `git push` after tile generation |
| API (FastAPI, LOCAL_MODE) | Vercel serverless function | `git push` to GitHub |
| Tile refresh schedule | GitHub Actions | Daily 06:00 UTC, or manual |

### Initial Vercel deployment

**Prerequisites:** Vercel CLI installed (`npm install -g vercel`), authenticated (`vercel login`).

1. From `predictive_parking/`:
   ```
   vercel deploy --scope YOUR_TEAM_SLUG --no-wait
   ```
   On first run this creates a Vercel project and links it to your GitHub repository.

2. Every subsequent `git push origin HEAD` will automatically trigger a new deployment. No manual steps needed.

3. The live URL will appear in `vercel ls --scope YOUR_TEAM_SLUG`.

### Setting environment variables on Vercel

The `.env` file is excluded from deployment. Add variables in the Vercel dashboard:

1. Go to https://vercel.com/dashboard → your project → **Settings → Environment Variables**
2. Add `LOCAL_MODE = true` (and any others needed)
3. Redeploy for changes to take effect

For the current LOCAL_MODE deployment, only `LOCAL_MODE=true` is strictly required — all other services are unused.

### Tile refresh automation (GitHub Actions)

The workflow at `.github/workflows/refresh-tiles.yml` runs daily at 06:00 UTC. It:
1. Generates new tiles from `cache/camden_osm.parquet`
2. Commits any changed `.pbf` files
3. Pushes to GitHub, which triggers a Vercel redeploy

To trigger it manually: GitHub → **Actions** tab → **Refresh Tiles** → **Run workflow**.

To change the schedule, edit the `cron` line in the workflow file. The format is `minute hour day month weekday`. For example, `0 8 * * 1-5` means 08:00 UTC on weekdays only. Use https://crontab.guru to build cron expressions.

### Switching to full production mode

When ready to enable the database, Redis, and R2:

1. Set `LOCAL_MODE=false` in `.env` (and in Vercel environment variables)
2. Restore or unpause the Supabase project
3. Run `alembic upgrade head` to apply migrations
4. Run `python ingestion/run_ingestion.py` to load road data
5. Run `python generate_tiles.py` to generate and upload tiles to R2
6. Redeploy to Vercel

The app will then use live data and the scheduler will start refreshing tiles every 5 minutes.
