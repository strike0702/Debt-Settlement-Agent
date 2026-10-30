#!/usr/bin/env bash
# Deploy main to the AWS demo host (EC2 + Caddy + systemd, see docs/DEPLOY_AWS.md).
#
# Pulls the pushed main on the server, rebuilds web/dist, syncs Python deps,
# restarts the `dsa` service and waits for /healthz through HTTPS. Push first:
# the server deploys what is on GitHub, not your working tree. Secrets stay in
# /opt/dsa/.env on the server; this script never reads or copies them.
#
# Usage: scripts/deploy_aws.sh   (DSA_HOST / DSA_KEY override the defaults)
set -euo pipefail

HOST="${DSA_HOST:-13.236.102.216}"
KEY="${DSA_KEY:-$HOME/.ssh/dsa-aws.pem}"
URL="${DSA_URL:-https://13-236-102-216.sslip.io}"

ssh -i "$KEY" "ubuntu@$HOST" 'bash -s' <<'EOF'
set -euo pipefail
cd /opt/dsa
git fetch -q origin main
git reset -q --hard origin/main
echo "deploying $(git log --oneline -1)"
(cd web && PATH=/opt/node/bin:$PATH npm ci --no-audit --no-fund --silent && PATH=/opt/node/bin:$PATH npm run build --silent >/dev/null)
~/.local/bin/uv sync --no-dev -q
sudo systemctl restart dsa
EOF

for _ in $(seq 1 45); do
  if curl -fs -m 3 "$URL/healthz" >/dev/null; then
    echo "up: $URL"
    exit 0
  fi
  sleep 2
done
echo "not healthy after 90 s; check: ssh -i $KEY ubuntu@$HOST 'sudo journalctl -u dsa -n 50'" >&2
exit 1
