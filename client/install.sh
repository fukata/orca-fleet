#!/bin/bash
# Install the orca-fleet client and the Claude Code skill for the current user.
#   ./install.sh [--no-skill]
# On Windows, run from Git Bash; a .cmd launcher is installed for PowerShell/cmd.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -D -m 0755 "$here/orca-fleet" "$HOME/.local/bin/orca-fleet"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) install -D -m 0644 "$here/orca-fleet.cmd" "$HOME/.local/bin/orca-fleet.cmd" ;;
esac
if [ "${1:-}" != "--no-skill" ]; then
  install -D -m 0644 "$here/../skill/orca-fleet/SKILL.md" "$HOME/.claude/skills/orca-fleet/SKILL.md"
fi
echo "installed ~/.local/bin/orca-fleet"
echo "next: orca-fleet discover            # review"
echo "      orca-fleet discover --write    # write ~/.config/orca-fleet/config.json"
