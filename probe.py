"""Замеры лампочки через API умного дома Яндекса — то, чем были найдены ограничения из README.

  python3 probe.py info              — лампы аккаунта и их умения
  python3 probe.py rtt 10            — 10 смен цвета подряд, время ответа API
  python3 probe.py scene 16          — своя анимация из N шагов (4 цвета блоками) — смотри на лампу
  python3 probe.py white             — вернуть обычный белый свет (сбрасывает зависшую анимацию)

Токен — YANDEX_IOT_TOKEN в .env. Лампа — по имени из LAMP_NAME или первая подходящая.
Прокси с российским выходом, если нужен, — LAMP_PROXY=http://host:port.
"""
import os
import statistics
import sys
import time

import httpx

API = "https://api.iot.yandex.net/v1.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def token():
    for line in open(os.environ.get("LAMP_ENV", os.path.join(HERE, ".env")), encoding="utf-8"):
        if line.startswith("YANDEX_IOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("нет YANDEX_IOT_TOKEN в .env")


def client():
    return httpx.Client(proxy=os.environ.get("LAMP_PROXY") or None, trust_env=False, timeout=15,
                        headers={"Authorization": f"Bearer {token()}"})


def lamps(c):
    r = c.get(f"{API}/user/info")
    r.raise_for_status()
    return [d for d in r.json()["devices"] if d["type"].startswith("devices.types.light")]


def pick(c):
    ls = lamps(c)
    name = os.environ.get("LAMP_NAME")
    for d in ls:
        if not name or d["name"] == name:
            return d
    sys.exit(f"лампа {name!r} не найдена")


def color(c, lamp_id, state):
    body = {"devices": [{"id": lamp_id, "actions": [
        {"type": "devices.capabilities.color_setting", "state": state}]}]}
    t0 = time.perf_counter()
    r = c.post(f"{API}/devices/actions", json=body)
    ms = (time.perf_counter() - t0) * 1000
    j = r.json()
    try:
        return ms, j["devices"][0]["capabilities"][0]["state"]["action_result"]["status"]
    except (KeyError, IndexError, TypeError):
        return ms, j.get("message", f"http {r.status_code}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "info"
    with client() as c:
        if mode == "info":
            for d in lamps(c):
                for cap in d["capabilities"]:
                    print(d["name"], d["id"], cap["type"].rsplit(".", 1)[-1], cap.get("parameters"))
            return
        lamp = pick(c)
        if mode == "rtt":
            xs = []
            for i in range(int(sys.argv[2]) if len(sys.argv) > 2 else 10):
                ms, st = color(c, lamp["id"], {"instance": "hsv", "value": {"h": 0 if i % 2 else 240, "s": 100, "v": 100}})
                xs.append(ms)
                print(f"{i + 1:2d}: {ms:.0f} мс {st}")
                time.sleep(0.5)
            print(f"медиана {statistics.median(xs):.0f} мс, макс {max(xs):.0f} мс")
        elif mode == "scene":
            n = int(sys.argv[2]) if len(sys.argv) > 2 else 16
            hues = (0, 120, 240, 60)   # красный, зелёный, синий, жёлтый — по n/4 шагов
            steps = [{"hs": {"h": hues[i * 4 // n], "s": 100}, "brightness": 100, "duration": 500} for i in range(n)]
            ms, st = color(c, lamp["id"], {"instance": "scene", "value": {"id": "", "steps": steps}})
            print(f"{n} шагов: {st} за {ms:.0f} мс — если лампа не сменила картинку, столько шагов она не держит")
        elif mode == "white":
            print(*color(c, lamp["id"], {"instance": "temperature_k", "value": 4000}))


if __name__ == "__main__":
    main()
