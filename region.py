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


@dataclass
class Region:
    id: str
    projection_id: str
    image_points: list[tuple[float, float]]   # 4 (x, y) pixel coords in input frame
    projection_uv: tuple[float, float, float, float]   # (u0, v0, u1, v1)
    dispatch_uv: tuple[float, float, float, float]     # subset of projection_uv
    min_bbox_height_px: int = 0
    H: np.ndarray = field(default=None, repr=False)    # 3x3, built at load time


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

    sys.exit(1 if failed else 0)
