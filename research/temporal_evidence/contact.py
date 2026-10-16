"""Offline: numbered contact sheets of every replay still, for labelling by eye.

Writes research/temporal_evidence/output/contact/<shot>_<camera>_<first>-<last>.jpg
Each tile is printed with its capture index and its offset from the replay start.

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\contact.py [first last]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "media" / "swing_shots"
OUT = Path(__file__).with_name("output") / "contact"
TILE_W = 300
COLUMNS = 4


def main(first: int, last: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default(size=26)
    for shot in sorted(p for p in SHOTS.iterdir() if p.is_dir()):
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        for camera in meta["diagnostic_timeline"]["cameras"]:
            frames = [
                f for f in camera["frames"] if first <= f["capture_index"] <= last
            ]
            tiles = []
            for f in frames:
                with Image.open(shot / f["file"]) as image:
                    scale = TILE_W / image.width
                    tile = image.convert("RGB").resize(
                        (TILE_W, round(image.height * scale)), Image.Resampling.LANCZOS
                    )
                draw = ImageDraw.Draw(tile)
                rel = f["offset_from_replay_start_seconds"]
                text = f"{f['capture_index']:02d} {rel:+.3f}"
                draw.rectangle((0, 0, TILE_W, 32), fill=(0, 0, 0))
                draw.text((6, 2), text, fill=(255, 255, 0), font=font)
                tiles.append(tile)
            if not tiles:
                continue
            th = tiles[0].height
            rows = (len(tiles) + COLUMNS - 1) // COLUMNS
            sheet = Image.new("RGB", (COLUMNS * TILE_W, rows * th), (20, 20, 20))
            for i, tile in enumerate(tiles):
                sheet.paste(tile, ((i % COLUMNS) * TILE_W, (i // COLUMNS) * th))
            name = f"{shot.name}_{camera['camera_id']}_{first:02d}-{last:02d}.jpg"
            sheet.save(OUT / name, quality=90)
            print(OUT / name)


if __name__ == "__main__":
    a, b = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (15, 51)
    main(a, b)
