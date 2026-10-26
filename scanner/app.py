"""Card scanner, phase 1: show the camera with a card outline, find the card held up in
it, and record labeled training photos per rarity.

Training captures are saved as data/training/<rarity>/<capture id>/ with the straightened
card photos (00.jpg, 01.jpg, ...), one full camera frame (full.jpg) and meta.json.

    python -m scanner [--camera N] [--port 7778] [--demo]

Needs OpenCV and NumPy:  python -m pip install --user opencv-python numpy
"""

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import unquote, urlsplit

import cv2
import numpy as np

from . import detect, names, obsws, synth, tacocam_feed
from . import rarity as recog

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PAGE = HERE / "scanner.html"
DEFAULT_DATA = ROOT / "data"
COUNTER_URL = "http://127.0.0.1:7777"
COUNTER_TIMEOUT = 10        # seconds for a request to the counter

DEFAULT_RARITIES = [
    {"id": "ultra", "label": "Ultra Rare", "short": "UR", "color": "#f5c542"},
    {"id": "secret", "label": "Secret Rare", "short": "SCR", "color": "#c0c8d8"},
    {"id": "overframe", "label": "Overframe", "short": "OF", "color": "#ff6b35"},
    {"id": "starlight", "label": "Starlight Rare", "short": "STR", "color": "#7fd4ff"},
    {"id": "starlight_overframe", "label": "Starlight Overframe", "short": "SOF", "color": "#d48cff"},
]
HIT_IDS = [r["id"] for r in DEFAULT_RARITIES]
# Commons, rares, supers...: captured too, so auto mode learns what to ignore. Never sent to the counter.
NOT_HIT = {"id": "none", "label": "Not a hit", "short": "\u2014", "color": "#8a90a0"}
RARITY_IDS = HIT_IDS + [NOT_HIT["id"]]   # what a capture can be labelled as

FRAMES_PER_CAPTURE = 20     # photos kept per capture
GUESS_FRAMES = 10           # photos for a quick guess (nothing saved)
LIVE_WINDOW = 50            # live accuracy is over each rarity's latest guesses
MIN_FRAMES = 8              # fewer than this and the capture is thrown away
CAPTURE_SECONDS = 6.0       # longest a capture runs
KEEP_INTERVAL = 0.12        # seconds between kept photos (so the tilt varies)
MIN_SHARP = 40.0            # blurrier photos are skipped
FPS_CHOICES = (10, 15, 24, 30)
DEFAULT_FPS = 30
PREVIEW_WIDTH = 960
CARD_MAX = 80
# Auto mode: read each card held up and log the hits on the counter.
AUTO_STEADY = 0.5           # seconds a card is in view before it's read
AUTO_BLINK = 0.25           # detection gaps shorter than this don't count as the card leaving
AUTO_GONE = 0.7             # seconds out of view before the same card would be read again
AUTO_CHANGED = 0.4          # a card in view this unlike the one just read is a different card...
AUTO_CHANGED_FRAMES = 10    # ...once it stays unlike it for this many frames
AUTO_SAME = 0.85            # a read this like a hit added in the last AUTO_REPEAT_SECONDS is that card shown again
AUTO_REPEAT_SECONDS = 180
AUTO_THRESHOLDS = (0.6, 0.7, 0.8, 0.85, 0.9, 0.95)
DEFAULT_AUTO_THRESHOLD = 0.85
AUTO_KEEP = 24              # latest reads kept for review (in memory); reads waiting for an answer are kept too...
AUTO_KEEP_MAX = 100         # ...up to this many reads in all
RETRAIN_GAP = 30            # while streaming, retrain at most this often (seconds), and skip the slow held-out test
SET_CODE = re.compile(r"^[A-Z0-9]{2,6}$")   # set code prefix, e.g. CORI from CORI-EN092
CAPTURE_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
MAX_CAMERA_INDEX = 6


class BadRequest(Exception):
    pass


# ---- frame sources -----------------------------------------------------------

class CameraSource:
    """Reads a camera on its own thread and keeps only the latest frame."""

    def __init__(self, index):
        self.index = index
        self.name = f"Camera {index}"
        self.error = None
        self._frame, self._ts = None, 0.0
        self._stop = threading.Event()
        threading.Thread(target=self._run, name=f"camera-{index}", daemon=True).start()

    def _run(self):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.index, backend)
        if not cap.isOpened():
            self.error = f"Can't open camera {self.index}. It may be unplugged or in use by an app that won't share it."
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        cap.set(cv2.CAP_PROP_FPS, 30)
        fails = 0
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 40:
                        self.error = f"Camera {self.index} stopped sending pictures."
                    time.sleep(0.05)
                    continue
                fails = 0
                self.error = None
                self._frame, self._ts = frame, time.monotonic()
        finally:
            cap.release()

    def latest(self):
        return self._frame, self._ts

    def close(self):
        self._stop.set()


OBS_PREFIX = "obs:"
OBS_CAMERA_KINDS = {"dshow_input", "av_capture_input", "v4l2_input", "macos-avcapture", "ndi_source"}
_ROTATIONS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def _obs_rotation(obs, source_name):
    """How the source is turned in OBS's live scene (0/90/180/270), so frames come out
    the way viewers see them."""
    try:
        scene = obs.request("GetCurrentProgramScene").get("currentProgramSceneName")
        items = obs.request("GetSceneItemList", {"sceneName": scene}).get("sceneItems", [])
    except obsws.ObsError:
        return 0
    for item in items:
        if item.get("sourceName") == source_name:
            return int(round(item.get("sceneItemTransform", {}).get("rotation", 0) / 90.0)) % 4 * 90
    return 0


def _obs_frame(obs, source_name, quality=92):
    data = obs.request("GetSourceScreenshot", {"sourceName": source_name, "imageFormat": "jpg",
                                               "imageCompressionQuality": quality})["imageData"]
    img = cv2.imdecode(np.frombuffer(base64.b64decode(data.split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise obsws.ObsError("OBS sent an unreadable frame")
    return img


class ObsSource:
    """Frames of a camera source straight from OBS (via obs-websocket), so OBS keeps the
    camera. Rotated the way the source is turned in the live scene.

    One OBS frame takes ~60 ms at 1080p (OBS compresses it first), so several connections
    fetch in turn: 2 of them reach ~36 fps."""

    WORKERS = 2

    def __init__(self, source_name, fps=DEFAULT_FPS):
        self.source_name = source_name
        self.index = OBS_PREFIX + source_name
        self.name = f"OBS: {source_name}"
        self.fps = fps
        self.error = None
        self._frame, self._ts = None, 0.0
        self._rotation = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        for n in range(self.WORKERS):
            threading.Thread(target=self._run, args=(n,), name=f"obs-source-{n}", daemon=True).start()

    def _run(self, worker):
        # Stagger the workers so frames arrive evenly spaced.
        self._stop.wait(worker / max(1, self.fps))
        while not self._stop.is_set():
            try:
                with obsws.ObsClient() as obs:
                    checked = 0.0
                    while not self._stop.is_set():
                        started = time.monotonic()
                        if worker == 0 and started - checked > 10:
                            self._rotation, checked = _obs_rotation(obs, self.source_name), started
                        img = _obs_frame(obs, self.source_name)
                        if self._rotation in _ROTATIONS:
                            img = cv2.rotate(img, _ROTATIONS[self._rotation])
                        with self._lock:
                            self._frame, self._ts, self.error = img, time.monotonic(), None
                        interval = self.WORKERS / max(1, self.fps)  # each worker supplies 1/WORKERS of the frames
                        self._stop.wait(max(0.0, interval - (time.monotonic() - started)))
            except (OSError, obsws.ObsError) as exc:
                self.error = f"Can't get {self.source_name!r} from OBS: {exc}. Is OBS open?"
                self._stop.wait(2.0)

    def latest(self):
        with self._lock:
            return self._frame, self._ts

    def close(self):
        self._stop.set()


TACOCAM = "tacocam"
TACOCAM_NAME = "Taco Cam (direct, full quality)"


class TacoCamSource:
    """The iPhone's full-quality picture straight from Taco Cam (shared memory, tacocam_feed.py): no OBS
    screenshot, no JPEG, and 4K when the phone sends 4K. Taco Cam stores it as BGR, already turned upright
    (its clip/scanner turn), so this only copies it (~8 ms at 4K)."""

    def __init__(self, fps=DEFAULT_FPS, feed_name=None):
        self.index = TACOCAM
        self.name = TACOCAM_NAME
        self.fps = fps
        self.feed_name = feed_name or tacocam_feed.FEED_NAME
        self.error = None
        self._frame, self._ts = None, 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._run, name="tacocam-source", daemon=True).start()

    def _run(self):
        reader, last = None, None
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                if reader is None:
                    try:
                        reader = tacocam_feed.FeedReader(self.feed_name)
                    except (FileNotFoundError, ValueError, OSError):
                        self.error = "Taco Cam isn't running on this PC. Open Taco Cam."
                        self._stop.wait(2.0)
                        continue
                got = reader.read()
                if got is None:
                    self.error = "Taco Cam has no picture from the iPhone right now."
                    self._stop.wait(0.2)
                    continue
                img, picture_id = got
                if picture_id != last:
                    last = picture_id
                    with self._lock:
                        self._frame, self._ts, self.error = img, time.monotonic(), None
                self._stop.wait(max(0.0, 1.0 / max(1, self.fps) - (time.monotonic() - started)))
        finally:
            if reader is not None:
                reader.close()

    def latest(self):
        with self._lock:
            return self._frame, self._ts

    def close(self):
        self._stop.set()


def probe_tacocam(feed_name=None):
    """The Taco Cam entry for the camera list (with a thumbnail), or None when Taco Cam isn't running."""
    try:
        reader = tacocam_feed.FeedReader(feed_name or tacocam_feed.FEED_NAME)
    except (FileNotFoundError, ValueError, OSError):
        return None
    entry = {"index": TACOCAM, "name": TACOCAM_NAME, "status": "black", "width": None, "height": None, "thumb": None}
    try:
        end = time.monotonic() + 1.5            # Taco Cam starts copying pictures once it is asked
        got = None
        while got is None and time.monotonic() < end:
            got = reader.read()
            if got is None:
                time.sleep(0.1)
        if got is not None:
            img = got[0]
            h, w = img.shape[:2]
            entry.update(width=w, height=h, status="black" if img.mean() < 4 else "ok")
            thumb = cv2.resize(img, (192, max(1, int(192 * h / w))))
            ok, jpg = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                entry["thumb"] = "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()
    finally:
        reader.close()
    return entry


def make_source(spec, fps=DEFAULT_FPS):
    if spec == TACOCAM:
        return TacoCamSource(fps)
    if isinstance(spec, str) and spec.startswith(OBS_PREFIX):
        return ObsSource(spec[len(OBS_PREFIX):], fps)
    return CameraSource(int(spec))


def probe_obs():
    """Camera sources in OBS, each with a thumbnail, or [] when OBS isn't reachable."""
    out = []
    try:
        with obsws.ObsClient(timeout=3) as obs:
            inputs = obs.request("GetInputList").get("inputs", [])
            for item in inputs:
                if item.get("inputKind") not in OBS_CAMERA_KINDS:
                    continue
                name = item["inputName"]
                entry = {"index": OBS_PREFIX + name, "name": "OBS: " + name, "status": "unavailable",
                         "width": None, "height": None, "thumb": None}
                try:
                    img = _obs_frame(obs, name, quality=70)
                    rotation = _obs_rotation(obs, name)
                    if rotation in _ROTATIONS:
                        img = cv2.rotate(img, _ROTATIONS[rotation])
                    h, w = img.shape[:2]
                    entry.update(width=w, height=h, status="black" if img.mean() < 4 else "ok")
                    thumb = cv2.resize(img, (192, max(1, int(192 * h / w))))
                    ok, jpg = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok:
                        entry["thumb"] = "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()
                except obsws.ObsError:
                    pass
                out.append(entry)
    except (OSError, obsws.ObsError):
        pass
    return out


class DemoSource:
    """A synthetic card being held up and tilted, cycling through the rarities."""

    name = "Demo camera (synthetic card)"
    index = -1
    error = None

    def __init__(self):
        self._t0 = time.monotonic()

    def latest(self):
        now = time.monotonic()
        t = now - self._t0
        rarity = HIT_IDS[int(t // 12) % len(HIT_IDS)]
        present = (t % 12) < 10.5  # card lowered for a moment every 12 s
        frame, _ = synth.make_frame(t, rarity=rarity, present=present)
        return frame, now

    def close(self):
        pass


def probe_camera(index, timeout=20.0):
    """Open a camera briefly: {"index", "status": ok|black|unavailable, "width", "height", "thumb"}."""
    result = {"index": index, "status": "unavailable", "width": None, "height": None, "thumb": None}
    done = threading.Event()

    def run():
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(index, backend)
        try:
            if not cap.isOpened():
                return
            frame = None
            for _ in range(12):  # let exposure settle
                ok, f = cap.read()
                if ok and f is not None:
                    frame = f
            if frame is None:
                return
            h, w = frame.shape[:2]
            result.update(width=w, height=h, status="black" if frame.mean() < 4 else "ok")
            thumb = cv2.resize(frame, (192, int(192 * h / w)))
            ok, jpg = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                result["thumb"] = "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()
        finally:
            cap.release()
            done.set()

    threading.Thread(target=run, daemon=True).start()
    done.wait(timeout)
    return result


# ---- training data -----------------------------------------------------------

class Dataset:
    """data/training/<rarity>/<capture id>/{00.jpg.., full.jpg, meta.json}"""

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    def save(self, rarity, card, frames, full_jpg, extra):
        cid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        folder = self.root / rarity / cid
        with self._lock:
            folder.mkdir(parents=True, exist_ok=False)
            listed = []
            for i, (jpg, info) in enumerate(frames):
                name = f"{i:02d}.jpg"
                (folder / name).write_bytes(jpg)
                listed.append({"file": name, **info})
            if full_jpg is not None:
                (folder / "full.jpg").write_bytes(full_jpg)
            meta = {"id": cid, "rarity": rarity, "card": card, "created": time.time(), "frames": listed, **extra}
            # default=float: a NumPy number in the meta must never cost the user a capture
            (folder / "meta.json").write_text(json.dumps(meta, indent=1, default=float), encoding="utf-8")
        return meta

    def captures(self):
        out = []
        with self._lock:
            if not self.root.is_dir():
                return out
            for meta_path in self.root.glob("*/*/meta.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(meta, dict) and CAPTURE_ID.match(str(meta.get("id", ""))):
                    out.append(meta)
        out.sort(key=lambda m: m.get("created", 0), reverse=True)
        return out

    def summary(self, captures=None):
        counts = {rid: {"cards": 0, "frames": 0, "stream": 0} for rid in RARITY_IDS}
        for meta in captures if captures is not None else self.captures():
            c = counts.setdefault(meta.get("rarity", "?"), {"cards": 0, "frames": 0, "stream": 0})
            c["cards"] += 1
            c["frames"] += len(meta.get("frames", []))
            c["stream"] += meta.get("source") == "auto"
        return counts

    def update_meta(self, cid, **fields):
        folder = self.find(cid)
        if folder is None:
            return False
        with self._lock:
            path = folder / "meta.json"
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta.update(fields)
            path.write_text(json.dumps(meta, indent=1, default=float), encoding="utf-8")
        return True

    def find(self, cid):
        if not CAPTURE_ID.match(str(cid)):
            return None
        for folder in self.root.glob(f"*/{cid}"):
            if folder.is_dir():
                return folder
        return None

    def delete(self, cid):
        folder = self.find(cid)
        if folder is None:
            return False
        with self._lock:
            shutil.rmtree(folder)
        return True

    def file(self, rarity, cid, name):
        """Path of one saved image, or None (guards against path tricks)."""
        if rarity not in RARITY_IDS or not CAPTURE_ID.match(cid) or not re.fullmatch(r"(\d{2}|full)\.jpg", name):
            return None
        path = self.root / rarity / cid / name
        return path if path.is_file() else None


# ---- the scanner -------------------------------------------------------------

class Scanner:
    def __init__(self, data_dir, camera=None, demo=False, counter_url=COUNTER_URL):
        self.data_dir = Path(data_dir)
        self.dataset = Dataset(self.data_dir / "training")
        self.debug_dir = self.data_dir / "debug"
        self._last_frame = None  # newest raw camera frame, for debug saves
        self.config_path = self.data_dir / "scanner.json"
        self.counter_url = counter_url
        self.config = {"camera": 0, "guide": "medium", "allowGuide": False, "fps": DEFAULT_FPS,
                       "autoThreshold": DEFAULT_AUTO_THRESHOLD, "set": None}
        self._load_config()
        if camera is not None:
            self.config["camera"] = camera
        # Re-entrant: status() takes it too, so a caller already holding it can't deadlock.
        self.lock = threading.RLock()
        self.preview_cond = threading.Condition()
        self.preview, self.preview_seq = None, 0
        self.detection = {"found": False, "method": None, "aspect": None}
        self.quality = {"sharp": 0.0, "bright": 0.0, "glare": 0.0}
        self.frame_info = {"width": None, "height": None, "black": False}
        self.fps = 0.0
        self.detect_fps = 0.0
        self._det_full = {"found": False}
        self.capture = None
        self.message = None
        self.cameras, self.scanning = [], False
        self.rarities, self.counter_ok = DEFAULT_RARITIES + [NOT_HIT], False
        # Auto mode starts off on every launch, so a practice session never adds hits by surprise.
        self.auto_on = False
        # Test mode: auto mode posting to the counter's test list (shown, never counted, not on stream).
        self.test_mode = False
        self.auto = _auto_idle()
        self.auto_reads = []  # newest first
        self.auto_learned = 0  # reads saved to the training photos this session
        self.retrain_gap = RETRAIN_GAP
        self._captures_cache = None
        self.source = DemoSource() if demo else make_source(self.config["camera"], self.config["fps"])
        self._stop = threading.Event()
        threading.Thread(target=self._process_loop, name="scanner-preview", daemon=True).start()
        threading.Thread(target=self._detect_loop, name="scanner-detect", daemon=True).start()
        threading.Thread(target=self._rarity_loop, name="rarities", daemon=True).start()
        # rarity recognition: retrained in the background whenever the training photos change
        self.model = None
        self.recognition = {"ready": False, "counts": {}, "evaluation": None, "signals": [], "training": True}
        self.guess_log_path = self.data_dir / "guesses.jsonl"
        self.guess_log = self._load_guess_log()
        # card names: the card list from YGOPRODeck (refreshed weekly; not in demo mode)
        self.card_list = names.CardList(self.data_dir / "cards.json")
        if not demo and self.card_list.stale():
            threading.Thread(target=self.card_list.download, name="card-list", daemon=True).start()
        self._retrain = threading.Event()
        self._retrain.set()
        threading.Thread(target=self._train_loop, name="train", daemon=True).start()

    # -- config
    def _load_config(self):
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            camera = raw.get("camera")
            if (isinstance(camera, int) and not isinstance(camera, bool)) or camera == TACOCAM or (
                    isinstance(camera, str) and camera.startswith(OBS_PREFIX) and len(camera) > len(OBS_PREFIX)):
                self.config["camera"] = camera
            if raw.get("guide") in detect.GUIDE_SIZES:
                self.config["guide"] = raw["guide"]
            if isinstance(raw.get("allowGuide"), bool):
                self.config["allowGuide"] = raw["allowGuide"]
            if raw.get("fps") in FPS_CHOICES:
                self.config["fps"] = raw["fps"]
            if raw.get("autoThreshold") in AUTO_THRESHOLDS:
                self.config["autoThreshold"] = raw["autoThreshold"]
            if isinstance(raw.get("set"), str) and SET_CODE.match(raw["set"]):
                self.config["set"] = raw["set"]

    def _save_config(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(self.config, indent=1), encoding="utf-8")
        except OSError as exc:
            print(f"[scanner] could not save {self.config_path}: {exc}", file=sys.stderr)

    # -- rarity names/colours from the counter
    def _rarity_loop(self):
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.counter_url + "/api/state", timeout=3) as resp:
                    state = json.loads(resp.read().decode("utf-8"))
                rarities = [r for r in state.get("rarities", []) if r.get("id") in HIT_IDS]
                if len(rarities) == len(HIT_IDS):
                    self.rarities = rarities + [NOT_HIT]
                self.counter_ok = True
            except (OSError, ValueError):
                self.counter_ok = False
            self._stop.wait(20)

    # -- main loop: grab, find the card, capture, draw the preview
    def _process_loop(self):
        """Preview loop: draws and sends the newest camera frame at the camera's rate, with
        the latest detection drawn on it. Card finding runs in _detect_loop on its own, so a
        slow detection never slows the picture down."""
        last_ts, frames, t_fps = 0.0, 0, time.monotonic()
        while not self._stop.is_set():
            started = time.monotonic()
            frame, ts = self.source.latest()
            if frame is None or ts == last_ts:
                if frame is None:
                    self._publish(self._placeholder(self.source.error or "Waiting for the camera..."))
                    time.sleep(0.25)
                else:
                    time.sleep(0.005)
                continue
            last_ts = ts
            self._last_frame = frame
            with self.lock:
                det = dict(self._det_full)
                capture = dict(self.capture) if self.capture else None
                black = self.frame_info["black"]
            self._publish(self._annotate(frame, det, capture, black))
            frames += 1
            if started - t_fps >= 1.0:
                self.fps, frames, t_fps = frames / (started - t_fps), 0, started
            time.sleep(max(0.0, 1.0 / self.config["fps"] - (time.monotonic() - started)))

    def _detect_loop(self):
        """Finds the card on the newest frame as fast as it can, and feeds captures (each kept
        photo is cut from the exact frame its corners were found on)."""
        last_ts, frames, t_fps = 0.0, 0, time.monotonic()
        tick, tracking = 0, False
        while not self._stop.is_set():
            started = time.monotonic()
            frame, ts = self.source.latest()
            if frame is None or ts == last_ts:
                time.sleep(0.005)
                continue
            last_ts = ts
            h, w = frame.shape[:2]
            guide = detect.guide_box(w, h, self.config["guide"])
            # The dark-card passes are slow: run them every frame while a card is in view,
            # otherwise every third frame (a dark card is still picked up within ~0.1 s).
            tick += 1
            det = detect.find_card(frame, guide, deep=tracking or tick % 3 == 0)
            tracking = det["found"]
            quad = det["quad"] if det["found"] else detect.guide_quad(guide)
            card = detect.warp_card(frame, quad)
            q = detect.quality(card)
            black = float(frame[::8, ::8].mean()) < 4
            with self.lock:
                self.detection = {k: det[k] for k in ("found", "method", "aspect")}
                self._det_full = {**det, "guide": guide, "size": (w, h)}
                self.quality = q
                self.frame_info = {"width": w, "height": h, "black": black}
                if self.capture is not None:
                    self._capture_step(frame, card, det, q, black)
                if self.auto_on:
                    self._auto_step(det, card, q, black)
            frames += 1
            if started - t_fps >= 1.0:
                self.detect_fps, frames, t_fps = frames / (started - t_fps), 0, started

    def _capture_step(self, frame, card, det, q, black):
        c = self.capture
        now = time.monotonic()
        outline_ok = self.config["allowGuide"] and not c["auto"]  # auto reads need the real card edge
        usable = (det["found"] or outline_ok) and q["sharp"] >= MIN_SHARP and not black
        if usable and now - c["last_keep"] >= KEEP_INTERVAL:
            ok, jpg = cv2.imencode(".jpg", card, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if ok:
                quad = det["quad"] if det["found"] else None
                corners = [[round(float(v), 1) for v in pt] for pt in quad] if quad is not None else None
                c["frames"].append((jpg.tobytes(), {"method": det["method"] or "outline", "corners": corners, **q}))
                c["last_keep"] = now
                if c["full"] is None:
                    ok, full = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    c["full"] = full.tobytes() if ok else None
        c["kept"] = len(c["frames"])
        if c["kept"] >= c["target"] or now >= c["deadline"]:
            self._finish_capture()

    def _finish_capture(self):
        c, self.capture = self.capture, None
        c["frames"], dropped = _drop_glitches(c["frames"])
        now = time.monotonic()
        if self.auto_on and len(c["frames"]) >= GUESS_FRAMES // 2:
            # The card just read (or captured by hand) isn't read again until it's put down.
            c["sig"] = _capture_signature(c["frames"])
            self.auto = {**_auto_idle(), "phase": "holding", "sig": c["sig"], "since": now, "last": now}
        if c["auto"]:
            if len(c["frames"]) < GUESS_FRAMES // 2:  # moved away or blurred: try again once it settles
                self.auto = {**_auto_idle(), "phase": "steady", "since": now, "last": now}
            else:
                threading.Thread(target=self._auto_read, args=(c,), name="auto-read", daemon=True).start()
            return
        if len(c["frames"]) < (MIN_FRAMES if c["save"] else GUESS_FRAMES // 2):
            self._save_debug(f"capture-failed-{c['rarity'] or 'guess'}")
            self.message = {"ok": False, "at": time.time(), "text":
                            f"Not saved: only {len(c['frames'])} sharp photos with the card found. Line the card "
                            "up with the outline, hold it a little steadier and try again. (The camera frame "
                            "was kept in data/debug so detection can be tuned for this card.)"}
            return
        # Measuring the photos and saving takes a moment: do it off the camera loop.
        self.message = {"ok": True, "pending": True, "at": time.time(),
                        "text": "Saving and guessing\u2026" if c["save"] else "Guessing\u2026"}
        extra = {"camera": self.source.name, "guide": self.config["guide"],
                 "frameSize": [self.frame_info["width"], self.frame_info["height"]]}
        threading.Thread(target=self._after_capture, args=(c, extra), name="after-capture", daemon=True).start()

    def _after_capture(self, c, extra):
        try:
            cards = [cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR) for jpg, _ in c["frames"]]
            vec = recog.capture_features([x for x in cards if x is not None])
            with self.lock:
                model = self.model
            guess = model.predict(vec) if model is not None and model.ready else None
            best = next(iter(guess)) if guess else None
            reading = self._marker_reading(model, vec) if guess else ""
            name = self._read_name([x for x in cards if x is not None], c["rarity"] or best)
            if c["save"] and not c["card"] and name and name["sure"]:
                c["card"] = name["name"]  # no name typed: use the one read off the card
            if not c["save"]:  # quick guess only
                if guess is None:
                    text = "Can't guess yet: it needs at least 3 captures of 2 different rarities to compare."
                else:
                    others = ", ".join(f"{self._label(r)} {round(p * 100)}%" for r, p in list(guess.items())[1:3] if p > 0)
                    text = f"Looks like {self._label(best)} ({round(guess[best] * 100)}%)" + (f" \u00b7 {others}" if others else "")
                    if reading:
                        text += f" ({reading})"
                if name:
                    text += f" Name: {name['name']}" + ("." if name["sure"] else " (not sure).")
                with self.lock:
                    self.message = {"ok": True, "at": time.time(), "guess": guess, "text": text}
                return
            if guess:
                extra["guess"] = {"rarity": best, "probabilities": guess}
            meta = self.dataset.save(c["rarity"], c["card"], c["frames"], c["full"], extra)
            folder = self.dataset.find(meta["id"])
            if folder is not None:
                recog.save_capture_features(folder, vec)
            label = self._label(c["rarity"])
            who = f" \u2014 {c['card']}" if c["card"] else ""
            text = f"Saved {len(c['frames'])} photos of {label}{who}."
            if guess:
                right = best == c["rarity"]
                text += f" It guessed {self._label(best)} ({round(guess[best] * 100)}%): {'right' if right else 'wrong'}."
                if reading:
                    text += f" ({reading})"
                self._log_guess(meta["id"], c["rarity"], best, guess[best])
            with self.lock:
                self._captures_cache = None
                self.message = {"ok": True, "at": time.time(), "id": meta["id"], "guess": guess, "text": text}
            self._retrain.set()
        except Exception as exc:  # never leave the page stuck on "Saving..."
            with self.lock:
                self.message = {"ok": False, "at": time.time(), "text": f"Saving failed: {exc}"}

    # -- recognition
    def _train_loop(self):
        last = 0.0
        while not self._stop.is_set():
            if not self._retrain.wait(1.0):
                continue
            if self.auto_on and time.monotonic() - last < self.retrain_gap:
                self._stop.wait(1.0)  # streaming: gather the new captures, retrain at most every RETRAIN_GAP s
                continue
            self._retrain.clear()
            last, streaming = time.monotonic(), self.auto_on
            with self.lock:
                self.recognition = {**self.recognition, "training": True}
            try:
                X, y, _ = recog.load_training(self.dataset.root)
                counts = {r: int((y == r).sum()) for r in sorted(set(y.tolist()))}
                model = recog.RarityModel().fit(X, y) if len(y) else None
                ready = model is not None and model.ready
                # The held-out test refits the model once per capture: too much CPU while streaming.
                # It's rerun when auto mode is turned off.
                stale = ready and streaming
                evaluation = recog.evaluate(X, y) if ready and not streaming else None
                with self.lock:
                    if stale:
                        evaluation = self.recognition.get("evaluation")
                    self.model = model
                    self.recognition = {"ready": ready, "counts": counts, "evaluation": evaluation,
                                        "evaluationStale": stale,
                                        "signals": model.top_signals() if ready else [], "training": False,
                                        "markers": model.marker_info() if model is not None else {}}
            except Exception as exc:
                print(f"[scanner] training failed: {exc}", file=sys.stderr)
                with self.lock:
                    self.recognition = {**self.recognition, "training": False, "error": str(exc)}

    def _load_guess_log(self):
        out = []
        try:
            for line in self.guess_log_path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def _log_guess(self, cid, truth, guess, confidence):
        entry = {"id": cid, "truth": truth, "guess": guess, "confidence": round(float(confidence), 3), "time": time.time()}
        with self.lock:
            self.guess_log.append(entry)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.guess_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def _live_accuracy(self):
        """Per rarity: how often the guess made before saving was right, over its latest guesses."""
        live = {}
        for e in self.guess_log:
            live.setdefault(e["truth"], []).append(e["guess"] == e["truth"])
        return {r: {"right": sum(v[-LIVE_WINDOW:]), "total": len(v[-LIVE_WINDOW:])} for r, v in live.items()}

    DEBUG_KEEP = 80  # newest debug frames kept

    def _save_debug(self, reason):
        """Keep the current raw camera frame (and what detection made of it) in data/debug,
        so cards the scanner struggles with can be looked at and the detection tuned."""
        frame = self._last_frame
        if frame is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2) + "-" + re.sub(r"[^a-z0-9_-]", "", reason)
            cv2.imwrite(str(self.debug_dir / (name + ".jpg")), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            info = {"reason": reason, "time": time.time(), "camera": self.source.name, "guide": self.config["guide"],
                    "detection": {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in self.detection.items()},
                    "quality": self.quality}
            (self.debug_dir / (name + ".json")).write_text(json.dumps(info, indent=1), encoding="utf-8")
            old = sorted(self.debug_dir.glob("*.jpg"))[:-self.DEBUG_KEEP]
            for f in old:
                f.unlink(missing_ok=True)
                f.with_suffix(".json").unlink(missing_ok=True)
            return name
        except OSError as exc:
            print(f"[scanner] could not save debug frame: {exc}", file=sys.stderr)
            return None

    def debug_frame(self, body):
        note = body.get("note") or "manual"
        if not isinstance(note, str):
            raise BadRequest("'note' must be a string")
        with self.lock:
            name = self._save_debug(note[:40].lower().replace(" ", "-"))
            if name is None:
                raise BadRequest("no camera frame to save yet")
            self.message = {"ok": True, "at": time.time(),
                            "text": "Frame saved for tuning (data/debug). Thanks — try a few angles of the same card."}
        return self.status()

    def _label(self, rid):
        return next((r["label"] for r in self.rarities if r["id"] == rid), rid)

    # -- drawing
    def _placeholder(self, text):
        img = np.full((540, PREVIEW_WIDTH, 3), 30, np.uint8)
        y = 250
        for line in _wrap(text, 52):
            cv2.putText(img, line, (40, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2, cv2.LINE_AA)
            y += 40
        return img

    def _annotate(self, frame, det, capture, black):
        # Shrink first, then draw: drawing on a full 1080p frame is the slow part.
        scale = min(1.0, PREVIEW_WIDTH / max(frame.shape[:2]))  # longer side at most PREVIEW_WIDTH, never enlarged
        if scale < 1.0:
            img = cv2.resize(frame, (int(frame.shape[1] * scale), int(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        else:
            img = frame.copy()
        fh, fw = frame.shape[:2]
        guide = det.get("guide") if det.get("size") == (fw, fh) else None
        x, y, w, h = guide or detect.guide_box(fw, fh, self.config["guide"])
        cv2.rectangle(img, (int(x * scale), int(y * scale)), (int((x + w) * scale), int((y + h) * scale)),
                      (255, 255, 255), 2, cv2.LINE_AA)
        if det.get("found") and det.get("size") == (fw, fh):
            colour = (90, 220, 90) if det["method"] in ("edges", "expanded") else (60, 200, 230)
            cv2.polylines(img, [(det["quad"] * scale).astype(np.int32)], True, colour, 3, cv2.LINE_AA)
        if capture:
            cv2.rectangle(img, (3, 3), (img.shape[1] - 4, img.shape[0] - 4), (60, 60, 240), 6)
        if black:
            cv2.putText(img, "This camera is sending a black picture", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 80, 255), 2, cv2.LINE_AA)
        return img

    def _publish(self, img):
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 72])
        if not ok:
            return
        with self.preview_cond:
            self.preview, self.preview_seq = jpg.tobytes(), self.preview_seq + 1
            self.preview_cond.notify_all()

    def wait_preview(self, after_seq, timeout=2.0):
        with self.preview_cond:
            self.preview_cond.wait_for(lambda: self.preview_seq != after_seq or self._stop.is_set(), timeout)
            return self.preview, self.preview_seq

    # -- actions
    def start_capture(self, body):
        guess_only = body.get("guess") is True
        rarity = body.get("rarity")
        if guess_only:
            rarity = None
        elif rarity not in RARITY_IDS:
            raise BadRequest("'rarity' must be one of: " + ", ".join(RARITY_IDS))
        card = body.get("card")
        if card is not None and not isinstance(card, str):
            raise BadRequest("'card' must be a string")
        card = (card or "").strip()[:CARD_MAX] or None
        with self.lock:
            if self.capture is not None and not self.capture["auto"]:  # a capture by hand beats an auto read
                raise BadRequest("a capture is already running")
            self.capture = _new_capture(rarity, card, guess_only)
            self.message = None
        return self.status()

    def cancel_capture(self, body):
        with self.lock:
            if self.capture is not None:
                self.capture = None
                self.message = {"ok": False, "at": time.time(), "text": "Capture cancelled."}
        return self.status()

    def set_camera(self, body):
        index = body.get("index")
        is_obs = isinstance(index, str) and index.startswith(OBS_PREFIX) and 0 < len(index) - len(OBS_PREFIX) <= 200
        is_cam = isinstance(index, int) and not isinstance(index, bool) and 0 <= index <= MAX_CAMERA_INDEX
        if not (is_obs or is_cam or index == TACOCAM):
            raise BadRequest(f"'index' must be a camera number from 0 to {MAX_CAMERA_INDEX}, \"obs:<source name>\" "
                             f"or \"{TACOCAM}\"")
        with self.lock:
            self.capture = None
            old, self.source = self.source, make_source(index, self.config["fps"])
            self.config["camera"] = index
        old.close()
        self._save_config()
        return self.status()

    def set_settings(self, body):
        guide, allow, fps = body.get("guide"), body.get("allowGuide"), body.get("fps")
        if guide is not None and guide not in detect.GUIDE_SIZES:
            raise BadRequest("'guide' must be one of: " + ", ".join(detect.GUIDE_SIZES))
        if allow is not None and not isinstance(allow, bool):
            raise BadRequest("'allowGuide' must be true or false")
        if fps is not None and (isinstance(fps, bool) or fps not in FPS_CHOICES):
            raise BadRequest("'fps' must be one of: " + ", ".join(map(str, FPS_CHOICES)))
        if guide is None and allow is None and fps is None:
            raise BadRequest("send 'guide', 'allowGuide' and/or 'fps'")
        with self.lock:
            if guide is not None:
                self.config["guide"] = guide
            if allow is not None:
                self.config["allowGuide"] = allow
            if fps is not None:
                self.config["fps"] = fps
                if hasattr(self.source, "fps"):
                    self.source.fps = fps
        self._save_config()
        return self.status()

    def scan_cameras(self, body):
        with self.lock:
            already = self.scanning
            if not already:
                self.scanning, self.cameras = True, []
        if already:  # a search is running; just report on it
            return self.status()

        def run():
            try:
                taco = probe_tacocam()  # Taco Cam's full-quality feed first, when Taco Cam is running
                found = ([taco] if taco else []) + probe_obs()  # then OBS sources: OBS stays in charge
                with self.lock:
                    self.cameras = found
                for i in range(MAX_CAMERA_INDEX):
                    if isinstance(self.source, CameraSource) and i == self.source.index:
                        frame, _ = self.source.latest()
                        entry = {"index": i, "status": "selected", "width": None, "height": None, "thumb": None}
                        if frame is not None:
                            entry["height"], entry["width"] = frame.shape[:2]
                    else:
                        entry = probe_camera(i)
                    with self.lock:
                        self.cameras = self.cameras + [entry]
            finally:
                with self.lock:
                    self.scanning = False

        threading.Thread(target=run, name="camera-scan", daemon=True).start()
        return self.status()

    def delete_capture(self, body):
        cid = body.get("id")
        with self.lock:
            if not isinstance(cid, str) or not self.dataset.delete(cid):
                raise BadRequest("no capture with that id")
            self._captures_cache = None
        self._retrain.set()
        return self.status()

    # -- auto mode: read each card held up and log the hits on the counter
    def _auto_step(self, det, card, q, black):
        """Once per detected frame (under the lock): wait for a card to settle, read it once,
        then wait for it to leave, or a different card to replace it, before reading again."""
        a, now = self.auto, time.monotonic()
        found = det["found"] and not black
        if found:
            if a["since"] is None or now - a["last"] > AUTO_BLINK:
                a["since"] = now
            a["last"] = now
        if a["phase"] == "reading":
            if self.capture is None or not self.capture["auto"]:  # cancelled, or a hand capture took over
                self.auto = _auto_idle()
            return
        if a["phase"] == "holding":
            if now - a["last"] > AUTO_GONE:
                self.auto = _auto_idle()
            elif found and a["sig"] is not None:
                unlike = _similarity(_signature(card), a["sig"]) < AUTO_CHANGED
                a["changed"] = a["changed"] + 1 if unlike else 0
                if a["changed"] >= AUTO_CHANGED_FRAMES:  # a different card went straight in front of the camera
                    self.auto = {**_auto_idle(), "phase": "steady", "since": now, "last": now}
            return
        if not found:
            if now - a["last"] > AUTO_BLINK:
                a["phase"], a["since"] = "waiting", None
            return
        a["phase"] = "steady"
        if (now - a["since"] >= AUTO_STEADY and q["sharp"] >= MIN_SHARP and self.capture is None
                and self.model is not None and self.model.ready):
            self.capture = _new_capture(None, None, True, auto=True)
            a["phase"] = "reading"

    def _marker_reading(self, model, vec):
        markers = model.markers(vec)
        sides = model.breakout_sides(vec)
        labels = (("name_shine", "name shines"),
                  ("overframe", "art breaks the box" + (f" ({', '.join(sides)})" if sides else "")),
                  ("starlight", "Starlight shine"))
        return " \u00b7 ".join(f"{label} {round(markers[k] * 100)}%" for k, label in labels if k in markers)

    def _auto_read(self, c):
        """Guess a card auto mode just read and, when sure enough, add it to the counter."""
        read = {"id": secrets.token_hex(4), "time": time.time(), "guess": None, "confidence": 0.0, "top": [],
                "reading": "", "status": "error", "logged": None, "entry": None, "text": "", "trained": None,
                "trained_id": None, "verified": False, "frames": c["frames"], "full": c["full"], "vec": None,
                "sig": c.get("sig"), "name": None, "nameSure": False, "nameCandidates": [], "printed": None,
                "test": self.test_mode}
        try:
            cards = [cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR) for jpg, _ in c["frames"]]
            vec = recog.capture_features([x for x in cards if x is not None])
            read["vec"] = vec
            with self.lock:
                model, threshold = self.model, self.config["autoThreshold"]
            guess = model.predict(vec) if model is not None and model.ready else None
            if not guess:
                read.update(status="untrained", text="Can't read cards yet: it needs more training captures.")
            else:
                best, p = next(iter(guess.items()))
                read.update(guess=best, confidence=p, top=[[r, v] for r, v in list(guess.items())[:3]],
                            reading=self._marker_reading(model, vec))
                label, pct = self._label(best), f"{round(p * 100)}%"
                repeat = self._recent_same(read["sig"], best) if best != NOT_HIT["id"] else None
                if p >= threshold and best == NOT_HIT["id"]:
                    name = None  # a sure non-hit: no need to read its name
                else:
                    name = self._read_name([x for x in cards if x is not None], best)
                if name:
                    read.update(name=name["name"], nameSure=name["sure"], printed=name["printed"],
                                nameCandidates=[n for n, _ in name["candidates"]])
                clash = self._printing_clash(read, best)
                if p < threshold:
                    read.update(status="unsure", text=f"Not sure: {label} {pct}. Not added yet: it needs your answer.")
                elif clash:
                    read.update(status="unsure", text=f"{label} {pct}, but {clash} Not added yet: it needs your answer.")
                elif best == NOT_HIT["id"]:
                    read.update(status="skipped", text=f"Not a hit ({pct}), ignored.")
                elif repeat is not None:
                    at = time.strftime("%H:%M:%S", time.localtime(repeat["time"]))
                    read.update(status="repeat", text=f"{label} {pct}: the same card as the one added at {at}, "
                                                      "so not added again.")
                else:
                    card = read["name"] if read["nameSure"] else None
                    where = "the counter's test hits" if read["test"] else "the counter"
                    read.update(status="logged", logged=best, entry=self._counter_hit(best, card, read["test"]),
                                text=f"{label} {pct}" + (f": {card}" if card else "") + f", added to {where}."
                                     + ("" if card or not read["name"] else f" Name not sure (maybe {read['name']})."))
                    # Sure hits grow the training photos on their own (fix any that are wrong
                    # under Camera reads: the capture moves to the right rarity). Not test reads:
                    # the same card shown over and over would crowd the photos.
                    if not read["test"]:
                        try:
                            self._learn(read, best, verified=False)
                            read["text"] += " Saved to your card photos."
                        except OSError as exc:
                            print(f"[scanner] could not save a camera read: {exc}", file=sys.stderr)
        except OSError:
            read.update(status="offline", text="The counter isn't reachable, so nothing was added. Is it running?")
        except Exception as exc:
            read.update(status="error", text=f"Reading failed: {exc}")
        with self.lock:
            self.auto_reads.insert(0, read)
            while len(self.auto_reads) > AUTO_KEEP:  # drop the oldest, but keep reads waiting for an answer
                old = next((r for r in reversed(self.auto_reads) if r["status"] != "unsure"), None)
                if old is None:
                    if len(self.auto_reads) <= AUTO_KEEP_MAX:
                        break
                    old = self.auto_reads[-1]
                self.auto_reads.remove(old)
            ok = read["status"] in ("logged", "skipped", "repeat")
            self.message = {"ok": ok, "at": time.time(), "text": ("TEST " if read["test"] else "") + "Camera read: " + read["text"]
                            + (f" ({read['reading']})" if read["reading"] else "")}

    def _recent_same(self, sig, rarity):
        """The hit of this rarity added in the last few minutes that this card looks just like,
        or None. Only the same rarity: the same card can come in two rarities (an Ultra and a
        Secret print look alike), and both count."""
        if sig is None:
            return None
        with self.lock:
            for r in self.auto_reads:
                if time.time() - r["time"] > AUTO_REPEAT_SECONDS:
                    break
                if (r["logged"] == rarity and r["sig"] is not None
                        and _similarity(sig, r["sig"]) >= AUTO_SAME):
                    return r
        return None

    def _learn(self, read, rarity, verified):
        """Keep a camera read as a training capture of `rarity` (replacing the one saved for it
        earlier, if its rarity changed), so recognition keeps growing from the stream.
        verified: the rarity came from you, not from the scanner's own guess."""
        if read["vec"] is None:
            return
        if read["trained"] != rarity:
            if read["trained_id"]:
                self.dataset.delete(read["trained_id"])
            extra = {"camera": self.source.name, "source": "auto", "verified": verified,
                     "frameSize": [self.frame_info["width"], self.frame_info["height"]],
                     "guess": {"rarity": read["guess"], "probabilities": dict(read["top"])}}
            card = read["name"] if read["nameSure"] else None
            meta = self.dataset.save(rarity, card, read["frames"], read["full"], extra)
            folder = self.dataset.find(meta["id"])
            if folder is not None:
                recog.save_capture_features(folder, read["vec"])
            if not read["trained"]:
                self.auto_learned += 1
            read["trained"], read["trained_id"] = rarity, meta["id"]
        elif verified and not read["verified"]:
            self.dataset.update_meta(read["trained_id"], verified=True)
        if verified and not read["verified"]:  # your first answer on a read counts toward the live accuracy
            self._log_guess(read["trained_id"], rarity, read["guess"], read["confidence"])
            read["verified"] = True
        with self.lock:
            self._captures_cache = None
        self._retrain.set()

    def _counter_post(self, path, body):
        req = urllib.request.Request(self.counter_url + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        # Generous: these run off the camera loop, and on a busy streaming PC a request can stall for
        # a few seconds. (Never retried: a hit that did arrive would be counted twice.)
        with urllib.request.urlopen(req, timeout=COUNTER_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _read_name(self, cards, rarity):
        """The card name in a capture's photos (see names.read_name), or None. Never fails a read."""
        try:
            return names.read_name(cards, self.card_list, rarity=rarity, set_prefix=self.config["set"])
        except Exception as exc:
            print(f"[scanner] reading the card name failed: {exc}", file=sys.stderr)
            return None

    def _printing_clash(self, read, rarity):
        """Why the card list says this card can't be this rarity, or None. Only for a name read
        surely, and only for rarities the list records (Overframes aren't in it yet)."""
        if not read["nameSure"] or rarity not in names.LIST_RARITIES or read["printed"] is None:
            return None
        if rarity in read["printed"]:
            return None
        if not read["printed"]:
            return f"the card list says {read['name']} was never printed as a hit."
        return (f"the card list has no {self._label(rarity)} of {read['name']} (only "
                + ", ".join(self._label(r) for r in read["printed"]) + ").")

    def _counter_hit(self, rarity, card=None, test=False):
        """Add one hit to the counter (to its test list in test mode); returns its log entry id
        (to correct it later)."""
        state = self._counter_post("/api/hit", {"rarity": rarity, "card": card, "auto": True, "test": test})
        for e in reversed(state.get("testLog" if test else "log", [])):
            if e.get("rarity") == rarity and e.get("auto"):
                return e.get("id")
        return None

    def _find_read(self, body):
        rid = body.get("id")
        read = next((r for r in self.auto_reads if r["id"] == rid), None) if isinstance(rid, str) else None
        if read is None:
            raise BadRequest(f"no such read (only the latest {AUTO_KEEP} are kept)")
        return read

    def set_auto(self, body):
        on, threshold, test = body.get("on"), body.get("threshold"), body.get("test")
        if test is not None:
            if not isinstance(test, bool):
                raise BadRequest("'test' must be true or false")
            with self.lock:
                self.test_mode = test
            if test and not self.auto_on:
                on = True  # test mode reads cards: switch auto mode on with it
            elif on is None and threshold is None:
                with self.lock:
                    self.message = {"ok": True, "at": time.time(), "text":
                                    "Test mode on: hits go to the counter's Test hits list, not your real counts."
                                    if test else "Test mode off: hits count for real again."}
                return self.status()
        if "set" in body:  # the set being opened (optional): its cards win look-alike names
            code = body["set"]
            if code is not None and not (isinstance(code, str) and SET_CODE.match(code.strip().upper())):
                raise BadRequest("'set' must be a set code like CORI, or null")
            with self.lock:
                self.config["set"] = code.strip().upper() if code else None
            self._save_config()
            if on is None and threshold is None:
                return self.status()
        if on is not None and not isinstance(on, bool):
            raise BadRequest("'on' must be true or false")
        if threshold is not None and (isinstance(threshold, bool) or threshold not in AUTO_THRESHOLDS):
            raise BadRequest("'threshold' must be one of: " + ", ".join(map(str, AUTO_THRESHOLDS)))
        if on is None and threshold is None:
            raise BadRequest("send 'on', 'threshold', 'test' and/or 'set'")
        with self.lock:
            if threshold is not None:
                self.config["autoThreshold"] = threshold
            if on is not None and on != self.auto_on:
                self.auto_on, self.auto = on, _auto_idle()
                if self.capture is not None and self.capture["auto"]:
                    self.capture = None
                if on and not (self.model is not None and self.model.ready):
                    text = "Auto mode is on, but it can't read cards until it has more training captures."
                elif on and self.test_mode:
                    text = ("Test mode on: hold a card up and watch the counter's Test hits list (and the overlay "
                            "preview in the control panel). Nothing counts and nothing shows on stream.")
                elif on:
                    text = "Auto mode on: hold each hit up to the camera for a moment and it's added to the counter."
                else:
                    self.test_mode = False  # test mode only runs with auto mode
                    text = "Auto mode off."
                    self._retrain.set()  # catch up on the held-out test skipped while streaming
                self.message = {"ok": True, "at": time.time(), "text": text}
        if threshold is not None:
            self._save_config()
        return self.status()

    def fix_read(self, body):
        """The right rarity for a camera read: corrects the counter if the read got it wrong
        (or adds a hit it wasn't sure about), and keeps the read as a training capture so
        the scanner learns from its mistakes."""
        rarity = body.get("rarity")
        if rarity not in RARITY_IDS:
            raise BadRequest("'rarity' must be one of: " + ", ".join(RARITY_IDS))
        with self.lock:
            read = self._find_read(body)
            if read.get("busy"):
                raise BadRequest("that read is already being changed")
            read["busy"] = True
        want = rarity if rarity in HIT_IDS else None
        try:
            if read["logged"] != want:
                if read["entry"]:
                    self._counter_post("/api/delete", {"id": read["entry"]})
                    read["entry"], read["logged"] = None, None
                if want:
                    card = read["name"] if read["nameSure"] else None
                    read["entry"], read["logged"] = self._counter_hit(want, card, read["test"]), want
            self._learn(read, rarity, verified=True)
        except urllib.error.URLError:
            raise BadRequest("the counter isn't reachable; is it running?")
        except OSError as exc:
            raise BadRequest(f"couldn't save the photos: {exc}")
        finally:
            read["busy"] = False
        with self.lock:
            read["status"] = "confirmed" if rarity == read["guess"] else "fixed"
            label = self._label(rarity)
            read["text"] = ("Fixed: " if read["status"] == "fixed" else "Confirmed: ") + label + (
                " (on the counter)." if want else " (not on the counter).")
            self._captures_cache = None
            self.message = {"ok": True, "at": time.time(), "text": read["text"] + " Saved as a training capture."}
        return self.status()

    def name_read(self, body):
        """Set the card name of a camera read: on its counter hit and its saved photos."""
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BadRequest("'name' must be the card's name")
        name = name.strip()[:CARD_MAX]
        with self.lock:
            read = self._find_read(body)
            read.update(name=name, nameSure=True)
        if read["entry"]:
            try:
                self._counter_post("/api/rename", {"id": read["entry"], "card": name})
            except urllib.error.URLError:
                raise BadRequest("the counter isn't reachable; is it running?")
        if read["trained_id"]:
            self.dataset.update_meta(read["trained_id"], card=name)
        with self.lock:
            self._captures_cache = None
            self.message = {"ok": True, "at": time.time(), "text": f"Name set: {name}."}
        return self.status()

    def update_card_list(self, body):
        if not self.card_list.downloading:
            threading.Thread(target=self.card_list.download, name="card-list", daemon=True).start()
            self.card_list.downloading = True
        return self.status()

    def card_sets(self):
        return {"sets": [{"code": k, "name": v} for k, v in sorted(self.card_list.sets.items())]}

    def forget_read(self, body):
        """Drop a camera read from the list (the counter is left as it is)."""
        with self.lock:
            read = self._find_read(body)
            self.auto_reads.remove(read)
        return self.status()

    def read_thumb(self, rid):
        with self.lock:
            read = next((r for r in self.auto_reads if r["id"] == rid), None)
            return read["frames"][len(read["frames"]) // 2][0] if read and read["frames"] else None

    def status(self):
        with self.lock:
            # Read under the same lock saves and deletes happen under, so a reply never
            # says "saved" while still listing the captures from before the save.
            if self._captures_cache is None:
                self._captures_cache = self.dataset.captures()
            captures = self._captures_cache
            c = self.capture
            return {
                "camera": {"index": self.source.index, "name": self.source.name, "error": self.source.error,
                           **self.frame_info},
                "cameras": list(self.cameras),
                "scanning": self.scanning,
                "guide": self.config["guide"],
                "allowGuide": self.config["allowGuide"],
                "targetFps": self.config["fps"],
                "fpsChoices": list(FPS_CHOICES),
                "detection": dict(self.detection),
                "quality": dict(self.quality),
                "fps": round(self.fps, 1),
                "detectFps": round(self.detect_fps, 1),
                "capture": None if c is None else {
                    "rarity": c["rarity"], "card": c["card"], "kept": c["kept"], "target": c["target"],
                    "auto": c["auto"],
                    "secondsLeft": round(max(0.0, c["deadline"] - time.monotonic()), 1)},
                "message": self.message,
                "rarities": self.rarities,
                "counterOnline": self.counter_ok,
                "dataset": self.dataset.summary(captures),
                "recent": [{"id": m["id"], "rarity": m.get("rarity"), "card": m.get("card"),
                            "created": m.get("created"), "frames": len(m.get("frames", [])),
                            "thumb": f"/training/{m.get('rarity')}/{m['id']}/00.jpg"} for m in captures[:12]],
                "recognition": {**self.recognition, "live": self._live_accuracy()},
                "settings": {"framesPerCapture": FRAMES_PER_CAPTURE, "minFrames": MIN_FRAMES,
                             "seconds": CAPTURE_SECONDS},
                "auto": {"on": self.auto_on, "test": self.test_mode,
                         "phase": self.auto["phase"] if self.auto_on else "off",
                         "threshold": self.config["autoThreshold"], "thresholds": list(AUTO_THRESHOLDS),
                         "reads": [_public_read(r) for r in self.auto_reads],
                         "waiting": sum(r["status"] == "unsure" for r in self.auto_reads),
                         "learned": self.auto_learned},
                "names": {"ocr": names.ocr_available(), "ocrError": names.OCR_ERROR,
                          "cards": len(self.card_list.cards), "updated": self.card_list.updated,
                          "downloading": self.card_list.downloading, "error": self.card_list.error,
                          "set": self.config["set"],
                          "setName": self.card_list.sets.get(self.config["set"]) if self.config["set"] else None},
            }

    def close(self):
        self._stop.set()
        self.source.close()
        with self.preview_cond:
            self.preview_cond.notify_all()


def _auto_idle():
    return {"phase": "waiting", "since": None, "last": 0.0, "sig": None, "changed": 0}


def _new_capture(rarity, card, guess_only, auto=False):
    now = time.monotonic()
    return {"rarity": rarity, "card": card, "frames": [], "full": None, "kept": 0,
            "target": GUESS_FRAMES if guess_only else FRAMES_PER_CAPTURE, "save": not guess_only,
            "started": now, "deadline": now + CAPTURE_SECONDS, "last_keep": 0.0, "auto": auto}


def _signature(card):
    """A tiny blurred greyscale copy of the card, for "is this the same card?" checks."""
    g = cv2.resize(cv2.cvtColor(card, cv2.COLOR_BGR2GRAY), (21, 30), interpolation=cv2.INTER_AREA).astype(np.float32)
    g = cv2.GaussianBlur(g, (0, 0), 1.0)
    return (g - g.mean()) / (g.std() + 1e-6)


def _capture_signature(frames):
    """Signature of a capture: the median over its photos, so one glare-washed photo doesn't count."""
    cards = (cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR) for jpg, _ in frames)
    sigs = [_signature(c) for c in cards if c is not None]
    if not sigs:
        return None
    m = np.median(sigs, axis=0)
    return (m - m.mean()) / (m.std() + 1e-6)


def _similarity(a, b):
    """1 = the same picture, about 0 = unrelated."""
    return float((a * b).mean())


def _public_read(r):
    return {"id": r["id"], "time": r["time"], "guess": r["guess"], "confidence": r["confidence"], "top": r["top"],
            "reading": r["reading"], "status": r["status"], "logged": r["logged"], "text": r["text"],
            "trained": r["trained"], "verified": r["verified"], "name": r.get("name"),
            "nameSure": r.get("nameSure", False), "nameCandidates": r.get("nameCandidates", []),
            "printed": r.get("printed"), "test": r.get("test", False),
            "thumb": f"/auto/{r['id']}.jpg" if r["frames"] else None}


def _drop_glitches(frames):
    """The card barely moves between a capture's photos, so a photo whose corners are far
    from the capture's typical corners is a detection glitch (e.g. it grabbed only part of
    the card): drop it. Returns (kept frames, number dropped)."""
    with_corners = [i for i, (_, info) in enumerate(frames) if info.get("corners")]
    if len(with_corners) < 5:
        return frames, 0
    quads = np.array([detect.order_corners(np.array(frames[i][1]["corners"], np.float32)) for i in with_corners])
    median = np.median(quads, axis=0)
    size = float(max(detect._sides(median)))
    deviation = np.abs(quads - median).max(axis=(1, 2))
    bad = {with_corners[k] for k, dev in enumerate(deviation) if dev > 0.06 * size}
    return [f for i, f in enumerate(frames) if i not in bad], len(bad)


def _wrap(text, width):
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + ([line] if line else [])


# ---- HTTP --------------------------------------------------------------------

POST_ROUTES = {
    "/api/capture": "start_capture",
    "/api/capture/cancel": "cancel_capture",
    "/api/camera": "set_camera",
    "/api/cameras/scan": "scan_cameras",
    "/api/settings": "set_settings",
    "/api/captures/delete": "delete_capture",
    "/api/debug-frame": "debug_frame",
    "/api/auto": "set_auto",
    "/api/auto/fix": "fix_read",
    "/api/auto/forget": "forget_read",
    "/api/auto/name": "name_read",
    "/api/cards/update": "update_card_list",
}
_DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


class Handler(BaseHTTPRequestHandler):
    server_version = "WhatnotCardScanner/1.0"
    protocol_version = "HTTP/1.0"

    @property
    def scanner(self):
        return self.server.scanner

    def _send(self, status, body, ctype, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/scanner"):
                try:
                    self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8", "no-cache")
                except OSError:
                    self._send(404, b"scanner.html is missing", "text/plain; charset=utf-8")
            elif path == "/api/status":
                self._json(200, self.scanner.status())
            elif path == "/api/sets":
                self._json(200, self.scanner.card_sets())
            elif path == "/video.mjpg":
                self._stream()
            elif path.startswith("/auto/") and path.endswith(".jpg"):
                jpg = self.scanner.read_thumb(path[len("/auto/"):-len(".jpg")])
                if jpg is None:
                    self._json(404, {"error": "no such read"})
                else:
                    self._send(200, jpg, "image/jpeg", "max-age=3600")
            elif path.startswith("/training/"):
                parts = [unquote(p) for p in path.split("/")[2:]]
                file = self.scanner.dataset.file(*parts) if len(parts) == 3 else None
                if file is None:
                    self._json(404, {"error": "no such image"})
                else:
                    self._send(200, file.read_bytes(), "image/jpeg", "max-age=3600")
            else:
                self._json(404, {"error": f"not found: {path}"})
        except _DISCONNECT:
            self.close_connection = True

    def _stream(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        seq = -1
        try:
            while not self.scanner._stop.is_set():
                jpg, seq = self.scanner.wait_preview(seq)
                if jpg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                self.wfile.flush()
        except (OSError, ValueError):
            pass

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            action = POST_ROUTES.get(path)
            if action is None:
                self._json(404, {"error": f"not found: {path}"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if 0 < n <= 65536 else b""
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                if not isinstance(body, dict):
                    raise BadRequest("request body must be a JSON object")
                self._json(200, getattr(self.scanner, action)(body))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "invalid JSON"})
            except BadRequest as exc:
                self._json(400, {"error": str(exc)})
        except _DISCONNECT:
            self.close_connection = True

    def log_request(self, code="-", size="-"):
        try:
            if int(code) >= 400:
                super().log_request(code, size)
        except (TypeError, ValueError):
            pass


class ScannerServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, scanner):
        self.scanner = scanner
        super().__init__(address, Handler)

    def server_bind(self):
        TCPServer.server_bind(self)  # skip the slow getfqdn() lookup
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], _DISCONNECT):
            super().handle_error(request, client_address)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Card scanner: collect training photos per rarity")
    parser.add_argument("--port", type=int, default=7778, help="port (default 7778)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--camera", default=None,
                        help='camera number, or "obs:<source name>" to take frames from OBS (default: last used)')
    parser.add_argument("--demo", action="store_true", help="use a synthetic demo camera instead of a real one")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="data folder (default data/ in the project)")
    args = parser.parse_args(argv)

    camera = int(args.camera) if args.camera is not None and args.camera.isdigit() else args.camera
    scanner = Scanner(args.data, camera=camera, demo=args.demo)
    try:
        httpd = ScannerServer((args.host, args.port), scanner)
    except OSError as exc:
        print(f"Could not listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        scanner.close()
        return 1
    print("Card scanner running.")
    print(f"  Open:     http://{args.host}:{httpd.server_address[1]}/")
    print(f"  Camera:   {scanner.source.name}")
    print(f"  Photos:   {scanner.dataset.root}")
    print("Press Ctrl+C to stop.", flush=True)
    if hasattr(__import__("signal"), "SIGBREAK"):
        import signal
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        scanner.close()
        httpd.server_close()
    return 0
