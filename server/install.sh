#!/bin/bash
# Install orca-serve-update on one server (run as root, from this directory).
#
#   sudo ./install.sh [--service orca-serve.service] [--operator USER]...
#                     [--self-update-repo OWNER/NAME [--self-update-ref main]] [--dry-run]
#
# - installs /usr/local/sbin/orca-serve-update (root:root 0755)
# - creates group orca-updaters and a sudoers rule limited to the helper's
#   public subcommands (never `_worker` or `init`)
# - discovers the existing orca serve unit and writes /etc/orca-serve-update/config.json
# - --self-update-repo: lets `orca-serve-update self-update` replace the helper with
#   server/orca-serve-update from that GitHub repo/ref (root-owned config; operators
#   can trigger it but cannot choose the source). Off unless given.
# Does NOT stop, restart or modify the running orca serve service.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
service=""
operators=()
dry_run=0
self_repo=""
self_ref="main"
while [ $# -gt 0 ]; do
  case "$1" in
    --service) service="$2"; shift 2 ;;
    --operator) operators+=("$2"); shift 2 ;;
    --self-update-repo) self_repo="$2"; shift 2 ;;
    --self-update-ref) self_ref="$2"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
for u in "${operators[@]}"; do
  [[ "$u" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] && id "$u" >/dev/null 2>&1 || { echo "bad operator user: $u" >&2; exit 2; }
done
[ -z "$self_repo" ] || [[ "$self_repo" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$ ]] || { echo "bad repo: $self_repo" >&2; exit 2; }
[[ "$self_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ && "$self_ref" != *..* ]] || { echo "bad ref: $self_ref" >&2; exit 2; }
[ -z "$service" ] || [[ "$service" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$ ]] || { echo "bad unit: $service" >&2; exit 2; }
command -v python3 >/dev/null && command -v systemd-run >/dev/null && command -v dpkg >/dev/null \
  || { echo "requires python3, systemd-run and dpkg" >&2; exit 1; }

run() { if [ "$dry_run" = 1 ]; then echo "+ $*"; else "$@"; fi; }

run install -o root -g root -m 0755 "$here/orca-serve-update" /usr/local/sbin/orca-serve-update
getent group orca-updaters >/dev/null || run groupadd --system orca-updaters
for u in "${operators[@]}"; do run usermod -aG orca-updaters "$u"; done

sudoers_tmp="$(mktemp)"
trap 'rm -f "$sudoers_tmp"' EXIT
cat >"$sudoers_tmp" <<'EOF'
# orca-serve-update: members of orca-updaters may run the updater's public
# subcommands as root. Arguments are validated by the helper itself.
Cmnd_Alias ORCA_SERVE_UPDATE = /usr/local/sbin/orca-serve-update --json status, \
    /usr/local/sbin/orca-serve-update --json check *, \
    /usr/local/sbin/orca-serve-update --json prepare *, \
    /usr/local/sbin/orca-serve-update --json apply *, \
    /usr/local/sbin/orca-serve-update --json rollback, \
    /usr/local/sbin/orca-serve-update --json rollback *, \
    /usr/local/sbin/orca-serve-update --json recover, \
    /usr/local/sbin/orca-serve-update --json self-update, \
    /usr/local/sbin/orca-serve-update --json self-update --dry-run
Cmnd_Alias ORCA_SERVE_UPDATE_DENY = /usr/local/sbin/orca-serve-update *_worker*, \
    /usr/local/sbin/orca-serve-update *init*
Defaults!ORCA_SERVE_UPDATE env_reset, !setenv
%orca-updaters ALL=(root) NOPASSWD: ORCA_SERVE_UPDATE, !ORCA_SERVE_UPDATE_DENY
EOF
visudo -cf "$sudoers_tmp" >/dev/null
run install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/orca-serve-update

init_args=(init --write)
[ -n "$service" ] && init_args+=(--service "$service")
if [ "$dry_run" = 1 ]; then
  python3 -I "$here/orca-serve-update" init ${service:+--service "$service"}
else
  /usr/local/sbin/orca-serve-update "${init_args[@]}"
fi

if [ -n "$self_repo" ]; then
  if [ "$dry_run" = 1 ]; then
    echo "+ enable self_update from $self_repo@$self_ref in /etc/orca-serve-update/config.json"
  else
    python3 -I - "$self_repo" "$self_ref" <<'PY'
import json, os, sys
p = "/etc/orca-serve-update/config.json"
cfg = json.load(open(p))
cfg["self_update"] = {"repo": sys.argv[1], "ref": sys.argv[2]}
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(tmp, 0o644)
os.replace(tmp, p)
PY
    echo "self-update enabled: $self_repo@$self_ref"
  fi
fi

user="$(python3 -I -c 'import json;print(json.load(open("/etc/orca-serve-update/config.json"))["service_user"])' 2>/dev/null || true)"
if [ -n "$user" ] && [ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null)" != yes ]; then
  echo "WARNING: lingering is off for $user. Orca's PTY daemon then lives inside the service cgroup and" >&2
  echo "         every restart ends running terminals. Enable with: loginctl enable-linger $user" >&2
fi
echo "installed. operators in group orca-updaters: ${operators[*]:-(none added)}"
