#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP server for the Agent Bridge GNOME Shell extension.

Stdio MCP (newline-delimited JSON-RPC 2.0) -> D-Bus -> the extension inside
gnome-shell. No third-party dependencies: PyGObject is already on any GNOME
system, and the MCP framing is implemented here directly.

Tools: silent screenshots (image content blocks), native screen recording,
pointer/keyboard injection, window control, and an AT-SPI semantic layer.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time

import warnings

import gi

warnings.filterwarnings("ignore", category=DeprecationWarning)

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

try:
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # noqa: E402
except (ValueError, ImportError):  # AT-SPI is optional
    Atspi = None


def atspi_iface(node, name):
    """Compat accessor for AT-SPI interfaces.

    at-spi2-core 2.6x flattens the interfaces onto Atspi.Accessible (is_action(),
    get_n_actions(), ...), and the old get_*() accessors return the accessible
    itself. Older bindings return a separate interface object. Return whatever we
    get and let the callers use the attribute names both shapes share.
    """
    for attr in (f"{name}_iface", name):
        getter = getattr(node, attr, None)
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:
            continue
        if value is not None:
            return value
    return None


def node_actions(node):
    """Return (action names, do_action function) for an element."""
    try:
        if hasattr(node, "is_action") and node.is_action():
            count = node.get_n_actions()
            return [node.get_action_name(i) for i in range(count)], node.do_action
    except Exception:
        pass
    iface = atspi_iface(node, "get_action")
    try:
        if iface is not None and getattr(iface, "n_actions", 0):
            return ([iface.get_name(i) for i in range(iface.n_actions)],
                    iface.do_action)
    except Exception:
        pass
    return [], None


def node_bounds(node):
    """Bounds relative to the containing window.

    On Wayland the SCREEN coordinate space is unusable: every element reports
    0,0 (the compositor does not expose global coordinates to clients). The
    toolkit's WINDOW coordinates are correct, so callers add the window origin
    they get from the compositor (see window_offset).
    """
    for coord in ("WINDOW", "SCREEN"):
        try:
            coord_type = getattr(Atspi.CoordType, coord)
            if hasattr(node, "is_component") and node.is_component():
                rect = node.get_extents(coord_type)
            else:
                iface = atspi_iface(node, "get_component")
                if iface is None:
                    return None
                rect = iface.get_extents(coord_type)
        except Exception:
            continue
        if rect is not None and rect.width > 0 and rect.height > 0:
            return {"x": rect.x, "y": rect.y,
                    "width": rect.width, "height": rect.height}
    return None


def find_frame(node):
    """The frame/window node holding this element (or the app's first window)."""
    current = node
    for _ in range(16):
        if current is None:
            break
        try:
            role = current.get_role_name()
        except Exception:
            role = ""
        if role in ("frame", "window", "dialog"):
            return current
        try:
            current = current.get_parent()
        except Exception:
            current = None

    # An application root has no parent, so look one level down for its windows.
    try:
        role = node.get_role_name()
    except Exception:
        role = ""
    if role in ("application", "desktop frame", "desktop"):
        for child in _children(node):
            try:
                child_role = child.get_role_name()
            except Exception:
                continue
            if child_role in ("frame", "window", "dialog"):
                return child
    return None


def frame_title(node):
    """Title of the window holding this element (AT-SPI frame names match window titles)."""
    frame = find_frame(node)
    if frame is None:
        return None
    try:
        return frame.get_name()
    except Exception:
        return None


def window_offset(node) -> tuple[int, int] | None:
    """Compositor origin of the window holding this element, if we can ask for it."""
    title = (frame_title(node) or "").strip()
    if not title:
        return None
    try:
        windows = call_json("ListWindows") or []
    except BridgeError:
        return None
    for window in windows:
        if (window.get("title") or "").strip() == title:
            return int(window["x"]), int(window["y"])
    log(f"no compositor window titled {title!r}; bounds stay window-relative")
    return None


def node_is_editable(node) -> bool:
    try:
        if hasattr(node, "is_editable_text"):
            return bool(node.is_editable_text())
    except Exception:
        pass
    return False


def node_text(node, limit=200):
    """Read an element's text, across old and new AT-SPI bindings."""
    try:
        if hasattr(node, "get_character_count"):
            count = node.get_character_count()
        else:
            iface = atspi_iface(node, "get_text")
            count = getattr(iface, "character_count", 0) if iface else 0
    except Exception:
        count = 0
    if not count:
        return None

    if hasattr(node, "get_text_at_offset"):
        parts, offset = [], 0
        for _ in range(64):
            try:
                chunk = node.get_text_at_offset(offset,
                                                Atspi.TextBoundaryType.LINE_END)
            except Exception:
                break
            content = getattr(chunk, "content", None)
            if not content:
                break
            parts.append(content)
            end = getattr(chunk, "end_offset", offset + len(content))
            if end >= count or end <= offset:
                break
            offset = end + 1
        if parts:
            return "\n".join(parts)[:limit]

    iface = atspi_iface(node, "get_text")
    try:
        if iface is not None:
            return iface.get_text(0, min(count, limit))
    except Exception:
        pass
    return None


def node_set_text(node, text) -> bool:
    if hasattr(node, "set_text_contents"):
        try:
            node.set_text_contents(text)
            return True
        except Exception:
            pass
    iface = atspi_iface(node, "get_editable_text")
    try:
        if iface is not None and hasattr(iface, "set_text_contents"):
            iface.set_text_contents(text)
            return True
    except Exception:
        pass
    return False


def node_grab_focus(node) -> bool:
    try:
        node.grab_focus()
        return True
    except Exception:
        pass
    iface = atspi_iface(node, "get_component")
    try:
        if iface is not None:
            iface.grab_focus()
            return True
    except Exception:
        pass
    return False

SERVER_NAME = "gnome-agent-bridge"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"

DEST = "org.gnome.Shell"
OBJECT_PATH = "/org/gnome/AgentBridge"
IFACE = "org.gnome.AgentBridge"

MAX_IMAGE_DIM = 1600
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def log(*parts):
    print("[gnome-agent-bridge]", *parts, file=sys.stderr, flush=True)


class BridgeError(Exception):
    pass


# --------------------------------------------------------------------------- #
# D-Bus plumbing
# --------------------------------------------------------------------------- #
_bus = None


def ensure_runtime_dir():
    """GLib resolves the session bus from $XDG_RUNTIME_DIR/bus when the address is
    not exported (typical for a systemd user service like paseo). Fill it in when
    the caller's environment is too sparse."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    candidate = f"/run/user/{os.getuid()}"
    if os.path.isdir(candidate):
        os.environ["XDG_RUNTIME_DIR"] = candidate


def _session_address():
    """The address a *fresh* connection should use.

    Gio.bus_get_sync() caches one connection per bus type; after a logout or a bus
    restart that cached object stays closed forever, so a retry through it cannot
    recover. new_for_address_sync() always dials the address itself.
    """
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        return address
    ensure_runtime_dir()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return "unix:path=" + os.path.join(runtime, "bus")
    return None


def _connect():
    address = _session_address()
    if address:
        try:
            return Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
        except GLib.Error:
            pass  # fall through to GLib's own resolution (autolaunch, X11)
    ensure_runtime_dir()
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        raise BridgeError(
            f"cannot connect to the session bus: {exc.message}. "
            "With DBUS_SESSION_BUS_ADDRESS unset, GLib falls back to "
            "$XDG_RUNTIME_DIR/bus, so a session started from a systemd user "
            "service, cron job or container needs XDG_RUNTIME_DIR (and the "
            "bus socket) from the graphical session") from exc


def bus():
    global _bus
    if _bus is not None and _bus.is_closed():
        _reset_bus()
    if _bus is None:
        _bus = _connect()
    return _bus


def _is_connection_closed(exc: GLib.Error) -> bool:
    """A long-lived server outlives shells and buses.

    gnome-shell restarting (or a logged-out session) can close the cached bus
    connection; Gio then reports "The connection is closed" for every later call.
    Treat that as recoverable: drop the handle and reconnect once.
    """
    message = (exc.message or "").lower()
    return ("connection is closed" in message
            or "connection terminated" in message
            or "org.freedesktop.dbus.error.disconnected" in message
            or "org.freedesktop.dbus.error.noreply" in message)


def _reset_bus():
    global _bus
    if _bus is not None:
        log("dropping the cached session bus connection (the shell or bus restarted)")
    try:
        if _bus is not None:
            _bus.close_sync(None)
    except Exception:
        pass
    _bus = None


def _explain(exc: GLib.Error) -> str:
    message = exc.message
    if "org.freedesktop.DBus.Error.ServiceUnknown" in message or \
            "org.freedesktop.DBus.Error.NameHasNoOwner" in message:
        return ("the GNOME Shell Agent Bridge extension is not loaded; install and enable "
                "agentbridge@local and log in again")
    if "org.freedesktop.DBus.Error.UnknownMethod" in message or \
            "UnknownObject" in message:
        return ("the extension is loaded but does not expose this method; "
                "the extension and this MCP server are probably different versions")
    for prefix in ("org.gnome.AgentBridge.Error: ", "org.gnome.gjs.JSError.Error: "):
        if message.startswith(prefix):
            message = message[len(prefix):]
    return message


def call(method: str, signature: str | None = None, args: list | None = None,
         timeout_ms: int = 60000):
    variant = GLib.Variant(signature, args) if signature else None
    try:
        reply = bus().call_sync(DEST, OBJECT_PATH, IFACE, method, variant, None,
                                Gio.DBusCallFlags.NONE, timeout_ms, None)
    except GLib.Error as exc:
        if not _is_connection_closed(exc):
            raise BridgeError(_explain(exc)) from exc
        # Stale handle: the shell or the bus went away since we cached it.
        _reset_bus()
        try:
            reply = bus().call_sync(DEST, OBJECT_PATH, IFACE, method, variant,
                                    None, Gio.DBusCallFlags.NONE, timeout_ms, None)
        except GLib.Error as retry_exc:
            raise BridgeError(_explain(retry_exc)) from retry_exc
    if reply is None:
        return ()
    return reply.unpack()


def call_json(method: str, signature: str | None = None, args: list | None = None):
    value = call(method, signature, args)
    if not value:
        return None
    return json.loads(value[0])


API_VERSION = "agentbridge/2"


def ensure_extension() -> str:
    try:
        return call("Ping")[0]
    except BridgeError as exc:
        raise BridgeError(str(exc)) from exc


def is_stale_extension() -> bool:
    try:
        return API_VERSION not in call("Ping")[0]
    except BridgeError:
        return False


def introspect() -> str:
    """The extension's own D-Bus introspection XML (no side effects)."""
    try:
        reply = bus().call_sync(DEST, OBJECT_PATH, "org.freedesktop.DBus.Introspectable",
                                "Introspect", None, None, Gio.DBusCallFlags.NONE,
                                5000, None)
        return reply.unpack()[0]
    except GLib.Error as exc:
        if _is_connection_closed(exc):
            _reset_bus()
            try:
                reply = bus().call_sync(DEST, OBJECT_PATH,
                                        "org.freedesktop.DBus.Introspectable",
                                        "Introspect", None, None,
                                        Gio.DBusCallFlags.NONE, 5000, None)
                return reply.unpack()[0]
            except GLib.Error:
                return ""
        return ""


def capabilities() -> dict:
    """Which optional features this loaded extension actually has.

    GNOME Shell only loads extension changes at login, so a session can run an
    older build than this server. Ask the interface instead of trusting a version
    number: methods are additive, and the server degrades where it can.
    """
    xml = introspect()
    type_text = re.search(r'<method name="TypeText">(.*?)</method>', xml, re.S)
    type_text_inputs = len(re.findall(r'direction="in"', type_text.group(1))) if type_text else 0
    return {
        "screenshot_area": 'name="ScreenshotArea"' in xml,
        "type_text_unicode": type_text_inputs >= 3,
        "record_region": 'name="StartRecordingArea"' in xml,
        "start_recording": 'name="StartRecording"' in xml,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    path = os.path.join(base, "gnome-agent-bridge")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def videos_dir() -> str:
    try:
        out = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
        if out and os.path.isdir(out):
            return out
    except Exception:
        pass
    return os.path.expanduser("~")


def text_result(payload) -> dict:
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}]}


def image_result(pixbuf, meta: dict, fmt: str, quality: int, save_path: str | None) -> dict:
    if fmt == "jpeg":
        options, values = ["quality"], [str(int(quality))]
    else:
        options, values = [], []

    ok, data = pixbuf.save_to_bufferv(fmt, options, values)
    if not ok:
        raise BridgeError(f"could not encode the screenshot as {fmt}")

    if save_path:
        with open(save_path, "wb") as handle:
            handle.write(bytes(data))

    meta = dict(meta)
    meta.update({
        "image": {"width": pixbuf.get_width(), "height": pixbuf.get_height()},
        "format": fmt,
        "bytes": len(data),
        "saved_to": save_path,
    })

    return {
        "content": [
            {
                "type": "image",
                "data": base64.b64encode(bytes(data)).decode("ascii"),
                "mimeType": f"image/{fmt}",
            },
            {"type": "text", "text": json.dumps(meta, ensure_ascii=False)},
        ],
    }


MODIFIERS = {
    "ctrl": 0xFFE3, "control": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9,
    "mod1": 0xFFE9, "super": 0xFFEB, "meta": 0xFFEB, "win": 0xFFEB, "mod4": 0xFFEB,
}

SHIFT_SYMBOLS = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[",
    "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
}
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    SHIFT_SYMBOLS[_ch] = _ch.lower()

NAMED_KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B,
    "esc": 0xFF1B, "backspace": 0xFF08, "delete": 0xFFFF, "insert": 0xFF63,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "page_up": 0xFF55,
    "pagedown": 0xFF56, "page_down": 0xFF56, "left": 0xFF51, "up": 0xFF52,
    "right": 0xFF53, "down": 0xFF54, "space": 0x20, "menu": 0xFF67,
    "capslock": 0xFFE5, "print": 0xFF61, "pause": 0xFF13, "plus": 0x2B,
}
NAMED_KEYSYMS.update({f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)})


def keysym_for_char(ch: str) -> tuple[int, bool]:
    """Return (keysym, needs_shift) for one character (US layout assumption)."""
    if ch in SHIFT_SYMBOLS:
        return ord(SHIFT_SYMBOLS[ch]), True
    codepoint = ord(ch)
    if 0x20 <= codepoint <= 0xFF:
        return codepoint, False
    return 0x01000000 + codepoint, False


def parse_key_spec(spec: str) -> list[int]:
    """Turn "ctrl+alt+t", "Return" or "A" into a list of keysyms to press in order."""
    spec = spec.strip()
    if not spec:
        raise BridgeError("empty key specification")

    parts = [part.strip() for part in spec.split("+") if part.strip()]
    if not parts:
        parts = ["+"]

    keysyms: list[int] = []
    explicit_shift = False
    for part in parts[:-1]:
        key = part.lower()
        if key not in MODIFIERS:
            raise BridgeError(f"unknown modifier {part!r} in {spec!r}")
        if MODIFIERS[key] == 0xFFE1:
            explicit_shift = True
        keysyms.append(MODIFIERS[key])

    last = parts[-1]
    lowered = last.lower()
    if lowered in NAMED_KEYSYMS:
        keysyms.append(NAMED_KEYSYMS[lowered])
    elif lowered in MODIFIERS:
        keysyms.append(MODIFIERS[lowered])
    elif len(last) == 1:
        keysym, needs_shift = keysym_for_char(last)
        if needs_shift and not explicit_shift:
            keysyms.append(0xFFE1)
        keysyms.append(keysym)
    else:
        raise BridgeError(f"unknown key {last!r} in {spec!r}")
    return keysyms


# --------------------------------------------------------------------------- #
# AT-SPI semantic layer
# --------------------------------------------------------------------------- #
_state_names = None


def _state_name(value) -> str:
    global _state_names
    if _state_names is None:
        _state_names = {}
        for name in dir(Atspi.StateType):
            if name.isupper() or name[0].isupper():
                attr = getattr(Atspi.StateType, name)
                if isinstance(attr, Atspi.StateType):
                    _state_names[attr] = name.lower()
    return _state_names.get(value, str(value))


def _desktop():
    if Atspi is None:
        raise BridgeError("AT-SPI is not available (install python3-atspi / rebuild PyGObject)")
    return Atspi.get_desktop(0)


def _children(node) -> list:
    try:
        return [node.get_child_at_index(i) for i in range(node.get_child_count())]
    except Exception:
        return []


def _apps(desktop) -> list[tuple[str, object]]:
    out = []
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
        except Exception:
            continue
        if app is None:
            continue
        try:
            name = app.get_name() or ""
        except Exception:
            name = ""
        out.append((f"{i}", app, name))
    return out


def _match_app(desktop, wanted: str | None):
    apps = _apps(desktop)
    if not wanted:
        return None
    needle = wanted.strip().lower()
    exact = [a for a in apps if a[2].lower() == needle]
    if exact:
        return exact[0]
    partial = [a for a in apps if needle in a[2].lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(f"{a[2]} ({a[0]})" for a in partial)
        raise BridgeError(f"application name {wanted!r} is ambiguous: {names}")
    raise BridgeError(f"no running application matches {wanted!r}")


def _resolve(desktop, path: str, app: str | None = None):
    """Resolve a slash-separated index path ("0/2/1") to an AT-SPI node."""
    if path is None:
        raise BridgeError("path is required")
    indices = [int(p) for p in str(path).split("/") if p != ""]
    if not indices:
        raise BridgeError("invalid element path")

    if app:
        app_match = _match_app(desktop, app)
        node = app_match[1]
        indices = indices[1:] if indices and indices[0] == int(app_match[0]) else indices
    else:
        node = desktop.get_child_at_index(indices[0])
        indices = indices[1:]

    for index in indices:
        try:
            node = node.get_child_at_index(index)
        except Exception as exc:
            raise BridgeError(f"element path {path} no longer exists ({exc})") from exc
        if node is None:
            raise BridgeError(f"element path {path} no longer exists")
    return node


def _describe(node, path: str, depth_limit: int = 0) -> dict:
    info: dict = {"path": path}
    try:
        info["role"] = node.get_role_name()
    except Exception:
        pass
    try:
        name = node.get_name()
        if name:
            info["name"] = name
    except Exception:
        pass
    try:
        states = node.get_state_set()
        interesting = []
        for state_name in ("editable", "focused", "sensitive", "checked",
                           "expandable", "expanded", "selected", "showing"):
            value = getattr(Atspi.StateType, state_name.upper(), None)
            if value is not None and states.contains(value):
                interesting.append(state_name)
        if interesting:
            info["states"] = interesting
    except Exception:
        pass
    bounds = node_bounds(node)
    if bounds:
        info["bounds"] = bounds
        info["bounds_space"] = "window"

    actions, _ = node_actions(node)
    if actions:
        info["actions"] = actions

    if node_is_editable(node):
        info["editable"] = True

    content = node_text(node, limit=120)
    if content:
        info["text"] = content
    return info


def accessibility_hint() -> str | None:
    """Why an empty ui_tree is empty: no a11y bridge, no registry, or no apps."""
    problems = []

    try:
        settings = Gio.Settings.new("org.gnome.desktop.interface")
        if not settings.get_boolean("toolkit-accessibility"):
            problems.append(
                "org.gnome.desktop.interface toolkit-accessibility is false, so GTK "
                "applications do not publish accessibility trees; turn it on (or start "
                "the app with GTK_A11Y=atspi) and restart that application")
    except Exception:
        pass

    try:
        reply = bus().call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus",
                                "GetAddress", None, None, Gio.DBusCallFlags.NONE,
                                3000, None)
        address = reply.unpack()[0]
        a11y = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        owner = a11y.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", ["org.a11y.atspi.Registry"]),
            None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        if not owner:
            problems.append("no org.a11y.atspi.Registry is running on the "
                            "accessibility bus (at-spi2-core / at-spi-dbus-bus)")
    except Exception:
        problems.append("the accessibility bus (org.a11y.Bus) is not reachable")

    return "; ".join(problems) if problems else None


def build_tree(app: str | None, interactive_only: bool, max_nodes: int) -> dict:
    desktop = _desktop()
    if app:
        app_index, app_node, app_name = _match_app(desktop, app)
        roots = [(app_index, app_node, app_name)]
    else:
        roots = [(index, node, name) for index, node, name in _apps(desktop)]

    nodes: list[dict] = []
    deadline = time.time() + 8.0
    truncated = False

    def visit(node, path: str, depth: int):
        nonlocal truncated
        if len(nodes) >= max_nodes or time.time() > deadline:
            truncated = True
            return
        info = _describe(node, path)
        keep = (not interactive_only
                or bool(info.get("actions"))
                or bool(info.get("editable"))
                or info.get("role") in ("frame", "dialog", "window", "application"))
        if keep:
            nodes.append(info)
        for index, child in enumerate(_children(node)):
            visit(child, f"{path}/{index}", depth + 1)

    app_root = None
    for index, node, name in roots:
        if app:
            app_root = node
            visit(node, f"{index}", 0)
        else:
            if len(nodes) >= max_nodes or time.time() > deadline:
                truncated = True
                break
            root = _describe(node, f"{index}")
            root["role"] = root.get("role") or "application"
            root["name"] = root.get("name") or name
            nodes.append(root)

    # Translate window-relative bounds into stage coordinates when we can ask the
    # compositor where the window is; otherwise leave them window-relative.
    origin = window_offset(app_root) if app_root is not None else None
    if origin is not None:
        dx, dy = origin
        for info in nodes:
            bounds = info.get("bounds")
            if bounds:
                bounds["x"] += dx
                bounds["y"] += dy
                info["bounds_space"] = "stage"

    result = {
        "applications": [name for _, _, name in roots] if not app else [roots[0][2]],
        "node_count": len(nodes),
        "truncated": truncated,
        "origin": {"x": origin[0], "y": origin[1]} if origin else None,
        "nodes": nodes,
    }
    if not nodes:
        hint = accessibility_hint()
        if hint:
            result["hint"] = hint
    return result


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def stage_geometry() -> dict:
    """Union of the monitor rectangles, in logical stage coordinates."""
    state = call_json("GetState") or {}
    monitors = state.get("monitors") or []
    if not monitors:
        raise BridgeError("the extension reported no monitors")
    left = min(m["x"] for m in monitors)
    top = min(m["y"] for m in monitors)
    right = max(m["x"] + m["width"] for m in monitors)
    bottom = max(m["y"] + m["height"] for m in monitors)
    return {
        "x": left, "y": top, "width": right - left, "height": bottom - top,
        "monitors": monitors,
    }


def validate_rect(rect: dict, stage: dict, what: str = "region") -> dict:
    """Validate/clamp a rectangle given in logical stage coordinates."""
    try:
        region = {key: int(rect[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError(f"{what} needs integer x, y, width and height") from exc
    if region["width"] <= 0 or region["height"] <= 0:
        raise BridgeError(f"{what} width and height must be positive")

    x1 = max(region["x"], stage["x"])
    y1 = max(region["y"], stage["y"])
    x2 = min(region["x"] + region["width"], stage["x"] + stage["width"])
    y2 = min(region["y"] + region["height"], stage["y"] + stage["height"])
    if x2 <= x1 or y2 <= y1:
        raise BridgeError(
            f"{what} {region} lies outside the screen "
            f"({stage['width']}x{stage['height']} at {stage['x']},{stage['y']}); "
            "coordinates are logical stage pixels (see list_monitors)")

    clamped = {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}
    if clamped != region:
        clamped["clamped_from"] = region
    return clamped


def resolve_region(args: dict, stage: dict) -> dict | None:
    """monitor/crop -> a rectangle in logical stage coordinates (None = whole screen)."""
    monitor = args.get("monitor")
    if monitor is not None:
        match = next((m for m in stage["monitors"] if m["index"] == int(monitor)), None)
        if match is None:
            raise BridgeError(
                f"no monitor {monitor}; this session has {len(stage['monitors'])} "
                f"monitor(s), indexes 0..{len(stage['monitors']) - 1}")
        return {"x": match["x"], "y": match["y"],
                "width": match["width"], "height": match["height"]}

    crop = args.get("crop")
    if crop is None:
        return None
    return validate_rect(crop, stage, "crop")


def tool_screenshot(args: dict) -> dict:
    include_cursor = bool(args.get("include_cursor", False))
    fmt = (args.get("format") or "png").lower()
    if fmt in ("jpg", "jpeg"):
        fmt = "jpeg"
    if fmt not in ("png", "jpeg"):
        raise BridgeError("format must be png or jpeg")
    quality = int(args.get("quality") or 80)
    scale = float(args.get("scale") or 1.0)

    stage = stage_geometry()
    region = resolve_region(args, stage)
    region = region or {key: stage[key] for key in ("x", "y", "width", "height")}

    path = os.path.join(runtime_dir(), f"shot-{int(time.time() * 1000)}.png")
    try:
        call("ScreenshotArea", "(iiiis)",
             [region["x"], region["y"], region["width"], region["height"], path])
        how = "area"
    except BridgeError as exc:
        if "UnknownMethod" not in str(exc) and "does not expose" not in str(exc):
            raise
        # Older extension: shoot the whole screen and crop here. The compositor
        # scales the stage, so convert with the measured device-pixel ratio.
        call("Screenshot", "(bs)", [include_cursor, path])
        whole = GdkPixbuf.Pixbuf.new_from_file(path)
        factor = whole.get_width() / stage["width"]
        px_x = int((region["x"] - stage["x"]) * factor)
        px_y = int((region["y"] - stage["y"]) * factor)
        px_w = max(1, int(region["width"] * factor))
        px_h = max(1, int(region["height"] * factor))
        whole = whole.new_subpixbuf(px_x, px_y, px_w, px_h)
        ok, data = whole.save_to_bufferv("png", [], [])
        with open(path, "wb") as handle:
            handle.write(bytes(data))
        how = "full+crop (extension lacks ScreenshotArea)"

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
    device_scale = pixbuf.get_width() / max(1, region["width"])

    if scale != 1.0 and scale > 0:
        width = max(1, int(pixbuf.get_width() * scale))
        height = max(1, int(pixbuf.get_height() * scale))
        pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)

    if pixbuf.get_width() > MAX_IMAGE_DIM or pixbuf.get_height() > MAX_IMAGE_DIM:
        factor = MAX_IMAGE_DIM / max(pixbuf.get_width(), pixbuf.get_height())
        width = max(1, int(pixbuf.get_width() * factor))
        height = max(1, int(pixbuf.get_height() * factor))
        pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)

    meta = {
        "source": path,
        "method": how,
        "coordinate_space": "stage-logical",
        "region": {key: region[key] for key in ("x", "y", "width", "height")},
        "stage": {key: stage[key] for key in ("x", "y", "width", "height")},
        "monitors": stage["monitors"],
        "device_scale": round(device_scale, 4),
        # stage pixels per image pixel: click_x = region.x + image_x * this
        "stage_per_image_px": round(region["width"] / pixbuf.get_width(), 6),
        "include_cursor": include_cursor,
    }
    for key in ("clamped_from",):
        if key in region:
            meta[key] = region[key]

    try:
        os.unlink(path)
    except OSError:
        pass

    return image_result(pixbuf, meta, fmt, quality, args.get("save_path"))


def tool_record_start(args: dict) -> dict:
    template = args.get("path")
    if template:
        if template.endswith(".webm") or template.endswith(".mp4"):
            template = template.rsplit(".", 1)[0]
    else:
        template = os.path.join(
            videos_dir(), time.strftime("agent-recording-%Y%m%d-%H%M%S"))

    draw_cursor = bool(args.get("draw_cursor", True))
    framerate = int(args.get("framerate") or 30)

    region = args.get("region")
    if region:
        rect = validate_rect(region, stage_geometry(), "region")
        started = call("StartRecordingArea", "(iiiisbi)",
                       [rect["x"], rect["y"], rect["width"], rect["height"],
                        template, draw_cursor, framerate], 120000)
    else:
        rect = None
        started = call("StartRecording", "(sbi)", [template, draw_cursor, framerate],
                       120000)

    return text_result({
        "recording": True,
        "path": started[0],
        "region": rect,
        "draw_cursor": draw_cursor,
        "framerate": framerate,
    })


def tool_record_stop(args: dict) -> dict:
    stopped = call("StopRecording", None, None, 120000)
    path = stopped[0] if stopped else None
    result: dict = {"recording": False, "path": path}
    if path and os.path.exists(path):
        result["bytes"] = os.path.getsize(path)
    return text_result(result)


def tool_record_status(args: dict) -> dict:
    return text_result(call_json("GetRecordingState") or {})


def tool_pointer_move(args: dict) -> dict:
    call("MovePointer", "(dd)", [float(args["x"]), float(args["y"])])
    return text_result({"moved_to": {"x": args["x"], "y": args["y"]}})


_BUTTONS = {"left": 1, "middle": 2, "right": 3, "1": 1, "2": 2, "3": 3}


def _button(args: dict) -> int:
    raw = args.get("button", "left")
    if isinstance(raw, int):
        return raw
    key = str(raw).lower()
    if key not in _BUTTONS:
        raise BridgeError(f"unknown button {raw!r} (use left, middle, right)")
    return _BUTTONS[key]


def tool_click(args: dict) -> dict:
    button = _button(args)
    count = int(args.get("count") or 1)
    if "x" in args and "y" in args:
        call("Click", "(ddii)", [float(args["x"]), float(args["y"]), button, count])
        where = {"x": args["x"], "y": args["y"]}
    else:
        pointer = call_json("GetPointer") or {}
        if "x" not in pointer:
            raise BridgeError("could not read the pointer position")
        call("Click", "(ddii)", [pointer["x"], pointer["y"], button, count])
        where = pointer
    return text_result({"clicked": where, "button": button, "count": count})


def tool_pointer_button(args: dict) -> dict:
    pressed = bool(args.get("pressed", True))
    call("PointerButton", "(ib)", [_button(args), pressed])
    return text_result({"button": _button(args), "pressed": pressed})


def tool_drag(args: dict) -> dict:
    call("Drag", "(ddddii)", [
        float(args["from_x"]), float(args["from_y"]),
        float(args["to_x"]), float(args["to_y"]),
        _button(args), int(args.get("steps") or 12),
    ])
    return text_result({
        "dragged": {
            "from": {"x": args["from_x"], "y": args["from_y"]},
            "to": {"x": args["to_x"], "y": args["to_y"]},
        },
        "button": _button(args),
    })


def tool_scroll(args: dict) -> dict:
    x = float(args.get("x", -1))
    y = float(args.get("y", -1))
    call("Scroll", "(dddd)", [x, y, float(args.get("dx") or 0), float(args.get("dy") or 0)])
    return text_result({"scrolled": {"dx": args.get("dx", 0), "dy": args.get("dy", 0)}})


def tool_type_text(args: dict) -> dict:
    text = args.get("text", "")
    if not text:
        return text_result({"typed": 0, "method": "none"})

    method = (args.get("method") or "auto").lower()
    if method not in ("auto", "keysyms", "clipboard"):
        raise BridgeError("method must be one of auto, keysyms, clipboard")
    restore_delay = int(args.get("restore_delay_ms") or 400)

    try:
        reply = call("TypeText", "(ssu)", [text, method, restore_delay], 180000)
    except BridgeError as exc:
        if is_stale_extension():
            raise BridgeError(
                "the extension loaded in this session is older than this MCP server "
                "(the TypeText signature changed); log out and back in so GNOME Shell "
                f"loads the new extension. Original error: {exc}") from exc
        raise
    info = json.loads(reply[0]) if reply and reply[0] else {}
    info["text"] = text
    return text_result(info)


def tool_press_key(args: dict) -> dict:
    spec = args.get("key") or args.get("keys")
    if not spec:
        raise BridgeError("key is required, e.g. \"ctrl+alt+t\" or \"Return\"")
    keysyms = parse_key_spec(str(spec))
    if args.get("hold_ms"):
        for keysym in keysyms:
            call("Key", "(ub)", [keysym, True])
        time.sleep(float(args["hold_ms"]) / 1000.0)
        for keysym in reversed(keysyms):
            call("Key", "(ub)", [keysym, False])
    else:
        call("KeyChord", "(au)", [keysyms])
    return text_result({"key": spec, "keysyms": keysyms})


def tool_list_windows(args: dict) -> dict:
    windows = call_json("ListWindows") or []
    return text_result({"count": len(windows), "windows": windows})


def tool_focus_window(args: dict) -> dict:
    windows = call_json("ListWindows") or []
    target = None
    if "id" in args:
        target = next((w for w in windows if w["id"] == int(args["id"])), None)
    else:
        wanted = (args.get("title") or args.get("app") or "").strip().lower()
        if not wanted:
            raise BridgeError("give an id, a title or an app")
        matches = [w for w in windows
                   if wanted in (w.get("title") or "").lower()
                   or wanted in (w.get("wm_class") or "").lower()]
        if not matches:
            raise BridgeError(f"no window matches {args.get('title') or args.get('app')!r}")
        focused = [w for w in matches if w.get("focused")]
        target = focused[0] if focused else matches[0]

    if target is None:
        raise BridgeError(f"no window with id {args['id']}")
    call("ActivateWindow", "(t)", [target["id"]])

    # Confirm with the compositor which window really has focus. Selecting by
    # title/wm_class is ambiguous when one application holds several windows or
    # tabs, and typing into the wrong document is unrecoverable from the outside.
    time.sleep(0.15)
    actual = next((w for w in (call_json("ListWindows") or []) if w.get("focused")), None)
    result = {"focused": target,
              "focused_now": actual,
              "verified": bool(actual and actual["id"] == target["id"])}
    if not result["verified"]:
        result["note"] = ("another window took focus; check focused_now before "
                          "typing or clicking")
    return text_result(result)


def tool_move_window(args: dict) -> dict:
    window_id = int(args["id"])
    wanted = {key: int(args[key]) for key in ("x", "y", "width", "height")}
    call("MoveWindow", "(tiiii)", [window_id, wanted["x"], wanted["y"],
                                   wanted["width"], wanted["height"]])

    # Ask the compositor what actually happened instead of echoing the request:
    # Wayland clients position themselves, so a move can be declined silently.
    time.sleep(0.15)
    windows = call_json("ListWindows") or []
    actual = next((w for w in windows if w["id"] == window_id), None)
    if actual is None:
        raise BridgeError(f"window {window_id} disappeared during the move")

    geometry = {key: actual[key] for key in ("x", "y", "width", "height")}
    verified = all(abs(geometry[key] - wanted[key]) <= 2 for key in wanted)
    result = {"id": window_id, "requested": wanted, "geometry": geometry,
              "verified": verified}
    if not verified:
        result["note"] = ("the window did not move to the requested geometry; "
                          "Wayland clients position themselves, so only X11 "
                          "windows can be moved by the compositor "
                          "(use focus_window and the app's own controls instead)")
    return text_result(result)


def tool_list_monitors(args: dict) -> dict:
    state = call_json("GetState") or {}
    return text_result({"monitors": state.get("monitors", []),
                        "pointer": state.get("pointer")})


def tool_ui_tree(args: dict) -> dict:
    tree = build_tree(args.get("app"), bool(args.get("interactive_only", True)),
                      int(args.get("max_nodes") or 400))
    return text_result(tree)


def tool_click_element(args: dict) -> dict:
    desktop = _desktop()
    node = _resolve(desktop, args.get("path"), args.get("app"))
    info = _describe(node, str(args.get("path")))

    names, do_action = node_actions(node)
    used = None
    preferred = ("click", "press", "toggle")
    for index, name in enumerate(names):
        if (name or "").lower() in preferred:
            do_action(index)
            used = f"action:{(name or '').lower()}"
            break

    if used is None:
        bounds = node_bounds(node)
        if not bounds:
            raise BridgeError("element has no click action and no bounds to click")
        origin = window_offset(node) or (0, 0)
        x = origin[0] + bounds["x"] + bounds["width"] / 2
        y = origin[1] + bounds["y"] + bounds["height"] / 2
        node_grab_focus(node)
        call("Click", "(ddii)", [float(x), float(y), _button(args),
                                 int(args.get("count") or 1)])
        used = "pointer"

    return text_result({"element": info, "actions": names, "clicked_via": used})


def tool_set_value(args: dict) -> dict:
    desktop = _desktop()
    node = _resolve(desktop, args.get("path"), args.get("app"))
    text = args.get("text", "")
    if not node_is_editable(node):
        raise BridgeError("element is not an editable text field")
    if not node_set_text(node, text):
        raise BridgeError("the element refused the text (no EditableText support)")
    time.sleep(0.15)
    readback = node_text(node, limit=400)
    return text_result({"set": text, "readback": readback,
                        "verified": readback == text if readback is not None else None})


def tool_focus_app(args: dict) -> dict:
    desktop = _desktop()
    app_index, app_node, app_name = _match_app(desktop, args.get("app"))
    windows = call_json("ListWindows") or []
    matches = [w for w in windows
               if (w.get("wm_class") or "").lower() in app_name.lower()
               or app_name.lower() in (w.get("wm_class") or "").lower()]
    if not matches:
        raise BridgeError(f"no window found for {app_name!r}")
    call("ActivateWindow", "(t)", [matches[0]["id"]])
    return text_result({"app": app_name, "window": matches[0]})


def tool_state(args: dict) -> dict:
    version = ensure_extension()
    caps = capabilities()
    missing = [name for name, present in caps.items() if not present]
    result = {
        "bridge": version,
        "expected_api": API_VERSION,
        "api_current": API_VERSION in version,
        "capabilities": caps,
        "state": call_json("GetState") or {},
    }
    if missing:
        result["note"] = (
            f"this session runs an older extension build (missing: {', '.join(missing)}); "
            "log out and back in to get it. screenshot() falls back to full+crop, "
            "which is only exact at scale 1")
    return text_result(result)


def tool_set_automation(args: dict) -> dict:
    enabled = bool(args.get("enabled", True))
    result = call("SetEnabled", "(b)", [enabled])
    return text_result({"enabled": result[0] if result else enabled})


def tool_wait(args: dict) -> dict:
    milliseconds = int(args.get("ms") or 500)
    time.sleep(max(0, min(milliseconds, 60000)) / 1000.0)
    return text_result({"waited_ms": milliseconds})


# --------------------------------------------------------------------------- #
# Tool registry
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "name": "screenshot",
        "description": (
            "Capture the screen (or one monitor, or a region) and return it as an image. "
            "Silent: no confirmation dialog. Coordinates in the image map to the same "
            "coordinates used by click/move tools, unless you pass scale/crop - the "
            "returned metadata tells you the image size, the crop origin and the "
            "downscale factor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "monitor": {"type": "integer", "description": "Monitor index (see list_monitors)"},
                "crop": {
                    "type": "object",
                    "description": "Capture only this rectangle (stage coordinates)",
                    "properties": {
                        "x": {"type": "integer"}, "y": {"type": "integer"},
                        "width": {"type": "integer"}, "height": {"type": "integer"},
                    },
                    "required": ["x", "y", "width", "height"],
                },
                "scale": {"type": "number", "description": "Scale factor for the returned image (default 1)"},
                "format": {"type": "string", "enum": ["png", "jpeg"],
                           "description": "Image format (default png)"},
                "quality": {"type": "integer", "description": "JPEG quality 1-100 (default 80)"},
                "include_cursor": {"type": "boolean", "description": "Draw the pointer into the image"},
                "save_path": {"type": "string", "description": "Also write the image to this path"},
            },
        },
        "handler": tool_screenshot,
    },
    {
        "name": "record_start",
        "description": (
            "Start a screen recording with GNOME's native recorder (GStreamer + PipeWire, "
            "hardware encoders when available). Returns the file that is being written. "
            "The recording keeps running if this MCP server exits; use record_stop or the "
            "indicator menu to end it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "File template without extension (default ~/Videos/agent-recording-<timestamp>)"},
                "region": {
                    "type": "object",
                    "description": "Record only this rectangle (default: whole screen)",
                    "properties": {
                        "x": {"type": "integer"}, "y": {"type": "integer"},
                        "width": {"type": "integer"}, "height": {"type": "integer"},
                    },
                    "required": ["x", "y", "width", "height"],
                },
                "draw_cursor": {"type": "boolean", "description": "Include the cursor (default true)"},
                "framerate": {"type": "integer", "description": "Frames per second (default 30)"},
            },
        },
        "handler": tool_record_start,
    },
    {
        "name": "record_stop",
        "description": "Stop the screen recording and report the finished file, its size and duration.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_record_stop,
    },
    {
        "name": "record_status",
        "description": "Report whether a recording is running, where it writes and how long it has run.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_record_status,
    },
    {
        "name": "pointer_move",
        "description": "Move the pointer to absolute screen coordinates (stage pixels).",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"],
        },
        "handler": tool_pointer_move,
    },
    {
        "name": "click",
        "description": (
            "Click at coordinates (or at the current pointer position when x/y are "
            "omitted). Use count=2 for double click."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"}, "y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "middle", "right"]},
                "count": {"type": "integer", "description": "1-5 (default 1)"},
            },
        },
        "handler": tool_click,
    },
    {
        "name": "pointer_button",
        "description": "Press or release a mouse button without moving (for drag with separate steps).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "button": {"type": "string", "enum": ["left", "middle", "right"]},
                "pressed": {"type": "boolean", "description": "true = press, false = release"},
            },
            "required": ["pressed"],
        },
        "handler": tool_pointer_button,
    },
    {
        "name": "drag",
        "description": "Drag from one point to another with a smooth pointer path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_x": {"type": "number"}, "from_y": {"type": "number"},
                "to_x": {"type": "number"}, "to_y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "middle", "right"]},
                "steps": {"type": "integer", "description": "Intermediate moves (default 12)"},
            },
            "required": ["from_x", "from_y", "to_x", "to_y"],
        },
        "handler": tool_drag,
    },
    {
        "name": "scroll",
        "description": (
            "Scroll with the wheel. Positive dy scrolls down, negative dy scrolls up. "
            "Omit x/y to scroll at the current pointer position."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"}, "y": {"type": "number"},
                "dx": {"type": "number"}, "dy": {"type": "number"},
            },
        },
        "handler": tool_scroll,
    },
    {
        "name": "type_text",
        "description": (
            "Type literal text into the focused window (focus first with focus_window or "
            "click). ASCII is typed as keysyms, which is layout independent. Text with "
            "non-ASCII characters is put on the clipboard and pasted with Ctrl+V, because "
            "a keysym only works if the active keymap maps that character; the previous "
            "clipboard text is restored afterwards (cleared if it could not be read). "
            "Use method='keysyms' to force keystrokes (lossy for unmappable characters) "
            "or method='clipboard' to force a paste."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "method": {
                    "type": "string",
                    "enum": ["auto", "keysyms", "clipboard"],
                    "description": "auto (default): keysyms for ASCII, clipboard otherwise",
                },
                "restore_delay_ms": {
                    "type": "integer",
                    "description": "How long to wait before restoring the clipboard (default 400)",
                },
            },
            "required": ["text"],
        },
        "handler": tool_type_text,
    },
    {
        "name": "press_key",
        "description": (
            "Press a key or a chord, e.g. 'Return', 'Tab', 'Escape', 'F5', 'ctrl+alt+t', "
            "'ctrl+shift+v', 'super'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "hold_ms": {"type": "integer",
                            "description": "Hold the chord this long instead of a short tap"},
            },
            "required": ["key"],
        },
        "handler": tool_press_key,
    },
    {
        "name": "list_windows",
        "description": "List compositor windows with id, title, wm_class, pid, geometry, workspace and focus state.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_list_windows,
    },
    {
        "name": "focus_window",
        "description": "Focus and raise a window by id, or by a substring of its title or wm_class.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "app": {"type": "string"},
            },
        },
        "handler": tool_focus_window,
    },
    {
        "name": "move_window",
        "description": "Move and/or resize a window by id (stage coordinates).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "width": {"type": "integer"}, "height": {"type": "integer"},
            },
            "required": ["id", "x", "y", "width", "height"],
        },
        "handler": tool_move_window,
    },
    {
        "name": "list_monitors",
        "description": "List monitors with geometry and scale, plus the current pointer position.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_list_monitors,
    },
    {
        "name": "ui_tree",
        "description": (
            "Read the AT-SPI accessibility tree, optionally for one application. Returns "
            "elements with a stable 'path', role, name, states, screen bounds and actions. "
            "Prefer this over guessing pixels: click_element can activate an element "
            "without coordinates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Application name (substring)"},
                "interactive_only": {"type": "boolean",
                                     "description": "Only actionable/editable elements (default true)"},
                "max_nodes": {"type": "integer", "description": "Node budget (default 400)"},
            },
        },
        "handler": tool_ui_tree,
    },
    {
        "name": "click_element",
        "description": (
            "Activate one accessibility element by path (from ui_tree). Uses the element's "
            "AT-SPI action when it has one, otherwise a pointer click at its centre."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "app": {"type": "string"},
            },
            "required": ["path"],
        },
        "handler": tool_click_element,
    },
    {
        "name": "set_value",
        "description": "Write text straight into an editable accessibility element (no keystrokes).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "app": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["path", "text"],
        },
        "handler": tool_set_value,
    },
    {
        "name": "focus_app",
        "description": "Focus the window of a running application by name (substring).",
        "inputSchema": {
            "type": "object",
            "properties": {"app": {"type": "string"}},
            "required": ["app"],
        },
        "handler": tool_focus_app,
    },
    {
        "name": "bridge_status",
        "description": "Report the extension version, whether automation is enabled, monitors, pointer, recording state.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_state,
    },
    {
        "name": "set_automation",
        "description": (
            "Kill switch: enable or disable all input injection. Read-only tools (screenshot, "
            "ui_tree, list_windows) keep working while input is off."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
        "handler": tool_set_automation,
    },
    {
        "name": "wait",
        "description": "Sleep for a number of milliseconds (lets the UI settle between steps).",
        "inputSchema": {
            "type": "object",
            "properties": {"ms": {"type": "integer"}},
        },
        "handler": tool_wait,
    },
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# --------------------------------------------------------------------------- #
# MCP protocol
# --------------------------------------------------------------------------- #
def send(payload: dict):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(message_id, result):
    send({"jsonrpc": "2.0", "id": message_id, "result": result})


def error(message_id, code, message_text):
    send({"jsonrpc": "2.0", "id": message_id,
          "error": {"code": code, "message": message_text}})


def handle_tools_call(message_id, params):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        error(message_id, -32602, f"unknown tool: {name}")
        return
    try:
        result(message_id, tool["handler"](arguments))
    except BridgeError as exc:
        result(message_id, {"content": [{"type": "text", "text": f"error: {exc}"}],
                            "isError": True})
    except KeyError as exc:
        result(message_id, {"content": [{"type": "text",
                                         "text": f"missing argument: {exc}"}],
                            "isError": True})
    except Exception as exc:  # noqa: BLE001 - never kill the server on one bad call
        log("tool failed:", repr(exc))
        result(message_id, {"content": [{"type": "text", "text": f"error: {exc}"}],
                            "isError": True})


def handle(message: dict):
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        wanted = params.get("protocolVersion") or PROTOCOL_VERSION
        result(message_id, {
            "protocolVersion": wanted,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "GNOME desktop control through the Agent Bridge shell extension. "
                "Workflow: bridge_status -> screenshot or ui_tree -> act -> screenshot "
                "to verify. Coordinates are stage pixels; monitor offsets come from "
                "list_monitors. Recording uses GNOME's native recorder."
            ),
        })
    elif method == "notifications/initialized":
        return
    elif method == "tools/list":
        result(message_id, {"tools": [
            {"name": tool["name"], "description": tool["description"],
             "inputSchema": tool["inputSchema"]}
            for tool in TOOLS
        ]})
    elif method == "tools/call":
        handle_tools_call(message_id, params)
    elif method == "ping":
        result(message_id, {})
    elif method in ("resources/list", "prompts/list"):
        result(message_id,
               {"resources": []} if method == "resources/list" else {"prompts": []})
    elif method == "logging/setLevel":
        result(message_id, {})
    elif method == "shutdown":
        result(message_id, {})
        raise SystemExit(0)
    elif message_id is not None:
        error(message_id, -32601, f"method not found: {method}")


def main():
    log(f"starting {SERVER_NAME} {SERVER_VERSION} (pid {os.getpid()})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            error(None, -32700, f"parse error: {exc}")
            continue
        try:
            handle(message)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            log("handler failed:", repr(exc))
            if message.get("id") is not None:
                error(message["id"], -32603, f"internal error: {exc}")


if __name__ == "__main__":
    main()
