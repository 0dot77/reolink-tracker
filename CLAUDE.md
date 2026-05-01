# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two-Reolink-camera person-tracking pipeline for an interactive installation. Each camera streams RTSP → YOLO26 person detection + BoT-SORT tracking (defaults; ByteTrack still selectable) → OSC. Designed as the sensor side of a TouchDesigner / Max / Unity interactive system; this repo never renders the visuals, only emits per-track coordinates over OSC.

Project context (not in code): the installation work it feeds into is the 2026 청계천 미디어아트 전시 (봄랩, 청계천 박물관). Reference idea is ALTEMY + risa kagami's *Inter-Embodiment* at WHAT MUSEUM (YOLO + Reolink + TouchDesigner, multi-feed trajectory reconstruction). The Obsidian vault under `~/Documents/taeyang/01_Projects/봄랩/청계천 박물관/` has the planning notes — `청계천 5월 컨텐츠 기술 정리 1차.md` covers the camera-zoning approach (overlap two Reolinks across a ~40 m corridor, emit zone-level occupancy rather than precise foot tracking).

## Data model: shared projection UV

Detection runs per-camera, but the coordinates emitted to TD are in a **shared
projection coordinate space** identified by `projection_id`. The pixel-image
frame of each camera is no longer the wire format.

Each camera in `config.yaml` declares one or more `regions[]`. A region holds:

- `image_points` — the four pixels in this camera's frame that delimit the
  region (clicked in projection-UV order, see below).
- `projection_id` — which shared projection this region belongs to.
- `projection_uv` — the UV slice of that projection that the four
  `image_points` cover (e.g. `[0.0, 0.0, 0.55, 1.0]` for "left 55% of the
  corridor"). A homography is built from `image_points` ↔ the four corners of
  this UV rectangle.
- `dispatch_uv` (optional, defaults to `projection_uv`) — the UV slice this
  camera is **authoritative** for. Tracks whose foot pixel projects outside
  `dispatch_uv` are detected and drawn but not emitted on OSC.

Two cameras pointing at the same physical floor projection share one
`projection_id`; their `dispatch_uv` slices should be **non-overlapping** so a
single person produces one OSC stream at a time and `/proj/<id>/count` is not
inflated. Cross-camera ID fusion in the overlap region is deferred to v2.

Click order during region drawing follows projection-UV orientation
(top-left → top-right → bottom-right → bottom-left), **not camera image
orientation**. This is what makes the face-to-face mirrored two-camera setup
work: each operator clicks the same world-side corner first, so the resulting
homographies map both feeds into the same UV frame even though one camera sees
the corridor mirrored.

## Setup and run

```bash
# Python 3.12 specifically — PyTorch wheels are NOT stable on 3.14 (stated in README).
uv venv -p python3.12
source .venv/bin/activate
uv pip install -r requirements.txt    # plain pip works too

python tracker.py                      # headless, OSC only
python tracker.py --show               # preview window per camera; q or Esc to quit
python tracker.py --config foo.yaml    # alt config
python tracker.py --model yolov8s.pt --device cpu   # CLI overrides win over config
```

First run downloads `yolo26n.pt` into the working directory. Model files (`*.pt`, `runs/`) are gitignored.

There are no tests, lint config, or build step. Validation is empirical: run with `--show` against the live cameras and watch the OSC stream in TouchDesigner.

## Architecture

Single-process, thread-per-camera. Three layers:

1. **`FrameGrabber(threading.Thread)`** — one per camera. Drains the RTSP stream as fast as cv2 allows, keeping only the latest frame under a lock; older frames are dropped. Reconnects with exponential backoff (1→10 s) on read failure. Detection never blocks decode and never queues stale frames.

2. **`CamWorker`** — one per camera, runs on the main loop. Holds its own `YOLO` instance because ByteTrack state is per-model: sharing one YOLO across cameras would cross-contaminate track IDs. Track IDs are stable per camera for as long as the track holds, but **independent across cameras** — there is no cross-camera identity. Emits `lost` events by diffing `current_ids` against `last_ids`.

3. **Main loop** (`main()`) — polls each grabber's `(frame, idx)`, skips when `idx` hasn't advanced (no new frame), and calls `worker.step()`. Sleeps 5 ms when no camera has new data. Prints per-camera fps every 2 s. SIGINT/SIGTERM set `stop_flag` for clean shutdown.

### RTSP latency contract

`OPENCV_FFMPEG_CAPTURE_OPTIONS` is set **at module import time, before `import cv2`** (`tracker.py:25-28`). Moving this lower silently disables the low-latency flags. The combination — `rtsp_transport=tcp`, `nobuffer`, `low_delay`, `max_delay=500ms`, `reorder_queue_size=0`, plus `CAP_PROP_BUFFERSIZE=1` — gives ~200–400 ms glass-to-OSC. Lowering further requires dropping the keyframe interval to 1 s in Reolink Client.

### OSC schema

Primary channel is keyed by `projection_id` and `cam_name`, not by per-camera
prefix. Pixel coords (`u_px, v_px`) are appended when the projection has
`pixel_size` set.

| Address | Args | When |
|---|---|---|
| `/proj/<projection_id>/cam/<cam>/track/<id>` | `u, v, conf` (+ `u_px, v_px` when `projections[i].pixel_size` is set) | every frame the foot is in `image_points` polygon AND in `dispatch_uv` AND passes `min_bbox_height_px` |
| `/proj/<projection_id>/cam/<cam>/track/<id>/lost` | none | once when track ends or leaves dispatch |
| `/proj/<projection_id>/count` | int | every frame, sum across cameras (v1 does not de-dup; non-overlapping `dispatch_uv` is the v1 contract) |
| `/proj/<projection_id>/cam/<cam>/count` | int | every frame |
| `/proj/<projection_id>/cam/<cam>/active` | list of ids | every frame, only if non-empty |

`(u=0, v=0)` is the top-left of the projection; `(u=1, v=1)` is the bottom-right.

Legacy image-space messages (`<cam_prefix>/track/<id>` with `cx, cy, w, h, conf`,
plus `count`/`active`/`lost`) are emitted only when `osc.legacy_image_space: true`.
Default is off.

## Config gotchas

- **Password URL-encoding** in `config.yaml` `url:` is mandatory. `! → %21`, `# → %23`, `@ → %40`, `: → %3A`. Keep real camera credentials local-only.
- **Use the `_sub` stream**, not `_main`. Many Reolink models are H.265 main + H.264 sub; OpenCV's bundled FFmpeg often won't decode H.265.
- **Username is `admin`**, not the device friendly name (401 otherwise).
- **Device default**: `mps` on macOS, else `cpu`. If ultralytics throws `MPS not implemented`, either upgrade ultralytics or set `device: cpu` in config.
- **Tracker tuning**: default is `botsort.yaml`. For more throughput at the cost of smoothness, fall back to `bytetrack.yaml`. For occlusion-heavy scenes (corridor crowds, umbrellas) where IDs still flicker on BoT-SORT, escalate to `strongsort.yaml`. Note `iou` is recorded in config but inert under YOLO26 (NMS-free).

## When extending

- **Adding a third camera**: append a `cameras:` entry with a unique `name` and `osc_prefix`, plus its own `regions[]`. If the new camera covers the same physical projection as an existing one, reuse that `projection_id` and pick a `dispatch_uv` slice that does not overlap any other camera's `dispatch_uv` for the same projection. The grabber/worker pair is created automatically; no other code changes needed.
- **Cross-camera fusion / depth from stereo**: intentionally deferred to v2 (see the plan). The shared-UV data model already gives every camera a common coordinate frame, so v2 fusion is purely an ID-matching problem in UV space — not a coordinate-system problem. Any global identity layer belongs above `CamWorker`, consuming UV outputs or the OSC stream; do not reach into `CamWorker.step` to share state across cameras.
- **Changing OSC schema**: receivers (TouchDesigner patches) live outside this repo; coordinate before editing addresses or arg order.
- **The `r.plot() if True else frame` in `step()` (`tracker.py:145`)** always returns the annotated frame. The dead branch is intentional scaffolding for a future "raw frame" path.
