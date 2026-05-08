"""Operator viewer for the reolink-tracker.

Single cv2 window. Composes per-camera tiles with bbox/ID/region overlays
and an optional top-down projection UV canvas. In show mode, focused-camera
regions can be sketched with 4 mouse clicks and written back to config.yaml.
"""

import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from ipaddress import ip_address, ip_interface
from typing import Callable, Sequence, Optional

import numpy as np
import cv2

from region import (
    InteractionZone,
    Projection,
    Region,
    build_homography,
    dispatches_overlap,
    validate_dispatch,
)

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
_C_REGION_FOCUS = (0, 255, 255)
_C_FOCUS = (0, 255, 255)
_C_DRAFT = (0, 220, 255)
_C_EDIT_PROJ = (255, 255, 255)
_C_EDIT_DISPATCH = (0, 255, 255)
_C_ZONE = (255, 80, 220)
_C_ZONE_EDIT = (255, 255, 255)
_C_WARN = (60, 60, 240)
_C_PANEL_BG = (28, 28, 28)
_C_PANEL_TEXT = (220, 220, 220)
_C_PANEL_DIM = (140, 140, 140)
_C_DIRTY = (60, 180, 255)
_C_CLEAN = (140, 200, 140)
_FONT = cv2.FONT_HERSHEY_SIMPLEX

_EDIT_EDGES = ("u0", "v0", "u1", "v1")
_NUDGE_FINE = 0.01
_NUDGE_COARSE = 0.05
_UV_MIN_SPAN = 0.01


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
    frame_age_s: float = 0.0


@dataclass
class FusedPersonFrame:
    """One fused global person for dashboard rendering."""
    gid: int
    projection_id: str
    u: float
    v: float
    vx: float
    vy: float
    conf: float
    state: str
    source: tuple[str, int]


@dataclass(frozen=True)
class LanInterfaceFrame:
    """One macOS network interface snapshot for the operator panel."""
    device: str
    service: str
    mac: str = ""
    ipv4: tuple[str, ...] = ()
    status: str = "inactive"
    media: str = ""
    gateway: str = ""
    is_default: bool = False


@dataclass(frozen=True)
class NetworkTargetFrame:
    """Configured network endpoint the operator cares about."""
    name: str
    host: str
    port: int = 0
    kind: str = "target"


@dataclass(frozen=True)
class TargetRouteFrame:
    """Current route/ARP view for one configured network target."""
    target: NetworkTargetFrame
    iface: str = ""
    gateway: str = ""
    arp_seen: bool = False
    same_subnet: bool = False


def _grid_shape(n: int) -> tuple[int, int]:
    return (1, 1) if n <= 1 else (1, 2) if n == 2 else (2, 2)


def _fmt_uv(uv: tuple[float, float, float, float]) -> str:
    return f"[{uv[0]:.2f},{uv[1]:.2f},{uv[2]:.2f},{uv[3]:.2f}]"


def _projection_panel_size(
    proj: Projection,
    target_width: Optional[int],
) -> tuple[int, int]:
    aspect = (
        proj.pixel_size[0] / float(proj.pixel_size[1])
        if proj.pixel_size and proj.pixel_size[1] else 1.0
    )
    cw = target_width if target_width else 800
    ch = max(80, min(int(round(cw / aspect)) if aspect > 0 else cw, 1600))
    return cw, ch


def _gid_color(gid: int) -> tuple[int, int, int]:
    palette = [
        (95, 220, 255), (120, 180, 255), (120, 255, 170),
        (255, 190, 120), (220, 160, 255), (255, 130, 170),
        (180, 255, 255), (180, 220, 120),
    ]
    return palette[gid % len(palette)]


def _lan_color(index: int, active: bool = True) -> tuple[int, int, int]:
    palette = [
        (95, 220, 255), (120, 255, 170), (255, 190, 120),
        (220, 160, 255), (255, 130, 170), (180, 255, 255),
        (120, 180, 255), (180, 220, 120),
    ]
    color = palette[index % len(palette)]
    if active:
        return color
    return tuple(max(40, int(c * 0.45)) for c in color)


def _run_text_command(args: Sequence[str], timeout: float = 1.0) -> str:
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _parse_hardware_ports(text: str) -> dict[str, tuple[str, str]]:
    ports: dict[str, tuple[str, str]] = {}
    current_service = ""
    current_device = ""
    current_mac = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Hardware Port:"):
            if current_device:
                ports[current_device] = (current_service, current_mac)
            current_service = line.split(":", 1)[1].strip()
            current_device = ""
            current_mac = ""
        elif line.startswith("Device:"):
            current_device = line.split(":", 1)[1].strip()
        elif line.startswith("Ethernet Address:"):
            current_mac = line.split(":", 1)[1].strip()
    if current_device:
        ports[current_device] = (current_service, current_mac)
    return ports


def _prefix_from_netmask(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    try:
        if value.startswith("0x"):
            return str(int(value, 16).bit_count())
        octets = [int(part) for part in value.split(".")]
        if len(octets) == 4:
            return str(sum(o.bit_count() for o in octets))
    except ValueError:
        return ""
    return ""


def _default_route() -> tuple[str, str]:
    text = _run_text_command(("route", "-n", "get", "default"), timeout=1.0)
    return _parse_route_get(text)


def _parse_route_get(text: str) -> tuple[str, str]:
    iface = ""
    gateway = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("interface:"):
            iface = line.split(":", 1)[1].strip()
        elif line.startswith("gateway:"):
            gateway = line.split(":", 1)[1].strip()
    return iface, gateway


def _arp_hosts() -> set[str]:
    text = _run_text_command(("arp", "-an"), timeout=1.0)
    hosts: set[str] = set()
    for match in re.finditer(r"\(([^)]+)\)", text):
        hosts.add(match.group(1))
    return hosts


def _target_same_subnet(target_host: str, iface: LanInterfaceFrame) -> bool:
    try:
        target_ip = ip_address(target_host)
    except ValueError:
        return False
    for raw in iface.ipv4:
        try:
            if target_ip in ip_interface(raw).network:
                return True
        except ValueError:
            continue
    return False


def _collect_lan_interfaces() -> list[LanInterfaceFrame]:
    """Read the current Mac network interfaces without adding dependencies."""
    port_map = _parse_hardware_ports(
        _run_text_command(("networksetup", "-listallhardwareports"), timeout=1.5)
    )
    default_iface, default_gateway = _default_route()
    ifconfig = _run_text_command(("ifconfig",), timeout=1.5)
    interfaces: list[LanInterfaceFrame] = []

    block_re = re.compile(
        r"(?ms)^([A-Za-z0-9_.-]+): flags=.*?(?=^[A-Za-z0-9_.-]+: flags=|\Z)"
    )
    for match in block_re.finditer(ifconfig):
        device = match.group(1)
        block = match.group(0)
        if device.startswith(("lo", "awdl", "llw", "utun", "gif", "stf")):
            continue

        service, mac = port_map.get(device, (device, ""))
        inet_entries: list[str] = []
        for line in block.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == "inet":
                prefix = ""
                if "netmask" in parts:
                    mask_idx = parts.index("netmask") + 1
                    if mask_idx < len(parts):
                        prefix = _prefix_from_netmask(parts[mask_idx])
                inet_entries.append(f"{parts[1]}/{prefix}" if prefix else parts[1])

        status_match = re.search(r"\bstatus:\s*(\S+)", block)
        status = status_match.group(1) if status_match else (
            "active" if inet_entries else "inactive"
        )
        media_match = re.search(r"^\s*media:\s*(.+)$", block, flags=re.MULTILINE)
        media = media_match.group(1).strip() if media_match else ""

        # Show physical Mac services and any connected interface with an IPv4
        # address. This keeps virtual tunnel noise out while still showing
        # USB/Thunderbolt Ethernet dongles as they appear on site.
        if device not in port_map and not inet_entries:
            continue

        interfaces.append(
            LanInterfaceFrame(
                device=device,
                service=service,
                mac=mac,
                ipv4=tuple(inet_entries),
                status=status,
                media=media,
                gateway=default_gateway if device == default_iface else "",
                is_default=device == default_iface,
            )
        )

    def sort_key(item: LanInterfaceFrame) -> tuple[int, int, str]:
        active = item.status == "active" or bool(item.ipv4)
        wired_hint = any(
            token in item.service.lower()
            for token in ("ethernet", "lan", "usb", "thunderbolt")
        )
        return (0 if active else 1, 0 if wired_hint else 1, item.device)

    return sorted(interfaces, key=sort_key)


def _collect_target_routes(
    targets: Sequence[NetworkTargetFrame],
    interfaces: Sequence[LanInterfaceFrame],
) -> list[TargetRouteFrame]:
    arp_seen = _arp_hosts()
    iface_lookup = {item.device: item for item in interfaces}
    routes: list[TargetRouteFrame] = []
    for target in targets:
        iface = ""
        gateway = ""
        try:
            ip_address(target.host)
        except ValueError:
            pass
        else:
            iface, gateway = _parse_route_get(
                _run_text_command(("route", "-n", "get", target.host), timeout=1.0)
            )
        iface_frame = iface_lookup.get(iface)
        routes.append(
            TargetRouteFrame(
                target=target,
                iface=iface,
                gateway=gateway,
                arp_seen=target.host in arp_seen,
                same_subnet=(
                    _target_same_subnet(target.host, iface_frame)
                    if iface_frame is not None else False
                ),
            )
        )
    return routes


def _put_label(
    img: np.ndarray,
    text: str,
    org: tuple[int, int],
    scale: float = 0.48,
    color: tuple[int, int, int] = _C_PANEL_TEXT,
    thickness: int = 1,
) -> None:
    cv2.putText(img, text, org, _FONT, scale, color, thickness, cv2.LINE_AA)


def _draw_topology_box(
    img: np.ndarray,
    rect: tuple[int, int, int, int],
    title: str,
    lines: Sequence[str] = (),
    color: tuple[int, int, int] = _C_PANEL_TEXT,
    fill: tuple[int, int, int] = (34, 34, 34),
) -> None:
    x, y, w, h = rect
    cv2.rectangle(img, (x, y), (x + w, y + h), fill, -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), color, 1)
    _put_label(img, title, (x + 12, y + 26), 0.58, color, 1)
    line_y = y + 52
    for line in lines[:3]:
        _put_label(img, line[:34], (x + 12, line_y), 0.42, _C_PANEL_DIM, 1)
        line_y += 20


def _route_status(route: TargetRouteFrame) -> tuple[str, tuple[int, int, int]]:
    if route.same_subnet and route.arp_seen:
        return "same LAN + arp", _C_CLEAN
    if route.same_subnet:
        return "same LAN, no arp", _C_DIRTY
    if route.gateway:
        return "via gateway", _C_WARN
    return "off-subnet", _C_WARN


def _draw_field_topology(
    canvas: np.ndarray,
    interfaces: Sequence[LanInterfaceFrame],
    target_routes: Sequence[TargetRouteFrame],
    x: int,
    y: int,
    width: int,
    height: int,
) -> None:
    """Draw the field wiring model used by the installation."""
    cv2.rectangle(canvas, (x, y), (x + width, y + height), (29, 29, 29), -1)
    cv2.rectangle(canvas, (x, y), (x + width, y + height), (58, 58, 58), 1)
    _put_label(canvas, "Field topology", (x + 18, y + 30), 0.68,
               _C_PANEL_TEXT, 2)
    _put_label(canvas, "16-port backbone + 4-port PoE camera island",
               (x + 190, y + 29), 0.48, _C_PANEL_DIM, 1)

    inner_x = x + 24
    inner_y = y + 54
    box_h = 78
    gap = 24
    router_w = max(150, min(210, width // 7))
    core_w = max(190, min(260, width // 5))
    poe_w = max(180, min(240, width // 5))
    cams_w = max(220, width - router_w - core_w - poe_w - gap * 5 - 48)

    router = (inner_x, inner_y, router_w, box_h)
    core = (router[0] + router_w + gap, inner_y, core_w, box_h)
    poe = (core[0] + core_w + gap, inner_y, poe_w, box_h)
    cams = (poe[0] + poe_w + gap, inner_y, cams_w, box_h)

    default_iface = next((item for item in interfaces if item.is_default), None)
    active_wired = next(
        (
            item for item in interfaces
            if (item.status == "active" or item.ipv4)
            and any(token in item.service.lower()
                    for token in ("ethernet", "lan", "usb", "thunderbolt"))
        ),
        None,
    )
    mac_iface = active_wired or default_iface
    mac_lines = []
    if mac_iface:
        mac_lines.append(f"{mac_iface.device} {mac_iface.service}"[:34])
        if mac_iface.ipv4:
            mac_lines.append(", ".join(mac_iface.ipv4)[:34])
    else:
        mac_lines.append("no active interface")

    camera_routes = [route for route in target_routes if route.target.kind == "rtsp"]
    osc_routes = [route for route in target_routes if route.target.kind == "osc"]
    camera_lines = []
    for route in camera_routes[:2]:
        status, _color = _route_status(route)
        camera_lines.append(f"{route.target.name} {route.target.host}  {status}")
    if not camera_lines:
        camera_lines.append("camera RTSP targets from config")

    _draw_topology_box(canvas, router, "Router / DHCP",
                       ("IP assignment", "gateway optional"), _C_PANEL_DIM)
    _draw_topology_box(canvas, core, "16-port switch",
                       ("main LAN backbone", "Mac + OSC receiver"), _C_CLEAN)
    _draw_topology_box(canvas, poe, "4-port PoE switch",
                       ("camera power", "uplink to backbone"), _C_DIRTY)
    _draw_topology_box(canvas, cams, "Reolink cameras",
                       camera_lines, _C_PANEL_TEXT)

    for left, right, label in (
        (router, core, "LAN"),
        (core, poe, "uplink"),
        (poe, cams, "PoE"),
    ):
        y_mid = left[1] + left[3] // 2
        x0 = left[0] + left[2]
        x1 = right[0]
        cv2.line(canvas, (x0, y_mid), (x1, y_mid), _C_PANEL_DIM, 2, cv2.LINE_AA)
        _put_label(canvas, label, (x0 + 8, y_mid - 8), 0.36, _C_PANEL_DIM, 1)

    mac = (core[0], inner_y + box_h + 32, core_w, 70)
    receiver_host = ""
    if osc_routes:
        receiver_host = osc_routes[0].target.host
    receiver_lines = (
        (f"OSC host {receiver_host}" if receiver_host else "OSC receiver"),
        "127.0.0.1 means same Mac",
    )
    receiver = (core[0] + core_w + gap, inner_y + box_h + 32,
                max(220, min(320, width - (core[0] + core_w + gap) - x - 24)), 70)
    _draw_topology_box(canvas, mac, "Mac / tracker.py", mac_lines, _C_CLEAN)
    _draw_topology_box(canvas, receiver, "OSC receiver",
                       receiver_lines, _C_PANEL_TEXT)
    cv2.line(canvas, (core[0] + core_w // 2, core[1] + core[3]),
             (mac[0] + mac[2] // 2, mac[1]), _C_CLEAN, 2, cv2.LINE_AA)
    cv2.line(canvas, (mac[0] + mac[2], mac[1] + mac[3] // 2),
             (receiver[0], receiver[1] + receiver[3] // 2),
             _C_PANEL_TEXT, 2, cv2.LINE_AA)
    _put_label(canvas, "OSC :7000", (mac[0] + mac[2] + 12, mac[1] + 28),
               0.38, _C_PANEL_DIM, 1)

    status_x = cams[0] + 12
    status_y = cams[1] + cams[3] + 34
    for route in camera_routes[:4]:
        status, color = _route_status(route)
        _put_label(canvas, f"{route.target.name}: {status}",
                   (status_x, status_y), 0.42, color, 1)
        status_y += 20


def _draw_hud(tile: np.ndarray, cam: CamFrame) -> None:
    text = (
        f"{cam.name}  fps={cam.fps:.1f}  osc={cam.osc_rate:.1f}/s  "
        f"age={cam.frame_age_s:.1f}s  rc={cam.reconnects}"
    )
    (tw, th), base = cv2.getTextSize(text, _FONT, 0.5, 1)
    pad, H, W = 4, tile.shape[0], tile.shape[1]
    x1, y1, x2, y2 = W - tw - 2 * pad - 4, 4, W - 4, 4 + th + base + 2 * pad
    overlay = tile.copy()
    cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.5, tile, 0.5, 0, dst=tile)
    cv2.putText(tile, text, (x1 + pad, y2 - pad - base // 2),
                _FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA)


def _render_tile(
    cam: CamFrame,
    tile_size: tuple[int, int],
    show_hud: bool,
    focused_region_idx: int = -1,
) -> np.ndarray:
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

    for ri, reg in enumerate(cam.regions):
        pts = np.array([(int(round(x * sx)), int(round(y * sy)))
                        for (x, y) in reg.image_points], dtype=np.int32)
        is_focus = ri == focused_region_idx
        color = _C_REGION_FOCUS if is_focus else _C_REGION_POLY
        thickness = 2 if is_focus else 1
        cv2.polylines(tile, [pts], True, color, thickness, cv2.LINE_AA)
        u0, v0, u1, v1 = reg.projection_uv
        label = f"{reg.id} [{u0:.2f},{v0:.2f}->{u1:.2f},{v1:.2f}]"
        if is_focus:
            label = "* " + label
        tlx, tly = int(pts[:, 0].min()), int(pts[:, 1].min())
        cv2.putText(tile, label, (tlx + 2, max(12, tly - 4)),
                    _FONT, 0.4, color, 1, cv2.LINE_AA)

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


def _compose_cam_row(tiles: list[np.ndarray], focus_idx: int) -> np.ndarray:
    """Pack camera tiles into a single horizontal row of equal-size tiles.

    Tiles are assumed to share the same shape (the caller derives one
    `tile_size` per render). The focus camera gets a yellow outline drawn
    inside its tile so it is visible against the dark canvas background."""
    if not tiles:
        return np.zeros((1, 1, 3), dtype=np.uint8)
    th, tw = tiles[0].shape[:2]
    canvas = np.zeros((th, tw * len(tiles), 3), dtype=np.uint8)
    for i, tile in enumerate(tiles):
        if i == focus_idx:
            tile = tile.copy()
            cv2.rectangle(tile, (1, 1), (tw - 2, th - 2), _C_FOCUS, 2)
        canvas[:, i * tw:(i + 1) * tw] = tile
    return canvas


def _render_placeholder_tile(tile_size: tuple[int, int], label: str) -> np.ndarray:
    """Empty tile shown for cam slots reserved by the layout but not yet
    connected. Keeps cam0 and cam1 at the same size whether one or both
    cameras are active."""
    tw, th = tile_size
    tile = np.full((th, tw, 3), 30, dtype=np.uint8)
    cv2.rectangle(tile, (1, 1), (tw - 2, th - 2), (60, 60, 60), 1)
    msg = f"{label}  (slot reserved)"
    (mw, mh), _ = cv2.getTextSize(msg, _FONT, 0.6, 1)
    cv2.putText(tile, msg, ((tw - mw) // 2, (th + mh) // 2),
                _FONT, 0.6, _C_PANEL_DIM, 1, cv2.LINE_AA)
    return tile


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


def _uv_to_panel_rect(uv: tuple[float, float, float, float],
                      cw: int, ch: int,
                      inset_top: int = 24, inset: int = 3
                      ) -> tuple[int, int, int, int]:
    """Convert a UV rect to integer panel pixels with edge inset for visibility."""
    u0, v0, u1, v1 = uv
    x0 = int(round(u0 * (cw - 1)))
    y0 = int(round(v0 * (ch - 1)))
    x1 = int(round(u1 * (cw - 1)))
    y1 = int(round(v1 * (ch - 1)))
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0 = min(max(x0 + inset, inset), cw - inset - 1)
    y0 = min(max(y0 + inset, inset_top), ch - inset - 1)
    x1 = max(min(x1 - inset, cw - inset - 1), x0 + 1)
    y1 = max(min(y1 - inset, ch - inset - 1), y0 + 1)
    return x0, y0, x1, y1


def _render_uv_canvas(
    cams: Sequence[CamFrame],
    projections: dict[str, Projection],
    fused_persons: Sequence[FusedPersonFrame] = (),
    trails: Optional[dict[tuple[str, int], list[tuple[float, float]]]] = None,
    edit_target: Optional[tuple[int, str, str]] = None,
    zone_edit_target: Optional[tuple[str, str]] = None,
    overlaps_by_proj: Optional[dict[str, list[str]]] = None,
    target_width: Optional[int] = None,
) -> Optional[np.ndarray]:
    """Render a top-down panel per projection.

    `edit_target`, when set, is `(cam_idx, region_id, kind)` where kind is
    "projection" or "dispatch". The targeted slice gets a thick yellow
    highlight so the operator can see what their nudge keys are moving.
    `overlaps_by_proj` maps projection_id to a list of human-readable overlap
    descriptions; entries are drawn as red lines at the bottom of each panel.
    `target_width`, when set, forces panel width so callers can stack the UV
    canvas under (or beside) other panels of a known size.
    """
    if not projections:
        return None
    overlaps_by_proj = overlaps_by_proj or {}
    trails = trails or {}
    panels: list[np.ndarray] = []
    for proj_id, proj in projections.items():
        cw, ch = _projection_panel_size(proj, target_width)
        panel = np.full((ch, cw, 3), 24, dtype=np.uint8)
        cv2.rectangle(panel, (0, 0), (cw - 1, ch - 1), (90, 90, 90), 1)
        cv2.putText(panel, proj_id, (8, 18), _FONT, 0.5,
                    (200, 200, 200), 1, cv2.LINE_AA)

        reg_lookup: dict[tuple[int, str], Region] = {}
        for ci, cam in enumerate(cams):
            color = _CAM_COLORS[ci % len(_CAM_COLORS)]
            for reg in cam.regions:
                reg_lookup[(ci, reg.id)] = reg
                if reg.projection_id != proj_id:
                    continue
                # projection_uv: faint fill + dotted outline.
                px0, py0, px1, py1 = _uv_to_panel_rect(reg.projection_uv, cw, ch)
                overlay = panel.copy()
                cv2.rectangle(overlay, (px0, py0), (px1, py1), color, -1)
                cv2.addWeighted(overlay, 0.10, panel, 0.90, 0, dst=panel)
                _draw_dotted_rect(panel, (px0, py0), (px1, py1), color)

                # dispatch_uv: stronger fill + solid outline; this is what
                # actually drives OSC dispatch so it should read at a glance.
                dx0, dy0, dx1, dy1 = _uv_to_panel_rect(reg.dispatch_uv, cw, ch)
                overlay = panel.copy()
                cv2.rectangle(overlay, (dx0, dy0), (dx1, dy1), color, -1)
                cv2.addWeighted(overlay, 0.30, panel, 0.70, 0, dst=panel)
                cv2.rectangle(panel, (dx0, dy0), (dx1, dy1), color, 1, cv2.LINE_AA)

                label = f"{cam.name}:{reg.id}"
                cv2.putText(panel, label, (px0 + 6, min(py1 - 6, py0 + 18)),
                            _FONT, 0.45, color, 1, cv2.LINE_AA)

                if edit_target is not None:
                    et_ci, et_rid, et_kind = edit_target
                    if et_ci == ci and et_rid == reg.id:
                        if et_kind == "projection":
                            cv2.rectangle(panel, (px0, py0), (px1, py1),
                                          _C_EDIT_PROJ, 2, cv2.LINE_AA)
                        else:
                            cv2.rectangle(panel, (dx0, dy0), (dx1, dy1),
                                          _C_EDIT_DISPATCH, 2, cv2.LINE_AA)

        for zone in proj.interaction_zones:
            zx0, zy0, zx1, zy1 = _uv_to_panel_rect(zone.uv_rect, cw, ch)
            active_count = sum(
                1 for person in fused_persons
                if person.projection_id == proj_id
                and zone.uv_rect[0] <= person.u <= zone.uv_rect[2]
                and zone.uv_rect[1] <= person.v <= zone.uv_rect[3]
            )
            overlay = panel.copy()
            cv2.rectangle(overlay, (zx0, zy0), (zx1, zy1), _C_ZONE, -1)
            cv2.addWeighted(overlay, 0.18, panel, 0.82, 0, dst=panel)
            cv2.rectangle(panel, (zx0, zy0), (zx1, zy1), _C_ZONE, 2, cv2.LINE_AA)
            label = f"zone:{zone.id} n={active_count}"
            cv2.putText(panel, label, (zx0 + 6, max(34, zy0 + 18)),
                        _FONT, 0.46, _C_ZONE, 1, cv2.LINE_AA)
            if zone_edit_target == (proj_id, zone.id):
                cv2.rectangle(panel, (zx0, zy0), (zx1, zy1),
                              _C_ZONE_EDIT, 2, cv2.LINE_AA)

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

        for person in fused_persons:
            if person.projection_id != proj_id:
                continue
            color = _gid_color(person.gid)
            alpha = 0.95 if person.state == "fresh" else 0.45
            key = (person.projection_id, person.gid)
            pts = trails.get(key, [])
            if len(pts) >= 2:
                trail_pts = np.array([
                    (int(round(u * (cw - 1))), int(round(v * (ch - 1))))
                    for u, v in pts
                ], dtype=np.int32)
                overlay = panel.copy()
                cv2.polylines(overlay, [trail_pts], False, color, 2, cv2.LINE_AA)
                cv2.addWeighted(overlay, 0.45, panel, 0.55, 0, dst=panel)

            px = int(round(person.u * (cw - 1)))
            py = int(round(person.v * (ch - 1)))
            overlay = panel.copy()
            cv2.circle(overlay, (px, py), 10, color, -1, cv2.LINE_AA)
            cv2.circle(overlay, (px, py), 15, color, 1, cv2.LINE_AA)
            cv2.addWeighted(overlay, alpha, panel, 1.0 - alpha, 0, dst=panel)
            end = (
                int(round(px + person.vx * cw * 0.15)),
                int(round(py + person.vy * ch * 0.15)),
            )
            cv2.arrowedLine(panel, (px, py), end, color, 2, cv2.LINE_AA, tipLength=0.35)
            label = f"gid {person.gid} {person.state}"
            cv2.putText(panel, label, (px + 14, max(18, py - 10)),
                        _FONT, 0.48, color, 1, cv2.LINE_AA)

        warnings = overlaps_by_proj.get(proj_id, [])
        if warnings:
            wy = ch - 8 - 16 * len(warnings)
            wy = max(wy, 36)
            cv2.putText(panel, "dispatch overlap:", (8, wy),
                        _FONT, 0.45, _C_WARN, 1, cv2.LINE_AA)
            for i, line in enumerate(warnings):
                cv2.putText(panel, line, (8, wy + 16 * (i + 1)),
                            _FONT, 0.45, _C_WARN, 1, cv2.LINE_AA)
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


def _compute_dispatch_overlaps(
    cams: Sequence[CamFrame],
) -> dict[str, list[str]]:
    """Find dispatch_uv overlaps within each shared projection.

    Returns a dict mapping `projection_id` -> list of human-readable lines like
    `"cam0:near_half <-> cam1:far_half"`. Pairs are deduplicated and ordered.
    Cross-projection pairs are intentionally ignored — overlap only matters
    when two cameras claim the same shared projection.
    """
    by_proj: dict[str, list[tuple[str, str, tuple[float, float, float, float]]]] = {}
    for cam in cams:
        for reg in cam.regions:
            by_proj.setdefault(reg.projection_id, []).append(
                (cam.name, reg.id, reg.dispatch_uv)
            )
    out: dict[str, list[str]] = {}
    for proj_id, items in by_proj.items():
        warnings: list[str] = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a_cam, a_rid, a_rect = items[i]
                b_cam, b_rid, b_rect = items[j]
                if a_cam == b_cam:
                    # Two regions on the same camera typically describe two
                    # disjoint physical lanes; we still warn because OSC dispatch
                    # would double-emit, but skip pairs that exactly share id.
                    if a_rid == b_rid:
                        continue
                if dispatches_overlap(a_rect, b_rect):
                    warnings.append(f"{a_cam}:{a_rid} <-> {b_cam}:{b_rid}")
        if warnings:
            out[proj_id] = warnings
    return out


_PANEL_TABS = ("regions", "lan")


def _draw_panel_tabs(panel: np.ndarray, active_tab: str) -> int:
    h, w = panel.shape[:2]
    tab_h = 30
    tab_w = max(1, w // len(_PANEL_TABS))
    cv2.rectangle(panel, (0, 0), (w - 1, tab_h), (18, 18, 18), -1)
    for i, name in enumerate(_PANEL_TABS):
        x0 = i * tab_w
        x1 = w - 1 if i == len(_PANEL_TABS) - 1 else (i + 1) * tab_w
        selected = name == active_tab
        fill = (44, 44, 44) if selected else (24, 24, 24)
        text_color = _C_PANEL_TEXT if selected else _C_PANEL_DIM
        cv2.rectangle(panel, (x0, 0), (x1, tab_h), fill, -1)
        cv2.rectangle(panel, (x0, 0), (x1, tab_h), (70, 70, 70), 1)
        (tw, th), _ = cv2.getTextSize(name, _FONT, 0.45, 1)
        cv2.putText(panel, name, (x0 + max(6, (x1 - x0 - tw) // 2), 20),
                    _FONT, 0.45, text_color, 1, cv2.LINE_AA)
    return tab_h


def _render_region_panel(
    cams: Sequence[CamFrame],
    fused_persons: Sequence[FusedPersonFrame],
    width: int,
    height: int,
    focus_idx: int,
    focused_region_idx: int,
    dirty: bool,
    overlap_count: int,
    stats: Optional[dict[str, int]] = None,
    edit_status: str = "",
    active_tab: str = "regions",
) -> np.ndarray:
    """Right-side info panel.

    Top section surfaces the dirty/saved state, overlap count, and (when
    active) the current slice-edit target so operators don't have to look
    away from the cameras to see what their next keystroke will do.
    Each camera then gets a header line with fps + OSC rate, followed by
    its region list with the focused entry highlighted.
    """
    panel = np.full((height, width, 3), _C_PANEL_BG[0], dtype=np.uint8)
    cv2.rectangle(panel, (0, 0), (width - 1, height - 1), (90, 90, 90), 1)

    pad = 8
    line_h = 16
    y = _draw_panel_tabs(panel, active_tab) + pad + line_h
    stats = stats or {}

    state_text = "[unsaved]" if dirty else "[saved]"
    state_color = _C_DIRTY if dirty else _C_CLEAN
    cv2.putText(panel, state_text, (pad, y), _FONT, 0.5,
                state_color, 1, cv2.LINE_AA)
    y += line_h

    active_count = len(fused_persons)
    held_count = sum(1 for p in fused_persons if p.state == "held")
    summary = (
        f"active={active_count} held={held_count} "
        f"spawned={stats.get('spawned', 0)} handoff={stats.get('handoff', 0)} "
        f"lost={stats.get('lost', 0)}"
    )
    cv2.putText(panel, summary, (pad, y), _FONT, 0.4,
                _C_PANEL_TEXT, 1, cv2.LINE_AA)
    y += line_h

    if overlap_count > 0:
        cv2.putText(panel, f"overlap: {overlap_count}", (pad, y),
                    _FONT, 0.45, _C_WARN, 1, cv2.LINE_AA)
        y += line_h

    if edit_status:
        cv2.putText(panel, edit_status, (pad, y),
                    _FONT, 0.4, _C_REGION_FOCUS, 1, cv2.LINE_AA)
        y += line_h

    cv2.line(panel, (pad, y - 6), (width - pad, y - 6),
             (70, 70, 70), 1, cv2.LINE_AA)
    y += 4

    if fused_persons:
        cv2.putText(panel, "Fused persons", (pad, y), _FONT, 0.45,
                    _C_PANEL_TEXT, 1, cv2.LINE_AA)
        y += line_h
        for p in fused_persons[:6]:
            if y > height - line_h * 3:
                break
            color = _gid_color(p.gid)
            src = f"{p.source[0]}:{p.source[1]}"
            speed = (p.vx * p.vx + p.vy * p.vy) ** 0.5
            text = (
                f"  gid {p.gid} {p.state} {src} "
                f"uv={p.u:.2f},{p.v:.2f} spd={speed:.2f}"
            )
            cv2.putText(panel, text, (pad, y), _FONT, 0.37,
                        color, 1, cv2.LINE_AA)
            y += line_h
        cv2.line(panel, (pad, y - 6), (width - pad, y - 6),
                 (70, 70, 70), 1, cv2.LINE_AA)
        y += 4

    for ci, cam in enumerate(cams):
        if y > height - line_h * 2:
            cv2.putText(panel, "...", (pad, y), _FONT, 0.45,
                        _C_PANEL_DIM, 1, cv2.LINE_AA)
            break
        cam_color = _CAM_COLORS[ci % len(_CAM_COLORS)]
        is_focus_cam = ci == focus_idx
        marker = ">" if is_focus_cam else " "
        header = f"{marker} [{ci + 1}] {cam.name}  ({len(cam.regions)})"
        cv2.putText(panel, header, (pad, y), _FONT, 0.5,
                    cam_color, 1 if not is_focus_cam else 2, cv2.LINE_AA)
        y += line_h
        cam_stats = (
            f"   {cam.fps:.1f}fps  {cam.osc_rate:.0f}/s  "
            f"age={cam.frame_age_s:.1f}s  rc={cam.reconnects}"
        )
        cv2.putText(panel, cam_stats, (pad, y), _FONT, 0.4,
                    _C_PANEL_DIM, 1, cv2.LINE_AA)
        y += line_h
        if not cam.regions:
            cv2.putText(panel, "   (no regions)", (pad, y), _FONT, 0.4,
                        _C_PANEL_DIM, 1, cv2.LINE_AA)
            y += line_h
            continue
        for ri, reg in enumerate(cam.regions):
            if y > height - line_h:
                break
            is_focus_reg = is_focus_cam and ri == focused_region_idx
            prefix = "  *" if is_focus_reg else "   "
            d = reg.dispatch_uv
            text = (
                f"{prefix} {reg.id} d=[{d[0]:.2f},{d[1]:.2f}->"
                f"{d[2]:.2f},{d[3]:.2f}]"
            )
            text_color = _C_REGION_FOCUS if is_focus_reg else _C_PANEL_TEXT
            cv2.putText(panel, text, (pad, y), _FONT, 0.4,
                        text_color, 1, cv2.LINE_AA)
            y += line_h
        y += 2

    return panel


def _render_lan_panel(
    interfaces: Sequence[LanInterfaceFrame],
    width: int,
    height: int,
    refreshed_at: float,
    active_tab: str = "lan",
) -> np.ndarray:
    panel = np.full((height, width, 3), _C_PANEL_BG[0], dtype=np.uint8)
    cv2.rectangle(panel, (0, 0), (width - 1, height - 1), (90, 90, 90), 1)

    pad = 8
    y = _draw_panel_tabs(panel, active_tab) + pad + 16
    refreshed = time.strftime("%H:%M:%S", time.localtime(refreshed_at)) if refreshed_at else "--:--:--"
    active_count = sum(1 for item in interfaces if item.status == "active" or item.ipv4)
    cv2.putText(panel, f"LAN map  active={active_count}  {refreshed}",
                (pad, y), _FONT, 0.45, _C_PANEL_TEXT, 1, cv2.LINE_AA)
    y += 20

    cv2.putText(panel, "Mac", (pad, y), _FONT, 0.5, _C_CLEAN, 1, cv2.LINE_AA)
    cv2.line(panel, (pad + 24, y + 8), (pad + 24, height - pad),
             (70, 70, 70), 1, cv2.LINE_AA)
    y += 18

    if not interfaces:
        cv2.putText(panel, "No active LAN interfaces", (pad, y + 8),
                    _FONT, 0.42, _C_PANEL_DIM, 1, cv2.LINE_AA)
        return panel

    row_h = 48
    rows_drawn = 0
    for idx, item in enumerate(interfaces):
        if y + row_h > height - 18:
            remain = len(interfaces) - rows_drawn
            cv2.putText(panel, f"... {remain} more", (pad, height - 10),
                        _FONT, 0.42, _C_PANEL_DIM, 1, cv2.LINE_AA)
            break

        active = item.status == "active" or bool(item.ipv4)
        color = _lan_color(idx, active)
        node_x = pad + 24
        row_x = pad + 44
        cv2.circle(panel, (node_x, y + 9), 4, color, -1, cv2.LINE_AA)
        cv2.line(panel, (node_x, y + 9), (row_x - 8, y + 9),
                 color, 1, cv2.LINE_AA)

        badge = "default" if item.is_default else item.status
        badge_color = _C_CLEAN if active else _C_PANEL_DIM
        title = f"{item.device}  {item.service}"
        cv2.putText(panel, title, (row_x, y), _FONT, 0.44,
                    color, 1, cv2.LINE_AA)
        cv2.putText(panel, badge, (row_x, y + 15), _FONT, 0.38,
                    badge_color, 1, cv2.LINE_AA)

        ip_text = ", ".join(item.ipv4) if item.ipv4 else "(no IPv4)"
        cv2.putText(panel, ip_text[:34], (row_x, y + 30), _FONT, 0.38,
                    _C_PANEL_TEXT if item.ipv4 else _C_PANEL_DIM, 1, cv2.LINE_AA)

        detail = ""
        if item.gateway:
            detail = f"gw {item.gateway}"
        elif item.mac:
            detail = item.mac
        if detail:
            cv2.putText(panel, detail[:34], (row_x, y + 43), _FONT, 0.34,
                        _C_PANEL_DIM, 1, cv2.LINE_AA)
        y += row_h
        rows_drawn += 1

    return panel


def _render_lan_page(
    interfaces: Sequence[LanInterfaceFrame],
    target_routes: Sequence[TargetRouteFrame],
    width: int,
    height: int,
    refreshed_at: float,
    active_tab: str = "lan",
) -> np.ndarray:
    canvas = np.full((height, width, 3), 22, dtype=np.uint8)
    tab_h = _draw_panel_tabs(canvas, active_tab)
    pad = 36
    refreshed = (
        time.strftime("%H:%M:%S", time.localtime(refreshed_at))
        if refreshed_at else "--:--:--"
    )
    active_count = sum(1 for item in interfaces if item.status == "active" or item.ipv4)

    y = tab_h + 44
    cv2.putText(canvas, "LAN map", (pad, y), _FONT, 1.0,
                _C_PANEL_TEXT, 2, cv2.LINE_AA)
    summary = f"active={active_count}  refreshed={refreshed}"
    cv2.putText(canvas, summary, (pad + 190, y - 4), _FONT, 0.62,
                _C_PANEL_DIM, 1, cv2.LINE_AA)
    y += 34

    active_items = [item for item in interfaces if item.status == "active" or item.ipv4]
    inactive_items = [item for item in interfaces if item not in active_items]
    main_items = active_items or interfaces[:3]
    routes_by_iface: dict[str, list[TargetRouteFrame]] = {}
    unrouted_targets: list[TargetRouteFrame] = []
    for route in target_routes:
        if route.iface:
            routes_by_iface.setdefault(route.iface, []).append(route)
        else:
            unrouted_targets.append(route)

    topology_h = min(230, max(190, height // 3))
    _draw_field_topology(
        canvas,
        interfaces,
        target_routes,
        pad,
        y,
        width - pad * 2,
        topology_h,
    )
    y += topology_h + 34

    cv2.putText(canvas, "Interfaces / routes", (pad, y), _FONT, 0.72,
                _C_PANEL_TEXT, 2, cv2.LINE_AA)
    y += 14

    if not interfaces:
        cv2.putText(canvas, "No LAN interfaces found", (pad, y + 42),
                    _FONT, 0.68, _C_PANEL_DIM, 1, cv2.LINE_AA)
        cv2.putText(canvas, "Tab switches pages", (pad, height - 18),
                    _FONT, 0.5, _C_PANEL_DIM, 1, cv2.LINE_AA)
        return canvas

    mac_x = pad
    node_x = pad + 134
    card_x = pad + 196
    card_right = width - pad
    content_bottom = height - 42
    row_h = 108

    cv2.putText(canvas, "Mac", (mac_x, y + 44), _FONT, 0.78,
                _C_CLEAN, 2, cv2.LINE_AA)

    for idx, item in enumerate(main_items):
        row_top = y + idx * row_h
        row_mid = row_top + row_h // 2
        if row_top + row_h > content_bottom - 118:
            remain = len(main_items) - idx
            cv2.putText(canvas, f"... {remain} more", (card_x, content_bottom - 18),
                        _FONT, 0.55, _C_PANEL_DIM, 1, cv2.LINE_AA)
            break

        active = item.status == "active" or bool(item.ipv4)
        color = _lan_color(idx, active)
        fill = (34, 34, 34) if active else (27, 27, 27)
        cv2.circle(canvas, (node_x, row_mid), 8, color, -1, cv2.LINE_AA)
        cv2.line(canvas, (node_x + 10, row_mid), (card_x - 20, row_mid),
                 color, 2, cv2.LINE_AA)
        cv2.rectangle(canvas, (card_x - 20, row_top + 4),
                      (card_right, row_top + row_h - 8), fill, -1)
        cv2.rectangle(canvas, (card_x - 20, row_top + 4),
                      (card_right, row_top + row_h - 8),
                      (58, 58, 58), 1)

        title = f"{item.device}   {item.service}"
        cv2.putText(canvas, title, (card_x, row_top + 36),
                    _FONT, 0.82, color, 2, cv2.LINE_AA)

        badge = "DEFAULT ROUTE" if item.is_default else item.status.upper()
        badge_color = _C_CLEAN if active else _C_PANEL_DIM
        cv2.putText(canvas, badge, (card_right - 230, row_top + 36),
                    _FONT, 0.58, badge_color, 1, cv2.LINE_AA)

        ip_text = "  ".join(item.ipv4) if item.ipv4 else "no IPv4 assigned"
        cv2.putText(canvas, ip_text, (card_x, row_top + 76),
                    _FONT, 0.9,
                    _C_PANEL_TEXT if item.ipv4 else _C_PANEL_DIM,
                    2 if item.ipv4 else 1, cv2.LINE_AA)

        details = []
        if item.gateway:
            details.append(f"gateway {item.gateway}")
        if item.mac:
            details.append(f"mac {item.mac}")
        if item.media:
            details.append(item.media)
        if details:
            cv2.putText(canvas, "   ".join(details)[:92],
                        (card_x, row_top + row_h - 26),
                        _FONT, 0.48, _C_PANEL_DIM, 1, cv2.LINE_AA)

        targets = routes_by_iface.get(item.device, [])
        target_x = max(card_x + 640, card_right - 560)
        target_y = row_top + 62
        if targets:
            cv2.putText(canvas, "Targets", (target_x, row_top + 34),
                        _FONT, 0.48, _C_PANEL_DIM, 1, cv2.LINE_AA)
        for route in targets[:4]:
            status, status_color = _route_status(route)
            label = f"{route.target.name}  {route.target.host}"
            if route.target.port:
                label += f":{route.target.port}"
            cv2.circle(canvas, (target_x, target_y - 5), 5, status_color, -1, cv2.LINE_AA)
            cv2.putText(canvas, label[:42], (target_x + 14, target_y),
                        _FONT, 0.46, _C_PANEL_TEXT, 1, cv2.LINE_AA)
            cv2.putText(canvas, status, (target_x + 14, target_y + 20),
                        _FONT, 0.38, status_color, 1, cv2.LINE_AA)
            target_y += 42

    adapter_top = y + row_h * len(main_items) + 18
    adapter_top = min(adapter_top, max(y + 130, height - 220))
    cv2.line(canvas, (pad, adapter_top - 18), (width - pad, adapter_top - 18),
             (54, 54, 54), 1, cv2.LINE_AA)
    cv2.putText(canvas, "Adapters", (pad, adapter_top + 16), _FONT, 0.72,
                _C_PANEL_TEXT, 2, cv2.LINE_AA)
    cv2.putText(canvas, f"inactive={len(inactive_items)}", (pad + 150, adapter_top + 14),
                _FONT, 0.52, _C_PANEL_DIM, 1, cv2.LINE_AA)

    grid_x = pad
    grid_y = adapter_top + 42
    gap = 12
    usable_w = width - pad * 2
    col_count = max(2, min(6, usable_w // 240))
    card_w = max(180, (usable_w - gap * (col_count - 1)) // col_count)
    card_h = 74
    if inactive_items:
        max_cards = max(1, (content_bottom - grid_y - 20) // (card_h + gap) * col_count)
        for i, item in enumerate(inactive_items[:max_cards]):
            col = i % col_count
            row = i // col_count
            x = grid_x + col * (card_w + gap)
            y2 = grid_y + row * (card_h + gap)
            color = _lan_color(i + len(main_items), False)
            cv2.rectangle(canvas, (x, y2), (x + card_w, y2 + card_h),
                          (31, 31, 31), -1)
            cv2.rectangle(canvas, (x, y2), (x + card_w, y2 + card_h),
                          (52, 52, 52), 1)
            cv2.circle(canvas, (x + 18, y2 + 22), 5, color, -1, cv2.LINE_AA)
            cv2.putText(canvas, item.device, (x + 34, y2 + 28),
                        _FONT, 0.56, color, 1, cv2.LINE_AA)
            cv2.putText(canvas, item.service[:28], (x + 34, y2 + 52),
                        _FONT, 0.44, _C_PANEL_DIM, 1, cv2.LINE_AA)
        if len(inactive_items) > max_cards:
            more_y = content_bottom - 18
            cv2.putText(canvas, f"+{len(inactive_items) - max_cards} more adapters",
                        (grid_x, more_y), _FONT, 0.48, _C_PANEL_DIM, 1, cv2.LINE_AA)
    else:
        cv2.putText(canvas, "No inactive adapters", (grid_x, grid_y + 20),
                    _FONT, 0.52, _C_PANEL_DIM, 1, cv2.LINE_AA)

    if unrouted_targets:
        warn_x = max(pad, width - pad - 520)
        warn_y = height - 52
        cv2.putText(canvas, "Unrouted targets:", (warn_x, warn_y),
                    _FONT, 0.45, _C_WARN, 1, cv2.LINE_AA)
        x = warn_x + 170
        for route in unrouted_targets[:3]:
            cv2.putText(canvas, f"{route.target.name} {route.target.host}",
                        (x, warn_y), _FONT, 0.42, _C_PANEL_TEXT, 1, cv2.LINE_AA)
            x += 170

    cv2.putText(canvas, "Tab switches pages", (pad, height - 18),
                _FONT, 0.5, _C_PANEL_DIM, 1, cv2.LINE_AA)
    return canvas


def _draw_dashboard_bar(
    canvas: np.ndarray,
    cams: Sequence[CamFrame],
    fused_persons: Sequence[FusedPersonFrame],
    overlap_count: int,
    stats: Optional[dict[str, int]] = None,
) -> None:
    stats = stats or {}
    stale = any(cam.frame is None or cam.frame_age_s > 2.0 for cam in cams)
    degraded = stale or overlap_count > 0
    label = "DEGRADED" if degraded else "RUNNING"
    color = _C_WARN if degraded else _C_CLEAN
    bar_h = 34
    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, 0), (canvas.shape[1], bar_h), (12, 12, 12), -1)
    cv2.addWeighted(overlay, 0.82, canvas, 0.18, 0, dst=canvas)
    cv2.putText(canvas, label, (10, 23), _FONT, 0.65, color, 2, cv2.LINE_AA)
    held = sum(1 for p in fused_persons if p.state == "held")
    text = (
        f"active={len(fused_persons)} held={held} "
        f"spawned={stats.get('spawned', 0)} handoff={stats.get('handoff', 0)} "
        f"lost={stats.get('lost', 0)} overlap={overlap_count}"
    )
    cv2.putText(canvas, text, (140, 23), _FONT, 0.55,
                (230, 230, 230), 1, cv2.LINE_AA)


class Viewer:
    """Owns the cv2 window. Call render() each tick with the latest CamFrames."""

    def __init__(
        self,
        projections: dict[str, Projection],
        tile_size: tuple[int, int] = (640, 360),
        initial_window_size: tuple[int, int] = (1280, 720),
        network_targets: Sequence[NetworkTargetFrame] = (),
        on_regions_changed: Optional[Callable[[int, list[Region]], None]] = None,
        on_zones_changed: Optional[Callable[[str, list[InteractionZone]], None]] = None,
        on_save: Optional[Callable[[], None]] = None,
    ):
        self.projections = projections
        self.network_targets = list(network_targets)
        self.tile_size = tile_size
        self.initial_window_size = initial_window_size
        self.on_regions_changed = on_regions_changed
        self.on_zones_changed = on_zones_changed
        self.on_save = on_save
        self.show_hud = True
        self.show_uv = True
        self.show_panel = True
        self.panel_tab = "regions"
        self.focus_idx = 0
        self.focused_region_idx = -1  # -1 means "no region selected"
        self.draw_mode = False
        self.draft_points: list[tuple[float, float]] = []
        self.zone_draw_mode = False
        self.draft_zone_points: list[tuple[str, float, float]] = []
        # UV slice editing state. `edit_region_id` is None when not editing.
        self.edit_region_id: Optional[str] = None
        self.edit_kind: str = "projection"          # "projection" | "dispatch"
        self.edit_edge_idx: int = 0                  # index into _EDIT_EDGES
        self.edit_zone: Optional[tuple[str, str]] = None
        self.edit_zone_edge_idx: int = 0
        # dirty=False: regions match the last on-disk save (initial load = clean).
        # Flips True on every on_regions_changed and back to False after on_save.
        self.dirty = False
        self.status = (
            "d draw | z draw zone | e edit slices | v edit zone | [ ] cycle/nudge | x delete | w save | "
            "h/u/p toggles | Tab panel | 1-9 focus | q quit"
        )
        self._last_cams: Sequence[CamFrame] = []
        self._rendered_tile_size: tuple[int, int] = tile_size
        self._window_created = False
        self._window_failed = False
        self._topmost_pumped = False
        self._info_panel_width = 280
        self._target_canvas_width = max(initial_window_size[0], 800)
        self._min_cam_slots = 2
        self._person_trails: dict[tuple[str, int], list[tuple[float, float]]] = {}
        self._trail_len = 48
        self._uv_panel_bounds: dict[str, tuple[int, int, int, int]] = {}
        self._lan_snapshot: list[LanInterfaceFrame] = []
        self._target_routes: list[TargetRouteFrame] = []
        self._lan_refreshed_at = 0.0
        self._lan_last_refresh_mono = 0.0
        self._lan_refresh_interval_s = 2.0

    def _ensure_window(self) -> bool:
        """Create the cv2 window once. Returns False if cv2 GUI is unavailable
        (e.g. opencv-python-headless build). Subsequent calls become no-ops.
        """
        if self._window_created:
            return True
        if self._window_failed:
            return False
        try:
            # WINDOW_GUI_NORMAL avoids the Cocoa toolbar overlay on macOS and
            # keeps imshow event handling consistent across platforms.
            flags = cv2.WINDOW_NORMAL
            gui_normal = getattr(cv2, "WINDOW_GUI_NORMAL", 0)
            cv2.namedWindow(WINDOW_NAME, flags | gui_normal)
            cv2.setMouseCallback(WINDOW_NAME, self._on_mouse)
            # Open at a sensible size; WINDOW_NORMAL lets the operator drag-resize.
            try:
                iw, ih = self.initial_window_size
                cv2.resizeWindow(WINDOW_NAME, int(iw), int(ih))
            except cv2.error:
                pass
            # Pull the window to the front once at startup; we clear the
            # topmost flag again right after the first imshow so the operator
            # can stack other apps on top later if they want.
            try:
                cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_TOPMOST, 1)
            except cv2.error:
                # Some cv2 builds lack TOPMOST; non-fatal.
                pass
            self._window_created = True
            print(
                f"[viewer] window '{WINDOW_NAME}' opened "
                "(use q or Esc to quit)",
                flush=True,
            )
            return True
        except cv2.error as ex:
            self._window_failed = True
            print(
                f"[viewer] failed to open cv2 window: {ex}. "
                "If you installed opencv-python-headless, replace it with "
                "opencv-python; otherwise check that a display is available.",
                file=sys.stderr,
                flush=True,
            )
            return False

    def _on_mouse(self, event: int, x: int, y: int, _flags: int, _param: object) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        if self.zone_draw_mode:
            mapped_zone = self._mouse_to_uv_point(x, y)
            if mapped_zone is None:
                self.status = "click inside the projection UV canvas"
                return
            pid, u, v = mapped_zone
            if self.draft_zone_points and self.draft_zone_points[0][0] != pid:
                self.draft_zone_points = []
                self.status = "zone draft restarted on a different projection"
            self.draft_zone_points.append((pid, u, v))
            self.status = f"zone corner {len(self.draft_zone_points)}/2 added"
            if len(self.draft_zone_points) == 2:
                self._commit_draft_zone()
            return
        if not self.draw_mode:
            return
        mapped = self._mouse_to_frame_point(x, y)
        if mapped is None:
            self.status = "click inside the focused camera tile"
            return
        self.draft_points.append(mapped)
        self.status = f"point {len(self.draft_points)}/4 added"
        if len(self.draft_points) == 4:
            self._commit_draft_region()

    def _mouse_to_uv_point(self, x: int, y: int) -> Optional[tuple[str, float, float]]:
        for pid, (x0, y0, w, h) in self._uv_panel_bounds.items():
            if x0 <= x < x0 + w and y0 <= y < y0 + h:
                u = max(0.0, min(1.0, (x - x0) / float(max(w - 1, 1))))
                v = max(0.0, min(1.0, (y - y0) / float(max(h - 1, 1))))
                return pid, u, v
        return None

    def _mouse_to_frame_point(self, x: int, y: int) -> Optional[tuple[float, float]]:
        if self.focus_idx >= len(self._last_cams):
            return None
        cam = self._last_cams[self.focus_idx]
        if cam.frame is None:
            return None
        tw, th = self._rendered_tile_size
        # Cameras are packed into a single row at the top-left of the canvas
        # (info panel sits to their right, UV sits below — neither is a hit).
        tile_x = x - self.focus_idx * tw
        tile_y = y
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

    def _next_zone_id(self, projection_id: str) -> str:
        projection = self.projections.get(projection_id)
        existing = {z.id for z in projection.interaction_zones} if projection else set()
        base = "zone"
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
        self.dirty = True
        self.focused_region_idx = len(regions) - 1
        self.draft_points = []
        self.draw_mode = False
        self.status = f"added {region.id}; press w to save config"

    def _commit_draft_zone(self) -> None:
        if len(self.draft_zone_points) != 2:
            return
        pid0, u0, v0 = self.draft_zone_points[0]
        pid1, u1, v1 = self.draft_zone_points[1]
        self.draft_zone_points = []
        self.zone_draw_mode = False
        if pid0 != pid1 or pid0 not in self.projections:
            self.status = "zone rejected: corners must be on one projection"
            return
        u0, u1 = sorted((round(u0, 4), round(u1, 4)))
        v0, v1 = sorted((round(v0, 4), round(v1, 4)))
        if u1 - u0 < _UV_MIN_SPAN or v1 - v0 < _UV_MIN_SPAN:
            self.status = "zone rejected: rectangle is too small"
            return
        projection = self.projections[pid0]
        zone = InteractionZone(
            projection_id=pid0,
            id=self._next_zone_id(pid0),
            uv_rect=(u0, v0, u1, v1),
            release_after_s=0.6,
        )
        zones = [*projection.interaction_zones, zone]
        if self.on_zones_changed is not None:
            self.on_zones_changed(pid0, zones)
        self.edit_zone = (pid0, zone.id)
        self.edit_zone_edge_idx = 0
        self.dirty = True
        self.status = f"added zone {zone.id}; press w to save config"

    def _delete_last_region(self) -> None:
        if self.focus_idx >= len(self._last_cams):
            return
        cam = self._last_cams[self.focus_idx]
        if not cam.regions:
            self.status = f"{cam.name} has no regions to delete"
            return
        removed = cam.regions[-1]
        if removed.id == self.edit_region_id:
            self._exit_edit_mode()
        if self.on_regions_changed is not None:
            self.on_regions_changed(self.focus_idx, list(cam.regions[:-1]))
        self.dirty = True
        if self.focused_region_idx >= len(cam.regions) - 1:
            self.focused_region_idx = -1
        self.status = f"deleted {removed.id}; press w to save config"

    def _delete_focused_zone(self) -> None:
        if self.edit_zone is None:
            return
        pid, zid = self.edit_zone
        projection = self.projections.get(pid)
        if projection is None:
            self.edit_zone = None
            return
        zones = [z for z in projection.interaction_zones if z.id != zid]
        if len(zones) == len(projection.interaction_zones):
            self.edit_zone = None
            self.status = "zone no longer exists"
            return
        if self.on_zones_changed is not None:
            self.on_zones_changed(pid, zones)
        self.edit_zone = None
        self.dirty = True
        self.status = f"deleted zone {zid}; press w to save config"

    def _exit_edit_mode(self) -> None:
        self.edit_region_id = None
        self.edit_edge_idx = 0
        self.edit_kind = "projection"

    def _exit_zone_edit_mode(self) -> None:
        self.edit_zone = None
        self.edit_zone_edge_idx = 0

    def _focused_cam(self) -> Optional[CamFrame]:
        if self.focus_idx >= len(self._last_cams):
            return None
        return self._last_cams[self.focus_idx]

    def _focused_region(self) -> Optional[Region]:
        cam = self._focused_cam()
        if cam is None or self.edit_region_id is None:
            return None
        for reg in cam.regions:
            if reg.id == self.edit_region_id:
                return reg
        return None

    def _all_zone_keys(self) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        for pid, projection in self.projections.items():
            for zone in projection.interaction_zones:
                keys.append((pid, zone.id))
        return keys

    def _focused_zone(self) -> Optional[InteractionZone]:
        if self.edit_zone is None:
            return None
        pid, zid = self.edit_zone
        projection = self.projections.get(pid)
        if projection is None:
            return None
        return next((z for z in projection.interaction_zones if z.id == zid), None)

    def _toggle_edit_mode(self) -> None:
        cam = self._focused_cam()
        if cam is None or not cam.regions:
            self.status = "no region to edit; draw one with d first"
            self._exit_edit_mode()
            return
        ids = [r.id for r in cam.regions]
        if self.edit_region_id is None or self.edit_region_id not in ids:
            # Enter edit mode on the first region of the focused camera.
            self.edit_region_id = ids[0]
            self.edit_kind = "projection"
            self.edit_edge_idx = 0
        else:
            # Already editing — cycle to next region within the focused cam,
            # exiting after the last one.
            cur = ids.index(self.edit_region_id)
            if cur + 1 < len(ids):
                self.edit_region_id = ids[cur + 1]
                self.edit_kind = "projection"
                self.edit_edge_idx = 0
            else:
                self._exit_edit_mode()
                self.status = "exited slice edit mode"
                return
        self.status = self._edit_status()

    def _toggle_zone_edit_mode(self) -> None:
        keys = self._all_zone_keys()
        if not keys:
            self.status = "no interaction zones; draw one with z first"
            self._exit_zone_edit_mode()
            return
        if self.edit_zone is None or self.edit_zone not in keys:
            self.edit_zone = keys[0]
            self.edit_zone_edge_idx = 0
        else:
            cur = keys.index(self.edit_zone)
            if cur + 1 < len(keys):
                self.edit_zone = keys[cur + 1]
                self.edit_zone_edge_idx = 0
            else:
                self._exit_zone_edit_mode()
                self.status = "exited zone edit mode"
                return
        self.status = self._zone_edit_status()

    def _toggle_edit_kind(self) -> None:
        if self.edit_region_id is None:
            return
        self.edit_kind = "dispatch" if self.edit_kind == "projection" else "projection"
        self.status = self._edit_status()

    def _cycle_edit_edge(self) -> None:
        if self.edit_region_id is None:
            return
        self.edit_edge_idx = (self.edit_edge_idx + 1) % len(_EDIT_EDGES)
        self.status = self._edit_status()

    def _cycle_zone_edge(self) -> None:
        if self.edit_zone is None:
            return
        self.edit_zone_edge_idx = (self.edit_zone_edge_idx + 1) % len(_EDIT_EDGES)
        self.status = self._zone_edit_status()

    def _edit_status(self) -> str:
        edge = _EDIT_EDGES[self.edit_edge_idx]
        return (f"edit {self.edit_kind}_uv {self.edit_region_id} edge={edge}"
                f"  [/]±{_NUDGE_FINE:.2f}  ,/.±{_NUDGE_COARSE:.2f}"
                f"  t kind  g edge  r reset  e next/exit")

    def _zone_edit_status(self) -> str:
        if self.edit_zone is None:
            return ""
        pid, zid = self.edit_zone
        edge = _EDIT_EDGES[self.edit_zone_edge_idx]
        return (f"edit zone {pid}:{zid} edge={edge}"
                f"  [/]±{_NUDGE_FINE:.2f}  ,/.±{_NUDGE_COARSE:.2f}"
                f"  g edge  x delete  v next/exit")

    def _reset_edit_slice(self) -> None:
        cam = self._focused_cam()
        reg = self._focused_region()
        if cam is None or reg is None:
            return
        if self.edit_kind == "projection":
            new_proj = (0.0, 0.0, 1.0, 1.0)
            new_disp = new_proj
        else:
            new_proj = reg.projection_uv
            new_disp = reg.projection_uv
        self._apply_slice_change(cam, reg, new_proj, new_disp,
                                 reason=f"reset {self.edit_kind}_uv")

    def _nudge_slice(self, delta: float) -> None:
        cam = self._focused_cam()
        reg = self._focused_region()
        if cam is None or reg is None:
            return
        edge = _EDIT_EDGES[self.edit_edge_idx]
        new_proj = list(reg.projection_uv)
        new_disp = list(reg.dispatch_uv)
        target = new_proj if self.edit_kind == "projection" else new_disp
        idx = _EDIT_EDGES.index(edge)
        nv = round(target[idx] + delta, 4)
        # Clamp to [0, 1] and keep min span between paired edges.
        if idx == 0:        # u0
            nv = max(0.0, min(nv, target[2] - _UV_MIN_SPAN))
        elif idx == 2:      # u1
            nv = min(1.0, max(nv, target[0] + _UV_MIN_SPAN))
        elif idx == 1:      # v0
            nv = max(0.0, min(nv, target[3] - _UV_MIN_SPAN))
        else:               # v1
            nv = min(1.0, max(nv, target[1] + _UV_MIN_SPAN))
        target[idx] = nv
        self._apply_slice_change(cam, reg, tuple(new_proj), tuple(new_disp),
                                 reason=f"{self.edit_kind}.{edge}={nv:+.2f}")

    def _nudge_zone(self, delta: float) -> None:
        zone = self._focused_zone()
        if zone is None:
            return
        edge = _EDIT_EDGES[self.edit_zone_edge_idx]
        rect = list(zone.uv_rect)
        idx = _EDIT_EDGES.index(edge)
        nv = round(rect[idx] + delta, 4)
        if idx == 0:
            nv = max(0.0, min(nv, rect[2] - _UV_MIN_SPAN))
        elif idx == 2:
            nv = min(1.0, max(nv, rect[0] + _UV_MIN_SPAN))
        elif idx == 1:
            nv = max(0.0, min(nv, rect[3] - _UV_MIN_SPAN))
        else:
            nv = min(1.0, max(nv, rect[1] + _UV_MIN_SPAN))
        rect[idx] = nv
        self._apply_zone_change(zone, tuple(rect), f"zone.{edge}={nv:+.2f}")

    def _apply_zone_change(
        self,
        zone: InteractionZone,
        uv_rect: tuple[float, float, float, float],
        reason: str,
    ) -> None:
        projection = self.projections.get(zone.projection_id)
        if projection is None:
            return
        u0, v0, u1, v1 = uv_rect
        if not (0.0 <= u0 < u1 <= 1.0 and 0.0 <= v0 < v1 <= 1.0):
            self.status = f"zone rejected: {_fmt_uv(uv_rect)}"
            return
        updated = replace(zone, uv_rect=uv_rect)
        zones = [updated if z.id == zone.id else z for z in projection.interaction_zones]
        if self.on_zones_changed is not None:
            self.on_zones_changed(zone.projection_id, zones)
        self.dirty = True
        self.status = f"{reason}  zone={_fmt_uv(uv_rect)}  (w to save)"

    def _apply_slice_change(self, cam: CamFrame, reg: Region,
                            new_proj: tuple[float, float, float, float],
                            new_disp: tuple[float, float, float, float],
                            reason: str) -> None:
        """Validate, rebuild homography, and push the new region list back."""
        try:
            # If projection_uv shrank below dispatch_uv, clip dispatch to fit
            # so the operator can keep editing without an immediate rejection.
            if self.edit_kind == "projection":
                pu0, pv0, pu1, pv1 = new_proj
                du0, dv0, du1, dv1 = new_disp
                new_disp = (
                    max(du0, pu0), max(dv0, pv0),
                    min(du1, pu1), min(dv1, pv1),
                )
            validate_dispatch(new_proj, new_disp)
            new_H = build_homography(reg.image_points, new_proj)
        except ValueError as ex:
            self.status = f"slice rejected: {ex}"
            return

        updated_region = replace(
            reg,
            projection_uv=new_proj,
            dispatch_uv=new_disp,
            H=new_H,
        )
        new_regions = [updated_region if r.id == reg.id else r
                       for r in cam.regions]
        if self.on_regions_changed is not None:
            self.on_regions_changed(self.focus_idx, new_regions)
        self.status = (f"{reason}  proj={_fmt_uv(new_proj)} "
                       f"disp={_fmt_uv(new_disp)}  (w to save)")

    def _draw_draft(self, canvas: np.ndarray) -> None:
        if self.draft_zone_points:
            pts: list[tuple[int, int]] = []
            for pid, u, v in self.draft_zone_points:
                bounds = self._uv_panel_bounds.get(pid)
                if bounds is None:
                    continue
                x0, y0, w, h = bounds
                pts.append((
                    x0 + int(round(u * (w - 1))),
                    y0 + int(round(v * (h - 1))),
                ))
            for i, pt in enumerate(pts):
                cv2.circle(canvas, pt, 5, _C_ZONE, -1, cv2.LINE_AA)
                cv2.putText(canvas, str(i + 1), (pt[0] + 7, pt[1] - 7),
                            _FONT, 0.5, _C_ZONE, 1, cv2.LINE_AA)
            if len(pts) == 2:
                _draw_dotted_rect(canvas, pts[0], pts[1], _C_ZONE)
        if not self.draft_points or self.focus_idx >= len(self._last_cams):
            return
        cam = self._last_cams[self.focus_idx]
        if cam.frame is None:
            return
        tw, th = self._rendered_tile_size
        # Cam tiles share one row, packed left-to-right starting at x=0.
        origin_x, origin_y = self.focus_idx * tw, 0
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

    def _draw_status(
        self,
        canvas: np.ndarray,
        overlap_summary: str = "",
    ) -> None:
        state_text = "[unsaved]" if self.dirty else "[saved]"
        state_color = _C_DIRTY if self.dirty else _C_CLEAN
        first_line = f"{state_text}  {self.status}"
        if overlap_summary:
            first_line += f"  | {overlap_summary}"
        lines = [
            first_line,
            "region draw: projection top-left -> top-right -> bottom-right -> bottom-left | zone draw: click two UV corners",
        ]
        pad = 8
        line_h = 20
        y0 = canvas.shape[0] - line_h * len(lines) - pad * 2
        overlay = canvas.copy()
        cv2.rectangle(overlay, (0, max(0, y0)), (canvas.shape[1], canvas.shape[0]),
                      (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.6, canvas, 0.4, 0, dst=canvas)
        # State badge in its own colour, then the rest of the status text in white.
        (sw, _sh), _ = cv2.getTextSize(state_text, _FONT, 0.5, 1)
        cv2.putText(canvas, state_text, (pad, y0 + pad + line_h - 5),
                    _FONT, 0.5, state_color, 1, cv2.LINE_AA)
        rest = first_line[len(state_text):]
        cv2.putText(canvas, rest, (pad + sw, y0 + pad + line_h - 5),
                    _FONT, 0.5, (230, 230, 230), 1, cv2.LINE_AA)
        for i, line in enumerate(lines[1:], start=2):
            cv2.putText(canvas, line, (pad, y0 + pad + line_h * i - 5),
                        _FONT, 0.5, (230, 230, 230), 1, cv2.LINE_AA)

    def _current_lan_interfaces(self) -> list[LanInterfaceFrame]:
        now = time.monotonic()
        if (
            self._lan_refreshed_at == 0.0
            or now - self._lan_last_refresh_mono >= self._lan_refresh_interval_s
        ):
            self._lan_snapshot = _collect_lan_interfaces()
            self._target_routes = _collect_target_routes(
                self.network_targets, self._lan_snapshot
            )
            self._lan_last_refresh_mono = now
            self._lan_refreshed_at = time.time()
        return self._lan_snapshot

    def _current_target_routes(self) -> list[TargetRouteFrame]:
        self._current_lan_interfaces()
        return self._target_routes

    def _window_canvas_size(self) -> tuple[int, int]:
        width = max(self._target_canvas_width, self.initial_window_size[0], 2048)
        height = max(self.initial_window_size[1], 720)
        try:
            _x, _y, win_w, win_h = cv2.getWindowImageRect(WINDOW_NAME)
            if win_w > 0 and win_h > 0:
                width = max(width, int(win_w))
                height = max(height, int(win_h))
        except (AttributeError, cv2.error):
            pass
        return width, height

    def render(
        self,
        cams: Sequence[CamFrame],
        fused_persons: Sequence[FusedPersonFrame] = (),
        stats: Optional[dict[str, int]] = None,
    ) -> bool:
        """Compose + show. Returns False to request shutdown (q/Esc pressed)."""
        if not self._ensure_window():
            # No GUI available; keep the OSC pipeline running but don't burn
            # a tight loop in the caller.
            self._last_cams = cams
            time.sleep(0.05)
            return True
        self._last_cams = cams
        active_keys = {(p.projection_id, p.gid) for p in fused_persons}
        for p in fused_persons:
            key = (p.projection_id, p.gid)
            trail = self._person_trails.setdefault(key, [])
            if not trail or abs(trail[-1][0] - p.u) > 1e-4 or abs(trail[-1][1] - p.v) > 1e-4:
                trail.append((p.u, p.v))
            del trail[:-self._trail_len]
        for key in list(self._person_trails.keys()):
            if key not in active_keys:
                del self._person_trails[key]
        # Keep focused_region_idx within bounds whenever the focused camera's
        # region list shrinks under us (e.g. external config reload).
        if 0 <= self.focus_idx < len(cams):
            n_regions = len(cams[self.focus_idx].regions)
            if self.focused_region_idx >= n_regions:
                self.focused_region_idx = n_regions - 1 if n_regions else -1
        else:
            self.focused_region_idx = -1
        if not cams:
            blank = np.zeros((self.tile_size[1], self.tile_size[0], 3), dtype=np.uint8)
            cv2.putText(blank, "no cameras", (20, 40),
                        _FONT, 0.7, (200, 200, 200), 2)
            self._draw_status(blank)
            try:
                cv2.imshow(WINDOW_NAME, blank)
            except cv2.error as ex:
                self._window_failed = True
                print(f"[viewer] imshow failed: {ex}", file=sys.stderr, flush=True)
                return True
        elif self.panel_tab == "lan":
            page_w, page_h = self._window_canvas_size()
            canvas = _render_lan_page(
                self._current_lan_interfaces(),
                self._current_target_routes(),
                page_w,
                page_h,
                self._lan_refreshed_at,
                self.panel_tab,
            )
            try:
                cv2.resizeWindow(WINDOW_NAME, canvas.shape[1], canvas.shape[0])
            except cv2.error:
                pass
            try:
                cv2.imshow(WINDOW_NAME, canvas)
            except cv2.error as ex:
                self._window_failed = True
                print(f"[viewer] imshow failed: {ex}", file=sys.stderr, flush=True)
                return True
        else:
            # Layout: cameras packed left-to-right in the top row, info panel
            # to their right, UV canvas full-width below. Status bar is drawn
            # on top of the bottom edge as a single overlay.
            info_w = self._info_panel_width if self.show_panel else 0
            cam_row_w = max(self._target_canvas_width - info_w, 320)
            # Reserve at least `min_cam_slots` tiles so cam0 and cam1 stay the
            # same size when only one camera is currently connected; the empty
            # slots render as dim placeholders rather than reflowing the row.
            slots = max(len(cams), self._min_cam_slots)
            tile_w = cam_row_w // slots
            tile_h = max(80, int(round(tile_w * 9 / 16)))  # 16:9 cam aspect
            self._rendered_tile_size = (tile_w, tile_h)
            tiles = [
                _render_tile(
                    c,
                    self._rendered_tile_size,
                    self.show_hud,
                    self.focused_region_idx if i == self.focus_idx else -1,
                )
                for i, c in enumerate(cams)
            ]
            for i in range(len(tiles), slots):
                tiles.append(_render_placeholder_tile(
                    self._rendered_tile_size, f"cam{i}"
                ))
            cam_row = _compose_cam_row(tiles, self.focus_idx)

            overlaps = _compute_dispatch_overlaps(cams)
            overlap_count = sum(len(v) for v in overlaps.values())

            if self.show_panel:
                if self.panel_tab == "lan":
                    panel = _render_lan_panel(
                        self._current_lan_interfaces(),
                        info_w,
                        cam_row.shape[0],
                        self._lan_refreshed_at,
                        self.panel_tab,
                    )
                else:
                    if self.edit_region_id is not None:
                        edit_status_text = self._edit_status()
                    elif self.edit_zone is not None:
                        edit_status_text = self._zone_edit_status()
                    else:
                        edit_status_text = ""
                    panel = _render_region_panel(
                        cams,
                        fused_persons,
                        info_w,
                        cam_row.shape[0],
                        self.focus_idx,
                        self.focused_region_idx,
                        self.dirty,
                        overlap_count,
                        stats,
                        edit_status_text,
                        self.panel_tab,
                    )
                top_row = np.hstack([cam_row, panel])
            else:
                top_row = cam_row

            if self.show_uv:
                self._uv_panel_bounds = {}
                uv_y = top_row.shape[0]
                for pid, proj in self.projections.items():
                    cw, ch = _projection_panel_size(proj, top_row.shape[1])
                    self._uv_panel_bounds[pid] = (0, uv_y, cw, ch)
                    uv_y += ch
                edit_target = None
                if self.edit_region_id is not None and self._focused_region() is not None:
                    edit_target = (self.focus_idx, self.edit_region_id, self.edit_kind)
                uv = _render_uv_canvas(
                    cams, self.projections, fused_persons, self._person_trails,
                    edit_target, self.edit_zone, overlaps,
                    target_width=top_row.shape[1],
                )
                if uv is not None:
                    canvas = np.vstack([top_row, uv])
                else:
                    canvas = top_row
            else:
                self._uv_panel_bounds = {}
                canvas = top_row

            self._draw_draft(canvas)

            overlap_summary = ""
            if overlap_count > 0:
                first_proj = next(iter(overlaps))
                first_pair = overlaps[first_proj][0]
                overlap_summary = (
                    f"overlap[{first_proj}]: {first_pair}"
                    + (f" (+{overlap_count - 1} more)" if overlap_count > 1 else "")
                )
            self._draw_status(canvas, overlap_summary)
            _draw_dashboard_bar(canvas, cams, fused_persons, overlap_count, stats)

            # Resize the cv2 window to match the canvas's natural aspect on
            # first paint, so we don't squish the UV strip vertically.
            if not self._topmost_pumped:
                try:
                    cv2.resizeWindow(WINDOW_NAME, canvas.shape[1], canvas.shape[0])
                except cv2.error:
                    pass

            try:
                cv2.imshow(WINDOW_NAME, canvas)
            except cv2.error as ex:
                self._window_failed = True
                print(f"[viewer] imshow failed: {ex}", file=sys.stderr, flush=True)
                return True

        # Pump the event queue once after the first imshow so macOS actually
        # paints the window, then release the topmost flag so the operator
        # can stack other apps over the viewer if they choose.
        if not self._topmost_pumped:
            cv2.waitKey(1)
            try:
                cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_TOPMOST, 0)
            except cv2.error:
                pass
            self._topmost_pumped = True

        key = cv2.waitKey(1) & 0xFF
        if key == 27 and self.zone_draw_mode:
            self.zone_draw_mode = False
            self.draft_zone_points = []
            self.status = "zone draw cancelled"
            return True
        if key == 27 and self.draw_mode:
            self.draw_mode = False
            self.draft_points = []
            self.status = "draw cancelled"
            return True
        if key == 27 and self.edit_region_id is not None:
            self._exit_edit_mode()
            self.status = "exited slice edit mode"
            return True
        if key == 27 and self.edit_zone is not None:
            self._exit_zone_edit_mode()
            self.status = "exited zone edit mode"
            return True
        if key in (ord("q"), 27):
            return False
        if key == ord("h"):
            self.show_hud = not self.show_hud
        elif key == ord("u"):
            self.show_uv = not self.show_uv
        elif key == ord("p"):
            self.show_panel = not self.show_panel
        elif key == 9:
            cur = _PANEL_TABS.index(self.panel_tab)
            self.panel_tab = _PANEL_TABS[(cur + 1) % len(_PANEL_TABS)]
            self.show_panel = True
            self.status = f"panel tab: {self.panel_tab}"
        elif key == ord("d"):
            self.draw_mode = True
            self.draft_points = []
            self.zone_draw_mode = False
            self.draft_zone_points = []
            self._exit_edit_mode()
            self._exit_zone_edit_mode()
            self.status = "draw mode: click 4 focused-camera points"
        elif key == ord("z"):
            self.zone_draw_mode = True
            self.draft_zone_points = []
            self.draw_mode = False
            self.draft_points = []
            self._exit_edit_mode()
            self._exit_zone_edit_mode()
            self.status = "zone draw mode: click 2 corners on UV canvas"
        elif key in (8, 127):
            if self.draft_zone_points:
                self.draft_zone_points.pop()
                self.status = f"zone corner removed; {len(self.draft_zone_points)}/2 remain"
            elif self.draft_points:
                self.draft_points.pop()
                self.status = f"point removed; {len(self.draft_points)}/4 remain"
        elif key == ord("x"):
            if self.edit_zone is not None:
                self._delete_focused_zone()
            else:
                self._delete_last_region()
        elif key == ord("w"):
            if self.on_save is not None:
                self.on_save()
            self.dirty = False
            self.status = "config saved"
        elif key == ord("e"):
            self._exit_zone_edit_mode()
            self._toggle_edit_mode()
        elif key == ord("v"):
            self._exit_edit_mode()
            self._toggle_zone_edit_mode()
        elif key == ord("t") and self.edit_region_id is not None:
            self._toggle_edit_kind()
        elif key == ord("g") and self.edit_zone is not None:
            self._cycle_zone_edge()
        elif key == ord("g") and self.edit_region_id is not None:
            self._cycle_edit_edge()
        elif key == ord("r") and self.edit_region_id is not None:
            self._reset_edit_slice()
        elif key == ord("["):
            if self.edit_zone is not None:
                self._nudge_zone(-_NUDGE_FINE)
            elif self.edit_region_id is not None:
                self._nudge_slice(-_NUDGE_FINE)
            else:
                self._cycle_focused_region(-1)
        elif key == ord("]"):
            if self.edit_zone is not None:
                self._nudge_zone(+_NUDGE_FINE)
            elif self.edit_region_id is not None:
                self._nudge_slice(+_NUDGE_FINE)
            else:
                self._cycle_focused_region(+1)
        elif key == ord(",") and self.edit_zone is not None:
            self._nudge_zone(-_NUDGE_COARSE)
        elif key == ord(",") and self.edit_region_id is not None:
            self._nudge_slice(-_NUDGE_COARSE)
        elif key == ord(".") and self.edit_zone is not None:
            self._nudge_zone(+_NUDGE_COARSE)
        elif key == ord(".") and self.edit_region_id is not None:
            self._nudge_slice(+_NUDGE_COARSE)
        elif ord("1") <= key <= ord("9"):
            idx = key - ord("1")
            if idx < len(cams):
                self.focus_idx = idx
                self.focused_region_idx = -1
                self.draft_points = []
                self.draw_mode = False
                self.draft_zone_points = []
                self.zone_draw_mode = False
                self._exit_edit_mode()
                self._exit_zone_edit_mode()
                self.status = f"focused {cams[idx].name}"
        return True

    def _cycle_focused_region(self, step: int) -> None:
        if self.focus_idx >= len(self._last_cams):
            return
        cam = self._last_cams[self.focus_idx]
        n = len(cam.regions)
        if n == 0:
            self.focused_region_idx = -1
            self.status = f"{cam.name} has no regions"
            return
        # Cycle through n+1 slots (indices 0..n-1 plus -1 = 'no selection')
        # so operators can step through every region and then return to none.
        cur = self.focused_region_idx
        nxt = cur + step
        if nxt >= n:
            nxt = -1
        elif nxt < -1:
            nxt = n - 1
        self.focused_region_idx = nxt
        if nxt == -1:
            self.status = f"{cam.name} region: (none)"
        else:
            self.status = f"{cam.name} region: {cam.regions[nxt].id}"

    def close(self) -> None:
        if self._window_created:
            try:
                cv2.destroyWindow(WINDOW_NAME)
                # macOS keeps the window in the Cocoa run loop until the
                # event queue is pumped a couple of times after destroyWindow.
                for _ in range(2):
                    cv2.waitKey(1)
            except cv2.error:
                pass
            self._window_created = False
        sys.stdout.flush()
        sys.stderr.flush()


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
