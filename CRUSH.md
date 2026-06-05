# Grimoire — Project context

What follows is hard-won knowledge from building M0 (injecting native ink
into reMarkable .rm v6 pages). Do not unlearn any of this.

## Coordinates

- **Origin is top-center**, not top-left. This is the single most important
  fact about the v6 format and it cost us an hour.
- `x = 0` is the horizontal center of the page; negative values go left,
  positive go right. Range is roughly -702 to 701 on a standard page.
- `y = 0` is the top of the page, increasing downward. Range is 0 to 1871.
- Real handwriting in existing notebooks shows x in the -300 to 300 range
  and y in the 700 to 1050 range (roughly the middle third of the page).
- Source: reMarkable-kaitai `rmv6.ksy` spec; confirmed by inspecting actual
  device files and rendering them.

## Stroke parameters (matching real ink)

- Tool: `Pen.BALLPOINT_2` (value 15)
- Color: `PenColor.BLACK` (value 0)
- `thickness_scale`: 2.0
- `starting_length`: 0.0
- Point `width`: 9
- Point `pressure`: 150 + small variance (0-30)
- Point `speed`: 5 + small variance (0-10)
- Point `direction`: 90 + small variance
- These values were tuned by comparing against strokes from a real notebook
  ("9.8" on Kieran's device). Real pen widths range 12-18, pressures
  0-255 averaging ~173.

## File format injection (the append strategy)

- **Never re-serialize original blocks.** `read_blocks()` followed by
  `write_blocks()` loses data. rmscene warns "Some data has not been read.
  The data may have been written using a newer format than this reader
  supports." Respect this warning — re-serialized files crash xochitl.
- Instead: serialize only your new `SceneLineItemBlock`s via
  `write_blocks()`, strip the 43-byte header it emits, and append the
  remaining bytes to the original file.
- Layer 1 is always `CrdtId(0, 11)`. Inject `SceneLineItemBlock`s directly
  into it as children. Do not create new SceneTreeBlocks or TreeNodeBlocks
  — xochitl doesn't need them and they caused crashes in early experiments.
- Reuse author 1 (always present). No need to modify `AuthorIdsBlock`.
- CRDT sequence IDs starting at 200 for our blocks — safely above anything
  in real files (which use low single digits).

## Device management

- `.content` file has `"sizeInBytes"` that must match the `.rm` file size
  or xochitl crashes on load. Fix with sed before restart:
  ```
  sed -i 's/"sizeInBytes": "[0-9]*"/"sizeInBytes": "NNNN"/' doc.content
  ```
- Too many `systemctl restart xochitl` calls trigger rate limiting. Use:
  ```
  systemctl reset-failed xochitl && systemctl restart xochitl
  ```
- Busybox on the device is limited: no `base64`, `head` needs `-n N` syntax,
  no `bash`, limited `find`. Use `od`/`hexdump` for binary inspection.
- The SSH MCP gets new session IDs on each connect. Old sessions die when
  the device reboots or the connection drops. Always use the latest.

## M0 script (`grimoire.py`)

- Single-file Python script, no dependencies beyond `rmscene` (installed in
  `./.venv`).
- Hershey simplex font subset: 21 glyphs (A-Z uppercase plus some
  punctuation).
- Hershey glyph coordinates are in a 0..21 x 0..32 grid per glyph,
  `GLYPH_WIDTH=21`, `GLYPH_SPACING=8`.
- Default text: "GRIMOIRE SAYS HELLO" at scale 1.1, origin (-300, 700).
- Usage:
  ```
  python grimoire.py input.rm output.rm
  ```

## Deploy workflow (from mac to device)

```sh
# 1. Generate reply
.venv/bin/python3.13 grimoire.py pages/original.rm pages/replied.rm

# 2. Push to device (adjust paths — get UUID from notebook metadata)
scp pages/replied.rm root@10.11.99.1:/path/to/page.rm

# 3. Fix metadata and restart
ssh root@10.11.99.1 \
  "sed -i 's/\"sizeInBytes\": \"[0-9]*\"/\"sizeInBytes\": \"3959\"/' /path/to/doc.content \
   && systemctl reset-failed xochitl \
   && systemctl restart xochitl"
```

## Notebook UUID for testing

Kieran's Grrimoire test notebook:
- UUID: `5c81a1df-c36c-4eea-9289-978dfcc655a1`
- Page: `5b66f39c-9d00-40a7-bce0-9fb3a044d446.rm`
- Path: `/home/root/.local/share/remarkable/xochitl/5c81a1df-c36c-4eea-9289-978dfcc655a1/`

## Open issues from M0

- xochitl still occasionally crashes on reload despite correct `sizeInBytes`.
  Need to investigate journalctl logs next time it happens.
- Hershey font subset is missing lowercase and numbers. M1 should add them.
- No whitespace awareness — text always renders at fixed position (M1).
- No trigger mechanism — just static injection (M2+).
