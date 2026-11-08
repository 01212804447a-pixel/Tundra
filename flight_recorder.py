#!/usr/bin/env python3
"""
Бортовой самописец War Thunder.

Пока вы летаете, программа читает локальное API игры (порт 8111) и пишет
параметры полёта: высоту, скорости, перегрузку, угол атаки, обороты,
температуры, топливо, положение на карте и сообщения журнала боя.
Когда полёт заканчивается (посадка в ангар, смерть, выход из боя), она
сохраняет папку с данными и строит отчёт с графиками (report.html).

    python flight_recorder.py                    # запись в папку flights/
    python flight_recorder.py --nick MyNickname  # выделять в журнале ваши события
    python flight_recorder.py --hz 10            # 10 замеров в секунду

Остановка — Ctrl+C (текущий полёт сохранится).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

import flight_report

DEFAULT_HOST = "http://127.0.0.1:8111"


# --------------------------------------------------------------------------- #
# Логика записи (без сети — легко тестировать)
# --------------------------------------------------------------------------- #
def numeric_items(d: Any, prefix: str = "") -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if k in ("valid", "type"):
            continue
        if isinstance(v, bool):
            out[prefix + k] = float(v)
        elif isinstance(v, (int, float)):
            out[prefix + k] = float(v)
    return out


class FlightSession:
    def __init__(self, wall_start: float, aircraft: str) -> None:
        self.wall_start = wall_start
        self.aircraft = aircraft
        self.rows: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.map_size_m: Optional[List[float]] = None
        self.last_valid = wall_start

    @property
    def duration(self) -> float:
        return self.rows[-1]["t"] if self.rows else 0.0


class Recorder:
    """Принимает замеры и режет их на отдельные полёты.

    Полёт начинается, когда state.json становится valid, и заканчивается,
    когда данные пропали дольше end_gap секунд или сменился самолёт.
    """

    def __init__(self, end_gap: float = 4.0, min_duration: float = 10.0,
                 nick: str = "") -> None:
        self.end_gap = end_gap
        self.min_duration = min_duration
        self.nick = nick.strip().lower()
        self.session: Optional[FlightSession] = None

    def feed(self, now: float, state: Any, indicators: Any = None,
             pos: Optional[Sequence[float]] = None) -> List[FlightSession]:
        """Добавляет замер. Возвращает завершённые полёты (обычно пустой список)."""
        finished: List[FlightSession] = []
        valid = isinstance(state, dict) and bool(state.get("valid"))
        aircraft = ""
        if isinstance(indicators, dict) and indicators.get("valid"):
            aircraft = str(indicators.get("type") or "")

        if self.session is not None:
            s = self.session
            changed = valid and aircraft and s.aircraft and aircraft != s.aircraft
            if changed or (not valid and now - s.last_valid > self.end_gap):
                done = self.finish()
                if done is not None:
                    finished.append(done)

        if not valid:
            return finished

        if self.session is None:
            self.session = FlightSession(now, aircraft)
        s = self.session
        if aircraft and not s.aircraft:
            s.aircraft = aircraft
        s.last_valid = now
        row: Dict[str, Any] = {"t": round(now - s.wall_start, 3)}
        row.update(numeric_items(state))
        row.update(numeric_items(indicators, "ind:"))
        if pos is not None:
            row["pos_x"], row["pos_y"] = round(pos[0], 2), round(pos[1], 2)
        s.rows.append(row)
        return finished

    def add_event(self, now: float, msg: str) -> None:
        if self.session is None or not msg:
            return
        mine = bool(self.nick) and self.nick in msg.lower()
        self.session.events.append({"t": round(now - self.session.wall_start, 1),
                                    "msg": msg, "mine": mine})

    def finish(self) -> Optional[FlightSession]:
        s, self.session = self.session, None
        if s is None or s.duration < self.min_duration:
            return None
        return s


# --------------------------------------------------------------------------- #
# Сохранение
# --------------------------------------------------------------------------- #
def safe_name(text: str) -> str:
    text = re.sub(r"[^\w\-.]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:60] or "unknown"


def save_session(s: FlightSession, root: str) -> str:
    started = datetime.fromtimestamp(s.wall_start)
    folder = os.path.join(root, f"{started:%Y-%m-%d_%H-%M-%S}_{safe_name(s.aircraft)}")
    os.makedirs(folder, exist_ok=True)

    keys: List[str] = ["t"]
    for r in s.rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(os.path.join(folder, "data.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(s.rows)
    with open(os.path.join(folder, "events.json"), "w", encoding="utf-8") as f:
        json.dump(s.events, f, ensure_ascii=False, indent=1)
    meta = {"aircraft": s.aircraft or "неизвестно", "started": f"{started:%d.%m.%Y %H:%M:%S}",
            "duration_s": s.duration, "samples": len(s.rows), "map_size_m": s.map_size_m}
    with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    flight_report.write_report(folder)
    flight_report.write_index(root)
    return folder


# --------------------------------------------------------------------------- #
# Работа с API игры
# --------------------------------------------------------------------------- #
def fetch_json(host: str, path: str, timeout: float = 0.5) -> Any:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(host.rstrip("/") + "/" + path, timeout=timeout) as r:
            raw = r.read()
    except (urllib.error.URLError, OSError):
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace")) if raw.strip() else None
    except ValueError:
        return None


def player_position(map_info: Any, map_obj: Any) -> Optional[List[float]]:
    """Положение игрока в метрах (или в долях карты, если размер неизвестен)."""
    if not isinstance(map_obj, list):
        return None
    me = next((o for o in map_obj if isinstance(o, dict) and o.get("icon") == "Player"), None)
    if me is None:
        return None
    try:
        x, y = float(me["x"]), float(me["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(map_info, dict) and map_info.get("valid"):
        try:
            sx = abs(float(map_info["map_max"][0]) - float(map_info["map_min"][0]))
            sy = abs(float(map_info["map_max"][1]) - float(map_info["map_min"][1]))
            return [x * sx, y * sy]
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    return [x, y]


def map_size(map_info: Any) -> Optional[List[float]]:
    try:
        return [abs(float(map_info["map_max"][0]) - float(map_info["map_min"][0])),
                abs(float(map_info["map_max"][1]) - float(map_info["map_min"][1]))]
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def run(host: str, out_dir: str, hz: float, nick: str,
        log: Callable[[str], None] = print, stop_after: Optional[float] = None,
        min_duration: float = 10.0) -> int:
    os.makedirs(out_dir, exist_ok=True)
    rec = Recorder(nick=nick, min_duration=min_duration)
    period = 1.0 / max(0.5, hz)
    last_ids = {"evt": 0, "dmg": 0}
    last_map = 0.0
    last_status = 0.0
    pos: Optional[List[float]] = None
    map_info: Any = None
    began = time.time()
    log(f"Самописец запущен: {host}, {hz:g} замеров/с, папка «{out_dir}». Ctrl+C — стоп.")

    def save(sessions: List[FlightSession]) -> None:
        for s in sessions:
            folder = save_session(s, out_dir)
            log(f"✔ Полёт сохранён ({flight_report.fmt_time(s.duration)}): "
                f"{os.path.join(folder, 'report.html')}")

    try:
        while stop_after is None or time.time() - began < stop_after:
            tick = time.time()
            state = fetch_json(host, "state.json")
            valid = isinstance(state, dict) and bool(state.get("valid"))
            indicators = fetch_json(host, "indicators.json") if valid else None

            if valid and tick - last_map >= 1.0:  # позиция и журнал — раз в секунду
                last_map = tick
                if map_info is None or not (isinstance(map_info, dict) and map_info.get("valid")):
                    map_info = fetch_json(host, "map_info.json")
                pos = player_position(map_info, fetch_json(host, "map_obj.json"))
                hud = fetch_json(host, f"hudmsg?lastEvt={last_ids['evt']}&lastDmg={last_ids['dmg']}")
                if isinstance(hud, dict):
                    for item in hud.get("damage") or []:
                        if isinstance(item, dict):
                            last_ids["dmg"] = max(last_ids["dmg"], int(item.get("id", 0) or 0))
                            rec.add_event(tick, str(item.get("msg", "")))
                    for item in hud.get("events") or []:
                        if isinstance(item, dict):
                            last_ids["evt"] = max(last_ids["evt"], int(item.get("id", 0) or 0))
            if not valid:
                map_info = None

            done = rec.feed(tick, state, indicators, pos)
            if rec.session is not None and rec.session.map_size_m is None and map_info is not None:
                rec.session.map_size_m = map_size(map_info)
            save(done)

            if rec.session is not None and tick - last_status >= 5:
                last_status = tick
                row = rec.session.rows[-1]
                log(f"  ● запись {flight_report.fmt_time(rec.session.duration)} | "
                    f"H {row.get('H, m', 0):.0f} м | TAS {row.get('TAS, km/h', 0):.0f} км/ч | "
                    f"{rec.session.aircraft or '?'}")
            time.sleep(max(0.0, period - (time.time() - tick)))
    except KeyboardInterrupt:
        pass
    final = rec.finish()
    if final is not None:
        save([final])
    log("Самописец остановлен.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Бортовой самописец War Thunder: запись полёта и отчёт с графиками.")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"адрес API игры (по умолчанию {DEFAULT_HOST})")
    ap.add_argument("--out", default="flights", help="папка для записей (по умолчанию flights)")
    ap.add_argument("--hz", type=float, default=5.0, help="замеров в секунду (по умолчанию 5)")
    ap.add_argument("--nick", default="", help="ваш ник — чтобы выделять ваши события в журнале боя")
    args = ap.parse_args(argv)
    return run(args.host, args.out, args.hz, args.nick)


if __name__ == "__main__":
    sys.exit(main())
