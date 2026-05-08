"""
Local HTTP+SSE dashboard server for the ITS-G5 bridge.

- GET /           → serves dashboard.html
- GET /events     → Server-Sent Events stream of frame JSONs
- broadcast()     → push a frame to all connected browsers (called from the bridge)

Pure stdlib: http.server + threading + queue. No new dependencies.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# PyInstaller --onefile extracts bundled data into sys._MEIPASS; fall back to
# the script's own directory in normal `python its_g5_bridge.py` runs.
_HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_HTML_PATH = os.path.join(_HERE, "dashboard.html")

_HISTORY_MAX = 200

_subs_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_history: collections.deque[bytes] = collections.deque(maxlen=_HISTORY_MAX)
_node_id = "?"

# MQTT publishing state — exposed to the bridge via is_mqtt_enabled() and to
# the browser via SSE 'mqtt_state' / POST /api/mqtt.
_mqtt_enabled = True       # user-toggle (only meaningful while available)
_mqtt_available = False    # True once a client is actually connected


def is_mqtt_enabled() -> bool:
    return _mqtt_enabled and _mqtt_available


def get_mqtt_state() -> dict:
    return {"enabled": _mqtt_enabled, "available": _mqtt_available}


def set_mqtt_state(enabled: bool | None = None, available: bool | None = None) -> dict:
    """Update either flag (None = leave unchanged) and broadcast the result."""
    global _mqtt_enabled, _mqtt_available
    if available is not None:
        _mqtt_available = bool(available)
    if enabled is not None:
        _mqtt_enabled = bool(enabled)
    _broadcast(_format_event("mqtt_state", get_mqtt_state()), persist=False)
    return get_mqtt_state()


def _format_event(name: str, data: dict) -> bytes:
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n".encode("utf-8")


def _broadcast(msg: bytes, persist: bool) -> None:
    with _subs_lock:
        if persist:
            _history.append(msg)
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def broadcast_frame(frame: dict) -> None:
    """Push a frame dict to all currently-connected SSE clients and remember it."""
    _broadcast(_format_event("frame", frame), persist=True)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default access log
        logging.debug("dashboard %s - %s", self.address_string(), fmt % args)

    def do_GET(self):  # noqa: N802 (stdlib API)
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/events":
            self._serve_sse()
        elif self.path == "/api/mqtt":
            self._serve_json(get_mqtt_state())
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802 (stdlib API)
        if self.path == "/api/mqtt":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error(400, "invalid JSON")
                return
            if "enabled" not in body:
                self.send_error(400, "missing 'enabled'")
                return
            self._serve_json(set_mqtt_state(enabled=bool(body["enabled"])))
        else:
            self.send_error(404)

    def _serve_json(self, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        try:
            with open(_HTML_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(500, "dashboard.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=1000)
        with _subs_lock:
            backlog = list(_history)
            _subscribers.append(q)

        try:
            self.wfile.write(_format_event("hello", {"node_id": _node_id, "mqtt": get_mqtt_state()}))
            for msg in backlog:
                self.wfile.write(msg)
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    # heartbeat to keep proxies and the browser happy
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)


def start(port: int, node_id: str = "?") -> ThreadingHTTPServer:
    """Spin up the dashboard server on a daemon thread; return the server handle."""
    global _node_id
    _node_id = node_id
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True)
    t.start()
    logging.info("dashboard listening on http://127.0.0.1:%d", port)
    return server
