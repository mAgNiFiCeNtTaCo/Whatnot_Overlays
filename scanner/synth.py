"""Synthetic camera frames: a card-like rectangle held up and tilting in front of a
cluttered background, with a thumb over one edge. Used by the demo camera and the
tests, so the scanner can be exercised without a real camera."""

import math

import cv2
import numpy as np

from .detect import OUT_H, OUT_W

# Name-bar foil colour per rarity (BGR), so the classes look different later on.
_FOIL = {
    "ultra": (40, 190, 235),       # gold
    "secret": (215, 210, 200),     # silver
    "overframe": (60, 120, 255),
    "starlight": (255, 220, 140),
    "starlight_overframe": (255, 150, 220),
    "none": (70, 70, 70),          # not a hit: plain black name, no foil
}


def make_card(rarity="ultra", phase=0.0, seed=7):
    """A flat 420x612 card face."""
    rng = np.random.default_rng(seed)
    card = np.zeros((OUT_H, OUT_W, 3), np.uint8)
    card[:] = (25, 25, 25)                                   # thin black border
    cv2.rectangle(card, (8, 8), (OUT_W - 9, OUT_H - 9), (40, 105, 190), -1)   # effect-monster frame
    foil = np.array(_FOIL.get(rarity, (200, 200, 200)), np.float32)
    shimmer = 0.75 + 0.25 * math.sin(phase * 2.3)
    cv2.rectangle(card, (30, 26), (OUT_W - 30, 70), tuple(int(v) for v in foil * shimmer), -1)  # name bar
    cv2.putText(card, "SYNTHETIC CARD", (40, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (20, 20, 20), 2, cv2.LINE_AA)
    # art box: blobs on a gradient (overframe art spills over the frame: up and to the sides)
    overframe = "overframe" in rarity
    top = 52 if overframe else 100
    left = 24 if overframe else 45
    art = np.zeros((380 - top + 100, OUT_W - 2 * left, 3), np.uint8)
    gy = np.linspace(0, 1, art.shape[0])[:, None]
    art[..., 0] = (120 + 100 * gy).astype(np.uint8)
    art[..., 1] = (60 + 80 * gy).astype(np.uint8)
    art[..., 2] = 90
    for _ in range(9):
        c = tuple(int(v) for v in rng.integers(0, 255, 3))
        cv2.circle(art, (int(rng.integers(0, art.shape[1])), int(rng.integers(0, art.shape[0]))),
                   int(rng.integers(15, 60)), c, -1)
    card[top:top + art.shape[0], left:left + art.shape[1]] = art
    cv2.rectangle(card, (30, 490), (OUT_W - 30, OUT_H - 30), (190, 220, 235), -1)  # text box
    for i in range(4):
        cv2.line(card, (45, 512 + i * 18), (OUT_W - 60, 512 + i * 18), (90, 90, 90), 2)
    if rarity.startswith("starlight"):  # sparkle over the whole card that moves with the tilt
        sp = np.random.default_rng(int(phase * 10) % 1000).random(card.shape[:2]) > 0.985
        card[sp] = (255, 255, 255)
    return card


def _background(w, h, seed=3):
    rng = np.random.default_rng(seed)
    bg = np.zeros((h, w, 3), np.uint8)
    gx = np.linspace(0, 1, w)[None, :]
    bg[..., 0] = (40 + 30 * gx).astype(np.uint8)
    bg[..., 1] = (45 + 25 * gx).astype(np.uint8)
    bg[..., 2] = (55 + 20 * gx).astype(np.uint8)
    for _ in range(14):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        c = tuple(int(v) for v in rng.integers(30, 160, 3))
        cv2.rectangle(bg, (x, y), (x + int(rng.integers(30, 200)), y + int(rng.integers(30, 160))), c, -1)
    noise = rng.normal(0, 4, bg.shape)
    return np.clip(bg + noise, 0, 255).astype(np.uint8)


_BG_CACHE = {}


def make_frame(t=0.0, w=1280, h=720, rarity="ultra", present=True, thumb=True, seed=7):
    """A camera frame at time t (seconds). Returns (frame, true_corners or None)."""
    key = (w, h)
    if key not in _BG_CACHE:
        _BG_CACHE[key] = _background(w, h)
    frame = _BG_CACHE[key].copy()
    if not present:
        return frame, None

    card = make_card(rarity, phase=t, seed=seed)
    ch = h * 0.62
    cw = ch * OUT_W / OUT_H
    cx = w / 2 + 18 * math.sin(t * 0.9)
    cy = h / 2 + 10 * math.sin(t * 1.3)
    ang = math.radians(5 * math.sin(t * 0.7))
    tilt = 0.06 * math.sin(t * 1.1)  # perspective: one side a little taller than the other
    corners = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        px = sx * cw / 2
        py = sy * ch / 2 * (1 + tilt * sx)
        corners.append((cx + px * math.cos(ang) - py * math.sin(ang), cy + px * math.sin(ang) + py * math.cos(ang)))
    dst = np.array(corners, np.float32)
    src = np.array([[0, 0], [OUT_W - 1, 0], [OUT_W - 1, OUT_H - 1], [0, OUT_H - 1]], np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(card, m, (w, h))
    mask = cv2.warpPerspective(np.full((OUT_H, OUT_W), 255, np.uint8), m, (w, h))
    frame[mask > 0] = warped[mask > 0]
    if thumb:  # a thumb over the bottom-left edge, like holding the card up
        bl = dst[3]
        cv2.ellipse(frame, (int(bl[0] + cw * 0.22), int(bl[1] - 8)), (34, 58), 20, 0, 360, (120, 160, 215), -1)
    return frame, dst
