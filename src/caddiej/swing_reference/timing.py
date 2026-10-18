"""Monotonic timings for one coached shot.

Durations are milliseconds. The budget labels are product targets. A simulated
provider delay is not a measurement of GPT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CoachingTrace:
    """Marks for one shot. Recording a mark does not wait on anything."""

    shot_id: str
    feedback: str
    marks: dict[str, float] = field(default_factory=dict)
    request_type: str = ""
    model: str = ""
    image_count: int = 0
    image_detail: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    wording_input_tokens: int | None = None
    wording_output_tokens: int | None = None
    wording_cached_tokens: int | None = None
    wording_used: bool = False
    delivered: bool = False
    observation_included: bool = False
    simulated: bool = False
    evidence_stages: dict[str, int] = field(default_factory=dict)
    capture_id: str = ""
    selected: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    impact_status: str = ""
    impact_interval: str = ""
    decision: str = ""
    rule_id: str = ""
    errors: tuple[str, ...] = ()

    def mark(self, name: str, at: float) -> None:
        self.marks.setdefault(name, at)

    def span_ms(self, start: str, end: str) -> int | None:
        if start not in self.marks or end not in self.marks:
            return None
        return int(round((self.marks[end] - self.marks[start]) * 1000))

    def durations(self) -> dict[str, int | None]:
        """The spans a player can feel, plus the pieces inside them."""

        durations = {
            "capture_ms": self.span_ms("shot_completed", "swing_frames_ready"),
            "evidence_ms": self.span_ms("evidence_started", "evidence_completed"),
            "timeline_ms": self.evidence_stages.get("timeline_ms"),
            "decode_ms": self.evidence_stages.get("decode_ms"),
            "motion_ms": self.evidence_stages.get("motion_ms"),
            "image_loading_ms": self.evidence_stages.get("image_loading_ms"),
            "frame_difference_ms": self.evidence_stages.get("frame_difference_ms"),
            "optical_flow_ms": self.evidence_stages.get("optical_flow_ms"),
            "sampler_selection_ms": self.evidence_stages.get("sampler_selection_ms"),
            "pose_ms": self.evidence_stages.get("pose_ms"),
            "impact_ms": self.evidence_stages.get("impact_ms"),
            "selection_ms": self.evidence_stages.get("selection_ms"),
            "package_ms": self.evidence_stages.get("package_ms"),
            "request_prepare_ms": self.span_ms(
                "request_prepare_started", "request_prepare_completed"
            ),
            "stage_ms": self.evidence_stages.get("pose_ms")
            if self.evidence_stages
            else self.span_ms("stage_started", "stage_completed"),
            "vision_ms": self.span_ms("vision_started", "vision_received"),
            "validation_ms": self.span_ms("vision_received", "assessment_validated"),
            "decision_ms": self.span_ms(
                "assessment_validated", "coaching_decision_completed"
            ),
            "wording_ms": self.span_ms("wording_started", "wording_completed"),
            "tts_startup_ms": self.span_ms("hud_published", "tts_started"),
            "shot_to_hud_ms": self.span_ms("shot_completed", "hud_published"),
            "shot_to_audio_ms": self.span_ms("shot_completed", "tts_started"),
            "playback_ms": self.span_ms("tts_started", "tts_completed"),
        }
        return durations


def budget_for(milliseconds: int | None) -> str:
    """Product target for one wait. Not a claim about measured GPT latency."""

    if milliseconds is None:
        return "unmeasured"
    if milliseconds < 2000:
        return "excellent"
    if milliseconds <= 4000:
        return "good"
    if milliseconds <= 6000:
        return "needs_optimization"
    return "too_slow"


def latency_report(trace: CoachingTrace) -> dict[str, Any]:
    """Durations, usage, and budget labels for one shot.

    `simulated` is true when the provider delays were injected. Those numbers
    are not GPT latency.
    """

    durations = trace.durations()
    hud = durations["shot_to_hud_ms"]
    audio = durations["shot_to_audio_ms"]
    report: dict[str, Any] = {
        "shot_id": trace.shot_id,
        "feedback": trace.feedback,
        "request_type": trace.request_type,
        "model": trace.model,
        "image_count": trace.image_count,
        "image_detail": trace.image_detail,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cached_tokens": trace.cached_tokens,
        "wording_input_tokens": trace.wording_input_tokens,
        "wording_output_tokens": trace.wording_output_tokens,
        "wording_cached_tokens": trace.wording_cached_tokens,
        "wording_used": trace.wording_used,
        "observation_included": trace.observation_included,
        "delivered": trace.delivered,
        "simulated": trace.simulated,
        "hud_budget": budget_for(hud),
        "audio_budget": budget_for(audio),
    }
    report.update(durations)
    if trace.simulated:
        report["simulation_note"] = (
            "Simulated provider delays. Not measured GPT latency."
        )
    return report
