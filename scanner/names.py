"""Card names (phase 2): read the name bar with Windows' built-in text recognition (OCR) and
match what it read to the Yu-Gi-Oh card list from YGOPRODeck, downloaded once and kept in
data/cards.json. Each camera read has ~10 photos; every photo's best match is a vote, so a
name a few photos agree on wins even when the foil hides the letters on the others.

The card list also says which rarities each card was printed in, so a read can be checked:
a card only ever printed as a Super Rare isn't an Ultra Rare.

Needs the Windows OCR bindings:  python -m pip install --user winrt-runtime winrt-Windows.Media.Ocr
winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Foundation
winrt-Windows.Foundation.Collections winrt-Windows.Globalization
"""

import difflib
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

CARD_LIST_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
CARD_LIST_MAX_AGE = 7 * 24 * 3600   # refreshed in the background when older than this
NAME_REGION = (0.0, 0.135, 0.0, 0.875)  # y0, y1, x0, x1 of the straightened card: roomy, so a slightly
                                         # off outline still has the whole name in it
MATCH_MIN = 0.6      # a photo's text must be at least this like a card name to vote for it
SURE_VOTES = 2.0     # summed match of the photos voting for the winner...
SURE_SHARE = 0.7     # ...and its share of all votes, for the name to be used without asking
IN_RARITY_BONUS = 0.08   # a card printed in the rarity the scanner read wins near-ties
IN_SET_BONUS = 0.10      # so does a card from the set being opened
PARTIAL_WEIGHT = 0.9     # text matching only the start/end of a longer name counts this much

# Scanner rarity -> how the card list names those printings ("Overframe" isn't in it yet).
LIST_RARITIES = {
    "ultra": {"Ultra Rare", "Ultra Rare (Pharaoh's Rare)"},
    "secret": {"Secret Rare", "Quarter Century Secret Rare", "Platinum Secret Rare", "PLatinum Secret Rare",
               "Prismatic Secret Rare", "Gold Secret Rare", "Extra Secret Rare"},
    "starlight": {"Starlight Rare"},
}
HIT_LIST_RARITIES = set().union(*LIST_RARITIES.values())
# For choosing between look-alike names only: the card list records Starlight Overframes as Starlight Rares.
PREFER_RARITIES = {**LIST_RARITIES, "starlight_overframe": LIST_RARITIES["starlight"]}


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", text.upper())).strip()


def _trigrams(text):
    return {text[i:i + 3] for i in range(len(text) - 2)}


class CardList:
    """Names, set codes and printed rarities of every card, from data/cards.json."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.cards, self.sets, self.updated, self.error = [], {}, None, None
        self.downloading = False
        self._load()

    @property
    def ready(self):
        return bool(self.cards)

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cards = []
        for c in raw.get("cards", []):
            if isinstance(c, dict) and isinstance(c.get("n"), str):
                sets = [s for s in c.get("s", []) if isinstance(s, list) and len(s) >= 2]
                cards.append({"name": c["n"], "norm": _norm(c["n"]), "sets": sets})
        for c in cards:
            c["tri"] = _trigrams(c["norm"])
            c["rarities"] = {s[1] for s in c["sets"]}
            c["prefixes"] = {s[0].split("-")[0] for s in c["sets"]}
        with self.lock:
            self.cards = cards
            self.by_name = {c["name"]: c for c in cards}
            self.sets = raw.get("sets", {}) if isinstance(raw.get("sets"), dict) else {}
            self.updated = raw.get("updated")

    def stale(self):
        return not self.ready or not self.updated or time.time() - self.updated > CARD_LIST_MAX_AGE

    def download(self):
        """Fetch the full card list from YGOPRODeck and keep the names, set codes and rarities."""
        self.downloading, self.error = True, None
        try:
            req = urllib.request.Request(CARD_LIST_URL, headers={"User-Agent": "WhatnotHitCounter/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))["data"]
            cards, sets = [], {}
            for c in data:
                printed = sorted({(s["set_code"], s["set_rarity"]) for s in c.get("card_sets") or []
                                  if s.get("set_code") and s.get("set_rarity")})
                cards.append({"n": c["name"], "s": [list(p) for p in printed]})
                for s in c.get("card_sets") or []:
                    if s.get("set_code") and s.get("set_name"):
                        sets.setdefault(s["set_code"].split("-")[0], s["set_name"])
            out = {"source": CARD_LIST_URL, "updated": time.time(), "sets": sets, "cards": cards}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._load()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.error = f"couldn't download the card list: {exc}"
        finally:
            self.downloading = False

    def info(self, name):
        c = self.by_name.get(name) if self.ready else None
        return c

    def match(self, text, rarity=None, set_prefix=None):
        """(card name, score) for one line of OCR text, or (None, 0)."""
        t = _norm(text)
        if len(t) < 4 or not self.ready:
            return None, 0.0
        tt = _trigrams(t)
        if not tt:
            return None, 0.0
        wanted = PREFER_RARITIES.get(rarity)
        scored = []
        for c in self.cards:
            shared = len(tt & c["tri"])
            if shared:
                scored.append((shared / len(tt | c["tri"]), c))
        scored.sort(key=lambda sc: -sc[0])
        pool = [c for _, c in scored[:20]]
        if wanted or set_prefix:  # make sure the likeliest cards of this rarity / set are compared too
            pool += [c for _, c in scored if (wanted and c["rarities"] & wanted)
                     or (set_prefix and set_prefix in c["prefixes"])][:10]
        best, best_score = None, 0.0
        for c in pool:
            s = difflib.SequenceMatcher(None, t, c["norm"]).ratio()
            if len(c["norm"]) > len(t) + 3:
                # Foil often hides the end (or start) of a long name: compare with that much of it.
                n = len(t) + 2
                part = max(difflib.SequenceMatcher(None, t, c["norm"][:n]).ratio(),
                           difflib.SequenceMatcher(None, t, c["norm"][-n:]).ratio())
                s = max(s, PARTIAL_WEIGHT * part)
            if wanted and c["rarities"] & wanted:
                s += IN_RARITY_BONUS
            if set_prefix and set_prefix in c["prefixes"]:
                s += IN_SET_BONUS
            if s > best_score:
                best, best_score = c["name"], s
        return best, best_score

    def printed_hits(self, name):
        """Which of the scanner's rarities the card list says this card was printed in, or None
        when the card (or its printings) aren't in the list."""
        c = self.info(name)
        if c is None or not c["rarities"]:
            return None
        return sorted(r for r, names in LIST_RARITIES.items() if c["rarities"] & names)


# ---- Windows OCR ------------------------------------------------------------

try:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    _ENGINE = OcrEngine.try_create_from_language(Language("en-US"))
    OCR_ERROR = None if _ENGINE is not None else "Windows has no English text recognition installed"
except Exception as exc:  # not Windows, or the bindings aren't installed
    _ENGINE, OCR_ERROR = None, f"Windows text recognition isn't available ({exc.__class__.__name__})"

OCR_TIMEOUT = 5.0  # seconds for one image; past it that image just reads nothing
_OCR_LOCK = threading.Lock()


def ocr_available():
    return _ENGINE is not None


def _bitmap(img):
    bgra = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA if img.ndim == 2 else cv2.COLOR_BGR2BGRA)
    writer = DataWriter()
    writer.write_bytes(bgra.tobytes())
    return SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                  img.shape[1], img.shape[0])


def name_images(card):
    """The name bar of a straightened card, in the versions OCR reads best: as is, with the
    foil glitter smoothed away (median filter), as smoothed brightness, and as colour contrast
    (how far each pixel's strongest colour channel is from the bar's): rainbow foil letters can
    be as bright as the bar behind them (blue-purple on green), so only their colour stands out."""
    card = cv2.resize(card, (420, 612), interpolation=cv2.INTER_AREA)
    y0, y1, x0, x1 = NAME_REGION
    bar = card[int(y0 * 612):int(y1 * 612), int(x0 * 420):int(x1 * 420)]
    big = cv2.resize(bar, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    smooth = cv2.medianBlur(cv2.resize(cv2.medianBlur(bar, 3), None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC), 5)
    gray = cv2.morphologyEx(cv2.medianBlur(cv2.cvtColor(big, cv2.COLOR_BGR2GRAY), 5), cv2.MORPH_OPEN,
                            np.ones((3, 3), np.uint8))
    bgr = smooth.astype(np.float32)
    away = np.abs(bgr - np.median(bgr.reshape(-1, 3), axis=0)).max(axis=2)
    colour = 255 - cv2.normalize(cv2.medianBlur(away, 5), None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return VARIANTS([smooth, gray, big, colour])


def VARIANTS(images):  # (kept as a function so evaluations can pick a subset)
    return images


def _recognize(img):
    """Text lines in one image. Waits on Windows' own completion signal, not an asyncio loop:
    a Python event loop wakes up through a loopback socket, which some PCs delay or block."""
    op = _ENGINE.recognize_async(_bitmap(img))
    done = threading.Event()
    op.completed = lambda _op, _status: done.set()
    if not done.wait(OCR_TIMEOUT):
        op.cancel()
        return None
    return [line.text for line in op.get_results().lines]


def ocr_lines(images):
    """Text lines OCR finds in each image: [[line, ...], ...]."""
    if _ENGINE is None:
        return [[] for _ in images]
    out = []
    with _OCR_LOCK:
        for img in images:
            lines = _recognize(img)
            if lines is None:  # timed out: Windows' OCR is stuck, don't wait on the rest of this read
                return out + [[] for _ in range(len(images) - len(out))]
            out.append(lines)
    return out


# Frame text the name crop can catch (attribute icon, "[SPELL CARD]"): never part of a name.
FRAME_WORDS = {"SPELL", "TRAP", "CARD", "LIGHT", "DARK", "EARTH", "WATER", "FIRE", "WIND", "DIVINE"}


def _seen_word(word, seen):
    return word in seen or any(difflib.SequenceMatcher(None, word, s).ratio() >= 0.8 for s in seen)


def _longer_name(name, seen, card_list):
    """The longest card name containing `name` whose other words were all seen in the photos, or None."""
    info = card_list.info(name)
    if info is None:
        return None
    base = info["norm"]
    own = set(base.split())
    best, best_len = None, 0
    for c in card_list.cards:
        n = c["norm"]
        if len(n) <= len(base) or f" {base} " not in f" {n} ":
            continue
        extra = [w for w in f" {n} ".replace(f" {base} ", " ", 1).split() if len(w) >= 3 and w not in own]
        if extra and all(_seen_word(w, seen) for w in extra) and len(n) > best_len:
            best, best_len = c["name"], len(n)
    return best


def read_name(cards, card_list, rarity=None, set_prefix=None):
    """Name of the card in a capture's photos.
    {"name", "sure", "votes", "share", "candidates": [[name, votes], ...], "printed": [...] or None}
    or None when nothing in the photos looked like a card name."""
    if not cards or not card_list.ready or _ENGINE is None:
        return None
    votes, cache, seen = {}, {}, set()
    per_card = [name_images(card) for card in cards]
    found = iter(ocr_lines([img for imgs in per_card for img in imgs]))  # one batch for the whole read
    for imgs in per_card:
        best, best_score = None, 0.0
        for lines in (next(found) for _ in imgs):
            for line in lines:
                key = _norm(line)
                seen.update(w for w in key.split() if len(w) >= 3 and w not in FRAME_WORDS)
                if key not in cache:
                    cache[key] = card_list.match(line, rarity, set_prefix)
                name, score = cache[key]
                if name and score > best_score:
                    best, best_score = name, score
        if best and best_score >= MATCH_MIN:
            votes[best] = votes.get(best, 0.0) + min(best_score, 1.0)
    if not votes:
        return None
    # A short name can be the tail or head of a longer one (Polymerization / Heavy Polymerization)
    # when foil hides a word in most photos: if any photo shows the missing words, it's the longer one.
    for name in sorted(votes, key=lambda n: -votes[n])[:2]:
        longer = _longer_name(name, seen, card_list)
        if longer:
            votes[longer] = votes.get(longer, 0.0) + votes.pop(name)
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    name, top = ranked[0]
    share = top / sum(votes.values())
    return {"name": name, "sure": top >= SURE_VOTES and share >= SURE_SHARE, "votes": round(top, 2),
            "share": round(share, 2), "candidates": [[n, round(v, 2)] for n, v in ranked[:4]],
            "printed": card_list.printed_hits(name)}
