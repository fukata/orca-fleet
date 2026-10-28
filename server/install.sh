#!/bin/bash
# Install orca-serve-update on one server (run as root, from this directory).
#
#   sudo ./install.sh [--service orca-serve.service] [--operator USER]...
#                     [--self-update-repo OWNER/NAME [--self-update-ref main]] [--dry-run]
#   sudo ./install.sh --desktop [--user USER] ...   # this machine's .deb desktop app instead
#
# - installs /usr/local/sbin/orca-serve-update (root:root 0755)
# - creates group orca-updaters and a sudoers rule limited to the helper's
#   public subcommands (never `_worker` or `init`)
# - discovers the existing orca serve unit and writes /etc/orca-serve-update/config.json
# - --self-update-repo: lets `orca-serve-update self-update` replace the helper with
#   server/orca-serve-update from that GitHub repo/ref (root-owned config; operators
#   can trigger it but cannot choose the source). Off unless given.
# - --desktop: manage USER's (default: the sudo caller) .deb desktop app instead of an
#   orca serve unit; USER also becomes an operator, with a per-user sudoers line so it
#   works without logging in again.
# Does NOT stop, restart or modify the running orca serve service (or desktop app).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
service=""
operators=()
dry_run=0
self_repo=""
self_ref="main"
desktop=0
desktop_user=""
while [ $# -gt 0 ]; do
  case "$1" in
    --service) service="$2"; shift 2 ;;
    --operator) operators+=("$2"); shift 2 ;;
    --self-update-repo) self_repo="$2"; shift 2 ;;
    --self-update-ref) self_ref="$2"; shift 2 ;;
    --desktop) desktop=1; shift ;;
    --user) desktop_user="$2"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
if [ "$desktop" = 1 ]; then
  desktop_user="${desktop_user:-${SUDO_USER:-}}"
  [ -n "$desktop_user" ] && [ "$desktop_user" != root ] || { echo "--desktop: pass --user USER" >&2; exit 2; }
  [[ " ${operators[*]} " == *" $desktop_user "* ]] || operators+=("$desktop_user")
fi
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
Defaults!ORCA_SERVE_UPDATE env_reset, !setenv
EOF
# Every allowed command fixes the subcommand right after --json, so the deny list is only
# defense in depth. sudo-rs (Ubuntu 26.04+) rejects its mid-argument wildcards; skip it there.
grant="ORCA_SERVE_UPDATE"
if ! sudo --version 2>/dev/null | grep -q '^sudo-rs'; then
  cat >>"$sudoers_tmp" <<'EOF'
Cmnd_Alias ORCA_SERVE_UPDATE_DENY = /usr/local/sbin/orca-serve-update *_worker*, \
    /usr/local/sbin/orca-serve-update *init*
EOF
  grant="ORCA_SERVE_UPDATE, !ORCA_SERVE_UPDATE_DENY"
fi
echo "%orca-updaters ALL=(root) NOPASSWD: $grant" >>"$sudoers_tmp"
# per-user lines too: a new group membership only applies after the next login
for u in "${operators[@]}"; do
  echo "$u ALL=(root) NOPASSWD: $grant" >>"$sudoers_tmp"
done
visudo -cf "$sudoers_tmp" >/dev/null
run install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/orca-serve-update

init_args=(init --write)
[ -n "$service" ] && init_args+=(--service "$service")
[ "$desktop" = 1 ] && init_args+=(--desktop --user "$desktop_user")
if [ "$dry_run" = 1 ]; then
  python3 -I "$here/orca-serve-update" init "${init_args[@]:2}"
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
if [ "$desktop" = 0 ] && [ -n "$user" ] && [ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null)" != yes ]; then
  echo "WARNING: lingering is off for $user. Orca's PTY daemon then lives inside the service cgroup and" >&2
  echo "         every restart ends running terminals. Enable with: loginctl enable-linger $user" >&2
fi
echo "installed. operators in group orca-updaters: ${operators[*]:-(none added)}"
