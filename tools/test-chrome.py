#!/usr/bin/env python3
"""Bounded Chrome omnibox regression using private Xvfb, D-Bus and IBus.

Requires system Python gi/websocket, Chrome, xvfb-run and ibus-daemon. No normal
browser profile, desktop source configuration or physical input is accessed.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]


def stop_private_helpers(root):
    # D-Bus-activated portal/accessibility helpers can outlive their bus. Match
    # the exact marker inherited before session creation, never process names.
    marker = f'MUTE_CHROME_TEST_ROOT={root}'.encode()
    targets = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if marker in (entry / 'environ').read_bytes().split(b'\0'):
                targets.append(int(entry.name))
        except (OSError, PermissionError):
            continue
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in targets:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.15)


def outer():
    for tool in ('dbus-run-session', 'xvfb-run', 'ibus-daemon', 'google-chrome'):
        if not shutil.which(tool):
            raise SystemExit(f'Missing optional test dependency: {tool}')
    with tempfile.TemporaryDirectory(prefix='mute-chrome-test.') as directory:
        root = Path(directory)
        env = os.environ.copy()
        for name, child in (('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'),
                            ('XDG_DATA_HOME', 'data'), ('XDG_RUNTIME_DIR', 'runtime')):
            (root / child).mkdir(mode=0o700)
            env[name] = str(root / child)
        (root / 'components').mkdir()
        env.update(MUTE_CHROME_TEST_ROOT=str(root), MUTE_CHROME_PRIVATE='1',
                   IBUS_ADDRESS=f'unix:path={root}/ibus.sock',
                   IBUS_COMPONENT_PATH=str(root / 'components'),
                   GTK_IM_MODULE='ibus', QT_IM_MODULE='ibus', XMODIFIERS='@im=ibus',
                   GSETTINGS_BACKEND='memory', GIO_USE_VFS='local', PYTHONPATH=str(REPO))
        for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'IBUS_ADDRESS_FILE'):
            env.pop(name, None)
        command = ['dbus-run-session', '--', 'xvfb-run', '-a', '-s', '-screen 0 1100x800x24',
                   '/usr/bin/python3', str(Path(__file__).resolve()), '--inside']
        log = (root / 'session.log').open('w')
        process = subprocess.Popen(command, env=env, start_new_session=True, stderr=log)
        try:
            result = process.wait(timeout=65)
            if result:
                log.flush()
                print((root / 'session.log').read_text()[-5000:], file=sys.stderr)
            return result
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            raise SystemExit('Chrome regression exceeded its 65-second deadline')
        finally:
            stop_private_helpers(root)
            log.close()


def inside():
    root = Path(os.environ.get('MUTE_CHROME_TEST_ROOT', '/missing')).resolve()
    if (os.environ.get('MUTE_CHROME_PRIVATE') != '1' or root.parent != Path('/tmp')
            or not root.name.startswith('mute-chrome-test.')
            or os.environ.get('IBUS_ADDRESS') != f'unix:path={root}/ibus.sock'
            or not os.environ.get('DISPLAY', '').startswith(':')
            or not os.environ.get('DBUS_SESSION_BUS_ADDRESS')):
        raise RuntimeError('Use the isolated outer runner')
    import gi
    import websocket
    gi.require_version('IBus', '1.0')
    from gi.repository import GLib, IBus
    from mute_profanity import config, engine

    context = GLib.MainContext.default()
    def pump(seconds=0.03):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)

    def until(predicate, description, seconds=8):
        end = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= end:
                raise RuntimeError(f'Timed out: {description}')
            pump()

    daemon_log = (root / 'ibus.log').open('w')
    daemon = subprocess.Popen(['ibus-daemon', '--single', '--panel', 'disable',
                               '--emoji-extension', 'disable', '--config', 'disable',
                               '--cache', 'none', '--timeout', '3000', '--address',
                               os.environ['IBUS_ADDRESS']], stdout=daemon_log, stderr=daemon_log)
    chrome = None
    factory = runtime = None
    socket = None
    display = None
    try:
        until(lambda: (root / 'ibus.sock').exists(), 'private IBus socket')
        IBus.init()
        bus = IBus.Bus.new()
        if not bus.is_connected():
            raise RuntimeError('Cannot connect to private IBus')
        config.save(config.Settings(beep=False))
        runtime = engine.Runtime(popup=False)
        seen = []
        original = engine.MuteEngine
        class ObservedEngine(original):
            __gtype_name__ = 'MuteChromeTestEngine'
            def do_process_key_event(self, keyval, keycode, state):
                seen.append(keyval)
                return super().do_process_key_event(keyval, keycode, state)
        engine.MuteEngine = ObservedEngine
        factory = engine.Factory(bus, runtime)
        if not bus.request_name(engine.BUS_NAME, 0):
            raise RuntimeError('Cannot own private engine name')
        component = IBus.Component.new(engine.BUS_NAME, 'Private Chrome regression', '1',
                                       'MIT', '', '', '/bin/false', '')
        component.add_engine(IBus.EngineDesc.new('mute-en', 'Mute test', 'Private test',
                                                'en', 'MIT', '', '', 'us'))
        if not bus.register_component(component):
            raise RuntimeError('Cannot register private engine')
        completed = []
        def switched(source, result, *_):
            completed.append(source.set_global_engine_async_finish(result))
        bus.set_global_engine_async('mute-en', 3000, None, switched, None)
        until(lambda: completed, 'global engine selection')
        if completed != [True]:
            raise RuntimeError('Private engine selection failed')

        chrome_log = (root / 'chrome.log').open('w')
        chrome = subprocess.Popen(['google-chrome', f'--user-data-dir={root / "profile"}',
            '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
            '--disable-sync', '--disable-component-update', '--no-pings', '--password-store=basic',
            '--use-mock-keychain', '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=0', '--remote-allow-origins=*', '--ozone-platform=x11',
            '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1000,700',
            '--host-resolver-rules=MAP * ~NOTFOUND', 'about:blank'],
            stdout=chrome_log, stderr=chrome_log, start_new_session=True)
        port_file = root / 'profile' / 'DevToolsActivePort'
        until(port_file.exists, 'isolated Chrome startup', 15)
        port = int(port_file.read_text().splitlines()[0])
        pages = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=2))
        page = next(page for page in pages if page['type'] == 'page')
        socket = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=2)
        serial = 0
        def cdp(method, parameters=None):
            nonlocal serial
            serial += 1
            socket.send(json.dumps({'id': serial, 'method': method, 'params': parameters or {}}))
            while True:
                message = json.loads(socket.recv())
                if message.get('id') == serial:
                    if 'error' in message:
                        raise RuntimeError(message['error'])
                    return message.get('result', {})
        def location():
            return cdp('Runtime.evaluate', {'expression': 'location.href',
                                           'returnByValue': True})['result'].get('value')
        cdp('Page.navigate', {'url': 'chrome://version/'})
        until(lambda: location() == 'chrome://version/', 'synthetic history visit')
        cdp('Page.navigate', {'url': 'about:blank'})
        until(lambda: location() == 'about:blank', 'blank test page')

        x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
        xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_uint
        x11.XkbKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
        x11.XkbKeycodeToKeysym.restype = ctypes.c_ulong
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        display = x11.XOpenDisplay(None)
        if not display:
            raise RuntimeError('Cannot open the private Xvfb display')
        def key(keysym, pressed):
            code = x11.XKeysymToKeycode(display, keysym)
            if not code:
                raise RuntimeError('Missing private X11 key mapping')
            xtst.XTestFakeKeyEvent(display, code, pressed, 0)
            x11.XFlush(display)
        def press(keysym):
            key(keysym, True)
            key(keysym, False)
            pump(0.08)
        # Chrome's first and only window receives focus in a private X server.
        # Page.bringToFront raises that test window without targeting the host.
        cdp('Page.bringToFront')
        pump(0.3)
        key(0xffe3, True)  # Control_L
        press(ord('l'))
        key(0xffe3, False)
        pump(0.2)
        seen.clear()
        for character in 'chrome://ver':
            sym = ord(character)  # ASCII character keysyms equal their code points
            code = x11.XKeysymToKeycode(display, sym)
            shifted = x11.XkbKeycodeToKeysym(display, code, 0, 0) != sym
            if shifted:
                key(0xffe1, True)
            press(sym)
            if shifted:
                key(0xffe1, False)
        pump(0.5)
        gi.require_version('Gdk', '3.0')
        from gi.repository import Gdk
        screen = Gdk.get_default_root_window()
        pixbuf = Gdk.pixbuf_get_from_window(screen, 0, 0, screen.get_width(), screen.get_height())
        pixbuf.savev('/tmp/mute-chrome-regression.png', 'png', [], [])
        before_enter = len(seen)
        if before_enter < len('chrome://ver'):
            raise RuntimeError(f'Native keys did not traverse Mute IBus: {before_enter} events')
        press(0xff0d)  # Return, including the real IBus key boundary
        try:
            until(lambda: location() == 'chrome://version/', 'native inline completion accepted')
        except RuntimeError:
            print(f'Observed navigation: {location()}', flush=True)
            raise
        if 0xff0d not in seen:
            raise RuntimeError('Return did not traverse the private Mute engine')
        print(f'PASS Chrome native omnibox completion: chrome://ver → chrome://version/; '
              f'{len(seen)} real IBus key events, private profile/display/session')
        return 0
    finally:
        if socket:
            socket.close()
        if display:
            x11.XCloseDisplay(display)
        if chrome:
            try:
                os.killpg(chrome.pid, signal.SIGTERM)
                chrome.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(chrome.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if runtime:
            for instance in tuple(runtime.engines):
                instance.destroy()
            for monitor in runtime.monitors:
                monitor.cancel()
        if factory:
            factory.destroy()
        daemon.terminate()
        try:
            daemon.wait(timeout=2)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=2)


if __name__ == '__main__':
    raise SystemExit(inside() if sys.argv[1:] == ['--inside'] else outer())
