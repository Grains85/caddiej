"""Offline: summarise the real diagnostic swing shots. Reads only; writes nothing.

Usage: .venv\\Scripts\\python.exe research\\temporal_evidence\\inspect_shots.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "media" / "swing_shots"


def main() -> None:
    for shot in sorted(p for p in SHOTS.iterdir() if p.is_dir()):
        meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
        print("=" * 100)
        print(shot.name)
        cfg = meta.get("configuration", {})
        print("configuration:", json.dumps(cfg, sort_keys=True)[:1500])
        sheet = meta.get("evidence_sheet") or {}
        print(
            "sheet:",
            sheet.get("width"),
            "x",
            sheet.get("height"),
            "bytes",
            sheet.get("bytes"),
            "q",
            sheet.get("quality"),
        )
        for cell in sheet.get("cells", []):
            print(
                f"   {cell['cell_id']:<11} {cell['frame_id']:<16} aimed={cell.get('aimed_phase')!s:<11}"
                f" off={cell.get('offset_seconds')} box={cell.get('box')} scale={cell.get('scale')}"
            )
        cm = meta.get("coach_metrics") or {}
        print("all_values:", cm.get("width"), "x", cm.get("height"), cm.get("file"))
        req = meta.get("coach_request")
        print("coach_request:", req)
        diag = meta.get("diagnostic_timeline") or {}
        for cam in diag.get("cameras", []):
            print(
                f"-- {cam['camera_id']} view={cam['view']} captured={cam['captured_frames']}"
                f" failed={cam['failed_frames']} replay_start={cam['replay_start_seconds']}"
                f" pre_replay={cam['pre_replay_frames']} borrowed={cam['replay_start_borrowed_from']}"
            )
            cad = cam.get("cadence") or {}
            print("   cadence:", {k: cad.get(k) for k in ("achieved_fps", "downswing_achieved_fps", "interval_min_ms", "interval_max_ms", "spacing_holds", "missed_deadlines")})
            line = []
            for fr in cam["frames"]:
                rel = fr["offset_from_replay_start_seconds"]
                tag = ""
                if fr["selected"]:
                    tag = "*" + (fr.get("selected_phase") or "?")[:5]
                ch = "" if fr["changed"] else "="
                line.append(
                    f"{fr['capture_index']:02d}:{'' if rel is None else f'{rel:+.3f}'}{ch}{tag}"
                    f"[{fr['width']}x{fr['height']},{fr['bytes'] // 1000}k]"
                )
            for i in range(0, len(line), 4):
                print("   " + "  ".join(line[i : i + 4]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
