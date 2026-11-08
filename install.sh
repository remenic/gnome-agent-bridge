#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Install (or reinstall) the Agent Bridge GNOME Shell extension.
#
# The GNOME Shell only scans ~/.local/share/gnome-shell/extensions at startup and
# GNOME 51 does not implement org.gnome.Shell.Extensions.ReloadExtension, so a
# newly installed extension becomes active on the next login.
set -euo pipefail

UUID="agentbridge@local"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/extension/$UUID"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
LINK=0

for arg in "$@"; do
    case "$arg" in
    --link) LINK=1 ;;
    -h | --help)
        echo "usage: $0 [--link]   (--link symlinks the repo for development)"
        exit 0
        ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

[ -f "$SRC/metadata.json" ] || { echo "missing $SRC/metadata.json" >&2; exit 1; }

echo "installing $UUID -> $DEST"
mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
if [ "$LINK" = 1 ]; then
    ln -s "$SRC" "$DEST"
else
    cp -r "$SRC" "$DEST"
fi

# Add to enabled-extensions without dropping the ones already there.
python3 - "$UUID" <<'PY'
import ast
import subprocess
import sys

uuid = sys.argv[1]
current = ast.literal_eval(subprocess.run(
    ["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
    capture_output=True, text=True, check=True).stdout)
if uuid in current:
    print(f"already enabled: {uuid}")
else:
    current.append(uuid)
    value = "[" + ", ".join(f"'{item}'" for item in current) + "]"
    subprocess.run(["gsettings", "set", "org.gnome.shell",
                    "enabled-extensions", value], check=True)
    print(f"enabled: {uuid}")
PY

if gdbus call --session --dest org.gnome.Shell \
    --object-path /org/gnome/AgentBridge \
    --method org.gnome.AgentBridge.Ping >/dev/null 2>&1; then
    echo "the bridge is already answering on this session"
else
    cat <<EOF

Installed. Log out and back in so GNOME Shell loads the extension
(a Wayland session cannot reload extensions live), then check:

  gdbus call --session --dest org.gnome.Shell \\
    --object-path /org/gnome/AgentBridge \\
    --method org.gnome.AgentBridge.Ping

EOF
fi
