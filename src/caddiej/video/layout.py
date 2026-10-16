"""Every path the video subsystem is allowed to write.

One place, so that "what does CaddieJ put on my disk" has a single answer and
deleting the media root is guaranteed to be enough.

Clips are grouped by day rather than by session. Session directories would read
better in a file browser, but history is optional and its session row is created
lazily, so video would have to depend on persistence to name a directory.
Grouping by day needs nothing, and grouping by session stays available as a
query: the shot row knows its session and points at the clip.
"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from shutil import rmtree
from uuid import UUID

logger = logging.getLogger(__name__)


class MediaLayout:
    """Where the buffer, the clips and their metadata live."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def buffer_dir(self, camera_id: str) -> Path:
        return self._root / "buffer" / camera_id

    def segment_pattern(self, camera_id: str) -> Path:
        """What FFmpeg is told to write. The counter is its own."""

        return self.buffer_dir(camera_id) / "seg-%06d.mkv"

    def segment_list(self, camera_id: str) -> Path:
        return self.buffer_dir(camera_id) / "segments.csv"

    def clip_dir(self, when: datetime) -> Path:
        return self._root / "clips" / when.strftime("%Y-%m-%d")

    def clip_path(self, when: datetime, shot_id: UUID, camera_id: str) -> Path:
        """A name that sorts into playing order and says what it is.

        The time first so a directory listing is a round in sequence, then
        enough of the shot id to tie the file to a row in history.
        """

        stamp = when.strftime("%H%M%S")
        return self.clip_dir(when) / f"{stamp}-{str(shot_id)[:8]}-{camera_id}.mp4"

    def sidecar_path(self, clip_path: Path) -> Path:
        return clip_path.with_suffix(".json")

    def temporary_path(self, final_path: Path, clip_id: UUID) -> Path:
        """A working name in the same directory as the finished file.

        The same directory because publication is an atomic rename, and a
        rename is only atomic within one filesystem. The leading dot keeps a
        half-written clip out of anything globbing for media.
        """

        return final_path.with_name(f".tmp-{clip_id}{final_path.suffix}")

    def concat_path(self, camera_id: str, clip_id: UUID) -> Path:
        """The list of segments handed to FFmpeg, in the buffer it names."""

        return self.buffer_dir(camera_id) / f".concat-{clip_id}.txt"

    def still_path(self, clip_id: UUID, index: int) -> Path:
        """A working file for one extracted frame.

        Under its own hidden directory rather than beside the clips, because
        these live for the length of one analysis and a directory of clips should
        contain clips. Deleted as soon as the bytes have been read; the directory
        is swept at start-up in case a crash left some behind.
        """

        return self._root / ".frames" / f"{clip_id}-{index:02d}.jpg"

    def prepare_frames(self) -> None:
        self.frames_dir().mkdir(parents=True, exist_ok=True)

    def frames_dir(self) -> Path:
        return self._root / ".frames"

    def clear_frames(self) -> int:
        """Remove stills left behind by a run that did not finish."""

        directory = self.frames_dir()
        if not directory.exists():
            return 0

        removed = 0
        for path in directory.glob("*.jpg"):
            try:
                path.unlink()
            except OSError:
                logger.warning("video_frame_file_kept", extra={"clip_path": str(path)})
            else:
                removed += 1
        return removed

    def prune_clips(self, keep_days: int, today: date) -> int:
        """Delete clip directories older than `keep_days` days.

        By day rather than by file, because that is the unit they are written
        in and a whole day is what somebody deciding they no longer need last
        month's footage means. Nothing here was ever removed: the rolling
        buffer is pruned and the stills are swept, but a finished clip and its
        sidecar stayed for good, so a season of golf filled the disk and the
        first thing to fail was whatever wrote next.

        Zero keeps everything, for a bay that would rather manage this itself.
        """

        directory = self._root / "clips"
        if keep_days <= 0 or not directory.exists():
            return 0

        cutoff = today - timedelta(days=keep_days)
        removed = 0
        for day in sorted(directory.iterdir()):
            if not day.is_dir():
                continue
            try:
                when = date.fromisoformat(day.name)
            except ValueError:
                # Not a day this wrote. Left alone rather than guessed at.
                continue
            if when >= cutoff:
                continue
            try:
                rmtree(day)
            except OSError:
                # A file something still holds open, which on Windows is
                # ordinary. Next start-up finds it again.
                logger.warning("video_clip_day_kept", extra={"clip_path": str(day)})
            else:
                removed += 1
                logger.info("video_clip_day_removed", extra={"clip_path": str(day)})
        return removed

    def relative(self, path: Path) -> Path:
        """A path as it should be stored: relative to the root, if it is inside."""

        try:
            return path.relative_to(self._root)
        except ValueError:
            return path

    def prepare(self, camera_id: str) -> None:
        self.buffer_dir(camera_id).mkdir(parents=True, exist_ok=True)

    def clear_buffer(self, camera_id: str) -> int:
        """Throw away every segment and start the index again.

        Called whenever capture starts, including after a restart, because
        FFmpeg's media clock restarts at zero and its segment counter with it.
        Footage from a previous run cannot be placed in time and its filenames
        are about to be reused, so keeping it would mean serving frames from the
        wrong minute. Losing the buffer on restart is the honest outcome.

        Only the segments and the index, not the directory's contents. A clip
        being extracted at this moment has its segment list in here, and
        deleting that would fail a clip because a camera restarted.
        """

        directory = self.buffer_dir(camera_id)
        if not directory.exists():
            return 0

        removed = 0
        for path in (*directory.glob("seg-*"), self.segment_list(camera_id)):
            if not path.is_file():
                continue
            try:
                path.unlink()
            except OSError:
                logger.warning("video_buffer_file_kept", extra={"clip_path": str(path)})
            else:
                removed += 1
        return removed
