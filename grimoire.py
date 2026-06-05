#!/usr/bin/env python3
"""
Inject reply text as native ink into reMarkable .rm v6 pages.

Reads an existing .rm v6 page and appends Hershey-rendered text strokes
directly into Layer 1 as SceneLineItemBlocks. Original blocks are never
re-serialized — replies are appended to the raw bytes, so data that
rmscene can't fully parse survives untouched.

M0 proof for the Grimoire project. Depth 1 (file + reload).
See spec.md for the full architecture.

v6 coordinate system: origin is top-center of page.
  x = 0 -> center; negative -> left; positive -> right.
  y = 0 -> top; positive -> down.

Usage:
    python grimoire.py input.rm output.rm       # writes to output.rm
    python grimoire.py input.rm                 # overwrites input

Deploy to device:
    scp replied.rm root@remarkable:/path/to/page.rm
    ssh root@remarkable "sed -i 's/\"sizeInBytes\": \"[0-9]*\"/\"sizeInBytes\": \"3959\"/' /path/to/doc.content && systemctl reset-failed xochitl && systemctl restart xochitl"
"""

import sys
from io import BytesIO

from rmscene import (
    CrdtId,
    CrdtSequenceItem,
    read_blocks,
    write_blocks,
    scene_items as si,
)
from rmscene.scene_stream import (
    SceneLineItemBlock,
)


# -- Hershey simplex font (subset) ----------------------------------------
# Each glyph is a list of polylines; each polyline is [(x,y), ...].
# Coordinates are in a 0..21 x 0..32 grid (width x height).
# Source: derived from Hershey Simplex Roman, normalized.

HERSHEY: dict[str, list[list[tuple[float, float]]]] = {
    "A": [[(0, 32), (10, 0), (21, 32)], [(4, 20), (17, 20)]],
    "B": [[(0, 0), (0, 32), (14, 32), (19, 28), (19, 22), (14, 18), (0, 18)],
           [(14, 18), (19, 14), (19, 6), (14, 0), (0, 0)]],
    "C": [[(19, 28), (14, 32), (5, 32), (0, 28), (0, 4), (5, 0), (14, 0), (19, 4)]],
    "D": [[(0, 0), (0, 32), (12, 32), (19, 26), (19, 6), (12, 0), (0, 0)]],
    "E": [[(19, 0), (0, 0), (0, 32), (19, 32)], [(0, 16), (14, 16)]],
    "F": [[(19, 0), (0, 0), (0, 32), (19, 32)], [(0, 16), (14, 16)]],
    "G": [[(19, 28), (14, 32), (5, 32), (0, 28), (0, 4), (5, 0), (14, 0), (19, 4), (19, 16), (12, 16)]],
    "H": [[(0, 0), (0, 32)], [(21, 0), (21, 32)], [(0, 16), (21, 16)]],
    "I": [[(5, 0), (16, 0)], [(10, 0), (10, 32)], [(5, 32), (16, 32)]],
    "K": [[(0, 0), (0, 32)], [(19, 32), (0, 14), (19, 0)]],
    "L": [[(0, 0), (0, 32), (19, 32)]],
    "M": [[(0, 32), (0, 0), (10, 16), (21, 0), (21, 32)]],
    "N": [[(0, 32), (0, 0), (21, 32), (21, 0)]],
    "O": [[(5, 0), (0, 4), (0, 28), (5, 32), (16, 32), (21, 28), (21, 4), (16, 0), (5, 0)]],
    "P": [[(0, 32), (0, 0), (14, 0), (19, 4), (19, 14), (14, 18), (0, 18)]],
    "R": [[(0, 32), (0, 0), (14, 0), (19, 4), (19, 14), (14, 18), (0, 18)],
           [(14, 18), (19, 32)]],
    "S": [[(19, 4), (14, 0), (5, 0), (0, 4), (0, 12), (5, 16), (14, 16), (19, 20), (19, 28), (14, 32), (5, 32), (0, 28)]],
    "T": [[(0, 0), (21, 0)], [(10, 0), (10, 32)]],
    "U": [[(0, 0), (0, 28), (5, 32), (16, 32), (21, 28), (21, 0)]],
    "W": [[(0, 0), (4, 32), (10, 16), (16, 32), (21, 0)]],
    "Y": [[(0, 0), (10, 16), (21, 0)], [(10, 16), (10, 32)]],
    " ": [],
    "!": [[(10, 8), (10, 32)], [(10, 0), (10, 4)]],
    "?": [[(0, 4), (5, 0), (16, 0), (21, 4), (21, 10), (10, 18), (10, 22)],
           [(10, 28), (10, 32)]],
    ".": [[(10, 30), (10, 32)]],
    ",": [[(8, 30), (10, 34)]],
    "-": [[(0, 16), (21, 16)]],
}

GLYPH_WIDTH = 21
GLYPH_SPACING = 8


def text_to_strokes(
    text: str,
    origin_x: float,
    origin_y: float,
    scale: float = 1.0,
) -> list[si.Line]:
    """Render text as a list of Line scene items using Hershey glyphs."""
    lines: list[si.Line] = []
    cursor_x = origin_x

    for ch in text.upper():
        glyph = HERSHEY.get(ch)
        if glyph is None:
            cursor_x += (GLYPH_WIDTH + GLYPH_SPACING) * scale
            continue

        for polyline in glyph:
            if len(polyline) < 2:
                continue
            points = []
            for j, (gx, gy) in enumerate(polyline):
                px = cursor_x + gx * scale
                py = origin_y + gy * scale
                points.append(si.Point(
                    x=px, y=py,
                    speed=5 + (j % 10),
                    direction=90 + (j * 7) % 60,
                    width=9,
                    pressure=150 + (j % 30),
                ))
            lines.append(si.Line(
                color=si.PenColor.BLACK,
                tool=si.Pen.BALLPOINT_2,
                points=points,
                thickness_scale=2.0,
                starting_length=0.0,
            ))

        cursor_x += (GLYPH_WIDTH + GLYPH_SPACING) * scale

    return lines


def make_reply_blocks(
    text: str,
    origin_x: float,
    origin_y: float,
    scale: float = 1.1,
    author_id: int = 1,
) -> list:
    """
    Build SceneLineItemBlocks for reply text, anchored to existing Layer 1.

    Layer 1 is always node_id CrdtId(0, 11) in valid .rm v6 files.
    No SceneTreeBlock or TreeNodeBlock needed — we inject into the
    existing scene tree.
    """
    LAYER_1 = CrdtId(0, 11)
    blocks = []

    strokes = text_to_strokes(text, origin_x, origin_y, scale)
    for i, line in enumerate(strokes):
        blocks.append(SceneLineItemBlock(
            parent_id=LAYER_1,
            item=CrdtSequenceItem(
                item_id=CrdtId(author_id, 200 + i),
                left_id=CrdtId(0, 0),
                right_id=CrdtId(0, 0),
                deleted_length=0,
                value=line,
            ),
        ))

    return blocks


def splice_reply(input_path: str, output_path: str, reply_text: str) -> None:
    """
    Read an .rm v6 page, append reply strokes, write to output.

    Original blocks are left untouched. Reply blocks are serialized
    independently and appended to the raw bytes.
    """
    with open(input_path, "rb") as f:
        original_data = f.read()

    reply_blocks = make_reply_blocks(
        reply_text,
        origin_x=-300.0,  # v6 origin is top-center; negative = left half
        origin_y=700.0,
        scale=1.1,
    )

    # Serialize only the reply blocks
    reply_buf = BytesIO()
    write_blocks(reply_buf, reply_blocks, options={"version": "3.4"})
    reply_bytes = reply_buf.getvalue()

    # Strip the redundant 43-byte header that write_blocks always emits
    HEADER_SIZE = 43
    if reply_bytes[:HEADER_SIZE] == original_data[:HEADER_SIZE]:
        reply_bytes = reply_bytes[HEADER_SIZE:]

    output_data = original_data + reply_bytes

    with open(output_path, "wb") as f:
        f.write(output_data)

    print(f"Wrote {output_path} ({len(reply_blocks)} blocks, "
          f"{len(original_data)}+{len(reply_bytes)}={len(output_data)} bytes)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else input_path
    reply = "GRIMOIRE SAYS HELLO"

    splice_reply(input_path, output_path, reply)
