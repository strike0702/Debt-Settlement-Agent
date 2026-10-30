# Hosting on AWS

The voice demo runs on one small EC2 instance. Text works on any host; voice needs real CPU,
because Pipecat decodes the mic, runs the Silero VAD, resamples for Deepgram and encodes the
agent's audio in Python for every 20 ms frame. On Render's free instance (0.1 CPU) the agent's
speech came out slow and choppy and transcripts lagged. On EC2 one call peaked at about 0.7 of a
CPU (35% of a t3.small) and averaged 12%, and it sounded the same as on a laptop
([D17](history/DECISIONS.md)).

## What runs where

| Piece | Choice | Why |
|---|---|---|
| Instance | `t3.small` (2 vCPU burstable, 2 GB), Ubuntu 24.04, `ap-southeast-2` | 0.4 vCPU sustained plus burst credits, which covers a call's 0.7 vCPU peaks. `CpuCredits=standard`, so it slows down instead of billing extra when credits run out. |
| Disk | 20 GB gp3, 2 GB swap file | About 600 MB of Python packages plus the web build; the SQLite audit log and voice budget live here and survive restarts. |
| Address | Elastic IP, served as `https://<ip-with-dashes>.sslip.io` | A stable IP keeps the hostname and certificate valid across stop/start; sslip.io resolves the name to the IP without buying a domain. |
| HTTPS | Caddy, automatic Let's Encrypt certificate | Browsers only allow the microphone on HTTPS. Caddy forwards to the app on `127.0.0.1:8000` (WebSockets included), so the app is never exposed directly. |
| App | systemd unit `dsa`: `uv run --no-dev uvicorn app.main:app --host 127.0.0.1 --port 8000` | Starts at boot, restarts on crash, logs to `journalctl -u dsa`. Non-secret settings (same as `render.yaml`, plus `VOICE_SERVER_TURN=false`) are `Environment=` lines in the unit; secrets are in `/opt/dsa/.env` (mode 600). |
| Instance metadata | IMDSv2 required | Blocks the classic SSRF path to instance credentials. |

## Network rules (security group)

| Port | From | Why |
|---|---|---|
| TCP 22 | the owner's IP only (`/32`) | SSH for administration; nobody else can even attempt a login. Update the rule when the owner's IP changes. |
| TCP 80 | anywhere | Let's Encrypt's HTTP challenge; Caddy redirects to HTTPS. |
| TCP 443 | anywhere | The site, its WebSocket and WebRTC signalling. |
| UDP | **no inbound rule** | Voice audio is UDP on a random port per call. Security groups are stateful: once the server sends to the browser during ICE, the browser's replies are let back in, so a direct path usually opens without opening about 64,000 ports. When it does not (strict networks), the browser relays through Cloudflare TURN and the server still only sends outward. |

The server's own ICE uses STUN only (`VOICE_SERVER_TURN=false`): it has a public IP, so it needs
no relay of its own, and aioice's TURN channel bind against Cloudflare fails with a 401 anyway.
The browser still gets short-lived Cloudflare TURN credentials from `GET /voice/ice`. Add
`?ice=relay` to the page URL to force the relay and check it works.

## Deploy

Push to `main`, then:

```bash
scripts/deploy_aws.sh
```

It pulls `origin/main` on the server, rebuilds `web/dist`, syncs Python packages, restarts the
service and waits for `/healthz` over HTTPS. It never reads or copies secrets.

## Operate

```bash
ssh -i ~/.ssh/dsa-aws.pem ubuntu@<elastic-ip>
sudo journalctl -u dsa -f          # app log
sudo systemctl restart dsa         # restart the app
top                                # CPU during a call
```

Secrets: edit `/opt/dsa/.env` on the server, then `sudo systemctl restart dsa`. For a production
service they would come from AWS Secrets Manager or SSM Parameter Store through an instance role
instead of a file.

Cost: about $19 a month for the instance plus about $3.60 for the public IPv4 address. Stopping
the instance stops the compute charge (the Elastic IP is still billed); terminating it and
releasing the address ends both.
