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
    enemy_spawns: List[Target] = field(default_factory=list)

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


def is_spawn(obj: MapObject) -> bool:
    return obj.type.startswith("respawn_base")


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
        elif is_spawn(o) and o.side == "enemy" and o.point.on_map():
            m.enemy_spawns.append(make_target("spawn", o.type, player, o.point, info))

    m.markers.sort(key=lambda t: t.distance_m)
    m.enemies.sort(key=lambda t: t.distance_m)
    m.enemy_spawns.sort(key=lambda t: t.distance_m)
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
    if m.enemy_spawns:
        lines.append(f"Спавн врага: {format_target(m.enemy_spawns[0])}")
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


def fetch_map_image(host: str) -> Optional[bytes]:
    """Картинка карты (map.img) из игры, или None."""
    try:
        raw = fetch_raw(host, "map.img", timeout=2.0)
    except ApiUnavailable:
        return None
    return raw or None


def make_photo(tk: Any, raw: bytes, size: int) -> Any:
    """Масштабирует картинку карты под размер. Для JPEG нужен Pillow."""
    try:
        from io import BytesIO

        from PIL import Image, ImageTk  # type: ignore

        img = Image.open(BytesIO(raw)).convert("RGB").resize((size, size))
        return ImageTk.PhotoImage(img)
    except Exception:  # Pillow не установлен или формат не поддерживается
        pass
    try:  # Tk сам умеет PNG/GIF, но масштабирует только в целое число раз
        img = tk.PhotoImage(data=base64.b64encode(raw).decode("ascii"))
        if img.width() > size:
            return img.subsample(max(1, round(img.width() / size)))
        if img.width() < size:
            return img.zoom(max(1, size // img.width()))
        return img
    except Exception:
        return None


class RangefinderApp:
    BG = "#111111"

    def __init__(self, host: str, interval: float, alpha: float, map_size: int,
                 fullscreen: bool = False) -> None:
        import tkinter as tk

        self.tk = tk
        self.host = host
        self.interval_ms = int(interval * 1000)
        self.size = map_size        # текущий размер квадрата карты, px
        self.ox = self.oy = 0       # отступ карты внутри холста (центрирование)
        self.click: Optional[Point] = None
        self.map_key: Optional[Tuple[float, ...]] = None
        self.map_raw: Optional[bytes] = None
        self.bg_cache: Dict[int, Any] = {}
        self.last: Optional[Measurement] = None
        self.redraw_job: Optional[str] = None
        self.fullscreen = False

        self.root = tk.Tk()
        self.root.title("WT Дальномер")
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            pass
        self.root.configure(bg=self.BG)
        self.root.minsize(260, 300)

        top = tk.Frame(self.root, bg=self.BG)
        top.pack(fill="x")
        self.dist_var = tk.StringVar(value="—")
        self.info_var = tk.StringVar(value="Запуск…")
        tk.Label(top, textvariable=self.dist_var, font=("Consolas", 30, "bold"),
                 fg="#7CFC00", bg=self.BG, padx=8).pack(side="left")
        self.full_btn = tk.Button(top, text="⛶ Весь экран", command=self.toggle_fullscreen,
                                  bg="#2A2A2A", fg="#DDDDDD", activebackground="#3A3A3A",
                                  activeforeground="#FFFFFF", relief="flat", padx=10)
        self.full_btn.pack(side="right", padx=6, pady=6)
        tk.Label(self.root, textvariable=self.info_var, font=("Segoe UI", 10),
                 fg="#DDDDDD", bg=self.BG, justify="left", anchor="w",
                 padx=8, pady=2).pack(fill="x")
        tk.Label(self.root, text="ЛКМ — цель   ПКМ — сброс   F11 — весь экран   "
                                 "Esc — выйти из полноэкранного   Ctrl+M — скрыть карту",
                 font=("Segoe UI", 9), fg="#888888", bg=self.BG).pack(side="bottom", pady=(0, 4))

        self.canvas = tk.Canvas(self.root, width=map_size, height=map_size,
                                bg=self.BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Button-3>", self.on_clear)
        self.canvas.bind("<Configure>", self.on_resize)
        self.map_visible = True
        self.root.bind("<Control-m>", self.toggle_map)
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", lambda _e: self.set_fullscreen(False))
        if fullscreen:
            self.root.after(100, lambda: self.set_fullscreen(True))

    # --- события ---------------------------------------------------------- #
    def on_click(self, event: Any) -> None:
        p = Point((event.x - self.ox) / self.size, (event.y - self.oy) / self.size)
        if p.on_map():
            self.click = p
            self.refresh(reschedule=False)

    def on_clear(self, _event: Any = None) -> None:
        self.click = None
        self.refresh(reschedule=False)

    def on_resize(self, event: Any) -> None:
        side = max(100, min(event.width, event.height))
        self.size = side
        self.ox = (event.width - side) // 2
        self.oy = (event.height - side) // 2
        if self.redraw_job:
            self.root.after_cancel(self.redraw_job)
        self.redraw_job = self.root.after(60, self._redraw_last)

    def _redraw_last(self) -> None:
        self.redraw_job = None
        if self.last is not None and self.last.map_info is not None:
            self.draw(self.last)

    def toggle_map(self, _event: Any = None) -> None:
        if self.map_visible:
            self.canvas.pack_forget()
        else:
            self.canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.map_visible = not self.map_visible

    def set_fullscreen(self, on: bool) -> None:
        if on == self.fullscreen:
            return
        if on:
            self.saved_geometry = self.root.geometry()
        self.fullscreen = on
        try:
            self.root.attributes("-fullscreen", on)
        except self.tk.TclError:
            pass
        if not on and getattr(self, "saved_geometry", None):
            self.root.geometry(self.saved_geometry)
        self.full_btn.configure(text="✕ Свернуть" if on else "⛶ Весь экран")
        if on:
            self.root.after(250, self._ensure_fullscreen)

    def _ensure_fullscreen(self) -> None:
        """Если оконный менеджер не развернул окно — растягиваем вручную."""
        if not self.fullscreen:
            return
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if self.root.winfo_width() < sw * 0.9 or self.root.winfo_height() < sh * 0.9:
            self.root.geometry(f"{sw}x{sh}+0+0")

    def toggle_fullscreen(self, _event: Any = None) -> None:
        self.set_fullscreen(not self.fullscreen)

    # --- отрисовка -------------------------------------------------------- #
    def _xy(self, p: Point) -> Tuple[float, float]:
        return self.ox + p.x * self.size, self.oy + p.y * self.size

    def _background(self) -> Any:
        if self.map_raw is None:
            return None
        if self.size not in self.bg_cache:
            if len(self.bg_cache) > 4:
                self.bg_cache.clear()
            self.bg_cache[self.size] = make_photo(self.tk, self.map_raw, self.size)
        return self.bg_cache[self.size]

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
            self.map_raw = fetch_map_image(self.host)
            self.bg_cache.clear()

        k = max(1.0, self.size / 420)  # масштаб значков
        c.create_rectangle(self.ox, self.oy, self.ox + self.size, self.oy + self.size,
                           fill="#1E2A1E", outline="")
        bg = self._background()
        if bg is not None:
            c.create_image(self.ox, self.oy, image=bg, anchor="nw")
        self._draw_grid(info, k)

        seen = set()
        for o in m.objects:
            if not o.point.on_map() or o.icon == PLAYER_ICON:
                continue
            x, y = self._xy(o.point)
            color = SIDE_COLORS.get(o.side, "#BBBBBB")
            if is_spawn(o):
                dedup = (round(o.point.x, 4), round(o.point.y, 4), o.side)
                if dedup in seen:
                    continue
                seen.add(dedup)
                r = 4 * k
                if o.type == "respawn_base_tank":
                    c.create_polygon(x, y - r, x + r, y + r * 0.8, x - r, y + r * 0.8,
                                     outline=color, fill="", width=max(1, int(k)))
                else:  # авиа-/прочие спавны, если они внутри карты
                    c.create_polygon(x, y - r, x + r, y, x, y + r, x - r, y,
                                     outline=color, fill="", width=max(1, int(k)))
            elif o.type == "capture_zone":
                r = 9 * k
                c.create_oval(x - r, y - r, x + r, y + r, outline=color, width=2)
                c.create_text(x, y, text=o.label, fill=color,
                              font=("Segoe UI", int(9 * k), "bold"))
            elif is_marker(o):
                c.create_text(x, y, text="✚", fill="#FFFFFF", font=("Segoe UI", int(12 * k), "bold"))
            else:
                r = 3 * k
                c.create_rectangle(x - r, y - r, x + r, y + r, fill=color, outline="black")

        if m.player:
            px, py = self._xy(m.player)
            target = m.main
            if target:
                tx, ty = self._xy(target.point)
                c.create_line(px, py, tx, ty, fill="#7CFC00", dash=(4, 3), width=2)
                c.create_text((px + tx) / 2, (py + ty) / 2 - 10 * k,
                              text=format_distance(target.distance_m),
                              fill="#7CFC00", font=("Consolas", int(11 * k), "bold"))
            r = 5 * k
            c.create_oval(px - r, py - r, px + r, py + r, fill=SIDE_COLORS["player"], outline="black")
        if m.click:
            x, y = self._xy(m.click.point)
            r = 8 * k
            c.create_line(x - r, y, x + r, y, fill="#FF00FF", width=2)
            c.create_line(x, y - r, x, y + r, fill="#FF00FF", width=2)

    def _draw_grid(self, info: MapInfo, k: float) -> None:
        if not (info.grid_step_x and info.grid_step_y):
            return
        c, s, ox, oy = self.canvas, self.size, self.ox, self.oy
        font = ("Segoe UI", max(7, int(7 * k)))
        gx0 = info.grid_zero_x if info.grid_zero_x is not None else info.min_x
        gy0 = info.grid_zero_y if info.grid_zero_y is not None else info.max_y
        for i in range(0, 60):
            nx = (gx0 + i * info.grid_step_x - info.min_x) / info.size_x
            if nx > 1:
                break
            if nx >= 0:
                c.create_line(ox + nx * s, oy, ox + nx * s, oy + s, fill="#3A4A3A")
                c.create_text(ox + nx * s + 3, oy + 2, text=str(i + 1), anchor="nw",
                              fill="#8A9A8A", font=font)
        for j in range(0, 26):
            ny = (info.max_y - (gy0 - j * info.grid_step_y)) / info.size_y
            if ny > 1:
                break
            if ny >= 0:
                c.create_line(ox, oy + ny * s, ox + s, oy + ny * s, fill="#3A4A3A")
                c.create_text(ox + 2, oy + ny * s + 12 * k, text=chr(ord("A") + j), anchor="nw",
                              fill="#8A9A8A", font=font)

    def refresh(self, reschedule: bool = True) -> None:
        m = measure(self.host, timeout=min(1.0, self.interval_ms / 1000), click=self.click)
        self.last = m
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


def run_gui(host: str, interval: float, alpha: float, map_size: int,
            fullscreen: bool = False) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter недоступен — запускаю консольный режим", file=sys.stderr)
        return run_console(host, interval, once=False)
    RangefinderApp(host, interval, alpha, map_size, fullscreen).run()
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
    parser.add_argument("--map-size", type=int, default=600,
                        help="начальный размер карты в окне, пикселей (по умолчанию 600)")
    parser.add_argument("--fullscreen", action="store_true",
                        help="сразу открыть на весь экран (F11 — переключить)")
    args = parser.parse_args(argv)

    interval = max(0.1, args.interval)
    if args.console or args.once:
        return run_console(args.host, interval, args.once)
    return run_gui(args.host, interval, min(1.0, max(0.2, args.alpha)),
                   max(200, args.map_size), args.fullscreen)


if __name__ == "__main__":
    sys.exit(main())
