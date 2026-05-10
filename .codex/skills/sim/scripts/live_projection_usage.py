#!/usr/bin/env python3
"""Record live configured cameras and render projection usage simulation."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000|reorder_queue_size;0|stimeout;5000000",
)

import cv2
import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

import tracker as tr  # noqa: E402
from fusion import PersonTracker  # noqa: E402


VIDEO_W = 1280
VIDEO_H = 600
CAM_W = 640
CAM_H = 240
PROJ_W = 1280
PROJ_H = 360
GRID_W = 24
GRID_H = 8


class NullOSC:
    def send_message(self, *_args: object, **_kwargs: object) -> None:
        return None


def gid_color(gid: int) -> tuple[int, int, int]:
    rng = np.random.default_rng(gid * 1009)
    return tuple(int(v) for v in rng.integers(80, 255, 3))


def camera_color(index: int) -> tuple[int, int, int]:
    colors = [
        (255, 190, 40),
        (80, 255, 80),
        (255, 110, 110),
        (110, 190, 255),
        (230, 110, 255),
        (255, 255, 120),
    ]
    return colors[index % len(colors)]


def uv_to_px(u: float, v: float) -> tuple[int, int]:
    x = int(round(max(0.0, min(0.999, float(u))) * PROJ_W))
    y = int(round(max(0.0, min(0.999, float(v))) * PROJ_H))
    return min(PROJ_W - 1, x), min(PROJ_H - 1, y)


def record_live_clips(
    cfg: dict[str, Any],
    out_dir: Path,
    seconds: float,
    writer_fps: float,
) -> dict[str, dict[str, Any]]:
    target_frames = int(seconds * writer_fps)
    caps: list[cv2.VideoCapture | None] = []
    writers: list[cv2.VideoWriter | None] = []
    results: dict[str, dict[str, Any]] = {}
    cameras = cfg.get("cameras", [])

    for idx, cam in enumerate(cameras):
        name = cam.get("name", f"cam{idx}")
        cap = cv2.VideoCapture(cam.get("url"), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            results[name] = {"ok": False, "error": "open failed"}
            caps.append(None)
            writers.append(None)
            continue

        frame = None
        deadline = time.time() + 6.0
        while time.time() < deadline:
            ok, img = cap.read()
            if ok:
                frame = img
                break
            time.sleep(0.05)
        if frame is None:
            results[name] = {"ok": False, "error": "initial read failed"}
            cap.release()
            caps.append(None)
            writers.append(None)
            continue

        h, w = frame.shape[:2]
        path = out_dir / f"{name}.mp4"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), writer_fps, (w, h))
        if not writer.isOpened():
            results[name] = {"ok": False, "error": "writer open failed", "path": str(path)}
            cap.release()
            caps.append(None)
            writers.append(None)
            continue

        writer.write(frame)
        results[name] = {
            "ok": True,
            "path": str(path),
            "width": w,
            "height": h,
            "writer_fps": writer_fps,
            "frames": 1,
        }
        caps.append(cap)
        writers.append(writer)

    if len(results) != len(cameras) or not all(item.get("ok") for item in results.values()):
        for cap in caps:
            if cap is not None:
                cap.release()
        for writer in writers:
            if writer is not None:
                writer.release()
        return results

    start = time.time()
    next_tick = start + 1.0 / writer_fps
    while min(results[name]["frames"] for name in results) < target_frames:
        now = time.time()
        if now < next_tick:
            time.sleep(min(0.005, next_tick - now))
            continue
        next_tick += 1.0 / writer_fps
        for cap, writer, cam in zip(caps, writers, cameras):
            if cap is None or writer is None:
                continue
            name = cam.get("name")
            if results[name]["frames"] >= target_frames:
                continue
            ok, frame = cap.read()
            if not ok:
                results[name]["read_failures"] = results[name].get("read_failures", 0) + 1
                continue
            writer.write(frame)
            results[name]["frames"] += 1

    elapsed = max(time.time() - start, 1e-6)
    for cap in caps:
        if cap is not None:
            cap.release()
    for writer in writers:
        if writer is not None:
            writer.release()
    for item in results.values():
        if item.get("ok"):
            item["measured_fps"] = round(item["frames"] / elapsed, 2)
    return results


def make_sim_config(
    base_cfg: dict[str, Any],
    record_results: dict[str, dict[str, Any]],
    imgsz: int,
    conf: float,
) -> dict[str, Any]:
    cfg = copy.deepcopy(base_cfg)
    cfg["imgsz"] = imgsz
    cfg["conf"] = conf
    cfg.setdefault("fusion", {})["max_update_jump_uv"] = 0.08
    cfg.setdefault(
        "detection_filter",
        {
            "enabled": True,
            "min_confidence": 0.28,
            "min_bbox_height_px": 42,
            "min_bbox_area_px": 900,
            "min_aspect_h_over_w": 1.15,
            "max_aspect_h_over_w": 5.8,
            "max_width_over_height": 1.05,
            "projection_inner_margin_uv": 0.0,
            "confirm_hits": 3,
            "confirm_window_s": 0.8,
        },
    )
    cameras = []
    for idx, cam in enumerate(cfg.get("cameras", [])):
        name = cam.get("name", f"cam{idx}")
        if name not in record_results or not record_results[name].get("ok"):
            continue
        cam["url"] = record_results[name]["path"]
        cameras.append(cam)
    cfg["cameras"] = cameras
    return cfg


def resolve_processing_params(cfg: dict[str, Any], args: argparse.Namespace) -> tuple[int, float]:
    imgsz = int(args.imgsz) if args.imgsz is not None else int(cfg.get("imgsz", 640))
    conf = float(args.conf) if args.conf is not None else float(cfg.get("conf", 0.35))
    return imgsz, conf


def parse_runtime(cfg: dict[str, Any]) -> tuple[dict[str, Any], PersonTracker, list[tr.CamWorker]]:
    projections = tr._parse_projections(cfg)
    cameras = tr._parse_cameras(cfg, projections)
    detection_filter = tr._parse_detection_filter(cfg)
    tr._validate_dispatch_overlaps(cameras)
    fusion = cfg.get("fusion", {}) or {}
    person_tracker = PersonTracker(
        hand_off_window_s=float(fusion.get("hand_off_window_s", 2.5)),
        match_uv_radius=float(fusion.get("match_uv_radius", 0.05)),
        velocity_alpha=float(fusion.get("velocity_alpha", 0.3)),
        position_alpha=float(fusion.get("position_alpha", 0.45)),
        hold_boundary_margin_uv=float(fusion.get("hold_boundary_margin_uv", 0.08)),
        max_update_jump_uv=float(fusion.get("max_update_jump_uv", 0.08)),
        reuse_lost_gids=bool(fusion.get("reuse_lost_gids", True)),
    )
    workers = [
        tr.CamWorker(
            cam,
            cfg.get("model", "yolo26n.pt"),
            cfg.get("device") or ("mps" if sys.platform == "darwin" else "cpu"),
            NullOSC(),
            projections,
            legacy_image_space=False,
            raw_per_cam=False,
            miss_buffer_frames=max(int(fusion.get("miss_buffer_frames", 8)), 0),
            detection_filter=detection_filter,
        )
        for cam in cameras
    ]
    return projections, person_tracker, workers


def draw_camera(frame: np.ndarray, worker: tr.CamWorker, overlays: list, cam_idx: int) -> np.ndarray:
    h, w = frame.shape[:2]
    panel = cv2.resize(frame, (CAM_W, CAM_H), interpolation=cv2.INTER_AREA)
    sx, sy = CAM_W / w, CAM_H / h
    region_color = camera_color(cam_idx)
    for reg in worker.cam.regions:
        pts = np.array([(int(x * sx), int(y * sy)) for x, y in reg.image_points], np.int32)
        cv2.polylines(panel, [pts], True, region_color, 3, cv2.LINE_AA)
        if reg.body_catch_points:
            catch = np.array([(int(x * sx), int(y * sy)) for x, y in reg.body_catch_points], np.int32)
            cv2.polylines(panel, [catch], True, (255, 80, 220), 1, cv2.LINE_AA)
        if reg.relaxed_presence_points:
            relaxed = np.array([(int(x * sx), int(y * sy)) for x, y in reg.relaxed_presence_points], np.int32)
            cv2.polylines(panel, [relaxed], True, (80, 80, 255), 2, cv2.LINE_AA)
    for tid, bbox, conf, hits in overlays:
        x0, y0, x1, y1 = bbox
        p0 = (int(x0 * sx), int(y0 * sy))
        p1 = (int(x1 * sx), int(y1 * sy))
        color = (0, 230, 255) if hits else (150, 150, 150)
        cv2.rectangle(panel, p0, p1, color, 2, cv2.LINE_AA)
        cv2.putText(panel, f"{tid}:{conf:.2f}", (p0[0], max(14, p0[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1, cv2.LINE_AA)
    cv2.putText(panel, worker.cam.name, (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2, cv2.LINE_AA)
    return panel


def draw_camera_strip(cam_panels: list[np.ndarray]) -> np.ndarray:
    if not cam_panels:
        return np.full((CAM_H, VIDEO_W, 3), (8, 10, 12), np.uint8)
    cols = len(cam_panels)
    tile_w = max(1, VIDEO_W // cols)
    strip = np.full((CAM_H, VIDEO_W, 3), (8, 10, 12), np.uint8)
    x = 0
    for idx, panel in enumerate(cam_panels):
        next_x = VIDEO_W if idx == cols - 1 else min(VIDEO_W, x + tile_w)
        resized = cv2.resize(panel, (next_x - x, CAM_H), interpolation=cv2.INTER_AREA)
        strip[:, x:next_x] = resized
        if idx > 0:
            cv2.line(strip, (x, 0), (x, CAM_H), (45, 50, 55), 1, cv2.LINE_AA)
        x = next_x
    return strip


def source_sample_text(workers: list[tr.CamWorker], metrics: dict[str, Any]) -> str:
    source_samples = metrics["source_samples"]
    names = [worker.cam.name for worker in workers]
    extras = sorted(name for name in source_samples if name not in names)
    parts = [f"{name}={source_samples.get(name, 0)}" for name in names + extras]
    return " ".join(parts) if parts else "none"


def draw_usage_panel(
    workers: list[tr.CamWorker],
    persons: list,
    all_samples: list[tuple[float, float, int]],
    trails: dict[tuple[str, int], list[tuple[float, float]]],
    frame_idx: int,
    metrics: dict[str, Any],
) -> np.ndarray:
    panel = np.full((PROJ_H, PROJ_W, 3), (12, 20, 18), np.uint8)
    cell_w = PROJ_W / GRID_W
    cell_h = PROJ_H / GRID_H
    counts = np.zeros((GRID_H, GRID_W), np.int32)
    for u, v, _gid in all_samples:
        gx = min(GRID_W - 1, max(0, int(u * GRID_W)))
        gy = min(GRID_H - 1, max(0, int(v * GRID_H)))
        counts[gy, gx] += 1
    max_count = max(1, int(counts.max()))
    for gy in range(GRID_H):
        for gx in range(GRID_W):
            count = counts[gy, gx]
            if count <= 0:
                continue
            intensity = count / max_count
            color = (0, int(70 + 120 * intensity), 255)
            x0 = int(round(gx * cell_w))
            y0 = int(round(gy * cell_h))
            x1 = int(round((gx + 1) * cell_w))
            y1 = int(round((gy + 1) * cell_h))
            cv2.rectangle(panel, (x0, y0), (x1, y1), color, -1)
    for gx in range(GRID_W + 1):
        x = int(round(gx * cell_w))
        cv2.line(panel, (x, 0), (x, PROJ_H), (54, 64, 60), 1, cv2.LINE_AA)
    for gy in range(GRID_H + 1):
        y = int(round(gy * cell_h))
        cv2.line(panel, (0, y), (PROJ_W, y), (54, 64, 60), 1, cv2.LINE_AA)
    for ci, worker in enumerate(workers):
        color = camera_color(ci)
        for reg in worker.cam.regions:
            for rect, thickness in [(reg.projection_uv, 1), (reg.dispatch_uv, 2)]:
                x0, y0 = uv_to_px(rect[0], rect[1])
                x1, y1 = uv_to_px(rect[2], rect[3])
                cv2.rectangle(panel, (x0, y0), (x1, y1), color, thickness, cv2.LINE_AA)
    for person in persons:
        key = (person.projection_id, person.gid)
        trails.setdefault(key, []).append((person.u, person.v))
        trails[key] = trails[key][-80:]
    for (_proj_id, gid), pts in trails.items():
        if len(pts) >= 2:
            arr = np.array([uv_to_px(u, v) for u, v in pts], np.int32)
            cv2.polylines(panel, [arr], False, gid_color(gid), 1, cv2.LINE_AA)
    for person in persons:
        color = gid_color(person.gid)
        x, y = uv_to_px(person.u, person.v)
        cv2.circle(panel, (x, y), 13, (0, 255, 255), 3, cv2.LINE_AA)
        cv2.circle(panel, (x, y), 7, color, -1, cv2.LINE_AA)
        cv2.putText(panel, f"gid {person.gid}", (x + 10, max(16, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1, cv2.LINE_AA)
    current_pts = [uv_to_px(person.u, person.v) for person in persons]
    for i in range(len(current_pts)):
        for j in range(i + 1, min(len(current_pts), i + 5)):
            cv2.line(panel, current_pts[i], current_pts[j], (170, 125, 180), 1, cv2.LINE_AA)
    used = int((counts > 0).sum())
    lines = [
        f"projection usage simulation frame={frame_idx}",
        f"active={len(persons)} fresh={sum(1 for p in persons if p.state == 'fresh')} used_cells={used}/{GRID_W * GRID_H} ({used / (GRID_W * GRID_H) * 100:.1f}%)",
        f"sources {source_sample_text(workers, metrics)} center_overlap_samples={metrics['center_overlap_samples']}",
    ]
    y = 28
    for line in lines:
        cv2.putText(panel, line, (16, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
        y += 26
    return panel


def run(args: argparse.Namespace) -> dict[str, Any]:
    requested_config = Path(args.config).expanduser()
    config_path = tr.resolve_config_path(requested_config)
    if config_path.resolve(strict=False) != requested_config.resolve(strict=False):
        print(
            json.dumps(
                {
                    "status": "config-resolved",
                    "requested_config": str(requested_config),
                    "runtime_config": str(config_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    base_cfg = yaml.safe_load(config_path.read_text())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_root) / f"live-{stamp}-20s-usage"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_video = out_dir / "all-cameras-20s-projection-usage.mp4"
    summary_path = out_dir / "summary.json"
    sim_config_path = out_dir / "sim-config.yaml"

    print(json.dumps({"status": "recording", "out_dir": str(out_dir)}, ensure_ascii=False), flush=True)
    record_results = record_live_clips(base_cfg, out_dir, args.seconds, args.fps)
    if len(record_results) < 1 or not all(item.get("ok") for item in record_results.values()):
        raise RuntimeError(json.dumps(record_results, ensure_ascii=False, indent=2))
    print(json.dumps({"status": "processing", "record_results": record_results}, ensure_ascii=False), flush=True)

    sim_imgsz, sim_conf = resolve_processing_params(base_cfg, args)
    sim_cfg = make_sim_config(base_cfg, record_results, sim_imgsz, sim_conf)
    sim_config_path.write_text(yaml.safe_dump(sim_cfg, sort_keys=False, allow_unicode=True))
    _projections, person_tracker, workers = parse_runtime(sim_cfg)
    caps = [cv2.VideoCapture(worker.cam.url) for worker in workers]
    processed_frames = min(int(args.seconds * args.fps), *(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) for cap in caps))
    source_fps = min((cap.get(cv2.CAP_PROP_FPS) or args.fps) for cap in caps)
    writer = cv2.VideoWriter(str(output_video), cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (VIDEO_W, VIDEO_H))
    if not writer.isOpened():
        raise RuntimeError(f"failed to open output writer: {output_video}")

    all_samples: list[tuple[float, float, int]] = []
    trails: dict[tuple[str, int], list[tuple[float, float]]] = {}
    metrics: dict[str, Any] = {
        "active_frames": 0,
        "both_side_frames": 0,
        "multi_source_frames": 0,
        "max_active_persons": 0,
        "fresh_samples": 0,
        "center_overlap_samples": 0,
        "source_samples": {},
    }
    preview_frames: list[str] = []
    start = time.time()
    for frame_idx in range(processed_frames):
        frames = []
        ok = True
        for cap in caps:
            ret, frame = cap.read()
            if not ret:
                ok = False
                break
            frames.append(frame)
        if not ok:
            break
        frame_events = []
        frame_lost = []
        cam_panels = []
        for ci, (worker, frame) in enumerate(zip(workers, frames)):
            overlays, _regions, events, lost_sources = worker.step(
                frame,
                sim_imgsz,
                sim_conf,
                0.5,
                sim_cfg.get("tracker", "botsort.yaml"),
                frame_idx / source_fps,
            )
            frame_events.extend(events)
            frame_lost.extend(lost_sources)
            cam_panels.append(draw_camera(frame, worker, overlays, ci))
        persons = person_tracker.update(frame_events, frame_lost, frame_idx / source_fps)
        if persons:
            metrics["active_frames"] += 1
        metrics["max_active_persons"] = max(metrics["max_active_persons"], len(persons))
        metrics["fresh_samples"] += sum(1 for person in persons if person.state == "fresh")
        sources = {person.source[0] for person in persons}
        if len(sources) >= 2:
            metrics["multi_source_frames"] += 1
            metrics["both_side_frames"] += 1
        for person in persons:
            all_samples.append((person.u, person.v, person.gid))
            metrics["source_samples"][person.source[0]] = metrics["source_samples"].get(person.source[0], 0) + 1
            if 0.44 <= person.u <= 0.56:
                metrics["center_overlap_samples"] += 1
        canvas = np.vstack([draw_camera_strip(cam_panels), draw_usage_panel(workers, persons, all_samples, trails, frame_idx, metrics)])
        writer.write(canvas)
        if frame_idx in {100, 200, 300}:
            path = out_dir / f"preview-frame-{frame_idx}.jpg"
            cv2.imwrite(str(path), canvas)
            preview_frames.append(str(path))

    for cap in caps:
        cap.release()
    writer.release()
    used_cells = {
        (min(GRID_W - 1, max(0, int(u * GRID_W))), min(GRID_H - 1, max(0, int(v * GRID_H))))
        for u, v, _gid in all_samples
    }
    us = [u for u, _v, _gid in all_samples]
    vs = [v for _u, v, _gid in all_samples]
    summary = {
        "out_dir": str(out_dir),
        "runtime_config": str(config_path.resolve()),
        "requested_config": str(requested_config.resolve(strict=False)),
        "sim_config": str(sim_config_path),
        "record_results": record_results,
        "output_video": str(output_video),
        "preview_frames": preview_frames,
        "processed_frames": processed_frames,
        "source_fps": source_fps,
        "processed_seconds": round(processed_frames / source_fps, 3),
        "runtime_seconds": time.time() - start,
        "sim_conf": sim_conf,
        "sim_imgsz": sim_imgsz,
        "sim_conf_source": "cli" if args.conf is not None else "config",
        "sim_imgsz_source": "cli" if args.imgsz is not None else "config",
        "active_frames": metrics["active_frames"],
        "active_frame_ratio": metrics["active_frames"] / max(1, processed_frames),
        "both_side_frames": metrics["both_side_frames"],
        "both_side_frame_ratio": metrics["both_side_frames"] / max(1, processed_frames),
        "multi_source_frames": metrics["multi_source_frames"],
        "multi_source_frame_ratio": metrics["multi_source_frames"] / max(1, processed_frames),
        "max_active_persons": metrics["max_active_persons"],
        "fresh_samples": metrics["fresh_samples"],
        "left_samples": metrics["source_samples"].get("cam0", 0),
        "right_samples": metrics["source_samples"].get("cam1", 0),
        "center_overlap_samples": metrics["center_overlap_samples"],
        "grid": {
            "width": GRID_W,
            "height": GRID_H,
            "used_cells": len(used_cells),
            "total_cells": GRID_W * GRID_H,
            "used_cell_ratio": len(used_cells) / (GRID_W * GRID_H),
        },
        "uv_bounds": {
            "min_u": min(us) if us else None,
            "max_u": max(us) if us else None,
            "min_v": min(vs) if vs else None,
            "max_v": max(vs) if vs else None,
            "mean_u": float(np.mean(us)) if us else None,
            "mean_v": float(np.mean(vs)) if vs else None,
        },
        "source_samples": metrics["source_samples"],
        "spawned_gids": person_tracker.spawned_count,
        "handoff_count": person_tracker.handoff_count,
        "teleport_reject_count": person_tracker.teleport_reject_count,
        "lost_count": person_tracker.lost_count,
        "osc_messages_captured": 0,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--imgsz", type=int, default=None)
    parser.add_argument("--conf", type=float, default=None)
    parser.add_argument("--out-root", default="/private/tmp/reolink-video-sim")
    args = parser.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
