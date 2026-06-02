"""
webhook.py — простой HTTP-сервер для ручного запуска трекера.
Запускается параллельно с scheduler.py в отдельном потоке.
POST /run  → запускает polki_tracker.main() в фоне
GET  /     → возвращает статус
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import polki_tracker

log = logging.getLogger(__name__)
_running = threading.Event()


def _run_tracker():
    if _running.is_set():
        log.info("Трекер уже запущен, пропускаем")
        return
    _running.set()
    try:
        log.info("Webhook: запуск трекера...")
        polki_tracker.main()
        log.info("Webhook: трекер завершён")
    finally:
        _running.clear()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info("HTTP %s", format % args)

    def do_GET(self):
        status = "running" if _running.is_set() else "idle"
        body = f'{{"status": "{status}"}}\n'.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/run":
            threading.Thread(target=_run_tracker, daemon=True).start()
            body = b'{"ok": true, "message": "started"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def start(port: int = 8080):
    server = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Webhook-сервер запущен на порту %d", port)
