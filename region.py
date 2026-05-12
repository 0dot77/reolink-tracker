"""Ground-plane image pixels → shared projection UV homography.

Used by the Reolink corridor tracker so every camera reports person positions
in one shared projection coordinate system. Two cameras facing each other
across a 40 m corridor each click 4 points on their own video frame; those 4
points correspond — in clockwise order starting at top-left — to the 4 corners
of a UV slice of the shared projection. This module just builds and applies
the 3x3 perspective transform; CamWorker keeps the runtime state.

Pure-Python: only stdlib + numpy + cv2. No project imports.
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence
import sys

import numpy as np
import cv2


@dataclass
class Projection:
    id: str
    pixel_size: Optional[tuple[int, int]] = None       # (w_px, h_px). None → no /px channel.
    world_size_m: Optional[tuple[float, float]] = None  # metadata only.
    output_warp_points: Optional[list[tuple[float, float]]] = None
    interaction_zones: list["InteractionZone"] = field(default_factory=list)


@dataclass
class InteractionZone:
    """Rectangular interaction surface in a projection's UV coordinate space."""

    projection_id: str
    id: str
    uv_rect: tuple[float, float, float, float]
    release_after_s: float = 0.6


@dataclass
class Region:
    id: str
    projection_id: str
    image_points: list[tuple[float, float]]   # 4 (x, y) pixel coords in input frame
    projection_uv: tuple[float, float, float, float]   # (u0, v0, u1, v1)
    dispatch_uv: tuple[float, float, float, float]     # subset of projection_uv
    min_bbox_height_px: int = 0
    body_catch_points: list[tuple[float, float]] = field(default_factory=list)
    body_catch_margin_uv: float = 0.0
    body_catch_min_confidence: float = 0.0
    relaxed_presence_enabled: bool = True
    relaxed_presence_points: list[tuple[float, float]] = field(default_factory=list)
    relaxed_presence_uv: Optional[tuple[float, float, float, float]] = None
    relaxed_presence_margin_uv: float = 0.0
    relaxed_presence_min_confidence: float = 0.0
    relaxed_presence_v: Optional[float] = None
    H: np.ndarray = field(default=None, repr=False)    # 3x3, built at load time
    relaxed_presence_H: Optional[np.ndarray] = field(default=None, repr=False)


def _is_convex_simple(pts: Sequence[tuple[float, float]]) -> bool:
    """Return True if the 4-point polygon is simple (non-self-intersecting)
    and convex. Uses cross-product sign consistency around the loop."""
    n = len(pts)
    sign = 0
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        cx, cy = pts[(i + 2) % n]
        # cross product of (b-a) x (c-b)
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if cross == 0:
            # 3 consecutive points collinear — degenerate
            return False
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


def build_homography(
    image_points: Sequence[tuple[float, float]],
    projection_uv: tuple[float, float, float, float],
) -> np.ndarray:
    """Build a 3x3 perspective transform mapping 4 image pixels → 4 UV corners.

    `image_points` is 4 (x, y) tuples in clockwise order:
        top-left, top-right, bottom-right, bottom-left.
    `projection_uv` is (u0, v0, u1, v1) with u0 < u1 and v0 < v1; it is mapped
    to (u0,v0), (u1,v0), (u1,v1), (u0,v1) respectively.

    Raises ValueError on degenerate input (duplicate points, 3-point
    collinearity, or self-intersecting polygon).
    """
    if len(image_points) != 4:
        raise ValueError(
            f"image_points must have exactly 4 points, got {len(image_points)}"
        )

    u0, v0, u1, v1 = projection_uv
    if not (u0 < u1):
        raise ValueError(
            f"projection_uv requires u0 < u1, got u0={u0}, u1={u1}"
        )
    if not (v0 < v1):
        raise ValueError(
            f"projection_uv requires v0 < v1, got v0={v0}, v1={v1}"
        )

    pts = [(float(x), float(y)) for (x, y) in image_points]

    # Distinct points
    seen = set()
    for p in pts:
        if p in seen:
            raise ValueError(f"image_points contains duplicate point {p}")
        seen.add(p)

    # No 3 collinear AND not self-intersecting — a simple convex check
    # (consistent cross-product sign around the loop) covers both.
    if not _is_convex_simple(pts):
        raise ValueError(
            "image_points must form a simple convex quadrilateral "
            "(found 3-point collinearity or self-intersection): "
            f"{pts}"
        )

    src = np.asarray(pts, dtype=np.float32)
    dst = np.asarray(
        [(u0, v0), (u1, v0), (u1, v1), (u0, v1)],
        dtype=np.float32,
    )
    return cv2.getPerspectiveTransform(src, dst)


def project(pt_px: tuple[float, float], H: np.ndarray) -> tuple[float, float]:
    """Apply a 3x3 homography to a 2D pixel point. Returns (u, v) floats."""
    arr = np.asarray([[[float(pt_px[0]), float(pt_px[1])]]], dtype=np.float32)
    out = cv2.perspectiveTransform(arr, H)
    u, v = out[0, 0]
    return (float(u), float(v))


def is_inside_uv(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    """Inclusive containment test: u0 ≤ u ≤ u1 and v0 ≤ v ≤ v1."""
    u, v = uv
    u0, v0, u1, v1 = rect
    return (u0 <= u <= u1) and (v0 <= v <= v1)


def validate_uv_quad(
    points: Sequence[Sequence[float]],
    label: str = "uv_quad",
) -> list[tuple[float, float]]:
    """Validate 4 UV points ordered top-left, top-right, bottom-right, bottom-left."""
    if len(points) != 4:
        raise ValueError(f"{label} must contain exactly 4 points, got {len(points)}")
    out: list[tuple[float, float]] = []
    for idx, point in enumerate(points):
        if len(point) != 2:
            raise ValueError(f"{label}[{idx}] must contain [u, v]")
        try:
            u = float(point[0])
            v = float(point[1])
        except (TypeError, ValueError) as ex:
            raise ValueError(f"{label}[{idx}] must contain numeric values") from ex
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            raise ValueError(f"{label}[{idx}] must stay inside 0..1, got {(u, v)}")
        out.append((u, v))
    if not _is_convex_simple(out):
        raise ValueError(f"{label} must form a simple convex quadrilateral: {out}")
    return out


def warp_uv(
    uv: tuple[float, float],
    output_warp_points: Optional[Sequence[tuple[float, float]]],
) -> tuple[float, float]:
    """Bilinearly map a final projection UV point through an output warp quad.

    `output_warp_points` uses top-left, top-right, bottom-right, bottom-left
    order. `None` or an empty sequence leaves the point unchanged.
    """
    if not output_warp_points:
        return (float(uv[0]), float(uv[1]))
    if len(output_warp_points) != 4:
        raise ValueError(
            f"output_warp_points must contain exactly 4 points, got {len(output_warp_points)}"
        )
    u = min(max(float(uv[0]), 0.0), 1.0)
    v = min(max(float(uv[1]), 0.0), 1.0)
    tl, tr, br, bl = output_warp_points
    top_u = tl[0] * (1.0 - u) + tr[0] * u
    top_v = tl[1] * (1.0 - u) + tr[1] * u
    bottom_u = bl[0] * (1.0 - u) + br[0] * u
    bottom_v = bl[1] * (1.0 - u) + br[1] * u
    return (
        top_u * (1.0 - v) + bottom_u * v,
        top_v * (1.0 - v) + bottom_v * v,
    )


def warp_uv_velocity(
    uv: tuple[float, float],
    velocity_uv_s: tuple[float, float],
    output_warp_points: Optional[Sequence[tuple[float, float]]],
) -> tuple[float, float]:
    """Map a UV velocity through the local derivative of the output warp."""
    if not output_warp_points:
        return (float(velocity_uv_s[0]), float(velocity_uv_s[1]))
    if len(output_warp_points) != 4:
        raise ValueError(
            f"output_warp_points must contain exactly 4 points, got {len(output_warp_points)}"
        )
    u = min(max(float(uv[0]), 0.0), 1.0)
    v = min(max(float(uv[1]), 0.0), 1.0)
    vx, vy = (float(velocity_uv_s[0]), float(velocity_uv_s[1]))
    tl, tr, br, bl = output_warp_points
    du_u = (tr[0] - tl[0]) * (1.0 - v) + (br[0] - bl[0]) * v
    du_v = (bl[0] - tl[0]) * (1.0 - u) + (br[0] - tr[0]) * u
    dv_u = (tr[1] - tl[1]) * (1.0 - v) + (br[1] - bl[1]) * v
    dv_v = (bl[1] - tl[1]) * (1.0 - u) + (br[1] - tr[1]) * u
    return (du_u * vx + du_v * vy, dv_u * vx + dv_v * vy)


def _point_in_rect(
    point: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    x, y = point
    x0, y0, x1, y1 = rect
    lo_x, hi_x = sorted((x0, x1))
    lo_y, hi_y = sorted((y0, y1))
    return lo_x <= x <= hi_x and lo_y <= y <= hi_y


def _segments_intersect(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
) -> bool:
    def orient(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
    ) -> bool:
        return (
            min(p[0], r[0]) <= q[0] <= max(p[0], r[0])
            and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])
        )

    o1 = orient(a0, a1, b0)
    o2 = orient(a0, a1, b1)
    o3 = orient(b0, b1, a0)
    o4 = orient(b0, b1, a1)
    eps = 1e-9
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if abs(o1) <= eps and on_segment(a0, b0, a1):
        return True
    if abs(o2) <= eps and on_segment(a0, b1, a1):
        return True
    if abs(o3) <= eps and on_segment(b0, a0, b1):
        return True
    if abs(o4) <= eps and on_segment(b0, a1, b1):
        return True
    return False


def bbox_intersects_polygon(
    bbox_xyxy: tuple[float, float, float, float],
    polygon: Sequence[tuple[float, float]],
) -> bool:
    """Return True when an axis-aligned bbox touches or crosses a polygon.

    Used for body-catch regions: bbox corner-in-polygon and polygon
    vertex-in-bbox checks miss thin overlap cases where only edges cross, so
    this also tests every bbox edge against every polygon edge.
    """
    if len(polygon) < 3:
        return False
    x0, y0, x1, y1 = bbox_xyxy
    lo_x, hi_x = sorted((float(x0), float(x1)))
    lo_y, hi_y = sorted((float(y0), float(y1)))
    rect = (lo_x, lo_y, hi_x, hi_y)
    bbox_pts = [
        (lo_x, lo_y),
        (hi_x, lo_y),
        (hi_x, hi_y),
        (lo_x, hi_y),
    ]
    poly = np.asarray([(float(x), float(y)) for x, y in polygon], dtype=np.float32)
    if any(cv2.pointPolygonTest(poly, point, False) >= 0 for point in bbox_pts):
        return True
    poly_pts = [(float(x), float(y)) for x, y in polygon]
    if any(_point_in_rect(point, rect) for point in poly_pts):
        return True
    bbox_edges = list(zip(bbox_pts, bbox_pts[1:] + bbox_pts[:1]))
    poly_edges = list(zip(poly_pts, poly_pts[1:] + poly_pts[:1]))
    return any(
        _segments_intersect(a0, a1, b0, b1)
        for a0, a1 in bbox_edges
        for b0, b1 in poly_edges
    )


def dispatches_overlap(
    rect_a: tuple[float, float, float, float],
    rect_b: tuple[float, float, float, float],
) -> bool:
    """Return True if two UV rectangles share an open interior.

    Each rect is `(u0, v0, u1, v1)` with u0<u1 and v0<v1. Touching edges
    (e.g. one ends at u=0.5 and the other starts at u=0.5) are *not*
    considered overlapping; only positive-area intersection counts. Used by
    the viewer to warn operators when two cameras claim the same projection
    slice for OSC dispatch.
    """
    a_u0, a_v0, a_u1, a_v1 = rect_a
    b_u0, b_v0, b_u1, b_v1 = rect_b
    return max(a_u0, b_u0) < min(a_u1, b_u1) and max(a_v0, b_v0) < min(a_v1, b_v1)


def validate_dispatch(
    projection_uv: tuple[float, float, float, float],
    dispatch_uv: tuple[float, float, float, float],
) -> None:
    """Raise ValueError if dispatch_uv is not a subset of projection_uv."""
    pu0, pv0, pu1, pv1 = projection_uv
    du0, dv0, du1, dv1 = dispatch_uv
    if not (du0 < du1 and dv0 < dv1):
        raise ValueError(
            f"dispatch_uv must have u0<u1 and v0<v1, got {dispatch_uv}"
        )
    if not (du0 >= pu0 and du1 <= pu1 and dv0 >= pv0 and dv1 <= pv1):
        raise ValueError(
            f"dispatch_uv {dispatch_uv} is not a subset of "
            f"projection_uv {projection_uv}"
        )


if __name__ == "__main__":
    failed = 0

    def _check(name: str, ok: bool, detail: str = "") -> None:
        global failed
        if ok:
            print(f"PASS  {name}")
        else:
            print(f"FAIL  {name}  {detail}")
            failed += 1

    # (a) Round-trip: full projection.
    try:
        img_pts = [(100, 100), (700, 100), (700, 500), (100, 500)]
        H = build_homography(img_pts, (0.0, 0.0, 1.0, 1.0))
        expected = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        max_err = 0.0
        for p, e in zip(img_pts, expected):
            u, v = project(p, H)
            max_err = max(max_err, abs(u - e[0]), abs(v - e[1]))
        _check(
            "(a) round-trip full projection",
            max_err < 1e-5,
            f"max_err={max_err}",
        )
    except Exception as ex:
        _check("(a) round-trip full projection", False, f"raised {ex!r}")

    # (b) Round-trip: UV slice.
    try:
        img_pts = [(100, 100), (700, 100), (700, 500), (100, 500)]
        H = build_homography(img_pts, (0.0, 0.0, 0.55, 1.0))
        expected = [(0.0, 0.0), (0.55, 0.0), (0.55, 1.0), (0.0, 1.0)]
        max_err = 0.0
        for p, e in zip(img_pts, expected):
            u, v = project(p, H)
            max_err = max(max_err, abs(u - e[0]), abs(v - e[1]))
        _check(
            "(b) round-trip UV slice",
            max_err < 1e-5,
            f"max_err={max_err}",
        )
    except Exception as ex:
        _check("(b) round-trip UV slice", False, f"raised {ex!r}")

    # (c) Collinear input must be rejected.
    try:
        build_homography(
            [(0, 0), (100, 0), (200, 0), (300, 0)],
            (0.0, 0.0, 1.0, 1.0),
        )
        _check("(c) collinear rejected", False, "no exception raised")
    except ValueError:
        _check("(c) collinear rejected", True)
    except Exception as ex:
        _check("(c) collinear rejected", False, f"wrong exception {ex!r}")

    # (d) Body-catch geometry catches edge-only overlaps. A thin polygon strip
    # can cross a bbox without either shape containing the other's vertices.
    try:
        bbox = (10.0, 10.0, 20.0, 20.0)
        strip = [(5.0, 14.0), (25.0, 14.0), (25.0, 16.0), (5.0, 16.0)]
        far = [(30.0, 30.0), (40.0, 30.0), (40.0, 40.0), (30.0, 40.0)]
        _check(
            "(d) bbox/polygon intersection handles edge crossing",
            bbox_intersects_polygon(bbox, strip)
            and not bbox_intersects_polygon(bbox, far),
        )
    except Exception as ex:
        _check("(d) bbox/polygon intersection handles edge crossing", False, f"raised {ex!r}")

    # (e) Output warp identity leaves final projection UV unchanged.
    try:
        quad = validate_uv_quad(
            [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
            "identity_output_warp",
        )
        warped = warp_uv((0.25, 0.75), quad)
        _check(
            "(e) identity output warp keeps UV",
            abs(warped[0] - 0.25) < 1e-6 and abs(warped[1] - 0.75) < 1e-6,
            f"warped={warped}",
        )
    except Exception as ex:
        _check("(e) identity output warp keeps UV", False, f"raised {ex!r}")

    # (f) Non-identity output warp moves a point before downstream zone tests.
    try:
        quad = validate_uv_quad(
            [(0.20, 0.0), (1.0, 0.0), (1.0, 1.0), (0.20, 1.0)],
            "shifted_output_warp",
        )
        raw = (0.10, 0.50)
        warped = warp_uv(raw, quad)
        warped_velocity = warp_uv_velocity(raw, (1.0, 0.0), quad)
        zone = (0.25, 0.40, 0.35, 0.60)
        _check(
            "(f) output warp can move a point into an interaction zone",
            not is_inside_uv(raw, zone)
            and is_inside_uv(warped, zone)
            and abs(warped_velocity[0] - 0.8) < 1e-6
            and abs(warped_velocity[1]) < 1e-6,
            f"raw={raw}, warped={warped}, velocity={warped_velocity}, zone={zone}",
        )
    except Exception as ex:
        _check("(f) output warp can move a point into an interaction zone", False, f"raised {ex!r}")

    sys.exit(1 if failed else 0)
