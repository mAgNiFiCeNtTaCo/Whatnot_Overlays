"""Tests for the card scanner (phase 1). Skipped when OpenCV/NumPy aren't installed."""

import json
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import cv2
    import numpy as np

    from scanner import app, detect, synth
except ImportError:  # pragma: no cover - depends on the machine
    cv2 = None

NO_COUNTER = "http://127.0.0.1:9"  # nothing listens here; keeps tests off the live counter


@unittest.skipIf(cv2 is None, "OpenCV/NumPy not installed")
class DetectTests(unittest.TestCase):
    def test_finds_held_card_with_thumb(self):
        errors, found = [], 0
        for i in range(60):
            t = i * 0.31
            rarity = app.RARITY_IDS[i % len(app.RARITY_IDS)]
            frame, truth = synth.make_frame(t, rarity=rarity)
            d = detect.find_card(frame, detect.guide_box(frame.shape[1], frame.shape[0]))
            if d["found"]:
                found += 1
                errors.append(float(np.abs(detect.order_corners(d["quad"]) - detect.order_corners(truth)).max()))
        self.assertGreaterEqual(found, 54, "should find the card in at least 90% of frames")
        self.assertLess(float(np.median(errors)), 8.0)
        self.assertLess(float(np.percentile(errors, 90)), 15.0)

    def test_no_card_no_find(self):
        for t in np.linspace(0, 8, 10):
            frame, _ = synth.make_frame(float(t), present=False)
            self.assertFalse(detect.find_card(frame, detect.guide_box(1280, 720))["found"])

    def test_warp_is_always_portrait(self):
        frame, truth = synth.make_frame(1.0)
        self.assertEqual(detect.warp_card(frame, truth).shape, (detect.OUT_H, detect.OUT_W, 3))
        sideways = np.array([[300, 200], [800, 200], [800, 543], [300, 543]], np.float32)
        self.assertEqual(detect.warp_card(frame, sideways).shape, (detect.OUT_H, detect.OUT_W, 3))

    def test_quality_sees_blur(self):
        card = synth.make_card("ultra")
        sharp = detect.quality(card)["sharp"]
        blurred = detect.quality(cv2.GaussianBlur(card, (15, 15), 0))["sharp"]
        self.assertGreater(sharp, blurred * 3)

    def test_guide_is_card_shaped_and_centred(self):
        x, y, w, h = detect.guide_box(1280, 720, "medium")
        self.assertAlmostEqual(w / h, detect.CARD_ASPECT, places=2)
        self.assertLessEqual(abs(x + w / 2 - 640), 1)
        self.assertLessEqual(abs(y + h / 2 - 360), 1)


@unittest.skipIf(cv2 is None, "OpenCV/NumPy not installed")
class GlitchFilterTests(unittest.TestCase):
    def test_drops_photos_whose_corners_jump(self):
        base = [[100, 100], [400, 100], [400, 540], [100, 540]]
        frames = []
        for k in range(10):
            corners = [[x + k, y + k] for x, y in base]  # slow drift: fine
            frames.append((b"jpg", {"corners": corners}))
        frames.append((b"jpg", {"corners": [[100, 160], [400, 160], [400, 540], [100, 540]]}))  # lost the name bar
        frames.append((b"jpg", {"corners": None}))  # outline fallback: kept
        kept, dropped = app._drop_glitches(frames)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 11)


@unittest.skipIf(cv2 is None, "OpenCV/NumPy not installed")
class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ds = app.Dataset(Path(self.tmp.name) / "training")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_list_summary_delete(self):
        ok, jpg = cv2.imencode(".jpg", synth.make_card("secret"))
        frames = [(jpg.tobytes(), {"method": "edges", "sharp": 100.0, "bright": 150.0, "glare": 0.0})] * 9
        meta = self.ds.save("secret", "Some Card", frames, jpg.tobytes(), {"camera": "test"})
        self.assertRegex(meta["id"], app.CAPTURE_ID)
        folder = self.ds.find(meta["id"])
        self.assertEqual(sorted(p.name for p in folder.iterdir())[:2], ["00.jpg", "01.jpg"])
        self.assertTrue((folder / "full.jpg").is_file())
        self.assertEqual(self.ds.summary()["secret"], {"cards": 1, "frames": 9, "stream": 0})
        self.assertEqual(self.ds.captures()[0]["card"], "Some Card")
        self.assertIsNotNone(self.ds.file("secret", meta["id"], "00.jpg"))
        for bad in (("secret", meta["id"], "../meta.json"), ("secret", "..", "00.jpg"),
                    ("nope", meta["id"], "00.jpg"), ("secret", meta["id"], "meta.json")):
            self.assertIsNone(self.ds.file(*bad), bad)
        self.assertTrue(self.ds.delete(meta["id"]))
        self.assertFalse(self.ds.delete(meta["id"]))
        self.assertFalse(self.ds.delete("../../etc"))
        self.assertEqual(self.ds.summary()["secret"], {"cards": 0, "frames": 0, "stream": 0})


@unittest.skipIf(cv2 is None, "OpenCV/NumPy not installed")
class ScannerServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.scanner = app.Scanner(cls.tmp.name, demo=True, counter_url=NO_COUNTER)
        cls.httpd = app.ScannerServer(("127.0.0.1", 0), cls.scanner)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.scanner.close()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        return json.loads(urllib.request.urlopen(self.base + path, timeout=30).read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(req, timeout=30).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait(self, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            s = self.get("/api/status")
            if cond(s):
                return s
            time.sleep(0.2)
        self.fail("timed out")

    def test_capture_saves_training_photos(self):
        self.wait(lambda s: s["detection"]["found"])
        status, s = self.post("/api/capture", {"rarity": "starlight", "card": "  Test Card  "})
        self.assertEqual(status, 200)
        self.assertEqual(s["capture"]["card"], "Test Card")
        self.assertEqual(self.post("/api/capture", {"rarity": "ultra"})[0], 400)  # one at a time
        s = self.wait(lambda s: s["capture"] is None and s["message"] and not s["message"].get("pending"))
        self.assertTrue(s["message"]["ok"], s["message"])
        self.assertGreaterEqual(s["dataset"]["starlight"]["frames"], app.MIN_FRAMES)
        shot = s["recent"][0]
        self.assertEqual((shot["rarity"], shot["card"]), ("starlight", "Test Card"))
        img = urllib.request.urlopen(self.base + shot["thumb"], timeout=30).read()
        self.assertEqual(img[:3], b"\xff\xd8\xff")
        status, s = self.post("/api/captures/delete", {"id": shot["id"]})
        self.assertEqual(status, 200)
        self.assertNotIn(shot["id"], [r["id"] for r in s["recent"]])

    def test_guess_only_saves_nothing(self):
        self.wait(lambda s: s["detection"]["found"])
        before = sum(v["frames"] for v in self.get("/api/status")["dataset"].values())
        status, s = self.post("/api/capture", {"guess": True})
        self.assertEqual(status, 200)
        self.assertEqual(s["capture"]["target"], app.GUESS_FRAMES)
        s = self.wait(lambda s: s["capture"] is None and s["message"] and not s["message"].get("pending"))
        self.assertIn("guess", s["message"]["text"].lower() + ("guess" if s["message"].get("guess") else ""))
        self.assertEqual(sum(v["frames"] for v in s["dataset"].values()), before)

    def test_capture_saves_with_a_trained_model(self):
        # Regression: once recognition was trained, the guess stored with each capture
        # (NumPy numbers) made every save fail.
        from scanner import rarity
        X = np.random.default_rng(0).normal(size=(8, len(rarity.ALL_FEATURES))).astype(np.float32)
        y = np.array(["ultra"] * 4 + ["secret"] * 4)
        # let background retraining (from earlier tests' captures) finish first, or it would
        # replace the model planted here
        self.wait(lambda s: not s["recognition"]["training"] and not self.scanner._retrain.is_set())
        with self.scanner.lock:
            self.scanner.model = rarity.RarityModel().fit(X, y)
        self.wait(lambda s: s["detection"]["found"])
        before = self.get("/api/status")["dataset"]["secret"]["cards"]
        self.post("/api/capture", {"rarity": "secret"})
        s = self.wait(lambda s: s["capture"] is None and s["message"] and not s["message"].get("pending"))
        self.assertTrue(s["message"]["ok"], s["message"])
        self.assertIn("It guessed", s["message"]["text"])
        self.assertEqual(s["dataset"]["secret"]["cards"], before + 1)

    def test_cancel(self):
        self.post("/api/capture", {"rarity": "ultra"})
        status, s = self.post("/api/capture/cancel", {})
        self.assertEqual(status, 200)
        self.assertIsNone(s["capture"])
        self.assertFalse(s["message"]["ok"])

    def test_bad_requests(self):
        for path, body in (("/api/capture", {"rarity": "common"}), ("/api/capture", {"rarity": "ultra", "card": 5}),
                           ("/api/settings", {"guide": "huge"}), ("/api/settings", {}),
                           ("/api/settings", {"allowGuide": "yes"}), ("/api/settings", {"fps": 60}),
                           ("/api/settings", {"fps": True}), ("/api/camera", {"index": -1}),
                           ("/api/camera", {"index": "0"}), ("/api/captures/delete", {"id": "nope"})):
            status, payload = self.post(path, body)
            self.assertEqual(status, 400, (path, body))
            self.assertIn("error", payload)

    def test_settings_saved(self):
        status, s = self.post("/api/settings", {"guide": "large", "allowGuide": True, "fps": 15})
        self.assertEqual((s["guide"], s["allowGuide"], s["targetFps"]), ("large", True, 15))
        saved = json.loads((Path(self.tmp.name) / "scanner.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["guide"], saved["allowGuide"], saved["fps"]), ("large", True, 15))
        self.post("/api/settings", {"guide": "medium", "allowGuide": False, "fps": 30})

    def test_preview_stream(self):
        resp = urllib.request.urlopen(self.base + "/video.mjpg", timeout=30)
        try:
            self.assertTrue(resp.headers["Content-Type"].startswith("multipart/x-mixed-replace"))
            head = resp.read(2048)
            self.assertTrue(head.startswith(b"--frame"))
            self.assertIn(b"\xff\xd8", head)
        finally:
            resp.close()

    def test_find_cameras_twice_does_not_freeze(self):
        # Regression: a second "Find cameras" while one was running deadlocked the scanner.
        self.scanner.scanning = True  # pretend a search is already running (no real cameras probed)
        try:
            status, s = self.post("/api/cameras/scan", {})
            self.assertEqual(status, 200)
            self.assertTrue(s["scanning"])
            self.get("/api/status")  # still answering
        finally:
            self.scanner.scanning = False

    def test_debug_frames(self):
        self.wait(lambda s: s["camera"]["width"])
        status, s = self.post("/api/debug-frame", {"note": "Fusion test"})
        self.assertEqual(status, 200)
        saved = list((Path(self.tmp.name) / "debug").glob("*-fusion-test.jpg"))
        self.assertEqual(len(saved), 1)
        info = json.loads(saved[0].with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["reason"], "fusion-test")
        self.assertEqual(self.post("/api/debug-frame", {"note": 5})[0], 400)

    def test_page_served(self):
        html = urllib.request.urlopen(self.base + "/", timeout=30).read()
        self.assertIn(b"<title>Card Scanner</title>", html)


class StubSource:
    """A synthetic card the test puts in front of the camera and takes away."""
    name, index, error = "Stub camera", -2, None

    def __init__(self):
        self.present, self.seed, self._t0 = False, 7, time.monotonic()

    def latest(self):
        now = time.monotonic()
        frame, _ = synth.make_frame((now - self._t0) * 0.3, rarity="starlight", present=self.present, seed=self.seed)
        time.sleep(0.03)
        return frame, now

    def close(self):
        pass


class StubModel:
    """Recognition stand-in: says whatever the test wants."""
    ready = True

    def __init__(self):
        self.answer = {"starlight": 0.95, "secret": 0.05}

    def predict(self, vec):
        return dict(self.answer)

    def markers(self, vec):
        return {}

    def breakout_sides(self, vec):
        return []


@unittest.skipIf(cv2 is None, "OpenCV/NumPy not installed")
class AutoModeTests(unittest.TestCase):
    """Auto mode against a real counter: read each card once, add sure hits, skip the rest."""

    def setUp(self):
        import server
        self.tmp = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.tmp.name) / "state.json")
        self.counter = server.HitCounterServer(("127.0.0.1", 0), self.store)
        threading.Thread(target=self.counter.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        url = f"http://127.0.0.1:{self.counter.server_address[1]}"
        self.scanner = app.Scanner(Path(self.tmp.name) / "scan", demo=True, counter_url=url)
        self.cam, self.model = StubSource(), StubModel()
        # let the first (empty) training pass finish, or it would replace the stand-in model
        self.until(lambda: not self.scanner.recognition["training"] and not self.scanner._retrain.is_set())
        with self.scanner.lock:
            self.scanner.source, self.scanner.model = self.cam, self.model
        # keep the stand-in model: no retraining from the captures auto mode saves
        self.scanner.retrain_gap = 1e9

    def tearDown(self):
        self.scanner.close()
        self.counter.shutdown()
        self.counter.server_close()
        self.tmp.cleanup()

    def until(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        self.fail("timed out")

    def reads(self):
        return self.scanner.status()["auto"]["reads"]

    def hits(self):
        return [(e["rarity"], e.get("auto")) for e in self.store.state()["log"]]

    def show_card(self, n_reads):
        """Hold the card up until the scanner has made n_reads reads in all, then put it down."""
        self.cam.present = True
        self.until(lambda: len(self.reads()) >= n_reads)
        self.cam.present = False
        self.until(lambda: self.scanner.status()["auto"]["phase"] == "waiting")
        return self.reads()[0]

    def test_reads_each_card_once_and_adds_sure_hits(self):
        self.scanner.set_auto({"on": True})
        read = self.show_card(1)
        self.assertEqual((read["status"], read["guess"], read["logged"]), ("logged", "starlight", "starlight"), read)
        self.assertEqual(self.hits(), [("starlight", True)])
        # a sure hit is saved to the training photos on its own, marked as not checked by you
        self.assertEqual((read["trained"], read["verified"]), ("starlight", False))
        metas = self.scanner.dataset.captures()
        self.assertEqual([(m["rarity"], m["source"], m["verified"]) for m in metas], [("starlight", "auto", False)])
        self.assertEqual(self.scanner.status()["dataset"]["starlight"]["stream"], 1)

        # held up again: same card, not added twice
        self.cam.present = True
        self.until(lambda: len(self.reads()) >= 2)
        self.assertEqual(self.reads()[0]["status"], "repeat")
        time.sleep(1.5)  # still in view: not read again
        self.assertEqual(len(self.reads()), 2)
        self.assertEqual(self.scanner.status()["auto"]["phase"], "holding")
        self.cam.present = False
        self.until(lambda: self.scanner.status()["auto"]["phase"] == "waiting")

        # not a hit: ignored
        self.model.answer = {"none": 0.9, "ultra": 0.1}
        self.assertEqual(self.show_card(3)["status"], "skipped")
        # not sure enough: not added
        self.cam.seed = 99  # a different card
        self.model.answer = {"secret": 0.6, "ultra": 0.4}
        unsure = self.show_card(4)
        self.assertEqual(unsure["status"], "unsure")
        self.assertIsNone(unsure["trained"])  # unsure reads wait for your answer, nothing saved yet
        self.assertEqual(self.scanner.status()["auto"]["waiting"], 1)
        self.assertEqual(self.hits(), [("starlight", True)])

        # fixing reads corrects the counter and keeps them as training captures
        self.scanner.fix_read({"id": unsure["id"], "rarity": "secret"})
        self.assertEqual(self.hits(), [("starlight", True), ("secret", True)])
        self.scanner.fix_read({"id": read["id"], "rarity": "ultra"})
        self.assertEqual(sorted(self.hits()), [("secret", True), ("ultra", True)])
        self.scanner.fix_read({"id": read["id"], "rarity": "none"})
        self.assertEqual(self.hits(), [("secret", True)])
        s = self.scanner.status()
        self.assertEqual((s["dataset"]["secret"]["cards"], s["dataset"]["none"]["cards"]), (1, 1))
        self.assertEqual(s["dataset"]["ultra"]["cards"], 0)  # the read moved from Ultra to Not a hit
        self.assertEqual({r["id"]: r["status"] for r in s["auto"]["reads"]}[read["id"]], "fixed")
        self.assertIsNotNone(self.scanner.read_thumb(read["id"]))

        self.assertTrue(all(m["verified"] for m in self.scanner.dataset.captures()))  # your answers are checked
        self.assertEqual(self.scanner.status()["auto"]["waiting"], 0)
        self.scanner.forget_read({"id": unsure["id"]})
        self.assertNotIn(unsure["id"], [r["id"] for r in self.reads()])
        self.assertEqual(self.hits(), [("secret", True)])  # forgetting leaves the counter alone

    def test_same_card_other_rarity_counts_and_unsure_non_hits_ask(self):
        self.scanner.set_auto({"on": True})
        self.show_card(1)  # Starlight, added
        self.model.answer = {"ultra": 0.9, "secret": 0.1}  # the same card art, but an Ultra print
        self.assertEqual(self.show_card(2)["status"], "logged")
        self.assertEqual(self.hits(), [("starlight", True), ("ultra", True)])
        self.model.answer = {"none": 0.6, "secret": 0.4}  # unsure non-hit: asks too
        asked = self.show_card(3)
        self.assertEqual(asked["status"], "unsure")
        self.scanner.fix_read({"id": asked["id"], "rarity": "secret"})  # it was a hit after all
        self.assertEqual(self.hits(), [("starlight", True), ("ultra", True), ("secret", True)])
        self.assertEqual(sorted(m["rarity"] for m in self.scanner.dataset.captures()), ["secret", "starlight", "ultra"])

    def test_waiting_reads_are_kept(self):
        with self.scanner.lock:
            self.scanner.auto_reads = [{"id": f"r{i}", "time": time.time(), "status": "unsure" if i == 0 else "logged",
                                        "frames": [], "sig": None, "logged": None, "guess": "ultra", "confidence": 0.5,
                                        "top": [], "reading": "", "text": "", "trained": None, "verified": False}
                                       for i in range(app.AUTO_KEEP)]
        self.scanner.set_auto({"on": True})
        self.model.answer = {"none": 0.95, "ultra": 0.05}
        self.cam.present = True  # one more read: the oldest *answered* read goes, not the waiting one
        self.until(lambda: self.reads()[0]["status"] == "skipped")
        ids = [r["id"] for r in self.scanner.auto_reads]
        self.assertEqual(len(ids), app.AUTO_KEEP)
        self.assertIn("r0", ids)
        self.assertNotIn(f"r{app.AUTO_KEEP - 1}", ids)

    def test_off_by_default_and_settings(self):
        self.cam.present = True
        time.sleep(1.5)
        self.assertEqual(self.reads(), [])
        self.assertEqual(self.scanner.status()["auto"]["phase"], "off")
        s = self.scanner.set_auto({"threshold": 0.95})
        self.assertEqual(s["auto"]["threshold"], 0.95)
        saved = json.loads((Path(self.tmp.name) / "scan" / "scanner.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["autoThreshold"], 0.95)
        for body in ({}, {"on": "yes"}, {"threshold": 0.5}, {"threshold": True}):
            with self.assertRaises(app.BadRequest):
                self.scanner.set_auto(body)
        for body in ({"id": "nope", "rarity": "ultra"}, {"id": None, "rarity": "ultra"}, {"rarity": "common"}):
            with self.assertRaises(app.BadRequest):
                self.scanner.fix_read(body)

    def test_card_names(self):
        def named(name, sure=True, printed=("secret", "starlight", "ultra")):
            res = {"name": name, "sure": sure, "votes": 5.0, "share": 1.0, "candidates": [[name, 5.0], ["Other Card", 1.0]],
                   "printed": list(printed) if printed is not None else None}
            return mock.patch.object(self.scanner, "_read_name", lambda cards, rarity: res)

        self.scanner.set_auto({"on": True})
        with named("Visas Samsara"):
            read = self.show_card(1)
        self.assertEqual((read["status"], read["name"]), ("logged", "Visas Samsara"))
        self.assertEqual(self.store.state()["log"][-1]["card"], "Visas Samsara")  # the name is on the counter
        self.assertEqual(self.scanner.dataset.captures()[0]["card"], "Visas Samsara")  # and on the saved photos

        # the card list says this card never came as a Starlight: ask instead of counting it
        self.cam.seed = 99
        with named("Ketu Dracotail", printed=("secret", "ultra")):
            clash = self.show_card(2)
        self.assertEqual(clash["status"], "unsure")
        self.assertIn("card list", clash["text"])
        self.assertEqual(len(self.store.state()["log"]), 1)

        # not sure of the name: the hit counts, without a name, and the name can be set after
        self.cam.seed = 55
        with named("Solemn Judgment", sure=False):
            read = self.show_card(3)
        self.assertEqual(read["status"], "logged")
        self.assertIsNone(self.store.state()["log"][-1]["card"])
        self.assertEqual(read["nameCandidates"], ["Solemn Judgment", "Other Card"])
        self.scanner.name_read({"id": read["id"], "name": "Solemn Judgment"})
        self.assertEqual(self.store.state()["log"][-1]["card"], "Solemn Judgment")
        for body in ({"id": read["id"], "name": ""}, {"id": read["id"]}, {"id": "nope", "name": "X"}):
            with self.assertRaises(app.BadRequest):
                self.scanner.name_read(body)

    def test_test_mode(self):
        s = self.scanner.set_auto({"test": True})
        self.assertTrue(s["auto"]["on"] and s["auto"]["test"])  # test mode switches auto mode on
        res = {"name": "Solemn Judgment", "sure": True, "votes": 5.0, "share": 1.0,
               "candidates": [["Solemn Judgment", 5.0]], "printed": ["secret", "starlight", "ultra"]}
        with mock.patch.object(self.scanner, "_read_name", lambda cards, rarity: res):
            read = self.show_card(1)
        self.assertEqual((read["status"], read["test"]), ("logged", True))
        state = self.store.state()
        self.assertEqual([(e["rarity"], e["card"]) for e in state["testLog"]], [("starlight", "Solemn Judgment")])
        self.assertEqual(state["log"], [])  # nothing counted for real
        self.assertEqual(self.scanner.dataset.captures(), [])  # test reads aren't saved as photos on their own
        self.scanner.fix_read({"id": read["id"], "rarity": "ultra"})  # a fix stays in the test list
        state = self.store.state()
        self.assertEqual([(e["rarity"], e["card"]) for e in state["testLog"]], [("ultra", "Solemn Judgment")])
        self.assertEqual(state["log"], [])
        s = self.scanner.set_auto({"on": False})
        self.assertFalse(s["auto"]["test"])  # auto mode off ends test mode too
        with self.assertRaises(app.BadRequest):
            self.scanner.set_auto({"test": "yes"})

    def test_set_code(self):
        s = self.scanner.set_auto({"set": " cori "})
        self.assertEqual(s["names"]["set"], "CORI")
        s = self.scanner.set_auto({"set": None})
        self.assertIsNone(s["names"]["set"])
        for bad in ("C", "TOO-LONG", 5):
            with self.assertRaises(app.BadRequest):
                self.scanner.set_auto({"set": bad})

    def test_counter_offline(self):
        self.scanner.counter_url = NO_COUNTER
        self.scanner.set_auto({"on": True})
        read = self.show_card(1)
        self.assertEqual(read["status"], "offline")
        self.assertIn("isn't reachable", read["text"])


if __name__ == "__main__":
    unittest.main()
