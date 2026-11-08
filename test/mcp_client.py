#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Smoke test for the MCP server: drives it over stdio exactly like a real client.

Run it with DBUS_SESSION_BUS_ADDRESS pointing at the session you want to test,
e.g. the nested test shell:

    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/agtest/bus \
        python3 test/mcp_client.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp",
                      "gnome_agent_mcp.py")
OUTDIR = os.environ.get("AGTEST_RUNTIME", "/run/user/%d/agtest" % os.getuid())


class Client:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self.next_id = 0

    def request(self, method, params=None, timeout=60):
        self.next_id += 1
        message_id = self.next_id
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout\n" + self.proc.stderr.read())
            reply = json.loads(line)
            if reply.get("id") == message_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def call(self, tool, **arguments):
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        texts, images = [], []
        for block in result.get("content", []):
            if block["type"] == "text":
                texts.append(block["text"])
            elif block["type"] == "image":
                images.append(block)
        return result.get("isError", False), texts, images

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait(timeout=10)


def spawn_target(client):
    """Start the GTK4 test window inside the session under test and wait for it."""
    env = dict(os.environ)
    env.setdefault("WAYLAND_DISPLAY", os.environ.get("AGTEST_DISPLAY", "wayland-agtest"))
    env.setdefault("GDK_BACKEND", "wayland")
    env.setdefault("AGTEST_ENTRY_FILE", os.path.join(OUTDIR, "entry.txt"))
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtk_target.py")
    process = subprocess.Popen([sys.executable, app], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    print(f"       started test target (pid {process.pid})")
    for _ in range(30):
        time.sleep(0.2)
        windows = json.loads(client.call("list_windows")[1][0])["windows"]
        match = next((w for w in windows if "Agent Bridge" in (w["title"] or "")), None)
        if match and match["width"] > 0 and match["height"] > 0:
            return match
    return None


def show(label, failed, texts, images=None, expect_error=False):
    images = images or []
    if expect_error:
        status = "ok  " if failed else "FAIL"
    else:
        status = "FAIL" if failed else "ok  "
    body = " | ".join(texts)[:300].replace("\n", " ")
    extra = f" (+{len(images)} image)" if images else ""
    print(f"[{status}] {label}: {body}{extra}", flush=True)


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    client = Client()

    init = client.request("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke-test", "version": "1"},
    })
    print("server:", init["serverInfo"], "protocol:", init["protocolVersion"])
    client.notify("notifications/initialized")

    tools = client.request("tools/list")["tools"]
    print(f"tools: {len(tools)} ->", ", ".join(t["name"] for t in tools))

    show("bridge_status", *client.call("bridge_status"))
    show("list_monitors", *client.call("list_monitors"))

    failed, texts, images = client.call("screenshot", include_cursor=True)
    show("screenshot", failed, texts, images)
    if images:
        path = os.path.join(OUTDIR, "mcp-shot.png")
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(images[0]["data"]))
        print(f"       wrote {path} ({images[0]['mimeType']})")

    failed, texts, _ = client.call("screenshot", crop={"x": 100, "y": 100,
                                                       "width": 400, "height": 250},
                                   scale=0.5, format="jpeg")
    show("screenshot(crop,scale,jpeg)", failed, texts)

    monitors = json.loads(client.call("list_monitors")[1][0])["monitors"]
    for monitor in monitors:
        failed, texts, images = client.call("screenshot", monitor=monitor["index"])
        meta = json.loads(texts[0]) if not failed else {}
        size = meta.get("image") or {}
        ok = size.get("width") == monitor["width"] and size.get("height") == monitor["height"]
        print(f"[{'ok  ' if ok else 'FAIL'}] screenshot(monitor={monitor['index']}): "
              f"{size.get('width')}x{size.get('height')} for a "
              f"{monitor['width']}x{monitor['height']} monitor, "
              f"stage_per_image_px={meta.get('stage_per_image_px')}", flush=True)

    failed, texts, _ = client.call("screenshot", monitor=99)
    show("screenshot(monitor=99) refused as expected", failed, texts,
         expect_error=True)

    windows = json.loads(client.call("list_windows")[1][0])["windows"]
    target = next((w for w in windows if "Agent Bridge" in (w["title"] or "")), None)

    if target is None and os.environ.get("AGTEST_SPAWN_TARGET") == "1":
        target = spawn_target(client)

    if target:
        print(f"target window: {target['title']!r} at {target['x']},{target['y']} "
              f"{target['width']}x{target['height']}")
        show("focus_window", *client.call("focus_window", id=target["id"]))
        entry_x = target["x"] + target["width"] / 2
        entry_y = target["y"] + 120
        show("click(entry)", *client.call("click", x=entry_x, y=entry_y))
        show("press_key(ctrl+a)", *client.call("press_key", key="ctrl+a"))
        show("type_text", *client.call("type_text", text="Typed via MCP: 42%"))
        if os.path.exists(os.path.join(OUTDIR, "entry.txt")):
            with open(os.path.join(OUTDIR, "entry.txt"), encoding="utf-8") as handle:
                print(f"       entry now contains: {handle.read()!r}")

        UNICODE = "Grüße, 日本語, 🚀 — klaar"
        client.call("press_key", key="ctrl+a")
        show("type_text(unicode/auto)", *client.call("type_text", text=UNICODE))
        time.sleep(0.5)
        if os.path.exists(os.path.join(OUTDIR, "entry.txt")):
            with open(os.path.join(OUTDIR, "entry.txt"), encoding="utf-8") as handle:
                got = handle.read()
            print(f"       unicode round trip: {got == UNICODE} ({got!r})")
        show("press_key(Return)", *client.call("press_key", key="Return"))
        show("scroll", *client.call("scroll", dy=-2))
        show("drag", *client.call("drag", from_x=entry_x, from_y=entry_y,
                                  to_x=entry_x + 60, to_y=entry_y))
    else:
        print("no test window found (start test/gtk_target.py first)")

    show("record_start", *client.call("record_start",
                                      path=os.path.join(OUTDIR, "mcp-rec"),
                                      draw_cursor=True, framerate=30))
    client.call("pointer_move", x=300, y=300)
    client.call("pointer_move", x=700, y=400)
    client.call("type_text", text="rec")
    client.call("wait", ms=800)
    show("record_status", *client.call("record_status"))
    show("record_stop", *client.call("record_stop"))

    region_path = os.path.join(OUTDIR, "mcp-region-rec")
    show("record_start(region 320x200)", *client.call(
        "record_start", path=region_path,
        region={"x": 0, "y": 0, "width": 320, "height": 200}))
    client.call("wait", ms=700)
    show("record_stop(region)", *client.call("record_stop"))

    show("ui_tree", *client.call("ui_tree", max_nodes=8))
    show("set_automation(false)", *client.call("set_automation", enabled=False))
    show("click while disabled refused", *client.call("click", x=10, y=10),
         expect_error=True)
    show("set_automation(true)", *client.call("set_automation", enabled=True))

    client.close()
    print("done")


if __name__ == "__main__":
    main()
