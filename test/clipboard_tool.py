#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Set or read the clipboard inside the session under test.

usage:
  clipboard_tool.py set TEXT          set and exit (ownership is lost on exit)
  clipboard_tool.py hold TEXT [SECS]  own the clipboard for a while
  clipboard_tool.py get               print the current clipboard text
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "get"

    if not Gtk.init_check():
        print("no display available", file=sys.stderr)
        return 1

    clipboard = Gdk.Display.get_default().get_clipboard()

    if mode in ("set", "hold"):
        clipboard.set(sys.argv[2] if len(sys.argv) > 2 else "")
        context = GLib.MainContext.default()
        for _ in range(20):
            while context.pending():
                context.iteration(False)
            GLib.usleep(50_000)

        if mode == "set":
            print("set ok (ownership is lost when this process exits)")
            return 0

        # A clipboard owner must stay alive, so hold the selection while the test runs.
        seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 60
        loop = GLib.MainLoop()
        GLib.timeout_add_seconds(seconds, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
        print("holding", flush=True)
        loop.run()
        return 0

    if mode == "mimes":
        formats = clipboard.get_formats()
        print(f"MIMES={sorted(formats.get_mime_types())}")
        return 0

    if mode == "setimage":
        import gi as _gi
        _gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 64, 64)
        pixbuf.fill(0x336699ff)
        ok, png = pixbuf.save_to_bufferv("png", [], [])
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(bytes(png)))
        provider = Gdk.ContentProvider.new_for_value(texture)
        clipboard.set_content(provider)
        loop = GLib.MainLoop()
        GLib.timeout_add_seconds(int(sys.argv[2]) if len(sys.argv) > 2 else 60,
                                 lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
        print("holding image", flush=True)
        loop.run()
        return 0

    if mode != "get":
        print(f"unknown mode {mode!r}", file=sys.stderr)
        return 2

    loop = GLib.MainLoop()

    def done(source, result):
        try:
            text = source.read_text_finish(result)
        except Exception as exc:  # noqa: BLE001
            text = f"<error: {exc}>"
        print(f"CLIPBOARD={text!r}")
        loop.quit()

    def timeout():
        print("CLIPBOARD=<timeout>")
        loop.quit()
        return GLib.SOURCE_REMOVE

    clipboard.read_text_async(None, done)
    GLib.timeout_add_seconds(5, timeout)
    loop.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
