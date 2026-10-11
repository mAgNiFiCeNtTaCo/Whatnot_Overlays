# Whatnot hit counter: progress log

Live Yu-Gi-Oh box-break hit counter for a Whatnot stream, plus a camera card scanner that reads each
hit's rarity and name and logs it automatically. Everything runs locally on the streaming PC.
How to use it: README.md. API and data formats: CONTRACT.md. This file: what was built, where it stands,
what's next.

## Where it stands (2026-10-11)

- **Counter** (`server.py`, port 7777): control panel `/control`, OBS overlay `/overlay`. Stdlib Python.
- **Scanner** (`python -m scanner` or `scan.bat`, port 7778): needs OpenCV + NumPy; card names need the
  Windows OCR bindings (winrt-*). Reads the camera from OBS via obs-websocket (`OBS: Video Capture Device`).
- **Tests:** 114, all passing (`python -m unittest discover -s tests`, ~90 s). One timing flake was fixed
  (counter request timeout raised to 10 s).
- **Training data** (`data/training/`, ~340 MB, not in any repo): 160 captures. Not a hit 10,
  Overframe 30, Secret 42, Starlight 33, Starlight Overframe 15, Ultra 30. 11 of those were learned
  from stream reads.
- **Recognition:** held-out accuracy 91.2% (leave-one-capture-out). Names: sure on ~90% of Secrets,
  ~80% of Ultras/Starlights; Japanese cards aren't read (English OCR).

## Log

### 2026-10-11: names on shiny cards, test mode, stream polish, brain
- **Card names on rainbow foil:** Heavy Polymerization logged as "Polymerization" (HEAVY in blue-purple foil
  is as bright as the green bar, so brightness-based OCR lost it). Added a colour-contrast image version
  (max channel distance from the bar), widened the name crop to the card edge, and "longer name"
  completion (a word seen in any photo upgrades Polymerization to Heavy Polymerization). Results on all
  captures: Heavy Poly 3/3 sure, Gagaga Magic 7 sure (was 0), no wrong upgrades; ~1 s per read. Fixed
  the user's two mis-named counter entries via `/api/auto/name`.
- **Test mode:** scanner switch (turns auto mode on); hits go to the counter's `testLog` (control panel
  "Test hits", overlay preview with `?test=1`), never counted, never on the OBS overlay.
- **Overlay:** Biggest hits became a right-to-left ticker (JS-driven; checked moving in a real browser via
  CDP); modern glass redesign with embedded Outfit font (old look kept as `web/overlay-classic.html`);
  Panel background slider 0-100% (`panelOpacity`, default 85; `?panel=` per source) with text outline
  that strengthens as panels fade; holo borders redrawn as masked rings.
- **Fix:** scanner -> counter requests time out at 10 s (was 3 s; a busy PC caused false "counter
  offline"), still never retried.
- **Decided against:** hit prices (no legitimate per-rarity TCGplayer source; user wanted TCGplayer +
  eBay or nothing).
- **Setup:** created this log, the shared brain (`D:\ClaudeProjects\_brain`, session-start scan hook) and
  the `/save-progress` command. Tests: 114 passing.
- **Open:** see "Known gaps / next steps" below.

## Built, in order

1. **Counter + overlay:** 5 rarities (UR, SCR, OF, STR, SOF); boxes on the table as groups
   ("BOXES 1–4 / 12") with total; packs across the table (0/96, stops at the cap, rolls to the next
   group); packs-left chip (green / yellow pulse < 50% / red fast pulse < 20%); case tracker (OF, STR, SOF
   totals); case log per group; On stream toggles; foot pedal = global hotkeys (Ctrl+Shift+9 / +8).
2. **Phase 1, training photos:** card detection (Canny + RANSAC sides + card-shape checks + expansion to
   the outer edge), captures of ~20 straightened photos per card, OBS screenshots as the camera source.
3. **Phase 3, rarity recognition ("mix" approach):** foil measurements per region + tilt changes,
   logistic regression + Fisher-weighted kNN blend, plus markers: art breaks the box (Overframe, any
   side), Starlight shine, name shines (hit vs not a hit; rainbow-across-letters feature). Product of
   experts combines them; Starlight Overframe can be named from markers alone.
4. **Auto mode:** reads each card held up once (steady 0.5 s, 10 photos), adds sure hits (>= 85%) to the
   counter, skips non-hits, "shown again" only for the same rarity within 3 min. Unsure reads go to a
   **Needs your answer** queue (keys 1-6). Learns while streaming: sure hits saved as unchecked
   captures, answers saved as checked; retrains at most every 30 s while streaming.
5. **Phase 2, card names:** Windows OCR of the name bar (4 image versions incl. colour contrast for
   rainbow foil), all photos vote, matched to the YGOPRODeck card list (`data/cards.json`, refreshed
   weekly), partial-name and "longer name" completion (Heavy Polymerization). Card-list printing check:
   a sure name never printed in the read rarity goes to the answer queue. Names editable in the control
   panel hit log and scanner.
6. **Test mode:** hits go to the counter's `testLog` (shown in the control panel, overlay preview only
   with `?test=1`), never counted or on stream.
7. **Stream polish:** Biggest hits right-to-left ticker (SOF > STR > OF, off by default), modern glass
   look with embedded Outfit font (offline-safe; old design in `web/overlay-classic.html`), Panel
   background slider 0-100% (default 85).

## Decisions worth remembering

- Overlay pages must stay self-contained (no external fonts/scripts): a test enforces it.
- **Hit prices: dropped (2026-10-11).** No legitimate per-rarity source: TCGplayer API is closed and
  scraping is against their rules; YGOPRODeck's per-printing `set_price` is 0 and its card prices are
  cheapest-copy only; yugiohprices.com is gone. The user wanted TCGplayer + eBay or nothing. An eBay-only
  version (official Browse API, user's own free keys, control panel only) is possible if wanted.
- Windows OCR must not use asyncio event loops on this PC (loopback socket stalls): it waits on the
  WinRT completion handler with a 5 s timeout.
- Counter requests from the scanner are never retried (a retry could double-count a hit).

## Known gaps / next steps

- Data the user can add: 30+ different Not-a-hit cards (especially Rares and Spell/Trap Supers),
  different Overframe and Starlight Overframe cards (each is mostly one card now), a practice break in
  Test mode.
- Answer queue is in memory only: unanswered reads are lost if the scanner closes.
- Ideas not built: set-code OCR (exact printing), Japanese OCR, Test-mode accuracy scorecard, chase list
  for the set, card art in the hit banner, review page for unchecked stream-learned captures.
