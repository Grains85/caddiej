"""Briefly revealing a source somebody already built in OBS.

Two features need exactly this — the ProTee Shot Video panel and the ProTee
Top Impact view — and they need it with the same care: never on the floor's
path, never able to let an older shot's hide black out a newer shot's reveal,
and never left showing after CaddieJ stops. That care is the whole of
`TimedObsSource`; what a feature does with it is a scene name, a source name,
two durations and a name for the log, which is `RevealFeature` and nothing
more. Each feature is still its own instance with its own connection, lock and
generation counter, so a bay that has one source and not the other is normal
and one failing cannot cost the other.

The placement, crop, scale and stacking of the source stay in OBS, where the
person who set the bay up put them. Nothing here switches scenes, reorders
sources, creates inputs, or reads video.
"""

from caddiej.obs_reveal.diagnose import ObsSourceProbe, ProbeClient, probe_obs_source
from caddiej.obs_reveal.features import RevealFeature
from caddiej.obs_reveal.timed_source import (
    ClientFactory,
    Sleep,
    TimedObsSource,
    VisibilityClient,
)

__all__ = [
    "ClientFactory",
    "ObsSourceProbe",
    "ProbeClient",
    "RevealFeature",
    "Sleep",
    "TimedObsSource",
    "VisibilityClient",
    "probe_obs_source",
]
