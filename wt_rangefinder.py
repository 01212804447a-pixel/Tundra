#!/usr/bin/env python3
"""
War Thunder — дальномер по карте.

Программа читает локальное API игры (http://127.0.0.1:8111), которое War Thunder
поднимает во время боя, находит на мини-карте вашу технику и поставленную вами
метку (пинг на карте) и считает расстояние до неё в метрах.

Как пользоваться в бою:
    1. Запустите программу (python wt_rangefinder.py).
    2. Откройте карту (по умолчанию M) и поставьте метку на цель
       (клик по карте / клавиша «отметить на карте»).
    3. Программа покажет дистанцию и азимут до метки.

Только стандартная библиотека Python 3.8+; окно — на tkinter.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_HOST = "http://127.0.0.1:8111"

# Типы/иконки объектов в map_obj.json, которые считаются «целью» (меткой на карте).
TARGET_TYPES = {"point_of_interest"}
TARGET_ICONS = {"PointOfInterest", "point_of_interest"}
PLAYER_ICON = "Player"


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MapInfo:
    """Размеры карты в метрах (из map_info.json)."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float
    grid_step_x: Optional[float] = None
    grid_step_y: Optional[float] = None

    @property
    def size_x(self) -> float:
        return abs(self.max_x - self.min_x)

    @property
    def size_y(self) -> float:
        return abs(self.max_y - self.min_y)


@dataclass(frozen=True)
class Point:
    """Точка на карте в нормализованных координатах (0..1, y растёт вниз)."""

    x: float
    y: float


@dataclass(frozen=True)
class Target:
    point: Point
    distance_m: float
    bearing_deg: float
    color: str = ""


@dataclass(frozen=True)
class Measurement:
    player: Optional[Point]
    targets: List[Target]
    map_info: Optional[MapInfo]
    status: str  # "ok", "no_battle", "no_player", "no_target", "no_game"

    @property
    def nearest(self) -> Optional[Target]:
        return self.targets[0] if self.targets else None


class ApiUnavailable(Exception):
    """Игра не запущена или API на порту 8111 недоступно."""


# --------------------------------------------------------------------------- #
# Чистые функции (легко тестировать)
# --------------------------------------------------------------------------- #
def parse_map_info(data: Dict[str, Any]) -> Optional[MapInfo]:
    """Разбирает map_info.json. Возвращает None, если боя сейчас нет."""
    if not isinstance(data, dict) or not data.get("valid", False):
        return None
    try:
        min_x, min_y = (float(v) for v in data["map_min"])
        max_x, max_y = (float(v) for v in data["map_max"])
    except (KeyError, TypeError, ValueError):
        return None
    if max_x == min_x or max_y == min_y:
        return None
    step_x = step_y = None
    steps = data.get("grid_steps")
    if isinstance(steps, (list, tuple)) and len(steps) >= 2:
        try:
            step_x, step_y = float(steps[0]), float(steps[1])
        except (TypeError, ValueError):
            step_x = step_y = None
    return MapInfo(min_x, min_y, max_x, max_y, step_x, step_y)


def _point_of(obj: Dict[str, Any]) -> Optional[Point]:
    try:
        return Point(float(obj["x"]), float(obj["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def find_player(objects: Iterable[Dict[str, Any]]) -> Optional[Point]:
    """Ищет собственную технику игрока (icon == "Player")."""
    for obj in objects:
        if isinstance(obj, dict) and obj.get("icon") == PLAYER_ICON:
            point = _point_of(obj)
            if point is not None:
                return point
    return None


def is_target(obj: Dict[str, Any]) -> bool:
    return obj.get("type") in TARGET_TYPES or obj.get("icon") in TARGET_ICONS


def find_target_objects(objects: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Все метки на карте, которые могут быть целью."""
    return [o for o in objects if isinstance(o, dict) and is_target(o) and _point_of(o)]


def distance_m(a: Point, b: Point, info: MapInfo) -> float:
    """Горизонтальное расстояние между двумя точками карты в метрах."""
    dx = (b.x - a.x) * info.size_x
    dy = (b.y - a.y) * info.size_y
    return math.hypot(dx, dy)


def bearing_deg(a: Point, b: Point, info: MapInfo) -> float:
    """Азимут от a к b: 0° — север (верх карты), 90° — восток, по часовой."""
    dx = (b.x - a.x) * info.size_x
    dy = (b.y - a.y) * info.size_y
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def measure_from_data(map_info_json: Any, map_obj_json: Any) -> Measurement:
    """Считает дистанции по уже полученным JSON-ответам API."""
    info = parse_map_info(map_info_json)
    if info is None:
        return Measurement(None, [], None, "no_battle")

    objects: Sequence[Dict[str, Any]] = map_obj_json if isinstance(map_obj_json, list) else []
    player = find_player(objects)
    if player is None:
        return Measurement(None, [], info, "no_player")

    targets = []
    for obj in find_target_objects(objects):
        p = _point_of(obj)
        assert p is not None
        targets.append(
            Target(
                point=p,
                distance_m=distance_m(player, p, info),
                bearing_deg=bearing_deg(player, p, info),
                color=str(obj.get("color", "")),
            )
        )
    targets.sort(key=lambda t: t.distance_m)
    return Measurement(player, targets, info, "ok" if targets else "no_target")


# --------------------------------------------------------------------------- #
# Работа с API игры
# --------------------------------------------------------------------------- #
def fetch_json(host: str, path: str, timeout: float = 1.0) -> Any:
    url = host.rstrip("/") + "/" + path.lstrip("/")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ApiUnavailable(str(exc)) from exc
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def measure(host: str = DEFAULT_HOST, timeout: float = 1.0) -> Measurement:
    try:
        info = fetch_json(host, "map_info.json", timeout)
        objs = fetch_json(host, "map_obj.json", timeout)
    except ApiUnavailable:
        return Measurement(None, [], None, "no_game")
    return measure_from_data(info, objs)


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #
STATUS_TEXT = {
    "no_game": "Игра не найдена (порт 8111). Запустите War Thunder.",
    "no_battle": "Ожидание боя…",
    "no_player": "Ваша техника не видна на карте",
    "no_target": "Поставьте метку на карте (M → клик по цели)",
}


def format_distance(meters: float) -> str:
    if meters >= 10_000:
        return f"{meters / 1000:.1f} км"
    return f"{meters:.0f} м"


def compass_point(bearing: float) -> str:
    names = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
    return names[int((bearing + 22.5) // 45) % 8]


def format_measurement(m: Measurement) -> str:
    if m.status != "ok" or m.nearest is None:
        return STATUS_TEXT.get(m.status, m.status)
    t = m.nearest
    text = f"{format_distance(t.distance_m)}  |  азимут {t.bearing_deg:.0f}° ({compass_point(t.bearing_deg)})"
    if len(m.targets) > 1:
        others = ", ".join(format_distance(o.distance_m) for o in m.targets[1:4])
        text += f"  |  ещё меток: {len(m.targets) - 1} ({others})"
    return text


# --------------------------------------------------------------------------- #
# Интерфейсы: консоль и окно поверх игры
# --------------------------------------------------------------------------- #
def run_console(host: str, interval: float, once: bool) -> int:
    import time

    last = None
    try:
        while True:
            line = format_measurement(measure(host))
            if once:
                print(line)
                return 0
            if line != last:
                print(line, flush=True)
                last = line
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def run_gui(host: str, interval: float, alpha: float) -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter недоступен — запускаю консольный режим", file=sys.stderr)
        return run_console(host, interval, once=False)

    root = tk.Tk()
    root.title("WT Дальномер")
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", alpha)
    except tk.TclError:
        pass
    root.configure(bg="#111111")
    root.resizable(False, False)

    dist_var = tk.StringVar(value="—")
    info_var = tk.StringVar(value="Запуск…")

    tk.Label(root, textvariable=dist_var, font=("Consolas", 32, "bold"),
             fg="#7CFC00", bg="#111111", padx=16).pack()
    tk.Label(root, textvariable=info_var, font=("Segoe UI", 11),
             fg="#DDDDDD", bg="#111111", padx=16, pady=6, justify="center").pack()

    def tick() -> None:
        m = measure(host, timeout=min(1.0, interval))
        if m.status == "ok" and m.nearest is not None:
            t = m.nearest
            dist_var.set(format_distance(t.distance_m))
            extra = f"азимут {t.bearing_deg:.0f}° ({compass_point(t.bearing_deg)})"
            if len(m.targets) > 1:
                extra += f"\nещё меток: {len(m.targets) - 1}"
            info_var.set(extra)
        else:
            dist_var.set("—")
            info_var.set(STATUS_TEXT.get(m.status, m.status))
        root.after(int(interval * 1000), tick)

    tick()
    root.mainloop()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Дальномер для War Thunder: дистанция от вашей техники до метки на карте."
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"адрес локального API игры (по умолчанию {DEFAULT_HOST})")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="период обновления, сек (по умолчанию 0.5)")
    parser.add_argument("--console", action="store_true", help="вывод в консоль вместо окна")
    parser.add_argument("--once", action="store_true", help="один замер и выход (консоль)")
    parser.add_argument("--alpha", type=float, default=0.85,
                        help="прозрачность окна 0.2–1.0 (по умолчанию 0.85)")
    args = parser.parse_args(argv)

    interval = max(0.1, args.interval)
    if args.console or args.once:
        return run_console(args.host, interval, args.once)
    return run_gui(args.host, interval, min(1.0, max(0.2, args.alpha)))


if __name__ == "__main__":
    sys.exit(main())
