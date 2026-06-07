# Glossa — A Handwritten LLM Loop for the reMarkable 2

> Write a question by hand on a native notebook page; the device "writes back" an
> answer as ink on the same page, leaving your original strokes untouched.

**Status:** draft spec
**Target device:** reMarkable 2 (i.MX7, Cortex‑A7, 32‑bit ARM / armhf — confirm for your firmware)
**Approach:** hook `xochitl`, work with native notebooks, inject reply strokes as real `.rm` v6 ink.

---

## 1. Goal

A closed loop on the device:

1. User writes a question on a normal notebook page.
2. A deliberate **trigger** fires (sigil glyph / corner tap / hooked toolbar action).
3. The new strokes are captured, recognized, and sent to an LLM.
4. The answer is rendered as **hand‑inked strokes** in whitespace on a dedicated
   layer, then composited back to the e‑paper.

The reply must be **native ink** (persisted in the notebook, survives sync), not a
framebuffer overlay, and must **leave the original writing alone**.

---

## 2. Prior art & source material

- HN thread that kicked this off — *Show HN: Edsger, a handwritten Clojure REPL for the reMarkable 2*: <https://news.ycombinator.com/item?id=48374552>
- Edsger write‑up (Daniel Janus): <https://handwritten.danieljanus.pl/2026-06-01-edsger.html>
- Author's hand‑written blog with embedded hyperlinks (technique reference): <https://handwritten.danieljanus.pl/>
- Curated ecosystem index: [awesome-reMarkable](https://github.com/rehackable/awesome-remarkable)

The key takeaway from the thread (per the author's own FAQ, echoed by `jcattle`/`xnorswap`):
Edsger's ~14 s latency is almost entirely **xochitl taking ~12 s to flush the
notebook to disk**. Polling the on‑disk file is the simplest approach and a dead
end for anything that should feel live. Hooking xochitl lets us bypass the flush
entirely by reading and writing the **in‑memory scene**.

---

## 3. The core constraint

| Path | Latency source | Verdict |
|---|---|---|
| Poll `.rm` on disk | ~12 s xochitl flush | prototype only |
| Read in‑memory scene via hook | none | target |
| Write reply by editing `.rm` + reload | reload cost | prototype only |
| Inject SceneItems in memory + repaint | none | target |

Everything below is designed to remove the disk round‑trip from the hot path.

---

## 4. Architecture

```
            ┌─────────────────────────── reMarkable 2 (xochitl, hooked via xovi) ──────────────┐
            │                                                                                  │
  pen ──►  stroke-finalized hook ──► trigger detector ──► capture new strokes (vectors)        │
            │                                                  │                               │
            │                                                  ▼                               │
            │                                          rasterize strokes → PNG                 │
            └──────────────────────────────────────────────────┼──────────────────────────────┘
                                                                │  (SSH / Tailscale)
                                                                ▼
                                              ┌──────── server (terebithia) ────────┐
                                              │  PaddleOCR (local)  → question text  │
                                              │  LLM via potluck /v1 → answer text   │
                                              │  Hershey render → reply strokes      │
                                              │  rmscene → SceneItems (v6)           │
                                              └───────────────────┬──────────────────┘
                                                                  │
            ┌─────────────────────────────────────────────────────┼──────────────────────────┐
            │  inject SceneItems into in-memory scene (reply layer) ──► repaint hook           │
            └──────────────────────────────────────────────────────────────────────────────────┘
```

Recognition + rendering live off‑device; only capture, inject, and repaint are
in‑process hooks. The LLM call routes through **potluck** so the device holds zero
provider keys.

---

## 5. Output path — writing native ink

The `.rm` v6 ("lines") format is **writable**, so replies can be true ink.

- **[rmscene](https://github.com/ricklupton/rmscene)** (ricklupton) — reads *and writes*
  v6 files. Exposes a `SceneTree` of `SceneItems` grouped into layers, and a writer
  that can emulate specific software versions for round‑trip safety. This is the
  primary library for generating reply strokes.
- **[rmc](https://github.com/ricklupton/rmc)** ([PyPI](https://pypi.org/project/rmc/)) —
  CLI built on rmscene; `rmc -t rm text.md -o text.rm` does text→`.rm`. Text‑box
  layout is still rough, so treat as a reference, not the final renderer.
- **[jacob414/rm-files](https://github.com/jacob414/rm-files)** — friendlier wrapper:
  `RemarkableNotebook`, named layers, pen/tool selection, filled polygons, scanline
  hatching. Assembles device‑like block sequences. Page geometry is **1404×1872 px**.
- **[bsdz/remarkable-layers](https://github.com/bsdz/remarkable-layers)**
  ([active fork](https://github.com/YakBarber/remarkable-layers)) — v5 only, but
  important: it places text using **Hershey stroke fonts** with pen styles. Hershey
  (single‑line) glyphs *are* pen strokes → they convert to `.rm` lines and read as
  hand‑inked, not typeset. Port the Hershey→strokes idea onto rmscene for v6.
- Format references: [reMarkable‑kaitai](https://github.com/matomatical/reMarkable-kaitai)
  (Kaitai Struct spec), and `ddvk/reader` (credited by rmc for decoding v6).

**Reply renderer pipeline:** answer text → Hershey/calligraphic single‑line font →
polyline points (+ synthetic pressure/width for the inked look) → rmscene
`SceneItems` on a dedicated **"replies" layer** → inject. Lay out into detected
whitespace so the original strokes are never modified.

---

## 6. Input path — reading what was written

Because we hook the scene model, the user's strokes are already **vectors** — no
framebuffer scrape needed.

- Read the new strokes directly from the in‑memory `SceneTree` on trigger.
- Rasterize only the new strokes to a small PNG locally (trivial polyline raster).

For prototyping the read half *before* the hook exists, stream pen strokes
off‑device using tools from [awesome-reMarkable](https://github.com/rehackable/awesome-remarkable):
`goMarkableStream`, `pipes and paper`, `reStream`.

---

## 7. Recognition

- **Local OCR:** [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) on the server.
  Fast, no network hop, recommended in the HN thread (`deivid`) for handwriting.
- **LLM:** send recognized text (or the rasterized image, for a vision model) to the
  LLM **via potluck's OpenAI‑compatible `/v1` passthrough**. Keeps keys off the device.

---

## 8. Reverse engineering xochitl

Substrate: **[xovi](https://github.com/asivery/xovi)** (asivery) — function‑hooking /
extension framework built for xochitl; [rm-appload](https://github.com/asivery/rm-appload)
sits on top of it. Installation made easy by
[reManager](https://github.com/rmitchellscott/reManager) + Vellum.

xochitl is a stripped C++/Qt binary — in Ghidra, match Qt vtables and string xrefs
rather than raw offsets (your GhidraMCP loop fits here). Pin one firmware version;
the well‑known fragility is that symbol addresses move across updates.

**Functions to locate, in priority order:**

1. **pen‑up / stroke‑finalized handler** — trigger point + cleanest place to read the
   just‑drawn strokes.
2. **document / scene model** — container of pages → layers → lines; used for both
   reading existing ink and injecting reply `SceneItems`.
3. **repaint / refresh** — flush injected strokes to e‑paper immediately.
4. **save handler** — optional; only to force persistence on demand.

### Lower‑level display drivers (fallback / reference only)

Not needed if we inject native strokes, but useful background and a fallback for an
instant preview overlay while real strokes are generated:

- [waved](https://github.com/matteodelabre/waved) — C++ direct display driver.
- [dazed](https://github.com/jakubvf/dazed) — Zig rewrite of waved, ships an **SDL
  emulator** of the display (develop without flashing).
- [swtcon](https://github.com/yobert/swtcon) — Rust reimplementation.
- [remarkable2-framebuffer](https://github.com/ddvk/remarkable2-framebuffer) — instant
  on‑screen state.
- [glider waveform notes](https://gitlab.com/zephray/glider) — why e‑paper needs
  multi‑frame waveforms.

---

## 9. Integration depths

- **Depth 1 — file + reload (no output hook):** read page `.rm`, splice reply layer
  with rmscene, write back, force document reload. Entirely server‑side over SSH.
  Proves the loop; inherits reload latency.
- **Depth 2 — in‑memory injection (target):** read strokes from the live scene, inject
  reply `SceneItems`, call repaint. No disk round‑trip; this is what makes it feel
  like magic.

Build Depth 1 first, then move only the hot path into the hook.

---

## 10. Trigger UX

Don't guess when the user is done — make it deliberate:

- a recognizable **sigil glyph** detected in the new strokes, or
- a **corner tap** zone, or
- a **hooked toolbar / menu action** (via rm-appload).

On trigger: capture new strokes → rasterize → OCR → LLM → Hershey render → inject
into whitespace on the replies layer → repaint.

---

## 11. Milestones

- **M0 — Loop proof (Depth 1, offline).** SSH in, pull a page `.rm`, render a
  hardcoded reply with a Hershey font via rmscene onto a new layer, write back,
  reload, confirm it shows as ink that **survives sync**. De‑risks everything.
- **M1 — Renderer.** Hershey glyph → `SceneItem` line with points + pressure on a
  named layer; whitespace layout; tune the inked aesthetic.
- **M2 — Recognition.** Rasterize strokes → PaddleOCR → text; wire LLM call through
  potluck.
- **M3 — xochitl hooks.** Locate the four functions; read live strokes + inject in
  memory + repaint (Depth 2).
- **M4 — Trigger + UX.** Pick the trigger mechanism; end‑to‑end live glossa.
- **M5 — Polish.** Latency budget, error states, multi‑page, persistence guarantees.

---

## 12. Risks & open questions

- **Firmware drift** breaks hooks — pin a version, key off Qt symbols/strings.
- **`.rm` writer fidelity** — confirm injected strokes round‑trip cleanly through
  sync and the official apps (rmscene emulates versions; verify yours).
- **Hershey→v6 port** — the existing Hershey path is v5; budget time to port.
- **Trigger false positives** — sigil detection vs. ordinary writing.
- **Repaint partial‑refresh** — getting a clean region update without ghosting.
- **Device fragility** — HN reports of weak USB‑C on rM2; newer "Pure" model is more
  repairable.

---

## 13. Reference index

**Project / thread**
- HN thread — <https://news.ycombinator.com/item?id=48374552>
- Edsger post — <https://handwritten.danieljanus.pl/2026-06-01-edsger.html>
- awesome-reMarkable — <https://github.com/rehackable/awesome-remarkable>

**`.rm` format & stroke generation**
- rmscene — <https://github.com/ricklupton/rmscene>
- rmc — <https://github.com/ricklupton/rmc> · <https://pypi.org/project/rmc/>
- rm-files — <https://github.com/jacob414/rm-files>
- remarkable-layers (Hershey) — <https://github.com/bsdz/remarkable-layers> · fork <https://github.com/YakBarber/remarkable-layers>
- reMarkable-kaitai — <https://github.com/matomatical/reMarkable-kaitai>

**Hooking & apps**
- xovi — <https://github.com/asivery/xovi>
- rm-appload — <https://github.com/asivery/rm-appload>
- reManager — <https://github.com/rmitchellscott/reManager>

**Display drivers (fallback)**
- waved — <https://github.com/matteodelabre/waved>
- dazed — <https://github.com/jakubvf/dazed>
- swtcon — <https://github.com/yobert/swtcon>
- remarkable2-framebuffer — <https://github.com/ddvk/remarkable2-framebuffer>
- glider waveform notes — <https://gitlab.com/zephray/glider>

**Recognition**
- PaddleOCR — <https://github.com/PaddlePaddle/PaddleOCR>