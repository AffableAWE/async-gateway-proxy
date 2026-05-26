# Async API Gateway (MVP)

A minimal async API gateway / reverse proxy in Python. Routes incoming HTTP
requests to configured upstream services based on path prefixes.

## Architecture

client → gateway (FastAPI + Uvicorn)
│
├── /health  → liveness response
│
└── catch-all → route lookup (YAML config)
│
├── match → httpx.AsyncClient → upstream
└── no match → 404
