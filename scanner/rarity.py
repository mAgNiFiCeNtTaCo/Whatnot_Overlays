"""Rarity recognition (phase 3, "mix" approach).

Each straightened card photo is measured in fixed regions that carry the rarity, not the
card's identity:
  * name text  - colour and shine of the foil letters against the name bar
                 (gold = Ultra, silver = Secret, rainbow = Starlight)
  * art box    - foil texture: specular sparkle, diagonal-line pattern, colour spread
  * text box   - sparkle where a normal card has plain paper (Starlight is foil all over)
  * art border - how straight/strong the art box's top border is (Overframe art crosses it)
A capture (several photos while tilting) is summarised by the average of those numbers
plus how much the shine changes with the tilt. A new capture is compared with your
labeled captures (nearest neighbours, weighting the measurements that actually separate
the rarities you've shown it).
"""

import json
from pathlib import Path

import cv2
import numpy as np

FEATURE_VERSION = 4  # 2: + colour shift / twinkle across the tilt; 3: + overframe breakout markers; 4: + name rainbow
REGIONS = {  # (x0, y0, x1, y1) as a share of the straightened card
    "name": (0.045, 0.030, 0.840, 0.100),
    "art": (0.120, 0.180, 0.880, 0.665),
    "text": (0.065, 0.700, 0.935, 0.925),
    "art_top": (0.140, 0.120, 0.860, 0.215),
    "frame_left": (0.030, 0.200, 0.100, 0.640),   # plain printed frame on most cards, glitter on Starlights
    "frame_right": (0.900, 0.200, 0.970, 0.640),
    "frame_top": (0.140, 0.105, 0.860, 0.165),     # between the name and the art box
}
# Art-box edges: (row/col, nominal position, span along the edge). A normal card has a
# straight border line there; Overframe art runs across it.
ART_BORDERS = {
    "top": ("row", 0.180, (0.15, 0.85)),
    "left": ("col", 0.120, (0.22, 0.62)),
    "right": ("col", 0.880, (0.22, 0.62)),
}

# (feature, plain-language description); order = the vector layout
FRAME_FEATURES = [
    ("name_L", "name text brightness"),
    ("name_L_std", "name text shine variation"),
    ("name_a", "name text red-green tint"),
    ("name_b", "name text gold-blue tint"),
    ("name_chroma", "name text colourfulness"),
    ("name_hue_cos", "name text hue (x)"),
    ("name_hue_sin", "name text hue (y)"),
    ("name_hue_focus", "name text single colour vs rainbow"),
    ("name_contrast", "name text vs name bar brightness"),
    ("name_bright", "share of very bright name text"),
    ("name_rainbow", "name letters in more than one colour (foil sheen)"),
    ("name_sheen", "colour variation across the name letters"),
    ("art_specular", "art sparkle (white highlights)"),
    ("art_sat_std", "art colour-strength spread"),
    ("art_detail", "art fine texture"),
    ("art_diagonal", "art diagonal-line foil pattern"),
    ("art_hue_spread", "art rainbow colour spread"),
    ("text_colour", "colour in the text box"),
    ("text_specular", "sparkle in the text box"),
    ("text_L_std", "text box brightness variation"),
    ("border_peak", "strength of the art box's top border"),
    ("art_left", "art breaking out into the left frame"),
    ("art_right", "art breaking out into the right frame"),
    ("art_top", "art breaking out above the art box"),
    ("border_top", "art box top border line"),
    ("border_left", "art box left border line"),
    ("border_right", "art box right border line"),
]
SHINE = ["name_L", "art_specular", "text_specular", "text_colour"]  # their change across the tilt is measured too
DESCRIPTIONS = dict(FRAME_FEATURES) | {s + "_tilt": f"change in {dict(FRAME_FEATURES)[s]} as the card tilts" for s in SHINE}


def _crop(img, region):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = REGIONS[region]
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _lab(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    lab[:, 0] *= 100.0 / 255.0
    lab[:, 1:] -= 128.0
    return lab


def frame_features(card):
    """Measurements of one straightened card photo (BGR, any size with card proportions)."""
    card = cv2.resize(card, (420, 612), interpolation=cv2.INTER_AREA)
    f = {}

    # name text: the pixels furthest in colour from the name bar's typical colour
    lab = _lab(_crop(card, "name"))
    med = np.median(lab, axis=0)
    dist = np.linalg.norm(lab - med, axis=1)
    text = lab[dist >= np.percentile(dist, 75)]
    L, a, b = text[:, 0], text[:, 1], text[:, 2]
    chroma = np.hypot(a, b)
    hue = np.arctan2(b, a)
    wsum = float(chroma.sum()) + 1e-6
    hc, hs = float((chroma * np.cos(hue)).sum() / wsum), float((chroma * np.sin(hue)).sum() / wsum)
    f.update(name_L=float(L.mean()), name_L_std=float(L.std()), name_a=float(a.mean()), name_b=float(b.mean()),
             name_chroma=float(chroma.mean()), name_hue_cos=hc, name_hue_sin=hs,
             name_hue_focus=float(np.hypot(hc, hs)), name_contrast=float(L.mean() - med[0]),
             name_bright=float((L > 80).mean()))
    # Foil letters catch the light in several colours at once (a Secret's silver runs through
    # the rainbow); printed ink is one flat colour, even flat silver (Rare) or white (Spells).
    # Measured on the letters' cores, away from the edges blended with the name bar.
    core = text[L >= np.median(L)] if L.mean() > med[0] else text[L <= np.median(L)]
    ab = core[:, 1:]
    ch, hue_c = np.hypot(ab[:, 0], ab[:, 1]), np.arctan2(ab[:, 1], ab[:, 0])
    coloured = ch > 6
    f["name_rainbow"] = (1.0 - float(np.hypot(np.cos(hue_c[coloured]).mean(), np.sin(hue_c[coloured]).mean()))
                         if coloured.sum() > 20 else 0.0)
    f["name_sheen"] = float(np.linalg.norm(ab - ab.mean(axis=0), axis=1).mean())

    # art foil texture
    art = _crop(card, "art")
    hsv = cv2.cvtColor(art, cv2.COLOR_BGR2HSV)
    S, V = hsv[..., 1].astype(np.float32), hsv[..., 2].astype(np.float32)
    gray = cv2.cvtColor(art, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    ang = (np.degrees(np.arctan2(gy, gx)) + 180.0) % 180.0
    diag = ((np.abs(ang - 45) < 15) | (np.abs(ang - 135) < 15))
    sat = S > 80
    if sat.any():
        h = hsv[..., 0][sat].astype(np.float32) * (np.pi / 90.0)  # OpenCV hue 0-180 -> radians
        spread = 1.0 - float(np.hypot(np.cos(h).mean(), np.sin(h).mean()))
    else:
        spread = 0.0
    f.update(art_specular=float(((V > 235) & (S < 60)).mean()), art_sat_std=float(S.std() / 255.0),
             art_detail=float(cv2.Laplacian(gray, cv2.CV_32F).var() / (gray.var() + 1.0)),
             art_diagonal=float(mag[diag].sum() / (mag.sum() + 1e-6)), art_hue_spread=spread)

    # text box: plain paper on most cards, foil on Starlights
    tb = _crop(card, "text")
    tlab = _lab(tb)
    thsv = cv2.cvtColor(tb, cv2.COLOR_BGR2HSV)
    f.update(text_colour=float((np.hypot(tlab[:, 1], tlab[:, 2]) > 25).mean()),
             text_specular=float(((thsv[..., 2] > 240) & (thsv[..., 1] < 40)).mean()),
             text_L_std=float(tlab[:, 0].std()))

    # art box top border: a strong straight line on normal cards, broken on Overframes
    top = cv2.cvtColor(_crop(card, "art_top"), cv2.COLOR_BGR2GRAY).astype(np.float32)
    rows = np.abs(cv2.Sobel(top, cv2.CV_32F, 0, 1, ksize=3)).mean(axis=1)
    f["border_peak"] = float(rows.max() / (np.median(rows) + 1.0))

    # Overframe breakout: art spilling into the frame, and the art box's border lines missing
    for side, region in (("left", "frame_left"), ("right", "frame_right"), ("top", "frame_top")):
        f["art_" + side] = _busyness(_crop(card, region))
    gray_card = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY).astype(np.float32)
    for side, (axis, pos, span) in ART_BORDERS.items():
        f["border_" + side] = _border_line(gray_card, axis, pos, span)
    return np.array([f[name] for name, _ in FRAME_FEATURES], np.float32)


def _busyness(strip):
    """Coarse colour/brightness variation once fine detail is blurred away: a printed frame
    (even a glittery Starlight one) is nearly flat; Overframe art spilling in is not."""
    lab = cv2.cvtColor(strip, cv2.COLOR_BGR2LAB).astype(np.float32)
    blur = cv2.GaussianBlur(lab, (0, 0), 4.0)
    return float(np.sqrt((blur.reshape(-1, 3).std(axis=0) ** 2).sum()))


def _border_line(gray, axis, pos, span):
    """How strongly a straight line stands out near a nominal art-box edge (peak / median)."""
    h, w = gray.shape
    if axis == "row":
        band = gray[int((pos - 0.035) * h):int((pos + 0.035) * h), int(span[0] * w):int(span[1] * w)]
        profile = np.abs(cv2.Sobel(band, cv2.CV_32F, 0, 1, ksize=3)).mean(axis=1)
    else:
        band = gray[int(span[0] * h):int(span[1] * h), int((pos - 0.035) * w):int((pos + 0.035) * w)]
        profile = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3)).mean(axis=0)
    return float(profile.max() / (np.median(profile) + 1.0))


def _name_text_tint(card):
    """Mean a, b (colour tint) of the name's foil letters."""
    lab = _lab(_crop(card, "name"))
    med = np.median(lab, axis=0)
    dist = np.linalg.norm(lab - med, axis=1)
    text = lab[dist >= np.percentile(dist, 75)]
    return text[:, 1].mean(), text[:, 2].mean()


def tilt_features(cards):
    """How colour and sparkle change as the card tilts. The photos are straightened and lined
    up, so a printed area stays put while foil shifts: a Starlight's name and frame run
    through the rainbow and its frame glitter twinkles; an Ultra's name stays gold."""
    cards = [cv2.resize(c, (420, 612), interpolation=cv2.INTER_AREA) for c in cards]
    tint = np.array([_name_text_tint(c) for c in cards], np.float32)
    hue = np.arctan2(tint[:, 1], tint[:, 0])
    focus = float(np.hypot(np.cos(hue).mean(), np.sin(hue).mean()))
    name_hue = float(np.sqrt(-2.0 * np.log(max(focus, 1e-6))))   # circular spread of the name's hue
    name_tint = float(np.linalg.norm(tint - tint.mean(axis=0), axis=1).mean())
    frame_ab, twinkle = [], []
    for region in ("frame_left", "frame_right"):
        ab, highs = [], []
        for c in cards:
            lab = cv2.cvtColor(_crop(c, region), cv2.COLOR_BGR2LAB).astype(np.float32)
            ab.append(lab[..., 1:].reshape(-1, 2).mean(axis=0))
            L = lab[..., 0]
            highs.append((L - cv2.GaussianBlur(L, (0, 0), 2.0)) / (L.mean() + 10.0))  # fine glitter detail
        ab = np.array(ab)
        frame_ab.append(float(np.linalg.norm(ab - ab.mean(axis=0), axis=1).mean()))
        twinkle.append(float(np.stack(highs).std(axis=0).mean()))
    return np.array([name_hue, name_tint, float(np.mean(frame_ab)), float(np.mean(twinkle))], np.float32)


TILT_FEATURES = [
    ("name_hue_tilt", "name colour shifting through the rainbow as it tilts"),
    ("name_tint_tilt", "name tint changing as it tilts"),
    ("frame_colour_tilt", "frame colour changing as it tilts"),
    ("frame_twinkle", "frame glitter twinkling as it tilts"),
]
DESCRIPTIONS.update(dict(TILT_FEATURES))

NOT_HIT = "none"
PLAIN_RARITIES = ("ultra", "secret", NOT_HIT)  # neither breaks the box nor has Starlight shine
HITS = {"ultra", "secret", "overframe", "starlight", "starlight_overframe"}
NAME_FEATURES = ["name_L", "name_L_std", "name_a", "name_b", "name_chroma", "name_hue_cos", "name_hue_sin",
                 "name_hue_focus", "name_contrast", "name_bright", "name_rainbow", "name_sheen",
                 "name_L_tilt", "name_hue_tilt", "name_tint_tilt"]
# What makes a name shine: several colours at once (silver foil, rainbow), gold, and colour shifting with the tilt
NAME_SHINE = ["name_rainbow", "name_sheen", "name_b", "name_tint_tilt", "name_hue_tilt"]
# marker -> (rarities that have it, the measurements its detector may use)
MARKERS = {
    "name_shine": (HITS, NAME_SHINE),  # foil name = a hit; plain ink name = not a hit
    "overframe": ({"overframe", "starlight_overframe"},
                  ["art_left", "art_right", "art_top", "border_top", "border_left", "border_right", "border_peak"]),
    "starlight": ({"starlight", "starlight_overframe"},
                  ["name_hue_tilt", "name_tint_tilt", "frame_colour_tilt", "frame_twinkle", "name_hue_focus",
                   "name_chroma", "name_a", "name_b", "name_hue_cos", "name_hue_sin", "name_L",
                   "text_colour", "text_specular", "text_L_std", "text_colour_tilt", "text_specular_tilt"]),
}


def capture_features(cards):
    """One vector for a capture: average of the photos' measurements + change across the tilt."""
    frames = np.stack([frame_features(c) for c in cards])
    names = [n for n, _ in FRAME_FEATURES]
    tilt = [float(frames[:, names.index(s)].std()) for s in SHINE]
    return np.concatenate([frames.mean(axis=0), np.array(tilt, np.float32), tilt_features(cards)])


ALL_FEATURES = [n for n, _ in FRAME_FEATURES] + [s + "_tilt" for s in SHINE] + [n for n, _ in TILT_FEATURES]


def describe(feature):
    return DESCRIPTIONS.get(feature, feature)


# ---- the model --------------------------------------------------------------

class RarityModel:
    """Blend of two comparisons over standardised measurements, probabilities averaged:
      * multinomial logistic regression: each rarity gets its own weighting, so a signal that
        only separates Starlight from Ultra (frame twinkle, the name's colour shift across the
        tilt) isn't diluted by the other rarities. Strongest with plenty of captures.
      * nearest neighbours (measurements weighted by how well they separate the rarities):
        steadier while a rarity has only a few captures.
    Held-out on 121 real captures: kNN 85%, logistic 88%, blend 88% (Starlight 70% -> 80%,
    Ultra 77% -> 83%); on a small 5-per-rarity set the blend keeps 88% where logistic drops to 80%."""

    K = 5
    MARKER_MIN = 3  # captures needed on each side before a marker detector is trained

    L2 = 1.0
    ITERATIONS = 500
    MIN_PER_CLASS = 3

    def __init__(self):
        self.y = None
        self.classes = []

    def fit(self, X, y):
        X = np.asarray(X, np.float64)
        y = np.asarray(y)
        self.y = y
        self.classes = sorted(set(y.tolist()))
        self.mu, self.sd = X.mean(axis=0), X.std(axis=0) + 1e-6
        Z = (X - self.mu) / self.sd
        k, (n, d) = len(self.classes), Z.shape
        fisher = np.sqrt(fisher_scores(Z, y)) if k > 1 else np.ones(d)
        self.knn_w = fisher / (fisher.max() + 1e-9)
        self.knn_Z = Z * self.knn_w
        self.W, self.b = np.zeros((d, k)), np.zeros(k)
        self.markers_fitted = {}
        if k < 2:
            return self
        self.W, self.b = _fit_softmax(Z, np.array([self.classes.index(c) for c in y]), k, self.L2, self.ITERATIONS)
        # Marker detectors, each on its own kind of measurement only, so they learn the
        # marker itself (art breaking the box / Starlight shine) rather than the card.
        for marker, (positives, features) in MARKERS.items():
            target = np.isin(y, list(positives)).astype(int)
            if target.sum() < self.MARKER_MIN or (1 - target).sum() < self.MARKER_MIN:
                continue
            if marker == "overframe":
                # One number: how far the most unusual side of the art box is from normal cards
                # (art in the frame strip + weak border line). "Breaks out on ANY side", so it
                # can't memorise one Overframe card's layout; only the cut-off is learned.
                self.breakout_ref = _breakout_reference(X[target == 0])
                score = _breakout_scores(X, self.breakout_ref).max(axis=1)[:, None]
                W, b = _fit_softmax(score, target, 2, 0.1, self.ITERATIONS)
                self.markers_fitted[marker] = (None, W, b, int(target.sum()))
            else:
                cols = [ALL_FEATURES.index(f) for f in features]
                # each side counts equally, however many captures it has (a few non-hits vs many hits)
                weights = np.where(target == 1, 0.5 / target.mean(), 0.5 / (1 - target.mean()))
                W, b = _fit_softmax(Z[:, cols], target, 2, self.L2, self.ITERATIONS, weights)
                self.markers_fitted[marker] = (cols, W, b, int(target.sum()))
        return self

    @property
    def ready(self):
        if self.y is None or len(self.classes) < 2:
            return False
        counts = {c: int((self.y == c).sum()) for c in self.classes}
        return sum(n >= self.MIN_PER_CLASS for n in counts.values()) >= 2

    def _blend(self, z):
        logistic = _softmax((z @ self.W + self.b)[None, :])[0]
        d = np.linalg.norm(self.knn_Z - z * self.knn_w, axis=1)
        votes = np.zeros(len(self.classes))
        for i in np.argsort(d)[:min(self.K, len(d))]:
            votes[self.classes.index(self.y[i])] += 1.0 / (float(d[i]) + 0.5)
        return {c: float(v) for c, v in zip(self.classes, (logistic + votes / votes.sum()) / 2)}

    def markers(self, x):
        """{"overframe": P(art breaks the box), "starlight": P(Starlight shine)} for the
        markers that have been trained."""
        x = np.asarray(x, np.float64)
        z = (x - self.mu) / self.sd
        out = {}
        for marker, (cols, W, b, _) in self.markers_fitted.items():
            inputs = _breakout_scores(x[None, :], self.breakout_ref).max(axis=1) if cols is None else z[cols]
            out[marker] = round(float(_softmax((inputs @ W + b)[None, :])[0][1]), 3)
        return out

    def breakout_sides(self, x, level=1.5):
        """Sides of the art box the art breaks out of (for explaining a guess)."""
        if "overframe" not in self.markers_fitted:
            return []
        scores = _breakout_scores(np.asarray(x, np.float64)[None, :], self.breakout_ref)[0]
        return [side for (side, _, _), v in zip(BREAKOUT_SIDES, scores) if v > level]

    def predict(self, x):
        """{rarity: probability}, best first (plain str/float so guesses can be saved as JSON).

        With both markers trained, the overall comparison is combined with the markers
        (product of experts): breakout + shine -> Starlight Overframe, breakout alone ->
        Overframe, shine alone -> Starlight, neither -> Ultra/Secret (split by the overall
        comparison). A rarity with no captures yet (Starlight Overframe) can still be
        guessed from its markers."""
        z = (np.asarray(x, np.float64) - self.mu) / self.sd
        mc = self._blend(z)
        m = self.markers(x)
        if "overframe" in m and "starlight" in m:
            p_of, p_sh = m["overframe"], m["starlight"]
            plain = (1 - p_of) * (1 - p_sh)
            plain_split = {c: mc.get(c, 0.0) for c in PLAIN_RARITIES}
            total = sum(plain_split.values()) or 1.0
            factor = {"starlight_overframe": p_of * p_sh, "overframe": p_of * (1 - p_sh),
                      "starlight": (1 - p_of) * p_sh}
            factor.update({c: plain * v / total for c, v in plain_split.items()})
            if "starlight_overframe" not in mc:
                # No captures yet: rank it level with its stronger parent (Overframe or
                # Starlight), so the markers decide between them.
                mc["starlight_overframe"] = max(mc.get("overframe", 0.0), mc.get("starlight", 0.0))
            names = sorted(set(mc) | {c for c, v in factor.items() if c in mc})
            # While Starlight Overframe has no captures, the overall comparison has never seen one
            # and confidently calls it its nearest known rarity, so soften its vote and let the
            # markers lead (held-out on real cards with SOF unseen: 3/7 -> 5/7 named correctly).
            power = 1.0 if "starlight_overframe" in self.classes else 0.4
            joint = {c: (mc.get(c, 0.0) + 0.02) ** power * (factor.get(c, 0.0) + 0.02) for c in names}
            total = sum(joint.values())
            p = {c: v / total for c, v in joint.items()}
        else:
            p = mc
        if "name_shine" in m and NOT_HIT in p:
            # Every hit has a foil name (gold, silver foil, rainbow); a non-hit's name is plain
            # ink, even when its art is foil (Super Rare) or its name is flat silver (Rare).
            p_n = m["name_shine"]
            joint = {c: (v + 0.02) * ((1 - p_n) if c == NOT_HIT else p_n) for c, v in p.items()}
            total = sum(joint.values())
            p = {c: v / total for c, v in joint.items()}
        probs = {str(c): round(float(v), 3) for c, v in p.items()}
        return dict(sorted(probs.items(), key=lambda kv: -kv[1]))

    def marker_info(self):
        return {marker: {"trained": marker in self.markers_fitted,
                         "positives": self.markers_fitted[marker][3] if marker in self.markers_fitted else 0}
                for marker in MARKERS}

    def top_signals(self, n=5):
        weight = np.linalg.norm(self.W, axis=1)
        weight = weight / (weight.max() + 1e-9)
        order = np.argsort(-weight)[:n]
        return [{"feature": ALL_FEATURES[i], "label": describe(ALL_FEATURES[i]),
                 "weight": round(float(weight[i]), 2)} for i in order]


BREAKOUT_SIDES = [("left", "art_left", "border_left"), ("right", "art_right", "border_right"),
                  ("top", "art_top", "border_top")]


def _breakout_reference(X_normal):
    """Typical art-in-strip and border-line readings on cards without Overframe art."""
    ref = {}
    for _, art, border in BREAKOUT_SIDES:
        a = X_normal[:, ALL_FEATURES.index(art)]
        lb = np.log(np.maximum(X_normal[:, ALL_FEATURES.index(border)], 1e-3))
        ref[art] = (float(a.mean()), float(a.std() + 1e-6))
        ref[border] = (float(lb.mean()), float(lb.std() + 1e-6))
    return ref


def _breakout_scores(X, ref):
    """Per side (left, right, top): how unusual it is versus normal cards, averaging
    'more art in the frame strip' and 'weaker art-box border line' (both in standard deviations)."""
    out = []
    for _, art, border in BREAKOUT_SIDES:
        za = (X[:, ALL_FEATURES.index(art)] - ref[art][0]) / ref[art][1]
        zb = (ref[border][0] - np.log(np.maximum(X[:, ALL_FEATURES.index(border)], 1e-3))) / ref[border][1]
        out.append((za + zb) / 2)
    return np.stack(out, axis=1)


def _fit_softmax(Z, yi, k, l2, iterations, weights=None):
    """Multinomial logistic regression by plain gradient descent (small data, no extra packages).
    weights: optional per-sample weights (mean 1)."""
    n, d = Z.shape
    W, b = np.zeros((d, k)), np.zeros(k)
    Y = np.eye(k)[yi]
    w = np.ones((n, 1)) if weights is None else np.asarray(weights, np.float64)[:, None]
    for _ in range(iterations):
        G = (_softmax(Z @ W + b) - Y) * w
        W -= 0.5 * (Z.T @ G / n + l2 * W / n)
        b -= 0.5 * G.mean(axis=0)
    return W, b


def _softmax(logits):
    logits = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(logits)
    return e / e.sum(axis=1, keepdims=True)


def fisher_scores(Z, y):
    """Between-rarity spread / within-rarity spread, per measurement."""
    classes = sorted(set(y.tolist()))
    overall = Z.mean(axis=0)
    between = np.zeros(Z.shape[1], np.float32)
    within = np.zeros(Z.shape[1], np.float32)
    for c in classes:
        zc = Z[y == c]
        between += len(zc) * (zc.mean(axis=0) - overall) ** 2
        within += ((zc - zc.mean(axis=0)) ** 2).sum(axis=0)
    return between / (within + 1e-6)


def evaluate(X, y):
    """Leave-one-capture-out: each capture is guessed by a model trained on all the others.
    Returns accuracy, per-rarity results and the confusion table."""
    X = np.asarray(X, np.float32)
    y = np.asarray(y)
    guesses = []
    for i in range(len(y)):
        keep = np.arange(len(y)) != i
        model = RarityModel().fit(X[keep], y[keep])
        guesses.append(next(iter(model.predict(X[i]))) if len(model.classes) > 1 else None)
    per = {}
    confusion = {}
    for truth, guess in zip(y.tolist(), guesses):
        p = per.setdefault(truth, {"right": 0, "total": 0})
        p["total"] += 1
        p["right"] += int(truth == guess)
        confusion.setdefault(truth, {}).setdefault(str(guess), 0)
        confusion[truth][str(guess)] += 1
    right = sum(p["right"] for p in per.values())
    return {"accuracy": round(right / max(1, len(y)), 3), "captures": len(y), "perRarity": per, "confusion": confusion}


# ---- training data on disk ----------------------------------------------------

def save_capture_features(folder, vec):
    """Cache a capture's features next to its photos (so training never recomputes them)."""
    try:
        (Path(folder) / "features.json").write_text(json.dumps({
            "version": FEATURE_VERSION, "names": ALL_FEATURES,
            "vector": [round(float(v), 5) for v in vec]}), encoding="utf-8")
    except OSError:
        pass


def load_capture_features(folder):
    """Features of one saved capture, cached in features.json next to its photos."""
    folder = Path(folder)
    cache = folder / "features.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("version") == FEATURE_VERSION:
            return np.array(cached["vector"], np.float32)
    except (OSError, ValueError):
        pass
    cards = [cv2.imread(str(p)) for p in sorted(folder.glob("[0-9][0-9].jpg"))]
    cards = [c for c in cards if c is not None]
    if not cards:
        return None
    vec = capture_features(cards)
    save_capture_features(folder, vec)
    return vec


def load_training(root):
    """(X, y, capture ids) from data/training/<rarity>/<capture>/."""
    X, y, ids = [], [], []
    for folder in sorted(Path(root).glob("*/*/")):
        if not (folder / "meta.json").is_file():
            continue
        vec = load_capture_features(folder)
        if vec is not None:
            X.append(vec)
            y.append(folder.parent.name)
            ids.append(folder.name)
    return (np.array(X, np.float32) if X else np.zeros((0, len(ALL_FEATURES)), np.float32)), np.array(y), ids
