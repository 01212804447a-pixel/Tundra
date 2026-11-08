import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ballistics as b  # noqa: E402
import wt_rangefinder as wr  # noqa: E402

VACUUM = b.Shell(500, 10, 100, 0.0)
APHE_85 = b.Shell(792, 9.2, 85, 1.15)


def vacuum_angle(r, v):
    return math.degrees(0.5 * math.asin(b.G * r / v ** 2))


class Vacuum(unittest.TestCase):
    """Без сопротивления воздуха есть точная формула — сверяемся с ней."""

    def test_low_and_high_angles(self):
        for r in (500, 3000, 15000):
            fs = b.solve(VACUUM, r, high_angle=True)
            self.assertAlmostEqual(fs.low.elevation_deg, vacuum_angle(r, 500), places=3)
            self.assertAlmostEqual(fs.high.elevation_deg, 90 - vacuum_angle(r, 500), places=3)
            self.assertAlmostEqual(fs.low.sight_range_m, r, delta=0.5)

    def test_flight_time(self):
        fs = b.solve(VACUUM, 2000)
        th = math.radians(fs.low.elevation_deg)
        self.assertAlmostEqual(fs.low.time_s, 2000 / (500 * math.cos(th)), places=3)

    def test_height_difference(self):
        # цель на 200 м выше: проверяем, что траектория действительно проходит через цель
        fs = b.solve(VACUUM, 3000, own_alt=0, target_alt=200)
        th = math.radians(fs.low.elevation_deg)
        t = 3000 / (500 * math.cos(th))
        y = 500 * math.sin(th) * t - b.G * t * t / 2
        self.assertAlmostEqual(y, 200, delta=0.05)

    def test_out_of_range(self):
        fs = b.solve(VACUUM, 500 ** 2 / b.G + 100)
        self.assertIsNone(fs.low)
        self.assertTrue(fs.warnings)


class WithDrag(unittest.TestCase):
    def test_drag_needs_more_elevation(self):
        drag = b.solve(APHE_85, 2000).low.elevation_deg
        vac = vacuum_angle(2000, 792)
        self.assertGreater(drag, vac)

    def test_drag_slows_shell(self):
        s = b.solve(APHE_85, 1500).low
        self.assertLess(s.impact_speed, 792)
        self.assertGreater(s.impact_speed, 400)

    def test_rifleman_rule(self):
        # По шкале прицела ставится почти горизонтальная дальность, а не наклонная.
        for h in (-100, 50, 150):
            s = b.solve(APHE_85, 1500, own_alt=100, target_alt=100 + h).low
            self.assertAlmostEqual(s.sight_range_m, 1500, delta=15)
            self.assertAlmostEqual(s.los_deg, math.degrees(math.atan2(h, 1500)), places=6)

    def test_elevation_limits(self):
        fs = b.solve(APHE_85, 1000, own_alt=0, target_alt=500, max_elev=20)
        self.assertTrue(any("возвышения" in w for w in fs.warnings))
        fs = b.solve(APHE_85, 500, own_alt=200, target_alt=0, max_depr=8)
        self.assertTrue(any("склонения" in w for w in fs.warnings))

    def test_cd_table(self):
        self.assertAlmostEqual(b.cd_ref(0), 0.120)
        self.assertAlmostEqual(b.cd_ref(10), b.CD_TABLE[-1][1])
        self.assertGreater(b.cd_ref(1.05), b.cd_ref(0.8))

    def test_format(self):
        lines = b.format_solution(b.solve(APHE_85, 1500, 0, 30))
        self.assertTrue(lines[0].startswith("Прицел: "))


class Settings(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            st = wr.FireControlSettings(own_alt=12.5, v0=1650, shell_type="apfsds")
            st.save(path)
            loaded = wr.FireControlSettings.load(path)
            self.assertEqual(loaded.own_alt, 12.5)
            self.assertEqual(loaded.v0, 1650)
            self.assertEqual(loaded.shell_type, "apfsds")

    def test_parse_float(self):
        self.assertEqual(wr.parse_float(" 12,5 "), 12.5)
        self.assertIsNone(wr.parse_float("abc"))


if __name__ == "__main__":
    unittest.main()
