# Swing video

[← Documentation index](README.md)

> **Not yet run against a real camera.** The pipeline is developed against
> FFmpeg's own generated picture and a fake FFmpeg. See
> [Project status](../README.md#project-status).

CaddieJ can keep a camera pointed at the mat and save a clip of every swing. It
is off by default, and needs [FFmpeg](https://ffmpeg.org/) on `PATH`:

```shell
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian, Ubuntu
winget install ffmpeg        # Windows
```

FFmpeg is run as a subprocess and is never bundled or linked, so whichever build
you install stays under its own licence and CaddieJ stays under
[MIT](../LICENSE).

## Trying it without a camera

Nothing needs to be plugged in to see the whole thing work. FFmpeg can generate
its own picture, and CaddieJ treats that exactly as it treats a camera:

```shell
VIDEO_ENABLED=true GSPRO_MODE=simulate caddiej
```

That plays a scripted round against CaddieJ's own fake GSPro and writes a clip
per shot under `media/clips/<date>/`. The generated picture has a running
timestamp drawn into it, so a clip can be checked against its metadata by eye.

## Using a real camera

Find the device, then name it:

```shell
ffmpeg -f avfoundation -list_devices true -i ""     # macOS
v4l2-ctl --list-devices                             # Linux
ffmpeg -f dshow -list_devices true -i dummy         # Windows
```

```shell
VIDEO_ENABLED=true \
VIDEO_CAMERA_KIND=avfoundation \
VIDEO_CAMERA_DEVICE=0 \
VIDEO_CAMERA_FPS=60 \
caddiej
```

Use `v4l2` on Linux with a device node (`/dev/video0`), or `dshow` on Windows
with the friendly name from that listing. The `dshow` argument vector is
implemented and covered by unit tests; CI has no physical camera, and the
Windows software path has not yet been run against a real DirectShow device. On a machine with a hardware encoder,
`VIDEO_ENCODER=h264_videotoolbox` (macOS), `h264_nvenc` (NVIDIA) or `h264_qsv`
(Intel) leaves the CPU to the golf.

On macOS the first capture triggers a Camera permission prompt for the terminal
you started CaddieJ from. If no prompt appears and capture fails immediately,
grant it under **System Settings → Privacy & Security → Camera**.

A higher frame rate matters more than resolution for a swing: 720p at 120 fps
tells you far more than 4K at 30. `/healthz` reports whether the camera is
recording, how much footage is buffered, and how many clips have been written,
which is the quickest way to tell a wrong device index from a wrong window.

## How it works

FFmpeg records continuously into one-second segments and CaddieJ keeps the last
thirty seconds of them. When a shot completes, a clip is cut from the segments
around impact by copying them — nothing is re-encoded — and a `.json` file is
written beside it saying where impact falls, what window was asked for, what was
actually captured, and how much to trust the timing.

Impact timing is an estimate unless it has been measured. Two delays sit between
the ball being struck and the frame being written: the camera's own latency, and
how long the launch monitor took to report the shot. Set
`VIDEO_CAMERA_LATENCY_SECONDS` and `VIDEO_SHOT_REPORT_LAG_SECONDS` once measured;
until then the clip is deliberately cut wide and says so.

Filming never delays play. Clips are cut by a separate worker after the shot, and
a camera that cannot be opened, disappears mid-round, or fills the disk is
reported through `/healthz` and an event, and changes nothing else. Everything
video writes lives under `VIDEO_MEDIA_ROOT`, so deleting that directory is
always enough — and the footage stays on the machine that recorded it. Nothing
uploads anything.
