#!/bin/bash
# Move a running `orca serve` from a systemd *user* unit to a *system* unit
# (User=<operator>), the layout orca-serve-update supports. Run from this directory:
#
#   sudo ./migrate-to-system-unit.sh [--user USER] [--user-unit orca-serve.service]
#                                    [--install-deb VERSION] [--then-install] [--dry-run]
#
# - reads the serve arguments and PATH/LANG/DISPLAY... from the running serve process
# - --install-deb VERSION: switch an AppImage install to the official .deb first
#   (sha256 checked against the GitHub release digest)
# - enables lingering, writes /etc/systemd/system/orca-serve.service, stops and disables
#   the user unit, starts the system unit; if it does not listen, the user unit is restored
# - --then-install: run ./install.sh --operator USER afterwards
# PTY daemons live in their own scopes, so terminals are expected (not guaranteed) to survive.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

user="${SUDO_USER:-}"
user_unit=orca-serve.service
deb_version=""
then_install=0
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) user="$2"; shift 2 ;;
    --user-unit) user_unit="$2"; shift 2 ;;
    --install-deb) deb_version="$2"; shift 2 ;;
    --then-install) then_install=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] && id "$user" >/dev/null 2>&1 || { echo "bad user: '$user' (pass --user)" >&2; exit 2; }
[[ "$user_unit" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$ ]] || { echo "bad unit: $user_unit" >&2; exit 2; }
[ -z "$deb_version" ] || [[ "$deb_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad version: $deb_version" >&2; exit 2; }
[ ! -e /etc/systemd/system/orca-serve.service ] || { echo "/etc/systemd/system/orca-serve.service already exists" >&2; exit 1; }

uid=$(id -u "$user")
home=$(getent passwd "$user" | cut -d: -f6)
as_user() { runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"; }
run() { if [ "$dry_run" = 1 ]; then echo "+ $*"; else "$@"; fi; }
listening() { ss -ltn "sport = :$port" | grep -q LISTEN; }

echo "== discover the running serve ($user, user unit $user_unit)"
[ "$(as_user systemctl --user is-active "$user_unit" 2>/dev/null)" = active ] || { echo "user unit $user_unit is not active" >&2; exit 1; }
pid=$(pgrep -u "$user" -f 'cli/index\.js serve' | head -1 || true)
[ -n "$pid" ] || { echo "no running 'orca-ide serve' process for $user" >&2; exit 1; }
mapfile -d '' argv </proc/"$pid"/cmdline
serve_args=()
seen=0
for a in "${argv[@]}"; do
  if [ "$seen" = 1 ]; then serve_args+=("$a"); elif [ "$a" = serve ]; then seen=1; fi
done
for a in "${serve_args[@]}"; do [[ "$a" =~ ^[A-Za-z0-9_.:/=@-]+$ ]] || { echo "unexpected serve argument: $a" >&2; exit 1; }; done
[[ " ${serve_args[*]} " == *" --json "* ]] || serve_args+=(--json)
port=6768
for i in "${!serve_args[@]}"; do [ "${serve_args[$i]}" = --port ] && port="${serve_args[$((i + 1))]}"; done
[[ "$port" =~ ^[0-9]+$ ]] || { echo "bad port: $port" >&2; exit 1; }

declare -A env_vals=()
while IFS= read -r -d '' kv; do
  k="${kv%%=*}"
  case "$k" in PATH|LANG|DISPLAY|WAYLAND_DISPLAY|LIBGL_ALWAYS_SOFTWARE) env_vals[$k]="${kv#*=}" ;; esac
done </proc/"$pid"/environ
# drop AppImage mount/extract dirs from PATH (they disappear once the .deb is used)
if [ -n "${env_vals[PATH]:-}" ]; then
  env_vals[PATH]=$(tr ':' '\n' <<<"${env_vals[PATH]}" | grep -vE 'squashfs-root|/\.mount_' | paste -sd: -)
fi
echo "serve args: ${serve_args[*]}"
for k in "${!env_vals[@]}"; do echo "env: $k=${env_vals[$k]}"; done

if [ -n "$deb_version" ]; then
  echo "== install official orca-ide_${deb_version}_$(dpkg --print-architecture).deb"
  arch=$(dpkg --print-architecture)
  name="orca-ide_${deb_version}_${arch}.deb"
  sha=$(python3 -I - "$deb_version" "$name" <<'PY'
import json, sys, urllib.request
ver, name = sys.argv[1], sys.argv[2]
rel = json.load(urllib.request.urlopen(f"https://api.github.com/repos/stablyai/orca/releases/tags/v{ver}", timeout=30))
if rel.get("draft") or rel.get("prerelease"):
    sys.exit("refusing draft/prerelease")
digest = next(a.get("digest") or "" for a in rel["assets"] if a["name"] == name)
if not digest.startswith("sha256:"):
    sys.exit("no sha256 digest for " + name)
print(digest[7:])
PY
)
  deb="/var/cache/$name"
  if [ "$dry_run" = 1 ]; then
    echo "+ download $name (sha256 $sha) and apt-get install it"
  else
    if ! echo "$sha  $deb" | sha256sum -c --status 2>/dev/null; then
      curl -fL --proto '=https' -o "$deb.part" "https://github.com/stablyai/orca/releases/download/v${deb_version}/$name"
      echo "$sha  $deb.part" | sha256sum -c
      mv "$deb.part" "$deb"
    fi
    dpkg-deb -f "$deb" Package Version Architecture
    apt-get install -y "$deb"
    [ "$(dpkg-query -W -f='${Version}' orca-ide)" = "$deb_version" ]
  fi
fi
if [ "$dry_run" = 0 ] || [ -z "$deb_version" ]; then
  dpkg-query -W orca-ide >/dev/null 2>&1 && [[ "$(readlink -f /usr/bin/orca-ide)" == /opt/Orca/* ]] \
    || { echo "orca-ide is not installed from the .deb (pass --install-deb VERSION)" >&2; exit 1; }
fi
# ~/.local/bin usually comes first in PATH; keep it pointing at the .deb install
shim="$home/.local/bin/orca-ide"
if { [ -e "$shim" ] || [ -L "$shim" ]; } && [[ "$(readlink -f "$shim")" != /opt/Orca/* ]]; then
  run mv -n "$shim" "$shim.appimage-bak"
  run ln -s /usr/bin/orca-ide "$shim"
  run chown -h "$user:$user" "$shim"
fi

echo "== lingering"
run loginctl enable-linger "$user"

unit=$(cat <<EOF
[Unit]
Description=Orca headless runtime server (orca-ide serve)
Documentation=https://onorca.dev/docs/remote-servers
After=network-online.target user@${uid}.service
Wants=network-online.target user@${uid}.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${user}
WorkingDirectory=${home}
# Linux の Orca CLI は orca-ide。/usr/bin/orca は GNOME スクリーンリーダーなので使わない。
ExecStart=/usr/bin/orca-ide serve ${serve_args[*]}
Environment=HOME=${home}
Environment=XDG_RUNTIME_DIR=/run/user/${uid}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus
$(for k in "${!env_vals[@]}"; do echo "Environment=$k=${env_vals[$k]}"; done)
KillMode=control-group
Restart=on-failure
RestartPreventExitStatus=3 78
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
)
echo "== /etc/systemd/system/orca-serve.service"
echo "$unit"
if [ "$dry_run" = 1 ]; then
  echo "+ systemctl --user disable --now $user_unit (as $user); systemctl enable --now orca-serve.service"
  [ "$then_install" = 0 ] || echo "+ $here/install.sh --operator $user"
  exit 0
fi
printf '%s\n' "$unit" >/etc/systemd/system/orca-serve.service
systemctl daemon-reload

echo "== switch user unit -> system unit"
as_user systemctl --user disable --now "$user_unit"
for _ in $(seq 1 30); do listening || break; sleep 1; done
systemctl enable --now orca-serve.service
for _ in $(seq 1 60); do listening && break; sleep 1; done
if ! listening; then
  echo "orca serve is not listening on :$port; restoring the user unit" >&2
  systemctl disable --now orca-serve.service
  rm -f /etc/systemd/system/orca-serve.service
  systemctl daemon-reload
  as_user systemctl --user enable --now "$user_unit"
  exit 1
fi
systemctl --no-pager --lines=0 status orca-serve.service || true
echo "migrated. old user unit left in place but disabled: $user_unit"

if [ "$then_install" = 1 ]; then
  "$here/install.sh" --operator "$user"
fi
