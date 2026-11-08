import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fake_wt_server  # noqa: E402
import flight_recorder as fr  # noqa: E402
import flight_report as rep  # noqa: E402


def state(h=1000.0, tas=360.0, **extra):
    d = {"valid": True, "H, m": h, "TAS, km/h": tas, "IAS, km/h": tas * 0.9, "Ny": 1.0,
         "Vy, m/s": 0.0, "Mfuel, kg": 300.0, "throttle 1, %": 100.0}
    d.update(extra)
    return d


IND = {"valid": True, "type": "p-51d-5", "speed": 100.0}


class RecorderLogic(unittest.TestCase):
    def test_session_lifecycle(self):
        r = fr.Recorder(end_gap=4, min_duration=5)
        for i in range(11):                       # 10 секунд полёта
            self.assertEqual(r.feed(100 + i, state(), IND, (10, 20)), [])
        self.assertEqual(r.session.aircraft, "p-51d-5")
        self.assertEqual(r.feed(112, {"valid": False}), [])     # пауза < 4 с — ещё летим
        done = r.feed(116, {"valid": False})                    # пауза > 4 с — конец
        self.assertEqual(len(done), 1)
        self.assertEqual(len(done[0].rows), 11)
        self.assertEqual(done[0].rows[-1]["t"], 10)
        self.assertEqual(done[0].rows[0]["pos_x"], 10)
        self.assertIn("ind:speed", done[0].rows[0])
        self.assertIsNone(r.session)

    def test_short_flight_dropped(self):
        r = fr.Recorder(end_gap=1, min_duration=10)
        r.feed(0, state(), IND)
        r.feed(2, state(), IND)
        self.assertEqual(r.feed(10, {"valid": False}), [])

    def test_aircraft_change_splits(self):
        r = fr.Recorder(min_duration=1)
        for i in range(5):
            r.feed(i, state(), IND)
        done = r.feed(5, state(), {"valid": True, "type": "yak-3"})
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].aircraft, "p-51d-5")
        self.assertEqual(r.session.aircraft, "yak-3")

    def test_events_marked_by_nick(self):
        r = fr.Recorder(nick="Ace")
        r.feed(0, state(), IND)
        r.add_event(3, "ace (P-51D-5) сбил ✈Bf 109")
        r.add_event(4, "Other (Yak-3) сбил ✈Fw 190")
        self.assertEqual([e["mine"] for e in r.session.events], [True, False])
        self.assertEqual(r.session.events[0]["t"], 3)

    def test_numeric_items(self):
        self.assertEqual(fr.numeric_items({"valid": True, "type": "x", "H, m": 5, "s": "abc"}),
                         {"H, m": 5.0})

    def test_player_position(self):
        info = {"valid": True, "map_min": [0, 0], "map_max": [10000, 8000]}
        objs = [{"icon": "Player", "x": 0.5, "y": 0.25}]
        self.assertEqual(fr.player_position(info, objs), [5000, 2000])
        self.assertEqual(fr.player_position(None, objs), [0.5, 0.25])
        self.assertIsNone(fr.player_position(info, []))


def synthetic_rows():
    rows = []
    for i in range(61):  # 60 с, 1 замер в секунду
        rows.append({"t": i, "H, m": 500 + 100 * i, "TAS, km/h": 360, "Ny": 1 + (i % 10) / 2,
                     "Mfuel, kg": 300 - i * 0.5, "throttle 1, %": 110 if i < 30 else 100,
                     "water temp 1, C": 90 + i / 10, "pos_x": i * 100, "pos_y": 0})
    return rows


class Stats(unittest.TestCase):
    def setUp(self):
        self.f = rep.flight_from_rows(synthetic_rows(), {"aircraft": "test", "map_size_m": [8000, 8000]},
                                      [{"t": 20, "msg": "Ace сбил кого-то", "mine": True}])
        self.st = rep.compute_stats(self.f)

    def test_basic(self):
        self.assertEqual(self.st["duration_s"], 60)
        self.assertEqual(self.st["max_alt"], 6500)
        self.assertAlmostEqual(self.st["distance_km"], 6.0)      # 100 м/с × 60 с
        self.assertAlmostEqual(self.st["fuel_used"], 30)
        self.assertAlmostEqual(self.st["fuel_rate"], 30)        # кг/мин
        self.assertAlmostEqual(self.st["time_wep_s"], 30)
        self.assertAlmostEqual(self.st["max_ny"], 5.5)
        self.assertAlmostEqual(self.st["time_high_g_s"], 12)    # Ny = 5 и 5,5 — 2 с из каждых 10

    def test_alt_bands(self):
        bands = {lo: sec for lo, hi, sec in self.st["alt_bands"]}
        self.assertAlmostEqual(sum(bands.values()), 60)
        self.assertAlmostEqual(bands[0], 5)      # 500..900 м — первые 5 с
        self.assertAlmostEqual(bands[1000], 20)

    def test_energy(self):
        he = rep.energy_height([1000], [360])
        self.assertAlmostEqual(he[0], 1000 + 100 ** 2 / (2 * rep.G))
        d = rep.derivative([0, 1, 2, 3, 4], [0, 10, 20, 30, 40])
        self.assertAlmostEqual(d[2], 10)
        # Ps = скороподъёмность при постоянной скорости
        self.assertAlmostEqual(self.st["max_ps"], 100, places=5)

    def test_html(self):
        page = rep.render_html(self.f)
        self.assertNotIn("{{", page)
        self.assertIn("<title>Полёт: test</title>", page)
        data = page.split('<script id="flight-data" type="application/json">')[1].split("</script>")[0]
        payload = json.loads(data)
        ids = [c["id"] for c in payload["charts"]]
        for cid in ("alt", "speed", "ny", "he", "ps", "throttle", "temp", "fuel"):
            self.assertIn(cid, ids)
        self.assertNotIn("aoa", ids)            # нет данных — нет графика
        self.assertTrue(payload["track"]["metric"])
        self.assertEqual(payload["events"][0]["mine"], True)

    def test_missing_fields(self):
        f = rep.flight_from_rows([{"t": 0, "H, m": 100}, {"t": 1, "H, m": 120}])
        page = rep.render_html(f)
        self.assertIn("Высота", page)
        st = rep.compute_stats(f)
        self.assertNotIn("distance_km", st)

    def test_decimate(self):
        idx = rep._decimate(10000, 1000)
        self.assertLessEqual(len(idx), 1001)
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], 9999)


class SaveAndRun(unittest.TestCase):
    def test_save_session_and_index(self):
        s = fr.FlightSession(1_700_000_000, "la-5fn")
        s.rows = synthetic_rows()
        with tempfile.TemporaryDirectory() as d:
            folder = fr.save_session(s, d)
            for name in ("data.csv", "events.json", "meta.json", "report.html"):
                self.assertTrue(os.path.isfile(os.path.join(folder, name)), name)
            self.assertTrue(os.path.isfile(os.path.join(d, "index.html")))
            reloaded = rep.load_flight(folder)
            self.assertEqual(len(reloaded.t), 61)
            with open(os.path.join(d, "index.html"), encoding="utf-8") as f:
                self.assertIn("la-5fn", f.read())

    def test_record_from_fake_server(self):
        fake_wt_server.MODE.update(mode="air", speed=80.0)
        server = fake_wt_server.make_server(0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                fake_wt_server.START = __import__("time").time()
                fr.run(f"http://127.0.0.1:{port}", d, 20, "TestPilot", log=lambda s: None,
                       stop_after=2.5, min_duration=1.0)
                folders = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))]
                self.assertEqual(len(folders), 1)
                f = rep.load_flight(os.path.join(d, folders[0]))
                self.assertEqual(f.meta["aircraft"], "bf-109f-4")
                self.assertGreater(len(f.t), 10)
                self.assertIsNotNone(f.series("alt"))
                self.assertIsNotNone(f.cols.get("pos_x"))
        finally:
            fake_wt_server.MODE.update(mode="ground", speed=1.0)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
