"""The provisional result and progress rendering, pinned at the strings.

The overlay has no JS test runner, so what matters about it is held here the
way `test_hud_overlay_js` holds the rest: which keys are read, that model text
is assigned as text, that a missing frame hides rather than breaks, and that
progress is a stage word with no number anywhere near it.
"""

import re

from tests.unit.test_hud_overlay_js import FLOOR_CSS, INDEX_HTML, STATE_JS


def test_the_result_fields_are_read_from_the_advice_payload() -> None:
    assert "renderAdviceEvidence(advice.evidence_url)" in STATE_JS
    assert "renderAdviceScore(advice.swing_score)" in STATE_JS
    assert "renderAdviceObservation(advice.observation)" in STATE_JS


def test_the_observation_is_text_never_markup() -> None:
    assert "adviceObservationEl.textContent = text" in STATE_JS
    assert ".innerHTML =" not in STATE_JS


def test_a_frame_that_fails_to_load_hides_itself() -> None:
    assert 'url.startsWith("/evidence/")' in STATE_JS
    assert re.search(
        r"adviceEvidenceEl\.onerror = \(\) => \{\s*adviceEvidenceEl\.hidden = true;",
        STATE_JS,
    )


def test_the_artwork_and_the_player_s_frame_are_separate_elements() -> None:
    assert 'id="advice-cue"' in INDEX_HTML
    assert 'id="advice-evidence"' in INDEX_HTML
    assert "SWING SCORE" in INDEX_HTML


def test_progress_is_painted_and_is_a_stage_word_only() -> None:
    assert "renderProgress(state.progress)" in STATE_JS
    assert 'id="progress"' in INDEX_HTML
    body = STATE_JS.split("function renderProgress", 1)[1].split("\n}\n", 1)[0]
    assert "%" not in body
    assert "started_at" not in body, "elapsed time is not turned into a fraction"


def test_progress_does_not_move_for_reduced_motion() -> None:
    reduced = FLOOR_CSS.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".progress__bar::after" in reduced


def test_ball_status_is_painted_exactly_as_before() -> None:
    assert "renderBallStatus(state.ball_status, state.hitting_strip)" in STATE_JS
    paint = STATE_JS.split("function paint(snapshot)", 1)[1].split("\n}\n", 1)[0]
    assert paint.index("renderBallStatus") < paint.index("renderProgress")
