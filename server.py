"""Светошоу для лампочки Яндекса: отдаёт страницу из web/ и пересылает команды в API умного дома.

Браузер не может звать api.iot.yandex.net напрямую (нет CORS), поэтому команды идут
через этот маленький сервер. Токен берётся из .env (YANDEX_IOT_TOKEN) или из заголовка
Authorization запроса. Сервер слушает только 127.0.0.1 — не открывай его наружу:
кто достучится до порта, тот управляет твоей лампой.

Запуск:  pip install httpx && python3 server.py   →   http://127.0.0.1:8766
"""
import hashlib
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

API = "https://api.iot.yandex.net/v1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("LAMP_HOST", "127.0.0.1")
PORT = int(os.environ.get("LAMP_PORT", "8766"))
# Из-за границы API Яндекса может не отвечать — тогда нужен HTTP-прокси с российским выходом.
PROXY = os.environ.get("LAMP_PROXY") or None
ORIGINS = set(filter(None, os.environ.get("LAMP_ORIGINS", "").split(",")))   # для страницы с другого адреса
MAX_STEPS = 16
MAX_BODY = 64 * 1024
# Без заголовка Authorization берём токен из .env. Выключи (LAMP_LOCAL_TOKEN=0), если
# открываешь порт наружу: тогда каждый посетитель обязан прислать свой токен.
LOCAL_TOKEN_MODE = os.environ.get("LAMP_LOCAL_TOKEN", "1") == "1"
ENV_FILE = os.environ.get("LAMP_ENV", os.path.join(HERE, ".env"))
STATIC_DIR = os.environ.get("LAMP_STATIC", os.path.join(HERE, "web"))
# Каким адресом браузер обращается к серверу. Всё прочее отклоняется — иначе чужой сайт
# через DNS-rebinding мог бы управлять лампой. Свой домен добавляй через LAMP_HOSTS=a,b.
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}",
                 *filter(None, os.environ.get("LAMP_HOSTS", "").split(","))}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
        ".mp3": "audio/mpeg", ".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
        ".svg": "image/svg+xml", ".woff2": "font/woff2", ".json": "application/json",
        ".ico": "image/x-icon", ".webp": "image/webp", ".txt": "text/plain; charset=utf-8"}


def env_token():
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            if line.startswith("YANDEX_IOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None

# короткий таймаут: подвисший через прокси запрос лучше оборвать и повторить, чем ждать 10 с
_client = httpx.Client(proxy=PROXY, trust_env=False, timeout=httpx.Timeout(4.0, connect=8.0))
_locks_guard = threading.Lock()
_locks = {}      # (хэш токена, лампа) -> замок: куски одной лампы уходят в Яндекс строго по очереди
_last_seq = {}   # (хэш токена, лампа) -> номер последнего доставленного куска
_newest = {}     # (хэш токена, лампа) -> самый свежий кусок, который уже пришёл в очередь


def lamp_lock(key):
    with _locks_guard:
        if key not in _locks:
            if len(_locks) > 1000:          # чужие ключи не копим бесконечно
                _locks.clear()
                _last_seq.clear()
                _newest.clear()
            _locks[key] = threading.Lock()
        return _locks[key]


def call(method, path, token, body=None):
    """Запрос к API. Сетевые сбои прокси — до 3 попыток. Возвращает (status, json|None, ms)."""
    t0 = time.perf_counter()
    last = None
    for _ in range(3):
        try:
            r = _client.request(method, f"{API}{path}", json=body,
                                headers={"Authorization": f"Bearer {token}"})
            try:
                data = r.json()
            except ValueError:
                data = None
            return r.status_code, data, (time.perf_counter() - t0) * 1000
        except httpx.TransportError as e:
            last = type(e).__name__
            time.sleep(0.2)
    return 0, {"message": f"сеть: {last}"}, (time.perf_counter() - t0) * 1000


def color_action(token, lamp, state):
    body = {"devices": [{"id": lamp, "actions": [
        {"type": "devices.capabilities.color_setting", "state": state}]}]}
    status, data, ms = call("POST", "/devices/actions", token, body)
    try:
        res = data["devices"][0]["capabilities"][0]["state"]["action_result"]
        return {"ok": res.get("status") == "DONE", "ms": round(ms), "detail": res.get("status", "?")}
    except (TypeError, KeyError, IndexError):
        msg = data.get("message") if isinstance(data, dict) else None
        return {"ok": False, "ms": round(ms), "detail": msg or f"http {status}"}


def clean_steps(raw):
    steps = []
    for s in raw[:MAX_STEPS]:
        steps.append({
            "hs": {"h": int(s["h"]) % 360, "s": max(0, min(100, int(s["s"])))},
            "brightness": max(1, min(100, float(s.get("b", 100)))),
            "duration": max(50.0, min(600000.0, float(s["d"]))),
        })
    return steps


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _foreign(self, post=False):
        """Запрос не с нашей страницы: чужой Host (DNS-rebinding) или чужой Origin / не-JSON у POST (CSRF)."""
        host = self.headers.get("Host", "")
        if host not in ALLOWED_HOSTS:
            print(f"{time.strftime('%H:%M:%S')} отклонён Host {host!r} (добавь в LAMP_HOSTS, если это ты)", flush=True)
            return True
        if post:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return True
            origin = self.headers.get("Origin")
            if origin and origin not in ORIGINS and origin.split("://", 1)[-1] != host:
                return True
        return False

    def _token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and len(auth) > 20:
            return auth[7:].strip()
        return env_token() if LOCAL_TOKEN_MODE else None

    def _static(self):
        """Файлы собранного сайта, с поддержкой Range — без неё браузер плохо перематывает mp3."""
        if not STATIC_DIR:
            return self._json(404, {"error": "not found"})
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        root = os.path.realpath(STATIC_DIR)
        try:
            full = os.path.realpath(os.path.join(root, urllib.parse.unquote(path).lstrip("/")))
        except ValueError:
            return self._json(404, {"error": "not found"})
        if not (full == root or full.startswith(root + os.sep)):
            return self._json(404, {"error": "not found"})
        if os.path.isdir(full):
            if not path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", path + "/")
                self.end_headers()
                return
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        size = os.path.getsize(full)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        partial = rng.startswith("bytes=") and "," not in rng
        if partial:
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                else:
                    start, end = max(0, size - int(b)), size - 1
            except ValueError:
                partial = False
            if partial and (start > end or start >= size):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self._foreign():
            return self._json(403, {"error": "чужой адрес"})
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path != "/api/lamps":
            return self._static()
        token = self._token()
        if not token:
            return self._json(401, {"error": "нужен вход через Яндекс"})
        status, data, ms = call("GET", "/user/info", token)
        if status != 200 or not isinstance(data, dict):
            return self._json(status if status in (401, 403) else 502,
                              {"error": (data or {}).get("message") if isinstance(data, dict) else f"http {status}"})
        rooms = {r["id"]: r.get("name") for r in data.get("rooms", []) if isinstance(r, dict)}
        lamps = []
        for d in data.get("devices", []):
            if not str(d.get("type", "")).startswith("devices.types.light"):
                continue
            scene_ok = any(c.get("type") == "devices.capabilities.color_setting"
                           and (c.get("parameters") or {}).get("custom_params_available")
                           for c in d.get("capabilities", []))
            lamps.append({"id": d["id"], "name": d.get("name"), "room": rooms.get(d.get("room")),
                          "scenes": bool(scene_ok)})
        self._json(200, {"lamps": lamps, "ms": round(ms)})

    def do_POST(self):
        if self._foreign(post=True):
            return self._json(403, {"error": "запрос не со страницы светошоу"})
        token = self._token()
        if not token:
            return self._json(401, {"error": "нужен вход через Яндекс"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self._json(413, {"error": "слишком большой запрос"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if not isinstance(body, dict) or not isinstance(body.get("lamp"), str) or not body["lamp"]:
            return self._json(400, {"error": "нужен lamp"})
        lamp = body["lamp"]
        if self.path == "/api/scene":
            try:
                steps = clean_steps(body.get("steps") or [])
                seq = int(body.get("seq", 0))
            except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
                return self._json(400, {"error": "bad steps"})
            if not steps:
                return self._json(400, {"error": "empty steps"})
            key = (hashlib.sha256(token.encode()).hexdigest()[:16], lamp)
            if seq:
                with _locks_guard:
                    _newest[key] = max(seq, _newest.get(key, 0))
            with lamp_lock(key):
                # если прокси подвис и куски скопились, шлём только самый свежий
                if seq and (seq < _last_seq.get(key, 0) or seq < _newest.get(key, 0)):
                    return self._json(200, {"ok": False, "ms": 0, "detail": "устарел, пропущен"})
                res = color_action(token, lamp, {"instance": "scene", "value": {"id": "", "steps": steps}})
                if res["ok"] and seq:
                    _last_seq[key] = max(seq, _last_seq.get(key, 0))
        elif self.path == "/api/white":
            res = color_action(token, lamp, {"instance": "temperature_k", "value": 4000})
        else:
            return self._json(404, {"error": "not found"})
        print(f"{time.strftime('%H:%M:%S')} {self.path} ok={res['ok']} {res['ms']}ms {res['detail']}", flush=True)
        self._json(200, res)


def keep_warm():
    """Соединение через прокси открывается секунды. Держим его тёплым запросом без токена (401 — норма)."""
    while True:
        try:
            _client.get(f"{API}/user/info")
        except httpx.HTTPError as e:
            print(f"{time.strftime('%H:%M:%S')} прогрев: {type(e).__name__}", flush=True)
        time.sleep(20)


if __name__ == "__main__":
    print(f"светошоу: http://{HOST}:{PORT}  (токен из .env: {'да' if env_token() else 'нет'})", flush=True)
    threading.Thread(target=keep_warm, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
