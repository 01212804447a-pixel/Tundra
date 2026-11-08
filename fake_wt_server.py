#!/usr/bin/env python3
"""
Имитация локального API War Thunder (порт 8111) для проверки дальномера без игры.

    python fake_wt_server.py            # карта 4096 x 4096 м
    python wt_rangefinder.py            # в другом окне

Игрок медленно едет по карте, метка стоит на месте.

    python fake_wt_server.py --mode air          # имитация полёта для flight_recorder.py
    python fake_wt_server.py --mode air --speed 10   # полёт в 10 раз быстрее
"""
from __future__ import annotations

import argparse
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAP_SIZE = 4096.0
START = time.time()
MODE = {"mode": "ground", "speed": 1.0, "flight": 240.0, "pause": 6.0}


def map_info() -> dict:
    half = MAP_SIZE / 2
    return {
        "grid_steps": [400.0, 400.0],
        "grid_zero": [-half, half],
        "grid_size": [MAP_SIZE, MAP_SIZE],
        "hud_type": 0,
        "map_generation": 1,
        "map_min": [-half, -half],
        "map_max": [half, half],
        "valid": True,
    }


def map_obj() -> list:
    t = time.time() - START
    px = 0.3 + 0.1 * math.sin(t / 10)
    py = 0.7 + 0.1 * math.cos(t / 10)
    return [
        {"type": "ground_model", "color": "#faC81E", "blink": 0, "icon": "Player",
         "icon_bg": "none", "x": px, "y": py, "dx": 0.0, "dy": -1.0},
        {"type": "ground_model", "color": "#174DFF", "blink": 0, "icon": "MediumTank",
         "icon_bg": "none", "x": 0.32, "y": 0.68},
        {"type": "ground_model", "color": "#fa0C00", "color[]": [250, 12, 0], "blink": 0,
         "icon": "HeavyTank", "icon_bg": "none", "x": 0.55, "y": 0.35},
        {"type": "point_of_interest", "color": "#FFFFFF", "blink": 0, "icon": "none",
         "icon_bg": "none", "x": 0.6, "y": 0.4},
        *[{"type": "respawn_base_tank", "color": "#fa0C00", "color[]": [250, 12, 0], "blink": 0,
           "icon": "respawn_base_tank", "icon_bg": "none", "x": 0.62 + dx, "y": 0.08 + dy}
          for dx, dy in [(0, 0), (0.02, 0), (0.04, 0), (0.01, 0.02), (0.03, 0.02)]],
        *[{"type": "respawn_base_tank", "color": "#174DFF", "color[]": [23, 77, 255], "blink": 0,
           "icon": "respawn_base_tank", "icon_bg": "none", "x": 0.15 + dx, "y": 0.9 + dy}
          for dx, dy in [(0, 0), (0.02, 0), (0.04, 0), (0.01, 0.02), (0.03, 0.02)]],
        {"type": "capture_zone", "color": "#fa0C00", "blink": 0, "icon": "capture_zone",
         "icon_bg": "none", "x": 0.5, "y": 0.5, "zone_label": "A"},
        {"type": "capture_zone", "color": "#174DFF", "blink": 0, "icon": "capture_zone",
         "icon_bg": "none", "x": 0.25, "y": 0.6, "zone_label": "B"},
    ]


# --------------------------------------------------------------------------- #
# Имитация полёта (режим --mode air)
# --------------------------------------------------------------------------- #
def _interp(keys, x):
    """Плавная интерполяция по ключевым точкам [(x, y), ...]."""
    if x <= keys[0][0]:
        return keys[0][1]
    for (x0, y0), (x1, y1) in zip(keys, keys[1:]):
        if x <= x1:
            k = (x - x0) / (x1 - x0)
            k = k * k * (3 - 2 * k)
            return y0 + (y1 - y0) * k
    return keys[-1][1]


ALT_KEYS = [(0, 100), (60, 3000), (150, 2800), (200, 800), (240, 2200)]
TAS_KEYS = [(0, 250), (60, 430), (150, 380), (200, 690), (240, 420)]
KILLS = [(100, "TestPilot (Bf 109 F-4) сбил ✈Spitfire Mk Vb (Enemy1)"),
         (130, "Ally2 (Fw 190 A-4) сбил ✈P-40E (Enemy2)"),
         (190, "TestPilot (Bf 109 F-4) сбил ✈Typhoon Mk Ib (Enemy3)")]


def flight_clock():
    """(время полёта τ, идёт ли полёт)."""
    t = (time.time() - START) * MODE["speed"]
    cycle = MODE["flight"] + MODE["pause"] * MODE["speed"]
    tau = t % cycle
    return tau, tau < MODE["flight"]


def flight_values(tau: float) -> dict:
    h = _interp(ALT_KEYS, tau)
    tas = _interp(TAS_KEYS, tau)
    turning = 60 <= tau <= 150
    w = 2 * math.pi * 3 / 90  # три виража за 90 с, без скачков на границах
    if turning:
        h += 150 * math.sin(w * (tau - 60))
    ny = 1 + (2.5 * (1 - math.cos(w * (tau - 60))) if turning else 0.0)
    dh = _interp(ALT_KEYS, tau + 0.5) - _interp(ALT_KEYS, tau - 0.5)
    vy = dh + (150 * w * math.cos(w * (tau - 60)) if turning else 0.0)
    wep = turning
    throttle = 110.0 if wep else 100.0
    water = 80 + 25 * min(1.0, max(0.0, (tau - 60) / 90)) if tau < 150 else 105 - (tau - 150) * 0.2
    ias = tas * math.exp(-h / 18000)
    return {
        "valid": True, "aileron, %": 0, "elevator, %": round(-5 * (ny - 1), 1), "rudder, %": 0,
        "flaps, %": 0, "gear, %": 100 if tau < 10 else 0,
        "H, m": round(h), "TAS, km/h": round(tas), "IAS, km/h": round(ias),
        "M": round(tas / 3.6 / (340.3 - 0.0041 * h), 2),
        "AoA, deg": round(2 + 1.7 * (ny - 1), 1), "AoS, deg": 0.0, "Ny": round(ny, 2),
        "Vy, m/s": round(vy, 1), "Wx, deg/s": round(40 * math.sin(tau / 3) if turning else 0, 0),
        "Mfuel, kg": round(350 - 0.6 * tau, 1), "Mfuel0, kg": 350,
        "throttle 1, %": throttle, "RPM throttle 1, %": throttle, "mixture 1, %": 100,
        "radiator 1, %": 40, "magneto 1": 3, "power 1, hp": round(1200 + (150 if wep else 0) - h * 0.03),
        "RPM 1": 2700 if not wep else 2800, "manifold pressure 1, atm": 1.42 if wep else 1.3,
        "water temp 1, C": round(water), "oil temp 1, C": round(water - 15),
        "pitch 1, deg": 32.0, "thrust 1, kgs": round(500 - tas * 0.4), "efficiency 1, %": 80,
    }


def state_json() -> dict:
    if MODE["mode"] != "air":
        return {"valid": False}
    tau, flying = flight_clock()
    return flight_values(tau) if flying else {"valid": False}


def indicators_json() -> dict:
    tau, flying = flight_clock()
    if MODE["mode"] != "air" or not flying:
        return {"valid": False}
    v = flight_values(tau)
    return {"valid": True, "type": "bf-109f-4", "speed": v["IAS, km/h"] / 3.6,
            "altitude_hour": v["H, m"], "compass": (tau * 3) % 360}


def air_map_obj() -> list:
    tau, flying = flight_clock()
    objs = map_obj()[1:]
    if flying:
        objs.insert(0, {"type": "aircraft", "color": "#faC81E", "blink": 0, "icon": "Player",
                        "icon_bg": "none", "x": 0.5 + 0.3 * math.sin(tau / 40),
                        "y": 0.5 + 0.25 * math.sin(tau / 23), "dx": 0.0, "dy": -1.0})
    return objs


def hudmsg(query: str) -> dict:
    from urllib.parse import parse_qs

    q = parse_qs(query)
    last = int((q.get("lastDmg") or ["0"])[0] or 0)
    t = (time.time() - START) * MODE["speed"]
    cycle = MODE["flight"] + MODE["pause"] * MODE["speed"]
    n, tau = int(t // cycle), t % cycle
    dmg = []
    for i, (when, msg) in enumerate(KILLS):
        mid = n * 10 + i + 1
        if tau >= when and mid > last:
            dmg.append({"id": mid, "msg": msg, "sender": "", "enemy": False, "mode": "", "time": int(when)})
    return {"events": [], "damage": dmg}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/map_info.json":
            body = map_info()
        elif path == "/map_obj.json":
            body = air_map_obj() if MODE["mode"] == "air" else map_obj()
        elif path == "/state.json":
            body = state_json()
        elif path == "/indicators.json":
            body = indicators_json()
        elif path == "/hudmsg":
            body = hudmsg(query)
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # тихий режим
        pass


def make_server(port: int = 8111) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8111)
    ap.add_argument("--mode", choices=["ground", "air"], default="ground",
                    help="ground — танковый бой (дальномер), air — полёт (самописец)")
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение времени полёта")
    a = ap.parse_args()
    port = a.port
    MODE.update(mode=a.mode, speed=a.speed)
    print(f"Фейковый War Thunder API: http://127.0.0.1:{port}  (Ctrl+C — выход)")
    try:
        make_server(port).serve_forever()
    except KeyboardInterrupt:
        pass
