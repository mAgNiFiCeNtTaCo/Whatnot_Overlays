"""Card names: matching OCR text to the card list, voting across photos, printed rarities."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import cv2
    import numpy as np

    from scanner import names
except ImportError:  # pragma: no cover - depends on the machine
    cv2 = None

CARDS = [
    {"n": "Ketu Dracotail", "s": [["JUSH-EN008", "Super Rare"], ["26LP-EN004", "Secret Rare"]]},
    {"n": "Gagaga Magician", "s": [["MAMO-EN010", "Ultra Rare"]]},
    {"n": "Gagaga Magician - Gagaga Magic", "s": [["MAMO-EN011", "Starlight Rare"], ["MAMO-EN011", "Ultra Rare"]]},
    {"n": "Angelechy Destrier", "s": [["CORI-EN092", "Super Rare"]]},
    {"n": "Visas Samsara", "s": [["AGOV-EN004", "Secret Rare"]]},
    {"n": "Ash Blossom & Joyous Spring", "s": [["MACR-EN036", "Secret Rare"], ["RA01-EN008", "Starlight Rare"]]},
    {"n": "Brand New Card", "s": []},
    {"n": "Polymerization", "s": [["LOB-EN059", "Super Rare"], ["RA01-EN051", "Secret Rare"]]},
    {"n": "Heavy Polymerization", "s": [["RA03-EN054", "Starlight Rare"], ["RA03-EN054", "Secret Rare"]]},
]


def one_per_photo(texts, images):
    """Fake OCR for a batch: each photo's first image reads its text, the other versions read nothing."""
    per = len(images) // len(texts)
    return [t if i % per == 0 else [] for i, t in enumerate(x for t in texts for x in [t] * per)]


@unittest.skipIf(cv2 is None, "OpenCV/NumPy not installed")
class NameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "cards.json"
        path.write_text(json.dumps({"updated": 1, "sets": {"CORI": "Chaos Origins"}, "cards": CARDS}), encoding="utf-8")
        self.cl = names.CardList(path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_the_list(self):
        self.assertTrue(self.cl.ready)
        self.assertEqual(len(self.cl.cards), len(CARDS))
        self.assertEqual(self.cl.sets["CORI"], "Chaos Origins")
        self.assertTrue(self.cl.stale())  # "updated" long ago
        self.assertFalse(names.CardList(Path(self.tmp.name) / "missing.json").ready)

    def test_match_survives_ocr_mistakes(self):
        for text, want in (("VISAS SAMSAPA", "Visas Samsara"), ("KETU DRACOTAlL", "Ketu Dracotail"),
                           ("ASH BLOSSOM & JOYOUS SPRlNG", "Ash Blossom & Joyous Spring"),
                           ("Angelechy  Destrier.", "Angelechy Destrier")):
            name, score = self.cl.match(text)
            self.assertEqual(name, want, text)
            self.assertGreater(score, 0.8)
        self.assertEqual(self.cl.match("ab"), (None, 0.0))

    def test_rarity_breaks_look_alike_ties(self):
        # A cut-off "GAGAGA MAGICIAN GAGAGA" is nearer the shorter name, unless the scanner read a Starlight.
        self.assertEqual(self.cl.match("GAGAGA MAGICIAN - GAGAG")[0], "Gagaga Magician - Gagaga Magic")
        self.assertEqual(self.cl.match("GAGAGA MAGICIAN GA", rarity="starlight_overframe")[0],
                         "Gagaga Magician - Gagaga Magic")
        self.assertEqual(self.cl.match("GAGAGA MAGICIAN GA")[0], "Gagaga Magician")

    def test_printed_hits(self):
        self.assertEqual(self.cl.printed_hits("Ketu Dracotail"), ["secret"])
        self.assertEqual(self.cl.printed_hits("Angelechy Destrier"), [])  # Super Rare only: never a hit
        self.assertEqual(self.cl.printed_hits("Ash Blossom & Joyous Spring"), ["secret", "starlight"])
        self.assertIsNone(self.cl.printed_hits("Brand New Card"))  # no printings listed yet
        self.assertIsNone(self.cl.printed_hits("Not A Card"))

    def test_photos_vote(self):
        card = np.zeros((612, 420, 3), np.uint8)
        texts = [["VISAS SAMSARA"], ["VISAS SAMSAPA"], ["V1SAS SAMS"], ["KETU"], []]
        with mock.patch.object(names, "_ENGINE", object()), \
                mock.patch.object(names, "ocr_lines", lambda images: one_per_photo(texts, images)):
            res = names.read_name([card] * 5, self.cl, rarity="secret")
        self.assertEqual(res["name"], "Visas Samsara")
        self.assertTrue(res["sure"])
        self.assertEqual(res["printed"], ["secret"])
        self.assertEqual(res["candidates"][0][0], "Visas Samsara")

    def test_disagreeing_photos_are_not_sure(self):
        card = np.zeros((612, 420, 3), np.uint8)
        texts = [["VISAS SAMSARA"], ["KETU DRACOTAIL"], ["ANGELECHY DESTRIER"]]
        with mock.patch.object(names, "_ENGINE", object()), \
                mock.patch.object(names, "ocr_lines", lambda images: one_per_photo(texts, images)):
            res = names.read_name([card] * 3, self.cl)
        self.assertFalse(res["sure"])

    def test_a_word_seen_in_one_photo_completes_a_longer_name(self):
        # Rainbow foil hides HEAVY in most photos; one photo shows it: Heavy Polymerization, not Polymerization.
        card = np.zeros((612, 420, 3), np.uint8)
        texts = [["POLYMERIZATION"], ["LYMERIZATION"], ["HEAVY"], ["POLYMERIZATI0N"]]
        with mock.patch.object(names, "_ENGINE", object()),                 mock.patch.object(names, "ocr_lines", lambda images: one_per_photo(texts, images)):
            res = names.read_name([card] * 4, self.cl, rarity="starlight")
        self.assertEqual(res["name"], "Heavy Polymerization")
        texts = [["POLYMERIZATION"], ["LYMERIZATION"], ["[SPELL CARD]"], ["POLYMERIZATI0N"]]
        with mock.patch.object(names, "_ENGINE", object()),                 mock.patch.object(names, "ocr_lines", lambda images: one_per_photo(texts, images)):
            self.assertEqual(names.read_name([card] * 4, self.cl)["name"], "Polymerization")

    def test_colour_contrast_version_shows_letters_as_bright_as_the_bar(self):
        # Blue letters on a green bar of the same brightness: only the colour version shows them.
        card = np.zeros((612, 420, 3), np.uint8)
        card[:] = (90, 140, 60)  # green (BGR)
        cv2.putText(card, "HEAVY", (20, 55), cv2.FONT_HERSHEY_DUPLEX, 1.4, (200, 100, 120), 3)
        colour = names.name_images(card)[-1]
        self.assertEqual(colour.ndim, 2)
        self.assertLess(float(np.percentile(colour, 2)), 80)  # the letters stand out dark...
        self.assertGreater(float(np.median(colour)), 200)     # ...on a white bar
        gray = names.name_images(card)[1]  # while in plain brightness they barely do
        self.assertLess(float(np.median(gray)) - float(np.percentile(gray, 2)), 60)

    def test_no_text_no_name(self):
        card = np.zeros((612, 420, 3), np.uint8)
        with mock.patch.object(names, "_ENGINE", object()), \
                mock.patch.object(names, "ocr_lines", lambda images: [[] for _ in images]):
            self.assertIsNone(names.read_name([card] * 3, self.cl))

    @unittest.skipUnless(cv2 is not None and names.ocr_available(), "Windows text recognition not available")
    def test_windows_ocr_reads_a_printed_name(self):
        card = np.full((612, 420, 3), 225, np.uint8)
        cv2.putText(card, "VISAS SAMSARA", (30, 50), cv2.FONT_HERSHEY_DUPLEX, 1.1, (20, 20, 20), 2, cv2.LINE_AA)
        res = names.read_name([card] * 3, self.cl, rarity="secret")
        self.assertIsNotNone(res)
        self.assertEqual(res["name"], "Visas Samsara")


if __name__ == "__main__":
    unittest.main()
