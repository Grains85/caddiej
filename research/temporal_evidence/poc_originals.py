"""Full-resolution originals behind the POC's located cells, tiled for inspection.

For each camera and each capture index asked for, the native still cropped to
the golfer (no rescale unless --scale), labelled with capture index, sheet
cell and time. Written to output/poc/originals_<camera>_<first>-<last>.jpg.
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
SHOT = ROOT / "media" / "swing_shots" / "20261008T092949-4-d05a29c4"
HERE = Path(__file__).parent
OUT = HERE / "output" / "poc"
CROP = {"swing_behind": (100, 0, 900, 1200), "swing_side": (250, 150, 1150, 1350)}

parser = argparse.ArgumentParser()
parser.add_argument("camera")
parser.add_argument("indices", type=int, nargs="+")
parser.add_argument("--scale", type=float, default=1.0)
parser.add_argument("--box", type=int, nargs=4)
parser.add_argument("--cols", type=int, default=4)
args = parser.parse_args()

meta = json.loads((SHOT / "metadata.json").read_text(encoding="utf-8"))
cam = next(c for c in meta["diagnostic_timeline"]["cameras"] if c["camera_id"] == args.camera)
frames = {f["capture_index"]: f for f in cam["frames"]}
cells = {r["capture_index"]: r["cell_id"] for r in json.loads((HERE / "output" / "sheets" / f"{SHOT.name}_T36.json").read_text(encoding="utf-8")) if r.get("camera_id") == args.camera}
box = tuple(args.box) if args.box else CROP[args.camera]
w = int((box[2] - box[0]) * args.scale)
h = int((box[3] - box[1]) * args.scale)
cols = min(args.cols, len(args.indices))
rows = -(-len(args.indices) // cols)
canvas = Image.new("RGB", (cols * (w + 6), rows * (h + 40)), (20, 20, 20))
draw = ImageDraw.Draw(canvas)
font = ImageFont.load_default(size=26)
for n, index in enumerate(args.indices):
    f = frames[index]
    with Image.open(SHOT / f["file"]) as im:
        tile = im.convert("RGB").crop(box)
    if args.scale != 1.0:
        tile = tile.resize((w, h), Image.Resampling.LANCZOS)
    x, y = (n % cols) * (w + 6), (n // cols) * (h + 40)
    canvas.paste(tile, (x, y + 40))
    draw.text((x + 6, y + 6), f"#{index} {cells.get(index, '(not on sheet)')} +{f['offset_from_replay_start_seconds']:.3f}s", fill="white", font=font)
OUT.mkdir(parents=True, exist_ok=True)
name = OUT / f"originals_{args.camera}_{args.indices[0]}-{args.indices[-1]}{'_box' if args.box else ''}.jpg"
canvas.save(name, quality=92)
print(name, canvas.size)
