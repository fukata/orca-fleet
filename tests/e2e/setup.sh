#!/bin/bash
# Stage 1: boot a systemd container that mirrors the dev server and start orca serve 1.4.x.
# usage: setup.sh <initial.deb>
set -euo pipefail
deb="$1"; name=orca-update-e2e
here="$(cd "$(dirname "$0")" && pwd)"; repo="$here/../.."
docker rm -f $name >/dev/null 2>&1 || true
docker run -d --name $name --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock orca-update-e2e >/dev/null
for i in $(seq 30); do docker exec $name systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break; sleep 1; done
docker cp "$deb" $name:/root/initial.deb
docker exec $name bash -c 'apt-get update -qq && apt-get install -y -qq /root/initial.deb >/dev/null 2>&1; dpkg-query -W orca-ide'
docker exec $name useradd -m -s /bin/bash -g users opsuser
docker exec $name useradd -m -s /bin/bash intruder
docker exec $name loginctl enable-linger orcauser
docker exec -i $name bash -c 'cat > /etc/systemd/system/orca-serve.service' <<'UNIT'
[Unit]
Description=Orca runtime server
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=orcauser
WorkingDirectory=/home/orcauser
Environment=LIBGL_ALWAYS_SOFTWARE=1
Environment=SHELL=/bin/bash
ExecStart=/usr/bin/orca-ide serve --port 6768 --pairing-address 127.0.0.1 --json
KillMode=control-group
TimeoutStopSec=30
Restart=on-failure
RestartPreventExitStatus=3
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
docker exec $name systemctl daemon-reload
docker exec $name systemctl enable --now orca-serve.service
for i in $(seq 90); do
  out=$(docker exec -u orcauser $name bash -c 'orca-ide status --json 2>/dev/null' || true)
  echo "$out" | grep -q '"state": "ready"' && break; sleep 2
done
docker exec -u orcauser $name bash -c 'orca-ide status --json' | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]["runtime"]; print("serve:", r["state"], r["appVersion"])'
docker cp "$repo/server" $name:/root/osu
echo setup-done
