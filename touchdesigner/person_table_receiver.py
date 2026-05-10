"""TouchDesigner OSC callback for the tracker person table.

Paste this into the callbacks DAT for the OSC In DAT that receives tracker OSC.
It prefers the normalized `/proj/corridor/uv` stream added by tracker.py and
falls back to `/proj/corridor/xy` only when UV has not arrived recently.
"""

from __future__ import annotations

from typing import Any, List


# Keep these aligned with the TouchDesigner world layout, not the OSC input.
# `/uv` input is normalized already; ASPECT only scales the output tx axis.
SCREEN_W = 7680.0
SCREEN_H = 1080.0
ASPECT = SCREEN_W / SCREEN_H

UV_ADDRESS = "/proj/corridor/uv"
XY_ADDRESS = "/proj/corridor/xy"
UV_FALLBACK_AFTER_S = 0.5

VEL_FLOOR = 0.06
TAIL_VEL = 0.15
MAX_TAIL = 4
EMIT_OFFSET = 0.012

# Active people are written first. Tails are added only with remaining capacity,
# so wake density cannot hide real gids.
MAX_ACTIVE = 64
MAX_SOURCES = 128
KEEP_STATIONARY_ACTIVE = True


# module-scope state
_prev_pos = {}       # gid -> (tx, tz, t)
_last_uv_time = -1.0


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _iter_uv_points(address: str, args: List[Any]):
    for i in range(0, len(args), 3):
        if i + 2 >= len(args):
            break
        gid = _safe_int(args[i])
        a = _safe_float(args[i + 1])
        b = _safe_float(args[i + 2])

        if address == UV_ADDRESS:
            u = a
            v = b
        else:
            u = a / SCREEN_W
            v = b / SCREEN_H

        yield gid, u, v


def _uv_to_td(u: float, v: float):
    tx = (u - 0.5) * ASPECT
    tz = 0.5 - v
    return tx, tz


def _append_person_row(rows, gid: int, tx: float, tz: float):
    rows.append([gid, tx, 0.0, tz, 1])


def onReceiveOSC(dat: oscinDAT, rowIndex: int, message: str,
                 byteData: bytes, timeStamp: float, address: str,
                 args: List[Any], peer: Peer):
    global _last_uv_time

    now = absTime.seconds
    if address == UV_ADDRESS:
        _last_uv_time = now
    elif address == XY_ADDRESS:
        if _last_uv_time >= 0.0 and now - _last_uv_time < UV_FALLBACK_AFTER_S:
            return
    else:
        return

    active_rows = []
    tail_rows = []
    seen = set()

    for gid, u, v in _iter_uv_points(address, args):
        if gid in seen:
            continue
        seen.add(gid)

        tx, tz = _uv_to_td(u, v)
        prev = _prev_pos.get(gid)
        _prev_pos[gid] = (tx, tz, now)

        if prev is None:
            if KEEP_STATIONARY_ACTIVE:
                _append_person_row(active_rows, gid, tx, tz)
            continue

        dt = max(now - prev[2], 1e-3)
        dx = tx - prev[0]
        dz = tz - prev[1]
        dist = (dx * dx + dz * dz) ** 0.5
        vel = dist / dt

        if dist > 1e-6:
            ux = dx / dist
            uz = dz / dist
        else:
            ux = 0.0
            uz = 0.0

        moving = vel >= VEL_FLOOR
        if moving:
            head_tx = tx - ux * EMIT_OFFSET
            head_tz = tz - uz * EMIT_OFFSET
            _append_person_row(active_rows, gid, head_tx, head_tz)

            if vel > TAIL_VEL:
                n_tail = min(int(vel / TAIL_VEL), MAX_TAIL)
                for k in range(1, n_tail + 1):
                    a = k / (n_tail + 1)
                    t_tx = tx - dx * a - ux * EMIT_OFFSET
                    t_tz = tz - dz * a - uz * EMIT_OFFSET
                    _append_person_row(tail_rows, gid * 100 + k, t_tx, t_tz)
        elif KEEP_STATIONARY_ACTIVE:
            _append_person_row(active_rows, gid, tx, tz)

    for stale_gid in [gid for gid in _prev_pos if gid not in seen]:
        _prev_pos.pop(stale_gid, None)

    table = op("person_table")
    table.clear()
    table.appendRow(["gid", "tx", "ty", "tz", "active"])

    written = 0
    for row in active_rows[:MAX_ACTIVE]:
        if written >= MAX_SOURCES:
            break
        table.appendRow(row)
        written += 1

    for row in tail_rows:
        if written >= MAX_SOURCES:
            break
        table.appendRow(row)
        written += 1

    return
