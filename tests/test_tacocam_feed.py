"""The "Taco Cam (direct, full quality)" camera: reading Taco Cam's shared-memory feed."""
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from multiprocessing import shared_memory
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import cv2
    import numpy as np
    from scanner import app, tacocam_feed
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"scanner needs OpenCV + NumPy: {exc}")

NO_COUNTER = "http://127.0.0.1:9"


class FakeTacoCam:
    """Writes pictures the way Taco Cam does (feed v2: two slots, BGR upright, seq per slot), privately named."""

    SLOT = 640 * 480 * 3

    def __init__(self, name):
        self.shm = shared_memory.SharedMemory(name=name, create=True, size=128 + 2 * tacocam_feed.SLOT_BYTES)
        struct.pack_into("<4sIdI", self.shm.buf, 0, b"TCF2", 2, 0.0, 0)
        self.seq = [0, 0]

    def wanted(self):
        return struct.unpack_from("<d", self.shm.buf, 8)[0] > time.time()

    def write(self, rgb, turn=0, age=0.0):
        """Like Taco Cam: RGB in, stored as BGR turned clockwise by `turn`."""
        img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        rot = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(turn)
        if rot is not None:
            img = cv2.rotate(img, rot)
        h, w = img.shape[:2]
        i = 1 - struct.unpack_from("<I", self.shm.buf, 16)[0]
        off = 64 + 32 * i
        self.seq[i] += 1
        struct.pack_into("<Q", self.shm.buf, off, self.seq[i])
        np.frombuffer(self.shm.buf, np.uint8, count=w * h * 3, offset=128 + i * tacocam_feed.SLOT_BYTES)[:] = \
            img.reshape(-1)
        self.seq[i] += 1
        struct.pack_into("<QIIIId", self.shm.buf, off, self.seq[i], w, h, 2, 0, time.time() - age)
        struct.pack_into("<I", self.shm.buf, 16, i)

    def close(self):
        self.shm.close()
        self.shm.unlink()


def picture(w=320, h=180):
    """RGB: red left half, blue right half (so colour order and turning are both visible)."""
    img = np.zeros((h, w, 3), np.uint8)
    img[:, : w // 2] = (255, 0, 0)
    img[:, w // 2:] = (0, 0, 255)
    return img


def wait_for(fn, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = fn()
        if isinstance(got, np.ndarray) or got:
            return got
        time.sleep(0.02)
    return fn()


class TacoCamFeedTests(unittest.TestCase):
    def setUp(self):
        self.name = f"TacoCamFeedScannerTest{os.getpid()}_{time.monotonic_ns()}"
        self.taco = FakeTacoCam(self.name)

    def tearDown(self):
        self.taco.close()

    def test_reader_round_trip_and_asks_for_pictures(self):
        r = tacocam_feed.FeedReader(self.name)
        try:
            self.assertIsNone(r.read())          # nothing written yet
            self.assertTrue(self.taco.wanted())  # ...but Taco Cam has been asked
            self.taco.write(picture(), turn=90)
            img, pid = r.read()
            self.assertEqual(img.shape, (320, 180, 3))
            self.assertEqual(tuple(img[5, 90]), (0, 0, 255))
            self.taco.write(picture(), age=10)   # stale picture: ignored
            self.assertIsNone(r.read())
        finally:
            r.close()

    def test_source_gives_bgr_turned_upright(self):
        self.taco.write(picture(), turn=90)
        src = app.TacoCamSource(fps=30, feed_name=self.name)
        try:
            frame = wait_for(lambda: src.latest()[0])
            self.assertIsNotNone(frame, src.error)
            self.assertEqual(frame.shape, (320, 180, 3))           # turned 90 degrees clockwise
            self.assertEqual(tuple(frame[5, 90]), (0, 0, 255))      # top was the red left half; BGR red
            self.assertEqual(tuple(frame[-5, 90]), (255, 0, 0))     # bottom: blue
            self.assertEqual((src.index, src.name), ("tacocam", app.TACOCAM_NAME))
        finally:
            src.close()

    def test_source_explains_when_taco_cam_is_closed(self):
        src = app.TacoCamSource(fps=30, feed_name=self.name + "missing")
        try:
            self.assertTrue(wait_for(lambda: src.error))
            self.assertIn("isn't running", src.error)
        finally:
            src.close()

    def test_probe_lists_taco_cam_with_a_thumbnail(self):
        self.taco.write(picture(), turn=0)
        entry = app.probe_tacocam(self.name)
        self.assertEqual(entry["index"], "tacocam")
        self.assertEqual((entry["width"], entry["height"], entry["status"]), (320, 180, "ok"))
        self.assertTrue(entry["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertIsNone(app.probe_tacocam(self.name + "missing"))


class ScannerChoosesTacoCamTests(unittest.TestCase):
    def test_set_camera_accepts_tacocam_and_remembers_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = app.Scanner(tmp, demo=True, counter_url=NO_COUNTER)
            try:
                st = scanner.set_camera({"index": "tacocam"})
                self.assertEqual(st["camera"]["index"], "tacocam")
                self.assertEqual(st["camera"]["name"], app.TACOCAM_NAME)
                with self.assertRaises(app.BadRequest):
                    scanner.set_camera({"index": "tacocam2"})
            finally:
                scanner.close()
            saved = json.loads(Path(tmp, "scanner.json").read_text(encoding="utf-8")) \
                if Path(tmp, "scanner.json").exists() else None
            if saved is not None:
                self.assertEqual(saved.get("camera"), "tacocam")


if __name__ == "__main__":
    unittest.main()
