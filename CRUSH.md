# Glossa — Project context

What follows is hard-won knowledge from building the glossa closed loop
on reMarkable 2. Do not unlearn any of this.

## Coordinates

- **Origin is top-center**, not top-left. This is the single most important
  fact about the v6 format and it cost us an hour.
- `x = 0` is the horizontal center of the page; negative values go left,
  positive go right. Range is roughly -702 to 701 on a standard page.
- `y = 0` is the top of the page, increasing downward. Range is 0 to 1871.

## Stroke parameters (matching real ink)

- Tool: `Pen.BALLPOINT_2` (value 15)
- Color: `PenColor.BLACK` (value 0)
- `thickness_scale`: 2.0, `starting_length`: 0.0
- Point `width`: 10–13, Point `pressure`: 160–190
- Point `speed`: 4–12, Point `direction`: 80–110
- Natural variation: y_jitter (-1.5 to 0.5), slant (-0.015 to 0.015),
  wobble amplitude 0.8

## Font rendering

- EMS Allure SVG single-stroke font (from GitLab `oskay/svg-fonts`).
- Straight-line paths only (M/L commands), no beziers.
- Per-glyph advance widths from `horiz-adv-x` attribute (not fixed width).
- Y-axis flip: SVG uses Y-up, `.rm` v6 uses Y-down. Apply `sy = -gy * scale`.
- Render params: `DEFAULT_SCALE = 0.07`, `DEFAULT_X = -550.0`,
  `DEFAULT_Y = 850.0`, `LINE_HEIGHT = 1.4`, `MAX_LINE_WIDTH = 1100`.
- Word wrapping implemented. Preview mode: `--preview "text"` renders SVG.
- Font files: `fonts/EMSAllure.svg` (primary), `EMSCasualHand.svg`, `EMSInvite.svg`.

## File format injection

- **Never re-serialize original blocks.** `read_blocks()` → `write_blocks()`
  loses data. Serialize only new blocks, strip 43-byte header, append to raw
  original bytes.
- Reuse author 1 (always present). CRDT sequence IDs starting at 200+.

### Layer-based injection (working)

Real reMarkable layers require specific block structure:
1. `SceneTreeBlock(tree_id=LAYER_ID, node_id=CrdtId(0,0), is_update=True, parent=ROOT)` — content tree root
2. `TreeNodeBlock(node_id=LAYER_ID, label="name", visible=True)` — metadata
3. `SceneGroupItemBlock(parent=ROOT, value=LAYER_ID, left_id=LAST_CHILD)` — links into sibling list
4. `SceneLineItemBlock(parent_id=LAYER_ID, ...)` — strokes parented to tree_id

Key: `left_id` in SceneGroupItemBlock must chain to the previous root child
for correct ordering. Use `_find_last_root_child()` to scan existing blocks.

## Device management

- SSH: `root@10.11.99.1` (config alias `remarkable`)
- `.content` file `sizeInBytes` must match `.rm` file size or xochitl crashes.
- `systemctl reset-failed xochitl && systemctl restart xochitl` to avoid rate limiting.
- Busybox limitations: `head -n N` syntax, no `base64`, limited `find`.
- **Page navigation reload**: writing a modified `.rm` to disk + fixing
  `sizeInBytes` is picked up when user navigates away and back. No restart
  needed. ~2s vs ~12s for full restart.

## Test notebook

- UUID: `5c81a1df-c36c-4eea-9289-978dfcc655a1`
- Page: `5b66f39c-9d00-40a7-bce0-9fb3a044d446.rm`
- Path: `/home/root/.local/share/remarkable/xochitl/5c81a1df-c36c-4eea-9289-978dfcc655a1/`

## Xovi extension (`xovi-ext/glossa-injector/`)

Native C++ extension that runs inside xochitl's process via LD_PRELOAD.

### What it does
- Registers `GlossaInjector` QML singleton at `dev.glossa.injector 0.1`
- Spawns a pthread that polls `/tmp/glossa_strokes.json` every 1s
- On file change, uses `QMetaObject::invokeMethod(Qt::QueuedConnection)` to
  call back into the main thread for stroke loading
- Creates `SceneLineItem` objects using reverse-engineered structs from
  clipboard-injector (rm_SceneItem, rm_SceneLineItem, rm_Line)

### Key learnings
- **QTimer does NOT work** in xovi QML singletons. The object created by
  `qmlRegisterSingletonInstance` doesn't get timer events processed. Use
  pthread + `QMetaObject::invokeMethod` instead.
- **`QCoreApplication::instance()` returns null** during `_xovi_construct`.
  Don't check app type in constructor. Filter workers in entry.c using
  `program_invocation_short_name`.
- **Xovi activation**: requires triple-tap (xovi-tripletap) or manual
  `xovi/start` over SSH. Uses tmpfs systemd drop-in, lost on reboot.
  `systemctl restart xochitl` alone preserves the tmpfs if already mounted.
- **Extension .so is loaded once** at xochitl startup. Updating the .so on
  disk requires restarting xochitl to pick up changes.
- **Vtable cloning**: SceneLineItems need a valid vtable pointer cloned from
  an existing xochitl-created item. Currently returns nil — needs fixing.
- **QQmlEngine access**: Can't find engine via `app->children()` iteration.
  Need alternative approach for clipboard/scene injection.

### Build & deploy workflow

The unified build script at `xovi-ext/build.sh` compiles **both** the
extension `.so` and the `uinject` binary, then deploys both to the device.

```sh
cd xovi-ext

# First time: creates persistent Docker builder container
# Subsequent runs: incremental rebuild (~30s vs ~2min full build)
bash build.sh

# Then restart xochitl on device to load new .so:
ssh remarkable 'systemctl reset-failed xochitl && systemctl restart xochitl'
```

The `build.sh` script:
1. Creates/reuses a persistent `glossa-builder` Docker container with the
   `eeems/remarkable-toolchain:latest-rm2` image. Mounts the **whole**
   `xovi-ext` tree at `/xovi-ext` (not just `glossa-injector`) so any
   sub-project can compile.
2. Builds the extension: `qmake6 && make` inside the container
   (incremental compilation).
3. Builds `uinject`: `$CC -o uinject uinject.c -O2 $CFLAGS $LDFLAGS` after
   sourcing the SDK environment-setup. **Must use `$CC` + `$CFLAGS`**, not a
   bare `arm-remarkable-linux-gnueabi-gcc`, or the sysroot include paths
   (stdio.h etc.) won't resolve.
4. Copies both artifacts out and SCPs them to the device.

**Deploy gotcha**: SCP to `/home/root/uinject` fails with "dest open ...
Failure" if a uinject process is still running or the file is busy. Always
`ssh remarkable 'killall uinject 2>/dev/null; rm -f /home/root/uinject'`
before the SCP. The build script does this, but if deploying manually,
remember the kill+rm dance.

**The .so is loaded once at xochitl startup.** Updating it on disk requires
restarting xochitl. The `uinject` binary, by contrast, is exec'd fresh each
call, so a new uinject takes effect immediately — no restart needed.

### xovi activation survives restart but not reboot

The xovi tmpfs systemd drop-in (LD_PRELOAD injection) is lost on reboot.
After a reboot, re-run `bash /home/root/xovi/start` (or triple-tap) to
remount it. A plain `systemctl restart xochitl` preserves the tmpfs if it's
already mounted.

### uinject CLI

```sh
# Inject pen strokes (speed = ms delay between points, lower = faster)
uinject --speed 3 strokes.json

# Erase by retracing stroke paths with the eraser tool (6 offset passes)
uinject --speed 2 --erase-strokes strokes.json

# Erase a rectangular region (rm v6 coords)
uinject --erase x,y,w,h
```

Key uinject learnings:
- **Pen input arrives as mouse + touch events, NOT Qt tablet events.** The
  reMarkable digitizer surfaces through `/dev/input/event1` as a Wacom
  device. uinject writes raw evdev events there directly.
- **Stroke injection is real-time replay** — uinject sleeps `--speed` ms
  between every point, so a long reply with thousands of points takes many
  seconds. Keep replies short and densify sparingly (see `_densify`
  `min_seg_len=300`). Render is instant; the bottleneck is event replay.
- **There is no erase primitive.** Erasing means retracing the ink path with
  `BTN_TOOL_RUBBER`. The eraser tip is narrower than expected, so
  `--erase-strokes` makes 6 offset passes (center + 4 cardinal ±10px + 1
  diagonal) to fully cover stroke width.
- **JSON parser must handle nested brackets.** The points array is
  `[[x,y,...],[x,y,...]]`. A naive "stop at first `]`" only erases the first
  point of each stroke. Track bracket depth.

### JSON stroke format (matches clipboard-injector)

```json
[{
  "points": [[x, y, speed, width, direction, pressure], ...],
  "rgba": 4278190080,
  "color": 0,
  "bounds": [minX, minY, width, height],
  "tool": 15,
  "maskScale": 2.0,
  "thickness": 2.0
}]
```

Generate with: `python glossa.py --json output.json "text"`

## Closed-loop daemon (`glossa_loop.py`)

The full pen-to-reply loop. Watches for pen idle, captures the screen,
sends it to a vision model, and writes the reply back as ink.

```sh
python glossa_loop.py [--model MODEL] [--once]
```

### Pipeline

1. **Idle detection** (xovi extension, `PenIdleWatcher`): an event filter on
   QGuiApplication watches mouse/touch press+release. A debounce thread
   writes `/tmp/glossa_idle` after the pen has been up for `DEBOUNCE_MS`
   (currently 2500ms). The Python daemon polls that file over persistent SSH.
2. **Capture**: trigger `/tmp/glossa_screenshot`, the extension dumps the
   framebuffer to `/tmp/glossa_fb.raw`, pull it via SCP.
3. **Crop**: drop top 80px (toolbar) and bottom 200px (swirl zone).
4. **Blank check**: skip the API entirely if no real content (see grid
   artifact note below).
5. **Vision model**: send the full page image; the model does OCR +
   relevance filtering + reply in one call. Returns `[NO_NEW_TEXT]` when
   there's nothing new for it.
6. **Position + inject**: find content bottom, render reply below it, SCP the
   JSON, run uinject.

### Persistent SSH

`SSHSession` uses ControlMaster multiplexing (one connection, reused for all
commands) to kill the ~1s handshake per call. Cleans up stale sockets on
open and uses `ConnectTimeout=10` so it fails fast instead of hanging.

### The reMarkable display grid artifact (IMPORTANT)

The captured framebuffer contains a **uniform grid pattern**: isolated
2px-tall rows every ~42px, each with **exactly 68 dark pixels**, pure black
(value 0), spanning the entire screen height. This is a display/refresh
artifact, NOT content. It is indistinguishable from ink by pixel value.

To find real handwriting, filter it out by:
- Counting dark pixels per row (`< 128` threshold).
- Ignoring rows with **exactly 68** dark pixels (the grid signature).
- Real text rows have variable counts (anything but 68) and form contiguous
  clusters many rows tall; the grid is always isolated 2-row pairs.

This filter is used both for the blank-page check and for finding the
content bottom to position replies.

### Vision model API

Currently uses Charm's Hyper gateway:
- Endpoint: `https://hyper.charmcli.dev/v1/chat/completions`
- Auth: `HYPER_API_KEY` from `.env`
- Model: `kimi-k2.6-vercel`
- OpenAI-compatible format. **Roles must be `user`/`assistant`**, not
  `model` (that's Gemini's format and Hyper rejects it with HTTP 400).
- Images sent as `image_url` with `data:image/png;base64,...` URLs.

Conversation history is kept as `(role, text)` tuples and replayed each
turn for context. It resets on a real page change but NOT when our own
injection changes the framebuffer hash (detected via `last_inject_y`).

### Thinking indicator

While capture + inference run, a background thread draws a small curly
swirl in the bottom-left corner (rm coords ~x=-580, y=1750), then erases and
redraws it in a loop (1s hold after draw, 3s pause after erase). On
completion it does a final erase. The animation thread is stopped on every
exit path (success, skip, blank, error) via an `Event`, and the main thread
joins with a generous 15s timeout so the final erase finishes cleanly —
**a short join timeout abandons the thread mid-erase and leaves ink behind.**

## Hard-won iteration loop

When working on glossa, this is the tight loop that works:

1. **Python changes** take effect on the next `glossa_loop.py` run. No
   deploy needed.
2. **uinject changes**: `cd xovi-ext && bash build.sh` then the kill+rm+scp
   dance. Effective immediately (exec'd per call).
3. **Extension (.so) changes**: `bash build.sh`, then restart xochitl.
   Slowest loop — avoid unless touching idle detection or framebuffer hooks.
4. **Debugging the extension**: stderr is swallowed by xochitl. Write to a
   file instead (`/tmp/glossa_ext.log`) via a `glossa_log()` helper.
   Read it with `ssh remarkable 'cat /tmp/glossa_ext.log'`.
5. **Inspecting captures**: pull `/tmp/glossa_fb.raw`, load as
   `Image.frombytes("RGBA", (1404, 1872), raw)`, annotate with PIL, save a
   PNG, and View it. This is how the grid artifact was found — always look
   at the actual pixels before trusting a heuristic.

## Script usage (`glossa.py`)

```sh
# File-based injection (creates new layer in .rm file)
python glossa.py input.rm output.rm "reply text"

# JSON export for xovi injector (--y sets start Y in device coords)
python glossa.py --json /tmp/glossa_strokes.json --y 400 "reply text"

# Preview (renders SVG for visual tuning)
python glossa.py --preview "text"
```

`glossa.py` is also importable as a library:
`text_to_strokes(text, x, y)` returns `si.Line` objects;
`strokes_to_json(strokes, path)` writes the uinject JSON. The loop uses it
this way for the thinking indicator instead of shelling out.
