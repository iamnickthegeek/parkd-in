# Deployment Checklist

## Pre-deployment (done)
- [x] All 21 tests passing
- [x] Static analysis clean (black, ruff, mypy)
- [x] .env in .gitignore
- [x] No hardcoded secrets in codebase
- [x] Private GitHub repo created

## Render Dashboard (manual steps)
1. Go to https://dashboard.render.com
2. Select your parkd-in service
3. Go to **Environment** tab
4. Add these variables:
   - `TFL_API_KEY` = (from your .env file)
   - `CORS_ORIGINS` = `https://YOUR_R2_PUBLIC_URL.r2.dev`
   - `ENVIRONMENT` = `production`
5. All other vars should already be set from initial provisioning

## UptimeRobot (manual step)
1. Go to https://uptimerobot.com
2. Add monitor: HTTP(S)
3. URL: `https://your-render-url.onrender.com/api/v1/health`
4. Interval: 5 minutes
5. This prevents Render from sleeping

## Verify Deployed
```bash
curl https://your-render-url.onrender.com/api/v1/health
```
Expected: `segment_count > 1500`, `db_connected: true`, `redis_connected: true`
