import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fake_wt_server  # noqa: E402
import wt_rangefinder as wr  # noqa: E402

INFO = {"valid": True, "map_min": [-2048, -2048], "map_max": [2048, 2048],
        "grid_steps": [400, 400]}


def obj(icon, x, y, type_="ground_model"):
    return {"type": type_, "icon": icon, "x": x, "y": y, "color": "#fff"}


class PureFunctions(unittest.TestCase):
    def test_parse_map_info(self):
        info = wr.parse_map_info(INFO)
        self.assertEqual(info.size_x, 4096)
        self.assertEqual(info.size_y, 4096)
        self.assertEqual(info.grid_step_x, 400)

    def test_parse_invalid(self):
        self.assertIsNone(wr.parse_map_info({"valid": False}))
        self.assertIsNone(wr.parse_map_info(None))
        self.assertIsNone(wr.parse_map_info({"valid": True}))

    def test_distance_and_bearing(self):
        info = wr.parse_map_info(INFO)
        a = wr.Point(0.5, 0.5)
        self.assertAlmostEqual(wr.distance_m(a, wr.Point(0.5, 0.25), info), 1024)
        self.assertAlmostEqual(wr.bearing_deg(a, wr.Point(0.5, 0.25), info), 0)    # север
        self.assertAlmostEqual(wr.bearing_deg(a, wr.Point(0.75, 0.5), info), 90)   # восток
        self.assertAlmostEqual(wr.bearing_deg(a, wr.Point(0.5, 0.75), info), 180)  # юг
        self.assertAlmostEqual(wr.bearing_deg(a, wr.Point(0.25, 0.5), info), 270)  # запад
        # 3-4-5 треугольник
        d = wr.distance_m(wr.Point(0, 0), wr.Point(300 / 4096, 400 / 4096), info)
        self.assertAlmostEqual(d, 500)

    def test_non_square_map(self):
        info = wr.parse_map_info({"valid": True, "map_min": [0, 0], "map_max": [2000, 1000]})
        self.assertAlmostEqual(wr.distance_m(wr.Point(0, 0), wr.Point(1, 0), info), 2000)
        self.assertAlmostEqual(wr.distance_m(wr.Point(0, 0), wr.Point(0, 1), info), 1000)

    def test_measure_statuses(self):
        self.assertEqual(wr.measure_from_data({"valid": False}, []).status, "no_battle")
        self.assertEqual(wr.measure_from_data(INFO, [obj("MediumTank", .1, .1)]).status,
                         "no_player")
        self.assertEqual(wr.measure_from_data(INFO, [obj("Player", .5, .5)]).status,
                         "no_target")

    def test_measure_sorts_targets(self):
        objs = [
            obj("Player", 0.5, 0.5),
            obj("none", 0.5, 0.0, "point_of_interest"),   # 2048 м
            obj("none", 0.5, 0.25, "point_of_interest"),  # 1024 м
            obj("MediumTank", 0.5, 0.49),                  # не цель
        ]
        m = wr.measure_from_data(INFO, objs)
        self.assertEqual(m.status, "ok")
        self.assertEqual([round(t.distance_m) for t in m.targets], [1024, 2048])
        self.assertIn("1024 м", wr.format_measurement(m))

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
            self.assertEqual(len(m.targets), 1)
            self.assertGreater(m.nearest.distance_m, 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_no_game(self):
        self.assertEqual(wr.measure("http://127.0.0.1:1", timeout=0.3).status, "no_game")


if __name__ == "__main__":
    unittest.main()
