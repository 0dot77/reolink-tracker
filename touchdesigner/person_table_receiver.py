"""TouchDesigner OSC callback for the tracker person table.

Paste this into the callbacks DAT for the OSC In DAT that receives tracker OSC.
It prefers the normalized `/proj/corridor/uv` stream added by tracker.py and
falls back to `/proj/corridor/xy` only when UV has not arrived recently.
The optional `/proj/corridor/person_zones` stream is merged by gid so seated
stair actors can use a separate TD lane without changing the primary UV packet.
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
PERSON_ZONES_ADDRESS = "/proj/corridor/person_zones"
UV_FALLBACK_AFTER_S = 0.5

VEL_FLOOR = 0.06
TAIL_VEL = 0.15
MAX_TAIL = 4
EMIT_OFFSET = 0.012

ZONE_FLOOR = 0
ZONE_BODY_CATCH = 1
ZONE_STAIR_RELAXED = 2
ZONE_NAME_TO_CODE = {
    "floor": ZONE_FLOOR,
    "body_catch": ZONE_BODY_CATCH,
    "stair_relaxed": ZONE_STAIR_RELAXED,
}
ZONE_CODE_TO_NAME = {code: name for name, code in ZONE_NAME_TO_CODE.items()}

# Current TD patch instances on the XZ plane, so stair actors are separated
# along Z while Y stays flat.
FLOOR_TY = 0.0
BODY_CATCH_TY = 0.0
STAIR_TY_OFFSET = 0.0
FLOOR_TZ_OFFSET = 0.0
BODY_CATCH_TZ_OFFSET = 0.0
STAIR_TZ_OFFSET = 0.18

# Active people are written first. Tails are added only with remaining capacity,
# so wake density cannot hide real gids.
MAX_ACTIVE = 64
MAX_SOURCES = 128
KEEP_STATIONARY_ACTIVE = True


# module-scope state
_prev_pos = {}       # gid -> (tx, tz, t)
_source_zone_by_gid = {}  # gid -> numeric zone code
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


def _zone_code(value: Any) -> int:
    if isinstance(value, str):
        return ZONE_NAME_TO_CODE.get(value, ZONE_FLOOR)
    code = _safe_int(value, ZONE_FLOOR)
    if code in ZONE_CODE_TO_NAME:
        return code
    return ZONE_FLOOR


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


def _lane_offsets(zone_code: int):
    if zone_code == ZONE_STAIR_RELAXED:
        return STAIR_TY_OFFSET, STAIR_TZ_OFFSET
    if zone_code == ZONE_BODY_CATCH:
        return BODY_CATCH_TY, BODY_CATCH_TZ_OFFSET
    return FLOOR_TY, FLOOR_TZ_OFFSET


def _append_person_row(rows, gid: int, tx: float, tz: float, zone_code: int):
    ty, tz_offset = _lane_offsets(zone_code)
    rows.append([gid, tx, ty, tz + tz_offset, 1, zone_code])


def _iter_person_zones(args: List[Any]):
    for i in range(0, len(args), 2):
        if i + 1 >= len(args):
            break
        gid = _safe_int(args[i])
        yield gid, _zone_code(args[i + 1])


def _parse_person_source_zone_address(address: str):
    parts = address.strip("/").split("/")
    if len(parts) != 5:
        return None
    if parts[0] != "proj" or parts[1] != "corridor":
        return None
    if parts[2] != "person" or parts[4] != "source_zone":
        return None
    return _safe_int(parts[3], -1)


def _parse_person_lost_address(address: str):
    parts = address.strip("/").split("/")
    if len(parts) != 5:
        return None
    if parts[0] != "proj" or parts[1] != "corridor":
        return None
    if parts[2] != "person" or parts[4] != "lost":
        return None
    return _safe_int(parts[3], -1)


def _update_person_zones(args: List[Any]):
    seen = set()
    for gid, zone_code in _iter_person_zones(args):
        if gid <= 0:
            continue
        seen.add(gid)
        _source_zone_by_gid[gid] = zone_code
    for stale_gid in [gid for gid in _source_zone_by_gid if gid not in seen]:
        _source_zone_by_gid.pop(stale_gid, None)


def _write_zone_table(active_gids):
    try:
        table = op("person_zone_table")
    except Exception:
        return
    if table is None:
        return
    table.clear()
    table.appendRow(["gid", "source_zone", "zone_code"])
    for gid in sorted(active_gids):
        zone_code = _source_zone_by_gid.get(gid, ZONE_FLOOR)
        table.appendRow([gid, ZONE_CODE_TO_NAME.get(zone_code, "floor"), zone_code])


def onReceiveOSC(dat: oscinDAT, rowIndex: int, message: str,
                 byteData: bytes, timeStamp: float, address: str,
                 args: List[Any], peer: Peer):
    global _last_uv_time

    now = absTime.seconds
    if address == PERSON_ZONES_ADDRESS:
        _update_person_zones(args)
        return

    source_zone_gid = _parse_person_source_zone_address(address)
    if source_zone_gid is not None:
        if source_zone_gid > 0 and args:
            _source_zone_by_gid[source_zone_gid] = _zone_code(args[0])
        return

    lost_gid = _parse_person_lost_address(address)
    if lost_gid is not None:
        _prev_pos.pop(lost_gid, None)
        _source_zone_by_gid.pop(lost_gid, None)
        return

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

        zone_code = _source_zone_by_gid.get(gid, ZONE_FLOOR)
        tx, tz = _uv_to_td(u, v)
        prev = _prev_pos.get(gid)
        _prev_pos[gid] = (tx, tz, now)

        if prev is None:
            if KEEP_STATIONARY_ACTIVE:
                _append_person_row(active_rows, gid, tx, tz, zone_code)
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
            _append_person_row(active_rows, gid, head_tx, head_tz, zone_code)

            if vel > TAIL_VEL:
                n_tail = min(int(vel / TAIL_VEL), MAX_TAIL)
                for k in range(1, n_tail + 1):
                    a = k / (n_tail + 1)
                    t_tx = tx - dx * a - ux * EMIT_OFFSET
                    t_tz = tz - dz * a - uz * EMIT_OFFSET
                    _append_person_row(
                        tail_rows,
                        gid * 100 + k,
                        t_tx,
                        t_tz,
                        zone_code,
                    )
        elif KEEP_STATIONARY_ACTIVE:
            _append_person_row(active_rows, gid, tx, tz, zone_code)

    for stale_gid in [gid for gid in _prev_pos if gid not in seen]:
        _prev_pos.pop(stale_gid, None)
        _source_zone_by_gid.pop(stale_gid, None)

    table = op("person_table")
    table.clear()
    table.appendRow(["gid", "tx", "ty", "tz", "active", "zone_code"])
    _write_zone_table(seen)

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
