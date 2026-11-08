# Pro Downloader

Telegram bot for public or otherwise authorized media, configured for [@prodownloadersbot](https://t.me/prodownloadersbot). Send one link and choose Original / best, a resolution, MP3, or All media / playlist. The public experience uses a Telegram private chat. The bot configures its Telegram commands and menu at startup.

## Deploy on Hostinger

Use Docker Engine 28 or later and Docker Compose v2 on Linux. Docker needs support for an internal bridge with isolated gateway mode. Check free memory and disk before building. Default extraction allows two concurrent jobs, with a 200 MiB total output cap per job and 2 GiB retained file storage. The extractor alone is allowed up to 1.4 GiB of memory. The browser image requires several additional GiB during the build.

Run these on the VPS.

```bash
docker version --format '{{.Server.Version}}'
docker compose version
free -h
df -h / /var/lib/docker
cd /root
GIT_TERMINAL_PROMPT=0 git clone https://github.com/promotionsbenedict/prodownloaderbot.git
cd /root/prodownloaderbot
umask 077
cp -n .env.example .env
chmod 600 .env
openssl rand -hex 32
nano .env
```

Set `BOT_TOKEN` from BotFather, `POSTGRES_PASSWORD` to the generated value, and `DENY_NETWORKS` to every public IPv4/IPv6 on this VPS and any public management IPs serving private resources. The token should exist only in this file on the VPS. Set numeric Telegram IDs in `ADMIN_IDS` and a support contact if desired. The migration adds admins without deleting existing grants. To revoke an admin, remove their ID from the environment list and delete their row from `admins`. Store the password before first start since changing the environment later does not rotate an initialized PostgreSQL role.

```bash
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
docker compose ps -a
docker compose logs --tail=50 bot worker extractor
docker compose exec -T postgres pg_isready -U prodownloader -d prodownloader
docker compose exec -T redis redis-cli ping
```

The Compose project is `prodownloader`. Default services publish zero host ports. The one-time migration container should exit successfully. Telegram polling clears any webhook registered for this specific bot token. Keep one polling instance.

### Optional large files

Telegram sends files up to 49,000,000 bytes through this application. To deliver larger files, provision a dedicated HTTPS hostname and set `PUBLIC_BASE_URL` and a separate random `FILE_SIGNING_KEY` generated with `openssl rand -hex 32`. Set an unused loopback `LINK_PORT` and configure that hostname to proxy only `/files/*` to the loopback port. If Caddy runs in a container, loopback refers to Caddy's own network namespace. In that case connect only the API to a suitable existing proxy network through a Compose override and use its service alias instead of the loopback override. Omit access logging for the signed paths and serve only through TLS. Link recipients can forward links until they expire, normally after 15 minutes. Use matching Compose flags for future updates.

```bash
docker compose -f compose.yaml -f compose.links.yaml up -d --build --wait
```

### Update

```bash
cd /root/prodownloaderbot
GIT_TERMINAL_PROMPT=0 git pull --ff-only origin main
docker compose up -d --build --wait --wait-timeout 180
```

### Production acceptance

- Check `telegram_ready` with six commands in the logs and ensure the polling container becomes healthy.
- From a Telegram account, run each command, paste a public test URL, and confirm that each selected format arrives. Repeat for an actual public photo, album, playlist, and audio file.
- Run two downloads from two different Telegram accounts. Cancel a running job from one account and confirm the other completes. Repeat a successful request and confirm it still delivers.
- Restart only this project's worker during a queued or running download with `docker compose restart worker`. Within the lease window the job should be reclaimed. If a restart happens during delivery, check the chat for partial delivery before retrying.
- Run `/admin` as a configured admin. Check the queue, recent failures, and worker heartbeat.
- Test a signed link if HTTPS delivery is enabled. Confirm it downloads before expiry and returns 404 after expiry.
- Record URL, media type, selected format, date, and result before advertising any source platform as verified. Site login requirements, upstream extractor breakages, and VPS IP restrictions can affect individual URLs.

## Behavior and operational limits

- Site identification is for display and statistics. Extraction is open-ended and uses site-specific or generic video extractors, direct media, public gallery URLs, and browser metadata or in-page media when available. Quality caps are offered up to the requested height and may be unavailable at particular sources. Live capture, private media, DRM circumvention, and social account login are outside the supported scope.
- Album mode has an eight-file limit by default. Individual posts and playlists can expose fewer items to an anonymous client. Original / best returns the available upstream quality without a size or quality guarantee beyond the configured output cap.
- PostgreSQL holds the authoritative queue and leases. Redis handles notification, limits, heartbeat, and reusable Telegram file IDs. History and system events expire after 30 days and daily platform aggregates after 90 days. File cache normally expires after one hour. Source URLs are stored with job history and are omitted from public responses and application logs.
- The extractor has no bot or database credentials. Its internet requests use a constrained forward proxy that checks each destination, resolves DNS once per connection, and connects to approved public addresses. The configured host IP deny list is required. Keep the extraction network internal and isolated.
- Retries are bounded and only scheduled for transient extraction problems. Partial Telegram delivery can occur if a process dies between uploads. The queue records delivery interruption without automatically resending files to the user.

## Development tests

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-extractor.txt pytest==9.2.0
python -m pytest -q
```

Set `TEST_DATABASE_URL` and `TEST_REDIS_URL` to disposable local services to enable the queue integration test. That test clears fixture tables. CI supplies isolated PostgreSQL and Redis, FFmpeg, and a Docker Compose startup check for the API and extractor.
