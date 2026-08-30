"""The three channels that reach subsystems CaddieJ already owns.

Each one is a translation and nothing more: an intensity becomes a dip, a
duration becomes an animation length, a line becomes something in the speech
queue. There is no timing, no retry and no state here, because all three belong
to the director or to the subsystem underneath — a channel that kept any of them
would be a second, quieter place where the room's behaviour was decided.

The mappings from intensity are the only judgements in the file, and each is
documented where it happens. They are worth reading as a set: the point of one
dial is that a par is a small pale word, a short shallow dip and silence, and an
ace is a large bright word, a deep long dip and a spoken sentence, without any of
the three having to be told about the other two.

OBS lives in `effects.obs` rather than here, because it is the one channel that
has to hold a connection to something that may not be running.
"""

import logging

from caddiej.domain.speech import SpeechPriority, Utterance
from caddiej.effects.effect import Effect
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorFlourish
from caddiej.lighting.modulation import Flourish
from caddiej.lighting.service import LightingService
from caddiej.speech.service import SpeechService

logger = logging.getLogger(__name__)

# How far a full-intensity effect dips the lights, as a fraction of whatever
# they were already at. Nearly dark at the top of the scale, because a hole in
# one should be unmistakable, and a room that goes to sixteen per cent for four
# seconds and comes back is unmistakable without being a light show.
MAX_DIP = 0.84


class FloorChannel:
    """Puts a celebration on the projected floor.

    The cheapest channel by a wide margin: writing floor state is a synchronous
    call and a fan-out to whatever clients are attached, so this cannot block,
    cannot fail and cannot be unavailable. It is also the only channel that is
    definitely working, which is why the catalogue is written so that the floor
    alone carries the whole meaning of an effect.
    """

    def __init__(self, floor: FloorService, enabled: bool = True) -> None:
        self._floor = floor
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "floor"

    @property
    def is_available(self) -> bool:
        return self._enabled

    async def begin(self, effect: Effect) -> None:
        self._floor.show_flourish(
            FloorFlourish(
                cue=effect.cue,
                label=effect.label,
                intensity=effect.intensity,
                seconds=effect.seconds,
            )
        )

    async def end(self, effect: Effect) -> None:
        # Clears one field. Whatever the round had on the floor underneath —
        # marker, numbers, a coaching line — is untouched, which is the whole
        # reason the flourish is a layer rather than a mode.
        self._floor.clear_flourish()

    async def aclose(self) -> None:
        return None


class SpeechChannel:
    """Says the name of what happened, if it is worth saying.

    Priority `ANNOUNCEMENT`, which is the tier the speech queue reserved for
    exactly this: something about the round rather than about the swing. It
    outranks coaching, so a birdie cuts off a sentence about the last shot's club
    path — correctly, because the player has stopped listening to that anyway.

    Not perishable, unlike coaching. Two things that happened are two things that
    happened, and a queue that quietly dropped the first because a second arrived
    would lose a hole.
    """

    def __init__(self, speech: SpeechService, enabled: bool = True) -> None:
        self._speech = speech
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "speech"

    @property
    def is_available(self) -> bool:
        return self._enabled and self._speech.is_available

    async def begin(self, effect: Effect) -> None:
        if not effect.say:
            return
        await self._speech.say(
            Utterance(
                text=effect.say,
                priority=SpeechPriority.ANNOUNCEMENT,
                perishable=False,
            )
        )

    async def end(self, effect: Effect) -> None:
        """Nothing. A word already spoken cannot be taken back.

        Deliberately not "stop talking". The visual and the sentence are
        different lengths and there is no reason they should not be: cutting the
        voice off when the label fades would clip the last syllable of a
        two-second effect, and the speech queue is already the thing that
        guarantees two voices never overlap.
        """

        return None

    async def aclose(self) -> None:
        return None


class LightsChannel:
    """Dips the room once, in proportion to the occasion.

    One gesture, and the lighting subsystem owns its own timing once it has been
    handed one — including putting the room back. So `end` does nothing here: a
    flourish that outlives a cancelled effect by a second finishes and restores
    itself, which is a better outcome than a light stopping mid-dip because a
    label faded.
    """

    def __init__(self, lighting: LightingService, enabled: bool = True) -> None:
        self._lighting = lighting
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "lights"

    @property
    def is_available(self) -> bool:
        return self._enabled and self._lighting.is_available

    async def begin(self, effect: Effect) -> None:
        # Linear from a barely-there dip to nearly dark. Linear because the
        # intensity scale is already the perceptual one — it is a judgement about
        # how much of an occasion something is, not a physical quantity — and
        # curving it again would only move where the tiers sit relative to each
        # other, which is a decision the catalogue has already made.
        await self._lighting.play(
            Flourish(
                target=1.0 - MAX_DIP * effect.intensity,
                seconds=effect.seconds,
            ),
            reason=effect.cue,
        )

    async def end(self, effect: Effect) -> None:
        return None

    async def aclose(self) -> None:
        return None
