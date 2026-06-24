# Deploying RAMS on Vercel

RAMS runs well on Vercel's **Python serverless** runtime: it already uses a
`BaseHTTPRequestHandler` and is **stateless** server-side (uploaded data lives in
the browser and is POSTed back on each call), and it has **zero third-party
dependencies**. Two files make it deployable:

- `api/index.py` — re-exports RAMS' `RAMSHandler` as Vercel's `handler`.
- `vercel.json` — a catch-all rewrite so the SPA (`/`), all `/api/*` routes and
  `/healthz` are served by that one function; `includeFiles` bundles the `rams/`
  package.

## Deploy

**Option A — Vercel CLI**
```bash
npm i -g vercel
cd <repo root>
vercel          # preview deploy (first run links/creates the project)
vercel --prod   # production deploy
```

**Option B — Git import**
Push the repo to GitHub/GitLab and "Import Project" in the Vercel dashboard. No
build settings are needed — `vercel.json` is picked up automatically.

## Verify
After deploy, open:
- `https://<your-app>.vercel.app/healthz` → `{"status": "ok"}`
- `https://<your-app>.vercel.app/` → the dashboard.

## What to know (limits)

| Topic | Detail |
|-------|--------|
| **Request body size** | Vercel serverless caps the request body at **~4.5 MB**. The base64 upload path (`/api/ingest_multi`) inflates files ~33%, so the practical file limit is ~3 MB; the streaming path (`/api/upload`) allows ~4.5 MB. **Very large NSV workbooks won't upload on Vercel** — for those, run RAMS locally (`python -m rams.server`) or on a normal VM/container. |
| **Execution time** | `maxDuration` is set to 30 s in `vercel.json`. RAMS computations are sub-second, so this is ample (Hobby allows up to 60 s, Pro up to 300 s). |
| **State** | None server-side — every request is self-contained, so serverless scaling is safe. |
| **Temp files** | The upload handler streams to `/tmp`, which is writable on Vercel. |
| **Python** | Auto-detected (3.12). RAMS targets 3.8+. |

## Alternatives for unlimited upload size
RAMS is a single stdlib process, so it also runs unchanged on any VM, container
or PaaS (`python -m rams.server --port $PORT`). Use that if you routinely ingest
large multi-MB survey workbooks.
