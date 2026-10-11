# Whatnot Hit Counter — shared contract

Live counter for Yu-Gi-Oh booster box breaks on Whatnot. The streamer clicks buttons in a
**control panel**; viewers see the counts in an **overlay** added to OBS as a Browser Source
(Whatnot streams from OBS via RTMP).

Hits tracked (fixed ids, in this display order):

| id                    | label                 | short | color     |
|-----------------------|-----------------------|-------|-----------|
| `ultra`               | Ultra Rare            | UR    | `#f5c542` |
| `secret`              | Secret Rare           | SCR   | `#c0c8d8` |
| `overframe`           | Overframe             | OF    | `#ff6b35` |
| `starlight`           | Starlight Rare        | STR   | `#7fd4ff` |
| `starlight_overframe` | Starlight Overframe   | SOF   | `#d48cff` |

## Runtime

- **Python 3.14 standard library only.** No pip installs, no Node, no CDN at runtime
  (fonts/scripts must be local or system fonts — the stream must work offline).
- Start: `python server.py [--host 127.0.0.1] [--port 7777]`. Default host `127.0.0.1`;
  `--host 0.0.0.0` lets a phone on the LAN open the control panel.
- `http.server.ThreadingHTTPServer`. State persisted to `data/state.json` (atomic write:
  temp file + `os.replace`), loaded on start. A lock guards all mutations.

## Files (owner in brackets)

```
server.py            [server]   HTTP server, state, persistence, SSE
web/overlay.html     [overlay]  OBS browser source (self-contained: inline CSS/JS)
web/control.html     [control]  streamer control panel (self-contained: inline CSS/JS)
tests/test_server.py [qa]
README.md, start.bat [qa]
```

Do not edit another station's files. If you need a contract change, write it under
"Open questions" at the bottom of this file and work to the contract as written.

## Routes

| Method | Path            | Result |
|--------|-----------------|--------|
| GET    | `/`             | 302 → `/control` |
| GET    | `/control`      | `web/control.html` |
| GET    | `/overlay`      | `web/overlay.html` (query string passed through untouched) |
| GET    | `/api/state`    | state JSON |
| GET    | `/api/events`   | Server-Sent Events (see below) |
| POST   | `/api/hit`      | body `{"rarity": id, "card": "optional name", "auto": optional bool, "test": optional bool}` → append log entry (`"auto": true` is kept on entries the camera scanner logged; `"test": true` appends to `testLog` instead: listed, never counted, sent as `event: testHit`) |
| POST   | `/api/rename`   | body `{"id": entryId, "card": "name" or null}` → change a logged (or test) hit's card name; unknown id → 400 |
| POST   | `/api/test/clear` | body `{}` → empty `testLog` |
| POST   | `/api/remove`   | body `{"rarity": id}` → delete the most recent entry of that rarity **in the current box** (no-op if none) |
| POST   | `/api/delete`   | body `{"id": entryId}` → delete that log entry |
| POST   | `/api/undo`     | `{}` → delete the most recent log entry overall (no-op if empty) |
| POST   | `/api/packs`    | body `{"delta": int}` or `{"set": int}` → packs opened in current box, clamped to `>= 0` |
| POST   | `/api/next-box` | `{}` → same as `/api/box {"delta": 1}` |
| POST   | `/api/box`      | body any of `{"delta": int}` or `{"set": int>=1}` (not both), `"span": 1..100`, `"of": int>=1 or null` (see Boxes on the table) |
| POST   | `/api/reset`    | `{}` → clear log, box number 1, packs 0 (title/settings/span/of kept) |
| POST   | `/api/settings` | body any of `{"title": str, "packsPerBox": int>=1, "labels": {id: str}, "display": {"packs": bool, "boxes": bool, ...}, "panelOpacity": 0..100}` |

- POST bodies are JSON. Every successful POST returns **200 with the full state JSON** and
  broadcasts it over SSE. Bad input (unknown rarity, wrong type, bad JSON) → **400**
  `{"error": "message"}`. Unknown path → 404 `{"error": ...}`.
- `card` is trimmed and capped at 80 chars; empty → `null`. `title` capped at 80 chars.
- JSON responses send `Cache-Control: no-store`.

## State JSON (`GET /api/state`, every POST response, every SSE `state` event)

```json
{
  "title": "Yu-Gi-Oh Box Break",
  "rarities": [
    {"id": "ultra", "label": "Ultra Rare", "short": "UR", "color": "#f5c542"}
  ],
  "box":     {"number": 1, "to": 4, "span": 4, "of": 12, "packsOpened": 0, "packsPerBox": 24, "packsTotal": 96,
              "counts": {"ultra": 0, "secret": 0, "overframe": 0, "starlight": 0, "starlight_overframe": 0},
              "total": 0},
  "session": {"counts": {"ultra": 0, "secret": 0, "overframe": 0, "starlight": 0, "starlight_overframe": 0},
              "total": 0, "boxes": 1},
  "display": {"packs": true, "boxes": true, "packsLeft": true, "tracker": true, "caseLog": true, "showcase": false},
  "panelOpacity": 85,   // overlay panel backgrounds, 0 (text only) .. 100 (solid); POST /api/settings {"panelOpacity": n}
  "log": [
    {"id": "a1b2c3", "rarity": "secret", "card": "Snake-Eye Ash", "box": 1, "boxTo": 4, "ts": 1760000000.123}
  ],
  "updatedAt": 1760000000.123
}
```

- `rarities` always has all five, in the order above; `label` reflects `/api/settings` overrides.
- `box.counts` / `box.total` are derived from log entries whose `box == box.number`.
  `session.counts` / `session.total` are derived from the whole log. `session.boxes` = `box.to`.

### Boxes on the table

- The streamer opens `span` boxes at a time: boxes `number`..`to` are on the table, where
  `to = min(number + span - 1, of)`. `of` = total boxes in the break (e.g. 12 for a case), `null` =
  no total. Defaults: `number 1, span 1, of null` (one box at a time, like before).
- `packsTotal = packsPerBox * (to - number + 1)`; `packsOpened` counts across the whole table and
  is clamped to `0..packsTotal`. `/api/packs` `delta > 0` at `packsTotal` moves to the next table
  (`number = to + 1`, packs 0), unless `to >= of`, where it stays full.
- Log entries carry `box` (first box on the table when pulled) and `boxTo` (last box).
  `box.counts` = entries with `box == box.number`.
- `/api/box`: `delta` moves by whole groups (`+1` = next table, `-1` = previous), never below box 1;
  `delta` past `of` raises `of` to the new table's last box. `set` jumps to a first box; `set`
  or `of` that leaves the first box past the total -> 400. Changing boxes: forward -> packs 0;
  back -> packs = that table's `packsTotal`. `span` alone keeps the first box and packs.
- `display.packs` / `display.boxes` are live show/hide switches for the overlay's pack and box
  counters (the overlay's `packs=0` / `boxes=0` URL params hide them regardless).
- `log` is oldest → newest, capped at 1000 entries (drop oldest). `id` is a short unique string.
- `ts` and `updatedAt` are Unix seconds (float).

### Foot pedal

- `pedal: {"plus": "F13" | null, "minus": "Ctrl+Alt+M" | null, "status": {"plus": "active" |
  "off" | "paused" | "busy" | "invalid" | "unavailable", ...}, "paused": bool, "available": bool}`.
- Set keys with `/api/settings {"pedal": {"plus": "F13"}}` (null clears; a bare letter/digit -> 400;
  the same key for both -> 400). On Windows the server registers them as global hotkeys.
- `POST /api/pedal {"paused": bool}` pauses the hotkeys while the panel learns a key (auto-resumes
  after 30 s).
- A press = `/api/packs {"delta": +1 | -1}`, then SSE `event: pedal` with
  `{"action": "plus" | "minus", "delta": 1 | -1, "moved": bool}` after the `state` event.

## Server-Sent Events (`GET /api/events`)

- Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`.
- On connect: immediately send `event: state` with the full state.
- After every successful mutation: `event: state` with the full state.
- State also has `testLog`: hits from the scanner's test mode (same entry shape), never counted.
- After a test hit (`"test": true`) the extra event is `event: testHit` instead of `hit`; the overlay shows it only with `?test=1` (the control panel's preview).
- After `/api/hit` additionally: `event: hit` with `data: {"entry": <log entry>, "rarity": <rarity object>}`
  — sent **after** the `state` event. Clients use it to play the hit animation.
- Heartbeat comment `: ping` every 15 s so dead clients are detected and OBS keeps the stream open.
- Each `data:` payload is a single line of JSON.
- Clients use `EventSource` (auto-reconnects) and also fetch `/api/state` on load.

## Overlay query parameters (`/overlay?...`)

| param    | values                         | default |
|----------|--------------------------------|---------|
| `layout` | `row` \| `column`              | `row` |
| `show`   | `box` \| `session` \| `both`   | `box` |
| `hide`   | comma list of rarity ids       | none |
| `scale`  | number (e.g. `1.5`)            | `1` |
| `title`  | `1` \| `0`                     | `1` |
| `packs`  | `1` \| `0`                     | `1` |
| `boxes`  | `1` \| `0`                     | `1` |
| `left`   | `1` \| `0`                     | `1` |
| `tracker`| `1` \| `0`                     | `1` |
| `caselog`| `1` \| `0`                     | `1` |
| `showcase`| `1` \| `0`                    | `1` (the "Biggest hits" panel also needs `display.showcase`, off by default) |
| `panel`  | `0`..`100`                     | follows `panelOpacity` (fixes this OBS source's panel opacity) |
| `alerts` | `1` \| `0` (hit pop-up banner) | `1` |
| `bg`     | `none` \| `dark`               | `none` (fully transparent) |

## Open questions

(stations append here)

- **[server] Additions & interpretations (no contract change needed, FYI for control/qa):**
  - Extra flag `--state PATH` (default `data/state.json`) so tests can use a temp state file.
    `server.Store(path)` + `server.HitCounterServer((host, port), store)` are importable.
  - `/api/packs` with both `delta` and `set` (or neither) → 400.
  - `/api/settings` with none of `title`/`packsPerBox`/`labels` → 400. Labels are trimmed, capped
    at 40 chars; an empty label (or the default text) restores the default. Title is trimmed.
  - `/api/delete` with an unknown id → 200 no-op (like `remove`/`undo` on nothing).
  - Empty POST body counts as `{}`. Known path with the wrong method (e.g. `GET /api/hit`) → 404.
  - A corrupt `state.json` is renamed to `state.json.corrupt-<ts>` and defaults are used.
  - Missing `web/*.html` → 404 HTML page explaining the file isn't built yet (no restart needed;
    pages are read from disk on every request).

- **[qa] Integration notes (no contract change):**
  - Tests use the server's `--state PATH` / `Store(path)` instead of a `--data` flag; no server change was needed.
  - Fixed in `web/control.html`: Settings dropped empty label fields, so clearing a label never
    restored the default (the server only restores on an explicit `""`). It now sends all five labels.
  - The intermittent multi-second stalls the overlay/control stations saw are this machine's
    loopback TCP (1/3/15 s SYN retransmits), reproduced with a bare socket server and no project
    code. Not a server bug; tests use 30 s client timeouts so a stall slows a run but doesn't fail it.
