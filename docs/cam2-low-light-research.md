# Cam2 Low-Light / Side-Profile Research

## Context

2026-05-12 current field problem:

- The Cheonggyecheon setup has tried three Reolink cameras across one ~40 m projection corridor.
- `cam0` and `cam1` cover the corridor from opposite ends and are the current primary tracking sources.
- The middle camera, `cam2`, should fill the central gap, but it mostly sees side-profile pedestrians.
- At night, Reolink Auto mode can switch the stream to IR black-and-white. That changes the image domain
  seen by the RGB-trained YOLO model and makes side-profile detection worse.
- Spotlight is not available for the immediate field setup, so the next tests must work under low-light
  visible/RGB conditions rather than assuming added illumination.
- Previous weak baseline: `_sub` RTSP stream, `yolo26n.pt`, `imgsz: 640`, low raw confidence around
  `conf: 0.12`.
- The goal is not to make `cam2` create actors directly again. The current implementation keeps
  `cam0`/`cam1` as actor owners and lets `cam2` contribute only auxiliary hand-off sightings.

## Findings

### 1. Camera mode is the first variable to lock

Reolink Day/Night Auto switches between Color and Black & White according to lighting. That is fine for
human monitoring, but it is bad for repeatable model behavior because the input domain can change between
RGB visible-light frames and IR/B&W frames.

For this installation, nightly tests should explicitly record and control:

- Day/Night mode: prefer `Color`, not `Auto`, during detection experiments if the stream remains usable.
- Ambient visible light state. Spotlight is currently unavailable, so do not design the plan around it.
- Whether IR lights are off or at least not driving the image into B&W mode.
- Exact camera model and firmware, because spotlight and threshold settings vary by model.

For Duo 2 PoE specifically, Reolink documents both spotlight night vision and infrared night vision. That
means the ideal path would be visible-light color night vision. In this field setup, the practical path is
low-light RGB/Color with existing ambient light, then software/model mitigation.

### 2. Color mode without enough visible light is not automatically better

Forcing Color is only useful if there is enough visible light for the sensor. If the frame becomes dark,
noisy, or motion-blurred, YOLO may still miss side-profile pedestrians. Because spotlight is not available,
the field test should compare:

1. Auto mode, actual night behavior.
2. Forced Color, spotlight off.
3. Forced Color with any existing ambient/projector light state held constant.
4. If the camera exposes threshold controls, a setting that delays or prevents B&W switching without
   introducing unusable noise.

The expected best candidate is not "Color at any cost"; it is stable low-light RGB input with enough
contrast around the side-profile body silhouette. If Color is too dark, software/model changes must be
validated against the actual low-light frames rather than assumed to help.

### 3. Current miss is likely from resolution plus model capacity

The current combination is intentionally light, but it is the weakest possible case for central
side-profile detection:

- `_sub` stream reduces the pixel budget before YOLO sees the person.
- `imgsz: 640` further resizes a thin distant silhouette.
- `yolo26n.pt` is the smallest model and has less recall on side-profile / partially occluded people.
- The middle of the 40 m projection is near the edge / far region of `cam0` and `cam1`, so people are
  small and oblique exactly where the hand-off matters.

Immediate hypothesis:

- If increasing `imgsz` and model capacity makes central side-profile detections visible, keep cam2
  work separate and evaluate whether 2-camera operation is enough.
- If `imgsz=1280 + yolo26s.pt` still misses the same frames, the problem is mostly camera geometry/input
  domain, so the next comparison is whether `cam2` auxiliary sightings reduce central hand-off drops.

### 4. Fusion cannot recover detections that do not exist

`cam2` as an auxiliary confirmer is only useful if it produces intermittent but spatially plausible person
sightings. If side-profile pedestrians are not detected at all, `role: auxiliary` and `aux_match_*` logic
will not fix the central gap by itself.

Decision rule:

- If `cam2` sees side-profile people in raw YOLO at least intermittently, keep auxiliary sighting enabled
  and tune the matching radius/time window conservatively.
- If raw YOLO stays mostly empty, fix input/model first: `imgsz`, model size, ROI/crop, CLAHE, then
  fine-tune. Do not expect fusion to invent observations.

### 5. CLAHE is a follow-up preprocessing experiment

CLAHE is a local contrast enhancement method with two important knobs: clip limit and tile grid. It can
help low-light contrast, but bad settings can amplify noise. Use it as a controlled A/B test, not as a
guaranteed improvement.

Implementation direction:

- Apply CLAHE only before YOLO inference.
- Prefer LAB L-channel enhancement so color channels remain stable.
- Start with `clip_limit: 2.0`, `tile_grid: [8, 8]`.
- Compare raw YOLO side-profile detections and false positives before touching fusion thresholds.

Do not implement CLAHE before the `imgsz/model` A/B unless the frame is visibly contrast-limited and the
simple config-only change has already been measured. CLAHE changes the pixel distribution and can make
attribution harder if mixed into the first run.

### 6. Site footage fine-tuning is the likely durable fix

The weak case is very specific: side-profile pedestrians, wide-angle camera, low-light corridor, small
bboxes, projector/background patterns, and reflective floor. Generic COCO person weights will not be
optimized for this domain.

Recommended dataset:

- 200-500 frames from `cam2`, sampled across:
  - forced Color without spotlight if usable,
  - Auto/B&W for negative comparison,
  - daytime or brighter reference if available.
- Label all visible people, especially side-profile and partially occluded pedestrians.
- Keep a hold-out clip that is never used for training.
- Train from the current pretrained YOLO weight, then compare baseline vs fine-tuned model on the same
  hold-out clips.

Fine-tune success metric should be side-profile missed-rate reduction, not just global mAP.

## Proposed Experiment Order

### Immediate plan

The agreed next move is **not** main stream first. Main stream can be the largest single improvement if
decode works, but it introduces H.265 / decode / bandwidth risk. The current field candidate uses `_sub`
with `imgsz: 1280`, `model: yolo26s.pt`, and cam2 as an auxiliary confirmer.

1. **Config-only A/B: `imgsz` + model capacity**
   - Current field candidate: `imgsz: 1280`.
   - Current field candidate: `model: yolo26s.pt`.
   - Keep stream as `_sub`.
   - Keep `conf` and detection filter settings otherwise stable for the first run.
   - Run one night/content cycle and inspect tracker FPS/OSC rate plus central side-profile detection.
   - If FPS drops below roughly 15 FPS or track IDs become less stable because of latency, fallback to
     `imgsz: 960` before changing architecture.

2. **Attribution checkpoint**
   - If `imgsz=1280 + yolo26s.pt` improves central detection enough, stay on 2-camera operation and tune
     thresholds only minimally.
   - If it does not improve central side-profile misses, do not spend much time on `hold_*` or relaxed
     thresholds. Move to cam2 auxiliary.

3. **Cam2 auxiliary confirmer**
   - Use `docs/cam2-auxiliary-direction.md` and run `cam2` with `tracking_enabled: true`,
     `role: auxiliary`.
   - Be prepared to lower `imgsz` or model size if 3-camera load pushes latency too high.
   - Verify against the 2-camera `imgsz/model` baseline, not against the old `640+n` baseline.

### 2026-05-12 replay checkpoint

The existing folder `~/Desktop/vom-reolink-videosim/live-20260511-204813-20s-usage` was replayed with
the current runtime config: `_sub`, `imgsz: 1280`, `model: yolo26s.pt`, `cam2 role: auxiliary`.

Result:

- processed frames: 400.
- accepted primary events: `cam0=144`, `cam1=1`.
- accepted auxiliary events:
  - strict first replay: `cam2=0`.
  - cam2 `conf: 0.08`, `body_catch_min_confidence: 0.08`, `auxiliary_confirm_hits: 1`: `cam2=2`.
  - same plus cam2-only CLAHE: `cam2=4`.
- accepted center auxiliary events: still `0`.
- `handoff_count=1`, `lost_count=5`.

Interpretation: in this low-light clip, cam2 does not produce accepted side-profile sightings, so
auxiliary fusion cannot yet improve the central hand-off. The next experiment should target cam2 input
quality or raw detection recall before widening auxiliary matching thresholds.

### Later options

- `_main` stream: keep as a later experiment only if `_sub + 1280 + s` is insufficient and decode risk is
  acceptable. Try one camera first.
- `relaxed_presence_points`: useful only if raw boxes exist but are being filtered out. It cannot help if
  YOLO emits no person box.
- CLAHE: implemented as `preprocessing.clahe` and currently used only on `cam2`; it improves accepted
  auxiliary event count slightly on the replay clip but does not solve center side-profile misses by itself.
- Fine-tune: use when side-profile misses persist after camera/config/auxiliary attempts.

## Implementation Guardrails

- Do not make `cam2` primary again just because it has a region. It should not create new `gid`s unless a
  later field decision explicitly reverses the auxiliary contract.
- Do not mix CLAHE or `_main` stream changes into the first `imgsz/model + cam2 auxiliary` run. Keep
  attribution clean enough to tell whether latency, raw detection, or auxiliary matching is the bottleneck.
- Do not switch to `_main` stream as the first fix; it adds decode/bandwidth risk and can obscure whether
  model/input-size alone solved the problem.
- Do not tune `hold_boundary_margin_uv` to hide central misses. It is for projection edge hold behavior.
- Do not widen `image_points` to catch bodies. Use `body_catch_points` for body bbox rescue and keep
  floor homography accurate.
- Do not change TouchDesigner primary OSC payload order. Add metadata only as additive streams.
- Keep Reolink credentials and real device IPs out of git.

## Sources

- Reolink Day/Night modes: https://support.reolink.com/hc/en-us/articles/360004687494
- Reolink web Day/Night modes: https://support.reolink.com/articles/17195336965529-How-to-Change-Day-and-Night-Mode-via-Web-Browsers/
- Reolink IR-cut behavior: https://support.reolink.com/hc/en-us/articles/900000495883-How-Does-IR-cut-Work/
- Reolink Duo 2 PoE specs: https://reolink.com/us/product/reolink-duo-poe/
- Reolink spotlight support: https://support.reolink.com/hc/en-us/articles/10446659667097-Which-Cameras-Support-the-Spotlight/
- Reolink ColorX spotlight behavior: https://support.reolink.com/hc/en-us/articles/22657927550361-Introduction-to-the-Spotlight-of-Reolink-ColorX-Series/
- OpenCV CLAHE docs: https://docs.opencv.org/4.x/javadoc/org/opencv/imgproc/CLAHE.html
- CLAHE hyperparameter discussion: https://jivp-eurasipjournals.springeropen.com/articles/10.1186/s13640-019-0445-4
- Ultralytics custom training overview: https://docs.ultralytics.com/
- Ultralytics dataset format: https://academy.ultralytics.com/courses/train-your-first-yolo/prepare-a-dataset
- Low-light detection survey direction via ExDark/DL-YOLO: https://www.techscience.com/cmc/v87n2/66579/html
