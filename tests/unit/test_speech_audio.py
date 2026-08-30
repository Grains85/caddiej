"""Rendered audio, the WAV it is written as, and the cache that keeps it.

Arithmetic and files. Nothing here loads a model or opens a speaker, so the
awkward cases — a half-written file, a cache over its limit, a directory left
behind by a run that was killed — can be arranged exactly rather than waited for.
"""

from pathlib import Path

import pytest

from caddiej.speech.audio import (
    DEFAULT_SAMPLE_RATE,
    Audio,
    chunks,
    from_wav,
    silence,
    to_wav,
)
from caddiej.speech.cache import AudioCache, voice_key


def a_tone(seconds: float = 1.0) -> Audio:
    """Distinguishable bytes, so a round trip cannot pass by accident."""

    frames = round(seconds * DEFAULT_SAMPLE_RATE)
    return Audio(data=bytes((index % 251, 1)[0] for index in range(frames * 2)))


class TestAudio:
    def test_a_duration_is_derived_from_the_samples(self) -> None:
        assert silence(1.5).duration_seconds == pytest.approx(1.5)
        assert silence(1.5).frame_count == 36_000

    def test_audio_that_is_not_a_whole_number_of_frames_is_refused(self) -> None:
        with pytest.raises(ValueError, match="whole number of frames"):
            Audio(data=b"\x00\x00\x00")

    def test_a_sample_width_this_project_does_not_produce_is_refused(self) -> None:
        """The one place a future engine handing back 24-bit audio can be
        caught before it reaches a speaker as noise."""

        with pytest.raises(ValueError, match="16-bit"):
            Audio(data=b"\x00" * 6, sample_width=3)

    def test_an_empty_utterance_is_legal_and_lasts_no_time(self) -> None:
        empty = Audio(data=b"")
        assert empty.is_empty
        assert empty.duration_seconds == 0.0


class TestWav:
    def test_a_round_trip_returns_exactly_what_went_in(self) -> None:
        original = a_tone(0.25)
        assert from_wav(to_wav(original)) == original

    def test_the_bytes_are_a_riff_file_anything_could_open(self) -> None:
        raw = to_wav(silence(0.1))
        assert raw[:4] == b"RIFF"
        assert raw[8:12] == b"WAVE"

    def test_something_that_is_not_a_wav_is_refused_rather_than_guessed_at(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="not a readable WAV"):
            from_wav(b"this is not audio")


class TestChunks:
    def test_the_pieces_reassemble_into_the_original(self) -> None:
        audio = a_tone(0.5)
        assert b"".join(chunks(audio, 0.05)) == audio.data

    def test_each_piece_is_the_length_asked_for(self) -> None:
        pieces = chunks(silence(1.0), 0.05)
        assert len(pieces) == 20
        # 0.05s of mono 16-bit at 24 kHz.
        assert all(len(piece) == 2400 for piece in pieces)

    def test_a_final_short_piece_is_kept_rather_than_padded(self) -> None:
        """Padding would append silence to every sentence that did not divide
        evenly, which is most of them."""

        pieces = chunks(silence(0.12), 0.05)
        assert [len(piece) for piece in pieces] == [2400, 2400, 960]

    def test_audio_shorter_than_one_piece_is_a_single_piece(self) -> None:
        assert len(chunks(silence(0.01), 0.05)) == 1


class TestVoiceKey:
    def test_the_same_sentence_asks_the_same_question(self) -> None:
        first = voice_key("Square the face.", "bm_george", 1.0, "kokoro")
        second = voice_key("Square the face.", "bm_george", 1.0, "kokoro")
        assert first == second

    def test_spacing_and_case_do_not_make_a_different_sentence(self) -> None:
        plain = voice_key("Square the face.", "bm_george", 1.0, "kokoro")
        untidy = voice_key("  SQUARE   the face. ", "bm_george", 1.0, "kokoro")
        assert plain == untidy

    @pytest.mark.parametrize(
        "changed",
        [
            {"voice": "bm_lewis"},
            {"speed": 1.2},
            {"engine": "other"},
            {"text": "Square the face a little earlier."},
        ],
    )
    def test_anything_that_changes_the_samples_changes_the_key(
        self, changed: dict[str, object]
    ) -> None:
        base = {
            "text": "Square the face.",
            "voice": "bm_george",
            "speed": 1.0,
            "engine": "kokoro",
        }
        assert voice_key(**base) != voice_key(**{**base, **changed})  # type: ignore[arg-type]


class TestMemoryCache:
    async def test_a_line_that_was_rendered_is_handed_back(self) -> None:
        cache = AudioCache()
        audio = a_tone(0.2)
        await cache.put("key", audio)

        assert await cache.get("key") == audio
        assert cache.hits == 1
        assert cache.misses == 0

    async def test_a_line_that_was_never_rendered_is_a_miss(self) -> None:
        cache = AudioCache()
        assert await cache.get("nothing") is None
        assert cache.misses == 1

    async def test_the_cache_is_bounded_by_bytes_rather_than_by_entries(self) -> None:
        """A sentence and a word differ by a hundredfold, so counting entries
        would cap the thing actually being spent at anything at all."""

        one_second = DEFAULT_SAMPLE_RATE * 2
        cache = AudioCache(max_bytes=2 * one_second + 1)
        for index in range(4):
            await cache.put(f"key-{index}", silence(1.0))

        assert cache.held_bytes <= 2 * one_second + 1
        assert len(cache) == 2
        assert await cache.get("key-0") is None
        assert await cache.get("key-3") is not None

    async def test_the_least_recently_used_line_is_the_one_evicted(self) -> None:
        one_second = DEFAULT_SAMPLE_RATE * 2
        cache = AudioCache(max_bytes=2 * one_second + 1)
        await cache.put("old", silence(1.0))
        await cache.put("new", silence(1.0))
        await cache.get("old")
        await cache.put("newest", silence(1.0))

        assert await cache.get("old") is not None
        assert await cache.get("new") is None

    async def test_an_empty_rendering_is_not_kept(self) -> None:
        cache = AudioCache()
        await cache.put("key", Audio(data=b""))
        assert len(cache) == 0


class TestDiskCache:
    async def test_a_line_survives_a_restart(self, tmp_path: Path) -> None:
        first = AudioCache(directory=tmp_path)
        await first.open()
        await first.put("key", a_tone(0.2))

        second = AudioCache(directory=tmp_path)
        await second.open()
        assert await second.get("key") == a_tone(0.2)

    async def test_what_is_written_is_a_wav_somebody_could_play(
        self, tmp_path: Path
    ) -> None:
        cache = AudioCache(directory=tmp_path)
        await cache.open()
        await cache.put("key", silence(0.2))

        written = tmp_path / "key.wav"
        assert written.is_file()
        assert from_wav(written.read_bytes()).duration_seconds == pytest.approx(0.2)

    async def test_no_half_written_file_is_left_where_a_reader_would_find_it(
        self, tmp_path: Path
    ) -> None:
        """Publication is a rename, so a reader finds a whole file or nothing."""

        cache = AudioCache(directory=tmp_path)
        await cache.open()
        await cache.put("key", silence(0.2))

        assert [path.name for path in tmp_path.iterdir()] == ["key.wav"]

    async def test_a_corrupted_entry_is_a_miss_and_is_thrown_away(
        self, tmp_path: Path
    ) -> None:
        """Resynthesising costs a second. Playing a misread file is a burst of
        noise in somebody's living room."""

        (tmp_path / "key.wav").write_bytes(b"not audio at all")
        cache = AudioCache(directory=tmp_path)
        await cache.open()

        assert await cache.get("key") is None
        assert not (tmp_path / "key.wav").exists()

    async def test_the_directory_is_pruned_to_its_limit(self, tmp_path: Path) -> None:
        cache = AudioCache(directory=tmp_path, max_files=3)
        await cache.open()
        for index in range(6):
            await cache.put(f"key-{index}", silence(0.05))

        assert len(list(tmp_path.glob("*.wav"))) <= 3

    async def test_a_directory_left_over_from_a_previous_run_is_brought_back_in_line(
        self, tmp_path: Path
    ) -> None:
        for index in range(8):
            (tmp_path / f"stale-{index}.wav").write_bytes(to_wav(silence(0.05)))

        cache = AudioCache(directory=tmp_path, max_files=2)
        await cache.open()

        assert len(list(tmp_path.glob("*.wav"))) == 2

    async def test_a_cache_directory_that_cannot_be_used_is_simply_not_used(
        self, tmp_path: Path
    ) -> None:
        """An optimisation is never worth refusing to speak over."""

        blocked = tmp_path / "wedged"
        blocked.write_text("this is a file, not a directory")
        cache = AudioCache(directory=blocked)
        await cache.open()

        await cache.put("key", silence(0.1))
        assert await cache.get("key") is not None
