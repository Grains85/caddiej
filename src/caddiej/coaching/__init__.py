"""Coaching: what was wrong with a shot, and one sentence about it.

A subsystem rather than an adapter, on the same reasoning as `persistence` and
`video`. The cloud provider that phrases a line *is* an adapter and lives in
`adapters/openai`; everything here — measuring the fault, ranking it, writing the
prompt, refusing an unacceptable answer, deciding what is worth asking about — is
CaddieJ's own and knows nothing about any vendor.

The subsystem is built in two tiers, and the lower one is the feature:

* `diagnose` names the fault from the numbers. Instant, offline, reproducible,
  and its templated line is on the floor before anything could be asked.
* `AnalysisService` asks a model to say it better, afterwards, and throws the
  answer away if it is late, unacceptable, or about a shot that has been
  superseded.

Nothing in the real-time path waits on any of it, and a model that is absent,
slow, over budget or wrong changes only how well the line is worded.
"""

from caddiej.coaching.baseline import Baseline, ClubBaseline
from caddiej.coaching.cache import AdviceCache, fingerprint
from caddiej.coaching.cues import CUES, CUES_BY_ID, admissible_cues, preferred_cue
from caddiej.coaching.diagnose import consistency_of, diagnose
from caddiej.coaching.evidence import render
from caddiej.coaching.faults import FAULTS, Fault, oriented
from caddiej.coaching.frames import DEFAULT_OFFSETS, FrameSource, offsets_for
from caddiej.coaching.probe import probe_coach, probe_coach_cues
from caddiej.coaching.prompt import PROMPT_VERSION, build_request, reply_schema
from caddiej.coaching.provider import (
    AdviceProvider,
    AdviceRequest,
    ImageInput,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRefusedError,
    ProviderReply,
    ProviderReplyError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from caddiej.coaching.service import AnalysisHealth, AnalysisService
from caddiej.coaching.session import CoachingSession, FocusTrend, SessionProgress
from caddiej.coaching.templates import display_for, legal_displays, speech_for
from caddiej.coaching.validate import AdviceRejected, from_diagnosis, validate

__all__ = [
    "CUES",
    "CUES_BY_ID",
    "DEFAULT_OFFSETS",
    "FAULTS",
    "PROMPT_VERSION",
    "AdviceCache",
    "AdviceProvider",
    "AdviceRejected",
    "AdviceRequest",
    "AnalysisHealth",
    "AnalysisService",
    "Baseline",
    "ClubBaseline",
    "CoachingSession",
    "Fault",
    "FocusTrend",
    "FrameSource",
    "ImageInput",
    "ProviderError",
    "ProviderRateLimitedError",
    "ProviderRefusedError",
    "ProviderReply",
    "ProviderReplyError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "SessionProgress",
    "admissible_cues",
    "build_request",
    "consistency_of",
    "diagnose",
    "display_for",
    "fingerprint",
    "from_diagnosis",
    "legal_displays",
    "offsets_for",
    "oriented",
    "preferred_cue",
    "probe_coach",
    "probe_coach_cues",
    "render",
    "reply_schema",
    "speech_for",
    "validate",
]
