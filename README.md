# Whatnot Yu-Gi-Oh Hit Counter

A live hit counter for Yu-Gi-Oh booster box breaks on Whatnot. You click buttons in a
**control panel**. Your viewers see the counts on an **overlay** that you add to OBS as a Browser
Source (Whatnot streams from OBS over RTMP).

It tracks five rarities, per box and for the whole session:
**Ultra Rare · Secret Rare · Overframe · Starlight Rare · Starlight Overframe**.
Every hit pops up a banner on stream. The two Starlight rarities get a holo "CHASE PULL" banner
with sparkles.

Everything runs on your own PC with Python's standard library: no installs, no internet and no
accounts. Counts are saved to `data/state.json` after every click, so a crash or reboot
doesn't lose them.

## Start

1. Install **Python 3** from <https://www.python.org/downloads/> (tested with 3.14) and tick
   *"Add python.exe to PATH"* during setup.
2. Double-click **`start.bat`**. A console window opens with the server and your browser opens
   the control panel at **<http://127.0.0.1:7777/control>**.
3. Keep that console window open while you stream. Press **Ctrl+C** in it (or close it) to stop.

Or start it from a terminal:

```
python server.py                     # http://127.0.0.1:7777
python server.py --port 8000         # another port
python server.py --host 0.0.0.0      # also reachable from your phone (see below)
```

> Use `127.0.0.1`, not `localhost`. On some Windows PCs `localhost` tries IPv6 first and
> each request stalls for a second or two.

## Add the overlay to OBS

The overlay has a modern look built for a vertical phone stream: a frosted glass panel for the
header, pill stats, glass rarity cards with a glowing colour strip, and a big hit banner. Its font
(Outfit, SIL Open Font License) is built into the page, so nothing is downloaded during a stream.
The previous design is kept as `web/overlay-classic.html` (not served) if you ever want it back.

**Panel background** (control panel, under the On stream switches) sets how solid those panels
are, live: 0% shows just the text over your stream, 100% solid panels (85% by default). The less
panel there is, the stronger the dark outline around the text, so it stays readable over the
camera. To fix one OBS source to its own value, add `panel=` (0-100) to its link.


1. In OBS: **Sources → + → Browser**, name it e.g. "Hit counter".
2. **URL:** `http://127.0.0.1:7777/overlay` (or the link you build in the control panel, see
   below).
3. **Width / Height**: pick by layout. These sizes leave room for the hit banner under the tiles:

   | Overlay link                         | Suggested size | Measured content at scale 1 |
   |--------------------------------------|----------------|-----------------------------|
   | `/overlay` (row, the default)        | **1280 × 340** | 734 × 325                   |
   | `/overlay?alerts=0` (row, no banner) | 1280 × 240     | 734 × 223                   |
   | `/overlay?layout=column&show=both`   | **440 × 560**  | 398 × 530                   |

   With `scale=1.5`, multiply by 1.5. The overlay sits in the top-left corner of the source, and
   the rest of the source is transparent, so a source that's too big does no harm. One that's too
   small cuts off the banner.
4. **Untick "Shutdown source when not visible"**, so the overlay stays connected and doesn't miss
   hits while you're on another scene.
5. Leave the default Custom CSS as it is. The overlay is already transparent.
6. Drag and crop the source where you want it on your Whatnot scene.

If you change the overlay link or update the files, right-click the source → **Refresh**.

### Build the overlay link in the control panel

Open **OBS overlay link** at the bottom of the control panel. Pick the layout, counts, scale,
background and which rarities to hide (a live preview shows the result), then press **Copy** and paste the link into the Browser
Source URL.

### Overlay options

Add them to the URL, e.g. `http://127.0.0.1:7777/overlay?layout=column&show=both&hide=overframe`.

| Option   | Values                         | Default | What it does |
|----------|--------------------------------|---------|--------------|
| `layout` | `row` \| `column`              | `row`   | Tiles side by side, or stacked |
| `show`   | `box` \| `session` \| `both`   | `box`   | Big number is the current box or the whole session; `both` = box big, session small |
| `hide`   | comma list of rarity ids       | none    | e.g. `hide=overframe,starlight_overframe`. Hides the tile and its banners |
| `scale`  | number, e.g. `1.5`             | `1`     | Make everything bigger or smaller |
| `title`  | `1` \| `0`                     | `1`     | Show the stream title |
| `packs`  | `1` \| `0`                     | `1`     | `0` always hides the "Pack X / Y" counter in this OBS source |
| `boxes`  | `1` \| `0`                     | `1`     | `0` always hides the box counter in this OBS source |
| `left`   | `1` \| `0`                     | `1`     | `0` always hides the packs-left counter in this OBS source |
| `tracker`| `1` \| `0`                     | `1`     | `0` always hides the case tracker in this OBS source |
| `caselog`| `1` \| `0`                     | `1`     | `0` always hides the case log in this OBS source |
| `showcase`| `1` \| `0`                    | `1`     | `0` always hides the Biggest hits panel in this OBS source |
| `panel`  | `0`..`100`                     | slider  | Panel background opacity for this OBS source (otherwise the control panel's slider) |
| `alerts` | `1` \| `0`                     | `1`     | Show the pop-up banner on each hit |
| `bg`     | `none` \| `dark`               | `none`  | Fully transparent, or a dark background (handy for testing in a normal browser) |

Rarity ids: `ultra`, `secret`, `overframe`, `starlight`, `starlight_overframe`.

You normally don't need `packs`, `boxes`, `left`, `tracker`, `caselog` or `showcase`: the **On stream** switches in the control panel show
or hide those counters live on every overlay. The URL options are only for hiding one for good
in a particular OBS source.

## Using the control panel

- **Log a hit:** optionally type the card name first, then tap the rarity. The name clears once
  the hit is saved. If a hit fails to save, a red **NOT saved** message appears, so tap again.
- **−** in a rarity's corner removes the latest hit of that rarity **in the current box**.
- **Undo last hit** removes the most recent hit overall. **✕** in the hit log deletes that one
  entry.
- **Case tracker:** a running count of **Overframe**, **Starlight** and **Starlight Overframe**
  pulled across the whole case (all 12 boxes), shown on the overlay as **CASE TOTAL** and under the
  rarity buttons in the panel. Unlike the per-table counts it never resets when you move to the
  next boxes; only **Reset session** clears it.
- **Case log:** the Overframe / Starlight / Starlight Overframe pulls broken down by each set of
  boxes on the table, e.g. **BOXES 1–4  2 OF · 1 STARLIGHT**, then **BOXES 5–8  1 OF**. The
  current boxes are highlighted. The overlay shows the latest 6 sets; the panel's
  **Chase pulls by boxes** card shows all of them.
- **Biggest hits:** a ticker that scrolls the case's best pulls by name from right to left,
  under the case log: every **Starlight Overframe** first, then **Starlights**, then **Overframes**,
  newest first within each, with the boxes each came from (e.g. **SOF Gagaga Magician - Gagaga
  Magic  BOXES 1–4**). It loops seamlessly at a steady speed whether there are 2 pulls or 20, and
  a new pull's tag glows for a few seconds. A hit logged without a name shows its rarity instead;
  add the name any time by clicking it in the hit log. Off until you switch it on (**Biggest hits**
  under On stream).
- **Packs:** use **+ / −** as you open packs. The count stops when the boxes on the table are
  done (e.g. 96/96 for 4 boxes), so hits from the last pack still count for those boxes. The
  next **+** moves on to the next boxes at 0/96. On the last boxes of the break it stays full.
- **Boxes at a time and the total:** in the box card, enter how many boxes you open together in
  **Boxes at a time** (e.g. `4`, or `1` for one box at a time) and the total boxes in the break in
  **Total boxes** (e.g. `12` for a case), then press **Set**. The counter lines up on whole
  groups: boxes 1–4, then 5–8, then 9–12. The overlay shows
  **BOXES 1–4 / 12** with a progress bar across all 12, and the panel shows how many boxes are
  finished. The pack counter covers every box on the table, so 4 boxes of 24 packs reads
  **PACK X / 96**. Leave **of** empty if there's no set total.
- **On stream switches:** **Packs**, **Boxes**, **Packs left**, **Case tracker**, **Case log** and **Biggest hits**
  show or hide those counters on the overlay right away, with no OBS change. Packs left counts what's still sealed in the boxes on
  the table (e.g. **57 PACKS LEFT**) and changes color with urgency: green over 50% left, yellow
  with a slow pulse under 50%, and red with a fast pulse under 20%, where it reads
  **ONLY 12 PACKS LEFT**.
- **Next box →** moves on to the next boxes, keeping the same number on the table (1–4, then 5–8,
  then 9–12). The per-table counts go back to 0, and the session totals carry on. Going past the
  total asks first and then raises the total.
- **← Prev** goes back to the previous boxes if you pressed Next by mistake. Hits always stay with
  the boxes they were logged in.
- **Settings:** set the stream title, packs per box (default 24) and your own rarity names.
  Clear a name and save to get the default back.
- **Danger zone → Reset session** clears every hit and goes back to box 1. It keeps the title,
  settings, how many boxes go on the table and the total.

Several devices can have the control panel open at once and they all stay in sync.

### Hotkeys (desktop, when you're not typing in a field)

| Key | Action |
|-----|--------|
| `1` – `5` | Hit: Ultra, Secret, Overframe, Starlight, Starlight Overframe |
| `Shift` + `1` – `5` | Remove one of that rarity from the current box |
| `Z` | Undo last hit |
| `+` / `−` | Packs opened +1 / −1 |
| `/` | Jump to the card name box |

## Foot pedal (count packs hands-free)

Most USB foot pedals act like a keyboard: pressing one "types" a key. The counter can listen for
that key whichever window is in front, OBS included, and add a pack each press.

1. In your pedal's own app, set it to send a key nobody types. **F13** to **F24** is ideal; a
   combination like **Ctrl+Alt+P** also works. A plain letter or digit is refused, because the
   counter claims that key for the whole PC and you couldn't type it anywhere else.
2. In the control panel open **Foot pedal**, click **Learn** next to **+1 pack** and press the
   pedal. It shows the key and **Active**.
3. Optional: a second pedal for **−1 pack** to fix a miscount, set the same way.

Each press counts one pack (holding the pedal down still counts once). At the end of the boxes on
the table (e.g. 96/96) the next press moves on to the next boxes, just like **+**. The panel shows a
short confirmation for every press. If it says **Another app already uses this key**, pick a
different key in the pedal's app and Learn again. The pedal works while the counter runs on Windows.

## Card scanner: training photos (phase 1)

The start of camera-based rarity recognition. Phase 1 collects labeled photos of each rarity
from your stream camera; later phases read the card name and suggest the rarity from these photos.

1. Double-click **scan.bat**. The first time, it installs OpenCV and NumPy (about 100 MB), then
   opens the scanner at <http://127.0.0.1:7778/>. Keep the counter running too, so the scanner
   uses your rarity names.
2. Open **Camera & outline**, click **Find cameras** and pick **OBS: <your camera source>**. The
   scanner takes frames straight from OBS (OBS keeps the camera), turned the way your scene turns
   them. This needs OBS's WebSocket server on (OBS > Tools > WebSocket Server Settings, on by
   default in OBS 28+); the scanner reads its password from OBS's settings on this PC. A camera that
   isn't in OBS can be picked by number instead.
3. Pick the rarity of the card in your hand (keys **1**-**6**; **6** = *Not a hit*, for commons,
   rares and supers), optionally type its name, hold the
   card inside the white outline until the frame turns green, press **Capture** (**Space**) and
   tilt the card slowly for a few seconds. It keeps up to 20 sharp photos where the card was found.

Photos are saved in `data/training/<rarity>/<capture>/` with a `meta.json`. Tips:

- Use the same camera, light and sleeves you stream with. That's what the recognition has to learn.
- Many different cards beat many photos of one card. Aim for 30+ cards per rarity; rare ones
  (Starlights) take longest, so use any singles you own.
- A bad capture can be deleted from **Latest captures**.
- `scan.bat --demo` runs a synthetic demo camera, handy for trying the page without a camera.
- Photo quality follows the OBS camera source's resolution: set it to 1920x1080 in the source's
  properties (Resolution/FPS Type: Custom) and halve the scene zoom to keep the same framing.
- **Frame rate** (Camera & outline) is 30 fps by default. Each frame costs OBS a little CPU (about
  3% of a 32-thread CPU at 30 fps); lower it if the stream ever stutters while the scanner is open.

## Rarity recognition (phase 3)

The scanner learns to tell the rarities apart from your training photos. It measures the parts
of the card that carry the rarity, not which card it is: the name text's foil colour (gold Ultra,
silver Secret, rainbow Starlight), the art's foil texture, sparkle in the text box (Starlights are
foil all over), the art box border (Overframe art crosses it), and how the shine changes as you
tilt: a Starlight's frame glitter twinkles and its name runs through the rainbow, while an
Ultra's printed frame stays put and its name stays gold. A new card is compared with your labeled
captures on those measurements (a blend of logistic regression, which weighs signals per rarity,
and nearest neighbours, which is steadier while a rarity has only a few captures).

Three **markers** are also learned on their own:
- **Name shines** (a hit): every hit has a foil name (gold Ultra, silver-foil Secret, rainbow
  Starlight); a non-hit's name is plain ink, even when its art is foil (Super Rare) or its name is
  flat silver (Rare). It looks for the name's letters catching the light in more than one colour
  (a Secret's silver runs through the rainbow; Rare silver and white Spell/Trap names are one flat
  colour), gold, and colour shifting with the tilt. Learned once there are 3+ *Not a hit* captures.
- **Art breaks the box** (Overframe): for each side of the art box (left, right, top) it measures
  art spilling into the frame strip and the art-box border line disappearing, compared with
  normal cards, and takes the strongest side, so art breaking out on *any* side counts.
- **Starlight shine**: frame glitter twinkle, the name shifting through the rainbow, sparkle.

Breakout + shine = **Starlight Overframe**, so it can be named even before you've captured one.
Each guess shows the readings, e.g. "art breaks the box (right, top) 84% · Starlight shine 97%".

Starlights: tilt them through a real angle (not just a wobble) so the rainbow shift and frame
twinkle show, and avoid holding them where glare turns the whole card white.

- It starts guessing once 2 rarities have 3+ captures each, and retrains after every capture.
- Every capture is guessed **before** it's saved; the message says whether it was right, and the
  **Recognition** panel keeps score per rarity (**Live guesses**, latest 50). **Held-out test**
  guesses each saved capture with a model trained on all the others.
- **Guess** (**G**) takes 10 photos and shows its guess without saving anything.
- The panel lists the measurements it relies on most.
- Mix card types within each rarity (Spells, Traps, Xyz, Link, Fusion...) so it learns the foil,
  not the frame colour; the same card in two rarities is the most useful capture of all.

## Auto mode: hits counted from the stream camera

With auto mode on, the scanner watches the camera and adds each hit you hold up to the counter:
the hit banner plays on the overlay and the counts update, no buttons pressed.

1. **Teach it what to ignore first.** Capture ordinary cards (commons, rares, supers) as
   **Not a hit** (key **6**): 10 to start, 30+ different cards over time, with plenty of Rares
   (flat silver names, the closest look-alike of a Secret's name) and Spell/Trap Supers (white names). Until it has some, every card held up reads as some kind of hit;
   the Auto mode panel warns you while that's the case.
2. Turn **Auto mode** on (button, or **A**). It starts off every time the scanner starts.
3. On stream, hold each hit up to the camera for about a second (tilting Starlights a little).
   Put it down, or swap straight to the next card, and the next one is read.

How it decides:
- A card is read once it has been in view for half a second (10 photos, about 1.5 s in all), and
  not again until it's put down or a different card replaces it.
- Only reads at least **85% sure** are added (change it in the panel). On your captures so far,
  checked card by card against a model that hadn't seen them: about 82% of hits are added on
  their own and about 4% of those are wrong; the rest are shown as "not sure" for one click.
- A hit shown to the camera again within 3 minutes is recognised as the same card and not added
  twice. Cards read as *Not a hit* are ignored.
- Hits it adds say **camera** in the control panel's hit log.

**Camera reads** lists the latest 24 reads with a picture and the verdict. Click the right
rarity on any read to correct it: a wrong hit is taken off the counter and the right one added
(**&mdash;** = not a hit, which just removes it), and the read is saved as a training capture,
so every correction makes it better. **&#x2715;** hides a read and leaves the counter alone.
Mistakes can also be deleted from the counter's hit log as usual.

### Test mode

Want to check what auto mode would log before a real break? Turn on **Test mode** in the
scanner's Auto mode panel (it switches auto mode on too). Hold cards up as you would on stream:

- Each hit goes to a **Test hits** list (marked TEST) at the top of the control panel's hit log,
  with the rarity and the card name it read. They never count toward totals, the case tracker or
  the case log, and the overlay in OBS ignores them; only the control panel's overlay preview
  shows their hit banner (labelled TEST), so you can see exactly what viewers would see.
- Unsure cards still ask you; corrections fix the test entry. Test reads aren't saved to your
  card photos on their own (your answers still are).
- **Clear test hits** empties the list. Test mode ends when auto mode is turned off, and starts
  off every time the scanner starts.

### Card names

Each card read also reads the **card name** off the name bar, and a sure name goes on the counter
with the hit (it shows in the overlay's hit banner and the hit log) and on the saved photos.

- Text recognition is Windows' own (built in, nothing sent anywhere); scan.bat installs the small
  Python bindings for it. The name it reads is matched to the full Yu-Gi-Oh card list from
  YGOPRODeck, downloaded once into `data/cards.json` and refreshed weekly (or with **Update card
  list**), so a misread letter or two still finds the right card.
- All ~10 photos of a read vote; foil glitter is smoothed out first. A name is used only when the
  photos agree; otherwise the hit is added without a name and the read shows the likely names: click
  one, or **✎ name** to type it. Names can also be changed in the counter's hit log (click the name).
- **Checked against the card list:** if a name is read surely and the card list says that card was
  never printed in the rarity read (say, a Super Rare-only card read as an Ultra), the card isn't
  added; it waits for your answer instead. The list doesn't have Overframes yet, so those aren't checked.
- **Set you're opening** (optional, e.g. `CORI`): cards from that set win when two names look alike.
- On your captures: names read surely on 32/34 Secrets, 22/30 Starlights, 21/30 Ultras. Rainbow
  Starlight names are the hardest. English cards only (the OCR is English).

### It learns while you stream

Only while auto mode is on:
- **Sure hits are saved to your card photos** under the rarity it read (marked *from stream, not
  checked*), and it retrains in the background, at most every 30 seconds so the stream stays
  smooth. Your photo collection grows with every break, and new cards are learned as they come.
  The training table's **From stream** column counts them. (The slow held-out test is skipped while
  streaming and rerun when you turn auto mode off.)
- **Unsure cards ask you.** Any read below the "sure" setting (non-hits included) is not added;
  it goes to the **Needs your answer** panel at the top of the scanner (the tab title shows how
  many wait, e.g. "(2) Card Scanner"). Answer with a click or keys **1**-**6**: a hit goes on the
  counter, and the card is saved to your photos as *checked by you*. Unanswered cards are kept
  until you answer or skip them (they're lost if the scanner is closed).
- **Wrong sure read?** Click the right rarity under Camera reads: the counter is corrected and
  its saved photos move to the right rarity.
- **Same card, different rarity:** recognition measures the foil, not the artwork, so an Ultra and
  a Secret print of the same card are told apart by their shine, and "shown again" only skips a
  card read as the *same* rarity, so both prints count.

Confident non-hits aren't saved (a break has far more of those than you need); answer or capture
them by hand when you want more.

The foot pedal still counts packs; auto mode only adds hits.

## Control it from your phone

1. Start the server so other devices on your Wi-Fi can reach it:
   `start.bat --host 0.0.0.0` (or `python server.py --host 0.0.0.0`).
   The console prints a second address such as `http://192.168.1.23:7777/control`.
2. Open that address on your phone (same Wi-Fi as the PC).
3. **Windows Firewall:** the first time, Windows asks whether Python may communicate on
   networks. Allow **Private networks**. If you missed the prompt, run this once in an
   *administrator* terminal:

   ```
   netsh advfirewall firewall add rule name="Whatnot hit counter" dir=in action=allow protocol=TCP localport=7777 profile=private
   ```

   Also make sure your Wi-Fi is set to *Private* (Settings → Network & internet → Wi-Fi → your
   network).

The panel has no password: anyone on the same network can open it. Only use `--host 0.0.0.0`
on your own home network. OBS can keep using `http://127.0.0.1:7777/overlay`.

## Troubleshooting

- **The overlay is blank:** the server isn't running, or the URL or port is wrong. The overlay
  stays invisible until it gets its first data, then reconnects on its own if the server
  restarts. Open the overlay URL with `?bg=dark` in a normal browser to check it.
- **"Could not listen on 127.0.0.1:7777":** the server is already running in another window,
  or something else uses the port. Close the other window, or use `--port 8000` (and change the
  OBS URL to match).
- **A tap sometimes takes a few seconds to show up:** on some Windows PCs, security software
  briefly holds up local network connections. It isn't the counter: a bare test server shows
  the same pauses. The hit still lands, and the panel shows "+1 …" once it's saved, so wait for
  that instead of tapping again. If it happens often, allow `python.exe` in your antivirus or
  firewall software.
- **Start over completely:** stop the server and delete `data/state.json`. If the file is ever
  damaged, the server renames it to `state.json.corrupt-<time>` and starts fresh, so nothing
  is overwritten.

## Files

```
start.bat            double-click to start
server.py            the server (Python standard library only)
web/control.html     the control panel
web/overlay.html     the OBS overlay
data/state.json      your counts (created on first run)
tests/test_server.py automated tests
CONTRACT.md          API and data format reference
```

Run the tests with `python -m unittest discover tests`.

## License

MIT (see `LICENSE`). The overlay embeds the Outfit font, which is under the SIL Open Font License 1.1
(`web/OFL-Outfit.txt`). Card names and printings come from the free YGOPRODeck API at run time; no card
data, images or training photos are included in this repository.
