# Product Context

## Purpose

`reolink-tracker` turns one or more Reolink RTSP camera feeds into low-latency OSC person-position messages for interactive media systems such as TouchDesigner, Max, or Unity.

The current installation target is a corridor-style projection setup where camera detections are transformed into a shared projection UV coordinate space.

## Users

- Installation operator calibrating Reolink cameras on site.
- Creative technologist receiving OSC in TouchDesigner/Max/Unity.
- Developer extending tracking, calibration, or cross-camera behavior.

## MVP

- Read Reolink sub-stream RTSP feeds with low latency.
- Detect and track people with Ultralytics YOLO + BoT-SORT or ByteTrack.
- Convert per-camera image detections into shared projection UV coordinates.
- Emit stable OSC messages for track positions, active IDs, and counts.
- Provide an operator preview window for camera/region validation.

## Non-Goals For v1

- Rendering visuals.
- Cross-camera identity fusion.
- Stereo/depth reconstruction.
- Cloud service operation.
- Persisting analytics data.

## Success Criteria

- The tool can run locally against live Reolink cameras.
- TouchDesigner receives per-person OSC updates with useful latency.
- Camera regions can be calibrated without changing code.
- Private camera credentials remain local-only.
