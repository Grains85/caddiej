"""Temporal Evidence V2 harness: separate root, sealed labels, one call, no send by default.

.venv\\Scripts\\python.exe -m pytest research\\temporal_evidence\\tests\\test_fresh_v2.py -q
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import fresh_freeze as ff  # noqa: E402
import fresh_run as fr  # noqa: E402
import fresh_v2_freeze as fz  # noqa: E402
import fresh_v2_run as v2  # noqa: E402
import v2_grade as grade  # noqa: E402

BASE = (
    fr.FRESH / "baseline"
    if (fr.FRESH / "baseline" / ff.BASELINE[0]).exists()
    else fr.SOURCE
)
needs_shots = pytest.mark.skipif(
    not all((BASE / name / "metadata.json").exists() for name in ff.BASELINE),
    reason="baseline shots not on disk",
)


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 100


class _Reply:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class FakeProvider:
    calls = 0

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.model = "gpt-5-mini"

    async def complete(self, request: Any) -> _Reply:
        FakeProvider.calls += 1
        return _Reply(self.payload)

    def usage(self) -> _Usage:
        return _Usage()

    async def aclose(self) -> None:
        return None


def test_the_real_v2_root_is_not_frozen() -> None:
    assert not (v2.ROOT / "config.json").exists()
    assert not (v2.ROOT / "criteria.json").exists()


def test_fingerprint_covers_every_v2_file_that_can_change_the_experiment() -> None:
    covered = set(v2.FINGERPRINTED)
    for name in (
        "research/temporal_evidence/fresh_v2_run.py",
        "research/temporal_evidence/fresh_v2_freeze.py",
        "research/temporal_evidence/v2_order.py",
        "research/temporal_evidence/v2_request.py",
        "research/temporal_evidence/v2_grade.py",
        "research/temporal_evidence/t36.py",
        "research/temporal_evidence/t36_request.py",
        "src/caddiej/coaching/vx.py",
        "src/caddiej/swing_capture/sheet.py",
    ):
        assert name in covered
        assert (fr.ROOT / name).is_file()
    assert "research/temporal_evidence/v2_offline.py" not in covered
    assert set(fr.FINGERPRINTED) <= covered


def test_a_blank_label_names_no_model_cell() -> None:
    doc = grade.template("shot", {"dtl": [16, 17], "fo": [16, 17]})
    text = json.dumps(doc)
    assert "DTL_" not in text and "FO_" not in text
    assert doc["sealed"] is False
    assert doc["dtl"]["contact_tick"] is None
    assert grade.problems(doc, {"dtl": [16, 17], "fo": [16, 17]})


@needs_shots
def test_freeze_copies_v1_settings_prior_and_thresholds(tmp_path: Path) -> None:
    root = tmp_path / "fresh_v2"
    config = fz.freeze(root)
    v1 = fr._json(fr.FRESH / "config.json")
    assert config["settings"] == fz.REQUIRED_SETTINGS == v1["settings"]
    assert config["impact_prior"] == v1["impact_prior"]
    assert config["sheet"]["variant"] == "T36S"
    criteria = fr._json(root / "criteria.json")
    assert criteria["thresholds"] == fz.THRESHOLDS
    assert criteria["grading"]["contact_tick"] == grade.CONTACT_RULE
    assert criteria["grading"]["p6"] == grade.P6_RULE
    assert "BEFORE the request" in criteria["grading"]["when"]
    assert set(config["fingerprints"]) == set(v2.FINGERPRINTED)
    with pytest.raises(SystemExit, match="already frozen"):
        fz.freeze(root)


def _shot(tmp: Path) -> Path:
    dest = tmp / ff.BASELINE[0]
    shutil.copytree(BASE / ff.BASELINE[0], dest)
    return dest


def _fill(shot: Path, root: Path) -> None:
    written = v2.label(shot, root)
    assert written["status"] == "template"
    doc = fr._json(Path(written["file"]))
    for key in ("dtl", "fo"):
        ticks = doc[key]["ticks"]
        doc[key]["contact_tick"] = ticks[17]
        for pos in grade.LABELLED_POSITIONS:
            tick = ticks[10 + int(pos[1:])]
            doc[key]["positions"][pos] = {"best": [tick], "acceptable": [tick]}
    fr._write(Path(written["file"]), doc)
    assert v2.label(shot, root)["status"] == "sealed"


def _ticks() -> dict[str, list[int]]:
    return {"dtl": list(range(10, 30)), "fo": list(range(10, 30))}


def _filled() -> dict[str, Any]:
    doc = grade.template("shot", _ticks())
    for key in ("dtl", "fo"):
        doc[key]["contact_tick"] = 17
        for pos in grade.LABELLED_POSITIONS:
            tick = 10 + int(pos[1:])
            doc[key]["positions"][pos] = {"best": [tick], "acceptable": [tick]}
    return doc


def test_a_filled_label_is_sealable_and_labels_every_position() -> None:
    assert grade.LABELLED_POSITIONS == tuple(f"P{n}" for n in range(1, 10))
    assert grade.problems(_filled(), _ticks()) == []


def test_a_label_may_not_name_a_sheet_cell_or_carry_unknown_fields() -> None:
    doc = _filled()
    doc["dtl"]["positions"]["P5"]["note"] = "model said DTL_11"
    assert any("sheet cell" in p for p in grade.problems(doc, _ticks()))
    doc = _filled()
    doc["fo"]["positions"]["P5"]["cell"] = 11
    assert any("unknown fields" in p for p in grade.problems(doc, _ticks()))
    doc = _filled()
    doc["model_groups"] = {}
    assert any("unknown fields" in p for p in grade.problems(doc, _ticks()))


def test_p6_labels_must_lie_between_p5_and_contact() -> None:
    doc = _filled()
    doc["dtl"]["positions"]["P6"]["acceptable"] = [15, 16]
    assert any("dtl P6" in p for p in grade.problems(doc, _ticks()))
    doc = _filled()
    doc["fo"]["positions"]["P6"]["acceptable"] = [16, 17]
    assert any("fo P6" in p for p in grade.problems(doc, _ticks()))


def test_an_unavailable_cell_is_never_counted_as_shown_on_the_sheet() -> None:
    rows = [
        {"camera_id": "swing_behind", "capture_index": 5},
        {"camera_id": "swing_behind", "capture_index": 6, "available": False},
        {"cell_id": "IMPACT"},
    ]
    assert v2.sheet_ticks(rows)["dtl"] == [5]
    assert grade.grade_pick(None, [6], [6], [5]) == ("unavailable", False)


@needs_shots
def test_dry_run_builds_the_v2_request_and_does_not_send(tmp_path: Path) -> None:
    root = tmp_path / "fresh_v2"
    fz.freeze(root)
    shot = _shot(tmp_path)

    def boom(_settings: Any) -> Any:
        raise AssertionError("a provider was constructed without --send")

    from caddiej.config import Settings

    result = v2.run(shot, root=root, settings_obj=Settings(), provider_factory=boom)
    assert result["mode"] == "dry"
    out = root / "runs" / shot.name
    assert not (out / "reply.json").exists()
    system = (out / "request_system.txt").read_text(encoding="utf-8")
    assert 'key is "transition"' in system
    assert "P5 transition" not in system
    config = fr._json(root / "config.json")
    meta = fr._json(out / "request_meta.json")
    assert meta["request_sha256"]["system"] == config["request_sha256"]["system"]
    assert meta["request_sha256"]["schema"] == config["request_sha256"]["schema"]
    assert meta["model"] == "gpt-5-mini" and meta["reasoning_effort"] == "minimal"
    assert any(key.endswith("v2_order.py") for key in meta["fingerprints"])


@needs_shots
def test_send_requires_sealed_labels_then_happens_once(tmp_path: Path) -> None:
    from caddiej.config import Settings

    root = tmp_path / "fresh_v2"
    fz.freeze(root)
    shot = _shot(tmp_path)
    FakeProvider.calls = 0
    payload = {
        "phases": {
            "backswing": {"d": 6, "f": 6, "a": "d", "s": [], "x": []},
            "top": {"d": 6, "f": 6, "a": "d", "s": [], "x": []},
        }
    }
    with pytest.raises(SystemExit, match="canonical labels"):
        v2.run(
            shot,
            send=True,
            root=root,
            settings_obj=Settings(),
            provider_factory=lambda _s: FakeProvider(payload),
        )
    assert FakeProvider.calls == 0
    _fill(shot, root)
    result = v2.run(
        shot,
        send=True,
        root=root,
        settings_obj=Settings(),
        provider_factory=lambda _s: FakeProvider(payload),
    )
    assert result["mode"] == "sent" and FakeProvider.calls == 1
    out = root / "runs" / shot.name
    notes = fr._json(out / "analysis.json")["compact"]["validator_notes"]
    assert any("strict order" in n for n in notes)
    ledger = [
        json.loads(line)
        for line in (root / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert ledger[-1]["reply_sha256"] == fr.sha256((out / "reply.json").read_bytes())
    assert ledger[-1]["canonical_sha256"] == fr.sha256(
        (out / "canonical.json").read_bytes()
    )
    with pytest.raises(SystemExit, match="No retries"):
        v2.run(
            shot,
            send=True,
            root=root,
            settings_obj=Settings(),
            provider_factory=lambda _s: FakeProvider(payload),
        )
    assert FakeProvider.calls == 1
    with pytest.raises(SystemExit, match="before the model"):
        v2.label(shot, root)


@needs_shots
def test_a_crash_before_the_call_never_sends_twice_or_swaps_labels(
    tmp_path: Path,
) -> None:
    from caddiej.config import Settings

    root = tmp_path / "fresh_v2"
    fz.freeze(root)
    shot = _shot(tmp_path)
    _fill(shot, root)
    FakeProvider.calls = 0

    def crash(_settings: Any) -> Any:
        raise RuntimeError("no API key")

    with pytest.raises(RuntimeError):
        v2.run(
            shot, send=True, root=root, settings_obj=Settings(), provider_factory=crash
        )
    out = root / "runs" / shot.name
    assert (out / "canonical.json").exists() and not (out / "reply.json").exists()
    labels = root / "labels" / f"{shot.name}.json"
    doc = fr._json(labels)
    doc["dtl"]["positions"]["P5"]["acceptable"].append(doc["dtl"]["ticks"][14])
    fr._write(labels, doc)
    v2.label(shot, root)
    with pytest.raises(SystemExit, match="different labels"):
        v2.run(
            shot,
            send=True,
            root=root,
            settings_obj=Settings(),
            provider_factory=lambda _s: FakeProvider({}),
        )
    assert FakeProvider.calls == 0


def _parity_root(tmp: Path, seed: int) -> None:
    import random

    rng = random.Random(seed)
    names = [f"s{i}" for i in range(5)]
    fr._write(tmp / "config.json", {})
    fr._write(tmp / "criteria.json", {"thresholds": fz.THRESHOLDS})
    fr._write(
        tmp / "manifest.json",
        {
            "test_set_size": 5,
            "shots": [
                {"name": n, "role": "test", "order": i + 1, "reason": None}
                for i, n in enumerate(names)
            ],
        },
    )
    for name in names:
        out = tmp / "runs" / name
        out.mkdir(parents=True)
        on_sheet = sorted(rng.sample(range(10, 30), 12))
        canonical: dict[str, Any] = {}
        grades: dict[str, Any] = {"contact_tick": {}, "groups": {}}
        groups = []
        for key in ("dtl", "fo"):
            contact = 17 + rng.choice((-1, 0, 0, 1))
            positions = {}
            grades["groups"][key] = {}
            for pos in grade.LABELLED_POSITIONS:
                n = int(pos[1:])
                best, acceptable = [10 + n], [10 + n, 11 + n]
                positions[pos] = {"best": best, "acceptable": acceptable}
                tick = rng.choice(
                    (None, 10 + n, 11 + n, 25, 10 + n + rng.choice((-2, 2)))
                )
                if pos == "P7" and tick is not None:
                    tick = contact + rng.choice((-2, -1, 0, 1, 2))
                verdict, shown = grade.grade_pick(tick, best, acceptable, on_sheet)
                grades["groups"][key][pos] = {"grade": verdict, "shown_on_sheet": shown}
                groups.append(
                    {
                        "position": pos,
                        "camera": key,
                        "cell": None if tick is None else f"{key}_{tick}",
                        "capture_index": tick,
                    }
                )
            canonical[key] = {"contact_tick": contact, "positions": positions}
            grades["contact_tick"][key] = contact
        accepted = [
            {"phase": rng.choice(fr.PHASES).value, "camera": rng.choice(("dtl", "fo"))}
            for _ in range(rng.randrange(0, 30))
        ]
        fr._write(
            out / "analysis.json",
            {
                "groups": groups,
                "accepted": accepted,
                "compact": {"wire_failures": ["x"] * rng.choice((0, 0, 1))},
            },
        )
        fr._write(out / "grades.json", grades)
        fr._write(
            out / "cells.json",
            [
                {"camera_id": cam, "capture_index": t}
                for cam in ("swing_behind", "swing_side")
                for t in on_sheet
            ],
        )
        data = json.dumps(canonical).encode()
        (out / "canonical.json").write_bytes(data)
        reply = json.dumps(
            {"status": "ok", "wall_seconds": round(rng.uniform(8, 16), 3)}
        ).encode()
        (out / "reply.json").write_bytes(reply)
        with (tmp / "ledger.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write(
                json.dumps(
                    {
                        "shot": name,
                        "reply_sha256": fr.sha256(reply),
                        "canonical_sha256": fr.sha256(data),
                    }
                )
                + "\n"
            )


@pytest.mark.parametrize("seed", range(40))
def test_v2_metrics_equal_v1_metrics_on_the_same_grades(
    tmp_path: Path, seed: int
) -> None:
    _parity_root(tmp_path, seed)
    v1 = fr.evaluate(tmp_path)
    v2_result = v2.evaluate(tmp_path)
    assert v2_result["metrics"] == v1["metrics"]
    assert v2_result["pass"] == v1["pass"]


def test_cli_does_not_send_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bool] = {}
    monkeypatch.setattr(
        v2, "run", lambda shot, send=False: seen.setdefault("send", send)
    )
    monkeypatch.setattr(fr, "resolve", lambda shot, root: Path(shot))
    v2.main(["run", "x"])
    assert seen["send"] is False


def test_evaluate_grades_from_sealed_labels(tmp_path: Path) -> None:
    fr._write(tmp_path / "config.json", {})
    fr._write(tmp_path / "criteria.json", {"thresholds": fz.THRESHOLDS})
    names = [f"s{i}" for i in range(5)]
    fr._write(
        tmp_path / "manifest.json",
        {
            "test_set_size": 5,
            "shots": [
                {"name": n, "role": "test", "order": i + 1, "reason": None}
                for i, n in enumerate(names)
            ],
        },
    )
    positions = {
        pos: {"best": [5], "acceptable": [5, 6]} for pos in grade.LABELLED_POSITIONS
    }

    def camera() -> dict[str, Any]:
        return {"contact_tick": 5, "positions": positions}

    for i, name in enumerate(names):
        out = tmp_path / "runs" / name
        out.mkdir(parents=True)
        groups = []
        for key in ("dtl", "fo"):
            for pos in grade.LABELLED_POSITIONS:
                tick = 99 if (i == 0 and key == "dtl" and pos == "P5") else 5
                groups.append(
                    {
                        "position": pos,
                        "phase": "x",
                        "camera": key,
                        "cell": f"{key}_{pos}",
                        "capture_index": tick,
                    }
                )
        accepted = [
            {"checkpoint": "p1", "phase": fr.PHASES[0].value, "camera": "dtl"},
            {"checkpoint": "p5", "phase": fr.PHASES[4].value, "camera": "dtl"},
        ]
        fr._write(
            out / "analysis.json",
            {
                "groups": groups,
                "accepted": accepted,
                "compact": {"wire_failures": []},
            },
        )
        fr._write(
            out / "cells.json",
            [
                {"camera_id": "swing_behind", "capture_index": 5},
                {"camera_id": "swing_side", "capture_index": 5},
            ],
        )
        canonical = {"dtl": camera(), "fo": camera()}
        data = json.dumps(canonical).encode()
        (out / "canonical.json").write_bytes(data)
        reply = json.dumps({"status": "ok", "wall_seconds": 9.0}).encode()
        (out / "reply.json").write_bytes(reply)
        with (tmp_path / "ledger.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write(
                json.dumps(
                    {
                        "shot": name,
                        "reply_sha256": fr.sha256(reply),
                        "canonical_sha256": fr.sha256(data),
                    }
                )
                + "\n"
            )
    metrics = v2.evaluate(tmp_path)["metrics"]
    # One wrong P5 (dtl, shot 0) out of 5 x 2 x P3..P8.
    assert metrics["M1_P3_P8_correct_or_acceptable"][0] == pytest.approx(59 / 60)
    assert metrics["M2_P6_P9_correct_or_acceptable"][0] == 1.0
    assert metrics["M3_P7_within_one_tick"][0] == "10/10"
    # As in V1, every accepted checkpoint counts, P1 included: 2 per shot.
    assert metrics["M4_wrong_position_accepts"][2] == "1/10"
    (tmp_path / "runs" / names[1] / "canonical.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="edited"):
        v2.evaluate(tmp_path)
