"""Full-resolution originals of any saved shot, tiled by capture index, for by-eye labelling.

    .venv\\Scripts\\python.exe research\\temporal_evidence\\sync_tiles.py <shot-prefix> <camera> <first> <last> [--width 260] [--cols 14]

Each tile is the native still cropped to the golfer, labelled with the capture
index and replay-relative time; byte-identical repeats are marked "dup".
Written to output/sync/tiles_<shot>_<camera>_<first>-<last>.jpg.
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "media" / "swing_shots"
OUT = Path(__file__).with_name("output") / "sync"
CROP = {"swing_behind": (100, 0, 900, 1200), "swing_side": (250, 150, 1150, 1350)}

parser = argparse.ArgumentParser()
parser.add_argument("shot")
parser.add_argument("camera")
parser.add_argument("first", type=int)
parser.add_argument("last", type=int)
parser.add_argument("--width", type=int, default=260)
parser.add_argument("--cols", type=int, default=14)
args = parser.parse_args()

shot = next(
    p for p in SHOTS.iterdir() if p.name.startswith(args.shot) or args.shot in p.name
)
meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
cam = next(
    c for c in meta["diagnostic_timeline"]["cameras"] if c["camera_id"] == args.camera
)
frames = {f["capture_index"]: f for f in cam["frames"]}
box = CROP[args.camera]
w = args.width
h = int(w * (box[3] - box[1]) / (box[2] - box[0]))
indices = list(range(args.first, args.last + 1))
cols = min(args.cols, len(indices))
rows = -(-len(indices) // cols)
canvas = Image.new("RGB", (cols * (w + 4), rows * (h + 28)), (20, 20, 20))
draw = ImageDraw.Draw(canvas)
font = ImageFont.load_default(size=20)
previous = None
for n, index in enumerate(indices):
    f = frames[index]
    with Image.open(shot / f["file"]) as im:
        tile = im.convert("RGB").crop(box).resize((w, h), Image.Resampling.LANCZOS)
    x, y = (n % cols) * (w + 4), (n // cols) * (h + 28)
    canvas.paste(tile, (x, y + 28))
    dup = " dup" if f["sha256"] == previous else ""
    previous = f["sha256"]
    draw.text(
        (x + 4, y + 4),
        f"#{index} +{f['offset_from_replay_start_seconds']:.2f}{dup}",
        fill="white",
        font=font,
    )
OUT.mkdir(parents=True, exist_ok=True)
name = OUT / f"tiles_{shot.name[:17]}_{args.camera[6:]}_{args.first}-{args.last}.jpg"
canvas.save(name, quality=90)
print(name, canvas.size)
