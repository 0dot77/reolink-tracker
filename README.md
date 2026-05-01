# reolink-tracker

Two-camera Reolink → YOLO26 person detection + BoT-SORT tracking → OSC.
Designed for low-latency interactive installations (TouchDesigner, Max, Unity, ...).
ByteTrack is still selectable as a faster alternative.

## Setup

PyTorch wheels are stable on Python 3.11–3.12. Don't use 3.14.

```bash
cd /Users/taeyang/Developer/tools/reolink-tracker
uv venv -p python3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

(`pip install -r requirements.txt` works too if you don't have `uv`.)

Create a local config from the sanitized example:

```bash
cp config.example.yaml config.yaml
```

Then edit `config.yaml` with real camera URLs, passwords, model, and OSC target.
`config.yaml` is intentionally gitignored because it can contain RTSP
credentials.

First run downloads `yolo26n.pt` into the working directory. YOLO26 was released
by Ultralytics on 2026-01-14: NMS-free end-to-end inference, ~+43% CPU speed,
and a small-target STAL head that helps far-end people in long corridors.

## Run

```bash
# headless (OSC only)
python tracker.py

# with preview windows (one per camera; press q to quit)
python tracker.py --show
```

Edit `config.yaml` for cameras, model, OSC target. Restart after editing.

## AI / GitHub workflow

This repo keeps project context in versioned markdown so AI coding tools do not
depend on chat history alone:

- `docs/product.md` — product goal, users, MVP, non-goals
- `docs/tech.md` — architecture and verification notes
- `docs/ai-rules.md` — stable instructions for AI agents
- `docs/decisions.md` — important decisions and reversals

Use GitHub Issues for small tasks and Pull Requests for review, even when
working solo. The PR diff becomes the place to review AI-generated changes
before they reach `main`.

## OSC schema

Coordinates are emitted in a **shared projection UV space**, identified by
`projection_id`. Two cameras pointing at the same physical floor projection share
one `projection_id`; their `(u, v)` values are directly comparable.

| Address | Args | When |
|---|---|---|
| `/proj/<projection_id>/cam/<cam_name>/track/<id>` | `u, v, conf` (+ `u_px, v_px` when the projection has `pixel_size`) | every frame the track's foot is inside the camera's region polygon and inside its `dispatch_uv` slice |
| `/proj/<projection_id>/cam/<cam_name>/track/<id>/lost` | none | once when an id disappears or leaves dispatch |
| `/proj/<projection_id>/count` | int | every frame, sum across cameras |
| `/proj/<projection_id>/cam/<cam_name>/count` | int | every frame |
| `/proj/<projection_id>/cam/<cam_name>/active` | list of ids | every frame, only if non-empty |

`(u=0, v=0)` is the top-left of the projection; `(u=1, v=1)` is the bottom-right.
`id` is stable per camera as long as the track holds; ids are still independent
across cameras in v1 (cross-camera fusion is deferred to v2).

Legacy image-space messages (`<cam_prefix>/track/<id>` with `cx, cy, w, h, conf`)
are still emitted when `osc.legacy_image_space: true` is set in `config.yaml`.
Default is off.

## TouchDesigner receiver

- Add an **OSC In DAT** (or **OSC In CHOP** for numeric streams).
- Network Port: `7000` (or whatever you set in `config.yaml`).
- Use the address pattern `/proj/corridor/cam/*/track/*` with wildcards to accept
  all cameras and all tracks for a given projection.

## Calibration / region drawing

Run with `python tracker.py --show`, focus a camera tile, then press `d` to
draw a 4-point region on that camera. Click order is **projection-UV
orientation**: top-left → top-right → bottom-right → bottom-left as seen on the
projection, NOT as seen in the camera image. After the four clicks, the console
prompts for `projection_id`, `projection_uv` (the UV slice these four pixels
cover, e.g. `0,0,0.55,1`), and an optional `dispatch_uv` (defaults to
`projection_uv` if blank). The region is written back to `config.yaml`.

Because clicks follow world/UV order rather than image order, the two cameras
in a face-to-face mirrored corridor setup still produce coherent shared
coordinates without any per-camera flip flag.

## Network setup for two cameras

Pick one. The tool only cares about the URLs in `config.yaml`.

1. **Both into a router** (recommended) — plug both into the iPTIME LAN ports;
   they get DHCP IPs in `172.30.1.x`. Mac talks over Wi-Fi or its LAN port.
   Easiest, most reliable.

2. **Both direct via switch** — a small unmanaged 5-port switch on the Mac's
   USB-Ethernet adapter. Run a DHCP server on the Mac (System Settings →
   General → Sharing → Internet Sharing) so both cameras get IPs.

3. **One direct, one via Wi-Fi** — works but mixed paths add complexity.

## Latency tuning

The included FFmpeg flags (`nobuffer`, `low_delay`, `max_delay=500ms`,
`reorder_queue=0`) plus `BUFFERSIZE=1` give roughly 200–400 ms glass-to-OSC
latency on the sub stream. To go lower, drop the camera's keyframe interval to
1 second in Reolink Client → Display → Stream → Frame Interval.

## Common issues

- **Black preview / `read failed`** — check the URL with `ffprobe`:
  `ffprobe -rtsp_transport tcp 'rtsp://admin:%21pass@1.2.3.4:554/h264Preview_01_sub'`
- **401 Unauthorized** — username is `admin`, not the device friendly name.
- **`h264Preview_01_main` won't decode** — many Reolink models are H.265 main + H.264 sub. Use `_sub`.
- **MPS `not implemented` warning** — upgrade ultralytics, or set `device: cpu` in config.
- **Track IDs jumping** — switch tracker to `bytetrack.yaml` (faster, less smooth) or escalate to StrongSORT (best ID stability under occlusion). Default is `botsort.yaml`.
