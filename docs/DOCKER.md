# Gem Search with Docker Desktop

Gem Search runs as one container, `gem-search`, from the image `gem-search:local`. All state lives in the named volume `gem-search-data`. The deployment is locked to **dry-run research mode**.

## Requirements

- Docker Desktop running Linux containers
- Docker Compose v2 or newer
- Run every command from the repository root (`C:\Projects\getm-search`)

## Start

```powershell
docker compose -f compose.yaml config --quiet   # validate
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml ps               # STATUS should read "healthy" within ~15 s
```

## Sign in to the dashboard

Open http://127.0.0.1:8787. The dashboard requires this install's sign-in token, because loopback-only publishing stops other machines from connecting but does not stop other programs on this computer. Get the link:

```powershell
docker exec gem-search python app.py --login-url
```

Open the printed link once. It sets an `HttpOnly`, `SameSite=Strict` cookie that lasts 30 days, then redirects you to the dashboard. You can also paste the token into the sign-in form. The token is stored in the volume at `/app/data/dashboard-token` (mode `0600`). It is never returned by any HTTP endpoint and never written to the service logs.

- **Rotate the token** (this signs out every browser): `docker exec gem-search rm /app/data/dashboard-token`, then `docker compose -f compose.yaml restart gem-search`.
- **Scripts on this machine** can send `Authorization: Bearer <token>` instead of the cookie.
- **Browser extension pairing** uses a separate key. It is shown only on the signed-in dashboard (**Connections**), and it grants only capture and status access. It cannot reach dashboard or launch routes.

## Health and logs

```powershell
docker compose -f compose.yaml ps
docker inspect --format '{{.State.Status}} / {{.State.Health.Status}}' gem-search
docker compose -f compose.yaml logs --tail 200
Invoke-WebRequest http://127.0.0.1:8787/healthz -UseBasicParsing   # {"ok": true}; no auth, no data
```

The health check calls `/healthz` inside the container every 30 s. Logs are capped at 3 × 10 MB.

## Stop without deleting data

```powershell
docker compose -f compose.yaml stop     # stop, keep the container
docker compose -f compose.yaml down     # remove the container and network; the volume is kept
```

> **Never** run `docker compose down -v`, `docker volume rm gem-search-data`, `docker volume prune` or `docker system prune --volumes`. Each of these deletes research data, job state and keys.

## Update and rebuild

```powershell
git pull --ff-only
docker compose -f compose.yaml build
docker compose -f compose.yaml up -d          # recreates only gem-search; the volume is reattached
docker compose -f compose.yaml ps
```

Take a backup (see below) before updates that change the schema or the queue.

### Updating the base image

The `FROM` line pins `python:3.12-slim` by its multi-arch index digest, so builds are reproducible and still work on amd64 and arm64. To take Python and Debian security updates:

```powershell
docker pull python:3.12-slim
docker buildx imagetools inspect python:3.12-slim   # copy the top-level "Digest:"
```

Replace the digest in `Dockerfile`, then rebuild, run the tests and commit.

## Safety settings

| Setting | Value | Effect |
|---|---|---|
| `LAUNCH_MODE` | `dry_run` | Queue rehearses offline only |
| `GEM_SEARCH_DRY_RUN_LOCK` | `1` | Forces dry-run even if `LAUNCH_MODE=live` is injected |
| `ENABLE_PAID_X` / `GROK_ENABLED` | `0` | No paid APIs |
| `SPIDER_ALLOW_LAUNCH` | `0` | Browser captures are research-only |
| `FREE_FEED_ENABLED` | `1` | Public Hacker News API, polled at most every 30 min, and only while autopilot is on |
| Port | `127.0.0.1:8787` | Loopback only |
| User | `10001` | Non-root; app code is root-owned and read-only |
| `read_only` | `true` | Only `/app/data` (volume) and `/tmp` (16 MB tmpfs, `noexec`) are writable |
| Limits | 512 MB RAM, 128 PIDs, 1 CPU | |
| Privileges | `cap_drop: ALL`, `no-new-privileges` | |

The image contains no `.env`, wallet, API key or `node_modules`. The Node launch signer is therefore absent, so even a live configuration could not sign transactions in this image. Do not add secrets to the image or to `compose.yaml`. Do not publish the port beyond `127.0.0.1`. If you change the host port, change `--port` (the container port) to match, because the server rejects requests whose `Host` header port differs from its own.

**Prohibited during normal safe research operation:**

- Setting `LAUNCH_MODE=live` or removing the dry-run lock
- Setting `ENABLE_PAID_X`, `GROK_ENABLED` or `SPIDER_ALLOW_LAUNCH` to `1`
- Mounting wallets or the Docker socket
- Using `privileged` mode or host networking
- Publishing the port on `0.0.0.0`
- Running any of the volume-deleting commands above

## Persistent data

The `gem-search-data` volume is mounted at `/app/data`:

| Path | Contents |
|---|---|
| `gem-search.sqlite` (+ `-wal`, `-shm`) | Projects, evidence, runs, events, launch drafts, queue jobs, caches |
| `dashboard-token`, `extension-key` | Sign-in token and extension pairing key (`0600`) |
| `inbox/` | Drop JSON captures here; processed every 5 min while autopilot is on |
| `processed/`, `failed/` | Archived inbox files. Invalid files go to `failed/`, are kept, and are never deleted on arrival |
| `launch-paused` | Present means the launch queue is paused, and it stays paused across restarts |
| `launch-jobs/` | Per-job recovery state, written by live signing only (absent in dry-run) |

**Retention:** files in `processed/` and `failed/` are removed after `GEM_SEARCH_RETENTION_DAYS` (30 by default). Expired crawl cache rows are removed after 24 h. The `inbox/` directory, `launch-jobs/` and research records are never pruned automatically.

**Monitor usage:**

```powershell
docker exec gem-search du -sh /app/data /app/data/*
docker system df -v | Select-String gem-search-data
```

SQLite runs in WAL mode. The `-wal` and `-shm` files belong to the database and must be backed up together with it, or not at all. If the volume fills, scans fail and report the failure. The inbox file stays in `inbox/` for a retry, and nothing is marked processed.

## Backup

Stop the service for a consistent copy of the whole tree, including the WAL:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
docker compose -f compose.yaml stop gem-search
docker run --rm -v gem-search-data:/data:ro -v "${PWD}\backups:/backup" python:3.12-slim `
  tar czf /backup/gem-search-data-$(Get-Date -Format yyyyMMdd-HHmmss).tgz -C /data .
docker compose -f compose.yaml start gem-search
```

`backups/` contains your tokens and keys. Keep it private and out of Git.

## Restore (test into an isolated volume first)

```powershell
docker volume create gem-search-restore-test
docker run --rm -v gem-search-restore-test:/data -v "${PWD}\backups:/backup:ro" python:3.12-slim `
  sh -c "tar xzf /backup/<file>.tgz -C /data && chown -R 10001:10001 /data"
docker run --rm -v gem-search-restore-test:/app/data gem-search:local python -c `
  "import sqlite3; c=sqlite3.connect('/app/data/gem-search.sqlite'); print(c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 'projects')"
docker volume rm gem-search-restore-test   # only the test volume
```

To restore into production, stop the service, extract into `gem-search-data` the same way, then start it again.

## Recovery: pending jobs and pause state

- On startup the queue reconciles any `running` or `pending` job before it reserves a new one. A job with an unknown outcome blocks new allocations instead of being retried as a new job. Dry-run jobs are completed offline and are never re-run.
- `launch-paused` survives restarts. Pause or resume from the dashboard, or with `POST /api/launch-control {"enabled": false}`.
- Jobs are pinned to the mode they were queued in, so a dry-run job never executes live. The mode, provider configuration and job shape are re-checked immediately before any provider call.

## Troubleshooting

- **`unhealthy`:** run `docker compose -f compose.yaml logs --tail 200`, then `docker exec gem-search python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8787/healthz').read())"`.
- **403 "Local requests only":** use `127.0.0.1:8787` or `localhost:8787`; other host names are rejected to prevent DNS rebinding.
- **Dashboard shows the sign-in page:** run `--login-url` again. The cookie is per browser and lasts 30 days.
- **Extension:** it uses `http://127.0.0.1:8787`. Re-pair from **Connections** after rotating `extension-key`.
