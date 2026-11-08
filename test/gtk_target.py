#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test target for input injection: a GTK4 window with one entry.

Every keystroke the entry receives is written to $AGTEST_ENTRY_FILE (and to stdout
when Enter is pressed), so the test harness can verify exactly what the bridge
managed to type - no OCR needed.
"""
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

OUT = os.environ.get("AGTEST_ENTRY_FILE") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
    "agtest", "entry.txt")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.local.AgentBridgeTarget")
        self.window = None
        self.entry = None

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("Agent Bridge test target")
        win.set_default_size(720, 220)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        label = Gtk.Label(label="Type here:")
        label.set_xalign(0)
        box.append(label)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("click me, then type")
        box.append(self.entry)

        self.status = Gtk.Label(label="focus: no")
        self.status.set_xalign(0)
        box.append(self.status)

        self.button = Gtk.Button(label="Press me")
        self.button.connect("clicked", self.on_button)
        box.append(self.button)

        win.set_child(box)
        win.present()
        self.window = win

        self.entry.grab_focus()

        self.entry.connect("changed", self.on_changed)
        self.entry.connect("activate", self.on_activate)
        win.connect("notify::is-active", lambda w, p: self.status.set_text(
            f"focus: {'yes' if w.is_active() else 'no'}"))

        sys.stdout.write("READY\n")
        sys.stdout.flush()

    def on_changed(self, entry):
        text = entry.get_text()
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stdout.write(f"CHANGED {text!r}\n")
        sys.stdout.flush()

    def on_activate(self, entry):
        sys.stdout.write(f"ACTIVATED {entry.get_text()!r}\n")
        sys.stdout.flush()

    def on_button(self, _button):
        with open(OUT + ".button", "a", encoding="utf-8") as fh:
            fh.write("BUTTON_PRESSED\n")
        sys.stdout.write("BUTTON_PRESSED\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(App().run([]))
