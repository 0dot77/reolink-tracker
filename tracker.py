"""Reolink RTSP → YOLO person detection + tracker → OSC (shared projection UV).

Usage:
    python tracker.py                # headless, OSC only
    python tracker.py --show         # operator viewer (cv2 window)
    python tracker.py --config foo.yaml

TouchDesigner minimal OSC schema (default, when osc.td_minimal: true):
    /proj/<projection_id>/active                [gid, gid, ...]
    /proj/<projection_id>/person_zones          [gid, zone_code, gid, zone_code, ...]
    /proj/<projection_id>/xy                    [gid, x, y, gid, x, y, ...]
    /proj/<projection_id>/uv                    [gid, u, v, gid, u, v, ...]
    /proj/<projection_id>/persons/count         int

Person-keyed debug OSC schema (when osc.td_minimal: false and osc.person_level: true):
    /proj/<projection_id>/person/<gid>          [u, v, vx, vy, conf, (u_px, v_px)?]
    /proj/<projection_id>/person/<gid>/source_zone [zone_code, zone_name]
    /proj/<projection_id>/person/<gid>/lost     []
    /proj/<projection_id>/persons               [gid, gid, ...]
    /proj/<projection_id>/persons/count         int

Interaction-zone OSC schema (when projection interaction_zones are configured):
    /proj/<projection_id>/zone/<zone_id>/person/<gid>
        [u, v, zone_u, zone_v, vx, vy, dwell_s, presence, state_code]
    /proj/<projection_id>/zone/<zone_id>/person/<gid>/enter [zone_u, zone_v]
    /proj/<projection_id>/zone/<zone_id>/person/<gid>/leave [reason_code, dwell_s]
    /proj/<projection_id>/zone/<zone_id>/count int

Raw per-cam OSC schema (when osc.raw_per_cam: true):
    /proj/<projection_id>/cam/<cam>/track/<id>      [u, v, conf, (u_px, v_px)?]
    /proj/<projection_id>/cam/<cam>/track/<id>/lost []
    /proj/<projection_id>/cam/<cam>/count           int
    /proj/<projection_id>/cam/<cam>/active          [id, id, ...]

Legacy image-space schema (when osc.legacy_image_space: true):
    <osc_prefix>/track/<id>           [cx, cy, w, h, conf]   normalized 0..1
    <osc_prefix>/track/<id>/lost      []
    <osc_prefix>/count                int
    <osc_prefix>/active               [id, ...]
"""

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

# Force low-latency RTSP via FFmpeg backend. Must be set before importing cv2 ops.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000|reorder_queue_size;0",
)

import cv2
import numpy as np
import yaml
from pythonosc.udp_client import SimpleUDPClient
from ultralytics import YOLO

from fusion import InteractionZoneTracker, LostPerson, PersonEvent, PersonTracker, ZoneUpdate
from region import (
    InteractionZone,
    Projection,
    Region,
    bbox_intersects_polygon,
    build_homography,
    dispatches_overlap,
    is_inside_uv,
    project,
    validate_uv_quad,
    validate_dispatch,
    warp_uv,
    warp_uv_velocity,
)

PERSON_CLASS_ID = 0  # COCO
TAURI_APP_IDENTIFIER = "com.taeyang.reolink-tracker"
CONFIG_RELOAD_INTERVAL_S = 0.5
MOBILE_PREVIEW_DEFAULT_INTERVAL_S = 0.75
MOBILE_PREVIEW_DEFAULT_TTL_S = 4.0
SOURCE_ZONE_CODES = {
    "floor": 0,
    "body_catch": 1,
    "stair_relaxed": 2,
}
SOURCE_ZONE_NAMES = {code: name for name, code in SOURCE_ZONE_CODES.items()}


class ConfigError(ValueError):
    """Raised when config.yaml is structurally valid YAML but not runnable."""


def repo_default_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def tauri_app_config_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / TAURI_APP_IDENTIFIER
        / "runtime"
        / "config.yaml"
    )


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolve the runtime config, preferring the Tauri app's saved config.

    The field app writes calibration edits to macOS app data, while older CLI
    workflows default to the repo-local config.yaml. Treat the app runtime config
    as authoritative whenever the caller asks for the default config. Explicit
    non-default config paths are still honored for isolated tests.
    """
    env_path = os.environ.get("REOLINK_TRACKER_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    default_path = repo_default_config_path()
    requested = Path(path).expanduser() if path else default_path
    requested_default_name = (
        path is not None
        and not requested.is_absolute()
        and requested.name == "config.yaml"
        and requested.parent in {Path("."), Path("")}
    )
    app_path = tauri_app_config_path()
    if not app_path.exists():
        return requested

    try:
        requested_resolved = requested.resolve(strict=False)
        default_resolved = default_path.resolve(strict=False)
    except OSError:
        return requested

    if requested_resolved == default_resolved or requested_default_name:
        return app_path
    return requested


def _redact_url(url: str) -> str:
    """Hide credentials when logging camera URLs."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(
        (parts.scheme, f"<credentials>@{host}", parts.path, parts.query, parts.fragment)
    )


def _network_target_from_camera(cam: "CamCfg") -> Optional[tuple[str, str, int, str]]:
    parts = urlsplit(cam.url)
    if not parts.hostname:
        return None
    return (cam.name, parts.hostname, int(parts.port or 554), "rtsp")


def _is_local_video_source(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme.lower() == "file" or not parts.scheme


def _network_target_from_osc(osc_cfg: dict) -> Optional[tuple[str, str, int, str]]:
    host = str(osc_cfg.get("host", "127.0.0.1"))
    if host in {"127.0.0.1", "::1", "localhost"}:
        return None
    return ("OSC receiver", host, int(osc_cfg.get("port", 7000)), "osc")


def _is_inside_expanded_uv(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
    margin: float,
) -> bool:
    u, v = uv
    u0, v0, u1, v1 = rect
    lo_u, hi_u = sorted((u0, u1))
    lo_v, hi_v = sorted((v0, v1))
    m = max(float(margin), 0.0)
    return lo_u - m <= u <= hi_u + m and lo_v - m <= v <= hi_v + m


def _clamp_uv_to_rect(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> tuple[float, float]:
    u, v = uv
    u0, v0, u1, v1 = rect
    lo_u, hi_u = sorted((u0, u1))
    lo_v, hi_v = sorted((v0, v1))
    return (min(max(u, lo_u), hi_u), min(max(v, lo_v), hi_v))


def _clamp_v_to_rect(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> tuple[float, float]:
    u, v = uv
    _u0, v0, _u1, v1 = rect
    lo_v, hi_v = sorted((v0, v1))
    return (u, min(max(v, lo_v), hi_v))


def _uv_distance_to_rect_center(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> float:
    u, v = uv
    u0, v0, u1, v1 = rect
    center_u = (u0 + u1) / 2.0
    center_v = (v0 + v1) / 2.0
    du = u - center_u
    dv = v - center_v
    return (du * du + dv * dv) ** 0.5


def _is_inside_expanded_u(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
    margin: float = 0.0,
) -> bool:
    u, _v = uv
    u0, _v0, u1, _v1 = rect
    lo_u, hi_u = sorted((u0, u1))
    m = max(float(margin), 0.0)
    return lo_u - m <= u <= hi_u + m


def _body_catch_inference_crop_rect(
    regions: list[Region],
    frame_shape: tuple[int, ...],
    margin_px: int,
) -> Optional[tuple[int, int, int, int]]:
    """Return a stable full-frame crop rect driven by body-catch polygons.

    The detector crop is based on regions that actually define
    `body_catch_points`, and includes each matching floor `image_points` too.
    That keeps the detector's ROI zoom focused on the body-catch band without
    accidentally cutting away lower body/foot pixels needed for stable person
    boxes and the existing floor homography.
    """
    if len(frame_shape) < 2:
        return None
    frame_h, frame_w = int(frame_shape[0]), int(frame_shape[1])
    if frame_w <= 1 or frame_h <= 1:
        return None

    points: list[tuple[float, float]] = []
    for reg in regions:
        if not reg.body_catch_points:
            continue
        points.extend(reg.body_catch_points)
        points.extend(reg.image_points)
    if not points:
        return None

    margin = max(int(margin_px), 0)
    xs = [float(x) for x, _y in points]
    ys = [float(y) for _x, y in points]
    x0 = max(int(np.floor(min(xs))) - margin, 0)
    y0 = max(int(np.floor(min(ys))) - margin, 0)
    x1 = min(int(np.ceil(max(xs))) + margin, frame_w)
    y1 = min(int(np.ceil(max(ys))) + margin, frame_h)
    if x1 - x0 <= 1 or y1 - y0 <= 1:
        return None
    if x0 == 0 and y0 == 0 and x1 == frame_w and y1 == frame_h:
        return None
    return (x0, y0, x1, y1)


def _offset_boxes_xyxy(
    boxes: np.ndarray,
    offset: tuple[int, int],
    frame_shape: tuple[int, ...],
) -> np.ndarray:
    """Translate crop-local YOLO boxes back into full-frame coordinates."""
    if boxes.size == 0:
        return boxes
    ox, oy = offset
    if ox == 0 and oy == 0:
        return boxes
    out = boxes.copy()
    out[:, [0, 2]] += float(ox)
    out[:, [1, 3]] += float(oy)
    if len(frame_shape) >= 2:
        frame_h, frame_w = int(frame_shape[0]), int(frame_shape[1])
        out[:, [0, 2]] = np.clip(out[:, [0, 2]], 0.0, float(frame_w))
        out[:, [1, 3]] = np.clip(out[:, [1, 3]], 0.0, float(frame_h))
    return out


@dataclass
class BodyCatchInferenceCropCfg:
    """Optional camera-level software zoom based on configured body-catch masks."""

    enabled: bool = False
    margin_px: int = 96


@dataclass
class CamCfg:
    name: str
    url: str
    osc_prefix: str
    regions: list[Region] = field(default_factory=list)
    tracking_enabled: bool = True
    body_catch_inference_crop: BodyCatchInferenceCropCfg = field(
        default_factory=BodyCatchInferenceCropCfg
    )


@dataclass
class MobilePreviewCfg:
    directory: Path
    request_file: Path
    max_width: int = 640
    interval_s: float = MOBILE_PREVIEW_DEFAULT_INTERVAL_S
    request_ttl_s: float = MOBILE_PREVIEW_DEFAULT_TTL_S
    jpeg_quality: int = 65


@dataclass
class DetectionFilterCfg:
    """Post-YOLO hygiene before raw tracks become interaction actors."""

    enabled: bool = True
    min_confidence: float = 0.28
    min_bbox_height_px: float = 42.0
    min_bbox_area_px: float = 900.0
    min_aspect_h_over_w: float = 1.15
    max_aspect_h_over_w: float = 5.8
    max_width_over_height: float = 1.05
    projection_inner_margin_uv: float = 0.0
    confirm_hits: int = 3
    confirm_window_s: float = 0.8
    relaxed_min_confidence: float = 0.12
    relaxed_min_bbox_height_px: float = 24.0
    relaxed_min_bbox_area_px: float = 500.0
    relaxed_min_aspect_h_over_w: float = 0.45
    relaxed_max_aspect_h_over_w: float = 6.5
    relaxed_max_width_over_height: float = 2.4


@dataclass
class ProcessingCfg:
    """Runtime scheduling policy for camera inference workers."""

    parallel: bool = True
    max_frame_age_s: float = 1.0
    result_queue_size: int = 4


@dataclass
class _PendingDetection:
    first_t: float
    last_t: float
    hits: int = 1


@dataclass
class OscPacket:
    address: str
    args: object


@dataclass
class CamStepResult:
    overlays: list
    regions: list[Region]
    person_events: list[PersonEvent]
    lost_sources: list[tuple[str, int]]
    osc_packets: list[OscPacket]
    speed_ms: dict[str, float]
    step_ms: float

    def legacy_tuple(self) -> tuple[list, list[Region], list[PersonEvent], list[tuple[str, int]]]:
        return (
            self.overlays,
            self.regions,
            self.person_events,
            self.lost_sources,
        )


@dataclass
class CameraProcessResult:
    cam_name: str
    frame: np.ndarray
    frame_idx: int
    frame_ts: float
    processed_mono: float
    frame_age_s: float
    step: CamStepResult
    dropped_frames: int = 0
    dropped_results: int = 0
    skipped_stale_frames: int = 0


@dataclass
class CameraStatsWindow:
    frames: int = 0
    osc_messages: int = 0
    step_ms_sum: float = 0.0
    step_ms_max: float = 0.0
    frame_age_sum: float = 0.0
    frame_age_max: float = 0.0
    dropped_frames: int = 0
    dropped_results: int = 0
    skipped_stale_frames: int = 0
    speed_ms_sum: dict[str, float] = field(default_factory=dict)
    speed_ms_count: dict[str, int] = field(default_factory=dict)

    def add_result(self, result: CameraProcessResult, emitted_osc: int) -> None:
        self.frames += 1
        self.osc_messages += emitted_osc
        self.step_ms_sum += result.step.step_ms
        self.step_ms_max = max(self.step_ms_max, result.step.step_ms)
        self.frame_age_sum += result.frame_age_s
        self.frame_age_max = max(self.frame_age_max, result.frame_age_s)
        self.dropped_frames += result.dropped_frames
        self.dropped_results += result.dropped_results
        self.skipped_stale_frames += result.skipped_stale_frames
        for key, value in result.step.speed_ms.items():
            self.speed_ms_sum[key] = self.speed_ms_sum.get(key, 0.0) + value
            self.speed_ms_count[key] = self.speed_ms_count.get(key, 0) + 1

    def snapshot(self, dt: float) -> dict:
        fps = self.frames / dt if dt > 0 else 0.0
        osc_rate = self.osc_messages / dt if dt > 0 else 0.0
        speed_avg = {
            key: self.speed_ms_sum[key] / max(1, self.speed_ms_count.get(key, 0))
            for key in sorted(self.speed_ms_sum)
        }
        return {
            "fps": fps,
            "osc_rate": osc_rate,
            "track_step_ms_avg": (
                self.step_ms_sum / self.frames if self.frames else None
            ),
            "track_step_ms_max": self.step_ms_max if self.frames else None,
            "frame_age_s_avg": (
                self.frame_age_sum / self.frames if self.frames else None
            ),
            "frame_age_s_max": self.frame_age_max if self.frames else None,
            "dropped_frames": self.dropped_frames,
            "dropped_results": self.dropped_results,
            "skipped_stale_frames": self.skipped_stale_frames,
            "yolo_speed_ms": speed_avg,
        }


class CameraProcessor(threading.Thread):
    """Runs one camera's YOLO tracker without blocking OSC heartbeat cadence."""

    def __init__(
        self,
        grabber: "FrameGrabber",
        worker: "CamWorker",
        imgsz: int,
        conf: float,
        iou: float,
        tracker: str,
        processing: ProcessingCfg,
    ):
        super().__init__(daemon=True, name=f"track-{grabber.cam.name}")
        self.grabber = grabber
        self.worker = worker
        self.imgsz = imgsz
        self.conf = conf
        self.iou = iou
        self.tracker = tracker
        self.processing = processing
        self._stop_event = threading.Event()
        self._result_q: queue.Queue[CameraProcessResult] = queue.Queue(
            maxsize=max(processing.result_queue_size, 1)
        )
        self._last_idx = -1
        self._dropped_frames = 0
        self._dropped_results = 0
        self._skipped_stale_frames = 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            frame, frame_idx, frame_ts = self.grabber.get()
            if frame is None or frame_idx == self._last_idx:
                self._stop_event.wait(0.002)
                continue
            dropped_since_last = max(0, frame_idx - self._last_idx - 1) if self._last_idx >= 0 else 0
            self._last_idx = frame_idx
            frame_age_s = time.time() - frame_ts if frame_ts else 999.0
            if (
                self.processing.max_frame_age_s > 0
                and frame_age_s > self.processing.max_frame_age_s
            ):
                self._skipped_stale_frames += 1
                continue
            step = self.worker.step(
                frame,
                self.imgsz,
                self.conf,
                self.iou,
                self.tracker,
                time.monotonic(),
            )
            dropped_results = self._dropped_results
            while not self._result_q.empty():
                try:
                    self._result_q.get_nowait()
                    dropped_results += 1
                except queue.Empty:
                    break
            result = CameraProcessResult(
                cam_name=self.grabber.cam.name,
                frame=frame,
                frame_idx=frame_idx,
                frame_ts=frame_ts,
                processed_mono=time.monotonic(),
                frame_age_s=frame_age_s,
                step=step,
                dropped_frames=dropped_since_last + self._dropped_frames,
                dropped_results=dropped_results,
                skipped_stale_frames=self._skipped_stale_frames,
            )
            self._dropped_frames = 0
            self._dropped_results = 0
            self._skipped_stale_frames = 0
            try:
                self._result_q.put_nowait(result)
            except queue.Full:
                self._dropped_results += 1

    def drain_results(self) -> list[CameraProcessResult]:
        results = []
        while True:
            try:
                results.append(self._result_q.get_nowait())
            except queue.Empty:
                return results

    def stop(self) -> None:
        self._stop_event.set()


class FrameGrabber(threading.Thread):
    """Drains an RTSP stream as fast as possible. Only the latest frame is kept;
    older frames are dropped. Reconnects automatically on read failure."""

    def __init__(self, cam: CamCfg):
        super().__init__(daemon=True, name=f"grab-{cam.name}")
        self.cam = cam
        self._latest: Optional[np.ndarray] = None
        self._idx = 0
        self._ts = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.reconnects = 0

    def run(self) -> None:
        backoff = 1.0
        first = True
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.cam.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                print(f"[{self.cam.name}] open failed; retry in {backoff:.0f}s")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            backoff = 1.0
            if not first:
                self.reconnects += 1
            first = False
            print(f"[{self.cam.name}] connected: {_redact_url(self.cam.url)}")
            is_file_source = _is_local_video_source(self.cam.url)
            source_fps = cap.get(cv2.CAP_PROP_FPS) if is_file_source else 0.0
            frame_interval = 1.0 / source_fps if source_fps and source_fps > 0 else 0.0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    if is_file_source:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    print(f"[{self.cam.name}] read failed; reconnecting")
                    break
                with self._lock:
                    self._latest = frame
                    self._idx += 1
                    self._ts = time.time()
                if frame_interval:
                    self._stop.wait(frame_interval)
            cap.release()

    def get(self) -> tuple[Optional[np.ndarray], int, float]:
        with self._lock:
            if self._latest is None:
                return None, 0, 0.0
            return self._latest, self._idx, self._ts

    def stop(self) -> None:
        self._stop.set()


def _safe_preview_name(name: str) -> str:
    safe = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in {"-", "_"}) else "_"
        for ch in name
    )
    return safe or "camera"


def _mobile_preview_cfg_from_env() -> Optional[MobilePreviewCfg]:
    directory = os.environ.get("REOLINK_MOBILE_PREVIEW_DIR")
    if not directory:
        return None
    preview_dir = Path(directory).expanduser()
    request_file = Path(
        os.environ.get(
            "REOLINK_MOBILE_PREVIEW_REQUEST_FILE",
            str(preview_dir / ".request"),
        )
    ).expanduser()
    try:
        max_width = max(int(os.environ.get("REOLINK_MOBILE_PREVIEW_MAX_WIDTH", "640")), 160)
    except ValueError:
        max_width = 640
    try:
        interval_s = max(float(os.environ.get("REOLINK_MOBILE_PREVIEW_INTERVAL_S", "0.75")), 0.25)
    except ValueError:
        interval_s = MOBILE_PREVIEW_DEFAULT_INTERVAL_S
    try:
        request_ttl_s = max(float(os.environ.get("REOLINK_MOBILE_PREVIEW_REQUEST_TTL_S", "4.0")), 1.0)
    except ValueError:
        request_ttl_s = MOBILE_PREVIEW_DEFAULT_TTL_S
    try:
        jpeg_quality = int(os.environ.get("REOLINK_MOBILE_PREVIEW_JPEG_QUALITY", "65"))
    except ValueError:
        jpeg_quality = 65
    return MobilePreviewCfg(
        directory=preview_dir,
        request_file=request_file,
        max_width=max_width,
        interval_s=interval_s,
        request_ttl_s=request_ttl_s,
        jpeg_quality=min(max(jpeg_quality, 35), 85),
    )


class MobilePreviewSink:
    """Writes low-rate mobile preview JPEGs only while the app requests them."""

    def __init__(self, cfg: MobilePreviewCfg):
        self.cfg = cfg
        self._last_write_mono: dict[str, float] = {}
        self.cfg.directory.mkdir(parents=True, exist_ok=True)

    def _requested(self) -> bool:
        try:
            age = time.time() - self.cfg.request_file.stat().st_mtime
        except OSError:
            return False
        return age <= self.cfg.request_ttl_s

    def write(
        self,
        camera_name: str,
        frame: np.ndarray,
        overlays: list,
        fps: float,
        osc_rate: float,
        reconnects: int,
        frame_age_s: float,
        now_mono: float,
    ) -> None:
        if not self._requested():
            return
        last_write = self._last_write_mono.get(camera_name, 0.0)
        if now_mono - last_write < self.cfg.interval_s:
            return

        try:
            jpg = self._render_jpeg(
                camera_name,
                frame,
                overlays,
                fps,
                osc_rate,
                reconnects,
                frame_age_s,
            )
            target = self.cfg.directory / f"{_safe_preview_name(camera_name)}.jpg"
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_bytes(jpg)
            os.replace(tmp, target)
            self._last_write_mono[camera_name] = now_mono
        except Exception as ex:  # noqa: BLE001 - preview must never break tracking.
            print(f"[mobile-preview] {camera_name}: {ex}", file=sys.stderr, flush=True)

    def _render_jpeg(
        self,
        camera_name: str,
        frame: np.ndarray,
        overlays: list,
        fps: float,
        osc_rate: float,
        reconnects: int,
        frame_age_s: float,
    ) -> bytes:
        canvas = frame.copy()
        for tid, bbox, conf, hits in overlays:
            x1, y1, x2, y2 = (int(round(v)) for v in bbox)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 138, 61), 2)
            label = f"id {tid} {conf:.2f}"
            cv2.putText(
                canvas,
                label,
                (x1, max(18, y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            for reg_id, u, v, in_dispatch in hits:
                color = (123, 216, 143) if in_dispatch else (255, 209, 102)
                text = f"{reg_id} {u:.2f},{v:.2f}"
                cv2.putText(
                    canvas,
                    text,
                    (x1, min(canvas.shape[0] - 10, y2 + 18)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    color,
                    1,
                    cv2.LINE_AA,
                )

        status = (
            f"{camera_name}  {fps:.1f}fps  osc={osc_rate:.1f}/s  "
            f"age={frame_age_s:.1f}s  rc={reconnects}"
        )
        cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 32), (8, 9, 11), -1)
        cv2.putText(
            canvas,
            status,
            (12, 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (240, 234, 220),
            1,
            cv2.LINE_AA,
        )

        h, w = canvas.shape[:2]
        if w > self.cfg.max_width:
            scale = self.cfg.max_width / float(w)
            canvas = cv2.resize(
                canvas,
                (self.cfg.max_width, max(1, int(round(h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(
            ".jpg",
            canvas,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality],
        )
        if not ok:
            raise RuntimeError("failed to encode jpeg")
        return encoded.tobytes()


class CamWorker:
    """Per-camera state: dedicated YOLO instance (own tracker), projections lookup,
    region homography, and last-seen IDs to emit 'lost' events on disappearance."""

    def __init__(
        self,
        cam: CamCfg,
        model_path: str,
        device: str,
        osc: SimpleUDPClient,
        projections: dict[str, Projection],
        legacy_image_space: bool,
        raw_per_cam: bool = True,
        miss_buffer_frames: int = 8,
        detection_filter: Optional[DetectionFilterCfg] = None,
    ):
        self.cam = cam
        self.osc = osc
        self.model_path = model_path
        self.model: Optional[YOLO] = YOLO(model_path) if cam.tracking_enabled else None
        self.device = device
        self.projections = projections
        self.legacy_image_space = legacy_image_space
        self.raw_per_cam = raw_per_cam
        self.detection_filter = detection_filter or DetectionFilterCfg()
        # last_ids per region_id; legacy uses the empty-string key.
        self.last_ids: dict[str, set[int]] = {r.id: set() for r in cam.regions}
        self.last_ids[""] = set()  # legacy bucket
        # last_projection_tids: track_ids whose foot fell into at least one
        # region's projection_uv last frame. A tid that drops out of this set
        # is a fusion lost_source — i.e., the camera no longer has anything
        # useful to say about that source. Crossing the dispatch_uv boundary
        # alone is *not* a loss (the gid stays alive, it just goes silent).
        self.last_projection_tids: set[int] = set()
        # _tid_miss buffers short detection drops so YOLO/BoT-SORT hiccups of
        # a few frames don't churn fusion gids. Each entry is (cam-local tid →
        # consecutive frames absent from projection_uv). Once the count
        # reaches miss_buffer_frames we emit lost_source; if the tid returns
        # before that, the entry is cleared and no loss is reported.
        self.miss_buffer_frames = max(int(miss_buffer_frames), 0)
        self._tid_miss: dict[int, int] = {}
        # Tracks must pass a short confirmation gate before they become
        # interaction actors. This keeps one-frame bag/shadow/person-part
        # detections from spawning OSC identities.
        self._pending_detections: dict[int, _PendingDetection] = {}
        self._confirmed_tids: set[int] = set()
        self._last_inference_crop_rect: Optional[tuple[int, int, int, int]] = None
        self.fps_count = 0
        self.osc_count = 0
        self._lock = threading.Lock()

    def update_regions(self, regions: list[Region], preserve_tracking: bool = False) -> None:
        """Replace active regions after operator edits in the viewer."""
        with self._lock:
            self._update_regions_unlocked(regions, preserve_tracking)

    def update_runtime_calibration(
        self,
        projections: dict[str, Projection],
        detection_filter: DetectionFilterCfg,
        tracking_enabled: bool,
        body_catch_inference_crop: BodyCatchInferenceCropCfg,
        regions: list[Region],
        preserve_tracking: bool = False,
    ) -> list[tuple[str, int]]:
        with self._lock:
            forced_lost_sources: list[tuple[str, int]] = []
            self.projections = projections
            self.detection_filter = detection_filter
            if self.cam.tracking_enabled != tracking_enabled:
                if self.cam.tracking_enabled and not tracking_enabled:
                    forced_lost_sources = [
                        (self.cam.name, tid) for tid in sorted(self.last_projection_tids)
                    ]
                self.cam.tracking_enabled = tracking_enabled
                preserve_tracking = False
                self._last_inference_crop_rect = None
            if self.cam.body_catch_inference_crop != body_catch_inference_crop:
                self.cam.body_catch_inference_crop = body_catch_inference_crop
                preserve_tracking = False
                self._last_inference_crop_rect = None
            self._update_regions_unlocked(regions, preserve_tracking)
            return forced_lost_sources

    def _ensure_model(self) -> YOLO:
        if self.model is None:
            self.model = YOLO(self.model_path)
        return self.model

    def _update_regions_unlocked(
        self,
        regions: list[Region],
        preserve_tracking: bool,
    ) -> None:
        previous_last_ids = self.last_ids
        self.cam.regions = regions
        self.last_ids = {
            r.id: set(previous_last_ids.get(r.id, set())) if preserve_tracking else set()
            for r in regions
        }
        self.last_ids[""] = (
            set(previous_last_ids.get("", set())) if preserve_tracking else set()
        )
        if not preserve_tracking:
            self.last_projection_tids = set()
            self._tid_miss = {}
            self._pending_detections = {}
            self._confirmed_tids = set()

    def _classify_detection(self, box: np.ndarray, conf: float) -> tuple[bool, str]:
        cfg = self.detection_filter
        if not cfg.enabled:
            return True, "accepted"
        x1, y1, x2, y2 = (float(v) for v in box)
        bw = x2 - x1
        bh = y2 - y1
        area = bw * bh
        if bh < cfg.min_bbox_height_px or area < cfg.min_bbox_area_px:
            return False, "too-small"
        if bw <= 1.0:
            return False, "bad-width"
        aspect_h_over_w = bh / bw
        if aspect_h_over_w < cfg.min_aspect_h_over_w:
            return False, "too-wide"
        if aspect_h_over_w > cfg.max_aspect_h_over_w:
            return False, "too-tall"
        if (bw / bh) > cfg.max_width_over_height:
            return False, "too-wide"
        if conf < cfg.min_confidence:
            return False, "low-conf"
        return True, "accepted"

    def _classify_relaxed_presence(self, box: np.ndarray, conf: float) -> bool:
        cfg = self.detection_filter
        if not cfg.enabled:
            return True
        x1, y1, x2, y2 = (float(v) for v in box)
        bw = x2 - x1
        bh = y2 - y1
        if bw <= 1.0 or bh <= 1.0:
            return False
        if conf < cfg.relaxed_min_confidence:
            return False
        if bh < cfg.relaxed_min_bbox_height_px:
            return False
        if bw * bh < cfg.relaxed_min_bbox_area_px:
            return False
        aspect_h_over_w = bh / bw
        if aspect_h_over_w < cfg.relaxed_min_aspect_h_over_w:
            return False
        if aspect_h_over_w > cfg.relaxed_max_aspect_h_over_w:
            return False
        if (bw / bh) > cfg.relaxed_max_width_over_height:
            return False
        return True

    def _passes_projection_margin(
        self,
        uv: tuple[float, float],
        rect: tuple[float, float, float, float],
    ) -> bool:
        margin = self.detection_filter.projection_inner_margin_uv
        if not self.detection_filter.enabled or margin <= 0:
            return True
        u, v = uv
        u0, v0, u1, v1 = rect
        lo_u, hi_u = sorted((u0, u1))
        lo_v, hi_v = sorted((v0, v1))
        return (
            lo_u + margin <= u <= hi_u - margin
            and lo_v + margin <= v <= hi_v - margin
        )

    def _is_confirmed_detection(self, tid: int, now: float) -> bool:
        cfg = self.detection_filter
        if not cfg.enabled or cfg.confirm_hits <= 1:
            self._confirmed_tids.add(tid)
            return True
        if tid in self._confirmed_tids:
            return True
        pending = self._pending_detections.get(tid)
        if pending is None or now - pending.last_t > cfg.confirm_window_s:
            self._pending_detections[tid] = _PendingDetection(first_t=now, last_t=now)
            return False
        pending.hits += 1
        pending.last_t = now
        if pending.hits >= cfg.confirm_hits:
            self._confirmed_tids.add(tid)
            self._pending_detections.pop(tid, None)
            return True
        return False

    def _prune_pending_detections(self, now: float) -> None:
        cfg = self.detection_filter
        if not cfg.enabled:
            return
        stale = [
            tid for tid, pending in self._pending_detections.items()
            if now - pending.last_t > cfg.confirm_window_s
        ]
        for tid in stale:
            self._pending_detections.pop(tid, None)

    def _body_catch_accepts(
        self,
        reg: Region,
        box: np.ndarray,
        uv: tuple[float, float],
        conf: float,
        accepted: bool,
        reason: str,
    ) -> bool:
        if not reg.body_catch_points:
            return False
        if not accepted:
            if reason == "too-small":
                if reg.min_bbox_height_px <= 0:
                    return False
                x1, y1, x2, y2 = (float(v) for v in box)
                bbox_h = y2 - y1
                bbox_area = (x2 - x1) * bbox_h
                if bbox_h < reg.min_bbox_height_px:
                    return False
                if bbox_area < self.detection_filter.relaxed_min_bbox_area_px:
                    return False
            elif reason != "low-conf":
                return False
            if reg.body_catch_min_confidence <= 0.0 or conf < reg.body_catch_min_confidence:
                return False
        if not bbox_intersects_polygon(tuple(float(v) for v in box), reg.body_catch_points):
            return False
        return _is_inside_expanded_uv(uv, reg.projection_uv, reg.body_catch_margin_uv)

    def _relaxed_presence_accepts(
        self,
        reg: Region,
        box: np.ndarray,
        uv: tuple[float, float],
        conf: float,
    ) -> bool:
        if not reg.relaxed_presence_enabled or not reg.relaxed_presence_points:
            return False
        if not self._classify_relaxed_presence(box, conf):
            return False
        if reg.relaxed_presence_min_confidence > 0.0 and conf < reg.relaxed_presence_min_confidence:
            return False
        if not bbox_intersects_polygon(
            tuple(float(v) for v in box),
            reg.relaxed_presence_points,
        ):
            return False
        return _is_inside_expanded_u(uv, reg.projection_uv, reg.relaxed_presence_margin_uv)

    def _select_inference_frame(
        self,
        frame: np.ndarray,
    ) -> tuple[np.ndarray, tuple[int, int]]:
        cfg = self.cam.body_catch_inference_crop
        if not cfg.enabled:
            return frame, (0, 0)
        rect = _body_catch_inference_crop_rect(
            self.cam.regions,
            frame.shape,
            cfg.margin_px,
        )
        if rect is None:
            if self._last_inference_crop_rect is not None:
                print(
                    f"[{self.cam.name}] body-catch inference crop disabled; using full frame",
                    flush=True,
                )
                self._last_inference_crop_rect = None
            return frame, (0, 0)
        x0, y0, x1, y1 = rect
        if rect != self._last_inference_crop_rect:
            print(
                f"[{self.cam.name}] body-catch inference crop: "
                f"{x1 - x0}x{y1 - y0} at x={x0}, y={y0}",
                flush=True,
            )
            self._last_inference_crop_rect = rect
        return frame[y0:y1, x0:x1], (x0, y0)

    def step(
        self,
        frame: np.ndarray,
        imgsz: int,
        conf: float,
        iou: float,
        tracker: str,
        now: float,
    ) -> CamStepResult:
        with self._lock:
            return self._step_unlocked(frame, imgsz, conf, iou, tracker, now)

    def _step_unlocked(
        self,
        frame: np.ndarray,
        imgsz: int,
        conf: float,
        iou: float,
        tracker: str,
        now: float,
    ) -> CamStepResult:
        """Run one detection+track step.

        Returns `(overlays, regions, person_events, lost_sources)`. Person
        events represent every dispatching track this frame (one entry per
        (track_id, region) hit that fell inside `dispatch_uv`). Lost sources
        are `(cam_name, track_id)` tuples whose tracks had a foot inside
        any region's `projection_uv` last frame and no longer do — i.e.,
        the camera has nothing useful to say about that source anymore.
        Tracks that merely cross the dispatch boundary (still inside
        projection but no longer dispatching) are *not* reported as lost,
        so the fusion layer keeps their gid alive across boundary jitter
        and only stops broadcasting `/person/<gid>` for that frame.

        Raw/legacy OSC packets are returned to the caller so the main thread
        remains the only OSC sender.
        """
        step_started = time.perf_counter()
        if not self.cam.tracking_enabled:
            return CamStepResult(
                overlays=[],
                regions=self.cam.regions,
                person_events=[],
                lost_sources=[],
                osc_packets=[],
                speed_ms={},
                step_ms=(time.perf_counter() - step_started) * 1000.0,
            )
        inference_frame, inference_offset = self._select_inference_frame(frame)
        results = self._ensure_model().track(
            inference_frame,
            imgsz=imgsz,
            conf=conf,
            iou=iou,
            classes=[PERSON_CLASS_ID],
            persist=True,
            tracker=tracker,
            device=self.device,
            verbose=False,
        )
        r = results[0]
        speed_ms = {
            str(key): float(value)
            for key, value in (getattr(r, "speed", {}) or {}).items()
            if isinstance(value, (int, float))
        }
        h, w = frame.shape[:2]

        overlays: list = []  # list[viewer.TrackOverlay] (lazy import below to avoid cycle when --show is off)
        # Per-region active ids this frame.
        active: dict[str, list[int]] = {reg.id: [] for reg in self.cam.regions}
        legacy_active: list[int] = []
        person_events: list[PersonEvent] = []
        osc_packets: list[OscPacket] = []
        projection_tids: set[int] = set()
        has_relaxed_regions = any(
            reg.relaxed_presence_enabled and reg.relaxed_presence_points
            for reg in self.cam.regions
        )

        if r.boxes is not None and r.boxes.id is not None:
            ids = r.boxes.id.cpu().numpy().astype(int)
            xyxy = _offset_boxes_xyxy(
                r.boxes.xyxy.cpu().numpy(),
                inference_offset,
                frame.shape,
            )
            confs = r.boxes.conf.cpu().numpy()

            for tid, box, c in zip(ids, xyxy, confs):
                accepted, _reason = self._classify_detection(box, float(c))
                if not accepted and _reason != "low-conf" and not has_relaxed_regions:
                    continue
                if not self._is_confirmed_detection(int(tid), now):
                    continue
                x1, y1, x2, y2 = (float(v) for v in box)
                bbox_h = y2 - y1
                foot_x = (x1 + x2) / 2.0
                foot_y = y2

                region_hits: list[tuple[str, float, float, bool]] = []
                in_projection = False
                for reg in self.cam.regions:
                    if reg.H is None:
                        continue
                    u, v = project((foot_x, foot_y), reg.H)
                    relaxed = self._relaxed_presence_accepts(
                        reg,
                        box,
                        (u, v),
                        float(c),
                    )
                    caught = self._body_catch_accepts(
                        reg,
                        box,
                        (u, v),
                        float(c),
                        accepted,
                        _reason,
                    )
                    if (
                        reg.min_bbox_height_px
                        and bbox_h < reg.min_bbox_height_px
                        and not relaxed
                    ):
                        # too small for the normal floor path.
                        continue
                    inside_projection = is_inside_uv((u, v), reg.projection_uv)
                    if not inside_projection and not caught and not relaxed:
                        continue
                    if not inside_projection and caught:
                        u, v = _clamp_uv_to_rect((u, v), reg.projection_uv)
                    elif relaxed:
                        if reg.relaxed_presence_H is not None:
                            u, v = project((foot_x, foot_y), reg.relaxed_presence_H)
                            if reg.relaxed_presence_uv is not None:
                                u, v = _clamp_uv_to_rect((u, v), reg.relaxed_presence_uv)
                        if reg.relaxed_presence_v is not None:
                            v = reg.relaxed_presence_v
                        else:
                            clamp_rect = reg.relaxed_presence_uv or reg.projection_uv
                            u, v = _clamp_v_to_rect((u, v), clamp_rect)
                    if (
                        not caught
                        and not relaxed
                        and not self._passes_projection_margin((u, v), reg.projection_uv)
                    ):
                        continue
                    if not accepted and not caught and not relaxed:
                        continue
                    in_projection = True
                    in_dispatch = (
                        _is_inside_expanded_u((u, v), reg.dispatch_uv)
                        if relaxed
                        else is_inside_uv((u, v), reg.dispatch_uv)
                    )
                    region_hits.append((reg.id, u, v, in_dispatch))
                    if relaxed:
                        source_zone = "stair_relaxed"
                    elif caught:
                        source_zone = "body_catch"
                    else:
                        source_zone = "floor"
                    person_events.append(PersonEvent(
                        projection_id=reg.projection_id,
                        cam_name=self.cam.name,
                        track_id=int(tid),
                        u=u,
                        v=v,
                        conf=float(c),
                        t=now,
                        dispatching=in_dispatch,
                        relaxed=relaxed,
                        source_zone=source_zone,
                        dispatch_center_distance=_uv_distance_to_rect_center(
                            (u, v),
                            reg.dispatch_uv,
                        ),
                    ))
                    if in_dispatch:
                        if self.raw_per_cam:
                            proj = self.projections.get(reg.projection_id)
                            addr = f"/proj/{reg.projection_id}/cam/{self.cam.name}/track/{int(tid)}"
                            args = [u, v, float(c)]
                            if proj is not None and proj.pixel_size is not None:
                                pw, ph = proj.pixel_size
                                args.extend([u * pw, v * ph])
                            osc_packets.append(OscPacket(addr, args))
                            self.osc_count += 1
                        active[reg.id].append(int(tid))

                if in_projection:
                    projection_tids.add(int(tid))

                # Legacy image-space dispatch (independent of regions).
                if self.legacy_image_space:
                    cx = foot_x / w
                    cy = (y1 + y2) / 2.0 / h
                    bw = (x2 - x1) / w
                    bh = bbox_h / h
                    osc_packets.append(OscPacket(
                        f"{self.cam.osc_prefix}/track/{int(tid)}",
                        [cx, cy, bw, bh, float(c)],
                    ))
                    self.osc_count += 1
                    legacy_active.append(int(tid))

                overlays.append((int(tid), (x1, y1, x2, y2), float(c), region_hits))

        # Region-level lost events + per-region count/active.
        for reg in self.cam.regions:
            cur = set(active[reg.id])
            if self.raw_per_cam:
                for lost_id in self.last_ids.get(reg.id, set()) - cur:
                    osc_packets.append(OscPacket(
                        f"/proj/{reg.projection_id}/cam/{self.cam.name}/track/{lost_id}/lost",
                        [],
                    ))
                    self.osc_count += 1
                osc_packets.append(OscPacket(
                    f"/proj/{reg.projection_id}/cam/{self.cam.name}/count",
                    len(cur),
                ))
                self.osc_count += 1
                if cur:
                    osc_packets.append(OscPacket(
                        f"/proj/{reg.projection_id}/cam/{self.cam.name}/active",
                        sorted(cur),
                    ))
                    self.osc_count += 1
            self.last_ids[reg.id] = cur

        # Legacy lost + count + active.
        if self.legacy_image_space:
            cur = set(legacy_active)
            for lost_id in self.last_ids[""] - cur:
                osc_packets.append(OscPacket(
                    f"{self.cam.osc_prefix}/track/{lost_id}/lost", []
                ))
                self.osc_count += 1
            self.last_ids[""] = cur
            osc_packets.append(OscPacket(f"{self.cam.osc_prefix}/count", len(cur)))
            self.osc_count += 1
            if cur:
                osc_packets.append(OscPacket(
                    f"{self.cam.osc_prefix}/active", sorted(cur)
                ))
                self.osc_count += 1

        # Fusion lost_sources: any tid that had a foot in projection_uv last
        # frame and no longer does (track left projection or YOLO dropped it).
        # Crossing the dispatch_uv boundary alone is *not* a loss — those
        # tracks just go silent for the broadcast layer until they re-enter
        # dispatch, so the gid stays stable across boundary jitter.
        #
        # A miss buffer absorbs short YOLO/BoT-SORT detection drops: a tid
        # only counts as lost after `miss_buffer_frames` consecutive absent
        # frames. If it returns before that, the counter resets silently.
        for tid in projection_tids:
            self._tid_miss.pop(tid, None)
        for tid in self.last_projection_tids - projection_tids:
            self._tid_miss[tid] = 1
        for tid in list(self._tid_miss.keys()):
            if tid in projection_tids or tid in self.last_projection_tids:
                continue
            self._tid_miss[tid] += 1
        threshold = max(self.miss_buffer_frames, 1)
        lost_sources: list[tuple[str, int]] = []
        for tid in list(self._tid_miss.keys()):
            if self._tid_miss[tid] >= threshold:
                lost_sources.append((self.cam.name, tid))
                del self._tid_miss[tid]
                self._confirmed_tids.discard(tid)
                self._pending_detections.pop(tid, None)
        self.last_projection_tids = projection_tids
        self._prune_pending_detections(now)

        self.fps_count += 1
        return CamStepResult(
            overlays=overlays,
            regions=self.cam.regions,
            person_events=person_events,
            lost_sources=lost_sources,
            osc_packets=osc_packets,
            speed_ms=speed_ms,
            step_ms=(time.perf_counter() - step_started) * 1000.0,
        )


def _require_mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _require_sequence(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a list")
    return value


def _require_uv_rect(value: object, label: str) -> tuple[float, float, float, float]:
    rect = _require_sequence(value, label)
    if len(rect) != 4:
        raise ConfigError(f"{label} must contain 4 values [u0, v0, u1, v1]")
    try:
        return tuple(float(v) for v in rect)
    except (TypeError, ValueError) as ex:
        raise ConfigError(f"{label} must contain numeric values") from ex


def _optional_uv_rect(
    value: object,
    label: str,
) -> Optional[tuple[float, float, float, float]]:
    if value in (None, ""):
        return None
    return _require_uv_rect(value, label)


def _optional_bool(value: object, default: bool, label: str) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    raise ConfigError(f"{label} must be a boolean")


def _optional_points(value: object, label: str) -> list[tuple[float, float]]:
    if value is None:
        return []
    points = _require_sequence(value, label)
    if len(points) < 3:
        raise ConfigError(f"{label} must contain at least 3 points")
    out: list[tuple[float, float]] = []
    for idx, point in enumerate(points):
        if not isinstance(point, list) or len(point) != 2:
            raise ConfigError(f"{label}[{idx}] must be [x, y]")
        try:
            out.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError) as ex:
            raise ConfigError(f"{label}[{idx}] must contain numeric values") from ex
    return out


def _optional_alias_points(
    entry: dict,
    primary: str,
    alias: str,
    label: str,
) -> list[tuple[float, float]]:
    if primary in entry and alias in entry:
        raise ConfigError(
            f"{label} cannot define both {primary!r} and {alias!r}; use {primary!r}"
        )
    key = primary if primary in entry else alias
    return _optional_points(entry.get(key), f"{label}.{key}")


def _optional_uv_quad(
    value: object,
    label: str,
) -> Optional[list[tuple[float, float]]]:
    if value in (None, ""):
        return None
    points = _require_sequence(value, label)
    try:
        return validate_uv_quad(points, label)
    except (TypeError, ValueError) as ex:
        raise ConfigError(str(ex)) from ex


def _require_field(entry: dict, key: str, label: str) -> object:
    if key not in entry or entry[key] in (None, ""):
        raise ConfigError(f"{label} is missing required field '{key}'")
    return entry[key]


def _validate_camera_url(url: object, label: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"{label}.url must be a non-empty RTSP URL or local video path")
    if "<" in url or ">" in url:
        raise ConfigError(
            f"{label}.url still contains placeholder values. Copy "
            "config.example.yaml to config.yaml, then replace <camera-ip> and "
            "<urlencoded-password> with the real Reolink RTSP URL."
        )
    parts = urlsplit(url)
    if parts.scheme.lower() == "rtsp" and parts.netloc:
        return url
    if parts.scheme.lower() == "file":
        path = Path(parts.path)
        if path.exists():
            return str(path)
        raise ConfigError(f"{label}.url video file does not exist: {path}")
    if not parts.scheme:
        path = Path(url).expanduser()
        if path.exists():
            return str(path)
        raise ConfigError(f"{label}.url video file does not exist: {path}")
    if parts.scheme.lower() != "rtsp" or not parts.netloc:
        raise ConfigError(
            f"{label}.url must be an rtsp:// URL or local video path, got {_redact_url(url)}"
        )
    return url


def _parse_projections(cfg: dict) -> dict[str, Projection]:
    out: dict[str, Projection] = {}
    projection_entries = _require_sequence(
        cfg.get("projections", []) or [],
        "projections",
    )
    for idx, entry in enumerate(projection_entries):
        label = f"projections[{idx}]"
        entry = _require_mapping(entry, label)
        pid = _require_field(entry, "id", label)
        ps = entry.get("pixel_size")
        ws = entry.get("world_size_m")
        out[pid] = Projection(
            id=pid,
            pixel_size=tuple(ps) if ps else None,
            world_size_m=tuple(ws) if ws else None,
            output_warp_points=_optional_uv_quad(
                entry.get("output_warp_points"),
                f"{label}.output_warp_points",
            ),
            interaction_zones=_parse_interaction_zones(entry, pid, label),
        )
    return out


def _parse_interaction_zones(
    projection_entry: dict,
    projection_id: str,
    projection_label: str,
) -> list[InteractionZone]:
    zones: list[InteractionZone] = []
    zone_entries = _require_sequence(
        projection_entry.get("interaction_zones", []) or [],
        f"{projection_label}.interaction_zones",
    )
    seen: set[str] = set()
    for idx, entry in enumerate(zone_entries):
        label = f"{projection_label}.interaction_zones[{idx}]"
        entry = _require_mapping(entry, label)
        zid = str(_require_field(entry, "id", label))
        if zid in seen:
            raise ConfigError(
                f"{projection_label}.interaction_zones id {zid!r} is duplicated"
            )
        seen.add(zid)
        uv_rect = _require_uv_rect(
            entry.get("uv_rect"),
            f"{label} {zid}.uv_rect",
        )
        _validate_unit_uv_rect(uv_rect, f"{label} {zid}.uv_rect")
        zones.append(
            InteractionZone(
                projection_id=projection_id,
                id=zid,
                uv_rect=uv_rect,
                release_after_s=max(float(entry.get("release_after_s", 0.6)), 0.0),
            )
        )
    return zones


def _validate_unit_uv_rect(
    rect: tuple[float, float, float, float],
    label: str,
) -> None:
    u0, v0, u1, v1 = rect
    if not (0.0 <= u0 < u1 <= 1.0 and 0.0 <= v0 < v1 <= 1.0):
        raise ConfigError(
            f"{label} must satisfy 0 <= u0 < u1 <= 1 and 0 <= v0 < v1 <= 1, got {rect}"
        )


def _parse_regions(cam_entry: dict, projections: dict[str, Projection]) -> list[Region]:
    regions: list[Region] = []
    cam_name = cam_entry.get("name", "<unnamed>")
    region_entries = _require_sequence(
        cam_entry.get("regions", []) or [],
        f"camera {cam_name}.regions",
    )
    for idx, r in enumerate(region_entries):
        label = f"camera {cam_name} region[{idx}]"
        r = _require_mapping(r, label)
        rid = _require_field(r, "id", label)
        pid = _require_field(r, "projection_id", f"{label} {rid}")
        if pid not in projections:
            known = ", ".join(sorted(projections.keys())) or "(none configured)"
            raise ConfigError(
                f"{label} {rid} references unknown projection_id={pid!r}; "
                f"known projections: {known}"
            )
        if "projection_uv" not in r:
            raise ConfigError(f"{label} {rid} is missing required field 'projection_uv'")
        if "image_points" not in r:
            raise ConfigError(f"{label} {rid} is missing required field 'image_points'")
        proj_uv = _require_uv_rect(r["projection_uv"], f"{label} {rid}.projection_uv")
        if "dispatch_uv" in r:
            disp_uv = _require_uv_rect(r["dispatch_uv"], f"{label} {rid}.dispatch_uv")
        else:
            disp_uv = proj_uv
        image_points = _require_sequence(r["image_points"], f"{label} {rid}.image_points")
        body_catch_points = _optional_points(
            r.get("body_catch_points"),
            f"{label} {rid}.body_catch_points",
        )
        relaxed_presence_points = _optional_alias_points(
            r,
            "relaxed_presence_points",
            "stair_catch_points",
            f"{label} {rid}",
        )
        relaxed_presence_enabled = _optional_bool(
            r.get("relaxed_presence_enabled"),
            True,
            f"{label} {rid}.relaxed_presence_enabled",
        )
        relaxed_presence_uv = _optional_uv_rect(
            r.get("relaxed_presence_uv"),
            f"{label} {rid}.relaxed_presence_uv",
        )
        if relaxed_presence_uv is not None:
            _validate_unit_uv_rect(
                relaxed_presence_uv,
                f"{label} {rid}.relaxed_presence_uv",
            )
        try:
            body_catch_margin_uv = max(float(r.get("body_catch_margin_uv", 0.0)), 0.0)
            body_catch_min_confidence = max(
                float(r.get("body_catch_min_confidence", 0.0)),
                0.0,
            )
            relaxed_presence_margin_uv = max(
                float(r.get("relaxed_presence_margin_uv", 0.0)),
                0.0,
            )
            relaxed_presence_min_confidence = max(
                float(
                    r.get(
                        "relaxed_presence_min_confidence",
                        r.get("stair_catch_min_confidence", 0.0),
                    )
                ),
                0.0,
            )
            relaxed_presence_v = (
                None
                if r.get("relaxed_presence_v") in (None, "")
                else min(max(float(r.get("relaxed_presence_v")), 0.0), 1.0)
            )
        except (TypeError, ValueError) as ex:
            raise ConfigError(
                f"{label} {rid} catch margins/confidence values must be numeric"
            ) from ex
        try:
            validate_dispatch(proj_uv, disp_uv)
            H = build_homography([tuple(p) for p in image_points], proj_uv)
            relaxed_presence_H = None
            if relaxed_presence_enabled and relaxed_presence_uv is not None:
                if not relaxed_presence_points:
                    raise ValueError(
                        "relaxed_presence_uv requires relaxed_presence_points"
                    )
                relaxed_presence_H = build_homography(
                    relaxed_presence_points,
                    relaxed_presence_uv,
                )
        except (TypeError, ValueError) as ex:
            raise ConfigError(f"{label} {rid}: {ex}") from ex
        regions.append(
            Region(
                id=rid,
                projection_id=pid,
                image_points=[tuple(p) for p in image_points],
                projection_uv=proj_uv,
                dispatch_uv=disp_uv,
                min_bbox_height_px=int(r.get("min_bbox_height_px", 0)),
                body_catch_points=body_catch_points,
                body_catch_margin_uv=body_catch_margin_uv,
                body_catch_min_confidence=body_catch_min_confidence,
                relaxed_presence_enabled=relaxed_presence_enabled,
                relaxed_presence_points=relaxed_presence_points,
                relaxed_presence_uv=relaxed_presence_uv,
                relaxed_presence_margin_uv=relaxed_presence_margin_uv,
                relaxed_presence_min_confidence=relaxed_presence_min_confidence,
                relaxed_presence_v=relaxed_presence_v,
                H=H,
                relaxed_presence_H=relaxed_presence_H,
            )
        )
    return regions


def _parse_body_catch_inference_crop(
    camera_entry: dict,
    label: str,
) -> BodyCatchInferenceCropCfg:
    raw = camera_entry.get("body_catch_inference_crop", False)
    default_margin = camera_entry.get("body_catch_crop_margin_px", 96)
    if isinstance(raw, dict):
        enabled = _optional_bool(
            raw.get("enabled"),
            False,
            f"{label}.body_catch_inference_crop.enabled",
        )
        margin_value = raw.get("margin_px", default_margin)
    else:
        enabled = _optional_bool(raw, False, f"{label}.body_catch_inference_crop")
        margin_value = default_margin
    try:
        margin_px = max(int(margin_value), 0)
    except (TypeError, ValueError) as ex:
        raise ConfigError(
            f"{label}.body_catch_inference_crop.margin_px must be an integer"
        ) from ex
    return BodyCatchInferenceCropCfg(enabled=enabled, margin_px=margin_px)


def _parse_cameras(cfg: dict, projections: dict[str, Projection]) -> list[CamCfg]:
    if "cameras" not in cfg:
        raise ConfigError("config is missing required top-level 'cameras' list")
    camera_entries = _require_sequence(cfg.get("cameras"), "cameras")
    if not camera_entries:
        raise ConfigError("config must define at least one camera in 'cameras'")

    cams: list[CamCfg] = []
    seen_names: set[str] = set()
    for idx, entry in enumerate(camera_entries):
        label = f"cameras[{idx}]"
        entry = _require_mapping(entry, label)
        name = _require_field(entry, "name", label)
        if name in seen_names:
            raise ConfigError(f"camera name {name!r} is duplicated")
        seen_names.add(name)
        url = _validate_camera_url(
            _require_field(entry, "url", label),
            f"camera {name}",
        )
        cams.append(
            CamCfg(
                name=name,
                url=url,
                osc_prefix=entry.get("osc_prefix", f"/cam/{name}"),
                regions=_parse_regions(entry, projections),
                tracking_enabled=_optional_bool(
                    entry.get("tracking_enabled"),
                    True,
                    f"{label}.tracking_enabled",
                ),
                body_catch_inference_crop=_parse_body_catch_inference_crop(
                    entry,
                    label,
                ),
            )
        )
    return cams


def _parse_detection_filter(cfg: dict) -> DetectionFilterCfg:
    raw = cfg.get("detection_filter", {}) or {}
    raw = _require_mapping(raw, "detection_filter")
    return DetectionFilterCfg(
        enabled=bool(raw.get("enabled", True)),
        min_confidence=float(raw.get("min_confidence", 0.28)),
        min_bbox_height_px=float(raw.get("min_bbox_height_px", 42.0)),
        min_bbox_area_px=float(raw.get("min_bbox_area_px", 900.0)),
        min_aspect_h_over_w=float(raw.get("min_aspect_h_over_w", 1.15)),
        max_aspect_h_over_w=float(raw.get("max_aspect_h_over_w", 5.8)),
        max_width_over_height=float(raw.get("max_width_over_height", 1.05)),
        projection_inner_margin_uv=float(raw.get("projection_inner_margin_uv", 0.0)),
        confirm_hits=max(int(raw.get("confirm_hits", 3)), 1),
        confirm_window_s=max(float(raw.get("confirm_window_s", 0.8)), 0.0),
        relaxed_min_confidence=float(raw.get("relaxed_min_confidence", 0.12)),
        relaxed_min_bbox_height_px=float(raw.get("relaxed_min_bbox_height_px", 24.0)),
        relaxed_min_bbox_area_px=float(raw.get("relaxed_min_bbox_area_px", 500.0)),
        relaxed_min_aspect_h_over_w=float(
            raw.get("relaxed_min_aspect_h_over_w", 0.45)
        ),
        relaxed_max_aspect_h_over_w=float(
            raw.get("relaxed_max_aspect_h_over_w", 6.5)
        ),
        relaxed_max_width_over_height=float(
            raw.get("relaxed_max_width_over_height", 2.4)
        ),
    )


def _parse_processing(cfg: dict) -> ProcessingCfg:
    raw = cfg.get("processing", {}) or {}
    raw = _require_mapping(raw, "processing")
    return ProcessingCfg(
        parallel=bool(raw.get("parallel", True)),
        max_frame_age_s=max(float(raw.get("max_frame_age_s", 1.0)), 0.0),
        result_queue_size=max(int(raw.get("result_queue_size", 4)), 1),
    )


def _validate_dispatch_overlaps(cams: list[CamCfg]) -> None:
    by_proj: dict[str, list[tuple[str, str, tuple[float, float, float, float]]]] = {}
    for cam in cams:
        if not cam.tracking_enabled:
            continue
        for reg in cam.regions:
            by_proj.setdefault(reg.projection_id, []).append(
                (cam.name, reg.id, reg.dispatch_uv)
            )
    for proj_id, entries in by_proj.items():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a_cam, a_rid, a_rect = entries[i]
                b_cam, b_rid, b_rect = entries[j]
                if a_cam == b_cam and a_rid == b_rid:
                    continue
                if dispatches_overlap(a_rect, b_rect):
                    raise ConfigError(
                        "dispatch_uv overlap in projection "
                        f"{proj_id!r}: {a_cam}:{a_rid} overlaps {b_cam}:{b_rid}. "
                        "Projection overlap is allowed for hand-off, but "
                        "dispatch_uv slices must only touch edges or be disjoint."
                    )


def load_cfg(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if cfg is None:
        raise ConfigError(f"{path} is empty")
    return _require_mapping(cfg, str(path))


def _region_to_cfg(region: Region) -> dict:
    out = {
        "id": region.id,
        "projection_id": region.projection_id,
        "image_points": [[round(float(x), 1), round(float(y), 1)]
                         for x, y in region.image_points],
        "projection_uv": [float(v) for v in region.projection_uv],
        "dispatch_uv": [float(v) for v in region.dispatch_uv],
    }
    if region.min_bbox_height_px:
        out["min_bbox_height_px"] = int(region.min_bbox_height_px)
    if region.body_catch_points:
        out["body_catch_points"] = [[round(float(x), 1), round(float(y), 1)]
                                    for x, y in region.body_catch_points]
        out["body_catch_margin_uv"] = float(region.body_catch_margin_uv)
        out["body_catch_min_confidence"] = float(region.body_catch_min_confidence)
    if region.relaxed_presence_points:
        if not region.relaxed_presence_enabled:
            out["relaxed_presence_enabled"] = False
        out["relaxed_presence_points"] = [
            [round(float(x), 1), round(float(y), 1)]
            for x, y in region.relaxed_presence_points
        ]
        if region.relaxed_presence_uv is not None:
            out["relaxed_presence_uv"] = [float(v) for v in region.relaxed_presence_uv]
        out["relaxed_presence_margin_uv"] = float(region.relaxed_presence_margin_uv)
        out["relaxed_presence_min_confidence"] = float(
            region.relaxed_presence_min_confidence
        )
        if region.relaxed_presence_v is not None:
            out["relaxed_presence_v"] = float(region.relaxed_presence_v)
    return out


def _zone_to_cfg(zone: InteractionZone) -> dict:
    return {
        "id": zone.id,
        "uv_rect": [float(v) for v in zone.uv_rect],
        "release_after_s": float(zone.release_after_s),
    }


def _all_interaction_zones(
    projections: dict[str, Projection],
) -> list[InteractionZone]:
    zones: list[InteractionZone] = []
    for projection in projections.values():
        zones.extend(projection.interaction_zones)
    return zones


def _warp_persons_for_output(
    projections: dict[str, Projection],
    persons: list,
) -> list:
    """Return output-facing person copies with projection output warp applied.

    Fusion state stays in the unwarped shared UV space. OSC, telemetry, viewer
    overlays, and interaction zones consume these copies so physical projection
    correction does not feed back into hand-off matching.
    """
    out = []
    for person in persons:
        projection = projections.get(person.projection_id)
        warp_points = projection.output_warp_points if projection is not None else None
        warped_u, warped_v = (
            warp_uv((person.u, person.v), warp_points)
            if warp_points
            else (person.u, person.v)
        )
        warped_vx, warped_vy = (
            warp_uv_velocity((person.u, person.v), (person.vx, person.vy), warp_points)
            if warp_points
            else (person.vx, person.vy)
        )
        out.append(
            replace(
                person,
                u=warped_u,
                v=warped_v,
                vx=warped_vx,
                vy=warped_vy,
                raw_u=person.u,
                raw_v=person.v,
            )
        )
    return out


def save_cfg(path: Path, cfg: dict) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def _config_mtime_ns(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _reload_runtime_calibration(
    config_path: Path,
    projections: dict[str, Projection],
    workers: list[CamWorker],
) -> tuple[dict, list[tuple[str, int]]]:
    next_cfg = load_cfg(config_path)
    next_projections = _parse_projections(next_cfg)
    next_cams = _parse_cameras(next_cfg, next_projections)
    next_detection_filter = _parse_detection_filter(next_cfg)
    _validate_dispatch_overlaps(next_cams)

    next_by_name = {cam.name: cam for cam in next_cams}
    missing = [worker.cam.name for worker in workers if worker.cam.name not in next_by_name]
    if missing:
        raise ConfigError(
            "live reload cannot remove active camera(s): " + ", ".join(sorted(missing))
        )

    forced_lost_sources: list[tuple[str, int]] = []
    for worker in workers:
        next_cam = next_by_name[worker.cam.name]
        forced_lost_sources.extend(
            worker.update_runtime_calibration(
                next_projections,
                next_detection_filter,
                next_cam.tracking_enabled,
                next_cam.body_catch_inference_crop,
                next_cam.regions,
                preserve_tracking=True,
            )
        )
    # Keep the original dict object alive for the main loop and viewer.
    projections.clear()
    projections.update(next_projections)
    return next_cfg, forced_lost_sources


def _emit_person_osc(
    osc: SimpleUDPClient,
    projections: dict[str, Projection],
    persons: list,
    lost_gids: list[LostPerson],
    td_minimal: bool,
    person_level: bool = True,
) -> int:
    """Send person-keyed OSC for the current frame.

    In the default TouchDesigner-minimal mode, each projection emits `/active`,
    `/person_zones`, `/xy`, `/uv`, and a compatibility `/persons/count`. `/xy`
    is packed as gid/x/y triples so TD can build one instancing table without
    dynamic per-person addresses. x/y are projection video pixels when
    `pixel_size` is configured; otherwise they fall back to normalized UV.
    `/uv` is always normalized 0..1 and mirrors the same gid order.

    When `person_level` is enabled, the older richer person/lost/list addresses
    are also emitted. This is intentionally additive so a TD patch that cannot
    unpack the variable-length `/xy` list can consume `/person/<gid>` rows
    without changing the primary minimal stream.
    Returns the number of OSC messages sent.
    """
    sent = 0
    by_proj: dict[str, list] = {pid: [] for pid in projections}
    for p in persons:
        by_proj.setdefault(p.projection_id, []).append(p)
    if td_minimal:
        for pid, plist in by_proj.items():
            proj = projections.get(pid)
            gids = sorted(p.gid for p in plist)
            zone_args = []
            xy_args = []
            uv_args = []
            for p in sorted(plist, key=lambda person: person.gid):
                zone_args.extend([
                    p.gid,
                    _source_zone_code(getattr(p, "source_zone", "floor")),
                ])
                if proj is not None and proj.pixel_size is not None:
                    pw, ph = proj.pixel_size
                    x, y = p.u * pw, p.v * ph
                else:
                    x, y = p.u, p.v
                xy_args.extend([p.gid, x, y])
                uv_args.extend([p.gid, p.u, p.v])
            osc.send_message(f"/proj/{pid}/active", gids)
            osc.send_message(f"/proj/{pid}/person_zones", zone_args)
            osc.send_message(f"/proj/{pid}/xy", xy_args)
            osc.send_message(f"/proj/{pid}/uv", uv_args)
            osc.send_message(f"/proj/{pid}/persons/count", len(gids))
            sent += 5
        if person_level:
            sent += _emit_person_level_osc(
                osc,
                projections,
                by_proj,
                lost_gids,
                include_count=False,
            )
        return sent
    if not person_level:
        return sent
    sent += _emit_person_level_osc(
        osc,
        projections,
        by_proj,
        lost_gids,
        include_count=True,
    )
    return sent


def _emit_osc_packets(osc: SimpleUDPClient, packets: list[OscPacket]) -> int:
    for packet in packets:
        osc.send_message(packet.address, packet.args)
    return len(packets)


def _emit_person_level_osc(
    osc: SimpleUDPClient,
    projections: dict[str, Projection],
    by_proj: dict[str, list],
    lost_gids: list[LostPerson],
    include_count: bool,
) -> int:
    sent = 0
    for lost in lost_gids:
        osc.send_message(f"/proj/{lost.projection_id}/person/{lost.gid}/lost", [])
        sent += 1
    for pid, plist in by_proj.items():
        proj = projections.get(pid)
        for p in plist:
            args = [p.u, p.v, p.vx, p.vy, p.conf]
            if proj is not None and proj.pixel_size is not None:
                pw, ph = proj.pixel_size
                args.extend([p.u * pw, p.v * ph])
            osc.send_message(f"/proj/{pid}/person/{p.gid}", args)
            zone_name = _source_zone_name(getattr(p, "source_zone", "floor"))
            osc.send_message(
                f"/proj/{pid}/person/{p.gid}/source_zone",
                [_source_zone_code(zone_name), zone_name],
            )
            sent += 2
        gids = sorted(p.gid for p in plist)
        osc.send_message(f"/proj/{pid}/persons", gids)
        sent += 1
        if include_count:
            osc.send_message(f"/proj/{pid}/persons/count", len(gids))
            sent += 1
    return sent


def _source_zone_name(value: object) -> str:
    zone = str(value or "floor")
    if zone in SOURCE_ZONE_CODES:
        return zone
    return "floor"


def _source_zone_code(value: object) -> int:
    return SOURCE_ZONE_CODES[_source_zone_name(value)]


def _emit_zone_osc(osc: SimpleUDPClient, update: ZoneUpdate) -> int:
    """Send interaction-zone OSC for the current person heartbeat."""
    sent = 0
    for transition in update.transitions:
        base = (
            f"/proj/{transition.projection_id}/zone/{transition.zone_id}"
            f"/person/{transition.gid}"
        )
        if transition.kind == "enter":
            osc.send_message(f"{base}/enter", [transition.zone_u, transition.zone_v])
        elif transition.kind == "leave":
            osc.send_message(f"{base}/leave", [transition.reason_code, transition.dwell_s])
        else:
            continue
        sent += 1
    for person in update.persons:
        osc.send_message(
            f"/proj/{person.projection_id}/zone/{person.zone_id}/person/{person.gid}",
            [
                person.u,
                person.v,
                person.zone_u,
                person.zone_v,
                person.vx,
                person.vy,
                person.dwell_s,
                person.presence,
                person.state_code,
            ],
        )
        sent += 1
    for (pid, zid), count in sorted(update.counts.items()):
        osc.send_message(f"/proj/{pid}/zone/{zid}/count", int(count))
        sent += 1
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--config",
        default=str(repo_default_config_path()),
        help=(
            "config path; the Tauri app runtime config is preferred when this "
            "points at the repo-local default"
        ),
    )
    ap.add_argument("--show", action="store_true", help="open the operator viewer")
    ap.add_argument("--model", default=None, help="override model path")
    ap.add_argument("--device", default=None, help="override device (mps|cpu|cuda)")
    args = ap.parse_args()

    requested_config_path = Path(args.config).expanduser()
    config_path = resolve_config_path(requested_config_path)
    try:
        cfg = load_cfg(config_path)
        projections = _parse_projections(cfg)
        cams = _parse_cameras(cfg, projections)
        detection_filter = _parse_detection_filter(cfg)
        processing = _parse_processing(cfg)
        _validate_dispatch_overlaps(cams)
    except (OSError, yaml.YAMLError, ConfigError) as ex:
        print(f"config error: {ex}", file=sys.stderr)
        return 2

    if config_path.resolve(strict=False) != requested_config_path.resolve(strict=False):
        print(f"config: using Tauri app runtime config {config_path}")

    osc_cfg = cfg.get("osc", {}) or {}
    osc = SimpleUDPClient(osc_cfg.get("host", "127.0.0.1"), int(osc_cfg.get("port", 7000)))
    legacy_image_space = bool(osc_cfg.get("legacy_image_space", False))
    td_minimal = bool(osc_cfg.get("td_minimal", True))
    person_level = bool(osc_cfg.get("person_level", True))
    raw_per_cam = bool(osc_cfg.get("raw_per_cam", not td_minimal)) and not td_minimal
    zone_level = bool(osc_cfg.get("zone_level", not td_minimal)) and not td_minimal
    heartbeat_interval_s = max(float(osc_cfg.get("heartbeat_interval_s", 0.1)), 0.02)
    print(
        f"OSC -> {osc_cfg.get('host', '127.0.0.1')}:{osc_cfg.get('port', 7000)} "
        f"(td_minimal={td_minimal} "
        f"person_level={person_level} raw_per_cam={raw_per_cam} "
        f"zone_level={zone_level} "
        f"legacy_image_space={legacy_image_space})"
    )
    print(
        f"projections: {sorted(projections.keys()) or '(none — only legacy or no-op)'}"
    )

    fusion_cfg = cfg.get("fusion", {}) or {}
    miss_buffer_frames = max(int(fusion_cfg.get("miss_buffer_frames", 8)), 0)
    match_uv_radius = float(fusion_cfg.get("match_uv_radius", 0.05))
    fusion_enabled = td_minimal or person_level or zone_level
    if fusion_enabled:
        person_tracker = PersonTracker(
            hand_off_window_s=float(fusion_cfg.get("hand_off_window_s", 2.5)),
            match_uv_radius=match_uv_radius,
            velocity_alpha=float(fusion_cfg.get("velocity_alpha", 0.3)),
            position_alpha=float(fusion_cfg.get("position_alpha", 0.45)),
            hold_boundary_margin_uv=float(fusion_cfg.get("hold_boundary_margin_uv", 0.08)),
            overlap_duplicate_radius_uv=float(
                fusion_cfg.get("overlap_duplicate_radius_uv", 0.04)
            ),
            max_update_jump_uv=float(fusion_cfg.get("max_update_jump_uv", 0.0)),
            relaxed_hold_s=float(fusion_cfg.get("relaxed_hold_s", 3.0)),
            reuse_lost_gids=bool(fusion_cfg.get("reuse_lost_gids", True)),
        )
        zone_tracker = InteractionZoneTracker()
        print(
            f"fusion: hand_off_window_s={person_tracker.hand_off_window_s} "
            f"match_uv_radius={person_tracker.match_uv_radius} "
            f"velocity_alpha={person_tracker.velocity_alpha} "
            f"position_alpha={person_tracker.position_alpha} "
            f"hold_boundary_margin_uv={person_tracker.hold_boundary_margin_uv} "
            f"overlap_duplicate_radius_uv={person_tracker.overlap_duplicate_radius_uv} "
            f"max_update_jump_uv={person_tracker.max_update_jump_uv} "
            f"relaxed_hold_s={person_tracker.relaxed_hold_s} "
            f"reuse_lost_gids={person_tracker.reuse_lost_gids} "
            f"miss_buffer_frames={miss_buffer_frames}"
        )
    else:
        person_tracker = None
        zone_tracker = None

    model_path = args.model or cfg.get("model", "yolo26n.pt")
    device = args.device or cfg.get("device") or ("mps" if sys.platform == "darwin" else "cpu")
    imgsz = int(cfg.get("imgsz", 640))
    conf = float(cfg.get("conf", 0.35))
    iou = float(cfg.get("iou", 0.5))  # inert under YOLO26 but ultralytics tolerates it
    tracker = cfg.get("tracker", "botsort.yaml")

    print(
        f"model={model_path} device={device} imgsz={imgsz} conf={conf} iou={iou} tracker={tracker}"
    )
    print(
        "detection_filter: "
        f"enabled={detection_filter.enabled} "
        f"min_confidence={detection_filter.min_confidence} "
        f"min_bbox_height_px={detection_filter.min_bbox_height_px} "
        f"min_bbox_area_px={detection_filter.min_bbox_area_px} "
        f"aspect={detection_filter.min_aspect_h_over_w}..{detection_filter.max_aspect_h_over_w} "
        f"relaxed_conf={detection_filter.relaxed_min_confidence} "
        f"relaxed_aspect={detection_filter.relaxed_min_aspect_h_over_w}..{detection_filter.relaxed_max_aspect_h_over_w} "
        f"confirm_hits={detection_filter.confirm_hits}/{detection_filter.confirm_window_s:.1f}s"
    )
    print(
        "processing: "
        f"parallel={processing.parallel} "
        f"max_frame_age_s={processing.max_frame_age_s:.2f} "
        f"result_queue_size={processing.result_queue_size}"
    )
    crop_cams = [
        f"{cam.name}(margin={cam.body_catch_inference_crop.margin_px}px)"
        for cam in cams
        if cam.body_catch_inference_crop.enabled
    ]
    if crop_cams:
        print("body-catch inference crop: " + ", ".join(crop_cams))
    disabled_cams = [cam.name for cam in cams if not cam.tracking_enabled]
    if disabled_cams:
        print("tracking disabled: " + ", ".join(disabled_cams))

    grabbers = [FrameGrabber(c) for c in cams]
    for g in grabbers:
        g.start()

    workers = [
        CamWorker(c, model_path, device, osc, projections, legacy_image_space,
                  raw_per_cam=raw_per_cam, miss_buffer_frames=miss_buffer_frames,
                  detection_filter=detection_filter)
        for c in cams
    ]
    camera_processors: list[CameraProcessor] = []
    if processing.parallel:
        camera_processors = [
            CameraProcessor(g, w, imgsz, conf, iou, tracker, processing)
            for g, w in zip(grabbers, workers)
        ]
        for processor in camera_processors:
            processor.start()

    viewer = None
    if args.show:
        # Lazy-import so headless runs don't pay viewer's import cost.
        from viewer import (
            CamFrame,
            FusedPersonFrame,
            NetworkTargetFrame,
            TrackOverlay,
            Viewer,
        )

        def _on_regions_changed(cam_idx: int, regions: list[Region]) -> None:
            workers[cam_idx].update_regions(regions)
            cfg["cameras"][cam_idx]["regions"] = [
                _region_to_cfg(region) for region in regions
            ]

        def _on_zones_changed(
            projection_id: str,
            zones: list[InteractionZone],
        ) -> None:
            projection = projections.get(projection_id)
            if projection is not None:
                projection.interaction_zones = zones
            for entry in cfg.get("projections", []) or []:
                if entry.get("id") == projection_id:
                    entry["interaction_zones"] = [
                        _zone_to_cfg(zone) for zone in zones
                    ]
                    break

        def _on_save() -> None:
            save_cfg(config_path, cfg)
            print(f"saved config: {config_path}")

        network_targets = [
            NetworkTargetFrame(name=name, host=host, port=port, kind=kind)
            for item in (
                [_network_target_from_camera(cam) for cam in cams]
                + [_network_target_from_osc(osc_cfg)]
            )
            if item is not None
            for name, host, port, kind in (item,)
        ]

        viewer = Viewer(
            projections,
            network_targets=network_targets,
            on_regions_changed=_on_regions_changed,
            on_zones_changed=_on_zones_changed,
            on_save=_on_save,
        )

    stop_flag = threading.Event()

    def _sig(*_) -> None:
        print("\nstopping...")
        stop_flag.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    last_fps = time.time()
    last_idx = {c.name: -1 for c in cams}
    # Per-cam latest frame + overlay snapshot for viewer (sticky between detection ticks).
    last_frame: dict[str, Optional[np.ndarray]] = {c.name: None for c in cams}
    last_frame_ts: dict[str, float] = {c.name: 0.0 for c in cams}
    last_overlays: dict[str, list] = {c.name: [] for c in cams}
    last_fps_per_cam: dict[str, float] = {c.name: 0.0 for c in cams}
    last_osc_per_cam: dict[str, float] = {c.name: 0.0 for c in cams}
    camera_stats: dict[str, CameraStatsWindow] = {
        c.name: CameraStatsWindow() for c in cams
    }
    last_fused_persons: list = []
    last_person_osc_mono = 0.0
    heartbeat_count = 0
    heartbeat_osc_messages = 0
    main_processing_ms_sum = 0.0
    main_processing_ms_count = 0
    main_processing_ms_max = 0.0
    last_config_mtime_ns = _config_mtime_ns(config_path)
    last_config_reload_check = time.monotonic()
    mobile_preview_cfg = _mobile_preview_cfg_from_env()
    mobile_preview = MobilePreviewSink(mobile_preview_cfg) if mobile_preview_cfg else None
    if mobile_preview_cfg is not None:
        print(
            "mobile_preview: "
            f"dir={mobile_preview_cfg.directory} "
            f"max_width={mobile_preview_cfg.max_width} "
            f"interval_s={mobile_preview_cfg.interval_s:.2f} "
            f"ttl_s={mobile_preview_cfg.request_ttl_s:.1f}",
            flush=True,
        )

    try:
        while not stop_flag.is_set():
            loop_started = time.perf_counter()
            any_new = False
            frame_events: list[PersonEvent] = []
            frame_lost_sources: list[tuple[str, int]] = []
            now_mono = time.monotonic()

            if now_mono - last_config_reload_check >= CONFIG_RELOAD_INTERVAL_S:
                last_config_reload_check = now_mono
                current_mtime_ns = _config_mtime_ns(config_path)
                if (
                    current_mtime_ns is not None
                    and last_config_mtime_ns is not None
                    and current_mtime_ns != last_config_mtime_ns
                ):
                    try:
                        cfg, reload_lost_sources = _reload_runtime_calibration(
                            config_path,
                            projections,
                            workers,
                        )
                        frame_lost_sources.extend(reload_lost_sources)
                        last_config_mtime_ns = current_mtime_ns
                        print(
                            "config: live calibration reload applied "
                            f"from {config_path}",
                            flush=True,
                        )
                    except (OSError, yaml.YAMLError, ConfigError) as ex:
                        last_config_mtime_ns = current_mtime_ns
                        print(
                            "config: live calibration reload skipped: "
                            f"{ex}",
                            file=sys.stderr,
                            flush=True,
                        )

            if processing.parallel:
                for processor in camera_processors:
                    for result in processor.drain_results():
                        result_updates_persons = (
                            bool(result.step.person_events)
                            or bool(result.step.lost_sources)
                            or bool(result.step.osc_packets)
                        )
                        any_new = any_new or result_updates_persons
                        emitted_raw = _emit_osc_packets(osc, result.step.osc_packets)
                        camera_stats[result.cam_name].add_result(result, emitted_raw)
                        last_frame[result.cam_name] = result.frame
                        last_frame_ts[result.cam_name] = result.frame_ts
                        last_overlays[result.cam_name] = result.step.overlays
                        if mobile_preview is not None:
                            grab = processor.grabber
                            mobile_preview.write(
                                result.cam_name,
                                result.frame,
                                result.step.overlays,
                                last_fps_per_cam[result.cam_name],
                                last_osc_per_cam[result.cam_name],
                                grab.reconnects,
                                time.time() - result.frame_ts if result.frame_ts else 999.0,
                                now_mono,
                            )
                        frame_events.extend(result.step.person_events)
                        frame_lost_sources.extend(result.step.lost_sources)
            else:
                for grab, worker in zip(grabbers, workers):
                    frame, fidx, frame_ts = grab.get()
                    if frame is None or fidx == last_idx[grab.cam.name]:
                        continue
                    previous_idx = last_idx[grab.cam.name]
                    last_idx[grab.cam.name] = fidx
                    frame_age_s = time.time() - frame_ts if frame_ts else 999.0
                    if (
                        processing.max_frame_age_s > 0
                        and frame_age_s > processing.max_frame_age_s
                    ):
                        camera_stats[grab.cam.name].skipped_stale_frames += 1
                        continue
                    step = worker.step(frame, imgsz, conf, iou, tracker, now_mono)
                    step_updates_persons = (
                        bool(step.person_events)
                        or bool(step.lost_sources)
                        or bool(step.osc_packets)
                    )
                    any_new = any_new or step_updates_persons
                    result = CameraProcessResult(
                        cam_name=grab.cam.name,
                        frame=frame,
                        frame_idx=fidx,
                        frame_ts=frame_ts,
                        processed_mono=now_mono,
                        frame_age_s=frame_age_s,
                        step=step,
                        dropped_frames=max(0, fidx - previous_idx - 1) if previous_idx >= 0 else 0,
                    )
                    emitted_raw = _emit_osc_packets(osc, step.osc_packets)
                    camera_stats[grab.cam.name].add_result(result, emitted_raw)
                    last_frame[grab.cam.name] = frame
                    last_frame_ts[grab.cam.name] = frame_ts
                    last_overlays[grab.cam.name] = step.overlays
                    if mobile_preview is not None:
                        mobile_preview.write(
                            grab.cam.name,
                            frame,
                            step.overlays,
                            last_fps_per_cam[grab.cam.name],
                            last_osc_per_cam[grab.cam.name],
                            grab.reconnects,
                            frame_age_s,
                            now_mono,
                        )
                    frame_events.extend(step.person_events)
                    frame_lost_sources.extend(step.lost_sources)

            should_heartbeat = (
                person_tracker is not None
                and now_mono - last_person_osc_mono >= heartbeat_interval_s
            )
            if person_tracker is not None and (
                any_new or frame_lost_sources or should_heartbeat
            ):
                lost_gids: list[LostPerson] = []
                if any_new or frame_lost_sources:
                    raw_persons = person_tracker.update(
                        frame_events, frame_lost_sources, now_mono
                    )
                    persons = _warp_persons_for_output(projections, raw_persons)
                    last_fused_persons = persons
                    lost_gids = person_tracker.drain_lost_gids()
                else:
                    raw_persons = person_tracker.update([], [], now_mono)
                    persons = _warp_persons_for_output(projections, raw_persons)
                    last_fused_persons = persons
                    lost_gids = person_tracker.drain_lost_gids()
                emitted = _emit_person_osc(
                    osc,
                    projections,
                    persons,
                    lost_gids,
                    td_minimal,
                    person_level=person_level,
                )
                if zone_level and zone_tracker is not None:
                    emitted += _emit_zone_osc(
                        osc,
                        zone_tracker.update(
                            _all_interaction_zones(projections),
                            persons,
                            now_mono,
                        ),
                    )
                last_person_osc_mono = now_mono
                # OSC accounting goes onto the first worker so the printed
                # rate still shows non-zero even when raw_per_cam is off.
                if workers and emitted:
                    camera_stats[workers[0].cam.name].osc_messages += emitted
                heartbeat_count += 1
                heartbeat_osc_messages += emitted

            if viewer is not None:
                from viewer import CamFrame, FusedPersonFrame, TrackOverlay  # type: ignore

                cam_frames = []
                now_wall = time.time()
                for grab, worker in zip(grabbers, workers):
                    name = grab.cam.name
                    frame_age = (
                        now_wall - last_frame_ts[name]
                        if last_frame_ts[name] else 999.0
                    )
                    tracks = [
                        TrackOverlay(tid, bbox, c, hits)
                        for (tid, bbox, c, hits) in last_overlays[name]
                    ]
                    cam_frames.append(
                        CamFrame(
                            name=name,
                            frame=last_frame[name],
                            tracks=tracks,
                            regions=worker.cam.regions,
                            fps=last_fps_per_cam[name],
                            osc_rate=last_osc_per_cam[name],
                            reconnects=grab.reconnects,
                            frame_age_s=frame_age,
                        )
                    )
                fused_frames = [
                    FusedPersonFrame(
                        gid=p.gid,
                        projection_id=p.projection_id,
                        u=p.u,
                        v=p.v,
                        vx=p.vx,
                        vy=p.vy,
                        conf=p.conf,
                        state=p.state,
                        source=p.source,
                    )
                    for p in last_fused_persons
                ]
                stats = {}
                if person_tracker is not None:
                    stats = {
                        "spawned": person_tracker.spawned_count,
                        "handoff": person_tracker.handoff_count,
                        "lost": person_tracker.lost_count,
                    }
                if not viewer.render(cam_frames, fused_frames, stats):
                    break

            loop_processing_ms = (time.perf_counter() - loop_started) * 1000.0
            main_processing_ms_sum += loop_processing_ms
            main_processing_ms_count += 1
            main_processing_ms_max = max(main_processing_ms_max, loop_processing_ms)
            if not any_new and viewer is None:
                time.sleep(0.005)

            now = time.time()
            if now - last_fps >= 2.0:
                dt = now - last_fps
                parts = []
                camera_status = []
                for w in workers:
                    snapshot = camera_stats[w.cam.name].snapshot(dt)
                    f = snapshot["fps"]
                    o = snapshot["osc_rate"]
                    last_fps_per_cam[w.cam.name] = f
                    last_osc_per_cam[w.cam.name] = o
                    timing_ms = snapshot["track_step_ms_avg"]
                    parts.append(
                        f"{w.cam.name}={f:.1f}fps osc={o:.0f}/s "
                        f"track={timing_ms:.1f}ms" if timing_ms is not None
                        else f"{w.cam.name}={f:.1f}fps osc={o:.0f}/s"
                    )
                    frame_age = (
                        now - last_frame_ts[w.cam.name]
                        if last_frame_ts[w.cam.name] else None
                    )
                    camera_status.append(
                        {
                            "name": w.cam.name,
                            "fps": f,
                            "osc_rate": o,
                            "reconnects": next(
                                (
                                    grab.reconnects
                                    for grab in grabbers
                                    if grab.cam.name == w.cam.name
                                ),
                                0,
                            ),
                            "frame_age_s": frame_age,
                            "frame_age_s_avg": snapshot["frame_age_s_avg"],
                            "frame_age_s_max": snapshot["frame_age_s_max"],
                            "track_step_ms_avg": snapshot["track_step_ms_avg"],
                            "track_step_ms_max": snapshot["track_step_ms_max"],
                            "camera_timing_ms": snapshot["track_step_ms_avg"],
                            "dropped_frames": snapshot["dropped_frames"],
                            "dropped_results": snapshot["dropped_results"],
                            "skipped_stale_frames": snapshot["skipped_stale_frames"],
                            "yolo_speed_ms": snapshot["yolo_speed_ms"],
                            "tracking_enabled": w.cam.tracking_enabled,
                            "body_catch_inference_crop": (
                                {
                                    "enabled": w.cam.body_catch_inference_crop.enabled,
                                    "margin_px": w.cam.body_catch_inference_crop.margin_px,
                                    "rect": list(w._last_inference_crop_rect)
                                    if w._last_inference_crop_rect is not None
                                    else None,
                                }
                            ),
                        }
                    )
                    camera_stats[w.cam.name] = CameraStatsWindow()
                projection_status = []
                for pid, projection in projections.items():
                    plist = [
                        p
                        for p in last_fused_persons
                        if p.projection_id == pid
                    ]
                    active = sorted(p.gid for p in plist)
                    persons_payload = []
                    xy_payload = []
                    uv_payload = []
                    for p in sorted(plist, key=lambda person: person.gid):
                        if projection.pixel_size is not None:
                            pw, ph = projection.pixel_size
                            x, y = p.u * pw, p.v * ph
                        else:
                            x, y = p.u, p.v
                        xy_payload.extend([p.gid, x, y])
                        uv_payload.extend([p.gid, p.u, p.v])
                        persons_payload.append(
                            {
                                "gid": p.gid,
                                "x": x,
                                "y": y,
                                "u": p.u,
                                "v": p.v,
                                "raw_u": getattr(p, "raw_u", p.u),
                                "raw_v": getattr(p, "raw_v", p.v),
                                "state": p.state,
                                "source": p.source,
                            }
                        )
                    projection_status.append(
                        {
                            "id": pid,
                            "active": active,
                            "xy": xy_payload,
                            "uv": uv_payload,
                            "persons": persons_payload,
                            "output_warp": bool(projection.output_warp_points),
                        }
                    )
                processing_ms_avg = (
                    main_processing_ms_sum / main_processing_ms_count
                    if main_processing_ms_count
                    else None
                )
                print("  ".join(parts))
                print(
                    json.dumps(
                        {
                            "event": "fps_tick",
                            "ts": time.time(),
                            "cameras": camera_status,
                            "projections": projection_status,
                            "settings": {
                                "confirm_hits": detection_filter.confirm_hits,
                                "confirm_window_s": detection_filter.confirm_window_s,
                                "position_alpha": (
                                    person_tracker.position_alpha
                                    if person_tracker is not None
                                    else None
                                ),
                                "heartbeat_interval_s": heartbeat_interval_s,
                                "heartbeat_count": heartbeat_count,
                                "heartbeat_osc_messages": heartbeat_osc_messages,
                                "processing_ms": processing_ms_avg,
                                "processing_ms_max": main_processing_ms_max,
                                "processing_parallel": processing.parallel,
                                "max_frame_age_s": processing.max_frame_age_s,
                                "td_minimal": td_minimal,
                                "person_level": person_level,
                            },
                        },
                        separators=(",", ":"),
                    )
                )
                heartbeat_count = 0
                heartbeat_osc_messages = 0
                main_processing_ms_sum = 0.0
                main_processing_ms_count = 0
                main_processing_ms_max = 0.0
                last_fps = now
    finally:
        for processor in camera_processors:
            processor.stop()
        for g in grabbers:
            g.stop()
        if viewer is not None:
            viewer.close()
        print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
