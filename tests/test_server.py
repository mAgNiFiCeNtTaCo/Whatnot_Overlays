"""Integration tests for the Whatnot hit counter (stdlib unittest only).

Run from the project root:

    python -m unittest discover tests

Most tests run the real server in-process on a free port with a temp state file.
One test launches `python server.py` as a subprocess to check the command line.
"""

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402

# The server logs every 4xx to stderr; the validation tests make hundreds on purpose.
server.Handler.log_message = lambda self, *args: None

IDS = ["ultra", "secret", "overframe", "starlight", "starlight_overframe"]
HOST = "127.0.0.1"
# Generous on purpose: some Windows PCs (security software filtering loopback) stall a
# burst of fresh localhost connections for 1-15 s. That slows a run; it must not fail it.
TIMEOUT = 30


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


class ServerHarness:
    """Real HitCounterServer on a free port, backed by a state file in `tmpdir`."""

    def __init__(self, state_path):
        self.state_path = Path(state_path)
        self.store = server.Store(self.state_path)
        self.httpd = server.HitCounterServer((HOST, 0), self.store)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    # ---- HTTP helpers ------------------------------------------------------

    def request(self, method, path, body=None, raw=None, headers=None):
        conn = http.client.HTTPConnection(HOST, self.port, timeout=TIMEOUT)
        try:
            hdrs = dict(headers or {})
            data = raw
            if body is not None:
                data = json.dumps(body).encode("utf-8")
            if data is not None:
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def get_json(self, path):
        status, headers, payload = self.request("GET", path)
        return status, headers, json.loads(payload)

    def post(self, path, body=None, raw=None):
        status, headers, payload = self.request("POST", path, body=body, raw=raw)
        return status, json.loads(payload) if payload else None

    def state(self):
        return self.get_json("/api/state")[2]


class SSEClient:
    """Minimal EventSource: yields (event, data, comment) from /api/events."""

    def __init__(self, port, timeout=TIMEOUT):
        self.conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
        self.conn.request("GET", "/api/events")
        self.resp = self.conn.getresponse()

    def next_frame(self):
        """Return the next frame as a dict: {'event', 'data' (raw str), 'comments', 'data_lines'}."""
        event, data_lines, comments = "message", [], []
        while True:
            line = self.resp.readline()
            if not line:
                raise EOFError("SSE stream closed")
            line = line.decode("utf-8").rstrip("\r\n")
            if line == "":
                if data_lines or comments:
                    return {"event": event, "data": "\n".join(data_lines),
                            "data_lines": len(data_lines), "comments": comments}
                continue
            if line.startswith(":"):
                comments.append(line[1:].strip())
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

    def next_event(self):
        """Next frame that carries data (skips heartbeat-only frames)."""
        while True:
            f = self.next_frame()
            if f["data_lines"]:
                return f["event"], json.loads(f["data"]), f

    def close(self):
        # The response holds its own reference to the socket; close both.
        self.resp.close()
        self.conn.close()


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.state_path = self.tmpdir / "data" / "state.json"
        self.srv = ServerHarness(self.state_path)

    def tearDown(self):
        self.srv.stop()
        self._tmp.cleanup()

    def restart(self):
        self.srv.stop()
        self.srv = ServerHarness(self.state_path)

    def hit(self, rarity, card=None):
        body = {"rarity": rarity}
        if card is not None:
            body["card"] = card
        status, state = self.srv.post("/api/hit", body)
        self.assertEqual(status, 200, state)
        return state

    def assertBadRequest(self, path, body=None, raw=None):
        status, payload = self.srv.post(path, body=body, raw=raw)
        self.assertEqual(status, 400, f"{path} {body or raw!r} -> {status} {payload}")
        self.assertIsInstance(payload.get("error"), str)
        self.assertTrue(payload["error"])


# --------------------------------------------------------------------------
# Routes and response shapes
# --------------------------------------------------------------------------


class RouteTests(ServerTestCase):
    def test_root_redirects_to_control(self):
        status, headers, _ = self.srv.request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/control")

    def test_pages_are_served(self):
        for path, marker in (("/control", b"<title>Hit Counter Control"),
                             ("/overlay", b"<title>Hit Counter Overlay"),
                             ("/overlay?layout=column&show=both&hide=ultra&scale=1.5", b"<title>Hit Counter Overlay")):
            status, headers, body = self.srv.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith("text/html"), path)
            self.assertIn(marker, body, path)

    def test_state_shape_matches_contract(self):
        status, headers, s = self.srv.get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(set(s), {"title", "rarities", "box", "session", "display", "panelOpacity", "pedal", "log", "testLog", "updatedAt"})
        self.assertEqual(s["title"], "Yu-Gi-Oh Box Break")
        self.assertEqual([r["id"] for r in s["rarities"]], IDS)
        self.assertEqual(s["rarities"][0], {"id": "ultra", "label": "Ultra Rare", "short": "UR", "color": "#f5c542"})
        self.assertEqual(
            [(r["short"], r["color"]) for r in s["rarities"]],
            [("UR", "#f5c542"), ("SCR", "#c0c8d8"), ("OF", "#ff6b35"), ("STR", "#7fd4ff"), ("SOF", "#d48cff")],
        )
        self.assertEqual(set(s["box"]), {"number", "to", "span", "of", "packsOpened", "packsPerBox",
                                         "packsTotal", "counts", "total"})
        self.assertEqual((s["box"]["to"], s["box"]["span"], s["box"]["of"], s["box"]["packsTotal"]), (1, 1, None, 24))
        self.assertEqual(s["display"], {"packs": True, "boxes": True, "packsLeft": True, "tracker": True, "caseLog": True, "showcase": False})
        self.assertEqual(s["box"]["number"], 1)
        self.assertEqual(s["box"]["packsOpened"], 0)
        self.assertEqual(s["box"]["packsPerBox"], 24)
        self.assertEqual(s["box"]["counts"], dict.fromkeys(IDS, 0))
        self.assertEqual(s["box"]["total"], 0)
        self.assertEqual(s["session"], {"counts": dict.fromkeys(IDS, 0), "total": 0, "boxes": 1})
        self.assertEqual(s["log"], [])
        self.assertIsInstance(s["updatedAt"], float)

    def test_unknown_paths_and_wrong_methods_are_404_json(self):
        for method, path in (("GET", "/nope"), ("POST", "/api/nope"), ("GET", "/api/hit"), ("POST", "/api/state")):
            status, headers, body = self.srv.request(method, path, body={} if method == "POST" else None)
            self.assertEqual(status, 404, f"{method} {path}")
            self.assertIn("error", json.loads(body))

    def test_every_post_returns_full_state_with_no_store(self):
        calls = [
            ("/api/hit", {"rarity": "ultra"}),
            ("/api/remove", {"rarity": "ultra"}),
            ("/api/hit", {"rarity": "secret"}),
            ("/api/delete", {"id": "zzzzzz"}),
            ("/api/undo", {}),
            ("/api/packs", {"delta": 1}),
            ("/api/next-box", {}),
            ("/api/box", {"delta": -1}),
            ("/api/reset", {}),
            ("/api/settings", {"title": "x"}),
            ("/api/test/clear", {}),
        ]
        last = 0.0
        for path, body in calls:
            status, headers, payload = self.srv.request("POST", path, body=body)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)
            s = json.loads(payload)
            self.assertEqual(set(s), {"title", "rarities", "box", "session", "display", "panelOpacity", "pedal", "log", "testLog", "updatedAt"}, path)
            self.assertGreaterEqual(s["updatedAt"], last, path)
            last = s["updatedAt"]

    def test_empty_post_body_counts_as_empty_object(self):
        status, _, _ = self.srv.request("POST", "/api/undo")  # no body, no Content-Length
        self.assertEqual(status, 200)
        status, _ = self.srv.post("/api/next-box", raw=b"")
        self.assertEqual(status, 200)


# --------------------------------------------------------------------------
# Hits, log and derived counts
# --------------------------------------------------------------------------


class HitTests(ServerTestCase):
    def test_hit_appends_entry(self):
        before = time.time()
        s = self.hit("secret", "Snake-Eye Ash")
        self.assertEqual(len(s["log"]), 1)
        e = s["log"][0]
        self.assertEqual(set(e), {"id", "rarity", "card", "box", "boxTo", "ts"})
        self.assertEqual(e["rarity"], "secret")
        self.assertEqual(e["card"], "Snake-Eye Ash")
        self.assertEqual(e["box"], 1)
        self.assertIsInstance(e["id"], str)
        self.assertTrue(e["id"])
        self.assertGreaterEqual(e["ts"], before - 1)
        self.assertLessEqual(e["ts"], time.time() + 1)
        self.assertEqual(s["box"]["counts"]["secret"], 1)
        self.assertEqual(s["box"]["total"], 1)
        self.assertEqual(s["session"]["total"], 1)

    def test_card_is_trimmed_capped_and_nulled(self):
        self.assertEqual(self.hit("ultra", "   Ash Blossom  ")["log"][-1]["card"], "Ash Blossom")
        self.assertEqual(len(self.hit("ultra", "X" * 200)["log"][-1]["card"]), 80)
        self.assertIsNone(self.hit("ultra", "   ")["log"][-1]["card"])
        self.assertIsNone(self.hit("ultra", "")["log"][-1]["card"])
        self.assertIsNone(self.hit("ultra")["log"][-1]["card"])
        self.assertIsNone(self.hit("ultra", None)["log"][-1]["card"])
        self.assertEqual(self.hit("ultra", "Blue-Eyes ✨ ドラゴン")["log"][-1]["card"], "Blue-Eyes ✨ ドラゴン")

    def test_auto_flag_marks_camera_hits(self):
        s = self.srv.post("/api/hit", {"rarity": "starlight", "auto": True})[1]
        self.assertIs(s["log"][-1]["auto"], True)
        s = self.srv.post("/api/hit", {"rarity": "ultra", "auto": False})[1]
        self.assertNotIn("auto", s["log"][-1])  # hand-logged entries look as before
        self.assertBadRequest("/api/hit", {"rarity": "ultra", "auto": "yes"})
        self.restart()
        self.assertEqual([e.get("auto") for e in self.srv.state()["log"]], [True, None])

    def test_rename_changes_a_logged_name(self):
        s = self.hit("secret", "Snake-Eye Ash")
        eid = s["log"][-1]["id"]
        s = self.srv.post("/api/rename", {"id": eid, "card": "  Ash Blossom & Joyous Spring  "})[1]
        self.assertEqual(s["log"][-1]["card"], "Ash Blossom & Joyous Spring")
        self.assertEqual(s["log"][-1]["rarity"], "secret")
        self.assertIsNone(self.srv.post("/api/rename", {"id": eid, "card": None})[1]["log"][-1]["card"])
        self.assertBadRequest("/api/rename", {"id": "nope", "card": "X"})
        self.assertBadRequest("/api/rename", {"card": "X"})
        self.assertBadRequest("/api/rename", {"id": eid, "card": 5})

    def test_test_hits_are_listed_but_never_counted(self):
        self.hit("ultra")
        s = self.srv.post("/api/hit", {"rarity": "starlight", "card": "Solemn Judgment", "auto": True, "test": True})[1]
        self.assertEqual([(e["rarity"], e["card"]) for e in s["testLog"]], [("starlight", "Solemn Judgment")])
        self.assertEqual([e["rarity"] for e in s["log"]], ["ultra"])  # the real log is untouched
        self.assertEqual((s["box"]["total"], s["session"]["total"], s["session"]["counts"]["starlight"]), (1, 1, 0))
        tid = s["testLog"][0]["id"]
        s = self.srv.post("/api/rename", {"id": tid, "card": "Monster Reborn"})[1]
        self.assertEqual(s["testLog"][0]["card"], "Monster Reborn")
        self.restart()  # kept on disk like the real log
        self.assertEqual(len(self.srv.state()["testLog"]), 1)
        s = self.srv.post("/api/delete", {"id": tid})[1]
        self.assertEqual(s["testLog"], [])
        self.srv.post("/api/hit", {"rarity": "secret", "test": True})
        s = self.srv.post("/api/test/clear", {})[1]
        self.assertEqual((s["testLog"], len(s["log"])), ([], 1))
        self.assertBadRequest("/api/hit", {"rarity": "ultra", "test": "yes"})

    def test_ids_are_unique(self):
        for _ in range(30):
            self.hit("ultra")
        ids = [e["id"] for e in self.srv.state()["log"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_counts_across_next_box(self):
        self.hit("ultra")
        self.hit("ultra")
        self.hit("starlight", "Accesscode Talker")
        s = self.srv.post("/api/packs", {"set": 24})[1]
        self.assertEqual(s["box"]["packsOpened"], 24)

        s = self.srv.post("/api/next-box", {})[1]
        self.assertEqual(s["box"]["number"], 2)
        self.assertEqual(s["box"]["packsOpened"], 0)
        self.assertEqual(s["box"]["counts"], dict.fromkeys(IDS, 0))
        self.assertEqual(s["box"]["total"], 0)
        self.assertEqual(s["session"]["counts"]["ultra"], 2)
        self.assertEqual(s["session"]["counts"]["starlight"], 1)
        self.assertEqual(s["session"]["total"], 3)
        self.assertEqual(s["session"]["boxes"], 2)

        self.hit("ultra")
        s = self.hit("secret")
        self.assertEqual(s["log"][-1]["box"], 2)
        self.assertEqual(s["box"]["counts"], {"ultra": 1, "secret": 1, "overframe": 0, "starlight": 0, "starlight_overframe": 0})
        self.assertEqual(s["box"]["total"], 2)
        self.assertEqual(s["session"]["counts"], {"ultra": 3, "secret": 1, "overframe": 0, "starlight": 1, "starlight_overframe": 0})
        self.assertEqual(s["session"]["total"], 5)

        s = self.srv.post("/api/next-box", {})[1]
        self.assertEqual(s["session"]["boxes"], 3)
        self.assertEqual(s["box"]["total"], 0)
        self.assertEqual(s["session"]["total"], 5)

    def test_remove_only_touches_current_box(self):
        self.hit("ultra", "box1-a")
        self.hit("ultra", "box1-b")
        self.srv.post("/api/next-box", {})
        # Nothing of that rarity in box 2: no-op, still 200.
        status, s = self.srv.post("/api/remove", {"rarity": "ultra"})
        self.assertEqual(status, 200)
        self.assertEqual(s["session"]["counts"]["ultra"], 2)

        self.hit("ultra", "box2-a")
        self.hit("secret", "box2-s")
        self.hit("ultra", "box2-b")
        s = self.srv.post("/api/remove", {"rarity": "ultra"})[1]
        # Most recent ultra of the current box is gone, nothing else.
        self.assertEqual([e["card"] for e in s["log"]], ["box1-a", "box1-b", "box2-a", "box2-s"])
        self.assertEqual(s["box"]["counts"]["ultra"], 1)
        self.assertEqual(s["session"]["counts"]["ultra"], 3)

    def test_delete_by_id(self):
        a = self.hit("ultra", "a")["log"][-1]
        self.hit("secret", "b")
        self.hit("overframe", "c")
        s = self.srv.post("/api/delete", {"id": a["id"]})[1]
        self.assertEqual([e["card"] for e in s["log"]], ["b", "c"])
        self.assertEqual(s["box"]["counts"]["ultra"], 0)
        # Unknown id: 200 no-op.
        status, s2 = self.srv.post("/api/delete", {"id": a["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(s2["log"], s["log"])

    def test_undo_removes_most_recent_overall(self):
        self.hit("ultra", "a")
        self.srv.post("/api/next-box", {})
        self.hit("secret", "b")
        s = self.srv.post("/api/undo", {})[1]
        self.assertEqual([e["card"] for e in s["log"]], ["a"])
        # Undo again reaches back into the previous box (contract: "overall").
        s = self.srv.post("/api/undo", {})[1]
        self.assertEqual(s["log"], [])
        self.assertEqual(s["box"]["number"], 2)  # undo never changes the box number
        status, s = self.srv.post("/api/undo", {})
        self.assertEqual(status, 200)
        self.assertEqual(s["log"], [])

    def test_log_cap_drops_oldest(self):
        self.assertEqual(server.LOG_CAP, 1000)
        old_cap = server.LOG_CAP
        server.LOG_CAP = 5  # same code path, fewer requests
        try:
            for i in range(8):
                self.hit("ultra" if i % 2 else "secret", f"c{i}")
            s = self.srv.state()
        finally:
            server.LOG_CAP = old_cap
        self.assertEqual([e["card"] for e in s["log"]], ["c3", "c4", "c5", "c6", "c7"])
        self.assertEqual(s["session"]["total"], 5)
        self.assertEqual(s["session"]["counts"]["ultra"], 3)
        self.assertEqual(s["session"]["counts"]["secret"], 2)

    def test_log_cap_at_real_size(self):
        # 1005 hits straight into the store (no HTTP) to prove the real cap.
        for i in range(1005):
            self.srv.store.hit({"rarity": "ultra", "card": str(i)})
        s = self.srv.state()
        self.assertEqual(len(s["log"]), 1000)
        self.assertEqual(s["log"][0]["card"], "5")
        self.assertEqual(s["log"][-1]["card"], "1004")
        self.assertEqual(s["session"]["total"], 1000)


# --------------------------------------------------------------------------
# Packs, next-box, reset, settings
# --------------------------------------------------------------------------


class BoxAndSettingsTests(ServerTestCase):
    def test_packs_delta_set_and_clamp(self):
        self.assertEqual(self.srv.post("/api/packs", {"delta": 3})[1]["box"]["packsOpened"], 3)
        self.assertEqual(self.srv.post("/api/packs", {"delta": -1})[1]["box"]["packsOpened"], 2)
        self.assertEqual(self.srv.post("/api/packs", {"delta": -10})[1]["box"]["packsOpened"], 0)
        self.assertEqual(self.srv.post("/api/packs", {"set": 17})[1]["box"]["packsOpened"], 17)
        self.assertEqual(self.srv.post("/api/packs", {"set": -4})[1]["box"]["packsOpened"], 0)
        # Never more than the boxes on the table hold (1 box x 24 here).
        self.assertEqual(self.srv.post("/api/packs", {"set": 30})[1]["box"]["packsOpened"], 24)

    def test_reset_keeps_title_and_settings(self):
        self.srv.post("/api/settings", {"title": "Friday Break", "packsPerBox": 36, "labels": {"ultra": "Ultra!"}})
        self.hit("ultra")
        self.srv.post("/api/packs", {"set": 9})
        self.srv.post("/api/next-box", {})
        self.hit("secret")
        s = self.srv.post("/api/reset", {})[1]
        self.assertEqual(s["log"], [])
        self.assertEqual(s["box"]["number"], 1)
        self.assertEqual(s["box"]["packsOpened"], 0)
        self.assertEqual(s["box"]["total"], 0)
        self.assertEqual(s["session"], {"counts": dict.fromkeys(IDS, 0), "total": 0, "boxes": 1})
        self.assertEqual(s["title"], "Friday Break")
        self.assertEqual(s["box"]["packsPerBox"], 36)
        self.assertEqual(s["rarities"][0]["label"], "Ultra!")

    def test_settings(self):
        s = self.srv.post("/api/settings", {"title": "  My Break  "})[1]
        self.assertEqual(s["title"], "My Break")
        s = self.srv.post("/api/settings", {"title": "T" * 200})[1]
        self.assertEqual(len(s["title"]), 80)
        s = self.srv.post("/api/settings", {"packsPerBox": 12})[1]
        self.assertEqual(s["box"]["packsPerBox"], 12)
        self.assertEqual(len(s["title"]), 80)  # untouched fields are kept

        s = self.srv.post("/api/settings", {"labels": {"secret": "Secret!!", "starlight": "Starlight ✨"}})[1]
        labels = {r["id"]: r["label"] for r in s["rarities"]}
        self.assertEqual(labels["secret"], "Secret!!")
        self.assertEqual(labels["starlight"], "Starlight ✨")
        self.assertEqual(labels["ultra"], "Ultra Rare")
        self.assertEqual([r["id"] for r in s["rarities"]], IDS)  # order never changes
        self.assertEqual(s["rarities"][1]["short"], "SCR")  # only label is overridable
        self.assertEqual(s["rarities"][1]["color"], "#c0c8d8")

        # Empty label restores the default; sending all five (as the control panel does) works.
        s = self.srv.post("/api/settings", {"labels": {"secret": "", "starlight": "Starlight ✨"}})[1]
        labels = {r["id"]: r["label"] for r in s["rarities"]}
        self.assertEqual(labels["secret"], "Secret Rare")
        self.assertEqual(labels["starlight"], "Starlight ✨")

        s = self.srv.post("/api/settings", {"labels": {"ultra": "U" * 100}})[1]
        self.assertEqual(len(s["rarities"][0]["label"]), 40)

    def test_settings_survive_restart(self):
        self.srv.post("/api/settings", {"title": "Saved", "packsPerBox": 10, "labels": {"overframe": "OF!"}})
        self.restart()
        s = self.srv.state()
        self.assertEqual(s["title"], "Saved")
        self.assertEqual(s["box"]["packsPerBox"], 10)
        self.assertEqual(s["rarities"][2]["label"], "OF!")


class BoxGroupTests(ServerTestCase):
    """Boxes on the table (number..to, `span` at a time) out of a total (`of`)."""

    def box(self, s):
        b = s["box"]
        return b["number"], b["to"], b["span"], b["of"], b["packsOpened"]

    def test_case_of_12_four_at_a_time(self):
        s = self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})[1]
        self.assertEqual(self.box(s), (1, 4, 4, 12, 0))
        self.assertEqual(s["box"]["packsTotal"], 96)
        self.assertEqual(s["session"]["boxes"], 4)
        s = self.srv.post("/api/next-box", {})[1]
        self.assertEqual(self.box(s), (5, 8, 4, 12, 0))
        s = self.srv.post("/api/next-box", {})[1]
        self.assertEqual(self.box(s)[:4], (9, 12, 4, 12))
        self.assertEqual(s["session"]["boxes"], 12)

    def test_last_group_is_capped_at_total(self):
        s = self.srv.post("/api/box", {"set": 9, "span": 4, "of": 10})[1]
        self.assertEqual(self.box(s)[:4], (9, 10, 4, 10))
        self.assertEqual(s["box"]["packsTotal"], 48)

    def test_next_past_total_extends_it(self):
        self.srv.post("/api/box", {"set": 9, "span": 4, "of": 12})
        s = self.srv.post("/api/next-box", {})[1]
        self.assertEqual(self.box(s)[:4], (13, 16, 4, 16))

    def test_prev_goes_back_a_group_with_packs_full(self):
        self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})
        self.srv.post("/api/next-box", {})
        s = self.srv.post("/api/box", {"delta": -1})[1]
        self.assertEqual(self.box(s), (1, 4, 4, 12, 96))
        s = self.srv.post("/api/box", {"delta": -1})[1]
        self.assertEqual(self.box(s)[0], 1)  # never below box 1

    def test_set_back_to_box_1_starts_fresh(self):
        # What the panel sends after one-at-a-time up to box 4, then "4 at a time, 12 total".
        self.srv.post("/api/box", {"set": 4})
        self.srv.post("/api/packs", {"set": 10})
        s = self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})[1]
        self.assertEqual(self.box(s), (1, 4, 4, 12, 0))
        self.assertEqual(s["box"]["packsTotal"], 96)

    def test_span_alone_keeps_packs_and_first_box(self):
        self.srv.post("/api/box", {"set": 1, "span": 4})
        self.srv.post("/api/packs", {"set": 30})
        s = self.srv.post("/api/box", {"span": 2})[1]
        self.assertEqual(self.box(s), (1, 2, 2, None, 30))
        s = self.srv.post("/api/box", {"span": 1})[1]
        self.assertEqual(self.box(s), (1, 1, 1, None, 24))  # clamped to the smaller table

    def test_packs_stop_at_full_then_next_pack_moves_on(self):
        self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})
        s = self.srv.post("/api/packs", {"set": 95})[1]
        s = self.srv.post("/api/packs", {"delta": 1})[1]
        self.assertEqual(self.box(s), (1, 4, 4, 12, 96))       # stops at 96/96
        s = self.srv.post("/api/packs", {"set": 200})[1]
        self.assertEqual(self.box(s), (1, 4, 4, 12, 96))       # set can't go over either
        s = self.srv.post("/api/packs", {"delta": 1})[1]
        self.assertEqual(self.box(s), (5, 8, 4, 12, 0))        # next pack: boxes 5-8 at 0/96
        self.assertEqual(s["box"]["packsTotal"], 96)

    def test_saved_or_shrunk_packs_are_clamped(self):
        self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})
        self.srv.post("/api/packs", {"set": 96})
        s = self.srv.post("/api/settings", {"packsPerBox": 20})[1]
        self.assertEqual(s["box"]["packsOpened"], 80)
        # A state file saved by an older version with packs over the top loads clamped.
        self.srv.stop()
        raw = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        raw["box"]["packsOpened"] = 104
        Path(self.state_path).write_text(json.dumps(raw), encoding="utf-8")
        self.srv = ServerHarness(self.state_path)
        self.assertEqual(self.srv.state()["box"]["packsOpened"], 80)

    def test_last_boxes_stay_full(self):
        self.srv.post("/api/box", {"set": 9, "span": 4, "of": 12})
        self.srv.post("/api/packs", {"set": 96})
        s = self.srv.post("/api/packs", {"delta": 1})[1]
        self.assertEqual(self.box(s), (9, 12, 4, 12, 96))      # case done: nothing next

    def test_full_table_moves_on_without_a_total(self):
        self.srv.post("/api/packs", {"set": 24})
        s = self.srv.post("/api/packs", {"delta": 1})[1]
        self.assertEqual(self.box(s), (2, 2, 1, None, 0))

    def test_total_alone_and_clearing_it(self):
        s = self.srv.post("/api/box", {"of": 12})[1]
        self.assertEqual(self.box(s)[:4], (1, 1, 1, 12))
        s = self.srv.post("/api/box", {"of": None})[1]
        self.assertIsNone(s["box"]["of"])

    def test_one_at_a_time_still_works(self):
        s = self.srv.post("/api/next-box", {})[1]
        self.assertEqual(self.box(s)[:4], (2, 2, 1, None))
        self.assertEqual(s["box"]["packsTotal"], 24)

    def test_hits_are_tagged_with_the_table(self):
        self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})
        e = self.hit("ultra")["log"][-1]
        self.assertEqual((e["box"], e["boxTo"]), (1, 4))
        self.srv.post("/api/next-box", {})
        s = self.hit("secret")
        self.assertEqual((s["log"][-1]["box"], s["log"][-1]["boxTo"]), (5, 8))
        self.assertEqual(s["box"]["counts"]["ultra"], 0)
        self.assertEqual(s["box"]["counts"]["secret"], 1)
        self.assertEqual(s["session"]["total"], 2)
        s = self.srv.post("/api/box", {"delta": -1})[1]
        self.assertEqual(s["box"]["counts"]["ultra"], 1)

    def test_reset_keeps_table_size_and_total(self):
        self.srv.post("/api/box", {"set": 5, "span": 4, "of": 12})
        self.hit("ultra")
        s = self.srv.post("/api/reset", {})[1]
        self.assertEqual(self.box(s), (1, 4, 4, 12, 0))

    def test_display_toggles(self):
        s = self.srv.post("/api/settings", {"display": {"packs": False}})[1]
        self.assertEqual(s["display"], {"packs": False, "boxes": True, "packsLeft": True, "tracker": True, "caseLog": True, "showcase": False})
        s = self.srv.post("/api/settings", {"display": {"boxes": False, "packs": True}})[1]
        self.assertEqual(s["display"], {"packs": True, "boxes": False, "packsLeft": True, "tracker": True, "caseLog": True, "showcase": False})
        s = self.srv.post("/api/settings", {"display": {"tracker": False}})[1]
        self.assertFalse(s["display"]["tracker"])
        s = self.srv.post("/api/settings", {"display": {"tracker": True}})[1]
        s = self.srv.post("/api/settings", {"display": {"caseLog": False}})[1]
        self.assertFalse(s["display"]["caseLog"])
        s = self.srv.post("/api/settings", {"display": {"caseLog": True}})[1]
        s = self.srv.post("/api/settings", {"display": {"showcase": True}})[1]
        self.assertTrue(s["display"]["showcase"])
        s = self.srv.post("/api/settings", {"display": {"showcase": False}})[1]
        s = self.srv.post("/api/settings", {"display": {"packsLeft": False}})[1]
        self.assertEqual(s["display"], {"packs": True, "boxes": False, "packsLeft": False, "tracker": True, "caseLog": True, "showcase": False})

    def test_panel_opacity(self):
        self.assertEqual(self.srv.state()["panelOpacity"], 85)
        s = self.srv.post("/api/settings", {"panelOpacity": 0})[1]
        self.assertEqual(s["panelOpacity"], 0)
        s = self.srv.post("/api/settings", {"panelOpacity": 100})[1]
        self.assertEqual(s["panelOpacity"], 100)
        for bad in (-1, 101, 50.5, "50", True, None):
            self.assertBadRequest("/api/settings", {"panelOpacity": bad})
        self.srv.post("/api/settings", {"panelOpacity": 40})
        self.restart()
        self.assertEqual(self.srv.state()["panelOpacity"], 40)

    def test_table_and_display_survive_restart(self):
        self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})
        self.srv.post("/api/settings", {"display": {"packs": False}})
        self.srv.post("/api/next-box", {})
        e = self.hit("starlight")["log"][-1]
        self.restart()
        s = self.srv.state()
        self.assertEqual(self.box(s)[:4], (5, 8, 4, 12))
        self.assertEqual(s["display"], {"packs": False, "boxes": True, "packsLeft": True, "tracker": True, "caseLog": True, "showcase": False})
        self.assertEqual(s["log"][-1], e)

    def test_bad_box_input(self):
        self.srv.post("/api/box", {"set": 5, "span": 4, "of": 12})
        before = self.srv.state()
        for path, body in (
            ("/api/box", {}),
            ("/api/box", {"delta": 1, "set": 2}),
            ("/api/box", {"delta": "1"}),
            ("/api/box", {"set": 0}),
            ("/api/box", {"span": 0}),
            ("/api/box", {"span": 101}),
            ("/api/box", {"span": "4"}),
            ("/api/box", {"of": 0}),
            ("/api/box", {"of": 4}),             # less than the first box on the table
            ("/api/box", {"set": 13}),           # past the total
            ("/api/box", {"set": 2, "of": 1}),
            ("/api/settings", {"boxStart": 1}),  # old range fields are gone
            ("/api/settings", {"display": {}}),
            ("/api/settings", {"display": {"packs": 1}}),
            ("/api/settings", {"display": {"title": False}}),
            ("/api/settings", {"display": True}),
        ):
            status, payload = self.srv.post(path, body)
            self.assertEqual(status, 400, (path, body))
            self.assertIn("error", payload)
        after = self.srv.state()
        self.assertEqual(before["box"], after["box"])
        self.assertEqual(before["display"], after["display"])


# --------------------------------------------------------------------------
# Validation: every bad input is a 400 with {"error": ...} and changes nothing
# --------------------------------------------------------------------------


class ValidationTests(ServerTestCase):
    def test_bad_json(self):
        for path in server.POST_ROUTES:
            self.assertBadRequest(path, raw=b"{not json")
            self.assertBadRequest(path, raw=b"[1, 2]")
            self.assertBadRequest(path, raw=b'"a string"')
            self.assertBadRequest(path, raw=b"\xff\xfe")

    def test_bad_hit_and_remove(self):
        for path in ("/api/hit", "/api/remove"):
            self.assertBadRequest(path, {})
            self.assertBadRequest(path, {"rarity": "common"})
            self.assertBadRequest(path, {"rarity": "ULTRA"})
            self.assertBadRequest(path, {"rarity": 1})
            self.assertBadRequest(path, {"rarity": None})
            self.assertBadRequest(path, {"rarity": ["ultra"]})
        self.assertBadRequest("/api/hit", {"rarity": "ultra", "card": 5})
        self.assertBadRequest("/api/hit", {"rarity": "ultra", "card": ["x"]})

    def test_bad_delete(self):
        self.assertBadRequest("/api/delete", {})
        self.assertBadRequest("/api/delete", {"id": ""})
        self.assertBadRequest("/api/delete", {"id": 123})

    def test_bad_packs(self):
        self.assertBadRequest("/api/packs", {})
        self.assertBadRequest("/api/packs", {"delta": 1, "set": 2})
        self.assertBadRequest("/api/packs", {"delta": "1"})
        self.assertBadRequest("/api/packs", {"delta": 1.5})
        self.assertBadRequest("/api/packs", {"delta": True})
        self.assertBadRequest("/api/packs", {"set": None})

    def test_bad_settings(self):
        self.assertBadRequest("/api/settings", {})
        self.assertBadRequest("/api/settings", {"title": 5})
        self.assertBadRequest("/api/settings", {"packsPerBox": 0})
        self.assertBadRequest("/api/settings", {"packsPerBox": -3})
        self.assertBadRequest("/api/settings", {"packsPerBox": "24"})
        self.assertBadRequest("/api/settings", {"packsPerBox": 2.5})
        self.assertBadRequest("/api/settings", {"packsPerBox": True})
        self.assertBadRequest("/api/settings", {"labels": "Ultra"})
        self.assertBadRequest("/api/settings", {"labels": {"common": "x"}})
        self.assertBadRequest("/api/settings", {"labels": {"ultra": 1}})
        # A bad field rejects the whole request: the good title must not be applied.
        self.assertBadRequest("/api/settings", {"title": "Should not stick", "packsPerBox": 0})
        self.assertEqual(self.srv.state()["title"], "Yu-Gi-Oh Box Break")

    def test_bad_content_length(self):
        status, _, body = self.srv.request("POST", "/api/undo", raw=b"{}", headers={"Content-Length": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_rejected_requests_do_not_change_state(self):
        self.hit("ultra")
        before = self.srv.state()
        self.assertBadRequest("/api/hit", {"rarity": "nope"})
        self.assertBadRequest("/api/packs", {"delta": "x"})
        self.assertEqual(self.srv.state(), before)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class PedalTests(ServerTestCase):
    def test_parse_hotkey(self):
        self.assertEqual(server.parse_hotkey("f13")[0], "F13")
        self.assertEqual(server.parse_hotkey("alt+ctrl+p")[0], "Ctrl+Alt+P")
        self.assertEqual(server.parse_hotkey("Shift+F5")[0], "Shift+F5")
        self.assertEqual(server.parse_hotkey("numpadadd")[0], "NumpadAdd")
        self.assertEqual(server.parse_hotkey("F24")[2], 0x87)
        for bad in ("", "B", "Shift+B", "7", "F25", "Hyper+F1", "Ctrl+", None, 5):
            with self.assertRaises(server.BadRequest, msg=repr(bad)):
                server.parse_hotkey(bad)

    def test_pedal_settings(self):
        s = self.srv.state()
        self.assertEqual({k: s["pedal"][k] for k in ("plus", "minus")}, {"plus": None, "minus": None})
        self.assertFalse(s["pedal"]["available"])  # no listener in the test harness
        s = self.srv.post("/api/settings", {"pedal": {"plus": "f13"}})[1]
        self.assertEqual((s["pedal"]["plus"], s["pedal"]["minus"]), ("F13", None))
        s = self.srv.post("/api/settings", {"pedal": {"minus": "ctrl+alt+m"}})[1]
        self.assertEqual((s["pedal"]["plus"], s["pedal"]["minus"]), ("F13", "Ctrl+Alt+M"))
        self.assertBadRequest("/api/settings", {"pedal": {"minus": "F13"}})  # same key twice
        self.assertBadRequest("/api/settings", {"pedal": {"plus": "B"}})
        self.assertBadRequest("/api/settings", {"pedal": {"next": "F14"}})
        self.assertBadRequest("/api/settings", {"pedal": {}})
        self.assertBadRequest("/api/settings", {"pedal": "F13"})
        s = self.srv.post("/api/settings", {"pedal": {"minus": None}})[1]
        self.assertIsNone(s["pedal"]["minus"])
        self.restart()
        self.assertEqual(self.srv.state()["pedal"]["plus"], "F13")

    def test_pedal_press_counts_and_rolls_over(self):
        self.srv.post("/api/box", {"set": 1, "span": 4, "of": 12})
        store = self.srv.store
        self.assertEqual(store.pedal_press("plus")["box"]["packsOpened"], 1)
        self.assertEqual(store.pedal_press("minus")["box"]["packsOpened"], 0)
        self.srv.post("/api/packs", {"set": 96})
        s = store.pedal_press("plus")
        self.assertEqual((s["box"]["number"], s["box"]["to"], s["box"]["packsOpened"]), (5, 8, 0))

    def test_pedal_event_follows_state(self):
        sse = SSEClient(self.srv.port)
        try:
            sse.next_event()  # initial state
            self.srv.store.pedal_press("plus")
            name, data, _ = sse.next_event()
            self.assertEqual(name, "state")
            name, data, _ = sse.next_event()
            self.assertEqual(name, "pedal")
            self.assertEqual(data, {"action": "plus", "delta": 1, "moved": False})
        finally:
            sse.close()

    def test_pause_route(self):
        status, s = self.srv.post("/api/pedal", {"paused": True})
        self.assertEqual(status, 200)
        self.assertIn("pedal", s)
        self.assertBadRequest("/api/pedal", {"paused": "yes"})
        self.assertBadRequest("/api/pedal", {})

    @unittest.skipUnless(os.name == "nt", "global hotkeys are Windows-only")
    def test_windows_listener_registers_and_counts(self):
        store = self.srv.store
        pedal = server.Pedal(store)
        store.pedal = pedal
        # An obscure combination so the test never clashes with a real shortcut.
        store.settings({"pedal": {"plus": "Ctrl+Alt+Shift+F24"}})
        pedal.start()
        try:
            deadline = time.time() + 5
            while pedal.status["plus"] == "off" and time.time() < deadline:
                time.sleep(0.05)
            if pedal.status["plus"] == "busy":
                self.skipTest("Ctrl+Alt+Shift+F24 is taken by another app on this PC")
            self.assertEqual(pedal.status["plus"], "active")
            # Deliver a hotkey message straight to the listener thread (no real keystroke).
            pedal._post(pedal.WM_HOTKEY, pedal.HOTKEY_IDS["plus"])
            deadline = time.time() + 5
            while store.state()["box"]["packsOpened"] < 1 and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(store.state()["box"]["packsOpened"], 1)
            # A bounce inside the debounce window is ignored.
            pedal._post(pedal.WM_HOTKEY, pedal.HOTKEY_IDS["plus"])
            time.sleep(0.1)
            self.assertEqual(store.state()["box"]["packsOpened"], 1)
            # Learn pauses the hotkey; resuming re-registers it.
            pedal.set_paused(True)
            deadline = time.time() + 5
            while pedal.status["plus"] != "paused" and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(pedal.status["plus"], "paused")
            pedal.set_paused(False)
            deadline = time.time() + 5
            while pedal.status["plus"] != "active" and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(pedal.status["plus"], "active")
        finally:
            pedal.stop()
            store.pedal = None


class PersistenceTests(ServerTestCase):
    def test_state_survives_restart(self):
        self.hit("ultra", "Maxx C")
        self.hit("starlight_overframe", "Ash")
        self.srv.post("/api/packs", {"set": 7})
        self.srv.post("/api/next-box", {})
        self.hit("secret")
        self.srv.post("/api/packs", {"delta": 2})
        before = self.srv.state()

        self.assertTrue(self.state_path.exists())
        on_disk = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["log"]), 3)

        self.restart()
        after = self.srv.state()
        self.assertEqual(after, before)
        # New hits after restart keep going in the same box with unique ids.
        s = self.hit("ultra")
        self.assertEqual(s["log"][-1]["box"], 2)
        self.assertEqual(len({e["id"] for e in s["log"]}), 4)

    def test_atomic_write_leaves_no_temp_files(self):
        for _ in range(5):
            self.hit("ultra")
        leftovers = [p.name for p in self.state_path.parent.iterdir() if p.name != "state.json"]
        self.assertEqual(leftovers, [])

    def test_missing_data_dir_is_created(self):
        self.assertFalse(self.state_path.parent.exists())
        self.hit("ultra")
        self.assertTrue(self.state_path.exists())

    def test_corrupt_state_file_is_set_aside(self):
        self.srv.stop()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{ this is not json", encoding="utf-8")
        self.srv = ServerHarness(self.state_path)
        s = self.srv.state()
        self.assertEqual(s["log"], [])
        self.assertEqual(s["box"]["number"], 1)
        backups = list(self.state_path.parent.glob("state.json.corrupt-*"))
        self.assertEqual(len(backups), 1)


# --------------------------------------------------------------------------
# Server-Sent Events
# --------------------------------------------------------------------------


class SSETests(ServerTestCase):
    def open_sse(self):
        c = SSEClient(self.srv.port)
        self.addCleanup(c.close)
        return c

    def test_headers_and_initial_state(self):
        self.hit("ultra", "before connect")
        c = self.open_sse()
        self.assertEqual(c.resp.status, 200)
        self.assertEqual(c.resp.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(c.resp.getheader("Cache-Control"), "no-cache")
        event, data, frame = c.next_event()
        self.assertEqual(event, "state")
        self.assertEqual(frame["data_lines"], 1)  # single-line JSON
        self.assertEqual(data, self.srv.state())

    def test_hit_sends_state_then_hit(self):
        c = self.open_sse()
        self.assertEqual(c.next_event()[0], "state")

        _, posted = self.srv.post("/api/hit", {"rarity": "starlight", "card": "Snake-Eye Ash"})

        event, state, frame = c.next_event()
        self.assertEqual(event, "state")
        self.assertEqual(frame["data_lines"], 1)
        self.assertEqual(state, posted)

        event, hit, frame = c.next_event()
        self.assertEqual(event, "hit")
        self.assertEqual(frame["data_lines"], 1)
        self.assertEqual(set(hit), {"entry", "rarity"})
        self.assertEqual(hit["entry"], posted["log"][-1])
        self.assertEqual(hit["entry"]["card"], "Snake-Eye Ash")
        self.assertEqual(hit["rarity"], {"id": "starlight", "label": "Starlight Rare", "short": "STR", "color": "#7fd4ff"})

    def test_test_hit_sends_testhit_not_hit(self):
        c = self.open_sse()
        self.assertEqual(c.next_event()[0], "state")
        _, posted = self.srv.post("/api/hit", {"rarity": "ultra", "card": "Waboku", "test": True})
        self.assertEqual(c.next_event()[0], "state")
        event, hit, _ = c.next_event()
        self.assertEqual(event, "testHit")  # the stream overlay only listens for "hit"
        self.assertEqual(hit["entry"], posted["testLog"][-1])

    def test_hit_event_uses_custom_label(self):
        self.srv.post("/api/settings", {"labels": {"ultra": "ULTRA!!"}})
        c = self.open_sse()
        c.next_event()
        self.hit("ultra")
        self.assertEqual(c.next_event()[0], "state")
        event, hit, _ = c.next_event()
        self.assertEqual(event, "hit")
        self.assertEqual(hit["rarity"]["label"], "ULTRA!!")

    def test_other_mutations_send_state_only(self):
        c = self.open_sse()
        c.next_event()
        self.hit("ultra")
        c.next_event(), c.next_event()  # state, hit
        for path, body in (("/api/undo", {}), ("/api/packs", {"delta": 1}), ("/api/next-box", {}),
                           ("/api/settings", {"title": "t"}), ("/api/reset", {})):
            _, posted = self.srv.post(path, body)
            event, data, _ = c.next_event()
            self.assertEqual(event, "state", path)
            self.assertEqual(data, posted, path)
        # Prove no stray `hit` event is queued: the next frame after one more hit is its `state`.
        _, posted = self.srv.post("/api/hit", {"rarity": "secret"})
        self.assertEqual(c.next_event()[1], posted)

    def test_rejected_post_sends_nothing(self):
        c = self.open_sse()
        c.next_event()
        self.assertBadRequest("/api/hit", {"rarity": "nope"})
        _, posted = self.srv.post("/api/packs", {"delta": 1})
        event, data, _ = c.next_event()
        self.assertEqual((event, data), ("state", posted))

    def test_multiple_clients_all_receive(self):
        clients = [self.open_sse() for _ in range(3)]
        for c in clients:
            c.next_event()
        _, posted = self.srv.post("/api/hit", {"rarity": "overframe"})
        for c in clients:
            self.assertEqual(c.next_event()[1], posted)
            event, hit, _ = c.next_event()
            self.assertEqual(event, "hit")
            self.assertEqual(hit["entry"]["id"], posted["log"][-1]["id"])

    def test_heartbeat_ping(self):
        old = server.PING_INTERVAL
        server.PING_INTERVAL = 0.3
        try:
            c = self.open_sse()
            c.next_event()
            frame = c.next_frame()
        finally:
            server.PING_INTERVAL = old
        self.assertEqual(server.PING_INTERVAL, 15.0)
        self.assertEqual(frame["comments"], ["ping"])
        self.assertEqual(frame["data_lines"], 0)

    def test_disconnected_client_is_dropped(self):
        c = SSEClient(self.srv.port)
        c.next_event()
        self.assertEqual(self.srv.store.client_count(), 1)
        c.close()
        # The handler notices on its next write.
        deadline = time.time() + 5
        while self.srv.store.client_count() and time.time() < deadline:
            self.srv.post("/api/packs", {"delta": 1})
            time.sleep(0.05)
        self.assertEqual(self.srv.store.client_count(), 0)


# --------------------------------------------------------------------------
# The real command line
# --------------------------------------------------------------------------


class CommandLineTests(unittest.TestCase):
    def test_server_py_runs_and_serves(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = free_port()
            state = Path(tmp) / "state.json"
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "server.py"), "--port", str(port), "--state", str(state)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            try:
                deadline = time.time() + TIMEOUT
                while True:
                    try:
                        conn = http.client.HTTPConnection(HOST, port, timeout=5)
                        conn.request("GET", "/api/state")
                        resp = conn.getresponse()
                        self.assertEqual(resp.status, 200)
                        resp.read()
                        conn.close()
                        break
                    except OSError:
                        if time.time() > deadline or proc.poll() is not None:
                            self.fail("server.py did not start: " + (proc.stdout.read() if proc.poll() is not None else ""))
                        time.sleep(0.1)

                conn = http.client.HTTPConnection(HOST, port, timeout=TIMEOUT)
                conn.request("POST", "/api/hit", body=b'{"rarity":"ultra"}', headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                resp.read()
                conn.close()
                self.assertTrue(state.exists())
            finally:
                if os.name == "nt":
                    import signal
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                try:
                    out, _ = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
            self.assertIn(f"http://127.0.0.1:{port}/control", out)
            self.assertIn(f"http://127.0.0.1:{port}/overlay", out)

    def test_port_in_use_is_a_clean_error(self):
        with socket.socket() as s:
            s.bind((HOST, 0))
            s.listen()
            port = s.getsockname()[1]
            proc = subprocess.run(
                [sys.executable, str(ROOT / "server.py"), "--port", str(port),
                 "--state", os.path.join(tempfile.gettempdir(), "whatnot-unused.json")],
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Could not listen", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


# --------------------------------------------------------------------------
# Static cross-check: the pages talk to the server the way the contract says
# --------------------------------------------------------------------------


class PageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = (ROOT / "web" / "control.html").read_text(encoding="utf-8")
        cls.overlay = (ROOT / "web" / "overlay.html").read_text(encoding="utf-8")

    def test_pages_are_self_contained(self):
        # Offline stream: no external scripts, styles, fonts or images.
        for name, html in (("control", self.control), ("overlay", self.overlay)):
            self.assertIsNone(re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", html), name)
            self.assertNotIn("@import", html, name)
            self.assertIsNone(re.search(r"url\(\s*['\"]?(https?:)?//", html), name)

    def test_control_posts_only_to_contract_routes(self):
        used = set(re.findall(r"""post\(\s*["'](/api/[\w/-]+)["']""", self.control))
        self.assertTrue(used)
        self.assertLessEqual(used, set(server.POST_ROUTES), used - set(server.POST_ROUTES))
        # It drives every mutation the streamer needs.
        self.assertEqual(used, set(server.POST_ROUTES))

    def test_pages_use_state_and_events(self):
        for name, html in (("control", self.control), ("overlay", self.overlay)):
            self.assertRegex(html, r"""fetch\(\s*["']/api/state["']""", name)
            self.assertRegex(html, r"""new EventSource\(\s*["']/api/events["']""", name)
            self.assertRegex(html, r"""addEventListener\(\s*["']state["']""", name)
            self.assertRegex(html, r"""addEventListener\(\s*["']hit["']""", name)

    def test_overlay_reads_every_contract_param(self):
        for param in ("layout", "show", "hide", "scale", "title", "packs", "boxes", "left", "tracker",
                      "caselog", "showcase", "panel", "alerts", "bg"):
            self.assertRegex(self.overlay, r"""(pick|flag|q\.get)\(\s*'%s'""" % param, param)

    def test_control_builds_every_overlay_param(self):
        # packs/boxes are switched live from the panel (state.display), not baked into the URL.
        for param in ("layout", "show", "hide", "scale", "title", "alerts", "bg"):
            self.assertRegex(self.control, r"""p\.set\(\s*"%s"\s*,""" % param, param)
        self.assertIn('"/overlay"', self.control)


if __name__ == "__main__":
    unittest.main()
