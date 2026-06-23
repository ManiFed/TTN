# The Telescope Net — marketing site

Standalone static marketing page that pulls **live data from the cloud API**.
No build step, no dependencies.

## Run it (two servers)

The page fetches from the cloud API, so run both:

```bash
# 1. Cloud API (serves real data on :8800)
#    shared_models lives in src/, so put both repo root and src/ on the path
PYTHONPATH="$PWD:$PWD/src" venv/bin/python3 -m cloud.main cloud/config.yaml

# 2. Marketing site (static, on :4180)
python3 -m http.server 4180 --directory website
#    → open http://localhost:4180
```

If the API is down the page still renders — stats fall back to `—` and the
visuals use sensible defaults, so it is never broken.

## Seed demo data

A fresh database is empty. Populate the real tables so the endpoints return
realistic content (23 nodes across 14 countries + an SS Cyg dwarf-nova light curve):

```bash
venv/bin/python3 scripts/seed_demo.py --wipe
```

Re-run any time to refresh node heartbeats (seeded nodes drop to "offline" after
15 minutes — real nodes heartbeat every 60s and stay online on their own).

## What's wired to the API

| UI element | Endpoint | Field(s) |
|------------|----------|----------|
| Hero stats + badge | `/api/v1/network/status` | `aavso_submitted`, `nodes_online`, acceptance, distinct countries |
| Hero reliability gauge | `/api/v1/network/status` | highest-`reliability_score` node |
| Live observations console | `/api/v1/lightcurves/SS Cyg` | recent `node_id` / `magnitude` / `received_at` |
| Light-curve section | `/api/v1/lightcurves/SS Cyg` | `magnitude` (mag-inverted axis), `aavso_submitted` → amber points |
| Network map dots | `/api/v1/network/status` | each node's `latitude` / `longitude`, online state |
| Hero reticle labels | `/api/v1/targets` | catalogue `name` + `mag` |

The cloud server sends permissive CORS headers (see `cloud/server.py` `_cors`)
so the browser can read these from another origin in dev.

## Files

| File | Purpose |
|------|---------|
| `index.html` | Structure and copy |
| `styles.css` | Design system (Lambda × Resend × Clear Street) |
| `app.js` | Data layer, plate-solving starfield, node builder, light curves, map |

## Design notes

- **Hero animation:** a drifting star field where targeting reticles acquire
  real catalogue objects one at a time — mirroring what the pipeline actually
  does (plate-solve → identify → measure). No decorative gradients.
- **Node Builder** (`#builder`) mirrors the Mac mini purchasing flow: choosing a
  telescope tier and toggling autonomy hardware lights up modules on a Seestar
  technical schematic and updates a live projected `reliability_score` — the same
  fields and formula the cloud scheduler uses (`cloud/registry.py`).

This HTML/CSS/JS maps 1:1 onto React/Next.js components when you move to a
build-based stack for the member dashboard.
