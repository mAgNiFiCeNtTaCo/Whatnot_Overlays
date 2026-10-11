"""Find a hand-held Yu-Gi-Oh card inside the on-screen outline, straighten it, and
measure whether the photo is good enough to keep."""

import cv2
import numpy as np

CARD_ASPECT = 59.0 / 86.0          # width / height of a Yu-Gi-Oh card (0.686)
OUT_W, OUT_H = 420, 612            # straightened card size (same aspect)
ASPECT_TOLERANCE = 0.09            # how far from 0.686 a found shape may be
SEARCH_PAD = 0.18                  # search this far around the outline (share of its height)
MAX_AREA_SHARE = 2.2               # a card may be up to this many times the outline's area
DETECT_MAX = 10_000                # shrink the search area above this height (shrinking hurt accuracy, so effectively off)
GUIDE_SIZES = {"xsmall": 0.35, "small": 0.5, "medium": 0.68, "large": 0.86}  # share of the frame


def guide_box(frame_w, frame_h, size="medium"):
    """The on-screen outline the card is held in: (x, y, w, h), centred, card-shaped.
    Sized by height, or by width when the frame is too narrow (portrait cameras)."""
    room = min(frame_h, frame_w / CARD_ASPECT)
    h = int(room * GUIDE_SIZES.get(size, GUIDE_SIZES["medium"]))
    w = int(h * CARD_ASPECT)
    return (frame_w - w) // 2, (frame_h - h) // 2, w, h


def guide_quad(guide):
    x, y, w, h = guide
    return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float32)


def order_corners(pts):
    """Four points -> top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(pts, np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()  # y - x
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def _sides(q):
    width = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    height = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    return width, height


def _fit_side(pts, a, b, rng):
    """Best straight line through the outline points along side a->b, or None if too
    little of that side is visible. RANSAC, so a thumb's curve is outvoted by the
    card's straight edge."""
    d = b - a
    length = float(np.linalg.norm(d))
    if length < 1:
        return None
    u = d / length
    n = np.array([-u[1], u[0]], np.float32)
    rel = pts - a
    along, across = rel @ u, rel @ n
    band = pts[(along > -0.05 * length) & (along < 1.05 * length) & (np.abs(across) < 0.12 * length)]
    need = max(15, int(0.3 * length))
    if len(band) < need:
        return None
    # 60 random point pairs, all scored at once; keep the line most points agree with.
    tol = 2.5
    i = rng.integers(0, len(band), 60)
    j = rng.integers(0, len(band), 60)
    v = band[j] - band[i]
    vl = np.linalg.norm(v, axis=1)
    ok = (vl >= 0.15 * length) & (np.abs(v @ u) >= 0.96 * np.maximum(vl, 1e-6))  # within ~16 degrees of the side
    if not ok.any():
        return None
    i, v, vl = i[ok], v[ok], vl[ok]
    normals = np.stack([-v[:, 1], v[:, 0]], axis=1) / vl[:, None]
    dist = np.abs((band[None, :, :] - band[i][:, None, :]) @ normals[:, :, None])[..., 0]
    counts = (dist < tol).sum(axis=1)
    k = int(np.argmax(counts))
    if counts[k] < need:
        return None
    best_inliers = dist[k] < tol
    vx, vy, x0, y0 = cv2.fitLine(band[best_inliers], cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    return np.array([x0, y0], np.float32), np.array([vx, vy], np.float32)


def _refine_edges(pts, quad):
    """Re-fit each side of a rough quad as a straight line; corners = line crossings.
    Returns (corners, number of sides found as straight lines)."""
    rng = np.random.default_rng(0)
    lines, straight = [], 0
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        fit = _fit_side(pts, a, b, rng)
        if fit is None:
            d = b - a
            lines.append((a, d / max(float(np.linalg.norm(d)), 1e-6)))
        else:
            lines.append(fit)
            straight += 1
    corners = []
    for i in range(4):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        m = np.array([d1, -d2], np.float32).T
        if abs(float(np.linalg.det(m))) < 1e-6:
            return quad, 0
        t, _ = np.linalg.solve(m, p2 - p1)
        corners.append(p1 + t * d1)
    corners = np.array(corners, np.float32)
    w, h = _sides(quad)
    if float(np.abs(corners - quad).max()) > 0.2 * max(w, h):  # refinement went wild: keep the rough quad
        return quad, 0
    return corners, straight


def _card_shaped(q):
    """A card held up to a camera stays close to a rectangle: corners within 20 degrees
    of square, opposite sides within 15% of each other and within 12 degrees of parallel. Rejects outlines that
    merged with something in the background (or the palm holding the card)."""
    for i in range(4):
        v1, v2 = q[i - 1] - q[i], q[(i + 1) % 4] - q[i]
        cos = float(v1 @ v2) / (float(np.linalg.norm(v1) * np.linalg.norm(v2)) + 1e-6)
        if abs(cos) > 0.34:  # cos 70 degrees
            return False
    side = [float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4)]
    if min(side[0], side[2]) / max(side[0], side[2]) <= 0.85 or min(side[1], side[3]) / max(side[1], side[3]) <= 0.85:
        return False
    for i in (0, 1):  # opposite edges stay nearly parallel (within 12 degrees)
        v1, v2 = q[i + 1] - q[i], q[(i + 3) % 4] - q[(i + 2) % 4]
        cos = abs(float(v1 @ v2)) / (float(np.linalg.norm(v1) * np.linalg.norm(v2)) + 1e-6)
        if cos < 0.978:
            return False
    return True


def _edge_map(gray, boost):
    """Edges of a grayscale region. boost=True lifts local contrast first, which brings out
    a dark card frame (Xyz, Fusion, Trap) against a dark sleeve or background."""
    if boost:
        lum = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
        return cv2.Canny(cv2.GaussianBlur(lum, (5, 5), 0), 30, 90)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    median = float(np.median(blurred[::4, ::4]))
    return cv2.Canny(blurred, int(max(10, 0.66 * median)), int(min(255, max(40, 1.33 * median))))


def _best_quad(edges, guide_area, close):
    """Biggest card-shaped outline in an edge map. close = how hard to bridge gaps."""
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=close)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    best, best_score = None, 0.0
    for c in contours:
        area = cv2.contourArea(c)
        if area < 0.2 * guide_area or area > MAX_AREA_SHARE * guide_area:
            continue
        hull = cv2.convexHull(c)
        hull_area = cv2.contourArea(hull)
        if hull_area <= 0 or area / hull_area < 0.6:  # fingers merged into the outline lower this
            continue
        rough = cv2.approxPolyN(hull, 4, ensure_convex=True).reshape(4, 2).astype(np.float32)
        quad, straight = _refine_edges(c.reshape(-1, 2).astype(np.float32), order_corners(rough))
        # "edges": all four sides were found as straight lines; "box": some side was
        # mostly hidden (fingers) and its position comes from the outline's hull.
        method = "edges" if straight == 4 else "box"
        q = order_corners(quad)
        qw, qh = _sides(q)
        if min(qw, qh) <= 0 or not _card_shaped(q):
            continue
        aspect = min(qw, qh) / max(qw, qh)
        if abs(aspect - CARD_ASPECT) > ASPECT_TOLERANCE:
            continue
        # The four sides must really sit on edges: rules out blobs of hand + background
        # that happen to have a card-like hull.
        support = [_side_support(edges, q[i], q[(i + 1) % 4]) for i in range(4)]
        if min(support) < 0.3 or sum(support) / 4 < 0.55:
            continue
        score = (cv2.contourArea(q) * (1.0 - 4 * abs(aspect - CARD_ASPECT)) * (1.1 if method == "edges" else 1.0)
                 * (sum(support) / 4))
        if score > best_score:
            best, best_score = (q, method, aspect), score
    return best


def _side_support(dil, a, b):
    """Share of the middle 80% of segment a-b that lies on an edge."""
    n = max(12, int(np.linalg.norm(b - a) / 4))
    ts = np.linspace(0.1, 0.9, n)[:, None]
    pts = (a[None, :] * (1 - ts) + b[None, :] * ts).round().astype(int)
    h, w = dil.shape
    ok = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
    hit = np.zeros(len(pts), bool)
    hit[ok] = dil[pts[ok, 1], pts[ok, 0]] > 0
    return float(hit.mean())


def _offset_support(dil, a, b, n, offsets):
    """_side_support of segment a-b shifted outwards by each offset, computed together."""
    if len(offsets) == 0:
        return np.zeros(0)
    count = max(12, int(np.linalg.norm(b - a) / 4))
    ts = np.linspace(0.1, 0.9, count, dtype=np.float32)[:, None]
    base = a[None, :] * (1 - ts) + b[None, :] * ts                       # points on the side
    pts = (base[None, :, :] + offsets[:, None, None] * n[None, None, :]).round().astype(int)
    h, w = dil.shape
    ok = (pts[..., 0] >= 0) & (pts[..., 0] < w) & (pts[..., 1] >= 0) & (pts[..., 1] < h)
    hit = np.zeros(pts.shape[:2], bool)
    hit[ok] = dil[pts[..., 1][ok], pts[..., 0][ok]] > 0
    return hit.mean(axis=1)


def _expand_to_outer(q, dil):
    """A Yu-Gi-Oh card's art + text area is itself card-shaped (about 87% x 83% of the card),
    and on a dark card it's often the only closed outline. If straight edges sit just
    outside the sides, at the distance a card border would be, grow to them.

    Needs 3 sides, or one opposite pair (top + bottom, or left + right): fingers often
    hide the other pair, whose position then follows from the card's shape.
    Returns the bigger quad or None."""
    q = order_corners(q)
    centre = q.mean(axis=0)
    qw, qh = _sides(q)
    normals, moves = [], []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        u = (b - a) / max(float(np.linalg.norm(b - a)), 1e-6)
        n = np.array([-u[1], u[0]], np.float32)
        if float(((a + b) / 2 - centre) @ n) < 0:
            n = -n  # point outwards
        perp = qh if i % 2 == 0 else qw  # top/bottom move along the height, sides along the width
        offsets = np.arange(0.045 * perp, 0.2 * perp, 2.0, dtype=np.float32)
        support = _offset_support(dil, a, b, n, offsets)
        good = np.nonzero(support >= 0.55)[0]
        normals.append((u, n))
        moves.append(float(offsets[good[-1]]) if len(good) else None)  # farthest good line: outer edge
    found = [m is not None for m in moves]
    if sum(found) < 3:
        if found[0] and found[2] and not (found[1] or found[3]):    # top + bottom: infer the sides
            spare = ((qh + moves[0] + moves[2]) * CARD_ASPECT - qw) / 2
            moves[1] = moves[3] = spare
        elif found[1] and found[3] and not (found[0] or found[2]):  # left + right: infer top/bottom
            spare = ((qw + moves[1] + moves[3]) / CARD_ASPECT - qh) / 2
            moves[0] = moves[2] = spare
        else:
            return None
        if spare <= 0:
            return None
    lines = []
    for i in range(4):
        u, n = normals[i]
        lines.append((q[i] + n * (moves[i] or 0.0), u))
    corners = []
    for i in range(4):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        m = np.array([d1, -d2], np.float32).T
        if abs(float(np.linalg.det(m))) < 1e-6:
            return None
        t, _ = np.linalg.solve(m, p2 - p1)
        corners.append(p1 + t * d1)
    out = order_corners(np.array(corners, np.float32))
    ow, oh = _sides(out)
    aspect = min(ow, oh) / max(ow, oh)
    if not (1.04 <= ow / qw <= 1.35 and 1.04 <= oh / qh <= 1.35):
        return None
    if abs(aspect - CARD_ASPECT) > ASPECT_TOLERANCE or not _card_shaped(out):
        return None
    return out


def find_card(frame, guide, deep=True):
    """Look for the card's outline near the guide. deep=False skips the slower passes for
    dark cards (callers run them every few frames while nothing is in view).

    Returns {"found": bool, "quad": 4x2 corners in frame coordinates or None,
    "method": "edges" (clean 4-corner outline) | "box" (outline partly hidden by
    fingers, squared off) | None, "aspect": float or None}."""
    fh, fw = frame.shape[:2]
    x, y, w, h = guide
    pad = int(SEARCH_PAD * h)  # cards are often held bigger than the outline: look well beyond it
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(fw, x + w + pad), min(fh, y + h + pad)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return {"found": False, "quad": None, "method": None, "aspect": None}
    # Work on a smaller copy of big frames: ~4x faster at 1080p, corners still within ~2 px.
    scale = min(1.0, DETECT_MAX / roi.shape[0])
    if scale < 1.0:
        roi = cv2.resize(roi, (int(roi.shape[1] * scale), int(roi.shape[0] * scale)), interpolation=cv2.INTER_AREA)

    guide_area = float(w * h) * scale * scale
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    boosted = None  # contrast-boosted edges: built only when needed (they cost ~10 ms)
    # Plain edges first; then contrast-boosted edges with more gap bridging for dark cards.
    found = _best_quad(_edge_map(gray, boost=False), guide_area, 2)
    if found is None and deep:
        boosted = _edge_map(gray, boost=True)
        found = _best_quad(boosted, guide_area, 2) or _best_quad(boosted, guide_area, 5)
    if found is None:
        return {"found": False, "quad": None, "method": None, "aspect": None}
    quad, method, aspect = found
    if boosted is None:
        boosted = _edge_map(gray, boost=True)
    bigger = _expand_to_outer(quad, cv2.dilate(boosted, np.ones((5, 5), np.uint8)))
    if bigger is not None:
        quad, method = bigger, "expanded"
        qw, qh = _sides(quad)
        aspect = min(qw, qh) / max(qw, qh)
    return {"found": True, "quad": quad / scale + np.array([x0, y0], np.float32), "method": method,
            "aspect": float(aspect)}


def warp_card(frame, quad):
    """Straighten the card to OUT_W x OUT_H, always portrait."""
    q = order_corners(quad)
    qw, qh = _sides(q)
    if qw > qh:  # held sideways: rotate the corner order so the card comes out upright
        q = np.array([q[3], q[0], q[1], q[2]], np.float32)
    dst = np.array([[0, 0], [OUT_W - 1, 0], [OUT_W - 1, OUT_H - 1], [0, OUT_H - 1]], np.float32)
    m = cv2.getPerspectiveTransform(q, dst)
    return cv2.warpPerspective(frame, m, (OUT_W, OUT_H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def quality(card):
    """Sharpness (higher = crisper), brightness 0-255 and glare share 0-1 of a straightened card."""
    gray = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(hsv[..., 2].mean())
    glare = float(np.count_nonzero((hsv[..., 2] > 245) & (hsv[..., 1] < 40)) / gray.size)
    return {"sharp": round(sharp, 1), "bright": round(bright, 1), "glare": round(glare, 4)}
