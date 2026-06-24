"""Vercel serverless entry point for the RAMS dashboard.

Vercel's Python runtime invokes a class named `handler` that subclasses
`http.server.BaseHTTPRequestHandler`. RAMS' own `RAMSHandler` already is one and
is fully stateless per request (uploaded data lives in the browser and is POSTed
back on each call), so we simply re-export it. All routes -- the SPA at `/`, the
`/api/*` endpoints and `/healthz` -- are served by this single function via the
catch-all rewrite in `vercel.json`.
"""
import os
import sys

# Make the repo root importable so `rams` resolves when bundled by Vercel.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rams.server import RAMSHandler  # noqa: E402


class handler(RAMSHandler):  # noqa: N801 - Vercel requires the name `handler`
    pass
