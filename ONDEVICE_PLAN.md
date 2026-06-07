# Glossa On-Device Port — Plan

Goal: run the full pen-to-reply loop entirely on the reMarkable, with a
toolbar toggle to arm/disarm it. No Mac, no SSH, no Python.

## Current architecture (Mac-hosted)

```
Mac: glossa_loop.py
  ├─ SSH ControlMaster ──────────────┐
  ├─ poll /tmp/glossa_idle          │
  ├─ trigger + pull framebuffer       ├── reMarkable
  ├─ crop + blank-check (numpy/PIL)   │     ├─ xovi ext: idle watch, fb dump
  ├─ POST image → Hyper vision API    │     └─ uinjectd: stroke replay
  └─ render reply → JSON → uinjectd ──┘
```

The Mac does: orchestration, image processing, the HTTPS API call, and
text→stroke rendering. The device only watches the pen and replays strokes.

## Target architecture (device-hosted)

```
reMarkable:
  ├─ xovi ext (glossa-injector.so)
  │    ├─ PenIdleWatcher          (already exists)
  │    ├─ framebuffer dump        (already exists)
  │    └─ GlossaInjector QML    (already exists)
  │
  ├─ QML toolbar toggle           (NEW — qt-resource-rebuilder .qmd)
  │    └─ writes /tmp/glossa_armed  (0/1)
  │
  └─ glossad (NEW standalone daemon, C++/Qt)
       ├─ watches /tmp/glossa_armed
       ├─ when armed: watches /tmp/glossa_idle
       ├─ on idle: reads /tmp/glossa_fb.raw
       ├─ crop + grid-filter + blank-check (port from numpy)
       ├─ QNetworkAccessManager → Hyper API (HTTPS, base64 png)
       ├─ text→strokes (port glossa.py font rendering to C++)
       └─ injects strokes directly via /dev/input/event1 (uinjectd absorbed)
```

uinjectd is folded into glossad: the stroke-replay code (open
/dev/input/event1, emit evdev events, clean pen lifts) becomes an
in-process module. No socket, no keepalive, no connection-drop handling.

## Decisions (locked)

1. **Inference**: device calls Hyper API directly over wifi.
   Device has internet (~27ms ping). Uses Qt `QNetworkAccessManager`.
2. **Split**: xovi extension owns toggle UI + state file; standalone
   `glossad` daemon does capture/inference/inject. Injection is done
   in-process (uinjectd's evdev replay absorbed), not over a socket.
3. **Toggle**: real QML toolbar button via qt-resource-rebuilder `.qmd`.

## Build order (each piece independently testable)

### Phase 1 — daemon skeleton + arming + in-process injection
- `glossad` binary: watches `/tmp/glossa_armed`, logs state changes.
- Absorb uinjectd's evdev replay (open /dev/input/event1, draw/erase
  with clean pen lifts) as an in-process module.
- Test: `echo 1 > /tmp/glossa_armed`, then feed a strokes file and
  confirm it draws — no separate daemon, no socket.

### Phase 2 — capture + image pipeline (the numpy port)
- Port `_page_is_empty`, `_find_new_region`, grid-artifact filter to C++.
- Read `/tmp/glossa_fb.raw` (1404x1872 RGBA), crop toolbar/swirl zones.
- Test against known framebuffer dumps; compare to Python output.

### Phase 3 — HTTPS vision call
- `QNetworkAccessManager` POST to hyper.charmcli.dev.
- base64-encode cropped PNG, build OpenAI-format JSON, parse reply.
- API key from a device-local file (`/home/root/.glossa_key`).
- Test: feed a fixed image, check the model reply text.

### Phase 4 — text → strokes on device
- Port `glossa.py` font rendering (EMSAllure SVG single-stroke) to C++.
- Reuse the densify + jitter logic. Output the uinjectd JSON format.
- Test: render "hello" and inject it.

### Phase 5 — QML toolbar toggle
- Write a `.qmd` patch adding a button to the editing toolbar.
- Button flips a property that writes `/tmp/glossa_armed`.
- Model after `toolbar_pages_button_hack.qmd`.
- Hardest/most fragile: hashes are firmware-specific (VERSION 3.24.0.149).
- Test: button appears, tap toggles the state file.

### Phase 6 — glue + thinking indicator
- Wire the full loop in glossad, port the corner ornaments + swirl.
- Conversation history in-memory. Reset on page change.

## Risks / unknowns

- **QML hashes**: the `.qmd` symbol hashes are tied to firmware version.
  Need to find how rebuilder resolves them (hashtab file) to author new
  patches. May need to extract symbols from a known-good hack.
- **TLS on device**: Qt network stack must trust the Hyper cert. Verify
  CA bundle presence; may need to point at /etc/ssl/certs.
- **Font rendering port**: the Python uses fontTools-style SVG parsing.
  Porting to C++ is the biggest pure-logic chunk.
- **Memory**: ~780MB free. base64 of a full-page PNG is a few MB, fine.
- **API key storage**: keep it in a root-only file, not in the binary.

## Reuse inventory

Already on device / in repo:
- uinjectd: stroke replay + socket protocol (keep as-is)
- xovi ext: idle watch, framebuffer dump (keep, maybe add hooks)
- qt-resource-rebuilder: QML patching engine + example hacks
- glossa.py: font tables, render params, densify (port reference)
- glossa_loop.py: pipeline logic, grid filter, API format (port reference)
