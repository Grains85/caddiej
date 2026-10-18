# Windows validation

[← Documentation index](README.md)

The golf computer has not been validated from the machine that wrote this
guide. Run the commands below on that computer. A passing result on macOS
does not count as a Windows result.

Keep paid vision calls off until the optional step at the end. The image
pipeline does not need an OpenAI key.

Four checks, in this order:

1. **Environment.** The virtual environment, OpenCV 4, MediaPipe, and the
   pose model.
2. **Saved swing.** Evidence on a swing that is already on disk.
3. **Live hardware.** ProTee, GSPro, and OBS, with the coach still not
   calling a model.
4. **Optional paid call.** One live assessment, only after the three above
   pass.

Start in the repository root, in PowerShell. Python 3.12 or newer.

## 1. Clean virtual environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
```

If `py -3.12` is not found, install Python 3.12 and run the same command
again. Use a new `.venv`. An existing environment that already has
`opencv-python` or `opencv-python-headless` can leave two copies of `cv2`.

## 2. Install project dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[vision]" -c constraints\vision.txt
```

This is the whole install. Do not uninstall OpenCV afterwards and do not
install a second OpenCV package. `constraints\vision.txt` pins
`opencv-contrib-python==4.14.0.94` and `mediapipe==0.10.35`, which is the
pair MediaPipe's own dependency can satisfy without selecting OpenCV 5.

## 3. Install and check the pose model

```powershell
.\.venv\Scripts\python.exe scripts\install-pose-model.py
.\.venv\Scripts\python.exe scripts\verify-vision.py --require-model
```

A file that already matches the checksum is left in place. The path is
`models\pose_landmarker_full.task`, or the path in `CADDIEJ_POSE_MODEL` when
that variable is set. The expected SHA-256 is
`5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1`.

`verify-vision.py` must print an OpenCV version starting with `4.`, exactly
one provider (`opencv-contrib-python`), MediaPipe `0.10.35`, and that
checksum. Exit code 1 means the install is not usable. The application does
not download the model during a shot.

## 4. Imports

Covered by the previous command. To repeat it:

```powershell
.\.venv\Scripts\python.exe scripts\verify-vision.py --require-model
```

## 5. Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[vision,test]" -c constraints\vision.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest research\direct_visual_coach\tests
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy
```

The default suite does not require ProTee, GSPro, OBS, or an API key.
Integration tests that need hardware stay skipped unless
`CADDIEJ_RUN_INTEGRATION=1`. Leave that unset for this pass.

## 6. Saved swing

This does not call OpenAI. The shot is in the repository.

```powershell
$env:COACHING_VISION_CALLS = "false"
$env:COACHING_PROVIDER = "none"
.\.venv\Scripts\python.exe scripts\benchmark-evidence.py --runs 3
```

Expect `frames=24`, counts `swing_behind` 20 and `swing_side` 4, and a
`selected_end` that reaches the late part of each camera's timeline. The
first `initialization` line is this process's startup. Run the same command
a second time for a warm start, after the font cache exists. The per-run
`wall` and `pose_seconds` lines are the pipeline with MediaPipe already
loaded.

## 7. Selected frames and stage labels

The benchmark prints them. For each run, read:

- `frames` and `counts`
- `impact` and `confidence`
- `stage swing_behind` and `stage swing_side`, including `unresolved`
- `timeline_end` and `selected_end` for both cameras

Unresolved stages are a reported result. They are not filled in.

## 8. Evidence-processing time

Use the three runs from step 6, not only the fastest. Report initialization
seconds, then for each run `wall`, `decode_seconds`, `motion_seconds`,
`optical_flow_seconds`, `pose_seconds`, `impact_seconds`,
`selection_seconds`, and `total_seconds`.

## 9. Live coach, without a paid call

Confirm the bay plays with the coach idle. In `.env`:

```
COACHING_ENABLED=true
COACHING_PROVIDER=none
COACHING_VISION_CALLS=false
COACHING_EVIDENCE_BUDGET=24
COACHING_EVIDENCE_ALLOCATION=dtl_primary
COACHING_FEEDBACK=observed
SPEECH_ENABLED=false
```

```powershell
$log = "caddiej-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
.\.venv\Scripts\caddiej.exe 2>&1 | Tee-Object -FilePath $log
```

Hit a ball. ProTee and GSPro should continue. In another window:

```powershell
(Invoke-RestMethod http://127.0.0.1:8730/healthz).coaching | ConvertTo-Json
```

`evidence_status` is `not_requested` while vision calls are off. No images
are sent anywhere.

To exercise evidence on a live capture without a network call, stop CaddieJ
and set `COACHING_PROVIDER=scripted` and `COACHING_VISION_CALLS=true`, then
start again. The in-process provider does not call OpenAI. `evidence_status`
should become `ready` after warmup, or `degraded` with a reason when the
pose model or MediaPipe cannot be used. A provider of `none` does not build
a live package, so it is not the live evidence check.

Hardware that this step is there to watch, and that this guide cannot sign
off: OBS screenshots of both replays, the capture timeline on disk, ProTee
staying up when evidence fails, and GSPro staying up when a newer shot
cancels the previous one.

## 10. Logs

```powershell
Select-String -Path .\caddiej-*.log -Pattern "evidence_warmup|reference_evidence_failed|coaching_vision_calls_disabled|swing_capture_completed|swing_capture_diagnostic_timeline"
```

`evidence_warmup` carries `evidence_status`, `evidence_detail`, and
`evidence_warmup_seconds`. A failed package logs
`reference_evidence_failed` with the exception type and message. The next
shot should still produce `swing_capture_completed`.

## Optional paid call

Only after the saved swing and the live hardware check. This spends money.

```
COACHING_PROVIDER=openai
COACHING_VISION_CALLS=true
COACHING_FEEDBACK=observed
COACHING_API_KEY=<the key, in .env, not on the command line>
```

`observed` is one vision call. `worded` adds a second text call. Leave
`COACHING_VISION_CALLS=false` again when the check is done.

## Session diagnostics during a live test

Diagnostics are on unless `.env` sets `DIAGNOSTICS_ENABLED=false`. They do
not call OpenAI and they do not change frame selection or the cue. Confirm
before the first ball:

```powershell
.\.venv\Scripts\caddiej.exe --check-config
```

The Diagnostics block should show Enabled, Retain GPT payloads, Retained
shots `3`, Auto-export latest, and Include images. The Latest ZIP line is
the file that is replaced after each finished shot. Leave
`DIAGNOSTICS_RETAIN_PAYLOADS=true` for a bay test. Those JSON files redact
API keys and do not contain image bytes or `config\ideal_swing.json`. The
selected frames are copied into the shot folder and into the zip.

Stop CaddieJ after the session so the summary is written. Reports are under
`media\diagnostics\<session id>\`:

- `summary.md` and `session.json` for the session
- `shots\<coaching shot id>\report.md` and `report.json` for one shot
- `contact_sheet.jpg` for the frames that shot selected

The coaching shot id is the strike id, the same value as
`SwingCapture.coaching_id`. GSPro's shot id is recorded beside it. It is not
the folder name. The selected frames are under `shots\<id>\images\`. A frame
marked NOT SENT was selected and was not in the model request.

Open `media\diagnostics\latest_shot.zip` after a shot finishes. It is only
that shot: `manifest.json`, `report.json`, `report.md`, `contact_sheet.jpg`,
the copied frames, and the model JSON when payload retention is on. Stop
CaddieJ before copying the zip if Windows still has it open.

A whole-session zip is optional:

```powershell
.\.venv\Scripts\caddiej.exe --export-diagnostics --diagnostics-include-images
```

The command prints the zip path. Add `--diagnostics-output C:\Temp\session.zip`
to choose the file, or `--diagnostics-session <id>` to choose a session.
The zip uses forward-slash names. It skips `.env`.

In `report.md`, read the status first. `success`, `failed`, `skipped`,
`cancelled`, `superseded`, and `unavailable` are separate. A queued speech
line is not spoken audio. Playback is confirmed only when a playback start
was observed. An impact or stage marked estimated is not a confirmed contact
or a confirmed position. A missing duration is unavailable. It is not zero.
