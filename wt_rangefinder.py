#!/usr/bin/env python3
"""
War Thunder — дальномер по карте.

Программа читает локальное API игры (http://127.0.0.1:8111), которое War Thunder
поднимает во время боя, и считает расстояние от вашей техники до цели:

  * до точки, по которой вы кликнули на карте в окне программы;
  * до метки на карте игры (если игра отдаёт её в map_obj.json);
  * до точек захвата (A, B, C…);
  * до засвеченных врагов.

Только стандартная библиотека Python 3.8+; окно — на tkinter.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_HOST = "http://127.0.0.1:8111"
PLAYER_ICON = "Player"

# Типы объектов map_obj.json, которые точно не являются меткой игрока.
KNOWN_TYPES = {
    "aircraft", "ground_model", "ship", "airfield", "capture_zone",
    "bombing_point", "defending_point",
    "respawn_base_tank", "respawn_base_fighter", "respawn_base_bomber",
    "respawn_base_ucav", "respawn_base_ship", "respawn_base_helicopter",
}
# Типы/иконки, которые считаем меткой на карте («пинг»).
MARKER_TYPES = {"point_of_interest"}
MARKER_ICONS = {"PointOfInterest", "point_of_interest"}
UNIT_TYPES = {"aircraft", "ground_model", "ship"}

RGB = Tuple[int, int, int]


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
    grid_zero_x: Optional[float] = None
    grid_zero_y: Optional[float] = None
    generation: int = 0

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

    def on_map(self) -> bool:
        return 0.0 <= self.x <= 1.0 and 0.0 <= self.y <= 1.0


@dataclass(frozen=True)
class MapObject:
    type: str
    icon: str
    point: Point
    rgb: Optional[RGB]
    side: str  # "player" | "squad" | "ally" | "enemy" | "neutral"
    label: str = ""
    blink: int = 0


@dataclass(frozen=True)
class Target:
    kind: str  # "click" | "marker" | "zone" | "enemy"
    name: str
    point: Point
    distance_m: float
    bearing_deg: float


@dataclass
class Measurement:
    status: str  # "ok" | "no_game" | "no_battle" | "no_player"
    map_info: Optional[MapInfo] = None
    player: Optional[Point] = None
    objects: List[MapObject] = field(default_factory=list)
    click: Optional[Target] = None
    markers: List[Target] = field(default_factory=list)
    zones: List[Target] = field(default_factory=list)
    enemies: List[Target] = field(default_factory=list)

    @property
    def main(self) -> Optional[Target]:
        """Главная цель: клик → метка → ближайший враг."""
        if self.click:
            return self.click
        if self.markers:
            return self.markers[0]
        if self.enemies:
            return self.enemies[0]
        return None


class ApiUnavailable(Exception):
    """Игра не запущена или API на порту 8111 недоступно."""


# --------------------------------------------------------------------------- #
# Разбор JSON
# --------------------------------------------------------------------------- #
def _pair(value: Any) -> Optional[Tuple[float, float]]:
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def parse_map_info(data: Any) -> Optional[MapInfo]:
    """Разбирает map_info.json. Возвращает None, если боя сейчас нет."""
    if not isinstance(data, dict) or not data.get("valid", False):
        return None
    mn, mx = _pair(data.get("map_min")), _pair(data.get("map_max"))
    if mn is None or mx is None or mn[0] == mx[0] or mn[1] == mx[1]:
        return None
    steps = _pair(data.get("grid_steps")) or (None, None)
    zero = _pair(data.get("grid_zero")) or (None, None)
    try:
        gen = int(data.get("map_generation", 0))
    except (TypeError, ValueError):
        gen = 0
    return MapInfo(mn[0], mn[1], mx[0], mx[1], steps[0], steps[1], zero[0], zero[1], gen)


def parse_color(obj: Dict[str, Any]) -> Optional[RGB]:
    arr = obj.get("color[]")
    if isinstance(arr, (list, tuple)) and len(arr) >= 3:
        try:
            return int(arr[0]), int(arr[1]), int(arr[2])
        except (TypeError, ValueError):
            pass
    s = obj.get("color")
    if isinstance(s, str) and s.startswith("#") and len(s) == 7:
        try:
            return int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
        except ValueError:
            pass
    return None


def classify_side(rgb: Optional[RGB], icon: str = "") -> str:
    """Определяет сторону по цвету значка (стандартная палитра игры).

    вы — жёлтый #faC81E, взвод — зелёный #67D756,
    союзники — синие (#174DFF и оттенки), враги — красный #fa0C00.
    """
    if icon == PLAYER_ICON:
        return "player"
    if rgb is None:
        return "neutral"
    r, g, b = rgb
    if r > 150 and g > 150 and b < 100:
        return "player"
    if r > 150 and r > 2 * g and r > 2 * b:
        return "enemy"
    if b > r and b > g:
        return "ally"
    if g > r and g > b:
        return "squad"
    return "neutral"


def parse_objects(data: Any) -> List[MapObject]:
    result: List[MapObject] = []
    if not isinstance(data, list):
        return result
    for obj in data:
        if not isinstance(obj, dict):
            continue
        try:
            point = Point(float(obj["x"]), float(obj["y"]))
        except (KeyError, TypeError, ValueError):
            continue  # аэродромы (sx/sy/ex/ey) и прочее без точки
        icon = str(obj.get("icon", ""))
        rgb = parse_color(obj)
        try:
            blink = int(obj.get("blink", 0))
        except (TypeError, ValueError):
            blink = 0
        result.append(MapObject(
            type=str(obj.get("type", "")), icon=icon, point=point, rgb=rgb,
            side=classify_side(rgb, icon), label=str(obj.get("zone_label", "")), blink=blink,
        ))
    return result


def is_marker(obj: MapObject) -> bool:
    if obj.type in MARKER_TYPES or obj.icon in MARKER_ICONS:
        return True
    # Неизвестный тип объекта на карте — скорее всего это и есть метка.
    return obj.type not in KNOWN_TYPES and obj.point.on_map()


# --------------------------------------------------------------------------- #
# Геометрия
# --------------------------------------------------------------------------- #
def distance_m(a: Point, b: Point, info: MapInfo) -> float:
    """Горизонтальное расстояние между двумя точками карты в метрах."""
    return math.hypot((b.x - a.x) * info.size_x, (b.y - a.y) * info.size_y)


def bearing_deg(a: Point, b: Point, info: MapInfo) -> float:
    """Азимут от a к b: 0° — север (верх карты), 90° — восток, по часовой."""
    dx = (b.x - a.x) * info.size_x
    dy = (b.y - a.y) * info.size_y
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def grid_square(p: Point, info: MapInfo) -> str:
    """Квадрат сетки карты, например «C5» (буквы — строки сверху вниз)."""
    if not (info.grid_step_x and info.grid_step_y):
        return ""
    gx0 = info.grid_zero_x if info.grid_zero_x is not None else info.min_x
    gy0 = info.grid_zero_y if info.grid_zero_y is not None else info.max_y
    wx = info.min_x + p.x * info.size_x
    wy = info.max_y - p.y * info.size_y
    col = int(math.floor((wx - gx0) / info.grid_step_x))
    row = int(math.floor((gy0 - wy) / info.grid_step_y))
    if col < 0 or row < 0 or row >= 26:
        return ""
    return f"{chr(ord('A') + row)}{col + 1}"


def make_target(kind: str, name: str, player: Point, p: Point, info: MapInfo) -> Target:
    return Target(kind, name, p, distance_m(player, p, info), bearing_deg(player, p, info))


# --------------------------------------------------------------------------- #
# Расчёт
# --------------------------------------------------------------------------- #
def measure_from_data(map_info_json: Any, map_obj_json: Any,
                      click: Optional[Point] = None) -> Measurement:
    """Считает дистанции по уже полученным JSON-ответам API."""
    info = parse_map_info(map_info_json)
    if info is None:
        return Measurement("no_battle")

    objects = parse_objects(map_obj_json)
    player_obj = next((o for o in objects if o.icon == PLAYER_ICON), None)
    if player_obj is None:
        return Measurement("no_player", info, None, objects)
    player = player_obj.point

    m = Measurement("ok", info, player, objects)
    if click is not None:
        m.click = make_target("click", "точка на карте", player, click, info)

    for o in objects:
        if o.icon == PLAYER_ICON:
            continue
        if is_marker(o):
            m.markers.append(make_target("marker", "метка", player, o.point, info))
        elif o.type == "capture_zone":
            m.zones.append(make_target("zone", o.label or "?", player, o.point, info))
        elif o.type in UNIT_TYPES and o.side == "enemy" and o.point.on_map():
            m.enemies.append(make_target("enemy", o.icon or o.type, player, o.point, info))

    m.markers.sort(key=lambda t: t.distance_m)
    m.enemies.sort(key=lambda t: t.distance_m)
    m.zones.sort(key=lambda t: t.name)
    return m


# --------------------------------------------------------------------------- #
# Работа с API игры
# --------------------------------------------------------------------------- #
def fetch_raw(host: str, path: str, timeout: float = 1.0) -> bytes:
    url = host.rstrip("/") + "/" + path.lstrip("/")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ApiUnavailable(str(exc)) from exc


def fetch_json(host: str, path: str, timeout: float = 1.0) -> Any:
    raw = fetch_raw(host, path, timeout)
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def measure(host: str = DEFAULT_HOST, timeout: float = 1.0,
            click: Optional[Point] = None) -> Measurement:
    try:
        info = fetch_json(host, "map_info.json", timeout)
        objs = fetch_json(host, "map_obj.json", timeout)
    except ApiUnavailable:
        return Measurement("no_game")
    return measure_from_data(info, objs, click)


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #
STATUS_TEXT = {
    "no_game": "Игра не найдена (порт 8111). Запустите War Thunder.",
    "no_battle": "Ожидание боя…",
    "no_player": "Ваша техника не видна на карте (выбор техники / уничтожен)",
}

ICON_NAMES = {
    "MediumTank": "СТ", "HeavyTank": "ТТ", "LightTank": "ЛТ", "TankDestroyer": "ПТ-САУ",
    "SPAA": "ЗСУ", "IFV": "БМП", "Fighter": "истребитель", "Assault": "штурмовик",
    "Bomber": "бомбардировщик", "Helicopter": "вертолёт",
}


def format_distance(meters: float) -> str:
    if meters >= 10_000:
        return f"{meters / 1000:.1f} км"
    return f"{meters:.0f} м"


def compass_point(bearing: float) -> str:
    names = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
    return names[int((bearing + 22.5) // 45) % 8]


def format_target(t: Target) -> str:
    return f"{format_distance(t.distance_m)}, азимут {t.bearing_deg:.0f}° ({compass_point(t.bearing_deg)})"


def describe(m: Measurement) -> List[str]:
    """Строки для вывода: главная цель первой."""
    if m.status != "ok":
        return [STATUS_TEXT.get(m.status, m.status)]
    lines: List[str] = []
    if m.click:
        lines.append(f"Точка на карте: {format_target(m.click)}")
    for t in m.markers[:3]:
        lines.append(f"Метка: {format_target(t)}")
    for t in m.enemies[:3]:
        lines.append(f"Враг ({ICON_NAMES.get(t.name, t.name)}): {format_target(t)}")
    if m.zones:
        lines.append("Точки: " + "  ".join(f"{z.name} {format_distance(z.distance_m)}" for z in m.zones))
    if not (m.click or m.markers or m.enemies):
        lines.insert(0, "Цель не указана: кликните по карте в окне программы "
                        "или поставьте метку в игре")
    return lines


# --------------------------------------------------------------------------- #
# Консоль
# --------------------------------------------------------------------------- #
def run_console(host: str, interval: float, once: bool) -> int:
    import time

    last = None
    try:
        while True:
            text = "\n".join(describe(measure(host)))
            if once:
                print(text)
                return 0
            if text != last:
                print(text + "\n", flush=True)
                last = text
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------- #
# Окно с картой
# --------------------------------------------------------------------------- #
SIDE_COLORS = {
    "player": "#FAC81E", "squad": "#67D756", "ally": "#4D7CFF",
    "enemy": "#FF3B30", "neutral": "#BBBBBB",
}


def load_map_image(tk: Any, host: str, size: int) -> Any:
    """Пытается загрузить картинку карты (map.img). Нужен Pillow для JPEG."""
    try:
        raw = fetch_raw(host, "map.img", timeout=2.0)
    except ApiUnavailable:
        return None
    if not raw:
        return None
    try:
        from io import BytesIO

        from PIL import Image, ImageTk  # type: ignore

        img = Image.open(BytesIO(raw)).convert("RGB").resize((size, size))
        return ImageTk.PhotoImage(img)
    except Exception:  # Pillow не установлен или формат не поддерживается
        pass
    try:  # Tk сам умеет PNG/GIF
        img = tk.PhotoImage(data=base64.b64encode(raw).decode("ascii"))
        factor = max(1, round(img.width() / size))
        return img.subsample(factor) if factor > 1 else img
    except Exception:
        return None


class RangefinderApp:
    def __init__(self, host: str, interval: float, alpha: float, map_size: int) -> None:
        import tkinter as tk

        self.tk = tk
        self.host = host
        self.interval_ms = int(interval * 1000)
        self.size = map_size
        self.click: Optional[Point] = None
        self.map_key: Optional[Tuple[float, ...]] = None
        self.bg_image: Any = None

        self.root = tk.Tk()
        self.root.title("WT Дальномер")
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            pass
        self.root.configure(bg="#111111")

        self.dist_var = tk.StringVar(value="—")
        self.info_var = tk.StringVar(value="Запуск…")
        tk.Label(self.root, textvariable=self.dist_var, font=("Consolas", 30, "bold"),
                 fg="#7CFC00", bg="#111111").pack(fill="x")
        tk.Label(self.root, textvariable=self.info_var, font=("Segoe UI", 10),
                 fg="#DDDDDD", bg="#111111", justify="left", anchor="w",
                 padx=8, pady=4).pack(fill="x")

        self.canvas = tk.Canvas(self.root, width=self.size, height=self.size,
                                bg="#1E2A1E", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=6, pady=6)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Button-3>", self.on_clear)
        tk.Label(self.root, text="ЛКМ — указать цель,  ПКМ — сбросить,  Ctrl+M — скрыть карту",
                 font=("Segoe UI", 9), fg="#888888", bg="#111111").pack(pady=(0, 6))
        self.map_visible = True
        self.root.bind("<Control-m>", self.toggle_map)

    # --- события ---------------------------------------------------------- #
    def on_click(self, event: Any) -> None:
        self.click = Point(event.x / self.size, event.y / self.size)
        self.refresh(reschedule=False)

    def on_clear(self, _event: Any = None) -> None:
        self.click = None
        self.refresh(reschedule=False)

    def toggle_map(self, _event: Any = None) -> None:
        if self.map_visible:
            self.canvas.pack_forget()
        else:
            self.canvas.pack(padx=6, pady=6)
        self.map_visible = not self.map_visible

    # --- отрисовка -------------------------------------------------------- #
    def _xy(self, p: Point) -> Tuple[float, float]:
        return p.x * self.size, p.y * self.size

    def draw(self, m: Measurement) -> None:
        c = self.canvas
        c.delete("all")
        info = m.map_info
        if info is None:
            return
        key = (info.min_x, info.min_y, info.max_x, info.max_y, info.generation)
        if key != self.map_key:  # новая карта — сброс цели и подложки
            self.map_key = key
            self.click = None
            m.click = None
            self.bg_image = load_map_image(self.tk, self.host, self.size)
        if self.bg_image is not None:
            c.create_image(0, 0, image=self.bg_image, anchor="nw")
        self._draw_grid(info)

        for o in m.objects:
            if not o.point.on_map() or o.type.startswith("respawn_base"):
                continue
            x, y = self._xy(o.point)
            color = SIDE_COLORS.get(o.side, "#BBBBBB")
            if o.type == "capture_zone":
                c.create_oval(x - 9, y - 9, x + 9, y + 9, outline=color, width=2)
                c.create_text(x, y, text=o.label, fill=color, font=("Segoe UI", 9, "bold"))
            elif is_marker(o):
                c.create_text(x, y, text="✚", fill="#FFFFFF", font=("Segoe UI", 12, "bold"))
            elif o.icon == PLAYER_ICON:
                continue
            else:
                c.create_rectangle(x - 3, y - 3, x + 3, y + 3, fill=color, outline="")

        if m.player:
            px, py = self._xy(m.player)
            target = m.main
            if target:
                tx, ty = self._xy(target.point)
                c.create_line(px, py, tx, ty, fill="#7CFC00", dash=(4, 3), width=2)
                c.create_text((px + tx) / 2, (py + ty) / 2 - 10,
                              text=format_distance(target.distance_m),
                              fill="#7CFC00", font=("Consolas", 11, "bold"))
            c.create_oval(px - 5, py - 5, px + 5, py + 5, fill=SIDE_COLORS["player"], outline="black")
        if m.click:
            x, y = self._xy(m.click.point)
            c.create_line(x - 8, y, x + 8, y, fill="#FF00FF", width=2)
            c.create_line(x, y - 8, x, y + 8, fill="#FF00FF", width=2)

    def _draw_grid(self, info: MapInfo) -> None:
        if not (info.grid_step_x and info.grid_step_y):
            return
        c = self.canvas
        gx0 = info.grid_zero_x if info.grid_zero_x is not None else info.min_x
        gy0 = info.grid_zero_y if info.grid_zero_y is not None else info.max_y
        for i in range(0, 60):
            wx = gx0 + i * info.grid_step_x
            nx = (wx - info.min_x) / info.size_x
            if nx > 1:
                break
            if nx >= 0:
                c.create_line(nx * self.size, 0, nx * self.size, self.size, fill="#3A4A3A")
                c.create_text(nx * self.size + 3, 2, text=str(i + 1), anchor="nw",
                              fill="#8A9A8A", font=("Segoe UI", 7))
        for j in range(0, 26):
            wy = gy0 - j * info.grid_step_y
            ny = (info.max_y - wy) / info.size_y
            if ny > 1:
                break
            if ny >= 0:
                c.create_line(0, ny * self.size, self.size, ny * self.size, fill="#3A4A3A")
                c.create_text(2, ny * self.size + 12, text=chr(ord("A") + j), anchor="nw",
                              fill="#8A9A8A", font=("Segoe UI", 7))

    def refresh(self, reschedule: bool = True) -> None:
        m = measure(self.host, timeout=min(1.0, self.interval_ms / 1000), click=self.click)
        if m.map_info is not None:
            self.draw(m)
        target = m.main
        self.dist_var.set(format_distance(target.distance_m) if target else "—")
        lines = describe(m)
        if target and m.map_info:
            sq = grid_square(target.point, m.map_info)
            if sq:
                lines[0] += f", квадрат {sq}"
        self.info_var.set("\n".join(lines))
        if reschedule:
            self.root.after(self.interval_ms, self.refresh)

    def run(self) -> None:
        self.refresh()
        self.root.mainloop()


def run_gui(host: str, interval: float, alpha: float, map_size: int) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter недоступен — запускаю консольный режим", file=sys.stderr)
        return run_console(host, interval, once=False)
    RangefinderApp(host, interval, alpha, map_size).run()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Дальномер для War Thunder: дистанция от вашей техники до цели на карте."
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"адрес локального API игры (по умолчанию {DEFAULT_HOST})")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="период обновления, сек (по умолчанию 0.5)")
    parser.add_argument("--console", action="store_true", help="вывод в консоль вместо окна")
    parser.add_argument("--once", action="store_true", help="один замер и выход (консоль)")
    parser.add_argument("--alpha", type=float, default=0.9,
                        help="прозрачность окна 0.2–1.0 (по умолчанию 0.9)")
    parser.add_argument("--map-size", type=int, default=420,
                        help="размер карты в окне, пикселей (по умолчанию 420)")
    args = parser.parse_args(argv)

    interval = max(0.1, args.interval)
    if args.console or args.once:
        return run_console(args.host, interval, args.once)
    return run_gui(args.host, interval, min(1.0, max(0.2, args.alpha)),
                   max(200, args.map_size))


if __name__ == "__main__":
    sys.exit(main())
