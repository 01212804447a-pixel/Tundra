#!/usr/bin/env python3
"""
Имитация локального API War Thunder (порт 8111) для проверки дальномера без игры.

    python fake_wt_server.py            # карта 4096 x 4096 м
    python wt_rangefinder.py            # в другом окне

Игрок медленно едет по карте, метка стоит на месте.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAP_SIZE = 4096.0
START = time.time()


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
        {"type": "point_of_interest", "color": "#FFFFFF", "blink": 0, "icon": "none",
         "icon_bg": "none", "x": 0.6, "y": 0.4},
        {"type": "capture_zone", "color": "#FAC81E", "blink": 0, "icon": "capture_zone",
         "icon_bg": "none", "x": 0.5, "y": 0.5},
    ]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/map_info.json"):
            body = map_info()
        elif self.path.startswith("/map_obj.json"):
            body = map_obj()
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
    port = ap.parse_args().port
    print(f"Фейковый War Thunder API: http://127.0.0.1:{port}  (Ctrl+C — выход)")
    try:
        make_server(port).serve_forever()
    except KeyboardInterrupt:
        pass
