#!/usr/bin/env python3
"""
Отчёт о полёте War Thunder: статистика и графики в одном HTML-файле.

    python flight_report.py flights/2026-11-08_20-15-03_bf-109f-4   # пересобрать отчёт
    python flight_report.py --index flights                          # список всех полётов

Данные пишет flight_recorder.py. Отчёт открывается в любом браузере
и работает без интернета (графики рисуются встроенным скриптом).
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

G = 9.80665

# Колонки state.json (самолёты/вертолёты). Для каждой величины — варианты имени.
FIELDS: Dict[str, Tuple[str, ...]] = {
    "alt": ("H, m",),
    "tas": ("TAS, km/h",),
    "ias": ("IAS, km/h",),
    "mach": ("M",),
    "aoa": ("AoA, deg",),
    "aos": ("AoS, deg",),
    "ny": ("Ny",),
    "vy": ("Vy, m/s",),
    "roll_rate": ("Wx, deg/s",),
    "fuel": ("Mfuel, kg",),
    "fuel_max": ("Mfuel0, kg",),
    "throttle": ("throttle 1, %",),
    "rpm": ("RPM 1",),
    "water": ("water temp 1, C",),
    "oil": ("oil temp 1, C",),
    "manifold": ("manifold pressure 1, atm",),
    "power": ("power 1, hp",),
    "thrust": ("thrust 1, kgs",),
    "flaps": ("flaps, %",),
    "gear": ("gear, %",),
}

ALT_BANDS = [(0, 1000), (1000, 3000), (3000, 5000), (5000, 7000), (7000, 99999)]


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #
@dataclass
class Flight:
    t: List[float]
    cols: Dict[str, List[Optional[float]]]   # все числовые колонки
    meta: Dict[str, Any]
    events: List[Dict[str, Any]]

    def series(self, key: str) -> Optional[List[Optional[float]]]:
        """Колонка по логическому имени (alt, tas…) или по точному имени."""
        for name in FIELDS.get(key, (key,)):
            if name in self.cols and any(v is not None for v in self.cols[name]):
                return self.cols[name]
        return None


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def flight_from_rows(rows: Sequence[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None,
                     events: Optional[List[Dict[str, Any]]] = None) -> Flight:
    keys: List[str] = []
    for r in rows:
        for k in r:
            if k not in keys and k not in ("t", "aircraft"):
                keys.append(k)
    t = [_num(r.get("t")) or 0.0 for r in rows]
    cols = {k: [_num(r.get(k)) for r in rows] for k in keys}
    cols = {k: v for k, v in cols.items() if any(x is not None for x in v)}
    return Flight(t, cols, dict(meta or {}), list(events or []))


def load_flight(folder: str) -> Flight:
    with open(os.path.join(folder, "data.csv"), encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    meta: Dict[str, Any] = {}
    events: List[Dict[str, Any]] = []
    try:
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(folder, "events.json"), encoding="utf-8") as f:
            events = json.load(f)
    except (OSError, ValueError):
        pass
    return flight_from_rows(rows, meta, events)


# --------------------------------------------------------------------------- #
# Расчёты
# --------------------------------------------------------------------------- #
def _vals(s: Optional[Sequence[Optional[float]]]) -> List[float]:
    return [v for v in s if v is not None] if s else []


def energy_height(alt: Sequence[Optional[float]], tas: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Энергетическая высота He = H + V²/2g (V — истинная скорость)."""
    out: List[Optional[float]] = []
    for h, v in zip(alt, tas):
        out.append(None if h is None or v is None else h + (v / 3.6) ** 2 / (2 * G))
    return out


def derivative(t: Sequence[float], y: Sequence[Optional[float]], window_s: float = 2.0) -> List[Optional[float]]:
    """Сглаженная производная dy/dt по окну ±window_s/2."""
    n = len(t)
    out: List[Optional[float]] = [None] * n
    j0 = j1 = 0
    for i in range(n):
        while j0 < i and t[i] - t[j0] > window_s / 2:
            j0 += 1
        while j1 < n - 1 and t[j1 + 1] - t[i] <= window_s / 2:
            j1 += 1
        a, b = j0, j1
        while a < i and y[a] is None:
            a += 1
        while b > i and y[b] is None:
            b -= 1
        if b > a and y[a] is not None and y[b] is not None and t[b] > t[a]:
            out[i] = (y[b] - y[a]) / (t[b] - t[a])  # type: ignore[operator]
    return out


def time_in(t: Sequence[float], s: Sequence[Optional[float]], pred) -> float:
    total = 0.0
    for i in range(1, len(t)):
        v = s[i - 1]
        dt = t[i] - t[i - 1]
        if v is not None and 0 < dt < 5 and pred(v):
            total += dt
    return total


def compute_stats(f: Flight) -> Dict[str, Any]:
    t = f.t
    st: Dict[str, Any] = {
        "aircraft": f.meta.get("aircraft") or "неизвестно",
        "started": f.meta.get("started", ""),
        "duration_s": (t[-1] - t[0]) if len(t) > 1 else 0.0,
        "samples": len(t),
    }

    def mx(key: str) -> Optional[float]:
        v = _vals(f.series(key))
        return max(v) if v else None

    def mn(key: str) -> Optional[float]:
        v = _vals(f.series(key))
        return min(v) if v else None

    def avg(key: str) -> Optional[float]:
        v = _vals(f.series(key))
        return sum(v) / len(v) if v else None

    for key in ("alt", "tas", "ias", "mach", "aoa", "ny", "vy", "water", "oil", "rpm", "power", "thrust"):
        st[f"max_{key}"] = mx(key)
    for key in ("alt", "tas", "ias"):
        st[f"avg_{key}"] = avg(key)
    st["min_ny"] = mn("ny")
    st["min_vy"] = mn("vy")

    tas = f.series("tas")
    if tas:
        dist = 0.0
        for i in range(1, len(t)):
            v = tas[i - 1]
            dt = t[i] - t[i - 1]
            if v is not None and 0 < dt < 5:
                dist += v / 3.6 * dt
        st["distance_km"] = dist / 1000

    fuel = _vals(f.series("fuel"))
    if len(fuel) >= 2:
        used = fuel[0] - fuel[-1]
        st["fuel_start"], st["fuel_end"], st["fuel_used"] = fuel[0], fuel[-1], used
        minutes = st["duration_s"] / 60
        if used > 0 and minutes > 0:
            st["fuel_rate"] = used / minutes
            st["endurance_min"] = fuel[-1] / st["fuel_rate"]

    thr = f.series("throttle")
    if thr:
        st["time_wep_s"] = time_in(t, thr, lambda v: v > 100.5)
        st["time_full_s"] = time_in(t, thr, lambda v: v >= 99.5)

    alt = f.series("alt")
    if alt:
        st["alt_bands"] = [(lo, hi, time_in(t, alt, lambda v, lo=lo, hi=hi: lo <= v < hi))
                           for lo, hi in ALT_BANDS]
    ny = f.series("ny")
    if ny:
        st["time_high_g_s"] = time_in(t, ny, lambda v: v >= 5)

    if alt and tas:
        he = energy_height(alt, tas)
        ps = derivative(t, he)
        st["max_he"] = max(_vals(he)) if _vals(he) else None
        st["max_ps"] = max(_vals(ps)) if _vals(ps) else None
    st["events"] = len(f.events)
    return st


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def fmt_time(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def fmt(v: Optional[float], unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{digits}f}".replace(",", " ")
    return f"{s} {unit}".strip()


def _decimate(n: int, limit: int = 1500) -> List[int]:
    if n <= limit:
        return list(range(n))
    step = n / limit
    idx = [int(i * step) for i in range(limit)]
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


def chart_specs(f: Flight) -> List[Dict[str, Any]]:
    """Список графиков: у каждого одна ось Y и одна единица измерения."""
    specs: List[Dict[str, Any]] = []

    def add(cid: str, title: str, unit: str, series: List[Tuple[str, str]], note: str = "",
            zero: bool = False) -> None:
        present = [(name, key) for name, key in series if f.series(key) is not None]
        if present:
            specs.append({"id": cid, "title": title, "unit": unit, "note": note, "zero": zero,
                          "series": [{"name": n, "key": k} for n, k in present]})

    add("alt", "Высота", "м", [("Высота", "alt")], zero=True)
    add("speed", "Скорость", "км/ч", [("Истинная (TAS)", "tas"), ("Приборная (IAS)", "ias")], zero=True)
    add("vy", "Вертикальная скорость", "м/с", [("Vy", "vy")],
        note="плюс — набор высоты, минус — снижение")
    add("ny", "Перегрузка", "g", [("Ny", "ny")])
    add("aoa", "Угол атаки", "°", [("Угол атаки", "aoa")])
    add("mach", "Число Маха", "M", [("M", "mach")], zero=True)
    if f.series("alt") is not None and f.series("tas") is not None:
        specs.append({"id": "he", "title": "Энергетическая высота", "unit": "м", "zero": True,
                      "note": "H + V²/2g — запас энергии: высота плюс скорость",
                      "series": [{"name": "He", "key": "__he"}]})
        specs.append({"id": "ps", "title": "Избыток энергии (Ps)", "unit": "м/с",
                      "note": "больше нуля — самолёт набирает энергию, меньше — теряет",
                      "series": [{"name": "Ps", "key": "__ps"}]})
    add("throttle", "Газ", "%", [("Газ", "throttle")], zero=True)
    add("rpm", "Обороты двигателя", "об/мин", [("Обороты", "rpm")], zero=True)
    add("temp", "Температуры двигателя", "°C", [("Вода", "water"), ("Масло", "oil")])
    add("thrust", "Тяга", "кгс", [("Тяга", "thrust")], zero=True)
    add("power", "Мощность", "л.с.", [("Мощность", "power")], zero=True)
    add("manifold", "Давление наддува", "атм", [("Наддув", "manifold")])
    add("fuel", "Топливо", "кг", [("Топливо", "fuel")], zero=True)
    return specs


def build_payload(f: Flight) -> Dict[str, Any]:
    idx = _decimate(len(f.t))
    data: Dict[str, List[Optional[float]]] = {}
    specs = chart_specs(f)
    extra: Dict[str, List[Optional[float]]] = {}
    alt, tas = f.series("alt"), f.series("tas")
    if alt is not None and tas is not None:
        he = energy_height(alt, tas)
        extra["__he"] = he
        extra["__ps"] = derivative(f.t, he)
    for spec in specs:
        for s in spec["series"]:
            src = extra.get(s["key"]) if s["key"].startswith("__") else f.series(s["key"])
            if src is not None:
                data[s["key"]] = [None if src[i] is None else round(src[i], 3) for i in idx]
    track = None
    xs, ys = f.cols.get("pos_x"), f.cols.get("pos_y")
    if xs and ys and alt is not None:
        pts = [(xs[i], ys[i], alt[i]) for i in idx
               if xs[i] is not None and ys[i] is not None and alt[i] is not None]
        if len(pts) >= 2:
            track = {"x": [round(p[0], 1) for p in pts], "y": [round(p[1], 1) for p in pts],
                     "h": [round(p[2]) for p in pts],
                     "metric": bool(f.meta.get("map_size_m"))}
    envelope = None
    if alt is not None and tas is not None:
        pts = [(tas[i], alt[i]) for i in idx if tas[i] is not None and alt[i] is not None]
        envelope = {"v": [round(p[0], 1) for p in pts], "h": [round(p[1]) for p in pts]}
    events = [{"t": round(float(e.get("t", 0)), 1), "msg": str(e.get("msg", "")),
               "mine": bool(e.get("mine"))} for e in f.events]
    return {"t": [round(f.t[i], 2) for i in idx], "data": data, "charts": specs,
            "track": track, "envelope": envelope, "events": events}


def stat_tiles(st: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(заголовок, значение, пояснение) для плиток вверху отчёта."""
    tiles = [
        ("Время полёта", fmt_time(st["duration_s"]), f"{st['samples']} замеров"),
        ("Макс. высота", fmt(st.get("max_alt"), "м"), f"средняя {fmt(st.get('avg_alt'), 'м')}"),
        ("Макс. скорость (TAS)", fmt(st.get("max_tas"), "км/ч"),
         f"средняя {fmt(st.get('avg_tas'), 'км/ч')}"),
        ("Макс. перегрузка", fmt(st.get("max_ny"), "g", 1), f"мин. {fmt(st.get('min_ny'), 'g', 1)}"),
        ("Пройдено", fmt(st.get("distance_km"), "км", 1), ""),
        ("Расход топлива", fmt(st.get("fuel_rate"), "кг/мин", 1),
         f"хватит ещё на ~{fmt(st.get('endurance_min'), 'мин')}" if st.get("endurance_min") else ""),
    ]
    return [t for t in tiles if t[1] != "—"]


def stat_rows(st: Dict[str, Any]) -> List[Tuple[str, str]]:
    rows = [
        ("Самолёт", str(st["aircraft"])),
        ("Начало записи", str(st.get("started") or "—")),
        ("Длительность", fmt_time(st["duration_s"])),
        ("Макс. / средняя высота", f"{fmt(st.get('max_alt'), 'м')} / {fmt(st.get('avg_alt'), 'м')}"),
        ("Макс. / средняя TAS", f"{fmt(st.get('max_tas'), 'км/ч')} / {fmt(st.get('avg_tas'), 'км/ч')}"),
        ("Макс. / средняя IAS", f"{fmt(st.get('max_ias'), 'км/ч')} / {fmt(st.get('avg_ias'), 'км/ч')}"),
        ("Макс. число Маха", fmt(st.get("max_mach"), "", 2)),
        ("Макс. скороподъёмность", fmt(st.get("max_vy"), "м/с", 1)),
        ("Макс. скорость снижения", fmt(-st["min_vy"] if st.get("min_vy") is not None else None, "м/с", 1)),
        ("Перегрузка макс. / мин.", f"{fmt(st.get('max_ny'), 'g', 1)} / {fmt(st.get('min_ny'), 'g', 1)}"),
        ("Время с перегрузкой ≥ 5g", fmt_time(st.get("time_high_g_s"))),
        ("Макс. угол атаки", fmt(st.get("max_aoa"), "°", 1)),
        ("Макс. энергетическая высота", fmt(st.get("max_he"), "м")),
        ("Макс. избыток энергии Ps", fmt(st.get("max_ps"), "м/с", 1)),
        ("Пройдено", fmt(st.get("distance_km"), "км", 1)),
        ("Топливо: начало → конец", f"{fmt(st.get('fuel_start'), 'кг')} → {fmt(st.get('fuel_end'), 'кг')}"),
        ("Израсходовано топлива", fmt(st.get("fuel_used"), "кг", 1)),
        ("Время на форсаже (газ > 100%)", fmt_time(st.get("time_wep_s"))),
        ("Время на полном газу (≥ 100%)", fmt_time(st.get("time_full_s"))),
        ("Макс. температура воды / масла",
         f"{fmt(st.get('max_water'), '°C')} / {fmt(st.get('max_oil'), '°C')}"),
        ("Макс. обороты", fmt(st.get("max_rpm"), "об/мин")),
        ("Макс. мощность", fmt(st.get("max_power"), "л.с.")),
        ("Макс. тяга", fmt(st.get("max_thrust"), "кгс")),
        ("События в журнале боя", str(st.get("events", 0))),
    ]
    return [r for r in rows if r[1] not in ("—", "— / —", "— → —")]


def render_html(f: Flight) -> str:
    st = compute_stats(f)
    payload = build_payload(f)
    title = f"Полёт: {st['aircraft']}"
    tiles = "".join(
        f'<div class="tile"><div class="tile-label">{html.escape(a)}</div>'
        f'<div class="tile-value">{html.escape(b)}</div>'
        f'<div class="tile-sub">{html.escape(c)}</div></div>' for a, b, c in stat_tiles(st))
    rows = "".join(f"<tr><th>{html.escape(a)}</th><td>{html.escape(b)}</td></tr>"
                   for a, b in stat_rows(st))
    bands = ""
    if st.get("alt_bands"):
        total = sum(b[2] for b in st["alt_bands"]) or 1
        items = []
        for lo, hi, sec in st["alt_bands"]:
            label = f"{lo // 1000}–{hi // 1000} км" if hi < 99999 else f"выше {lo // 1000} км"
            pct = sec / total * 100
            items.append(
                f'<div class="band" tabindex="0" title="{label}: {fmt_time(sec)} ({pct:.0f}%)">'
                f'<span class="band-label">{label}</span>'
                f'<span class="band-track"><span class="band-bar" style="width:{pct:.1f}%"></span></span>'
                f'<span class="band-value">{fmt_time(sec)} · {pct:.0f}%</span></div>')
        bands = ('<section class="card"><h2>Время на высотах</h2>'
                 '<p class="note">Сколько времени вы провели в каждом диапазоне высот</p>'
                 + "".join(items) + "</section>")
    events_html = ""
    if f.events:
        items = "".join(
            f'<li class="{"mine" if e.get("mine") else ""}"><span class="ev-t">{fmt_time(e.get("t"))}</span>'
            f'{html.escape(str(e.get("msg", "")))}</li>' for e in f.events)
        events_html = (f'<section class="card"><h2>Журнал боя</h2><p class="note">Строки с вашим '
                       f'ником выделены. На графиках события отмечены пунктиром.</p>'
                       f'<ul class="events">{items}</ul></section>')
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (HTML_TEMPLATE
            .replace("{{TITLE}}", html.escape(title))
            .replace("{{AIRCRAFT}}", html.escape(str(st["aircraft"])))
            .replace("{{STARTED}}", html.escape(str(st.get("started") or "")))
            .replace("{{TILES}}", tiles)
            .replace("{{ROWS}}", rows)
            .replace("{{BANDS}}", bands)
            .replace("{{EVENTS}}", events_html)
            .replace("{{DATA}}", data_json.replace("</", "<\\/")))


def write_report(folder: str) -> str:
    flight = load_flight(folder)
    path = os.path.join(folder, "report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_html(flight))
    return path


def write_index(root: str) -> str:
    """Сводная страница по всем полётам в папке root."""
    items = []
    for name in sorted(os.listdir(root), reverse=True):
        folder = os.path.join(root, name)
        if not os.path.isfile(os.path.join(folder, "data.csv")):
            continue
        try:
            st = compute_stats(load_flight(folder))
        except (OSError, ValueError):
            continue
        items.append(
            f"<tr><td><a href=\"{html.escape(name)}/report.html\">{html.escape(str(st.get('started') or name))}</a></td>"
            f"<td>{html.escape(str(st['aircraft']))}</td><td>{fmt_time(st['duration_s'])}</td>"
            f"<td>{fmt(st.get('max_alt'), 'м')}</td><td>{fmt(st.get('max_tas'), 'км/ч')}</td>"
            f"<td>{fmt(st.get('max_ny'), 'g', 1)}</td><td>{fmt(st.get('distance_km'), 'км', 1)}</td></tr>")
    body = ("<table class=\"stats\"><thead><tr><th>Начало</th><th>Самолёт</th><th>Время</th>"
            "<th>Макс. высота</th><th>Макс. TAS</th><th>Макс. g</th><th>Пройдено</th></tr></thead><tbody>"
            + "".join(items) + "</tbody></table>") if items else "<p>Записанных полётов пока нет.</p>"
    page = INDEX_TEMPLATE.replace("{{BODY}}", body)
    path = os.path.join(root, "index.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


# --------------------------------------------------------------------------- #
# Шаблоны
# --------------------------------------------------------------------------- #
BASE_CSS = r"""
:root {
  color-scheme: light;
  --surface-0: #f3f2ef; --surface-1: #fcfcfb; --border: #e2e1dc;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #77766f;
  --grid: #e9e8e3; --axis: #b9b8b1;
  --series-1: #2a78d6; --series-2: #eb6834;
  --accent-soft: #cde2fb; --mine: #fff4d6;
  --seq-lo: #86b6ef; --seq-hi: #0d366b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-0: #111110; --surface-1: #1a1a19; --border: #2e2e2c;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
    --grid: #262624; --axis: #4a4a46;
    --series-1: #3987e5; --series-2: #d95926;
    --accent-soft: #184f95; --mine: #3a3220;
    --seq-lo: #184f95; --seq-hi: #cde2fb;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-0: #111110; --surface-1: #1a1a19; --border: #2e2e2c;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #8f8e86;
  --grid: #262624; --axis: #4a4a46;
  --series-1: #3987e5; --series-2: #d95926;
  --accent-soft: #184f95; --mine: #3a3220;
  --seq-lo: #184f95; --seq-hi: #cde2fb;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); color: var(--text-primary);
  font: 14px/1.45 "Segoe UI", system-ui, -apple-system, sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0 0 2px; }
.sub, .note { color: var(--text-secondary); margin: 0 0 12px; }
.note { font-size: 12px; color: var(--text-muted); }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-top: 16px; min-width: 0; }
table.stats { width: 100%; border-collapse: collapse; }
table.stats th, table.stats td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border);
  font-variant-numeric: tabular-nums; }
table.stats th { color: var(--text-secondary); font-weight: 500; }
table.stats tbody th { width: 45%; }
a { color: var(--series-1); }
"""

HTML_TEMPLATE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>""" + BASE_CSS + r"""
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 16px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile-label { color: var(--text-secondary); font-size: 12px; }
.tile-value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; margin: 2px 0; white-space: nowrap; }
.tile-sub { color: var(--text-muted); font-size: 12px; min-height: 1em; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 520px), 1fr)); gap: 0 16px; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; touch-action: pan-y; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 2px 0 6px; font-size: 12px; color: var(--text-secondary); }
.legend i { display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 6px; border-radius: 1px; }
.axis-text { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--axis); stroke-width: 1; }
.cross { stroke: var(--text-muted); stroke-width: 1; }
.event-line { stroke: var(--text-muted); stroke-width: 1; stroke-dasharray: 3 3; opacity: .7; }
.direct-label { font-size: 11px; fill: var(--text-secondary); }
#tooltip { position: fixed; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,.18); display: none; z-index: 10; min-width: 140px; }
#tooltip .tt-head { color: var(--text-secondary); margin-bottom: 4px; }
#tooltip .tt-row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
#tooltip .tt-row span:first-child { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); }
#tooltip i { display: inline-block; width: 10px; height: 2px; }
#tooltip b { font-variant-numeric: tabular-nums; }
.band { display: grid; grid-template-columns: 110px 1fr 120px; align-items: center; gap: 10px; padding: 4px 0; }
.band-label, .band-value { font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.band-track { height: 14px; background: var(--grid); border-radius: 4px; overflow: hidden; }
.band-bar { display: block; height: 100%; background: var(--series-1); border-radius: 0 4px 4px 0; }
.events { list-style: none; margin: 0; padding: 0; max-height: 320px; overflow: auto; }
.events li { padding: 4px 6px; border-bottom: 1px solid var(--border); font-size: 13px; }
.events li.mine { background: var(--mine); }
.ev-t { color: var(--text-muted); font-variant-numeric: tabular-nums; margin-right: 10px; }
.ramp { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
.ramp span.bar { width: 120px; height: 8px; border-radius: 4px; background: linear-gradient(90deg, var(--seq-lo), var(--seq-hi)); }
@media (max-width: 560px) { .band { grid-template-columns: 70px 1fr 90px; } .tile-value { font-size: 22px; } }
</style></head>
<body><main>
<h1>{{AIRCRAFT}}</h1>
<p class="sub">Запись полёта · {{STARTED}}</p>
<div class="tiles">{{TILES}}</div>
<div id="charts" class="grid2"></div>
<div class="grid2">
  <section class="card" id="track-card" hidden><h2>Траектория на карте</h2>
    <p class="note">Цвет линии — высота. ○ — начало записи, ● — конец.</p>
    <div class="chart" id="track"></div>
    <div class="ramp"><span id="ramp-lo"></span><span class="bar"></span><span id="ramp-hi"></span></div></section>
  <section class="card" id="env-card" hidden><h2>Скорость — высота</h2>
    <p class="note">По горизонтали — истинная скорость, км/ч; по вертикали — высота, м. Каждая точка — один замер.</p>
    <div class="chart" id="envelope"></div></section>
</div>
{{BANDS}}
{{EVENTS}}
<section class="card"><h2>Вся статистика</h2><table class="stats"><tbody>{{ROWS}}</tbody></table>
<p class="note">Сырые данные — в файле data.csv рядом с этим отчётом (открывается в Excel).</p></section>
</main>
<div id="tooltip" role="status"></div>
<script id="flight-data" type="application/json">{{DATA}}</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("flight-data").textContent);
  var NS = "http://www.w3.org/2000/svg";
  var css = getComputedStyle(document.documentElement);
  function v(name) { return css.getPropertyValue(name).trim(); }
  var SERIES = ["--series-1", "--series-2"];
  var tip = document.getElementById("tooltip");
  var charts = [];

  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function fmtTime(s) { s = Math.round(s); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
  function fmtNum(x, d) {
    if (d === undefined) { var a = Math.abs(x); d = a >= 100 ? 0 : a >= 10 ? 1 : 2; }
    return x.toLocaleString("ru-RU", {minimumFractionDigits: d, maximumFractionDigits: d});
  }
  function niceTicks(lo, hi, n) {
    if (lo === hi) { hi = lo + 1; }
    var span = hi - lo, step = Math.pow(10, Math.floor(Math.log10(span / n)));
    var err = span / n / step;
    if (err >= 7.5) step *= 10; else if (err >= 3) step *= 5; else if (err >= 1.5) step *= 2;
    var ticks = [], t = Math.ceil(lo / step) * step;
    for (; t <= hi + step * 1e-6; t += step) ticks.push(+t.toFixed(10));
    return {ticks: ticks, lo: Math.floor(lo / step) * step, hi: Math.ceil(hi / step) * step,
            dec: Math.max(0, -Math.floor(Math.log10(step) + 1e-9))};
  }
  function nearest(arr, x) {
    var lo = 0, hi = arr.length - 1;
    while (hi - lo > 1) { var m = (lo + hi) >> 1; if (arr[m] < x) lo = m; else hi = m; }
    return Math.abs(arr[lo] - x) <= Math.abs(arr[hi] - x) ? lo : hi;
  }
  function clearKids(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  function showTip(evt, head, rows) {
    clearKids(tip);
    var h = document.createElement("div"); h.className = "tt-head"; h.textContent = head; tip.appendChild(h);
    rows.forEach(function (r) {
      var row = document.createElement("div"); row.className = "tt-row";
      var l = document.createElement("span");
      if (r.color) { var i = document.createElement("i"); i.style.background = r.color; l.appendChild(i); }
      l.appendChild(document.createTextNode(r.label));
      var b = document.createElement("b"); b.textContent = r.value;
      row.appendChild(l); row.appendChild(b); tip.appendChild(row);
    });
    tip.style.display = "block";
    var x = evt.clientX + 14, y = evt.clientY + 14, w = tip.offsetWidth, hgt = tip.offsetHeight;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - 14;
    if (y + hgt > window.innerHeight - 8) y = evt.clientY - hgt - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function hideTip() { tip.style.display = "none"; charts.forEach(function (c) { c.cross(null); }); }

  // ---- линейные графики по времени (общий курсор) -----------------------
  function lineChart(spec) {
    var card = document.createElement("section"); card.className = "card chart";
    var h = document.createElement("h2"); h.textContent = spec.title + ", " + spec.unit; card.appendChild(h);
    if (spec.note) { var p = document.createElement("p"); p.className = "note"; p.textContent = spec.note; card.appendChild(p); }
    if (spec.series.length > 1) {
      var lg = document.createElement("div"); lg.className = "legend";
      spec.series.forEach(function (s, k) {
        var it = document.createElement("span"), i = document.createElement("i");
        i.style.background = v(SERIES[k]); it.appendChild(i); it.appendChild(document.createTextNode(s.name));
        lg.appendChild(it);
      });
      card.appendChild(lg);
    }
    document.getElementById("charts").appendChild(card);

    var W = 560, H = 210, L = 52, R = 14, T = 10, B = 26;
    var svg = el("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
                         "aria-label": spec.title + " по времени"}, card);
    var t = D.t, t0 = t[0], t1 = t[t.length - 1] || 1;
    var lo = Infinity, hi = -Infinity;
    spec.series.forEach(function (s) { (D.data[s.key] || []).forEach(function (y) {
      if (y !== null) { if (y < lo) lo = y; if (y > hi) hi = y; } }); });
    if (!isFinite(lo)) return;
    if (spec.zero) lo = Math.min(lo, 0);
    var yt = niceTicks(lo, hi, 4);
    var X = function (x) { return L + (x - t0) / (t1 - t0 || 1) * (W - L - R); };
    var Y = function (y) { return T + (yt.hi - y) / (yt.hi - yt.lo || 1) * (H - T - B); };

    yt.ticks.forEach(function (y) {
      el("line", {x1: L, x2: W - R, y1: Y(y), y2: Y(y), "class": y === 0 ? "baseline" : "gridline"}, svg);
      var tx = el("text", {x: L - 6, y: Y(y) + 4, "text-anchor": "end", "class": "axis-text"}, svg);
      tx.textContent = fmtNum(y, yt.dec);
    });
    var xt = niceTicks(t0, t1, 6);
    xt.ticks.forEach(function (x) {
      if (x < t0 || x > t1) return;
      var tx = el("text", {x: X(x), y: H - 6, "text-anchor": "middle", "class": "axis-text"}, svg);
      tx.textContent = fmtTime(x);
    });
    D.events.forEach(function (e) {
      if (e.mine && e.t >= t0 && e.t <= t1) {
        var ln = el("line", {x1: X(e.t), x2: X(e.t), y1: T, y2: H - B, "class": "event-line"}, svg);
        var ti = el("title", {}, ln); ti.textContent = fmtTime(e.t) + " — " + e.msg;
      }
    });
    var labels = [];
    spec.series.forEach(function (s, k) {
      var ys = D.data[s.key] || [], d = "", pen = false;
      for (var i = 0; i < t.length; i++) {
        if (ys[i] === null || ys[i] === undefined) { pen = false; continue; }
        d += (pen ? "L" : "M") + X(t[i]).toFixed(1) + " " + Y(ys[i]).toFixed(1); pen = true;
      }
      el("path", {d: d, fill: "none", stroke: v(SERIES[k]), "stroke-width": 2,
                  "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
      if (spec.series.length > 1) {  // подпись у конца линии
        for (var j = t.length - 1; j >= 0; j--) if (ys[j] !== null) break;
        if (j >= 0) labels.push({x: X(t[j]) - 4, y: Y(ys[j]) - 6, text: s.name});
      }
    });
    labels.sort(function (a, b) { return a.y - b.y; });
    for (var q = 1; q < labels.length; q++) {
      if (labels[q].y - labels[q - 1].y < 13) labels[q].y = labels[q - 1].y + 13;
    }
    labels.forEach(function (lb) {
      el("text", {x: lb.x, y: Math.min(lb.y, H - B - 2), "text-anchor": "end", "class": "direct-label"}, svg).textContent = lb.text;
    });
    var cross = el("line", {y1: T, y2: H - B, "class": "cross", visibility: "hidden"}, svg);
    var dots = spec.series.map(function (s, k) {
      return el("circle", {r: 4, fill: v(SERIES[k]), stroke: v("--surface-1"), "stroke-width": 2, visibility: "hidden"}, svg);
    });
    var hit = el("rect", {x: L, y: 0, width: W - L - R, height: H, fill: "transparent"}, svg);
    var api = {
      cross: function (idx) {
        if (idx === null) { cross.setAttribute("visibility", "hidden"); dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); }); return; }
        var x = X(t[idx]);
        cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
        spec.series.forEach(function (s, k) {
          var y = (D.data[s.key] || [])[idx];
          if (y === null || y === undefined) { dots[k].setAttribute("visibility", "hidden"); return; }
          dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", Y(y)); dots[k].setAttribute("visibility", "visible");
        });
      }
    };
    charts.push(api);
    function move(evt) {
      var pt = svg.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
      var loc = pt.matrixTransform(svg.getScreenCTM().inverse());
      var tx = t0 + (loc.x - L) / (W - L - R) * (t1 - t0);
      var idx = nearest(t, Math.max(t0, Math.min(t1, tx)));
      charts.forEach(function (c) { c.cross(idx); });
      showTip(evt, "Время " + fmtTime(t[idx]), spec.series.map(function (s, k) {
        var y = (D.data[s.key] || [])[idx];
        return {label: s.name, color: v(SERIES[k]), value: y === null || y === undefined ? "—" : fmtNum(y) + " " + spec.unit};
      }));
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerleave", hideTip);
  }
  D.charts.forEach(lineChart);

  // ---- траектория на карте ----------------------------------------------
  function mix(a, b, k) {
    function p(h) { return [1, 3, 5].map(function (i) { return parseInt(h.substr(i, 2), 16); }); }
    var x = p(a), y = p(b);
    return "rgb(" + x.map(function (c, i) { return Math.round(c + (y[i] - c) * k); }).join(",") + ")";
  }
  if (D.track) {
    var card = document.getElementById("track-card"); card.hidden = false;
    var box = document.getElementById("track");
    var Tr = D.track, S = 420, P = 14;
    var minX = Math.min.apply(null, Tr.x), maxX = Math.max.apply(null, Tr.x);
    var minY = Math.min.apply(null, Tr.y), maxY = Math.max.apply(null, Tr.y);
    var span = Math.max(maxX - minX, maxY - minY) || 1;
    var cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    var sx = function (x) { return S / 2 + (x - cx) / span * (S - 2 * P); };
    var sy = function (y) { return S / 2 + (y - cy) / span * (S - 2 * P); };
    var svgT = el("svg", {viewBox: "0 0 " + S + " " + S, role: "img", "aria-label": "Траектория полёта на карте"}, box);
    el("rect", {x: 0, y: 0, width: S, height: S, fill: "transparent"}, svgT);
    var hmin = Math.min.apply(null, Tr.h), hmax = Math.max.apply(null, Tr.h), lo = v("--seq-lo"), hi = v("--seq-hi");
    for (var i = 1; i < Tr.x.length; i++) {
      var k = (Tr.h[i] - hmin) / ((hmax - hmin) || 1);
      el("line", {x1: sx(Tr.x[i - 1]), y1: sy(Tr.y[i - 1]), x2: sx(Tr.x[i]), y2: sy(Tr.y[i]),
                  stroke: mix(lo, hi, k), "stroke-width": 2, "stroke-linecap": "round"}, svgT);
    }
    var n = Tr.x.length - 1;
    el("circle", {cx: sx(Tr.x[0]), cy: sy(Tr.y[0]), r: 5, fill: v("--surface-1"), stroke: v("--text-primary"), "stroke-width": 2}, svgT);
    el("circle", {cx: sx(Tr.x[n]), cy: sy(Tr.y[n]), r: 5, fill: v("--text-primary"), stroke: v("--surface-1"), "stroke-width": 2}, svgT);
    // масштабная линейка
    if (Tr.metric) {
      var nice = niceTicks(0, span / 4, 1).ticks; var len = nice[nice.length - 1] || span / 4;
      var px = len / span * (S - 2 * P);
      el("line", {x1: P, x2: P + px, y1: S - P, y2: S - P, stroke: v("--text-muted"), "stroke-width": 2}, svgT);
      var lt = el("text", {x: P, y: S - P - 6, "class": "axis-text"}, svgT);
      lt.textContent = len >= 1000 ? fmtNum(len / 1000) + " км" : Math.round(len) + " м";
    }
    document.getElementById("ramp-lo").textContent = fmtNum(hmin) + " м";
    document.getElementById("ramp-hi").textContent = fmtNum(hmax) + " м";
    var hitT = el("rect", {x: 0, y: 0, width: S, height: S, fill: "transparent"}, svgT);
    var mark = el("circle", {r: 5, fill: v("--series-2"), stroke: v("--surface-1"), "stroke-width": 2, visibility: "hidden"}, svgT);
    hitT.addEventListener("pointermove", function (evt) {
      var pt = svgT.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
      var loc = pt.matrixTransform(svgT.getScreenCTM().inverse()), best = 0, bd = Infinity;
      for (var i = 0; i < Tr.x.length; i++) {
        var dx = sx(Tr.x[i]) - loc.x, dy = sy(Tr.y[i]) - loc.y, d = dx * dx + dy * dy;
        if (d < bd) { bd = d; best = i; }
      }
      if (bd > 900) { hideTip(); mark.setAttribute("visibility", "hidden"); return; }
      mark.setAttribute("cx", sx(Tr.x[best])); mark.setAttribute("cy", sy(Tr.y[best])); mark.setAttribute("visibility", "visible");
      showTip(evt, "Точка траектории", [{label: "Высота", value: fmtNum(Tr.h[best]) + " м"}]);
    });
    hitT.addEventListener("pointerleave", function () { hideTip(); mark.setAttribute("visibility", "hidden"); });
  }

  // ---- скорость — высота -------------------------------------------------
  if (D.envelope && D.envelope.v.length) {
    document.getElementById("env-card").hidden = false;
    var E = D.envelope, W = 420, H = 320, L = 52, R = 12, T = 10, B = 30;
    var svgE = el("svg", {viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Диаграмма скорость — высота"},
                  document.getElementById("envelope"));
    var vt = niceTicks(0, Math.max.apply(null, E.v), 5), ht = niceTicks(0, Math.max.apply(null, E.h), 4);
    var X = function (x) { return L + (x - vt.lo) / (vt.hi - vt.lo) * (W - L - R); };
    var Y = function (y) { return T + (ht.hi - y) / (ht.hi - ht.lo) * (H - T - B); };
    ht.ticks.forEach(function (y) {
      el("line", {x1: L, x2: W - R, y1: Y(y), y2: Y(y), "class": y === 0 ? "baseline" : "gridline"}, svgE);
      el("text", {x: L - 6, y: Y(y) + 4, "text-anchor": "end", "class": "axis-text"}, svgE).textContent = fmtNum(y, ht.dec);
    });
    vt.ticks.forEach(function (x) {
      el("text", {x: X(x), y: H - 12, "text-anchor": "middle", "class": "axis-text"}, svgE).textContent = fmtNum(x, vt.dec);
    });
    var c1 = v("--series-1");
    for (var i = 0; i < E.v.length; i++) {
      el("circle", {cx: X(E.v[i]).toFixed(1), cy: Y(E.h[i]).toFixed(1), r: 2.5, fill: c1, "fill-opacity": 0.35}, svgE);
    }
    var markE = el("circle", {r: 5, fill: c1, stroke: v("--surface-1"), "stroke-width": 2, visibility: "hidden"}, svgE);
    var hitE = el("rect", {x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent"}, svgE);
    hitE.addEventListener("pointermove", function (evt) {
      var pt = svgE.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
      var loc = pt.matrixTransform(svgE.getScreenCTM().inverse()), best = 0, bd = Infinity;
      for (var i = 0; i < E.v.length; i++) {
        var dx = X(E.v[i]) - loc.x, dy = Y(E.h[i]) - loc.y, d = dx * dx + dy * dy;
        if (d < bd) { bd = d; best = i; }
      }
      if (bd > 400) { hideTip(); markE.setAttribute("visibility", "hidden"); return; }
      markE.setAttribute("cx", X(E.v[best])); markE.setAttribute("cy", Y(E.h[best])); markE.setAttribute("visibility", "visible");
      showTip(evt, "Замер", [{label: "Скорость", value: fmtNum(E.v[best]) + " км/ч"},
                             {label: "Высота", value: fmtNum(E.h[best]) + " м"}]);
    });
    hitE.addEventListener("pointerleave", function () { hideTip(); markE.setAttribute("visibility", "hidden"); });
  }
})();
</script>
</body></html>
"""

INDEX_TEMPLATE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Журнал полётов</title>
<style>""" + BASE_CSS + r"""
.wrap { overflow-x: auto; }
</style></head>
<body><main>
<h1>Журнал полётов</h1>
<p class="sub">Все записанные полёты. Нажмите на дату, чтобы открыть отчёт.</p>
<section class="card wrap">{{BODY}}</section>
</main></body></html>
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт о полёте War Thunder (HTML с графиками).")
    ap.add_argument("folder", nargs="?", help="папка полёта (с data.csv)")
    ap.add_argument("--index", metavar="DIR", help="собрать сводную страницу по всем полётам в DIR")
    args = ap.parse_args(argv)
    if args.index:
        print(write_index(args.index))
        return 0
    if not args.folder:
        ap.error("укажите папку полёта или --index DIR")
    print(write_report(args.folder))
    return 0


if __name__ == "__main__":
    sys.exit(main())
