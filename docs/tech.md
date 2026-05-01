# Technical Context

## Runtime

- Python 3.12 is the expected runtime.
- PyTorch/Ultralytics wheels are not expected to be reliable on Python 3.14.
- The project is intentionally single-process with one RTSP reader thread per camera.

## Main Files

- `tracker.py`: configuration loading, RTSP grabbers, YOLO tracking, OSC dispatch, main loop.
- `region.py`: projection/region data models, homography construction, UV validation, small self-test.
- `viewer.py`: OpenCV operator preview and projection UV canvas.
- `config.example.yaml`: shareable configuration shape.
- `config.yaml`: local-only runtime configuration with real camera URLs.

## Architecture Notes

- `OPENCV_FFMPEG_CAPTURE_OPTIONS` must be set before importing `cv2`.
- Each `CamWorker` owns a dedicated `YOLO` instance because tracker state is per model instance.
- OSC primary output is shared projection UV, not raw image-space coordinates.
- `dispatch_uv` slices should not overlap for cameras that share a projection, otherwise counts can be inflated.

## Verification

Lightweight checks:

```bash
python -m py_compile tracker.py region.py viewer.py
```

Runtime checks require the actual cameras and local `config.yaml`:

```bash
python tracker.py
python tracker.py --show
```
