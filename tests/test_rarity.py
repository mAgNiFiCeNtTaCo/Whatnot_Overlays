"""Tests for rarity recognition (phase 3). Skipped when OpenCV/NumPy aren't installed."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import cv2
    import numpy as np

    from scanner import rarity, synth
except ImportError:  # pragma: no cover
    cv2 = None

RARITIES = ["ultra", "secret", "overframe", "starlight", "starlight_overframe", "none"]


def fake_capture(r, card, rng, frames=6):
    gain, bias = rng.uniform(0.85, 1.15), rng.uniform(-15, 15)
    out = []
    for k in range(frames):
        img = synth.make_card(r, phase=k * 0.4, seed=100 + card * 7 + RARITIES.index(r) * 13)
        out.append(np.clip(img.astype(np.float32) * gain + bias + rng.normal(0, 4, img.shape), 0, 255).astype(np.uint8))
    return out


@unittest.skipIf(cv2 is None, "OpenCV/NumPy not installed")
class RarityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(1)
        cls.X, cls.y = [], []
        for r in RARITIES[:5]:
            for card in range(8):
                cls.X.append(rarity.capture_features(fake_capture(r, card, rng)))
                cls.y.append(r)
        cls.X, cls.y = np.array(cls.X), np.array(cls.y)

    def test_vector_shape(self):
        self.assertEqual(self.X.shape, (40, len(rarity.ALL_FEATURES)))
        self.assertTrue(np.isfinite(self.X).all())

    def test_tells_synthetic_rarities_apart(self):
        result = rarity.evaluate(self.X, self.y)
        self.assertGreaterEqual(result["accuracy"], 0.9, result)

    def test_model_needs_two_rarities(self):
        one = rarity.RarityModel().fit(self.X[self.y == "ultra"], self.y[self.y == "ultra"])
        self.assertFalse(one.ready)
        pick = np.concatenate([np.nonzero(self.y == "ultra")[0][:4], np.nonzero(self.y == "secret")[0][:4]])
        two = rarity.RarityModel().fit(self.X[pick], self.y[pick])
        self.assertTrue(two.ready)
        probs = two.predict(self.X[0])
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=2)
        # Regression: guesses go into meta.json; NumPy floats there made every save fail.
        import json
        self.assertEqual(json.loads(json.dumps(probs)), probs)
        self.assertTrue(all(type(k) is str and type(v) is float for k, v in probs.items()))
        self.assertEqual(len(two.top_signals(3)), 3)

    def test_markers_predict_starlight_overframe_without_examples(self):
        # Train on everything except Starlight Overframe: the markers (art breaks the box from
        # Overframe, shine from Starlight) must still let it name a Starlight Overframe.
        keep = self.y != "starlight_overframe"
        model = rarity.RarityModel().fit(self.X[keep], self.y[keep])
        self.assertTrue(model.marker_info()["overframe"]["trained"])
        self.assertTrue(model.marker_info()["starlight"]["trained"])
        sof = self.X[~keep]
        guesses = [next(iter(model.predict(x))) for x in sof]
        self.assertGreaterEqual(guesses.count("starlight_overframe"), len(sof) // 2, guesses)
        m = model.markers(sof[0])
        self.assertGreater(m["overframe"], 0.5)
        self.assertGreater(m["starlight"], 0.5)

    def test_name_shine_marker_spots_non_hits(self):
        # "Not a hit" captures (plain ink name) train the name-shine marker: hits' names shine,
        # non-hits' don't, and non-hits are named as such.
        rng = np.random.default_rng(3)
        none = np.array([rarity.capture_features(fake_capture("none", card, rng)) for card in range(8)])
        X = np.concatenate([self.X, none[:6]])
        y = np.concatenate([self.y, ["none"] * 6])
        model = rarity.RarityModel().fit(X, y)
        self.assertTrue(model.marker_info()["name_shine"]["trained"])
        for x in none[6:]:
            self.assertLess(model.markers(x)["name_shine"], 0.5)
            self.assertEqual(next(iter(model.predict(x))), "none")
        hits = [model.markers(x)["name_shine"] for x in self.X]
        self.assertGreater(float(np.median(hits)), 0.5)

    def test_features_cached_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "secret" / "20260101-000000-abcd"
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text("{}", encoding="utf-8")
            for i, img in enumerate(fake_capture("secret", 1, np.random.default_rng(2), frames=3)):
                cv2.imwrite(str(folder / f"{i:02d}.jpg"), img)
            X, y, ids = rarity.load_training(tmp)
            self.assertEqual((len(y), y[0], ids[0]), (1, "secret", folder.name))
            self.assertTrue((folder / "features.json").is_file())
            X2, _, _ = rarity.load_training(tmp)  # from the cache
            np.testing.assert_allclose(X, X2, atol=1e-3)


if __name__ == "__main__":
    unittest.main()
