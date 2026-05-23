# kondev Dashboard

The central management dashboard for a self-hosted Oracle Cloud (ARM) server.
A single page shows running services, live resource usage, and — for the
administrator — central user management across all services.

Part of the **kondev** stack (Caddy reverse proxy + Authentik SSO + Docker
services such as a file vault and a voice chat).

## Features

- **Service catalog** — lists Docker services with live status; start stopped
  ones with one click. Newly detected containers are surfaced to the admin.
- **Resource monitoring** — real-time CPU / RAM / disk and a per-service
  breakdown, collected from the Docker stats API.
- **Central login (SSO)** — sign in once via Authentik (OIDC); the same account
  works across every service. Admin rights are derived from an Authentik group.
- **User management (admin)** — list users and reset password, change email,
  resend verification, deactivate/activate, or fully delete a user. Actions are
  propagated to the other services so a disable/delete takes effect immediately.
- **Mobile-friendly** — responsive layout for phones.

## Stack

| Layer | Tech |
|-------|------|
| Backend | FastAPI (Python), talks to the Docker socket and the Authentik API |
| Frontend | Alpine.js + Tailwind CSS (no build step), Lucide icons |
| Runtime | Docker; reachable through Caddy on the internal `web` network |

## Run

```bash
cp .env.example .env   # fill in OIDC + Authentik API + internal token values
docker compose up -d --build
```

The container needs access to `/var/run/docker.sock` (to read service status and
start containers) and reaches Authentik over the internal Docker network.

## Configuration

All settings come from `.env` (never committed). Key groups:

- **OIDC** (`OIDC_*`) — central Authentik login.
- **Authentik API** (`AUTHENTIK_API_*`) — user management.
- **Internal token** (`INTERNAL_API_TOKEN`) — shared secret used to propagate
  deactivation/deletion to the other services; must match their `.env`.

> **Security:** `.env` holds secrets (API tokens, admin password). It is
> git-ignored and must never be committed.

## License

Proprietary — all rights reserved. Use, copying, or deployment without the
author's written permission is prohibited. See [LICENSE](LICENSE).
