"""
Баллистический вычислитель (СУО) для дальномера War Thunder.

Модель: точечный снаряд, гравитация + сопротивление воздуха
    a = -g·ŷ - k(v, h)·|v|·v,   k = ρ(h)·Cd(M)·i·S / (2m)
где Cd(M) — эталонный закон сопротивления (близкий к G7), i — коэффициент
формы снаряда, S — площадь миделя, m — масса.

Главный результат — «метка прицела»: дальность, которую нужно выставить
по шкале прицела, чтобы попасть в цель выше/ниже вас. Шкала прицела
откалибрована для ровной местности, поэтому при превышении цели надо целиться
не по реальной дистанции, а по «эквивалентной». Ошибки модели сопротивления
в этой величине в основном взаимно компенсируются.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

G = 9.80665
MILS_PER_DEG = 6000 / 360  # тысячные (артиллерийские, 6000 в круге)

# Эталонный закон сопротивления Cd(M), близкий к G7 (вытянутые снаряды).
CD_TABLE: List[Tuple[float, float]] = [
    (0.00, 0.120), (0.50, 0.119), (0.70, 0.120), (0.80, 0.124), (0.90, 0.146),
    (0.95, 0.205), (1.00, 0.380), (1.05, 0.401), (1.10, 0.403), (1.20, 0.398),
    (1.40, 0.379), (1.60, 0.357), (1.80, 0.337), (2.00, 0.319), (2.50, 0.284),
    (3.00, 0.257), (3.50, 0.236), (4.00, 0.219), (5.00, 0.194), (6.00, 0.177),
]


@dataclass(frozen=True)
class ShellType:
    name: str
    form_factor: float
    hint: str


SHELL_TYPES: Dict[str, ShellType] = {
    "apfsds": ShellType("БПС (APFSDS)", 1.30,
                        "калибр = диаметр сердечника (≈ 1/4 калибра пушки)"),
    "ap": ShellType("Калиберный (AP/APHE/APCBC)", 1.15, "калибр пушки"),
    "apds": ShellType("Подкалиберный (APDS/APCR)", 1.10, "калибр = диаметр сердечника"),
    "heat": ShellType("Кумулятивный (HEAT)", 1.60, "калибр пушки"),
    "he": ShellType("Фугасный (HE)", 1.25, "калибр пушки"),
    "custom": ShellType("Свой коэффициент формы", 1.00, "задайте коэффициент формы"),
}


def cd_ref(mach: float) -> float:
    """Эталонный Cd при данном числе Маха (линейная интерполяция)."""
    if mach <= CD_TABLE[0][0]:
        return CD_TABLE[0][1]
    for (m0, c0), (m1, c1) in zip(CD_TABLE, CD_TABLE[1:]):
        if mach <= m1:
            return c0 + (c1 - c0) * (mach - m0) / (m1 - m0)
    return CD_TABLE[-1][1]


def air_density(alt_m: float) -> float:
    return 1.225 * math.exp(-max(-500.0, alt_m) / 8500.0)


def sound_speed(alt_m: float) -> float:
    return max(295.0, 340.3 - 0.0041 * alt_m)


@dataclass(frozen=True)
class Shell:
    v0: float            # начальная скорость, м/с
    mass_kg: float       # масса снаряда (для БПС — масса сердечника), кг
    caliber_mm: float    # диаметр (для БПС/APDS — диаметр сердечника), мм
    form_factor: float = 1.0  # 0 — без сопротивления воздуха

    @property
    def drag_const(self) -> float:
        """S·i / (2m) — без плотности и Cd."""
        if self.mass_kg <= 0 or self.caliber_mm <= 0:
            return 0.0
        area = math.pi * (self.caliber_mm / 2000.0) ** 2
        return area * self.form_factor / (2.0 * self.mass_kg)


@dataclass(frozen=True)
class Shot:
    """Результат пролёта до заданной горизонтальной дальности."""

    y: float        # высота траектории на этой дальности относительно орудия, м
    time_s: float
    speed: float


def _accel(c: float, alt0: float, y: float, vx: float, vy: float) -> Tuple[float, float]:
    v = math.hypot(vx, vy)
    if c == 0.0 or v == 0.0:
        return 0.0, -G
    alt = alt0 + y
    k = c * air_density(alt) * cd_ref(v / sound_speed(alt))
    return -k * v * vx, -G - k * v * vy


def fly(shell: Shell, elev_deg: float, x_target: float, alt0: float = 0.0,
        max_time: float = 240.0) -> Optional[Shot]:
    """Летит снарядом под углом elev_deg и возвращает высоту на дальности x_target.

    None — снаряд не долетает (теряет горизонтальную скорость или уходит
    далеко вниз раньше, чем достигнет x_target).
    """
    c = shell.drag_const
    th = math.radians(elev_deg)
    x, y = 0.0, 0.0
    vx, vy = shell.v0 * math.cos(th), shell.v0 * math.sin(th)
    t = 0.0
    floor = -max(3000.0, 2.0 * x_target)
    if x_target <= 0:
        return Shot(0.0, 0.0, shell.v0)
    while t < max_time:
        v = math.hypot(vx, vy)
        dt = min(0.05, 10.0 / max(v, 1.0))
        # RK2 (метод средней точки)
        ax1, ay1 = _accel(c, alt0, y, vx, vy)
        mvx, mvy = vx + ax1 * dt / 2, vy + ay1 * dt / 2
        ax2, ay2 = _accel(c, alt0, y + vy * dt / 2, mvx, mvy)
        nx, ny = x + mvx * dt, y + mvy * dt
        nvx, nvy = vx + ax2 * dt, vy + ay2 * dt
        if nx >= x_target:
            f = (x_target - x) / (nx - x) if nx != x else 0.0
            return Shot(y + (ny - y) * f, t + dt * f,
                        math.hypot(vx + (nvx - vx) * f, vy + (nvy - vy) * f))
        x, y, vx, vy, t = nx, ny, nvx, nvy, t + dt
        if vx <= 1e-3 or (vy < 0 and y < floor):
            return None
    return None


def flat_range(shell: Shell, elev_deg: float, alt0: float = 0.0,
               max_time: float = 240.0) -> Optional[float]:
    """Дальность по ровной местности (точка падения на высоте орудия)."""
    c = shell.drag_const
    th = math.radians(elev_deg)
    x, y = 0.0, 0.0
    vx, vy = shell.v0 * math.cos(th), shell.v0 * math.sin(th)
    t = 0.0
    if elev_deg <= 0:
        return 0.0
    while t < max_time:
        v = math.hypot(vx, vy)
        dt = min(0.05, 10.0 / max(v, 1.0))
        ax1, ay1 = _accel(c, alt0, y, vx, vy)
        mvx, mvy = vx + ax1 * dt / 2, vy + ay1 * dt / 2
        ax2, ay2 = _accel(c, alt0, y + vy * dt / 2, mvx, mvy)
        nx, ny = x + mvx * dt, y + mvy * dt
        nvx, nvy = vx + ax2 * dt, vy + ay2 * dt
        if ny <= 0 < y or (ny <= 0 and t > 0 and vy < 0):
            f = y / (y - ny) if y != ny else 0.0
            return x + (nx - x) * f
        x, y, vx, vy, t = nx, ny, nvx, nvy, t + dt
    return None


# --------------------------------------------------------------------------- #
# Решение задачи стрельбы
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Solution:
    elevation_deg: float    # угол возвышения орудия относительно горизонта
    los_deg: float          # угол линии визирования на цель
    correction_deg: float   # превышение ствола над линией визирования
    sight_range_m: Optional[float]  # какую дальность ставить по шкале прицела
    time_s: float
    impact_speed: float

    @property
    def elevation_mils(self) -> float:
        return self.elevation_deg * MILS_PER_DEG


@dataclass(frozen=True)
class FireSolution:
    distance_m: float       # горизонтальная дальность
    height_diff_m: float    # цель выше (+) / ниже (−) орудия
    low: Optional[Solution]
    high: Optional[Solution]
    warnings: Tuple[str, ...] = ()

    @property
    def slant_range_m(self) -> float:
        return math.hypot(self.distance_m, self.height_diff_m)


def _bisect(shell: Shell, d: float, h: float, alt0: float,
            lo: float, hi: float, f_lo: float) -> float:
    for _ in range(50):
        mid = (lo + hi) / 2
        shot = fly(shell, mid, d, alt0)
        f = (shot.y - h) if shot else -1e9
        if (f < 0) == (f_lo < 0):
            lo, f_lo = mid, f
        else:
            hi = mid
        if hi - lo < 1e-5:
            break
    return (lo + hi) / 2


def _make_solution(shell: Shell, d: float, h: float, alt0: float, elev: float) -> Solution:
    los = math.degrees(math.atan2(h, d))
    shot = fly(shell, elev, d, alt0)
    corr = elev - los
    sight = flat_range(shell, corr, alt0) if corr > 0 else 0.0
    return Solution(elev, los, corr, sight,
                    shot.time_s if shot else float("nan"),
                    shot.speed if shot else float("nan"))


def solve(shell: Shell, distance_m: float, own_alt: float = 0.0, target_alt: float = 0.0,
          high_angle: bool = False, max_elev: Optional[float] = None,
          max_depr: Optional[float] = None) -> FireSolution:
    """Находит угол возвышения для поражения цели.

    distance_m — горизонтальная дальность до цели (с карты),
    own_alt/target_alt — высоты орудия и цели, м,
    max_elev/max_depr — пределы наводки орудия, градусы (депрессия положительным числом).
    """
    h = target_alt - own_alt
    d = max(1.0, distance_m)
    warnings: List[str] = []
    if shell.v0 <= 0:
        return FireSolution(d, h, None, None, ("Не задана начальная скорость снаряда",))

    los = math.degrees(math.atan2(h, d))

    def f(theta: float) -> float:
        shot = fly(shell, theta, d, own_alt)
        return (shot.y - h) if shot else -1e9

    # Низкая (настильная) траектория: идём вверх от линии визирования.
    low = high = None
    theta, f_prev = los, f(los)
    step = 0.1
    peak_theta, peak_f = theta, f_prev
    while theta < 89.0:
        nt = min(89.0, theta + step)
        fn = f(nt)
        if fn >= 0 > f_prev:
            low = _bisect(shell, d, h, own_alt, theta, nt, f_prev)
            break
        if fn < f_prev - 1e-9 and fn < 0:  # прошли вершину и не достали
            break
        if fn > peak_f:
            peak_theta, peak_f = nt, fn
        theta, f_prev = nt, fn
        step = min(2.0, step * 1.6)

    if low is None:
        warnings.append("Цель вне досягаемости этого снаряда")
    elif high_angle:
        # Навесная траектория: ищем, где f снова становится < 0 выше вершины.
        theta = max(low + 0.5, 30.0)
        f_prev = f(theta)
        while theta < 89.95:
            nt = min(89.95, theta + 2.0)
            fn = f(nt)
            if f_prev >= 0 > fn:
                high = _bisect(shell, d, h, own_alt, theta, nt, f_prev)
                break
            theta, f_prev = nt, fn

    low_sol = _make_solution(shell, d, h, own_alt, low) if low is not None else None
    high_sol = _make_solution(shell, d, h, own_alt, high) if high is not None else None

    if low_sol is not None:
        if max_elev is not None and low_sol.elevation_deg > max_elev:
            warnings.append(f"Не хватает угла возвышения: нужно {low_sol.elevation_deg:+.1f}°, "
                            f"орудие {max_elev:+.0f}°")
        if max_depr is not None and low_sol.elevation_deg < -abs(max_depr):
            warnings.append(f"Не хватает склонения: нужно {low_sol.elevation_deg:+.1f}°, "
                            f"орудие −{abs(max_depr):.0f}°")
    if high_sol is not None and max_elev is not None and high_sol.elevation_deg > max_elev:
        warnings.append(f"Навесом не достать: нужно {high_sol.elevation_deg:+.1f}°")
    return FireSolution(d, h, low_sol, high_sol, tuple(warnings))


def format_solution(fs: FireSolution) -> List[str]:
    lines: List[str] = []
    s = fs.low
    if s is not None:
        if s.sight_range_m is not None:
            lines.append(f"Прицел: {s.sight_range_m:.0f} м")
        lines.append(f"Возвышение ствола: {s.elevation_deg:+.2f}° ({s.elevation_mils:+.0f} т.д.)")
        lines.append(f"Угол на цель: {s.los_deg:+.2f}°, поправка {s.correction_deg:+.2f}°")
        lines.append(f"Время полёта: {s.time_s:.2f} с, скорость у цели {s.impact_speed:.0f} м/с")
    if fs.high is not None:
        lines.append(f"Навесом: {fs.high.elevation_deg:+.1f}°, полёт {fs.high.time_s:.1f} с")
    lines.extend("⚠ " + w for w in fs.warnings)
    return lines
