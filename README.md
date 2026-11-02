# Homelab Dashboard

Python/FastAPI monitoring for Portainer-managed Docker environments. Discovers
nodes, normalizes container and stack state into SQLite, and records when a
container name appears on a different endpoint.

This public edition preserves the existing application's polling, storage,
movement detection, and HTML views. A synthetic Portainer adapter was added for
publication so it runs without access to the original homelab.

## Try it

```sh
docker compose up --build
```

Open http://localhost:8088. Every poll alternates a fictional inference container
between two fictional nodes. Visit `/moves` for movement history and `/health`
for the last poll. All demonstration records are generated locally.

Without Docker: install `requirements.txt`, set `DB_PATH` to a writable local
SQLite filename, and run `uvicorn app.main:app --host 127.0.0.1 --port 8088`.

## Architecture

Portainer API or synthetic adapter -> polling loop -> normalization -> SQLite
snapshots and movement history -> FastAPI -> Jinja HTML views.

Files of interest: `app/main.py` (original monitoring implementation),
`app/demo.py` (new synthetic adapter), and `tests/test_dashboard.py`.

## Connect your own environment

Copy `.env.example` to `.env`, set `DEMO_MODE=false`, `PORTAINER_URL` to your own
HTTPS endpoint and `PORTAINER_API_KEY` to your own token, then recreate the
container. TLS verification is always enabled in this edition. Use a trusted
certificate. No original addresses or tokens are supplied.

The dashboard displays infrastructure metadata and has no built-in user login.
Compose binds it to loopback; use an authenticated gateway before any remote
deployment. Movement detection compares names; duplicate names across nodes
can produce ambiguous results. Health reports process/storage availability and
last poll time, not a comprehensive cluster readiness check.

## Tests

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

Tests added for this public edition exercise normalization, movement detection,
HTML routes, health, and the guarantee that demonstration polling uses no HTTP.
