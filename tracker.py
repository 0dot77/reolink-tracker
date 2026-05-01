"""Reolink RTSP → YOLO person detection + tracker → OSC (shared projection UV).

Usage:
    python tracker.py                # headless, OSC only
    python tracker.py --show         # operator viewer (cv2 window)
    python tracker.py --config foo.yaml

OSC schema (primary, when regions are configured):
    /proj/<projection_id>/cam/<cam>/track/<id>      [u, v, conf, (u_px, v_px)?]
    /proj/<projection_id>/cam/<cam>/track/<id>/lost []
    /proj/<projection_id>/count                     int
    /proj/<projection_id>/cam/<cam>/count           int
    /proj/<projection_id>/cam/<cam>/active          [id, id, ...]

Legacy image-space schema (when osc.legacy_image_space: true):
    <osc_prefix>/track/<id>           [cx, cy, w, h, conf]   normalized 0..1
    <osc_prefix>/track/<id>/lost      []
    <osc_prefix>/count                int
    <osc_prefix>/active               [id, ...]
"""

import argparse
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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

from region import (
    Projection,
    Region,
    build_homography,
    is_inside_uv,
    project,
    validate_dispatch,
)

PERSON_CLASS_ID = 0  # COCO


@dataclass
class CamCfg:
    name: str
    url: str
    osc_prefix: str
    regions: list[Region] = field(default_factory=list)


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
            print(f"[{self.cam.name}] connected: {self.cam.url}")
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    print(f"[{self.cam.name}] read failed; reconnecting")
                    break
                with self._lock:
                    self._latest = frame
                    self._idx += 1
                    self._ts = time.time()
            cap.release()

    def get(self) -> tuple[Optional[np.ndarray], int, float]:
        with self._lock:
            if self._latest is None:
                return None, 0, 0.0
            return self._latest, self._idx, self._ts

    def stop(self) -> None:
        self._stop.set()


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
    ):
        self.cam = cam
        self.osc = osc
        self.model = YOLO(model_path)
        self.device = device
        self.projections = projections
        self.legacy_image_space = legacy_image_space
        # last_ids per region_id; legacy uses the empty-string key.
        self.last_ids: dict[str, set[int]] = {r.id: set() for r in cam.regions}
        self.last_ids[""] = set()  # legacy bucket
        self.fps_count = 0
        self.osc_count = 0

    def update_regions(self, regions: list[Region]) -> None:
        """Hook for v2 viewer-driven region edits."""
        self.cam.regions = regions
        self.last_ids = {r.id: set() for r in regions}
        self.last_ids[""] = set()

    def step(
        self,
        frame: np.ndarray,
        imgsz: int,
        conf: float,
        iou: float,
        tracker: str,
    ) -> tuple[list, list[Region]]:
        """Run one detection+track step. Emits OSC. Returns the per-track overlay list
        (for viewer rendering) and the active regions."""
        results = self.model.track(
            frame,
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
        h, w = frame.shape[:2]

        overlays: list = []  # list[viewer.TrackOverlay] (lazy import below to avoid cycle when --show is off)
        # Per-region active ids this frame.
        active: dict[str, list[int]] = {reg.id: [] for reg in self.cam.regions}
        legacy_active: list[int] = []

        if r.boxes is not None and r.boxes.id is not None:
            ids = r.boxes.id.cpu().numpy().astype(int)
            xyxy = r.boxes.xyxy.cpu().numpy()
            confs = r.boxes.conf.cpu().numpy()

            for tid, box, c in zip(ids, xyxy, confs):
                x1, y1, x2, y2 = (float(v) for v in box)
                bbox_h = y2 - y1
                foot_x = (x1 + x2) / 2.0
                foot_y = y2

                region_hits: list[tuple[str, float, float, bool]] = []
                for reg in self.cam.regions:
                    if reg.H is None:
                        continue
                    if reg.min_bbox_height_px and bbox_h < reg.min_bbox_height_px:
                        # too small — drop entirely (no hit, no dispatch)
                        continue
                    u, v = project((foot_x, foot_y), reg.H)
                    if not is_inside_uv((u, v), reg.projection_uv):
                        continue
                    in_dispatch = is_inside_uv((u, v), reg.dispatch_uv)
                    region_hits.append((reg.id, u, v, in_dispatch))
                    if in_dispatch:
                        proj = self.projections.get(reg.projection_id)
                        addr = f"/proj/{reg.projection_id}/cam/{self.cam.name}/track/{int(tid)}"
                        args = [u, v, float(c)]
                        if proj is not None and proj.pixel_size is not None:
                            pw, ph = proj.pixel_size
                            args.extend([u * pw, v * ph])
                        self.osc.send_message(addr, args)
                        self.osc_count += 1
                        active[reg.id].append(int(tid))

                # Legacy image-space dispatch (independent of regions).
                if self.legacy_image_space:
                    cx = foot_x / w
                    cy = (y1 + y2) / 2.0 / h
                    bw = (x2 - x1) / w
                    bh = bbox_h / h
                    self.osc.send_message(
                        f"{self.cam.osc_prefix}/track/{int(tid)}",
                        [cx, cy, bw, bh, float(c)],
                    )
                    self.osc_count += 1
                    legacy_active.append(int(tid))

                overlays.append((int(tid), (x1, y1, x2, y2), float(c), region_hits))

        # Region-level lost events + per-region count/active.
        for reg in self.cam.regions:
            cur = set(active[reg.id])
            for lost_id in self.last_ids.get(reg.id, set()) - cur:
                self.osc.send_message(
                    f"/proj/{reg.projection_id}/cam/{self.cam.name}/track/{lost_id}/lost",
                    [],
                )
                self.osc_count += 1
            self.last_ids[reg.id] = cur
            self.osc.send_message(
                f"/proj/{reg.projection_id}/cam/{self.cam.name}/count",
                len(cur),
            )
            self.osc_count += 1
            if cur:
                self.osc.send_message(
                    f"/proj/{reg.projection_id}/cam/{self.cam.name}/active",
                    sorted(cur),
                )
                self.osc_count += 1

        # Legacy lost + count + active.
        if self.legacy_image_space:
            cur = set(legacy_active)
            for lost_id in self.last_ids[""] - cur:
                self.osc.send_message(
                    f"{self.cam.osc_prefix}/track/{lost_id}/lost", []
                )
                self.osc_count += 1
            self.last_ids[""] = cur
            self.osc.send_message(f"{self.cam.osc_prefix}/count", len(cur))
            self.osc_count += 1
            if cur:
                self.osc.send_message(
                    f"{self.cam.osc_prefix}/active", sorted(cur)
                )
                self.osc_count += 1

        self.fps_count += 1
        return overlays, self.cam.regions


def _parse_projections(cfg: dict) -> dict[str, Projection]:
    out: dict[str, Projection] = {}
    for entry in cfg.get("projections", []) or []:
        pid = entry["id"]
        ps = entry.get("pixel_size")
        ws = entry.get("world_size_m")
        out[pid] = Projection(
            id=pid,
            pixel_size=tuple(ps) if ps else None,
            world_size_m=tuple(ws) if ws else None,
        )
    return out


def _parse_regions(cam_entry: dict, projections: dict[str, Projection]) -> list[Region]:
    regions: list[Region] = []
    for r in cam_entry.get("regions", []) or []:
        pid = r["projection_id"]
        if pid not in projections:
            print(
                f"warn: camera {cam_entry.get('name')} region {r.get('id')} "
                f"references unknown projection_id={pid}; skipping"
            )
            continue
        proj_uv = tuple(r["projection_uv"])
        disp_uv = tuple(r.get("dispatch_uv", proj_uv))
        try:
            validate_dispatch(proj_uv, disp_uv)
            H = build_homography([tuple(p) for p in r["image_points"]], proj_uv)
        except ValueError as ex:
            print(
                f"warn: camera {cam_entry.get('name')} region {r.get('id')}: {ex}; skipping"
            )
            continue
        regions.append(
            Region(
                id=r["id"],
                projection_id=pid,
                image_points=[tuple(p) for p in r["image_points"]],
                projection_uv=proj_uv,
                dispatch_uv=disp_uv,
                min_bbox_height_px=int(r.get("min_bbox_height_px", 0)),
                H=H,
            )
        )
    return regions


def load_cfg(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    ap.add_argument("--show", action="store_true", help="open the operator viewer")
    ap.add_argument("--model", default=None, help="override model path")
    ap.add_argument("--device", default=None, help="override device (mps|cpu|cuda)")
    args = ap.parse_args()

    cfg = load_cfg(Path(args.config))

    projections = _parse_projections(cfg)

    cams: list[CamCfg] = []
    for c in cfg.get("cameras", []) or []:
        cams.append(
            CamCfg(
                name=c["name"],
                url=c["url"],
                osc_prefix=c.get("osc_prefix", f"/cam/{c['name']}"),
                regions=_parse_regions(c, projections),
            )
        )
    if not cams:
        print("error: no cameras in config")
        return 2

    osc_cfg = cfg.get("osc", {}) or {}
    osc = SimpleUDPClient(osc_cfg.get("host", "127.0.0.1"), int(osc_cfg.get("port", 7000)))
    legacy_image_space = bool(osc_cfg.get("legacy_image_space", False))
    print(
        f"OSC -> {osc_cfg.get('host', '127.0.0.1')}:{osc_cfg.get('port', 7000)} "
        f"(legacy_image_space={legacy_image_space})"
    )
    print(f"projections: {sorted(projections.keys()) or '(none — only legacy or no-op)'}")

    model_path = args.model or cfg.get("model", "yolo26n.pt")
    device = args.device or cfg.get("device") or ("mps" if sys.platform == "darwin" else "cpu")
    imgsz = int(cfg.get("imgsz", 640))
    conf = float(cfg.get("conf", 0.35))
    iou = float(cfg.get("iou", 0.5))  # inert under YOLO26 but ultralytics tolerates it
    tracker = cfg.get("tracker", "botsort.yaml")

    print(f"model={model_path} device={device} imgsz={imgsz} conf={conf} iou={iou} tracker={tracker}")

    grabbers = [FrameGrabber(c) for c in cams]
    for g in grabbers:
        g.start()

    workers = [
        CamWorker(c, model_path, device, osc, projections, legacy_image_space)
        for c in cams
    ]

    viewer = None
    if args.show:
        # Lazy-import so headless runs don't pay viewer's import cost.
        from viewer import CamFrame, TrackOverlay, Viewer

        viewer = Viewer(projections)

    stop_flag = threading.Event()

    def _sig(*_):
        print("\nstopping...")
        stop_flag.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    last_fps = time.time()
    last_idx = {c.name: -1 for c in cams}
    # Per-cam latest frame + overlay snapshot for viewer (sticky between detection ticks).
    last_frame: dict[str, Optional[np.ndarray]] = {c.name: None for c in cams}
    last_overlays: dict[str, list] = {c.name: [] for c in cams}
    last_fps_per_cam: dict[str, float] = {c.name: 0.0 for c in cams}
    last_osc_per_cam: dict[str, float] = {c.name: 0.0 for c in cams}

    try:
        while not stop_flag.is_set():
            any_new = False
            for grab, worker in zip(grabbers, workers):
                frame, fidx, _ = grab.get()
                if frame is None or fidx == last_idx[grab.cam.name]:
                    continue
                last_idx[grab.cam.name] = fidx
                any_new = True
                overlays, _regions = worker.step(frame, imgsz, conf, iou, tracker)
                last_frame[grab.cam.name] = frame
                last_overlays[grab.cam.name] = overlays

            if viewer is not None:
                from viewer import CamFrame, TrackOverlay  # type: ignore

                cam_frames = []
                for grab, worker in zip(grabbers, workers):
                    name = grab.cam.name
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
                        )
                    )
                if not viewer.render(cam_frames):
                    break

            if not any_new and viewer is None:
                time.sleep(0.005)

            now = time.time()
            if now - last_fps >= 2.0:
                dt = now - last_fps
                parts = []
                for w in workers:
                    f = w.fps_count / dt
                    o = w.osc_count / dt
                    last_fps_per_cam[w.cam.name] = f
                    last_osc_per_cam[w.cam.name] = o
                    parts.append(f"{w.cam.name}={f:.1f}fps osc={o:.0f}/s")
                    w.fps_count = 0
                    w.osc_count = 0
                print("  ".join(parts))
                last_fps = now
    finally:
        for g in grabbers:
            g.stop()
        if viewer is not None:
            viewer.close()
        print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
