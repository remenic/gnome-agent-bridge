# gnome-agent-bridge

An MCP server that gives an AI agent hands and eyes on a GNOME desktop: silent
screenshots, native screen recording, pointer/keyboard injection, window control
and an AT-SPI semantic layer.

```
MCP client (Claude Code, Codex, pi, …)
        │  stdio, newline-delimited JSON-RPC
        ▼
mcp/gnome_agent_mcp.py            (no third-party dependencies)
        │  D-Bus session bus
        ▼
gnome-shell  ──►  agentbridge@local extension
                  ├── Shell.Screenshot          (in-process, no dialog)
                  ├── org.gnome.Shell.Screencast (native recorder, mp4/webm)
                  └── Clutter.VirtualInputDevice (pointer + keyboard)
```

## Example

There is nothing to look at in this repository: the output of this MCP server is
JSON and image blocks, not a user interface. A real round trip against a live
session looks like this (window titles redacted):

```jsonc
// tools/call  {"name": "bridge_status"}
{"bridge": "agentbridge/2 shell-51.1 user",
 "expected_api": "agentbridge/2", "api_current": true,
 "capabilities": {"screenshot_area": true, "type_text_unicode": true,
                  "record_region": true, "start_recording": true},
 "state": {"enabled": true, "input_error": null, "recording": false,
           "pointer": {"x": 1067, "y": 16},
           "monitors": [{"index": 0, "x": 0, "y": 0, "width": 2560,
                         "height": 1440, "scale": 1.5, "primary": true}]}}

// tools/call  {"name": "screenshot", "arguments": {"crop": {"x": 100, "y": 100,
//                                                          "width": 400, "height": 300}}}
{"method": "area", "region": {"x": 100, "y": 100, "width": 400, "height": 300},
 "device_scale": 1.5, "stage_per_image_px": 0.666667,
 "image": {"width": 600, "height": 450}}          // + one image content block

// tools/call  {"name": "type_text", "arguments": {"text": "Grüße, 日本語"}}
{"typed": 10, "method": "clipboard", "ascii": false,
 "clipboard_restored": true, "previous_clipboard_chars": 83, "text": "Grüße, 日本語"}

// tools/call  {"name": "click", "arguments": {"x": 340, "y": 250}}
{"clicked": {"x": 340, "y": 250}, "button": 1, "count": 1}
```

(`click`'s exact keys come from the tool's own reply; the point is that every call
returns plain JSON, plus an image content block for `screenshot`.)

The extension deletes its temporary capture file after encoding it, so nothing
of your screen is left behind on disk unless you pass `save_path`.

## Why an extension and not a normal app

Three facts about GNOME 51 shaped this design (all verified on GNOME Shell 51.1):

1. **`org.gnome.Shell.Screenshot` refuses outside callers.** The shell's
   `ScreenshotService` only answers an allowlist (`org.gnome.SettingsDaemon.MediaKeys`
   and the GNOME portal backend); anything else gets
   `AccessDenied: Screenshot is not allowed`. From inside the shell the
   in-process `Shell.Screenshot` API is used instead — the same code path as the
   shell's own screenshot UI.
2. **Recording is a separate service, not the shell.** `org.gnome.Shell.Screencast`
   is D-Bus-activated (`gjs /usr/share/gnome-shell/org.gnome.Shell.Screencast`) and
   records via `org.gnome.Mutter.ScreenCast` + GStreamer. Its `dbusService.js`
   deliberately does *not* watch the shell's own bus name, so a recording started
   from the extension keeps running even if the MCP server exits.
3. **Input injection needs the compositor.** `Clutter.VirtualInputDevice` on the
   default seat gives pointer/keyboard events with no portal consent dialog, no
   `ydotoold`, no `/dev/uinput` and no root.

A further trap worth knowing: `Clutter.get_default_backend()` is gone in mutter 51;
the seat is reached via `global.stage.context.get_backend()`.

## Requirements

- GNOME Shell 45–51 (Wayland or X11)
- `python3` with PyGObject (present on every GNOME system) — nothing else
- For screenshots on Wayland the shell must be the compositor (which it is)

### Running under a service manager

The server needs the **session bus**, not a display. A real case: an agent launched
by a systemd *user* service (paseo) gets `XDG_RUNTIME_DIR` from the user manager but
neither `DBUS_SESSION_BUS_ADDRESS` nor `DISPLAY`/`WAYLAND_DISPLAY`. That still works,
because:

- with the address unset, GLib falls back to `$XDG_RUNTIME_DIR/bus` (and the server
  fills in `XDG_RUNTIME_DIR=/run/user/<uid>` if even that is missing);
- screenshots are taken inside the compositor and input is injected by the
  extension, so no client-side display connection is ever opened — the MCP server
  does not initialise GTK/GDK at all.

What does not work: running the agent inside a container without the graphical
session's bus socket, or under a different uid. The server then fails with an
explicit message naming the two environment variables.

AT-SPI is the one feature with an extra dependency: the accessibility bridge must
be on (`org.gnome.desktop.interface toolkit-accessibility`, or `GTK_A11Y=atspi` per
application) **and** `org.a11y.atspi.Registry` must be running on the accessibility
bus. `ui_tree` returns a `hint` naming whichever of the two is missing — in practice
it is the bridge being off, or a stale accessibility bus after it was restarted:

```bash
gsettings set org.gnome.desktop.interface toolkit-accessibility true
systemctl --user restart at-spi-dbus-bus.service   # re-creates bus + registry
```

Applications started before the change need a restart before they publish a tree.
Revert with `gsettings set org.gnome.desktop.interface toolkit-accessibility false`.

## Install

```bash
git clone https://github.com/remenic/gnome-agent-bridge
cd gnome-agent-bridge
./install.sh
```

`install.sh` copies the extension to `~/.local/share/gnome-shell/extensions/` and
adds it to `org.gnome.shell enabled-extensions` (leaving your other entries alone).
The uuid is `agentbridge@local`; for a fork that may run next to this one, change it
in `metadata.json` and the `UUID` in `install.sh`.

Optionally install the server as a command instead of pointing clients at the
script (PyGObject has to come from the system, so the environment must see system
site-packages):

```bash
pipx install --system-site-packages .
# then: {"command": "gnome-agent-bridge"}
```

Then log out and back in: GNOME Shell only scans the extension directory at
startup, and GNOME 51 does not implement `org.gnome.Shell.Extensions.ReloadExtension`.
Verify:

```bash
gdbus call --session --dest org.gnome.Shell \
  --object-path /org/gnome/AgentBridge --method org.gnome.AgentBridge.Ping
# ('agentbridge/2 shell-51.1 user',)
```

Register the MCP server with your client (`command` must be an absolute path):

```json
{
  "mcpServers": {
    "gnome": {
      "command": "/home/you/gnome-agent-bridge/mcp/gnome_agent_mcp.py"
    }
  }
}
```

The server is a plain stdin/stdout MCP server, so any client works. If you
installed it with `pipx` above, swap the path for `gnome-agent-bridge`:

| Client | Register |
|---|---|
| Claude Code | `claude mcp add gnome -- /path/to/mcp/gnome_agent_mcp.py` |
| Codex CLI | `codex mcp add gnome -- /path/to/mcp/gnome_agent_mcp.py` |
| pi | `pi mcp add gnome -- /path/to/mcp/gnome_agent_mcp.py` |
| VS Code | `.vscode/mcp.json`: `{"servers": {"gnome": {"type": "stdio", "command": "/path/to/mcp/gnome_agent_mcp.py"}}}` |
| Cursor | `~/.cursor/mcp.json`, using the `mcpServers` block above |
| Claude Desktop | `claude_desktop_config.json`, using the `mcpServers` block above |

One caveat for all of them: the server talks to the *session* bus, so the client
must run inside your graphical login. A systemd user service does not inherit
`DBUS_SESSION_BUS_ADDRESS`; the server falls back to `$XDG_RUNTIME_DIR/bus`, which
is why that variable must be present (see *Running under a service manager*).

## Tools

| Group | Tools |
|---|---|
| Observation | `screenshot` (whole screen, `monitor`, or `crop`; scale/format; returns an image block), `ui_tree`, `list_windows`, `list_monitors`, `bridge_status` |
| Recording | `record_start` (full screen or region), `record_stop`, `record_status` |
| Pointer | `pointer_move`, `click`, `pointer_button`, `drag`, `scroll` |
| Keyboard | `type_text` (ASCII as keysyms, Unicode via clipboard paste), `press_key` (`"Return"`, `"ctrl+alt+t"`, `"ctrl+shift+v"`) |
| Windows | `focus_window`, `move_window`, `focus_app` |
| Semantic | `ui_tree`, `click_element`, `set_value` |
| Safety | `set_automation` (kill switch), `wait` |

Coordinates are stage pixels, the same space `click`/`pointer_move` expect.
`ui_tree` returns element bounds with `bounds_space`: `"stage"` when the
compositor origin could be resolved, `"window"` otherwise.

### Screenshots

`Shell.Screenshot` writes PNG to a `GOutputStream`; the MCP server crops, scales
and encodes (GDK-Pixbuf), then returns the image as an MCP `image` content block
plus a JSON metadata block. Typical latency on this machine: ~220 ms for a
1280×720 full-screen PNG.

### Monitors, regions and coordinates

All coordinates an agent sees and sends are **logical stage pixels** — the same
space as `list_windows`, `ui_tree` bounds and `click`. `monitor` and `crop` are
handed to `Shell.Screenshot.screenshot_area()` inside the compositor, which is
the shell's own scale-aware path, so a HiDPI or fractional-scale display does
not need any correction from the caller.

Screenshots report how to map image pixels back to stage pixels:

| field | meaning |
|---|---|
| `region` | the captured rectangle in stage coordinates |
| `stage` | the whole desktop rectangle (union of the monitors) |
| `device_scale` | image pixels per stage pixel before any downscale |
| `stage_per_image_px` | multiply an image pixel by this, add `region.x/y`, to get a click coordinate |
| `clamped_from` | present when a crop reached past the screen edge and was clamped |

A crop that lies entirely outside the desktop is refused with the stage size in
the message, and an unknown `monitor` index is refused with the monitor count —
no silent empty images.

`record_start` accepts the same rectangle as `region` (validated the same way);
`record_status` reports it while recording.

### Recording

Uses GNOME's own recorder, so the container/codec is whatever GNOME picks
(hardware encoders win when available: the test machine produced H.264 MP4,
2.3 s → 71 KB). Pass a path template *without* an extension — the shell appends
the one that matches the pipeline.

### Typing text, including Unicode

Keysyms are layout independent for ASCII, but a character only arrives if the
active keymap maps that keysym — on a plain US layout `ü`, `ß` or `日本語` are
simply dropped by the compositor (measured: `Grüße 日本語` → `Gre `). So:

| `method` | Behaviour |
|---|---|
| `auto` (default) | ASCII → keysyms; anything else → clipboard + `Ctrl+V` |
| `keysyms` | always keystrokes (lossy for unmappable characters) |
| `clipboard` | always a paste, even for ASCII |

The clipboard path saves the previous clipboard text, pastes, and puts it back —
verified by reading the clipboard again, not by assuming the write worked. If the
previous clipboard could not be read, it is *cleared* instead, so agent-typed text
(e.g. a password) does not linger.

Because a text round trip would destroy a non-text clipboard, `auto` first asks
`St.Clipboard.get_mimetypes()` and **refuses** when the clipboard holds something
like `image/png`, telling the caller to pick `clipboard` (overwrite) or `keysyms`
(keep it). `method: "clipboard"` reports
`previous_clipboard_type: "non-text (replaced)"` so the decision stays visible.

### Versioning

`Ping` returns `agentbridge/2 …`, and `bridge_status` also reports a capability
map derived from the extension's own introspection XML (`screenshot_area`,
`type_text_unicode`, `record_region`, …). Version numbers alone are a poor test —
GNOME Shell only loads extension changes at login, and method additions are
backwards compatible — so the server asks what the loaded build can actually do
and degrades where possible: an extension without `ScreenshotArea` still gets
correct `monitor`/`crop` screenshots through full-capture + local cropping at
scale 1, and the result says `method: "full+crop (extension lacks
ScreenshotArea)"` instead of pretending. Only a real signature change (the
`TypeText` arguments) produces a hard, actionable error.

## Semantic layer (AT-SPI)

`ui_tree` walks the accessibility tree and returns elements as
`{path, role, name, states, bounds, actions, editable, text}`. `click_element`
prefers the element's own AT-SPI action (`click`/`press`/`toggle`) and only falls
back to a pointer click, `set_value` writes through EditableText and verifies by
reading the field back.

Two AT-SPI details this handles for you:

- **`CoordType.SCREEN` is useless on Wayland** — every element reports `0,0`.
  The bridge asks for `CoordType.WINDOW` coordinates and adds the window origin
  from the compositor (matched by window title) to get stage pixels.
- **The bindings changed shape.** at-spi2-core 2.6x flattens the interfaces onto
  `Atspi.Accessible` (`is_action()`, `get_n_actions()`, `get_text_at_offset()`),
  while older versions return separate interface objects with `n_actions`/`get_text`.
  Both shapes work here.

AT-SPI is only populated when the accessibility bridge is on, i.e.
`org.gnome.desktop.interface toolkit-accessibility true` (or `GTK_A11Y=atspi` per
app). Without it `ui_tree` returns an empty list — that is a system setting, not a
bug in this bridge, and everything else keeps working.

## Verification

Everything below was executed against a **headless nested GNOME Shell 51.1** on
Arch (`test/nested-shell.sh`), so the author's live desktop was never touched:

- `Screenshot` → 1280×720 PNG, 225 ms; `crop`+`scale`+`jpeg` → 200×125 JPEG
- `TypeText "Hello, Agent Bridge! 123"` → the GTK4 entry contained exactly
  `Hello, Agent Bridge! 123` (shift handling for `H`, `A`, `B`, `!` verified)
- `KeyChord [Return]` → the app's `activate` handler fired with that text
- `Click`, `Drag`, `Scroll`, `MoveWindow`, `ActivateWindow` → no errors, window
  geometry verified through `ListWindows`
- `StartRecording`/`StopRecording` → 2.29 s H.264 MP4, 72 KB, `ffprobe` clean
- `SetEnabled(false)` → input tools refuse with
  `automation is disabled; call SetEnabled(true) first`
- AT-SPI (against a real session): 11 interactive nodes with actions,
  `click_element` pressed a GTK button, `set_value` verified its readback

And on a real logged-in GNOME 51.1 session (1920×1080, Wayland):

- extension `State: ACTIVE`, `Ping` → `agentbridge/1 shell-51.1 user` (that build;
  the current one reports `agentbridge/2`)
- full-screen screenshot in **207 ms**
- `ui_tree` resolved the compositor origin (`origin: {475, 442}`) and reported
  `bounds_space: "stage"`; `click_element` on a text entry used the pointer
  fallback at those stage coordinates and the following `type_text` landed in
  exactly that field — which also proves the AT-SPI→stage offset math
- a recording started by one MCP server process kept running after that process
  exited (3.19 s / 183 KB, stopped by a *different* server process), confirming
  the shell owns the recorder
- Unicode: `type_text "Grüße, 日本語, 🚀 — klaar"` → the GTK entry held exactly that
  string through the clipboard path (21 characters) and the previous clipboard
  content read back unchanged afterwards (`clipboard_restored: true`)
- the non-text guard, exercised with a faked `image/png` clipboard: `auto` refused
  with an explanation, `keysyms` left the clipboard alone, `clipboard` proceeded
  and reported `previous_clipboard_type: "non-text (replaced)"`

Multi-monitor setup (headless nested shell with two virtual outputs,
1280x720 + 1024x768 side by side, stage 2304x768):

- `screenshot` → 2304x768 region, downscaled to 1600x533 with
  `stage_per_image_px: 1.44`; `monitor: 0` → 1280x720, `monitor: 1` → 1024x768
- a crop of 200x100 at (1300,50) → 200x100, and a crop crossing the monitor seam
  worked; a crop running off the right edge was clamped (400x400 → 104x68 with
  `clamped_from`); a crop fully outside and `monitor: 7` were refused with
  actionable messages
- content verified visually: an Xterm moved to (1300,40) appears in
  `monitor: 1` and not in `monitor: 0`
- `record_start(region=monitor 1)` → H.264 **1024x768**, and a frame extracted
  with ffmpeg shows that monitor with the pointer drawn in
- `move_window` on the Xterm → `verified: true`; on a Wayland (GTK4) window →
  `verified: false` plus a note, because Wayland clients position themselves

## Testing

```bash
./test/nested-shell.sh start          # headless nested shell on a private bus
DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/agtest/bus \
    python3 test/mcp_client.py        # drives the MCP server like a real client
./test/nested-shell.sh stop
```

`test/gtk_target.py` is a GTK4 window with an entry and a button that write what
they receive to files, so input tests assert on exact text instead of OCR.

## License

GPL-3.0-or-later (see `LICENSE`). The code is original, so this was a free choice;
the `-or-later` form also keeps the option open to lift snippets from
GPL-2.0-or-later projects such as mutter and gnome-shell, whose behavior informed
this implementation.

## Provenance

The code was written from scratch; no code was copied. It builds only on platform
APIs — GJS (`gi://Gio`, `GLib`, `Clutter`, `St`, `Meta`, `Shell`) and PyGObject
(`Gio`, `GLib`, `GdkPixbuf`, `Atspi`) — with no third-party packages and nothing
vendored.

To make that checkable rather than a claim: comparing this extension against the
implementations it was designed alongside gives a `difflib` similarity of 0.006 to
0.025, and the 2–15 shared lines per file are all unavoidable vocabulary — `import`
statements, `GObject.registerClass(this)`, `invocation.return_value(null)`, the
D-Bus method names both projects expose, and `for (const actor of
global.get_window_actors())`.

Upstream sources were consulted for **facts**, each verified in the source or on a
live system, never copied:

| source | fact taken |
|---|---|
| mutter 51 `clutter/clutter/clutter-virtual-input-device.h` | `notify_*(time_us, …)` signatures |
| mutter 51 `clutter/clutter/clutter-seat.c` | `create_virtual_device(device_type)` takes one argument |
| `Clutter-51.gir` | enum members (`ButtonState.PRESSED`, `ScrollSource.WHEEL`, `ScrollFinishFlags.NONE`) |
| gnome-shell 51 `js/ui/keyboard.js` | the only way to a seat in 51: `global.stage.context.get_backend().get_default_seat()` |
| gnome-shell 51 `js/ui/screenshot.js` | the `org.gnome.Shell.Screenshot` sender allowlist, and that the shell promisifies `Shell.Screenshot` helpers |
| gnome-shell 51 `src/shell-screenshot.h` | `screenshot_area(x, y, width, height, stream)` |
| installed `org.gnome.Shell.Screencast` service | `_trackSender` deliberately skips the shell, so a recording outlives its caller |
| at-spi2-core (live system) | that 2.6x flattens interfaces onto `Accessible` (`get_text()` takes no range) |

## Security

This is a privilege boundary by design: while automation is enabled any process
that can talk to your session bus can drive your pointer, keyboard and screen.
Mitigations built in:

- `set_automation(false)` (or the panel indicator toggle) blocks all input
  injection; read-only tools keep working
- the panel indicator shows whether automation is on and whether a recording runs
- nothing listens on the network; MCP is stdio only
- the extension only exposes a local D-Bus interface; there is no portal prompt
  because there is no portal — treat the session bus as trusted, like GNOME does

## Limitations

- `monitor`/`crop` go through the compositor's `screenshot_area()` when the loaded
  extension exposes it; an older extension falls back to full-screen capture plus a
  local crop (exact at scale 1, approximated otherwise).
- `type_text` sends keysyms: ASCII is layout independent, non-ASCII depends on
  the active keymap (unmappable characters are dropped by the compositor) — hence
  the clipboard path for Unicode.
- `move_window` can only reposition **X11** windows. Wayland clients position
  themselves, so the compositor's request is ignored; the tool re-reads the
  geometry and reports `verified: false` with a note instead of pretending.
- HiDPI: verified on a 2560×1440 output at `scale 1.5` — a 400×300 stage crop came
  back as 600×450 device pixels with `device_scale: 1.5`. Fractional scales below
  1.5 were not available to test; the same compositor path is used, but treat
  other ratios as unverified.
- `ui_tree` positions on Wayland need the compositor origin; without the
  extension (or for a window the title match misses) bounds stay window-relative.
- Recording keeps running if the MCP server dies; stop it with `record_stop` or
  the indicator menu.
- GNOME 51 only: older shells may differ in `Shell.Screenshot` arity and
  `Screencast` options; both are called defensively, but only 51 is tested.
- The server reconnects when its session bus connection closes. A shell or bus
  restart makes Gio hand back the same *closed* connection from `bus_get_sync()`,
  so a plain retry cannot recover; the server detects `is_closed()` and dials a
  fresh `Gio.DBusConnection.new_for_address_sync()` instead. A running server
  therefore survives a logout/login, but an extension that is not loaded in the
  new session still needs a login first.
