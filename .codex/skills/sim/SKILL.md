---
name: sim
description: Project-local Reolink tracker simulation workflow for this repository. Use when the user asks to run "sim", make a live multi-camera 20-second projection usage simulation, compare tracking coverage, or generate the same 1280x600 projection heatmap video format under ~/Desktop/vom-reolink-videosim.
---

# Reolink Projection Simulation

Use this skill only in `/Users/taeyang/Developer/tools/reolink-tracker`.

## Workflow

1. Keep `config.yaml` as local runtime state. Do not commit it and do not print RTSP credentials.
2. Do not edit `image_points`, `projection_uv`, or `dispatch_uv` unless the user explicitly asks. The current floor calibration is treated as source of truth.
3. Run the bundled script from repo root. The script resolves `config.yaml` to
   the Tauri app runtime config when the app has saved one, so calibration drawn
   in the app is the source of truth for live simulations.

```bash
./.venv/bin/python .codex/skills/sim/scripts/live_projection_usage.py --config config.yaml
```

4. The script records every configured camera for 20 seconds, writes temporary `<camera>.mp4` files, then processes exactly 400 frames at 20 fps into the reference-style video:

```text
~/Desktop/vom-reolink-videosim/live-YYYYMMDD-HHMMSS-20s-usage/
  cam0.mp4
  cam2.mp4
  cam1.mp4
  sim-config.yaml
  summary.json
  preview-frame-100.jpg
  preview-frame-200.jpg
  preview-frame-300.jpg
  all-cameras-20s-projection-usage.mp4
```

5. To replay an existing recorded simulation folder with the current tracker/config logic, pass:

```bash
./.venv/bin/python .codex/skills/sim/scripts/live_projection_usage.py --config config.yaml --input-dir ~/Desktop/vom-reolink-videosim/live-YYYYMMDD-HHMMSS-20s-usage
```

6. Open the output folder or video when the user asks to see it:

```bash
open ~/Desktop/vom-reolink-videosim/live-YYYYMMDD-HHMMSS-20s-usage
open ~/Desktop/vom-reolink-videosim/live-YYYYMMDD-HHMMSS-20s-usage/all-cameras-20s-projection-usage.mp4
```

## Output Contract

Report:
- output video path
- summary path
- `processed_frames`, `fresh_samples`, `source_samples`, `center_overlap_samples`
- grid usage, `handoff_count`, `teleport_reject_count`, `lost_count`

If RTSP decode warnings appear but every configured camera recorded 400 frames, continue and mention the warning only if it affects output.
