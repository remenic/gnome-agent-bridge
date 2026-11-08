#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Test harness: a headless, private nested GNOME Shell on its own D-Bus session.
#
# Why: the running shell only scans ~/.local/share/gnome-shell/extensions at
# startup, and GNOME 51 does not implement
# org.gnome.Shell.Extensions.ReloadExtension. A headless nested shell gives a full
# compositor (real Clutter seat, real org.gnome.Mutter.ScreenCast, real
# Shell.Screenshot) that is invisible to the user and isolated from their session.
#
# Usage: nested-shell.sh start|stop|status|restart
set -euo pipefail

R="${AGTEST_RUNTIME:-/run/user/$(id -u)/agtest}"
DISPLAY_NAME="${AGTEST_DISPLAY:-wayland-agtest}"
MONITORS="${AGTEST_VIRTUAL_MONITORS:-1280x720}"
NO_X11="${AGTEST_NO_X11:-1}"
BUS_ADDR="unix:path=$R/bus"
PIDFILE="$R/shell.pid"
LOG="$R/shell.log"

sync_extension() {
    local src dest
    src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/extension/agentbridge@local"
    dest="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/agentbridge@local"
    [ -f "$src/metadata.json" ] || { echo "extension sources missing" >&2; return 1; }
    if [ -L "$dest" ] || [ ! -d "$dest" ]; then
        rm -rf "$dest"
        cp -r "$src" "$dest"
    fi
}

start_bus() {
    mkdir -p "$R"
    if DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR" \
        dbus-send --session --dest=org.freedesktop.DBus --type=method_call \
        --print-reply /org/freedesktop/DBus org.freedesktop.DBus.ListNames \
        >/dev/null 2>&1; then
        return 0
    fi
    rm -f "$R/bus" "$R/bus.pid"
    dbus-daemon --session --address="$BUS_ADDR" --fork \
        --print-address --print-pid >"$R/bus.info" 2>&1
}

start() {
    stop >/dev/null 2>&1 || true
    sync_extension
    start_bus
    export DBUS_SESSION_BUS_ADDRESS="$BUS_ADDR"
    local monitor_args=()
    local monitor
    for monitor in $MONITORS; do
        monitor_args+=(--virtual-monitor "$monitor")
    done
    local x11_args=()
    if [ "$NO_X11" = 1 ]; then
        x11_args+=(--no-x11)
    fi
    setsid nohup gnome-shell --headless --wayland \
        --wayland-display="$DISPLAY_NAME" "${monitor_args[@]}" "${x11_args[@]}" \
        >"$LOG" 2>&1 </dev/null &
    echo $! >"$PIDFILE"

    for _ in $(seq 1 30); do
        sleep 1
        if gdbus call --session --dest org.gnome.Shell \
            --object-path /org/gnome/AgentBridge \
            --method org.gnome.AgentBridge.Ping >/dev/null 2>&1; then
            echo "nested shell ready (pid $(cat "$PIDFILE"), bus $BUS_ADDR, display $DISPLAY_NAME, monitors: $MONITORS)"
            return 0
        fi
    done
    echo "nested shell did NOT come up; see $LOG" >&2
    tail -30 "$LOG" >&2
    return 1
}

stop() {
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            for _ in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
    # The dbus-daemon is started with --fork and is not in the shell's process
    # group, so it would survive and leak one daemon per stop(). Kill it too.
    if [ -f "$R/bus.info" ]; then
        local buspid
        buspid=$(sed -n '2p' "$R/bus.info" 2>/dev/null | tr -dc '0-9')
        if [ -n "$buspid" ] && kill -0 "$buspid" 2>/dev/null; then
            kill "$buspid" 2>/dev/null || true
            sleep 0.3
            kill -9 "$buspid" 2>/dev/null || true
        fi
        rm -f "$R/bus.info"
    fi
    echo stopped
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "running (pid $(cat "$PIDFILE"))"
    else
        echo "not running"
        return 1
    fi
}

case "${1:-start}" in
start) start ;;
stop) stop ;;
status) status ;;
restart) stop; start ;;
*)
    echo "usage: $0 start|stop|status|restart" >&2
    exit 2
    ;;
esac
