#!/usr/bin/env python3
"""Whatnot Yu-Gi-Oh hit counter server.

Serves the streamer control panel (/control), the OBS overlay (/overlay), a small
JSON API and a Server-Sent Events stream. State is kept in memory, guarded by one
lock, and persisted atomically to data/state.json after every mutation.

Standard library only. See CONTRACT.md for the full interface.

    python server.py [--host 127.0.0.1] [--port 7777] [--state data/state.json]
"""

import argparse
import ctypes
import json
import os
import queue
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_STATE_PATH = ROOT / "data" / "state.json"

RARITIES = (
    {"id": "ultra", "label": "Ultra Rare", "short": "UR", "color": "#f5c542"},
    {"id": "secret", "label": "Secret Rare", "short": "SCR", "color": "#c0c8d8"},
    {"id": "overframe", "label": "Overframe", "short": "OF", "color": "#ff6b35"},
    {"id": "starlight", "label": "Starlight Rare", "short": "STR", "color": "#7fd4ff"},
    {"id": "starlight_overframe", "label": "Starlight Overframe", "short": "SOF", "color": "#d48cff"},
)
RARITY_IDS = tuple(r["id"] for r in RARITIES)
DEFAULT_LABELS = {r["id"]: r["label"] for r in RARITIES}

DEFAULT_TITLE = "Yu-Gi-Oh Box Break"
DEFAULT_PACKS_PER_BOX = 24
LOG_CAP = 1000
TEST_LOG_CAP = 200  # test hits (scanner test mode): shown, never counted
CARD_MAX = 80
TITLE_MAX = 80
LABEL_MAX = 40
SPAN_MAX = 100
DEFAULT_PANEL_OPACITY = 85  # % opacity of the overlay's panel backgrounds (0 = text only)
DISPLAY_KEYS = ("packs", "boxes", "packsLeft", "tracker", "caseLog", "showcase")
PING_INTERVAL = 15.0
MAX_BODY = 64 * 1024
CLIENT_QUEUE_MAX = 256


class BadRequest(Exception):
    """Invalid client input; becomes a 400 {"error": message}."""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(body):
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    return body


def _require_rarity(body):
    rarity = body.get("rarity")
    if not isinstance(rarity, str):
        raise BadRequest("'rarity' must be a string, one of: " + ", ".join(RARITY_IDS))
    if rarity not in RARITY_IDS:
        raise BadRequest(f"unknown rarity {rarity!r}; expected one of: " + ", ".join(RARITY_IDS))
    return rarity


def _clean_card(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest("'card' must be a string or null")
    value = value.strip()[:CARD_MAX].strip()
    return value or None


def _encode_events(events):
    """Encode [(event_name, payload), ...] as one SSE chunk (one JSON line per data:)."""
    parts = []
    for name, payload in events:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"event: {name}\ndata: {data}\n\n")
    return "".join(parts).encode("utf-8")


def _default_data():
    return {
        "title": DEFAULT_TITLE,
        "packsPerBox": DEFAULT_PACKS_PER_BOX,
        "labels": {},
        # Boxes on the table: number .. number+span-1 (capped at `of`).
        # of = total boxes in the break (e.g. 12 for a case), None = not set.
        "box": {"number": 1, "packsOpened": 0, "span": 1, "of": None},
        "display": {"packs": True, "boxes": True, "packsLeft": True, "tracker": True, "caseLog": True,
                    "showcase": False},
        # Foot pedal keys, e.g. "F13" or "Ctrl+Alt+P"; None = not set.
        "pedal": dict.fromkeys(PEDAL_ACTIONS),
        "panelOpacity": DEFAULT_PANEL_OPACITY,
        "log": [],
        "testLog": [],
        "updatedAt": time.time(),
    }


def _sanitize(raw):
    """Turn whatever was on disk into valid internal data. Bad fields fall back to
    defaults; bad log entries are dropped. Raises ValueError if unusable."""
    if not isinstance(raw, dict):
        raise ValueError("state file is not a JSON object")
    data = _default_data()

    if isinstance(raw.get("title"), str):
        data["title"] = raw["title"][:TITLE_MAX]
    if _is_int(raw.get("packsPerBox")) and raw["packsPerBox"] >= 1:
        data["packsPerBox"] = raw["packsPerBox"]
    labels = raw.get("labels")
    if isinstance(labels, dict):
        data["labels"] = {
            k: v[:LABEL_MAX]
            for k, v in labels.items()
            if k in RARITY_IDS and isinstance(v, str) and v.strip()
        }
    box = raw.get("box")
    if isinstance(box, dict):
        if _is_int(box.get("number")) and box["number"] >= 1:
            data["box"]["number"] = box["number"]
        if _is_int(box.get("packsOpened")) and box["packsOpened"] >= 0:
            data["box"]["packsOpened"] = box["packsOpened"]
        if _is_int(box.get("span")) and 1 <= box["span"] <= SPAN_MAX:
            data["box"]["span"] = box["span"]
        if _is_int(box.get("of")) and box["of"] >= data["box"]["number"]:
            data["box"]["of"] = box["of"]
        _clamp_packs(data)
    display = raw.get("display")
    if isinstance(display, dict):
        for k in DISPLAY_KEYS:
            if isinstance(display.get(k), bool):
                data["display"][k] = display[k]
    if _is_int(raw.get("panelOpacity")) and 0 <= raw["panelOpacity"] <= 100:
        data["panelOpacity"] = raw["panelOpacity"]
    pedal = raw.get("pedal")
    if isinstance(pedal, dict):
        for action in PEDAL_ACTIONS:
            try:
                data["pedal"][action] = parse_hotkey(pedal[action])[0] if pedal.get(action) else None
            except BadRequest:
                pass
    if isinstance(raw.get("updatedAt"), (int, float)) and not isinstance(raw["updatedAt"], bool):
        data["updatedAt"] = float(raw["updatedAt"])

    data["log"] = _clean_log(raw.get("log"), LOG_CAP)
    data["testLog"] = _clean_log(raw.get("testLog"), TEST_LOG_CAP)
    return data


def _clean_log(log, cap):
    """Valid log entries from disk (bad ones dropped), newest `cap`."""
    clean = []
    if isinstance(log, list):
        seen = set()
        for e in log:
            if not isinstance(e, dict):
                continue
            eid, rarity, box_no, ts = e.get("id"), e.get("rarity"), e.get("box"), e.get("ts")
            if not isinstance(eid, str) or not eid or eid in seen:
                continue
            if rarity not in RARITY_IDS or not _is_int(box_no) or box_no < 1:
                continue
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            card = e.get("card")
            card = card.strip()[:CARD_MAX] or None if isinstance(card, str) else None
            box_to = e.get("boxTo")
            box_to = box_to if _is_int(box_to) and box_to >= box_no else box_no
            seen.add(eid)
            entry = {"id": eid, "rarity": rarity, "card": card, "box": box_no, "boxTo": box_to, "ts": float(ts)}
            if e.get("auto") is True:
                entry["auto"] = True
            clean.append(entry)
    return clean[-cap:]


# ---- foot pedal keys ----------------------------------------------------------

PEDAL_ACTIONS = {"plus": 1, "minus": -1}  # action -> packs delta
_HOTKEY_MODS = (("Ctrl", 0x0002), ("Alt", 0x0001), ("Shift", 0x0004), ("Win", 0x0008))
_MOD_BITS = {name.lower(): bit for name, bit in _HOTKEY_MODS} | {"control": 0x0002}
_MOD_NOREPEAT = 0x4000
_VK = {f"F{i}": 0x6F + i for i in range(1, 25)}
_VK.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
_VK.update({str(n): 0x30 + n for n in range(10)})
_VK.update({f"Numpad{n}": 0x60 + n for n in range(10)})
_VK.update({
    "Pause": 0x13, "ScrollLock": 0x91, "Insert": 0x2D, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22, "NumpadAdd": 0x6B, "NumpadSubtract": 0x6D,
    "NumpadMultiply": 0x6A, "NumpadDivide": 0x6F,
    "MediaPlayPause": 0xB3, "MediaNext": 0xB0, "MediaPrev": 0xB1,
})
_VK_BY_LOWER = {name.lower(): name for name in _VK}


def parse_hotkey(text):
    """'ctrl+alt+p' -> ('Ctrl+Alt+P', modifier bits, virtual-key code). Raises BadRequest.

    A global hotkey swallows its key everywhere, so a bare letter or digit is refused:
    it would stop that character typing in every app."""
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("a pedal key must be a string like 'F13' or 'Ctrl+Alt+P'")
    *mod_parts, key_part = [part.strip() for part in text.strip().split("+")]
    mods = 0
    for part in mod_parts:
        bit = _MOD_BITS.get(part.lower())
        if bit is None:
            raise BadRequest(f"unknown modifier {part!r} in {text!r}; use Ctrl, Alt, Shift or Win")
        mods |= bit
    name = _VK_BY_LOWER.get(key_part.lower())
    if name is None:
        raise BadRequest(f"unsupported pedal key {key_part!r}; use F1-F24, Pause, ScrollLock, "
                         "a Numpad key, or Ctrl/Alt + a letter")
    if len(name) == 1 and not mods & 0x000B:  # no Ctrl, Alt or Win
        raise BadRequest(f"{text.strip()!r} on its own would stop {name} typing anywhere on this PC; "
                         f"set the pedal to send F13-F24 or Ctrl+Alt+{name}")
    canonical = "+".join([n for n, bit in _HOTKEY_MODS if mods & bit] + [name])
    return canonical, mods, _VK[name]


def _clamp_packs(d):
    """Packs opened never exceed what the boxes on the table hold."""
    box = d["box"]
    last = Store._group_end(box)
    box["packsOpened"] = min(box["packsOpened"], d["packsPerBox"] * (last - box["number"] + 1))


class _Client:
    """One SSE subscriber: its own queue, written to only by its handler thread."""

    __slots__ = ("queue", "alive")

    def __init__(self):
        self.queue = queue.Queue(maxsize=CLIENT_QUEUE_MAX)
        self.alive = True


class Store:
    """All state lives here. Every read/mutation happens under self._lock, and SSE
    messages are queued under the same lock so every client sees the same order."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._clients = set()
        self._closed = False
        self._data = self._load()
        self.pedal = None  # Pedal, when the foot pedal listener runs (see main)

    # ---- persistence -------------------------------------------------------

    def _load(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _default_data()
        except OSError as exc:
            print(f"[state] could not read {self.path}: {exc}; starting from defaults", file=sys.stderr)
            return _default_data()
        try:
            return _sanitize(json.loads(text))
        except ValueError as exc:  # JSONDecodeError is a ValueError
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
                where = f"; moved it to {backup.name}"
            except OSError:
                where = ""
            print(f"[state] {self.path} is corrupt ({exc}){where}; starting from defaults", file=sys.stderr)
            return _default_data()

    def _save_locked(self):
        d = self._data
        payload = {
            "version": 1,
            "title": d["title"],
            "packsPerBox": d["packsPerBox"],
            "labels": d["labels"],
            "box": d["box"],
            "display": d["display"],
            "panelOpacity": d["panelOpacity"],
            "pedal": d["pedal"],
            "log": d["log"],
            "testLog": d["testLog"],
            "updatedAt": d["updatedAt"],
        }
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            # On Windows os.replace can briefly fail while a virus scanner or
            # indexer holds the target open; retry a few times.
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            # Keep serving from memory; the next successful save catches up.
            print(f"[state] WARNING: could not save {self.path}: {exc}", file=sys.stderr)

    # ---- views -------------------------------------------------------------

    @staticmethod
    def _group_end(box):
        """Last box on the table."""
        end = box["number"] + box["span"] - 1
        if box["of"] is not None:
            end = min(end, box["of"])
        return max(end, box["number"])

    def _rarities_locked(self):
        labels = self._data["labels"]
        return [dict(r, label=labels.get(r["id"], r["label"])) for r in RARITIES]

    def _public_locked(self):
        d = self._data
        box_no = d["box"]["number"]
        box_to = self._group_end(d["box"])
        box_counts = dict.fromkeys(RARITY_IDS, 0)
        session_counts = dict.fromkeys(RARITY_IDS, 0)
        for e in d["log"]:
            session_counts[e["rarity"]] += 1
            if e["box"] == box_no:
                box_counts[e["rarity"]] += 1
        return {
            "title": d["title"],
            "rarities": self._rarities_locked(),
            "box": {
                "number": box_no,
                "to": box_to,
                "span": d["box"]["span"],
                "of": d["box"]["of"],
                "packsOpened": d["box"]["packsOpened"],
                "packsPerBox": d["packsPerBox"],
                "packsTotal": d["packsPerBox"] * (box_to - box_no + 1),
                "counts": box_counts,
                "total": sum(box_counts.values()),
            },
            "session": {
                "counts": session_counts,
                "total": sum(session_counts.values()),
                "boxes": box_to,
            },
            "display": dict(d["display"]),
            "panelOpacity": d["panelOpacity"],
            "pedal": {
                **d["pedal"],
                "status": dict(self.pedal.status) if self.pedal else dict.fromkeys(PEDAL_ACTIONS, "unavailable"),
                "paused": bool(self.pedal and self.pedal.paused),
                "available": bool(self.pedal and self.pedal.available),
            },
            # Entries are never mutated after creation, so a shallow copy is safe
            # to serialise outside the lock.
            "log": list(d["log"]),
            "testLog": list(d["testLog"]),
            "updatedAt": d["updatedAt"],
        }

    def state(self):
        with self._lock:
            return self._public_locked()

    def pedal_keys(self):
        with self._lock:
            return dict(self._data["pedal"])

    def broadcast_state(self):
        """Push the current state without changing or saving anything (pedal status)."""
        with self._lock:
            if not self._closed:
                self._broadcast_locked([("state", self._public_locked())])

    # ---- SSE fan-out -------------------------------------------------------

    def subscribe(self):
        client = _Client()
        with self._lock:
            # Initial state is queued under the lock so no mutation can slip in
            # between "snapshot" and "registered".
            client.queue.put_nowait(_encode_events([("state", self._public_locked())]))
            if self._closed:
                client.queue.put_nowait(None)
            else:
                self._clients.add(client)
        return client

    def unsubscribe(self, client):
        client.alive = False
        with self._lock:
            self._clients.discard(client)

    def client_count(self):
        with self._lock:
            return len(self._clients)

    def _broadcast_locked(self, events):
        msg = _encode_events(events)
        for client in list(self._clients):
            try:
                client.queue.put_nowait(msg)
            except queue.Full:
                # Client stopped reading; drop it rather than buffer forever.
                client.alive = False
                self._clients.discard(client)

    def close(self):
        """Tell every SSE handler to finish (used on shutdown)."""
        with self._lock:
            self._closed = True
            for client in self._clients:
                client.alive = False
                try:
                    client.queue.put_nowait(None)
                except queue.Full:
                    pass
            self._clients.clear()

    # ---- mutations ---------------------------------------------------------

    def _mutate(self, apply):
        """Run apply(data) under the lock. apply validates first and raises
        BadRequest before changing anything; it may return extra SSE events
        that are sent after the `state` event."""
        with self._lock:
            extra = apply(self._data) or []
            self._data["updatedAt"] = time.time()
            self._save_locked()
            state = self._public_locked()
            self._broadcast_locked([("state", state)] + extra)
            return state

    def _new_id_locked(self):
        existing = {e["id"] for e in self._data["log"]} | {e["id"] for e in self._data["testLog"]}
        while True:
            eid = secrets.token_hex(3)
            if eid not in existing:
                return eid

    def hit(self, body):
        body = _require_object(body)
        rarity = _require_rarity(body)
        card = _clean_card(body.get("card"))
        auto = body.get("auto", False)
        if not isinstance(auto, bool):
            raise BadRequest("'auto' must be true or false")
        test = body.get("test", False)
        if not isinstance(test, bool):
            raise BadRequest("'test' must be true or false")

        def apply(d):
            entry = {
                "id": self._new_id_locked(),
                "rarity": rarity,
                "card": card,
                "box": d["box"]["number"],
                "boxTo": self._group_end(d["box"]),
                "ts": round(time.time(), 3),
            }
            if auto:  # logged by the camera scanner, not by hand
                entry["auto"] = True
            # A test hit (scanner test mode) goes to its own list: shown in the control panel,
            # never counted, and sent as "testHit" so the stream overlay ignores it.
            log, cap = (d["testLog"], TEST_LOG_CAP) if test else (d["log"], LOG_CAP)
            log.append(entry)
            if len(log) > cap:
                del log[: len(log) - cap]
            rarity_obj = next(r for r in self._rarities_locked() if r["id"] == rarity)
            return [("testHit" if test else "hit", {"entry": entry, "rarity": rarity_obj})]

        return self._mutate(apply)

    def remove(self, body):
        body = _require_object(body)
        rarity = _require_rarity(body)

        def apply(d):
            box_no = d["box"]["number"]
            log = d["log"]
            for i in range(len(log) - 1, -1, -1):
                if log[i]["rarity"] == rarity and log[i]["box"] == box_no:
                    del log[i]
                    break

        return self._mutate(apply)

    def delete(self, body):
        body = _require_object(body)
        eid = body.get("id")
        if not isinstance(eid, str) or not eid:
            raise BadRequest("'id' must be a non-empty string")

        def apply(d):
            # Unknown id is a no-op (idempotent: double-clicks don't error).
            d["log"] = [e for e in d["log"] if e["id"] != eid]
            d["testLog"] = [e for e in d["testLog"] if e["id"] != eid]

        return self._mutate(apply)

    def rename(self, body):
        """Change the card name on a hit already logged (e.g. the camera scanner corrected it)."""
        body = _require_object(body)
        eid = body.get("id")
        if not isinstance(eid, str) or not eid:
            raise BadRequest("'id' must be a non-empty string")
        card = _clean_card(body.get("card"))

        def apply(d):
            for log in (d["log"], d["testLog"]):
                for i, e in enumerate(log):
                    if e["id"] == eid:
                        log[i] = {**e, "card": card}  # a new dict: published entries are never mutated
                        return
            raise BadRequest("no hit with that id")

        return self._mutate(apply)

    def clear_test(self, body):
        """Throw away the test hits."""
        _require_object(body)

        def apply(d):
            d["testLog"] = []

        return self._mutate(apply)

    def undo(self, body):
        _require_object(body)

        def apply(d):
            if d["log"]:
                d["log"].pop()

        return self._mutate(apply)

    def packs(self, body):
        body = _require_object(body)
        has_delta, has_set = "delta" in body, "set" in body
        if has_delta == has_set:
            raise BadRequest("body must contain exactly one of 'delta' or 'set'")
        key = "delta" if has_delta else "set"
        value = body[key]
        if not _is_int(value):
            raise BadRequest(f"'{key}' must be an integer")
        return self._mutate(self._packs_apply(has_delta, value))

    def pedal_press(self, action):
        """A foot pedal press: +1 / -1 pack, then a `pedal` event for the panel."""
        packs_apply = self._packs_apply(True, PEDAL_ACTIONS[action])

        def apply(d):
            before = d["box"]["number"]
            packs_apply(d)
            moved = d["box"]["number"] != before
            return [("pedal", {"action": action, "delta": PEDAL_ACTIONS[action], "moved": moved})]

        return self._mutate(apply)

    def pedal_pause(self, body):
        """Learn mode: stop the pedal hotkeys while the panel listens for the key."""
        body = _require_object(body)
        if not isinstance(body.get("paused"), bool):
            raise BadRequest("body must be {\"paused\": true} or {\"paused\": false}")
        if self.pedal:
            self.pedal.set_paused(body["paused"])
        return self.state()

    def _packs_apply(self, has_delta, value):
        def apply(d):
            box = d["box"]
            last = self._group_end(box)
            cap = d["packsPerBox"] * (last - box["number"] + 1)
            current = box["packsOpened"]
            if has_delta and value > 0 and current >= cap:
                # Table finished (e.g. 96/96): the next pack starts the next boxes at 0.
                # On the last boxes of the break there is nothing next, so it stays full.
                if box["of"] is None or last + 1 <= box["of"]:
                    box["number"], box["packsOpened"] = last + 1, 0
                return
            box["packsOpened"] = max(0, min(cap, current + value if has_delta else value))

        return apply

    def next_box(self, body):
        _require_object(body)
        return self.box({"delta": 1})

    def box(self, body):
        """Change which boxes are on the table and/or the break's total.

        delta: move by whole groups (+1 = next group, -1 = previous group).
        set:   first box on the table.          span: boxes on the table at once.
        of:    total boxes in the break, or null.
        """
        body = _require_object(body)
        if not any(k in body for k in ("delta", "set", "span", "of")):
            raise BadRequest("body must contain at least one of 'delta', 'set', 'span', 'of'")
        if "delta" in body and "set" in body:
            raise BadRequest("send either 'delta' or 'set', not both")
        delta, first, span, of = body.get("delta"), body.get("set"), body.get("span"), body.get("of")
        if "delta" in body and not _is_int(delta):
            raise BadRequest("'delta' must be an integer")
        if "set" in body and (not _is_int(first) or first < 1):
            raise BadRequest("'set' must be an integer >= 1")
        if "span" in body and (not _is_int(span) or not 1 <= span <= SPAN_MAX):
            raise BadRequest(f"'span' must be an integer from 1 to {SPAN_MAX}")
        if of is not None and (not _is_int(of) or of < 1):
            raise BadRequest("'of' must be an integer >= 1, or null for no total")

        def apply(d):
            box = d["box"]
            new_span = span if "span" in body else box["span"]
            new_of = of if "of" in body else box["of"]
            number = box["number"]
            if "set" in body:
                number = first
            elif "delta" in body:
                number = max(1, number + delta * new_span)
            if new_of is not None and number > new_of:
                if "delta" in body and "of" not in body:
                    # Next past the last box: the break grew, so extend the total.
                    new_of = number + new_span - 1
                else:
                    raise BadRequest(f"box {number} is past the total of {new_of} boxes")
            # Checks done; now change state.
            old = box["number"]
            box["span"], box["of"], box["number"] = new_span, new_of, number
            # A smaller table can't have more packs opened than it holds.
            cap = d["packsPerBox"] * (self._group_end(box) - number + 1)
            box["packsOpened"] = min(box["packsOpened"], cap)
            if number != old:
                # Stepping back (Prev) assumes those boxes were finished; anything else
                # (Next, or jumping with `set`) starts the new table at 0 packs.
                size = self._group_end(box) - number + 1
                stepped_back = "delta" in body and number < old
                box["packsOpened"] = d["packsPerBox"] * size if stepped_back else 0

        return self._mutate(apply)

    def reset(self, body):
        _require_object(body)

        def apply(d):
            d["log"] = []
            d["box"]["number"] = 1
            d["box"]["packsOpened"] = 0

        return self._mutate(apply)

    def settings(self, body):
        body = _require_object(body)
        keys = ("title", "packsPerBox", "labels", "display", "panelOpacity", "pedal")
        if not any(k in body for k in keys):
            raise BadRequest("body must contain at least one of " + ", ".join(f"'{k}'" for k in keys))

        # Validate everything before touching state so a bad field changes nothing.
        title = None
        if "title" in body:
            if not isinstance(body["title"], str):
                raise BadRequest("'title' must be a string")
            title = body["title"].strip()[:TITLE_MAX].strip()
        ppb = None
        if "packsPerBox" in body:
            ppb = body["packsPerBox"]
            if not _is_int(ppb) or ppb < 1:
                raise BadRequest("'packsPerBox' must be an integer >= 1")
        labels = None
        if "labels" in body:
            if not isinstance(body["labels"], dict):
                raise BadRequest("'labels' must be an object of {rarity id: label}")
            labels = {}
            for rid, label in body["labels"].items():
                if rid not in RARITY_IDS:
                    raise BadRequest(f"unknown rarity {rid!r} in 'labels'")
                if not isinstance(label, str):
                    raise BadRequest(f"label for {rid!r} must be a string")
                labels[rid] = label.strip()[:LABEL_MAX].strip()
        display = None
        if "display" in body:
            display = body["display"]
            if not isinstance(display, dict) or not display:
                raise BadRequest("'display' must be an object like {\"packs\": true, \"packsLeft\": false}")
            for k, v in display.items():
                if k not in DISPLAY_KEYS:
                    raise BadRequest(f"unknown display option {k!r}; expected one of: " + ", ".join(DISPLAY_KEYS))
                if not isinstance(v, bool):
                    raise BadRequest(f"display option {k!r} must be true or false")
        opacity = body.get("panelOpacity")
        if "panelOpacity" in body and not (_is_int(opacity) and 0 <= opacity <= 100):
            raise BadRequest("'panelOpacity' must be a whole number from 0 to 100")
        pedal = None
        if "pedal" in body:
            if not isinstance(body["pedal"], dict) or not body["pedal"]:
                raise BadRequest("'pedal' must be an object like {\"plus\": \"F13\", \"minus\": null}")
            pedal = {}
            for action, key in body["pedal"].items():
                if action not in PEDAL_ACTIONS:
                    raise BadRequest(f"unknown pedal action {action!r}; expected 'plus' or 'minus'")
                pedal[action] = parse_hotkey(key)[0] if key is not None else None

        def apply(d):
            if pedal is not None:
                merged = {**d["pedal"], **pedal}
                if merged["plus"] and merged["plus"] == merged["minus"]:
                    raise BadRequest(f"{merged['plus']} can't be both +1 and -1 pack")
            if title is not None:
                d["title"] = title
            if ppb is not None:
                d["packsPerBox"] = ppb
                _clamp_packs(d)
            if labels is not None:
                for rid, label in labels.items():
                    # Empty (or the default text) restores the default label.
                    if not label or label == DEFAULT_LABELS[rid]:
                        d["labels"].pop(rid, None)
                    else:
                        d["labels"][rid] = label
            if display is not None:
                d["display"].update(display)
            if opacity is not None:
                d["panelOpacity"] = opacity
            if pedal is not None:
                d["pedal"] = merged
                if self.pedal:
                    self.pedal.sync()  # re-register on the pedal thread

        return self._mutate(apply)


class Pedal:
    """Foot pedal: the pedal's key is registered as a global Windows hotkey, so a press
    counts a pack whichever window is in front (OBS included). A Win32 message loop
    runs on its own thread; other threads only post messages to it."""

    WM_TIMER = 0x0113
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    WM_SYNC = 0x8001  # WM_APP + 1: re-read the keys and re-register
    HOTKEY_IDS = {"plus": 1, "minus": 2}
    DEBOUNCE = 0.25    # seconds; pedal switches can bounce
    PAUSE_MAX = 30.0   # Learn never leaves the pedal off longer than this

    def __init__(self, store):
        self.store = store
        self.available = os.name == "nt"
        self.status = dict.fromkeys(PEDAL_ACTIONS, "off" if self.available else "unavailable")
        self._paused_until = 0.0
        self._registered_paused = False
        self._tid = None
        self._user32 = None
        self._ready = threading.Event()
        self._last_press = {}

    @property
    def paused(self):
        return time.monotonic() < self._paused_until

    def start(self):
        if self.available:
            threading.Thread(target=self._run, name="pedal", daemon=True).start()
            self._ready.wait(5)

    def stop(self):
        self._post(self.WM_QUIT)

    def sync(self):
        self._post(self.WM_SYNC)

    def set_paused(self, paused):
        self._paused_until = time.monotonic() + self.PAUSE_MAX if paused else 0.0
        self.sync()

    def _post(self, message, wparam=0):
        if self._tid is not None:
            self._user32.PostThreadMessageW(self._tid, message, wparam, 0)

    def _run(self):
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32")
        user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT)
        user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
        user32.SetTimer.restype = ctypes.c_size_t

        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates this thread's message queue
        self._user32 = user32
        self._tid = kernel32.GetCurrentThreadId()
        user32.SetTimer(None, 0, 1000, None)  # 1 s tick: ends Learn's pause if the panel never does
        self._ready.set()
        self._register_all()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY:
                    self._press(msg.wParam)
                elif msg.message == self.WM_SYNC:
                    self._register_all()
                elif msg.message == self.WM_TIMER and self._registered_paused and not self.paused:
                    self._register_all()
        finally:
            for hid in self.HOTKEY_IDS.values():
                user32.UnregisterHotKey(None, hid)

    def _register_all(self):
        keys = self.store.pedal_keys()
        paused = self.paused
        status = {}
        for action, hid in self.HOTKEY_IDS.items():
            self._user32.UnregisterHotKey(None, hid)
            key = keys.get(action)
            if not key:
                status[action] = "off"
                continue
            if paused:
                status[action] = "paused"
                continue
            try:
                _, mods, vk = parse_hotkey(key)
            except BadRequest:
                status[action] = "invalid"
                continue
            if self._user32.RegisterHotKey(None, hid, mods | _MOD_NOREPEAT, vk):
                status[action] = "active"
            else:
                err = ctypes.get_last_error()
                status[action] = "busy" if err == 1409 else f"error {err}"  # 1409: taken by another app
        self._registered_paused = paused
        self.status = status
        self.store.broadcast_state()

    def _press(self, hotkey_id):
        action = next((a for a, hid in self.HOTKEY_IDS.items() if hid == hotkey_id), None)
        now = time.monotonic()
        if action is None or now - self._last_press.get(action, 0.0) < self.DEBOUNCE:
            return
        self._last_press[action] = now
        try:
            self.store.pedal_press(action)
        except Exception as exc:  # never let one press kill the listener
            print(f"[pedal] press failed: {exc}", file=sys.stderr)


POST_ROUTES = {
    "/api/hit": "hit",
    "/api/remove": "remove",
    "/api/delete": "delete",
    "/api/rename": "rename",
    "/api/test/clear": "clear_test",
    "/api/undo": "undo",
    "/api/packs": "packs",
    "/api/next-box": "next_box",
    "/api/box": "box",
    "/api/pedal": "pedal_pause",
    "/api/reset": "reset",
    "/api/settings": "settings",
}

PAGES = {"/control": "control.html", "/overlay": "overlay.html"}

_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


class Handler(BaseHTTPRequestHandler):
    server_version = "WhatnotHitCounter/1.0"
    # HTTP/1.0: one request per connection, so the SSE stream needs no chunking.
    protocol_version = "HTTP/1.0"

    @property
    def store(self):
        return self.server.store

    def _route(self):
        path = urlsplit(self.path).path
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        return path

    # ---- responses ---------------------------------------------------------

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_not_found(self):
        self._send_json(404, {"error": f"not found: {self.command} {self._route()}"})

    def _serve_page(self, filename):
        path = WEB_DIR / filename
        try:
            body = path.read_bytes()
        except OSError:
            msg = (
                "<!doctype html><meta charset=utf-8><title>Not ready</title>"
                "<body style='font:16px system-ui,sans-serif;padding:2em;color:#eee;background:#222'>"
                f"<h1>web/{filename} not found</h1>"
                f"<p>The server is running, but <code>web/{filename}</code> has not been created yet. "
                "Add the file and reload this page &mdash; no server restart needed.</p>"
            ).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(msg)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()

        client = self.store.subscribe()
        next_ping = time.monotonic() + PING_INTERVAL
        try:
            while client.alive:
                timeout = next_ping - time.monotonic()
                msg = b""
                if timeout > 0:
                    try:
                        msg = client.queue.get(timeout=timeout)
                    except queue.Empty:
                        msg = b""
                if msg is None:  # shutdown
                    break
                if time.monotonic() >= next_ping:
                    msg += b": ping\n\n"
                    next_ping = time.monotonic() + PING_INTERVAL
                if msg:
                    self.wfile.write(msg)
                    self.wfile.flush()
        except (OSError, ValueError):
            pass  # client went away (or socket closed during shutdown)
        finally:
            self.store.unsubscribe(client)

    # ---- request body ------------------------------------------------------

    def _read_json(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return {}
        try:
            n = int(length)
        except ValueError:
            raise BadRequest("invalid Content-Length") from None
        if n < 0:
            raise BadRequest("invalid Content-Length")
        if n > MAX_BODY:
            raise BadRequest(f"request body too large (max {MAX_BODY} bytes)")
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise BadRequest("request body must be UTF-8 JSON") from None
        except ValueError as exc:
            raise BadRequest(f"invalid JSON: {exc}") from None

    # ---- methods -----------------------------------------------------------

    def do_GET(self):
        try:
            path = self._route()
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/control")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path in PAGES:
                self._serve_page(PAGES[path])
            elif path == "/api/state":
                self._send_json(200, self.store.state())
            elif path == "/api/events":
                self._serve_events()
            else:
                self._send_not_found()
        except _DISCONNECT_ERRORS:
            self.close_connection = True

    def do_POST(self):
        try:
            action = POST_ROUTES.get(self._route())
            if action is None:
                self._send_not_found()
                return
            try:
                state = getattr(self.store, action)(self._read_json())
            except BadRequest as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, state)
        except _DISCONNECT_ERRORS:
            self.close_connection = True

    # Only log problems; the panel and overlay would otherwise flood the console.
    def log_request(self, code="-", size="-"):
        try:
            if int(code) >= 400:
                super().log_request(code, size)
        except (TypeError, ValueError):
            super().log_request(code, size)


class HitCounterServer(ThreadingHTTPServer):
    daemon_threads = True  # open SSE connections must not block shutdown
    block_on_close = False
    # On Windows SO_REUSEADDR lets a second server silently share the port;
    # use exclusive binding there instead.
    allow_reuse_address = os.name != "nt"

    def __init__(self, server_address, store):
        self.store = store
        super().__init__(server_address, Handler)

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skip HTTPServer.server_bind's socket.getfqdn(), which can stall for
        # seconds on Windows.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], _DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)

    def server_close(self):
        self.store.close()
        super().server_close()


def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Whatnot Yu-Gi-Oh hit counter server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default 127.0.0.1; 0.0.0.0 = reachable on the LAN)")
    parser.add_argument("--port", type=int, default=7777, help="port (default 7777)")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH),
                        help="state file (default data/state.json next to server.py)")
    args = parser.parse_args(argv)

    store = Store(args.state)
    store.pedal = Pedal(store)
    store.pedal.start()
    try:
        httpd = HitCounterServer((args.host, args.port), store)
    except OSError as exc:
        print(f"Could not listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        print("Is the server already running? Try another port with --port.", file=sys.stderr)
        return 1

    port = httpd.server_address[1]
    if args.host in ("0.0.0.0", ""):
        hosts = ["127.0.0.1"]
        lan = _lan_ip()
        if lan and lan != "127.0.0.1":
            hosts.append(lan)
    else:
        hosts = [args.host]

    print("Whatnot hit counter running.")
    for h in hosts:
        print(f"  Control panel: http://{h}:{port}/control")
    for h in hosts:
        print(f"  OBS overlay:   http://{h}:{port}/overlay")
    print(f"  State file:    {store.path}")
    if store.pedal.available:
        keys = store.pedal_keys()
        set_keys = [f"{k} -> {'+1' if a == 'plus' else '-1'} pack ({store.pedal.status[a]})"
                    for a, k in keys.items() if k]
        print("  Foot pedal:    " + ("; ".join(set_keys) if set_keys else "not set (control panel > Foot pedal)"))
    print("Press Ctrl+C to stop.", flush=True)

    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break: stop cleanly like Ctrl+C
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        store.pedal.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
