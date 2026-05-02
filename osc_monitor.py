"""Live OSC inspector for tracker.py.

Subscribes to the person-keyed schema (`/proj/<pid>/persons`,
`/person/<gid>/lost`) and prints a compact rolling view so you can eyeball
gid stability during a live test without TouchDesigner.

Run in a second terminal alongside `python tracker.py`:

    python osc_monitor.py                # listens on 127.0.0.1:7000
    python osc_monitor.py --port 7000

Pass-criterion at a glance: while one person stands in front of the camera,
`active=[N]` and `total_spawned=1` should stay flat for the duration. If
`total_spawned` ticks up, fusion is churning gids on detection drops.
"""

import argparse
import re
import time
from collections import defaultdict
from threading import Thread

from pythonosc import dispatcher, osc_server


PERSONS_RE = re.compile(r"^/proj/([^/]+)/persons$")
LOST_RE = re.compile(r"^/proj/([^/]+)/person/(\d+)/lost$")


class State:
    def __init__(self):
        self.last_active: dict[str, list[int]] = {}
        self.ever_seen: dict[str, set[int]] = defaultdict(set)
        self.lost_total: dict[str, int] = defaultdict(int)
        self.start = time.monotonic()


state = State()


def _ts() -> str:
    return f"t={time.monotonic() - state.start:6.1f}"


def _on_persons(addr: str, *args) -> None:
    m = PERSONS_RE.match(addr)
    if not m:
        return
    pid = m.group(1)
    gids = sorted(int(g) for g in args)
    new = [g for g in gids if g not in state.ever_seen[pid]]
    for g in new:
        state.ever_seen[pid].add(g)
        print(f"[{_ts()}] proj={pid}  NEW gid={g}")
    if gids != state.last_active.get(pid):
        state.last_active[pid] = gids
        print(
            f"[{_ts()}] proj={pid}  active={gids}  count={len(gids)}  "
            f"total_spawned={len(state.ever_seen[pid])}  "
            f"lost_total={state.lost_total[pid]}"
        )


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
            print(f"[{_ts()}] (no /persons received yet)")
            continue
        for pid, gids in state.last_active.items():
            print(
                f"[{_ts()}] heartbeat proj={pid}  active={gids}  "
                f"count={len(gids)}  total_spawned={len(state.ever_seen[pid])}  "
                f"lost_total={state.lost_total[pid]}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7000)
    args = ap.parse_args()

    disp = dispatcher.Dispatcher()
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
