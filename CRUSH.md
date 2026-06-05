# Grimoire — Project context

What follows is hard-won knowledge from building the grimoire closed loop
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

## Xovi extension (`xovi-ext/grimoire-injector/`)

Native C++ extension that runs inside xochitl's process via LD_PRELOAD.

### What it does
- Registers `GrimoireInjector` QML singleton at `dev.grimoire.injector 0.1`
- Spawns a pthread that polls `/tmp/grimoire_strokes.json` every 1s
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

```sh
cd xovi-ext/grimoire-injector

# First time: creates persistent Docker builder container
# Subsequent runs: incremental rebuild (~30s vs ~2min full build)
bash build.sh

# Then restart xochitl on device to load new .so:
ssh remarkable 'systemctl reset-failed xochitl && systemctl restart xochitl'
```

The `build.sh` script:
1. Creates/reuses a persistent `grimoire-builder` Docker container with the
   `eeems/remarkable-toolchain:latest-rm2` image
2. Runs `qmake6 && make` inside the container (incremental compilation)
3. Copies the `.so` out and SCPs it to the device

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

Generate with: `python grimoire.py --json output.json "text"`

### Remaining xovi work
- Fix vtable acquisition (hook QImage constructor like framebuffer-spy, or
  find existing SceneLineItems in memory)
- Find QQmlEngine to access Clipboard context property
- Trigger paste programmatically after setting Clipboard.items
- Alternative: direct scene graph manipulation (deeper RE needed)

## Script usage (`grimoire.py`)

```sh
# File-based injection (creates new layer in .rm file)
python grimoire.py input.rm output.rm "reply text"

# JSON export for xovi injector
python grimoire.py --json /tmp/grimoire_strokes.json "reply text"

# Preview (renders SVG for visual tuning)
python grimoire.py --preview "text"
```
