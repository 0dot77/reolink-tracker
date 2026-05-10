"""Live OSC inspector for tracker.py.

Subscribes to the default TouchDesigner-minimal schema (`/proj/<pid>/active`,
`/xy`, `/uv`, `/persons/count`) plus the person-keyed debug schema
(`/persons`, `/person/<gid>/lost`) and prints a compact rolling view so you can
eyeball gid stability during a live test without TouchDesigner.

Run in a second terminal alongside `python tracker.py`:

    python osc_monitor.py                # listens on 127.0.0.1:7000
    python osc_monitor.py --port 7000

Pass-criterion at a glance: while one person stands in front of the camera,
`active=[N]`, `count=1`, `xy_triples=1`, and `uv_triples=1` should stay flat
for the duration. If `total_spawned` ticks up, fusion is churning gids on
detection drops.
"""

import argparse
import re
import time
from collections import defaultdict
from threading import Thread

from pythonosc import dispatcher, osc_server


ACTIVE_RE = re.compile(r"^/proj/([^/]+)/active$")
XY_RE = re.compile(r"^/proj/([^/]+)/xy$")
UV_RE = re.compile(r"^/proj/([^/]+)/uv$")
COUNT_RE = re.compile(r"^/proj/([^/]+)/persons/count$")
PERSONS_RE = re.compile(r"^/proj/([^/]+)/persons$")
LOST_RE = re.compile(r"^/proj/([^/]+)/person/(\d+)/lost$")


class State:
    def __init__(self):
        self.last_active: dict[str, list[int]] = {}
        self.last_count: dict[str, int] = {}
        self.last_xy_triples: dict[str, int] = {}
        self.last_uv_triples: dict[str, int] = {}
        self.ever_seen: dict[str, set[int]] = defaultdict(set)
        self.lost_total: dict[str, int] = defaultdict(int)
        self.start = time.monotonic()


state = State()


def _ts() -> str:
    return f"t={time.monotonic() - state.start:6.1f}"


def _record_active(pid: str, gids: list[int], source: str) -> None:
    gids = sorted(gids)
    new = [g for g in gids if g not in state.ever_seen[pid]]
    for g in new:
        state.ever_seen[pid].add(g)
        print(f"[{_ts()}] proj={pid}  NEW gid={g}")
    if gids != state.last_active.get(pid):
        state.last_active[pid] = gids
        count = state.last_count.get(pid)
        xy_triples = state.last_xy_triples.get(pid)
        mismatch = ""
        if count is not None and count != len(gids):
            mismatch += f"  COUNT_MISMATCH count={count}"
        if xy_triples is not None and xy_triples != len(gids):
            mismatch += f"  XY_MISMATCH xy_triples={xy_triples}"
        uv_triples = state.last_uv_triples.get(pid)
        if uv_triples is not None and uv_triples != len(gids):
            mismatch += f"  UV_MISMATCH uv_triples={uv_triples}"
        print(
            f"[{_ts()}] proj={pid}  {source} active={gids}  count={len(gids)}  "
            f"total_spawned={len(state.ever_seen[pid])}  "
            f"lost_total={state.lost_total[pid]}{mismatch}"
        )


def _on_active(addr: str, *args) -> None:
    m = ACTIVE_RE.match(addr)
    if not m:
        return
    pid = m.group(1)
    gids = [int(g) for g in args]
    _record_active(pid, gids, "/active")


def _on_persons(addr: str, *args) -> None:
    m = PERSONS_RE.match(addr)
    if not m:
        return
    pid = m.group(1)
    gids = [int(g) for g in args]
    _record_active(pid, gids, "/persons")


def _on_xy(addr: str, *args) -> None:
    m = XY_RE.match(addr)
    if not m:
        return
    pid = m.group(1)
    triples = len(args) // 3
    malformed = len(args) % 3 != 0
    if triples == state.last_xy_triples.get(pid) and not malformed:
        return
    state.last_xy_triples[pid] = triples
    active = state.last_active.get(pid)
    mismatch = ""
    if active is not None and triples != len(active):
        mismatch = f"  XY_MISMATCH active={len(active)}"
    if malformed:
        mismatch += f"  MALFORMED values={len(args)}"
    print(f"[{_ts()}] proj={pid}  /xy triples={triples}{mismatch}")


def _on_uv(addr: str, *args) -> None:
    m = UV_RE.match(addr)
    if not m:
        return
    pid = m.group(1)
    triples = len(args) // 3
    malformed = len(args) % 3 != 0
    if triples == state.last_uv_triples.get(pid) and not malformed:
        return
    state.last_uv_triples[pid] = triples
    active = state.last_active.get(pid)
    mismatch = ""
    if active is not None and triples != len(active):
        mismatch = f"  UV_MISMATCH active={len(active)}"
    if malformed:
        mismatch += f"  MALFORMED values={len(args)}"
    print(f"[{_ts()}] proj={pid}  /uv triples={triples}{mismatch}")


def _on_count(addr: str, *args) -> None:
    m = COUNT_RE.match(addr)
    if not m or not args:
        return
    pid = m.group(1)
    count = int(args[0])
    if count == state.last_count.get(pid):
        return
    state.last_count[pid] = count
    active = state.last_active.get(pid)
    mismatch = ""
    if active is not None and count != len(active):
        mismatch = f"  COUNT_MISMATCH active={len(active)}"
    print(f"[{_ts()}] proj={pid}  /persons/count={count}{mismatch}")


def _on_lost(addr: str, *args) -> None:
    m = LOST_RE.match(addr)
    if not m:
        return
    pid, gid = m.group(1), int(m.group(2))
    state.lost_total[pid] += 1
    print(f"[{_ts()}] proj={pid}  LOST gid={gid}")


def _heartbeat() -> None:
    """Periodic summary so a long quiet stretch still shows the active state."""
    while True:
        time.sleep(5.0)
        if not state.last_active:
            print(f"[{_ts()}] (no /active or /persons received yet)")
            continue
        for pid, gids in state.last_active.items():
            count = state.last_count.get(pid, len(gids))
            xy_triples = state.last_xy_triples.get(pid, 0)
            uv_triples = state.last_uv_triples.get(pid, 0)
            print(
                f"[{_ts()}] heartbeat proj={pid}  active={gids}  "
                f"count={count}  xy_triples={xy_triples}  uv_triples={uv_triples}  "
                f"total_spawned={len(state.ever_seen[pid])}  "
                f"lost_total={state.lost_total[pid]}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7000)
    args = ap.parse_args()

    disp = dispatcher.Dispatcher()
    disp.map("/proj/*/active", _on_active)
    disp.map("/proj/*/xy", _on_xy)
    disp.map("/proj/*/uv", _on_uv)
    disp.map("/proj/*/persons/count", _on_count)
    disp.map("/proj/*/person/*/lost", _on_lost)
    disp.map("/proj/*/persons", _on_persons)

    Thread(target=_heartbeat, daemon=True).start()

    server = osc_server.ThreadingOSCUDPServer((args.host, args.port), disp)
    print(f"listening on {args.host}:{args.port}  (Ctrl-C to quit)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
