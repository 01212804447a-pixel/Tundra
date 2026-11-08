import json
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fake_wt_server  # noqa: E402
import wt_rangefinder as wr  # noqa: E402

INFO = {"valid": True, "map_min": [-2048, -2048], "map_max": [2048, 2048],
        "grid_steps": [400, 400], "grid_zero": [-2048, 2048], "map_generation": 3}


def obj(icon, x, y, type_="ground_model", color="#174DFF", **extra):
    d = {"type": type_, "icon": icon, "x": x, "y": y, "color": color}
    d.update(extra)
    return d


def load_real_dump():
    with open(os.path.join(ROOT, "tests", "data", "map_obj_real.json"), encoding="utf-8") as f:
        return json.load(f)


class Geometry(unittest.TestCase):
    def setUp(self):
        self.info = wr.parse_map_info(INFO)

    def test_parse_map_info(self):
        self.assertEqual((self.info.size_x, self.info.size_y), (4096, 4096))
        self.assertEqual(self.info.grid_step_x, 400)
        self.assertEqual(self.info.generation, 3)

    def test_parse_invalid(self):
        self.assertIsNone(wr.parse_map_info({"valid": False}))
        self.assertIsNone(wr.parse_map_info(None))
        self.assertIsNone(wr.parse_map_info({"valid": True}))

    def test_distance_and_bearing(self):
        a = wr.Point(0.5, 0.5)
        self.assertAlmostEqual(wr.distance_m(a, wr.Point(0.5, 0.25), self.info), 1024)
        for p, az in [((0.5, 0.25), 0), ((0.75, 0.5), 90), ((0.5, 0.75), 180), ((0.25, 0.5), 270)]:
            self.assertAlmostEqual(wr.bearing_deg(a, wr.Point(*p), self.info), az)
        d = wr.distance_m(wr.Point(0, 0), wr.Point(300 / 4096, 400 / 4096), self.info)
        self.assertAlmostEqual(d, 500)

    def test_non_square_map(self):
        info = wr.parse_map_info({"valid": True, "map_min": [0, 0], "map_max": [2000, 1000]})
        self.assertAlmostEqual(wr.distance_m(wr.Point(0, 0), wr.Point(1, 0), info), 2000)
        self.assertAlmostEqual(wr.distance_m(wr.Point(0, 0), wr.Point(0, 1), info), 1000)

    def test_grid_square(self):
        self.assertEqual(wr.grid_square(wr.Point(0.01, 0.01), self.info), "A1")
        # 0.5*4096 = 2048 м от левого края → 6-й столбец (по 400 м), 6-я строка → F
        self.assertEqual(wr.grid_square(wr.Point(0.5, 0.5), self.info), "F6")


class Sides(unittest.TestCase):
    def test_palette(self):
        self.assertEqual(wr.classify_side((250, 200, 30)), "player")
        self.assertEqual(wr.classify_side((103, 215, 86)), "squad")
        self.assertEqual(wr.classify_side((23, 77, 255)), "ally")
        self.assertEqual(wr.classify_side((4, 63, 255)), "ally")
        self.assertEqual(wr.classify_side((24, 57, 167)), "ally")
        self.assertEqual(wr.classify_side((250, 12, 0)), "enemy")
        self.assertEqual(wr.classify_side(None), "neutral")

    def test_color_from_hex(self):
        self.assertEqual(wr.parse_color({"color": "#fa0C00"}), (250, 12, 0))


class RealDump(unittest.TestCase):
    """Дамп map_obj.json из реального танкового боя (метка не поставлена)."""

    def setUp(self):
        self.m = wr.measure_from_data(INFO, load_real_dump())

    def test_player_found(self):
        self.assertEqual(self.m.status, "ok")
        self.assertAlmostEqual(self.m.player.x, 0.145702)
        self.assertAlmostEqual(self.m.player.y, 0.758814)

    def test_no_marker_and_no_spotted_enemies(self):
        self.assertEqual(self.m.markers, [])
        self.assertEqual(self.m.enemies, [])
        self.assertIsNone(self.m.main)
        self.assertIn("Цель не указана", wr.describe(self.m)[0])

    def test_zones(self):
        self.assertEqual([z.name for z in self.m.zones], ["A", "B", "C"])
        a = self.m.zones[0]
        expected = wr.distance_m(self.m.player, wr.Point(0.357068, 0.483776), self.m.map_info)
        self.assertAlmostEqual(a.distance_m, expected)

    def test_enemy_spawns(self):
        # в дампе одна вражеская танковая база внутри карты; авиа-спавн вне карты не считается
        self.assertEqual(len(self.m.enemy_spawns), 1)
        sp = self.m.enemy_spawns[0]
        self.assertAlmostEqual(sp.point.x, 0.103226)
        self.assertTrue(any(line.startswith("Спавн врага") for line in wr.describe(self.m)))

    def test_sides_in_dump(self):
        sides = {o.icon + str(o.rgb): o.side for o in self.m.objects if o.type == "ground_model"}
        self.assertEqual(sides["IFV(103, 215, 86)"], "squad")
        self.assertEqual(sides["Player(250, 200, 30)"], "player")
        self.assertNotIn("enemy", sides.values())

    def test_click_target(self):
        m = wr.measure_from_data(INFO, load_real_dump(), click=wr.Point(0.357068, 0.483776))
        self.assertEqual(m.main.kind, "click")
        self.assertAlmostEqual(m.main.distance_m, m.zones[0].distance_m)


class Targets(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(wr.measure_from_data({"valid": False}, []).status, "no_battle")
        self.assertEqual(wr.measure_from_data(INFO, [obj("MediumTank", .1, .1)]).status, "no_player")

    def test_priority_and_sorting(self):
        objs = [
            obj("Player", 0.5, 0.5, color="#faC81E"),
            obj("none", 0.5, 0.0, "point_of_interest", "#FFFFFF"),   # 2048 м
            obj("none", 0.5, 0.25, "point_of_interest", "#FFFFFF"),  # 1024 м
            obj("HeavyTank", 0.5, 0.4, color="#fa0C00"),             # враг 409.6 м
            obj("MediumTank", 0.5, 0.49),                            # союзник — не цель
            obj("respawn_base_tank", 0.5, 0.45, "respawn_base_tank", "#fa0C00"),
        ]
        m = wr.measure_from_data(INFO, objs)
        self.assertEqual([round(t.distance_m) for t in m.markers], [1024, 2048])
        self.assertEqual([round(t.distance_m) for t in m.enemies], [410])
        self.assertEqual(m.main.kind, "marker")
        m2 = wr.measure_from_data(INFO, objs[:1] + objs[3:])
        self.assertEqual(m2.main.kind, "enemy")

    def test_unknown_type_is_marker(self):
        objs = [obj("Player", 0.5, 0.5), obj("none", 0.5, 0.25, "some_new_ping", "#FFFFFF")]
        self.assertEqual(len(wr.measure_from_data(INFO, objs).markers), 1)

    def test_format(self):
        self.assertEqual(wr.format_distance(850.4), "850 м")
        self.assertEqual(wr.format_distance(12345), "12.3 км")
        self.assertEqual(wr.compass_point(0), "С")
        self.assertEqual(wr.compass_point(359), "С")
        self.assertEqual(wr.compass_point(135), "ЮВ")


class AgainstFakeServer(unittest.TestCase):
    def test_measure_over_http(self):
        server = fake_wt_server.make_server(0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            m = wr.measure(f"http://127.0.0.1:{port}")
            self.assertEqual(m.status, "ok")
            self.assertEqual(len(m.markers), 1)
            self.assertEqual(len(m.enemies), 1)
            self.assertEqual([z.name for z in m.zones], ["A", "B"])
            self.assertEqual(len(m.enemy_spawns), 5)
        finally:
            server.shutdown()
            server.server_close()

    def test_no_game(self):
        self.assertEqual(wr.measure("http://127.0.0.1:1", timeout=0.3).status, "no_game")


if __name__ == "__main__":
    unittest.main()
