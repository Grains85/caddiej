"""The fresh-swing harness: generic, dry by default, one call per shot, frozen prior.

.venv\\Scripts\\python.exe -m pytest research\\temporal_evidence\\tests\\test_fresh_run.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import fresh_freeze as ff  # noqa: E402
import fresh_run as fr  # noqa: E402
import hybrid_replay as hy  # noqa: E402
import poc4_vx14c as poc4  # noqa: E402
import t36  # noqa: E402

from caddiej.config import Settings  # noqa: E402

BASE = (
    fr.FRESH / "baseline"
    if (fr.FRESH / "baseline" / ff.BASELINE[0]).exists()
    else fr.SOURCE
)
needs_shots = pytest.mark.skipif(
    not all((BASE / name / "metadata.json").exists() for name in ff.BASELINE),
    reason="baseline shots not on disk",
)
POC4_PAYLOAD = (
    json.loads(poc4.REPLY.read_text(encoding="utf-8"))["payload"]
    if poc4.REPLY.exists()
    else None
)


@dataclass
class _Usage:
    input_tokens: int = 13000
    output_tokens: int = 700


class _Reply:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class FakeProvider:
    calls = 0

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.payload = payload
        self.model = "gpt-5-mini"

    async def complete(self, request: Any) -> _Reply:
        FakeProvider.calls += 1
        if self.payload is None:
            raise TimeoutError("fake timeout")
        return _Reply(self.payload)

    def usage(self) -> _Usage:
        return _Usage()

    async def aclose(self) -> None:
        return None


def _never(_settings: Any) -> Any:
    raise AssertionError("a provider was constructed without --send")


@pytest.fixture(scope="module")
def frozen(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not all((BASE / name / "metadata.json").exists() for name in ff.BASELINE):
        pytest.skip("baseline shots not on disk")
    root = tmp_path_factory.mktemp("fresh")
    ff.freeze(root, source=BASE, settings_obj=Settings())
    return root


def _shot(root: Path, name: str, tmp: Path) -> Path:
    dest = tmp / name
    shutil.copytree(root / "baseline" / name, dest)
    return dest


# --- generic, frozen ---------------------------------------------------------------------


def test_runner_carries_nothing_shot_specific() -> None:
    source = Path(fr.__file__).read_text(encoding="utf-8")
    for banned in (
        "20261008",
        "TRUTH",
        "grade_cell",
        "load_cells",
        "poc1.SHEET",
        "hy.summary",
        "IMPACT_SEEN",
    ):
        assert banned not in source, banned


@needs_shots
def test_prior_frozen_from_the_three_saved_shots_only(frozen: Path) -> None:
    config = json.loads((frozen / "config.json").read_text(encoding="utf-8"))
    prior = config["impact_prior"]
    assert [s["shot"] for s in prior["source"]] == list(ff.BASELINE)
    assert prior["centre_s"] == pytest.approx((1.986 + 1.983 + 1.964) / 3, abs=2e-3)
    assert prior["range_s"] == pytest.approx([1.7277, 2.2277], abs=1e-3)
    assert prior["half_width_s"] == hy.R7_HALF
    with pytest.raises(SystemExit, match="already frozen"):
        ff.freeze(frozen, source=BASE, settings_obj=Settings())


@needs_shots
def test_criteria_written_with_every_metric(frozen: Path) -> None:
    criteria = json.loads((frozen / "criteria.json").read_text(encoding="utf-8"))
    assert set(criteria["metrics"]) == {"M1", "M2", "M3", "M4", "M5", "M6", "overall"}
    assert criteria["thresholds"] == {
        "M1_min": 0.90,
        "M2_min": 0.95,
        "M3_min_of_10": 9,
        "M4_max": 0.05,
        "M5_max": 0,
        "M6_max_s": 15.0,
        "M6_preferred_median_s": 10.0,
    }


# --- decoding --------------------------------------------------------------------------------


def _cams(missing: set[str] = frozenset()) -> dict[str, Any]:
    rows = []
    for camera_id, prefix, view in (
        ("swing_behind", "DTL", "behind"),
        ("swing_side", "FO", "side"),
    ):
        for n in range(1, 19):
            cid = f"{prefix}_{n:02d}"
            if cid not in missing:
                rows.append(
                    {
                        "cell_id": cid,
                        "camera_id": camera_id,
                        "view": view,
                        "frame_id": f"{camera_id}_c{n:02d}",
                        "capture_index": n,
                        "offset_from_replay_start_seconds": n * 0.1,
                    }
                )
    return hy.sheet(rows)


def test_numbers_resolve_by_printed_id_never_by_position() -> None:
    cams = _cams({"FO_07"})
    assert fr.cell_of(cams, "fo", 7) == "invalid"
    assert fr.cell_of(cams, "fo", 8) == "FO_08"
    assert fr.cell_of(cams, "dtl", 7) == "DTL_07"
    assert fr.cell_of(cams, "dtl", 0) is None
    assert fr.cell_of(cams, "dtl", True) is None


@pytest.mark.skipif(POC4_PAYLOAD is None, reason="POC4 reply not on disk")
def test_decode_is_poc4_read_reply_on_a_full_sheet() -> None:
    cams = _cams()
    mine = fr.decode(POC4_PAYLOAD, cams)
    theirs = poc4.read_reply(POC4_PAYLOAD, cams)
    assert mine[:4] == theirs
    assert mine[4] == []


def test_schema_shape_folds_cell_lists_only() -> None:
    a = {"enum": ["DTL_01", "FO_02", "ALL_VALUES"], "other": ["a", "b"], "n": [0, 18]}
    b = {"enum": ["DTL_01", "ALL_VALUES"], "other": ["a", "b"], "n": [0, 18]}
    assert fr.schema_shape(a) == fr.schema_shape(b)
    assert fr.schema_shape({"other": ["a"]}) != fr.schema_shape({"other": ["b"]})


# --- no send by default; exactly one send ------------------------------------------------------


@needs_shots
def test_default_run_is_dry(frozen: Path, tmp_path: Path) -> None:
    shot = _shot(frozen, ff.BASELINE[1], tmp_path)
    result = fr.run(shot, root=frozen, settings_obj=Settings(), provider_factory=_never)
    out = frozen / "runs" / shot.name
    assert result["mode"] == "dry"
    assert not (out / "reply.json").exists()
    for name in (
        "sheet_T36S.jpg",
        "cells.json",
        "request_system.txt",
        "request_evidence.txt",
        "request_schema.json",
        "request_meta.json",
    ):
        assert (out / name).exists(), name
    meta = json.loads((out / "request_meta.json").read_text(encoding="utf-8"))
    assert meta["sheet"]["variant"] == t36.VARIANT and meta["sheet"]["width"] == 2048
    assert meta["reasoning_effort"] == "minimal" and meta["model"] == "gpt-5-mini"
    config = json.loads((frozen / "config.json").read_text(encoding="utf-8"))
    assert meta["request_sha256"]["system"] == config["request_sha256"]["system"]
    assert meta["request_sha256"]["schema"] == config["request_sha256"]["schema"]


def test_cli_has_no_send_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setattr(
        fr, "run", lambda shot, send=False: seen.setdefault("send", send) and {}
    )
    monkeypatch.setattr(fr, "resolve", lambda shot, root: Path(shot))
    fr.main(["run", "x"])
    assert seen["send"] is False


@needs_shots
@pytest.mark.skipif(POC4_PAYLOAD is None, reason="POC4 reply not on disk")
def test_send_once_saves_immutable_reply_then_refuses(
    frozen: Path, tmp_path: Path
) -> None:
    shot = _shot(frozen, ff.BASELINE[2], tmp_path)
    FakeProvider.calls = 0
    result = fr.run(
        shot,
        send=True,
        root=frozen,
        settings_obj=Settings(),
        provider_factory=lambda _s: FakeProvider(POC4_PAYLOAD),
    )
    out = frozen / "runs" / shot.name
    reply = out / "reply.json"
    assert result["mode"] == "sent" and FakeProvider.calls == 1
    assert not os.stat(reply).st_mode & stat.S_IWRITE
    ledger = [
        json.loads(x)
        for x in (frozen / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [x["shot"] for x in ledger].count(shot.name) == 1
    assert ledger[-1]["reply_sha256"] == fr.sha256(reply.read_bytes())
    analysis = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["impact_prior"] == pytest.approx([1.7277, 2.2277], abs=1e-3)
    assert len(analysis["groups"]) == 18
    for g in analysis["groups"]:
        if g["cell"] is not None:
            assert (
                g["original"].startswith("diagnostic/")
                and g["capture_index"] is not None
            )
    assert analysis["compact"]["wire_failures"] == []
    assert analysis["compact"]["cost_usd"] is not None
    assert (out / "grades_template.json").exists()
    with pytest.raises(SystemExit, match="No retries"):
        fr.run(
            shot,
            send=True,
            root=frozen,
            settings_obj=Settings(),
            provider_factory=lambda _s: FakeProvider(POC4_PAYLOAD),
        )
    assert FakeProvider.calls == 1
    assert (
        fr.run(shot, root=frozen, settings_obj=Settings(), provider_factory=_never)[
            "mode"
        ]
        == "dry"
    )
    assert fr.sha256(reply.read_bytes()) == ledger[-1]["reply_sha256"]


@needs_shots
def test_failed_call_is_recorded_as_wire_failure_not_retried(
    frozen: Path, tmp_path: Path
) -> None:
    shot = _shot(frozen, ff.BASELINE[0], tmp_path / "x")
    shot = shot.rename(tmp_path / "timeout-shot")
    FakeProvider.calls = 0
    fr.run(
        shot,
        send=True,
        root=frozen,
        settings_obj=Settings(),
        provider_factory=lambda _s: FakeProvider(None),
    )
    analysis = json.loads(
        (frozen / "runs" / shot.name / "analysis.json").read_text(encoding="utf-8")
    )
    assert FakeProvider.calls == 1
    assert analysis["compact"]["status"] == "error"
    assert analysis["compact"]["wire_failures"][0].startswith("no reply: TimeoutError")


@needs_shots
def test_send_refuses_when_anything_frozen_changed(
    frozen: Path, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for name in ("config.json", "criteria.json"):
        shutil.copy(frozen / name, root / name)
    shot = _shot(frozen, ff.BASELINE[1], tmp_path)
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    config["fingerprints"]["research/temporal_evidence/t36.py"] = "0" * 64
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="harness changed"):
        fr.run(
            shot, send=True, root=root, settings_obj=Settings(), provider_factory=_never
        )
    config["fingerprints"] = fr.fingerprints()
    config["settings"]["reasoning_effort"] = "low"
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="settings"):
        fr.run(
            shot, send=True, root=root, settings_obj=Settings(), provider_factory=_never
        )
    assert not (root / "runs" / shot.name / "reply.json").exists()


def test_send_refuses_before_the_freeze(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="fresh_freeze"):
        fr.run(
            tmp_path,
            send=True,
            root=tmp_path,
            settings_obj=Settings(),
            provider_factory=_never,
        )


# --- preserve ------------------------------------------------------------------------------------


@needs_shots
def test_preserve_copies_post_freeze_shots_and_classifies(
    frozen: Path, tmp_path: Path
) -> None:
    root, source = tmp_path / "root", tmp_path / "swing_shots"
    root.mkdir()
    source.mkdir()
    for name in ("config.json", "criteria.json"):
        shutil.copy(frozen / name, root / name)
    old = _shot(frozen, ff.BASELINE[0], source)
    good = source / "29990101T000000-7-aaaaaaaa"
    shutil.copytree(old, good)
    meta = json.loads((good / "metadata.json").read_text(encoding="utf-8"))
    meta["started_at"] = "2999-01-01T00:00:00+00:00"
    (good / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    bare = source / "29990101T000100-8-bbbbbbbb"
    bare.mkdir()
    (bare / "metadata.json").write_text(
        json.dumps(
            {
                **meta,
                "started_at": "2999-01-01T00:01:00+00:00",
                "diagnostic_timeline": None,
            }
        ),
        encoding="utf-8",
    )
    added = fr.preserve(root, source)
    assert [(e["name"], e["role"], e["order"]) for e in added] == [
        (good.name, "test", 1),
        (bare.name, "excluded", None),
    ]
    assert "diagnostic timeline" in added[1]["reason"]
    assert fr._tree_hashes(good) == fr._tree_hashes(root / "shots" / good.name)
    assert fr.preserve(root, source) == []
    shutil.rmtree(good)
    assert (root / "shots" / good.name / "metadata.json").exists()


# --- evaluate ------------------------------------------------------------------------------------


def _fake_run(
    root: Path, name: str, wrong: set[tuple[str, str]], p7_off: int, wall: float
) -> None:
    out = root / "runs" / name
    out.mkdir(parents=True)
    groups, grades = [], {"dtl": {}, "fo": {}}
    for cam in ("dtl", "fo"):
        for p, phase in enumerate(fr.PHASES):
            pos = fr.POSITIONS[p]
            groups.append(
                {
                    "position": pos,
                    "phase": phase.value,
                    "camera": cam,
                    "cell": f"{cam}_{p}",
                    "capture_index": 40 if pos == "P7" else p,
                }
            )
            grades[cam][pos] = {
                "grade": "wrong" if (cam, pos) in wrong else "correct",
                "shown_on_sheet": None,
            }
    accepted = [
        {"checkpoint": "x", "phase": fr.PHASES[p].value, "camera": "dtl"}
        for p in range(9)
    ]
    fr._write(
        out / "analysis.json",
        {"groups": groups, "accepted": accepted, "compact": {"wire_failures": []}},
    )
    fr._write(
        out / "grades.json",
        {"groups": grades, "contact_tick": {"dtl": 40 + p7_off, "fo": 40}},
    )
    data = json.dumps({"status": "ok", "wall_seconds": wall}).encode()
    (out / "reply.json").write_bytes(data)
    with (root / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"shot": name, "reply_sha256": fr.sha256(data)}) + "\n")


def test_evaluate_applies_the_written_definitions(tmp_path: Path) -> None:
    fr._write(tmp_path / "config.json", {})
    fr._write(tmp_path / "criteria.json", {"thresholds": ff.CRITERIA["thresholds"]})
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
    _fake_run(tmp_path, names[0], {("dtl", "P4"), ("fo", "P5")}, 0, 9.0)
    _fake_run(tmp_path, names[1], set(), 2, 11.0)
    for n in names[2:]:
        _fake_run(tmp_path, n, set(), 1, 10.0)
    m = fr.evaluate(tmp_path)["metrics"]
    assert m["M1_P3_P8_correct_or_acceptable"][0] == pytest.approx(58 / 60)
    assert m["M2_P6_P9_correct_or_acceptable"][0] == 1.0
    assert m["M3_P7_within_one_tick"][:2] == ["9/10", True]
    assert m["M4_wrong_position_accepts"][2] == "1/45"
    assert m["M6_openai_latency_max_s"][:2] == [11.0, True]
    os.chmod(tmp_path / "runs" / names[3] / "reply.json", stat.S_IWRITE | stat.S_IREAD)
    (tmp_path / "runs" / names[3] / "reply.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="edited"):
        fr.evaluate(tmp_path)
