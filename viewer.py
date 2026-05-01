"""Operator viewer for the reolink-tracker.

Single cv2 window. Composes per-camera tiles with bbox/ID/region overlays
and an optional top-down projection UV canvas. In show mode, focused-camera
regions can be sketched with 4 mouse clicks and written back to config.yaml.
"""

import time
from dataclasses import dataclass
from typing import Callable, Sequence, Optional

import numpy as np
import cv2

from region import Region, Projection, build_homography

WINDOW_NAME = "reolink-tracker"

# BGR palette indexed by camera enumeration order.
_CAM_COLORS: list[tuple[int, int, int]] = [
    (255, 128, 0), (0, 165, 255), (0, 255, 255),
    (255, 0, 255), (0, 255, 128), (128, 0, 255),
]
_C_DISPATCH = (0, 255, 0)
_C_REGION_ONLY = (0, 200, 255)
_C_NO_HIT = (160, 160, 160)
_C_REGION_POLY = (0, 200, 0)
_C_FOCUS = (0, 255, 255)
_C_DRAFT = (0, 220, 255)
_FONT = cv2.FONT_HERSHEY_SIMPLEX


@dataclass
class TrackOverlay:
    """One detected/tracked person on one camera, this frame."""
    track_id: int
    bbox_xyxy: tuple[float, float, float, float]
    conf: float
    # For each region the foot point falls in: (region_id, u, v, in_dispatch).
    region_hits: list[tuple[str, float, float, bool]]


@dataclass
class CamFrame:
    """Per-camera state passed to the viewer each tick."""
    name: str
    frame: Optional[np.ndarray]
    tracks: list[TrackOverlay]
    regions: list[Region]
    fps: float = 0.0
    osc_rate: float = 0.0
    reconnects: int = 0


def _grid_shape(n: int) -> tuple[int, int]:
    return (1, 1) if n <= 1 else (1, 2) if n == 2 else (2, 2)


def _draw_hud(tile: np.ndarray, cam: CamFrame) -> None:
    text = f"{cam.name}  fps={cam.fps:.1f}  osc={cam.osc_rate:.1f}/s  rc={cam.reconnects}"
    (tw, th), base = cv2.getTextSize(text, _FONT, 0.5, 1)
    pad, H, W = 4, tile.shape[0], tile.shape[1]
    x1, y1, x2, y2 = W - tw - 2 * pad - 4, 4, W - 4, 4 + th + base + 2 * pad
    overlay = tile.copy()
    cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.5, tile, 0.5, 0, dst=tile)
    cv2.putText(tile, text, (x1 + pad, y2 - pad - base // 2),
                _FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA)


def _render_tile(cam: CamFrame, tile_size: tuple[int, int], show_hud: bool) -> np.ndarray:
    tw, th = tile_size
    if cam.frame is None:
        tile = np.full((th, tw, 3), 40, dtype=np.uint8)
        msg = f"{cam.name} waiting..."
        (mw, mh), _ = cv2.getTextSize(msg, _FONT, 0.7, 2)
        cv2.putText(tile, msg, ((tw - mw) // 2, (th + mh) // 2),
                    _FONT, 0.7, (200, 200, 200), 2, cv2.LINE_AA)
        if show_hud:
            _draw_hud(tile, cam)
        return tile

    src_h, src_w = cam.frame.shape[:2]
    tile = cv2.resize(cam.frame, (tw, th), interpolation=cv2.INTER_AREA)
    sx, sy = tw / float(src_w), th / float(src_h)

    for reg in cam.regions:
        pts = np.array([(int(round(x * sx)), int(round(y * sy)))
                        for (x, y) in reg.image_points], dtype=np.int32)
        cv2.polylines(tile, [pts], True, _C_REGION_POLY, 1, cv2.LINE_AA)
        u0, v0, u1, v1 = reg.projection_uv
        label = f"{reg.id} [{u0:.2f},{v0:.2f}->{u1:.2f},{v1:.2f}]"
        tlx, tly = int(pts[:, 0].min()), int(pts[:, 1].min())
        cv2.putText(tile, label, (tlx + 2, max(12, tly - 4)),
                    _FONT, 0.4, _C_REGION_POLY, 1, cv2.LINE_AA)

    for t in cam.tracks:
        if any(hit[3] for hit in t.region_hits):
            color = _C_DISPATCH
        elif t.region_hits:
            color = _C_REGION_ONLY
        else:
            color = _C_NO_HIT
        x1, y1, x2, y2 = t.bbox_xyxy
        rx1, ry1 = int(round(x1 * sx)), int(round(y1 * sy))
        rx2, ry2 = int(round(x2 * sx)), int(round(y2 * sy))
        cv2.rectangle(tile, (rx1, ry1), (rx2, ry2), color, 2)
        label = f"id={t.track_id} conf={t.conf:.2f}"
        if t.region_hits:
            rid, u, v, _ = t.region_hits[0]
            label += f" ({rid} u={u:.2f} v={v:.2f})"
        cv2.putText(tile, label, (rx1, max(12, ry1 - 4)),
                    _FONT, 0.45, color, 1, cv2.LINE_AA)

    if show_hud:
        _draw_hud(tile, cam)
    return tile


def _compose_grid(tiles: list[np.ndarray], tile_size: tuple[int, int],
                  focus_idx: int) -> np.ndarray:
    rows, cols = _grid_shape(len(tiles))
    tw, th = tile_size
    canvas = np.zeros((rows * th, cols * tw, 3), dtype=np.uint8)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        x, y = c * tw, r * th
        if i == focus_idx:
            tile = tile.copy()
            cv2.rectangle(tile, (1, 1), (tw - 2, th - 2), _C_FOCUS, 2)
        canvas[y:y + th, x:x + tw] = tile
    return canvas


def _draw_dotted_rect(img: np.ndarray, p0: tuple[int, int], p1: tuple[int, int],
                      color: tuple[int, int, int], dash: int = 3) -> None:
    x0, y0 = p0
    x1, y1 = p1
    if x1 < x0: x0, x1 = x1, x0
    if y1 < y0: y0, y1 = y1, y0
    for x in range(x0, x1, dash * 2):
        cv2.line(img, (x, y0), (min(x + dash, x1), y0), color, 1)
        cv2.line(img, (x, y1), (min(x + dash, x1), y1), color, 1)
    for y in range(y0, y1, dash * 2):
        cv2.line(img, (x0, y), (x0, min(y + dash, y1)), color, 1)
        cv2.line(img, (x1, y), (x1, min(y + dash, y1)), color, 1)


def _render_uv_canvas(cams: Sequence[CamFrame],
                      projections: dict[str, Projection]) -> Optional[np.ndarray]:
    if not projections:
        return None
    panels: list[np.ndarray] = []
    for proj_id, proj in projections.items():
        aspect = (proj.pixel_size[0] / float(proj.pixel_size[1])
                  if proj.pixel_size and proj.pixel_size[1] else 1.0)
        cw = 800
        ch = max(80, min(int(round(800 / aspect)) if aspect > 0 else 800, 1600))
        panel = np.full((ch, cw, 3), 24, dtype=np.uint8)
        cv2.rectangle(panel, (0, 0), (cw - 1, ch - 1), (90, 90, 90), 1)
        cv2.putText(panel, proj_id, (8, 18), _FONT, 0.5,
                    (200, 200, 200), 1, cv2.LINE_AA)

        # Region projection_uv outlines. Newly drawn regions default to the
        # full projection, so inset the visual rectangle to keep it visible
        # instead of hiding under the panel border.
        reg_lookup: dict[tuple[int, str], Region] = {}
        for ci, cam in enumerate(cams):
            color = _CAM_COLORS[ci % len(_CAM_COLORS)]
            for reg in cam.regions:
                reg_lookup[(ci, reg.id)] = reg
                if reg.projection_id != proj_id:
                    continue
                u0, v0, u1, v1 = reg.projection_uv
                x0 = int(round(u0 * (cw - 1)))
                y0 = int(round(v0 * (ch - 1)))
                x1 = int(round(u1 * (cw - 1)))
                y1 = int(round(v1 * (ch - 1)))
                x0, x1 = sorted((x0, x1))
                y0, y1 = sorted((y0, y1))
                x0 = min(max(x0 + 3, 3), cw - 4)
                y0 = min(max(y0 + 3, 24), ch - 4)
                x1 = max(min(x1 - 3, cw - 4), x0 + 1)
                y1 = max(min(y1 - 3, ch - 4), y0 + 1)
                overlay = panel.copy()
                cv2.rectangle(overlay, (x0, y0), (x1, y1), color, -1)
                cv2.addWeighted(overlay, 0.12, panel, 0.88, 0, dst=panel)
                _draw_dotted_rect(
                    panel,
                    (x0, y0),
                    (x1, y1),
                    color,
                )
                label = f"{cam.name}:{reg.id}"
                cv2.putText(panel, label, (x0 + 6, min(y1 - 6, y0 + 18)),
                            _FONT, 0.45, color, 1, cv2.LINE_AA)

        for ci, cam in enumerate(cams):
            color = _CAM_COLORS[ci % len(_CAM_COLORS)]
            letter = (cam.name[:1].upper() if cam.name else "?")
            for t in cam.tracks:
                for rid, u, v, _ in t.region_hits:
                    reg = reg_lookup.get((ci, rid))
                    if reg is None or reg.projection_id != proj_id:
                        continue
                    px = int(round(u * (cw - 1)))
                    py = int(round(v * (ch - 1)))
                    cv2.circle(panel, (px, py), 5, color, -1, cv2.LINE_AA)
                    cv2.putText(panel, letter, (px + 6, py + 4),
                                _FONT, 0.4, color, 1, cv2.LINE_AA)
        panels.append(panel)

    if not panels:
        return None
    max_w = max(p.shape[1] for p in panels)
    padded = [np.hstack([p, np.zeros((p.shape[0], max_w - p.shape[1], 3), dtype=np.uint8)])
              if p.shape[1] < max_w else p for p in panels]
    return np.vstack(padded)


def _hstack_match_height(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    lh, rh = left.shape[0], right.shape[0]
    if rh != lh:
        new_w = max(1, int(round(right.shape[1] * (lh / float(rh)))))
        right = cv2.resize(right, (new_w, lh), interpolation=cv2.INTER_AREA)
    return np.hstack([left, right])


class Viewer:
    """Owns the cv2 window. Call render() each tick with the latest CamFrames."""

    def __init__(
        self,
        projections: dict[str, Projection],
        tile_size: tuple[int, int] = (640, 360),
        on_regions_changed: Optional[Callable[[int, list[Region]], None]] = None,
        on_save: Optional[Callable[[], None]] = None,
    ):
        self.projections = projections
        self.tile_size = tile_size
        self.on_regions_changed = on_regions_changed
        self.on_save = on_save
        self.show_hud = True
        self.show_uv = True
        self.focus_idx = 0
        self.draw_mode = False
        self.draft_points: list[tuple[float, float]] = []
        self.status = (
            "d draw | click 4 pts TL,TR,BR,BL | x delete | w save | "
            "h/u toggles | 1-9 focus | q quit"
        )
        self._last_cams: Sequence[CamFrame] = []
        self._window_created = False

    def _ensure_window(self) -> None:
        if not self._window_created:
            cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
            cv2.setMouseCallback(WINDOW_NAME, self._on_mouse)
            self._window_created = True

    def _on_mouse(self, event: int, x: int, y: int, _flags: int, _param: object) -> None:
        if event != cv2.EVENT_LBUTTONDOWN or not self.draw_mode:
            return
        mapped = self._mouse_to_frame_point(x, y)
        if mapped is None:
            self.status = "click inside the focused camera tile"
            return
        self.draft_points.append(mapped)
        self.status = f"point {len(self.draft_points)}/4 added"
        if len(self.draft_points) == 4:
            self._commit_draft_region()

    def _mouse_to_frame_point(self, x: int, y: int) -> Optional[tuple[float, float]]:
        if self.focus_idx >= len(self._last_cams):
            return None
        cam = self._last_cams[self.focus_idx]
        if cam.frame is None:
            return None
        tw, th = self.tile_size
        _rows, cols = _grid_shape(len(self._last_cams))
        row, col = divmod(self.focus_idx, cols)
        tile_x = x - col * tw
        tile_y = y - row * th
        if not (0 <= tile_x < tw and 0 <= tile_y < th):
            return None
        src_h, src_w = cam.frame.shape[:2]
        return (tile_x * src_w / float(tw), tile_y * src_h / float(th))

    def _next_region_id(self, cam: CamFrame) -> str:
        existing = {r.id for r in cam.regions}
        base = f"{cam.name}_region"
        idx = 1
        while f"{base}_{idx}" in existing:
            idx += 1
        return f"{base}_{idx}"

    def _commit_draft_region(self) -> None:
        if self.focus_idx >= len(self._last_cams) or not self.projections:
            self.draft_points = []
            self.draw_mode = False
            self.status = "cannot create region without a focused camera and projection"
            return
        cam = self._last_cams[self.focus_idx]
        proj_id = next(iter(self.projections))
        projection_uv = (0.0, 0.0, 1.0, 1.0)
        try:
            homography = build_homography(self.draft_points, projection_uv)
        except ValueError as ex:
            self.status = f"region rejected: {ex}"
            self.draft_points = []
            return

        region = Region(
            id=self._next_region_id(cam),
            projection_id=proj_id,
            image_points=list(self.draft_points),
            projection_uv=projection_uv,
            dispatch_uv=projection_uv,
            H=homography,
        )
        regions = [*cam.regions, region]
        if self.on_regions_changed is not None:
            self.on_regions_changed(self.focus_idx, regions)
        self.draft_points = []
        self.draw_mode = False
        self.status = f"added {region.id}; press w to save config"

    def _delete_last_region(self) -> None:
        if self.focus_idx >= len(self._last_cams):
            return
        cam = self._last_cams[self.focus_idx]
        if not cam.regions:
            self.status = f"{cam.name} has no regions to delete"
            return
        removed = cam.regions[-1]
        if self.on_regions_changed is not None:
            self.on_regions_changed(self.focus_idx, list(cam.regions[:-1]))
        self.status = f"deleted {removed.id}; press w to save config"

    def _draw_draft(self, canvas: np.ndarray) -> None:
        if not self.draft_points or self.focus_idx >= len(self._last_cams):
            return
        cam = self._last_cams[self.focus_idx]
        if cam.frame is None:
            return
        tw, th = self.tile_size
        _rows, cols = _grid_shape(len(self._last_cams))
        row, col = divmod(self.focus_idx, cols)
        origin_x, origin_y = col * tw, row * th
        src_h, src_w = cam.frame.shape[:2]
        pts: list[tuple[int, int]] = []
        for px, py in self.draft_points:
            tx = origin_x + int(round(px * tw / float(src_w)))
            ty = origin_y + int(round(py * th / float(src_h)))
            pts.append((tx, ty))
        for i, pt in enumerate(pts):
            cv2.circle(canvas, pt, 5, _C_DRAFT, -1, cv2.LINE_AA)
            cv2.putText(canvas, str(i + 1), (pt[0] + 7, pt[1] - 7),
                        _FONT, 0.5, _C_DRAFT, 1, cv2.LINE_AA)
        if len(pts) >= 2:
            cv2.polylines(canvas, [np.array(pts, dtype=np.int32)], False,
                          _C_DRAFT, 2, cv2.LINE_AA)

    def _draw_status(self, canvas: np.ndarray) -> None:
        lines = [
            self.status,
            "draw order: projection top-left -> top-right -> bottom-right -> bottom-left",
        ]
        pad = 8
        line_h = 20
        y0 = canvas.shape[0] - line_h * len(lines) - pad * 2
        overlay = canvas.copy()
        cv2.rectangle(overlay, (0, max(0, y0)), (canvas.shape[1], canvas.shape[0]),
                      (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.6, canvas, 0.4, 0, dst=canvas)
        for i, line in enumerate(lines):
            cv2.putText(canvas, line, (pad, y0 + pad + line_h * (i + 1) - 5),
                        _FONT, 0.5, (230, 230, 230), 1, cv2.LINE_AA)

    def render(self, cams: Sequence[CamFrame]) -> bool:
        """Compose + show. Returns False to request shutdown (q/Esc pressed)."""
        self._ensure_window()
        self._last_cams = cams
        if not cams:
            blank = np.zeros((self.tile_size[1], self.tile_size[0], 3), dtype=np.uint8)
            cv2.putText(blank, "no cameras", (20, 40),
                        _FONT, 0.7, (200, 200, 200), 2)
            self._draw_status(blank)
            cv2.imshow(WINDOW_NAME, blank)
        else:
            tiles = [_render_tile(c, self.tile_size, self.show_hud) for c in cams]
            canvas = _compose_grid(tiles, self.tile_size, self.focus_idx)
            self._draw_draft(canvas)
            if self.show_uv:
                uv = _render_uv_canvas(cams, self.projections)
                if uv is not None:
                    canvas = _hstack_match_height(canvas, uv)
            self._draw_status(canvas)
            cv2.imshow(WINDOW_NAME, canvas)

        key = cv2.waitKey(1) & 0xFF
        if key == 27 and self.draw_mode:
            self.draw_mode = False
            self.draft_points = []
            self.status = "draw cancelled"
            return True
        if key in (ord("q"), 27):
            return False
        if key == ord("h"):
            self.show_hud = not self.show_hud
        elif key == ord("u"):
            self.show_uv = not self.show_uv
        elif key == ord("d"):
            self.draw_mode = True
            self.draft_points = []
            self.status = "draw mode: click 4 focused-camera points"
        elif key in (8, 127):
            if self.draft_points:
                self.draft_points.pop()
                self.status = f"point removed; {len(self.draft_points)}/4 remain"
        elif key == ord("x"):
            self._delete_last_region()
        elif key == ord("w"):
            if self.on_save is not None:
                self.on_save()
            self.status = "config saved"
        elif ord("1") <= key <= ord("9"):
            idx = key - ord("1")
            if idx < len(cams):
                self.focus_idx = idx
                self.draft_points = []
                self.draw_mode = False
                self.status = f"focused {cams[idx].name}"
        return True

    def close(self) -> None:
        if self._window_created:
            try:
                cv2.destroyWindow(WINDOW_NAME)
            except cv2.error:
                pass
            self._window_created = False


if __name__ == "__main__":
    # Sanity loop: 2 fake cameras with synthetic gradient frames + 1 region each
    # + a couple of fake track overlays. Hard cap at 30 frames so it cannot hang.
    from region import build_homography

    def _gradient(w: int, h: int, shift: int) -> np.ndarray:
        xs = np.linspace(0, 255, w, dtype=np.uint8)
        ys = np.linspace(0, 255, h, dtype=np.uint8)
        gx, gy = np.meshgrid(xs, ys)
        b = ((gx.astype(int) + shift) % 256).astype(np.uint8)
        g = ((gy.astype(int) + shift) % 256).astype(np.uint8)
        r = ((gx.astype(int) + gy.astype(int) + shift) // 2 % 256).astype(np.uint8)
        return cv2.merge([b, g, r])

    fw, fh = 800, 450
    img_pts_a = [(80, 80), (720, 80), (720, 380), (80, 380)]
    img_pts_b = [(120, 100), (700, 100), (700, 360), (120, 360)]
    reg_a = Region("near_half", "corridor", img_pts_a,
                   (0.0, 0.0, 0.55, 1.0), (0.0, 0.0, 0.50, 1.0),
                   H=build_homography(img_pts_a, (0.0, 0.0, 0.55, 1.0)))
    reg_b = Region("far_half", "corridor", img_pts_b,
                   (0.45, 0.0, 1.0, 1.0), (0.50, 0.0, 1.0, 1.0),
                   H=build_homography(img_pts_b, (0.45, 0.0, 1.0, 1.0)))

    proj = {"corridor": Projection(id="corridor", pixel_size=(9600, 1080))}
    viewer = Viewer(proj)

    track_a = TrackOverlay(1, (300.0, 200.0, 380.0, 360.0), 0.82,
                           [("near_half", 0.30, 0.55, True)])
    track_a2 = TrackOverlay(2, (540.0, 220.0, 600.0, 350.0), 0.61,
                            [("near_half", 0.52, 0.60, False)])
    track_b = TrackOverlay(7, (420.0, 180.0, 500.0, 340.0), 0.74,
                           [("far_half", 0.62, 0.45, True)])

    try:
        for i in range(30):
            cams = [
                CamFrame("cam0", _gradient(fw, fh, i * 6), [track_a, track_a2],
                         [reg_a], fps=24.5, osc_rate=12.3, reconnects=0),
                CamFrame("cam1", _gradient(fw, fh, 128 - i * 4), [track_b],
                         [reg_b], fps=23.8, osc_rate=8.1, reconnects=1),
            ]
            if not viewer.render(cams):
                break
            time.sleep(1 / 30)
    finally:
        viewer.close()
