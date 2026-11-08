// Agent Bridge — GNOME Shell extension exposing a D-Bus API for AI agents.
//
// It runs inside the compositor, which is what makes the following possible
// without a portal dialog, without ydotool and without /dev/uinput:
//
//   * screenshots -> in-process Shell.Screenshot, the same code path the
//                    shell's own screenshot UI uses. (The org.gnome.Shell.Screenshot
//                    D-Bus interface only answers an allowlisted set of senders:
//                    org.gnome.SettingsDaemon.MediaKeys and the GNOME portal
//                    backend, so an MCP server cannot call it from outside.)
//   * recording   -> org.gnome.Shell.Screencast, a D-Bus-activated service that
//                    records through org.gnome.Mutter.ScreenCast + GStreamer.
//                    That service does not watch the shell's own bus name, so a
//                    recording started from here keeps running.
//   * input       -> Clutter.VirtualInputDevice on the default seat.
//
// SPDX-License-Identifier: GPL-3.0-or-later

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const OBJECT_PATH = '/org/gnome/AgentBridge';
const INTERFACE = 'org.gnome.AgentBridge';
const ERROR_NAME = 'org.gnome.AgentBridge.Error';

const SCREENCAST_BUS = 'org.gnome.Shell.Screencast';
const SCREENCAST_PATH = '/org/gnome/Shell/Screencast';
const SCREENCAST_IFACE = 'org.gnome.Shell.Screencast';

const IFACE_XML = `
<node>
  <interface name="${INTERFACE}">
    <method name="Ping">
      <arg type="s" direction="out" name="version"/>
    </method>
    <method name="GetState">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="SetEnabled">
      <arg type="b" direction="in" name="enabled"/>
      <arg type="b" direction="out" name="enabled"/>
    </method>

    <method name="Screenshot">
      <arg type="b" direction="in" name="include_cursor"/>
      <arg type="s" direction="in" name="path"/>
      <arg type="s" direction="out" name="path"/>
    </method>
    <method name="ScreenshotArea">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="s" direction="in" name="path"/>
      <arg type="s" direction="out" name="path"/>
    </method>

    <method name="StartRecording">
      <arg type="s" direction="in" name="file_template"/>
      <arg type="b" direction="in" name="draw_cursor"/>
      <arg type="i" direction="in" name="framerate"/>
      <arg type="s" direction="out" name="path"/>
    </method>
    <method name="StartRecordingArea">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="s" direction="in" name="file_template"/>
      <arg type="b" direction="in" name="draw_cursor"/>
      <arg type="i" direction="in" name="framerate"/>
      <arg type="s" direction="out" name="path"/>
    </method>
    <method name="StopRecording">
      <arg type="s" direction="out" name="path"/>
    </method>
    <method name="GetRecordingState">
      <arg type="s" direction="out" name="json"/>
    </method>

    <method name="MovePointer">
      <arg type="d" direction="in" name="x"/>
      <arg type="d" direction="in" name="y"/>
    </method>
    <method name="PointerButton">
      <arg type="i" direction="in" name="button"/>
      <arg type="b" direction="in" name="pressed"/>
    </method>
    <method name="Click">
      <arg type="d" direction="in" name="x"/>
      <arg type="d" direction="in" name="y"/>
      <arg type="i" direction="in" name="button"/>
      <arg type="i" direction="in" name="count"/>
    </method>
    <method name="Drag">
      <arg type="d" direction="in" name="x1"/>
      <arg type="d" direction="in" name="y1"/>
      <arg type="d" direction="in" name="x2"/>
      <arg type="d" direction="in" name="y2"/>
      <arg type="i" direction="in" name="button"/>
      <arg type="i" direction="in" name="steps"/>
    </method>
    <method name="Scroll">
      <arg type="d" direction="in" name="x"/>
      <arg type="d" direction="in" name="y"/>
      <arg type="d" direction="in" name="dx"/>
      <arg type="d" direction="in" name="dy"/>
    </method>
    <method name="Key">
      <arg type="u" direction="in" name="keysym"/>
      <arg type="b" direction="in" name="pressed"/>
    </method>
    <method name="KeyChord">
      <arg type="au" direction="in" name="keysyms"/>
    </method>
    <method name="TypeText">
      <arg type="s" direction="in" name="text"/>
      <arg type="s" direction="in" name="method"/>
      <arg type="u" direction="in" name="restore_delay_ms"/>
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="GetPointer">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="GetMonitors">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="ListWindows">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="ActivateWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="MoveWindow">
      <arg type="t" direction="in" name="id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
  </interface>
</node>`;

// X11 keysyms (values from keysymdef.h).
const XK = {
    BackSpace: 0xff08,
    Tab: 0xff09,
    Return: 0xff0d,
    Escape: 0xff1b,
    Delete: 0xffff,
    Home: 0xff50,
    Left: 0xff51,
    Up: 0xff52,
    Right: 0xff53,
    Down: 0xff54,
    Page_Up: 0xff55,
    Page_Down: 0xff56,
    End: 0xff57,
    Shift_L: 0xffe1,
    Control_L: 0xffe3,
    Alt_L: 0xffe9,
    Super_L: 0xffeb,
};

// Characters that need Shift on a US layout -> their unshifted base character.
const SHIFT_PAIRS = [
    ['`', '~'], ['1', '!'], ['2', '@'], ['3', '#'], ['4', '$'], ['5', '%'],
    ['6', '^'], ['7', '&'], ['8', '*'], ['9', '('], ['0', ')'], ['-', '_'],
    ['=', '+'], ['[', '{'], [']', '}'], ['\\', '|'], [';', ':'], ["'", '"'],
    [',', '<'], ['.', '>'], ['/', '?'],
];
const SHIFTED = new Map();
for (const [base, shifted] of SHIFT_PAIRS)
    SHIFTED.set(shifted, base);
for (let i = 0; i < 26; i++)
    SHIFTED.set(String.fromCharCode(65 + i), String.fromCharCode(97 + i));

const BUTTONS = {1: 1, 2: 2, 3: 3, left: 1, middle: 2, right: 3};

// Mimetypes we can safely save and restore as text. Anything else (images, files
// only) would be destroyed by a text clipboard round trip, so `auto` refuses.
const CLIPBOARD_TEXT_MIMES = new Set([
    'text/plain', 'text/plain;charset=utf-8', 'utf8_string', 'string', 'text',
    'text/uri-list', 'text/html',
]);

const BTN_PRESSED = Clutter.ButtonState.PRESSED;
const BTN_RELEASED = Clutter.ButtonState.RELEASED;
const KEY_PRESSED = Clutter.KeyState.PRESSED;
const KEY_RELEASED = Clutter.KeyState.RELEASED;

function nowUs() {
    return Math.max(1, GLib.get_monotonic_time());
}

function sleep(ms) {
    return new Promise(resolve => {
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, Math.max(1, Math.round(ms)), () => {
            resolve();
            return GLib.SOURCE_REMOVE;
        });
    });
}

function charToKeysym(ch) {
    switch (ch) {
    case '\n':
    case '\r':
        return {keysym: XK.Return, shift: false};
    case '\t':
        return {keysym: XK.Tab, shift: false};
    case '\b':
        return {keysym: XK.BackSpace, shift: false};
    default:
        break;
    }

    if (SHIFTED.has(ch)) {
        const base = SHIFTED.get(ch);
        return {keysym: base.codePointAt(0), shift: true};
    }

    const cp = ch.codePointAt(0);
    if (cp >= 0x20 && cp <= 0xff)
        return {keysym: cp, shift: false};

    // Unicode keysyms are 0x01000000 + codepoint.
    return {keysym: 0x01000000 + cp, shift: false};
}

class AgentBridgeService {
    constructor() {
        this._enabled = true;
        this._seat = null;
        this._pointer = null;
        this._keyboard = null;
        this._inputError = null;
        this._shooter = null;
        this._clipboard = null;
        this._recording = false;
        this._recordingPath = null;
        this._recordingRegion = null;
        this._recordingStartedAt = 0;
        this._screencastSupported = false;
        this._recordingError = null;
        this._errorSubId = 0;
        this._activityHook = null;
        this._lastActivity = 0;
    }

    // --- lifecycle -------------------------------------------------------
    enable() {
        this._shooter = new Shell.Screenshot();
        this._clipboard = St.Clipboard.get_default();

        try {
            // mutter 51 dropped Clutter.get_default_backend(); global.backend is the
            // Meta.Backend, which implements the Clutter.Backend interface.
            // GNOME 51: Clutter.get_default_backend() is gone; the backend hangs off
            // the stage context (see js/ui/keyboard.js in gnome-shell).
            const backend = global.stage.context.get_backend();
            this._seat = backend.get_default_seat();
            this._pointer = this._seat.create_virtual_device(
                Clutter.InputDeviceType.POINTER_DEVICE);
            this._keyboard = this._seat.create_virtual_device(
                Clutter.InputDeviceType.KEYBOARD_DEVICE);
        } catch (e) {
            this._inputError = e.message;
            logError(e, 'agentbridge: could not create virtual input devices');
        }

        this._probeScreencast();

        // The recorder can die mid-recording (encoder crash, out of disk space);
        // the service then emits Error and drops the recorder, so a later
        // StopScreencast only answers false. Keep the reason.
        this._errorSubId = Gio.DBus.session.signal_subscribe(
            SCREENCAST_BUS, SCREENCAST_IFACE, 'Error', SCREENCAST_PATH, null,
            Gio.DBusSignalFlags.NONE,
            (connection, sender, path, iface, signal, params) => {
                const [name, message] = params.deepUnpack();
                this._recordingError = `${name}: ${message}`;
                this._recording = false;
                this._touch('record-error');
                log(`agentbridge: screencast error: ${this._recordingError}`);
            });
    }

    destroy() {
        if (this._errorSubId) {
            Gio.DBus.session.signal_unsubscribe(this._errorSubId);
            this._errorSubId = 0;
        }
        this._pointer = null;
        this._keyboard = null;
        this._seat = null;
        this._shooter = null;
    }

    setActivityHook(fn) {
        this._activityHook = fn;
    }

    _touch(what) {
        this._lastActivity = GLib.get_monotonic_time();
        try {
            this._activityHook?.(what);
        } catch (e) {
            logError(e, 'agentbridge: activity hook failed');
        }
    }

    _probeScreencast() {
        Gio.DBus.session.call(SCREENCAST_BUS, SCREENCAST_PATH,
            'org.freedesktop.DBus.Properties', 'Get',
            new GLib.Variant('(ss)', [SCREENCAST_IFACE, 'ScreencastSupported']),
            null, Gio.DBusCallFlags.NONE, 5000, null,
            (connection, result) => {
                try {
                    const [variant] = connection.call_finish(result).deepUnpack();
                    this._screencastSupported = variant.get_boolean();
                } catch (e) {
                    this._screencastSupported = false;
                }
                this._touch('probe');
            });
    }

    _requireInput() {
        if (this._inputError)
            throw new Error(`virtual input devices unavailable: ${this._inputError}`);
        if (!this._pointer || !this._keyboard)
            throw new Error('virtual input devices not initialised');
        if (!this._enabled)
            throw new Error('automation is disabled; call SetEnabled(true) first');
    }

    // --- introspection ---------------------------------------------------
    Ping() {
        // Bump the API number whenever the D-Bus signatures change, so the MCP
        // server can tell when the session still runs an older extension.
        return `agentbridge/2 shell-${Config.PACKAGE_VERSION} ${Main.sessionMode.currentMode}`;
    }

    GetState() {
        const [, pointerX, pointerY] = global.get_pointer();
        return JSON.stringify({
            enabled: this._enabled,
            input_error: this._inputError,
            recording: this._recording,
            recording_path: this._recordingPath,
            recording_region: this._recordingRegion,
            recording_error: this._recordingError,
            recording_seconds: this._recordingStartedAt
                ? (GLib.get_monotonic_time() - this._recordingStartedAt) / 1e6
                : 0,
            pointer: {x: pointerX, y: pointerY},
            monitors: this._monitors(),
            screencast_supported: this._screencastSupported,
        });
    }

    SetEnabled(enabled) {
        this._enabled = !!enabled;
        if (!this._enabled && this._pointer && this._keyboard)
            this._releaseAllModifiers();
        return this._enabled;
    }

    // --- screenshots -----------------------------------------------------
    // gnome-shell's own js/ui/screenshot.js calls
    // Gio._promisify(Shell.Screenshot.prototype, 'screenshot') at import time, so in
    // a live shell that method has already been replaced by a Promise-returning
    // wrapper (Gio._promisify stashes the GI method as `_original_screenshot`).
    // Passing our own callback to the wrapper makes it append a second one, which
    // GJS rejects with "Too many arguments" and the promise then never settles.
    _screenshotAsync(includeCursor, stream) {
        if (typeof Shell.Screenshot.prototype._original_screenshot === 'function')
            return this._shooter.screenshot(includeCursor, stream);

        return new Promise((resolve, reject) => {
            this._shooter.screenshot(includeCursor, stream, null, (shooter, result) => {
                try {
                    shooter.screenshot_finish(result);
                    resolve();
                } catch (e) {
                    reject(e);
                }
            });
        });
    }

    // Same Gio._promisify caveat as screenshot(): the shell promisifies
    // screenshot_area too, and the promisified wrapper must be called without a
    // callback (see _screenshotAsync above).
    _screenshotAreaAsync(x, y, width, height, stream) {
        if (typeof Shell.Screenshot.prototype._original_screenshot_area === 'function')
            return this._shooter.screenshot_area(x, y, width, height, stream);

        return new Promise((resolve, reject) => {
            this._shooter.screenshot_area(x, y, width, height, stream, null,
                (shooter, result) => {
                    try {
                        shooter.screenshot_area_finish(result);
                        resolve();
                    } catch (e) {
                        reject(e);
                    }
                });
        });
    }

    async _shoot(path, shoot) {
        if (!this._shooter)
            this._shooter = new Shell.Screenshot();

        const file = Gio.File.new_for_path(path);
        const stream = file.replace(null, false,
            Gio.FileCreateFlags.REPLACE_DESTINATION, null);

        try {
            await shoot(stream);
        } finally {
            try {
                stream.close(null);
            } catch (e) {
                logError(e, 'agentbridge: closing screenshot stream');
            }
        }

        const size = file.query_info('standard::size',
            Gio.FileQueryInfoFlags.NONE, null).get_size();
        if (size === 0)
            throw new Error('screenshot produced an empty file');

        this._touch('screenshot');
        return path;
    }

    _screenshot(includeCursor, path) {
        return this._shoot(path, stream => this._screenshotAsync(includeCursor, stream));
    }

    _screenshotArea(x, y, width, height, path) {
        return this._shoot(path, stream =>
            this._screenshotAreaAsync(x, y, width, height, stream));
    }

    ScreenshotAsync([includeCursor, path], invocation) {
        this._screenshot(includeCursor, path)
            .then(p => invocation.return_value(new GLib.Variant('(s)', [p])))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    ScreenshotAreaAsync([x, y, width, height, path], invocation) {
        this._screenshotArea(x, y, width, height, path)
            .then(p => invocation.return_value(new GLib.Variant('(s)', [p])))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    // --- screen recording ------------------------------------------------
    _screencast(method, params) {
        return new Promise((resolve, reject) => {
            Gio.DBus.session.call(SCREENCAST_BUS, SCREENCAST_PATH, SCREENCAST_IFACE,
                method, params, null, Gio.DBusCallFlags.NONE, 60000, null,
                (connection, result) => {
                    try {
                        resolve(connection.call_finish(result).deepUnpack());
                    } catch (e) {
                        reject(e);
                    }
                });
        });
    }

    async _startRecording(fileTemplate, drawCursor, framerate, area) {
        if (this._recording)
            throw new Error(`already recording to ${this._recordingPath}`);

        const options = {
            'draw-cursor': new GLib.Variant('b', !!drawCursor),
            'framerate': new GLib.Variant('i', framerate > 0 ? framerate : 30),
        };

        let reply;
        if (area) {
            reply = await this._screencast('ScreencastArea',
                new GLib.Variant('(iiiisa{sv})',
                    [area.x, area.y, area.width, area.height, fileTemplate, options]));
        } else {
            reply = await this._screencast('Screencast',
                new GLib.Variant('(sa{sv})', [fileTemplate, options]));
        }

        const [success, filename] = reply;
        if (!success)
            throw new Error('the screencast service refused to start recording');

        this._recording = true;
        this._recordingPath = filename;
        this._recordingRegion = area ? {x: area.x, y: area.y,
            width: area.width, height: area.height} : null;
        this._recordingStartedAt = GLib.get_monotonic_time();
        this._recordingError = null;
        this._touch('record-start');
        return filename;
    }

    StartRecordingAsync([fileTemplate, drawCursor, framerate], invocation) {
        this._startRecording(fileTemplate, drawCursor, framerate, null)
            .then(p => invocation.return_value(new GLib.Variant('(s)', [p])))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    StartRecordingAreaAsync([x, y, width, height, fileTemplate, drawCursor, framerate],
        invocation) {
        this._startRecording(fileTemplate, drawCursor, framerate, {x, y, width, height})
            .then(p => invocation.return_value(new GLib.Variant('(s)', [p])))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    async _stopRecording() {
        const path = this._recordingPath;
        const failure = this._recordingError;
        const [success] = await this._screencast('StopScreencast', null);

        this._recording = false;
        this._recordingPath = null;
        this._recordingRegion = null;
        this._recordingStartedAt = 0;
        this._recordingError = null;
        this._touch('record-stop');

        if (!success) {
            if (failure)
                throw new Error(`the recorder stopped early: ${failure}`);
            throw new Error('no recording was running');
        }
        return path ?? '';
    }

    StopRecordingAsync(_params, invocation) {
        this._stopRecording()
            .then(p => invocation.return_value(new GLib.Variant('(s)', [p])))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    GetRecordingState() {
        return JSON.stringify({
            recording: this._recording,
            path: this._recordingPath,
            region: this._recordingRegion,
            error: this._recordingError,
            seconds: this._recordingStartedAt
                ? (GLib.get_monotonic_time() - this._recordingStartedAt) / 1e6
                : 0,
        });
    }

    // --- pointer ---------------------------------------------------------
    _moveTo(x, y) {
        this._pointer.notify_absolute_motion(nowUs(), x, y);
    }

    async _glide(x1, y1, x2, y2, steps) {
        const n = Math.max(1, Math.min(steps || 1, 120));
        for (let i = 1; i <= n; i++) {
            const t = i / n;
            this._moveTo(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t);
            if (i < n)
                await sleep(10);
        }
    }

    MovePointerAsync([x, y], invocation) {
        try {
            this._requireInput();
            this._moveTo(x, y);
            this._touch('pointer-move');
            invocation.return_value(null);
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
        }
    }

    PointerButtonAsync([button, pressed], invocation) {
        try {
            this._requireInput();
            const btn = BUTTONS[button] ?? (Number(button) || 1);
            this._pointer.notify_button(nowUs(), btn,
                pressed ? BTN_PRESSED : BTN_RELEASED);
            this._touch(pressed ? 'pointer-down' : 'pointer-up');
            invocation.return_value(null);
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
        }
    }

    async _click(x, y, button, count) {
        const btn = BUTTONS[button] ?? (Number(button) || 1);
        const n = Math.max(1, Math.min(count || 1, 5));

        this._moveTo(x, y);
        await sleep(30);

        for (let i = 0; i < n; i++) {
            this._pointer.notify_button(nowUs(), btn, BTN_PRESSED);
            await sleep(20);
            this._pointer.notify_button(nowUs(), btn, BTN_RELEASED);
            if (i < n - 1)
                await sleep(60);
        }
        this._touch('click');
    }

    ClickAsync([x, y, button, count], invocation) {
        try {
            this._requireInput();
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
            return;
        }
        this._click(x, y, button, count)
            .then(() => invocation.return_value(null))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    async _drag(x1, y1, x2, y2, button, steps) {
        const btn = BUTTONS[button] ?? (Number(button) || 1);

        this._moveTo(x1, y1);
        await sleep(30);
        this._pointer.notify_button(nowUs(), btn, BTN_PRESSED);
        await sleep(50);
        await this._glide(x1, y1, x2, y2, steps || 12);
        await sleep(50);
        this._pointer.notify_button(nowUs(), btn, BTN_RELEASED);
        await sleep(20);
        this._touch('drag');
    }

    DragAsync([x1, y1, x2, y2, button, steps], invocation) {
        try {
            this._requireInput();
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
            return;
        }
        this._drag(x1, y1, x2, y2, button, steps)
            .then(() => invocation.return_value(null))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    ScrollAsync([x, y, dx, dy], invocation) {
        try {
            this._requireInput();
            if (x >= 0 && y >= 0)
                this._moveTo(x, y);
            this._pointer.notify_scroll_continuous(nowUs(), dx, dy,
                Clutter.ScrollSource.WHEEL, Clutter.ScrollFinishFlags.NONE);
            this._touch('scroll');
            invocation.return_value(null);
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
        }
    }

    GetPointer() {
        const [, x, y] = global.get_pointer();
        return JSON.stringify({x, y});
    }

    GetMonitors() {
        return JSON.stringify(this._monitors());
    }

    _monitors() {
        // St.ThemeContext.scale_factor is the *integer UI* scale and stays 1 on a
        // fractionally scaled output (a 1.5 monitor still reports 1). The monitor's
        // own geometry_scale is the value an agent needs to reason about device
        // pixels, so prefer it and fall back to the UI scale only if it is absent.
        let uiScale = 1;
        try {
            uiScale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        } catch (e) {
            // ignore
        }
        return Main.layoutManager.monitors.map((m, i) => ({
            index: i,
            x: m.x,
            y: m.y,
            width: m.width,
            height: m.height,
            scale: typeof m.geometry_scale === 'number' ? m.geometry_scale : uiScale,
            primary: i === Main.layoutManager.primaryIndex,
        }));
    }

    // --- keyboard --------------------------------------------------------
    _keyval(keysym, pressed) {
        this._keyboard.notify_keyval(nowUs(), keysym,
            pressed ? KEY_PRESSED : KEY_RELEASED);
    }

    _releaseAllModifiers() {
        for (const mod of [XK.Shift_L, XK.Control_L, XK.Alt_L, XK.Super_L]) {
            try {
                this._keyval(mod, false);
            } catch (e) {
                // ignore
            }
        }
    }

    KeyAsync([keysym, pressed], invocation) {
        try {
            this._requireInput();
            this._keyval(keysym, pressed);
            this._touch(pressed ? 'key-down' : 'key-up');
            invocation.return_value(null);
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
        }
    }

    async _chord(keysyms) {
        const list = keysyms.filter(k => k > 0);
        for (const k of list) {
            this._keyval(k, true);
            await sleep(12);
        }
        for (const k of [...list].reverse()) {
            this._keyval(k, false);
            await sleep(12);
        }
        this._touch('chord');
    }

    KeyChordAsync([keysyms], invocation) {
        try {
            this._requireInput();
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
            return;
        }
        this._chord(keysyms)
            .then(() => invocation.return_value(null))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    _isAsciiText(text) {
        for (const ch of text) {
            const cp = ch.codePointAt(0);
            const printable = cp >= 0x20 && cp <= 0x7e;
            const control = ch === '\n' || ch === '\r' || ch === '\t' || ch === '\b';
            if (!printable && !control)
                return false;
        }
        return true;
    }

    // Reading the clipboard is async and never answers if nothing owns the
    // selection, so bound it with a short timeout.
    _clipboardText() {
        return new Promise(resolve => {
            let settled = false;
            const finish = value => {
                if (settled)
                    return;
                settled = true;
                resolve(value);
            };

            const timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
                finish(null);
                return GLib.SOURCE_REMOVE;
            });

            try {
                this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (clipboard, text) => {
                    GLib.source_remove(timeoutId);
                    finish(text ?? '');
                });
            } catch (e) {
                GLib.source_remove(timeoutId);
                finish(null);
            }
        });
    }

    _clipboardMimetypes() {
        try {
            return Array.from(
                this._clipboard.get_mimetypes(St.ClipboardType.CLIPBOARD) ?? []);
        } catch (e) {
            return [];
        }
    }

    async _typeViaClipboard(text, restoreDelay, force) {
        const mimes = this._clipboardMimetypes();
        const textual = mimes.length === 0 ||
            mimes.some(m => CLIPBOARD_TEXT_MIMES.has(m.toLowerCase()));

        if (!textual && !force) {
            throw new Error(`the clipboard holds non-text data (${mimes.join(', ')}); ` +
                'pass method "clipboard" to overwrite it, or "keysyms" to keep it');
        }

        const previous = await this._clipboardText();

        this._clipboard.set_text(St.ClipboardType.CLIPBOARD, text);
        await sleep(120);

        await this._chord([XK.Control_L, 0x76]); // Ctrl+V
        await sleep(Math.max(100, Math.min(restoreDelay || 400, 3000)));

        // Put the user's clipboard back; if we could not read it, clear it rather
        // than leave whatever the agent typed in the clipboard.
        this._clipboard.set_text(St.ClipboardType.CLIPBOARD, previous ?? '');

        const after = await this._clipboardText();
        const restored = previous === null
            ? (after === '' || after === null)
            : after === previous;

        return {
            restored,
            previous_chars: previous === null ? -1 : previous.length,
            previous_mimetypes: mimes,
            previous_replaced: !textual,
        };
    }

    async _typeText(text, method, restoreDelay) {
        const kind = (method || 'auto').toLowerCase();
        const ascii = this._isAsciiText(text);
        const useClipboard = kind === 'clipboard' ||
            (kind !== 'keysyms' && !ascii);

        if (useClipboard) {
            const {restored, previous_chars, previous_mimetypes, previous_replaced} =
                await this._typeViaClipboard(text, restoreDelay, kind === 'clipboard');
            this._touch('type-text-clipboard');
            return {
                typed: [...text].length,
                method: 'clipboard',
                ascii,
                clipboard_restored: restored,
                previous_clipboard_chars: previous_chars,
                previous_clipboard_type: previous_replaced ? 'non-text (replaced)'
                    : (previous_mimetypes.length ? 'text' : 'empty'),
            };
        }

        let typed = 0;
        for (const ch of text) {
            const {keysym, shift} = charToKeysym(ch);
            if (shift)
                this._keyval(XK.Shift_L, true);
            this._keyval(keysym, true);
            await sleep(6);
            this._keyval(keysym, false);
            if (shift) {
                await sleep(6);
                this._keyval(XK.Shift_L, false);
            }
            typed++;
            await sleep(6);
        }
        this._touch('type-text');
        return {typed, method: 'keysyms', ascii, clipboard_restored: null};
    }

    TypeTextAsync([text, method, restoreDelay], invocation) {
        try {
            this._requireInput();
        } catch (e) {
            invocation.return_dbus_error(ERROR_NAME, e.message);
            return;
        }
        this._typeText(text, method, restoreDelay)
            .then(info => invocation.return_value(
                new GLib.Variant('(s)', [JSON.stringify(info)])))
            .catch(e => invocation.return_dbus_error(ERROR_NAME, e.message));
    }

    // --- windows ---------------------------------------------------------
    ListWindows() {
        return JSON.stringify(this._windows());
    }

    _windows() {
        const out = [];
        const focus = global.display.focus_window;
        const focusId = focus ? focus.get_stable_sequence() : 0;

        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (!win)
                continue;

            const type = win.get_window_type();
            if (type === Meta.WindowType.DESKTOP || type === Meta.WindowType.DOCK ||
                type === Meta.WindowType.SPLASHSCREEN)
                continue;

            const rect = win.get_frame_rect();
            const workspace = win.get_workspace();
            out.push({
                id: win.get_stable_sequence(),
                title: win.get_title() ?? '',
                wm_class: win.get_wm_class() ?? '',
                pid: win.get_pid() ?? 0,
                focused: win.get_stable_sequence() === focusId,
                minimized: !!win.minimized,
                workspace: workspace ? workspace.index() : -1,
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
            });
        }

        out.sort((a, b) => (a.focused === b.focused ? 0 : a.focused ? -1 : 1));
        return out;
    }

    _windowById(id) {
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (win && win.get_stable_sequence() === id)
                return win;
        }
        return null;
    }

    // Note: synchronous D-Bus handlers are invoked with spread arguments
    // (plus a trailing Gio.UnixFDList), not with a single params array.
    ActivateWindow(id) {
        this._requireInput();
        const win = this._windowById(id);
        if (!win)
            throw new Error(`no window with id ${id}`);
        if (win.minimized)
            win.unminimize();
        Main.activateWindow(win);
        this._touch('focus-window');
        return true;
    }

    MoveWindow(id, x, y, width, height) {
        this._requireInput();
        const win = this._windowById(id);
        if (!win)
            throw new Error(`no window with id ${id}`);
        win.move_resize_frame(true, x, y, width, height);
        this._touch('move-window');
        return true;
    }
}

class AgentBridgeIndicator extends PanelMenu.Button {
    static {
        GObject.registerClass(this);
    }

    constructor() {
        super(0.5, 'Agent Bridge', false);

        this._icon = new St.Icon({
            icon_name: 'input-keyboard-symbolic',
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this._statusItem = new PopupMenu.PopupMenuItem('Automation: on', {reactive: false});
        this.menu.addMenuItem(this._statusItem);

        this._recordItem = new PopupMenu.PopupMenuItem('Recording: off', {reactive: false});
        this.menu.addMenuItem(this._recordItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._toggleItem = new PopupMenu.PopupMenuItem('Disable automation');
        this.menu.addMenuItem(this._toggleItem);

        this._stopItem = new PopupMenu.PopupMenuItem('Stop recording');
        this._stopItem.setSensitive(false);
        this.menu.addMenuItem(this._stopItem);
    }

    update(state) {
        const enabled = state?.enabled ?? true;
        const recording = state?.recording ?? false;

        this._icon.icon_name = recording
            ? 'media-record-symbolic'
            : 'input-keyboard-symbolic';
        this._statusItem.label.text = `Automation: ${enabled ? 'on' : 'off'}`;
        this._toggleItem.label.text = enabled ? 'Disable automation' : 'Enable automation';
        this._recordItem.label.text = recording ? 'Recording: yes' : 'Recording: off';
        this._stopItem.setSensitive(recording);
    }
}

export default class AgentBridgeExtension extends Extension {
    enable() {
        this._service = new AgentBridgeService();
        this._service.enable();
        this._service.setActivityHook(() => this._refresh());

        this._indicator = new AgentBridgeIndicator();
        Main.panel.addToStatusArea('agentbridge', this._indicator);

        this._indicator._toggleItem.connect('activate', () => {
            const state = this._state();
            this._service.SetEnabled(!(state?.enabled ?? true));
            this._refresh();
        });

        this._indicator._stopItem.connect('activate', () => {
            this._service._stopRecording()
                .catch(e => logError(e, 'agentbridge: stop recording'));
        });

        this._exported = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this._service);
        this._exported.export(Gio.DBus.session, OBJECT_PATH);

        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });

        this._refresh();
        console.log(`agentbridge: exported ${INTERFACE} at ${OBJECT_PATH}`);
    }

    _state() {
        try {
            return JSON.parse(this._service.GetState());
        } catch (e) {
            return null;
        }
    }

    _refresh() {
        try {
            this._indicator?.update(this._state());
        } catch (e) {
            logError(e, 'agentbridge: refreshing the indicator');
        }
    }

    disable() {
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = 0;
        }

        if (this._service?._recording) {
            this._service._stopRecording()
                .catch(e => logError(e, 'agentbridge: stopping recording on disable'));
        }

        this._exported?.unexport();
        this._exported = null;

        this._indicator?.destroy();
        this._indicator = null;

        this._service?.destroy();
        this._service = null;
    }
}
