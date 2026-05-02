"""Cross-camera person fusion in shared projection UV space.

`CamWorker` produces per-camera `(cam_name, track_id, u, v)` observations.
That ID is camera-local: a single person walking from one dispatch slice into
another shows up as `cam0:5` lost + `cam1:3` new, even though it is the same
person. `PersonTracker` stitches those events into stable global IDs (`gid`)
so downstream consumers (TouchDesigner, OSC subscribers) see one continuous
person stream.

Pure-Python: only stdlib. No cv2, no numpy, no project imports. The matching
logic is intentionally simple (UV distance + time window). v2 candidates:
appearance ReID, motion-direction priors, multi-hypothesis tracking.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PersonEvent:
    """One per-cam observation this frame, already projected to UV."""
    projection_id: str
    cam_name: str
    track_id: int
    u: float
    v: float
    conf: float
    t: float  # monotonic seconds


@dataclass
class Person:
    """Active fused person in shared UV space."""
    gid: int
    projection_id: str
    u: float
    v: float
    vx: float = 0.0
    vy: float = 0.0
    conf: float = 0.0
    last_t: float = 0.0
    source: tuple[str, int] = ("", -1)  # (cam_name, track_id) currently feeding this gid


@dataclass
class _Pending:
    """A gid whose feeding source just disappeared. Held for hand-off."""
    person: Person
    lost_t: float


class PersonTracker:
    """Stitches per-cam track events into stable global person IDs.

    Per `update()`:
      1. Active events whose `(cam, tid)` source matches an existing gid update
         that gid (position, velocity EMA, conf, timestamp).
      2. Active events with a new source try to revive a recently-lost gid in
         the *same projection* whose last UV is within `match_uv_radius` and
         whose loss is within `hand_off_window_s`. If matched, the new source
         takes over the gid.
      3. Otherwise a fresh gid is allocated.
      4. Sources that vanished this tick move from active → pending, keeping
         their last UV and lost timestamp for hand-off matching.
      5. Pending entries older than `hand_off_window_s` are evicted; their
         gids are reported in `newly_lost_gids` so the caller can emit a
         single terminal `/lost` message per gid.

    The matcher only stitches across cameras within the same `projection_id`.
    Cross-projection fusion is intentionally out of scope — the v1 contract is
    that every projection is a self-contained interaction surface.
    """

    def __init__(
        self,
        hand_off_window_s: float = 2.5,
        match_uv_radius: float = 0.05,
        velocity_alpha: float = 0.3,
        velocity_max_dt_s: float = 1.0,
        velocity_predict_max_dt_s: float = 1.0,
    ):
        self.hand_off_window_s = hand_off_window_s
        self.match_uv_radius = match_uv_radius
        self.velocity_alpha = velocity_alpha
        self.velocity_max_dt_s = velocity_max_dt_s
        # Cap how far we extrapolate the lost-position with the last (vx, vy)
        # when looking for a hand-off match. After this, the predicted point
        # freezes at the last seen UV — the longer the gap, the less we trust
        # the stored velocity, so we fall back to "match where we last saw it".
        self.velocity_predict_max_dt_s = velocity_predict_max_dt_s

        self._next_gid = 1
        self._persons: dict[int, Person] = {}
        self._source_to_gid: dict[tuple[str, int], int] = {}
        self._pending: dict[int, _Pending] = {}
        self._just_lost: list[int] = []  # drained by `drain_lost_gids()`

    def update(
        self,
        events: list[PersonEvent],
        lost_sources: list[tuple[str, int]],
        now: float,
    ) -> list[Person]:
        """Ingest one frame.

        `events` are this frame's active observations from all cameras —
        only tracks that fell inside `dispatch_uv` are emitted by the
        caller, so the size of `events` equals the broadcast count.
        `lost_sources` are `(cam_name, track_id)` tuples whose tracks
        truly disappeared (left projection_uv or YOLO dropped them) —
        not tracks that merely exited dispatch.

        Returns the list of persons that received a fresh event this
        frame ("broadcasting" persons). Sources that are still tracked
        but didn't produce an event this frame stay alive silently and
        keep their gid; they don't appear in the returned list and the
        caller therefore won't emit a `/person/<gid>` OSC for them.
        """
        # Step 1: any source that truly disappeared moves out of the active
        # set into pending. Pending persons are *not* returned because
        # their position is stale — they are kept around so the next
        # observation from another camera can revive the gid.
        for src in lost_sources:
            gid = self._source_to_gid.pop(src, None)
            if gid is None:
                continue
            person = self._persons.pop(gid, None)
            if person is None:
                continue
            self._pending[gid] = _Pending(person=person, lost_t=now)

        # Step 2: ingest active events. A single pending gid can only be
        # claimed once per frame; if multiple sources contend, the closer
        # one wins.
        fresh: set[int] = set()
        claimed_pending: set[int] = set()
        for ev in events:
            src = (ev.cam_name, ev.track_id)
            gid = self._source_to_gid.get(src)
            if gid is not None:
                self._update_person(gid, ev, now)
                fresh.add(gid)
                continue
            gid = self._best_pending_match(ev, claimed_pending)
            if gid is not None:
                claimed_pending.add(gid)
                pend = self._pending.pop(gid)
                self._persons[gid] = pend.person
                self._source_to_gid[src] = gid
                self._persons[gid].source = src
                self._update_person(gid, ev, now)
                fresh.add(gid)
                continue
            new_gid = self._spawn_person(src, ev, now)
            fresh.add(new_gid)

        # Step 3: evict pending entries that exceeded hand-off window. Each
        # eviction emits one terminal `/lost` for the caller via drain.
        evicted = [
            gid for gid, p in self._pending.items()
            if now - p.lost_t > self.hand_off_window_s
        ]
        for gid in evicted:
            self._pending.pop(gid, None)
            self._just_lost.append(gid)

        return [self._persons[gid] for gid in fresh if gid in self._persons]

    def drain_lost_gids(self) -> list[int]:
        """Return gids that transitioned to permanently-lost since the last
        call, then clear the buffer. Caller emits one `/lost` per gid."""
        out = self._just_lost
        self._just_lost = []
        return out

    def _best_pending_match(
        self,
        ev: PersonEvent,
        already_claimed: set[int],
    ) -> Optional[int]:
        # Match against a constant-velocity prediction of the lost position.
        # For a person standing still (vx, vy ≈ 0) this collapses to the last
        # UV — same as before. For a moving person, the candidate disc rides
        # along their direction of travel, so a Z-occluded walker that
        # reappears slightly ahead (or a tid-swap mid-stride) still matches
        # without a churn-prone larger radius.
        best_gid: Optional[int] = None
        best_dist = self.match_uv_radius
        for gid, p in self._pending.items():
            if gid in already_claimed:
                continue
            if p.person.projection_id != ev.projection_id:
                continue
            dt = max(0.0, min(ev.t - p.lost_t, self.velocity_predict_max_dt_s))
            pu = p.person.u + p.person.vx * dt
            pv = p.person.v + p.person.vy * dt
            du = ev.u - pu
            dv = ev.v - pv
            dist = (du * du + dv * dv) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best_gid = gid
        return best_gid

    def _spawn_person(
        self,
        src: tuple[str, int],
        ev: PersonEvent,
        now: float,
    ) -> int:
        gid = self._next_gid
        self._next_gid += 1
        self._persons[gid] = Person(
            gid=gid,
            projection_id=ev.projection_id,
            u=ev.u,
            v=ev.v,
            vx=0.0,
            vy=0.0,
            conf=ev.conf,
            last_t=now,
            source=src,
        )
        self._source_to_gid[src] = gid
        return gid

    def _update_person(self, gid: int, ev: PersonEvent, now: float) -> None:
        p = self._persons.get(gid)
        if p is None:
            return
        dt = now - p.last_t
        if 0.0 < dt <= self.velocity_max_dt_s:
            inst_vx = (ev.u - p.u) / dt
            inst_vy = (ev.v - p.v) / dt
            a = self.velocity_alpha
            p.vx = a * inst_vx + (1.0 - a) * p.vx
            p.vy = a * inst_vy + (1.0 - a) * p.vy
        else:
            # Long gap or first frame on this gid — reset velocity to avoid a
            # huge spike from the hand-off discontinuity.
            p.vx = 0.0
            p.vy = 0.0
        p.u = ev.u
        p.v = ev.v
        p.conf = ev.conf
        p.last_t = now


if __name__ == "__main__":
    import sys

    failed = 0

    def _check(name: str, ok: bool, detail: str = "") -> None:
        global failed
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            failed += 1

    # (a) Single-person traversal across cameras keeps one gid.
    pt = PersonTracker(hand_off_window_s=0.4, match_uv_radius=0.05)
    t = 0.0
    for u in (0.30, 0.35, 0.40, 0.45):
        pt.update(
            [PersonEvent("corridor", "cam0", 5, u, 0.5, 0.9, t)],
            [],
            t,
        )
        t += 0.05
    pt.update([], [("cam0", 5)], t)
    t += 0.10
    persons = pt.update(
        [PersonEvent("corridor", "cam1", 3, 0.47, 0.5, 0.85, t)],
        [],
        t,
    )
    gids = {p.gid for p in persons}
    _check(
        "(a) cam0->cam1 traversal keeps one gid",
        len(gids) == 1,
        f"gids={gids}",
    )

    # (b) Two simultaneous people get distinct gids; one of them stitches at boundary.
    pt = PersonTracker(hand_off_window_s=0.4, match_uv_radius=0.05)
    t = 0.0
    pt.update(
        [
            PersonEvent("corridor", "cam0", 5, 0.30, 0.5, 0.9, t),
            PersonEvent("corridor", "cam1", 9, 0.70, 0.5, 0.9, t),
        ],
        [],
        t,
    )
    t += 0.05
    pt.update(
        [
            PersonEvent("corridor", "cam0", 5, 0.45, 0.5, 0.9, t),
            PersonEvent("corridor", "cam1", 9, 0.65, 0.5, 0.9, t),
        ],
        [],
        t,
    )
    t += 0.05
    pt.update([], [("cam0", 5)], t)  # cam0 hands off
    t += 0.10
    persons = pt.update(
        [
            PersonEvent("corridor", "cam1", 4, 0.47, 0.5, 0.85, t),  # hand-off candidate
            PersonEvent("corridor", "cam1", 9, 0.60, 0.5, 0.9, t),
        ],
        [],
        t,
    )
    gid_for_orig_cam1 = pt._source_to_gid.get(("cam1", 9))
    gid_for_handoff = pt._source_to_gid.get(("cam1", 4))
    _check(
        "(b) two people: simultaneous tracks distinct, hand-off stitches",
        gid_for_orig_cam1 != gid_for_handoff
        and gid_for_handoff is not None
        and len({p.gid for p in persons}) == 2,
        f"orig={gid_for_orig_cam1}, handoff={gid_for_handoff}, persons={[p.gid for p in persons]}",
    )

    # (c) Lost source with no hand-off after window emits final lost.
    pt = PersonTracker(hand_off_window_s=0.3, match_uv_radius=0.05)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 7, 0.2, 0.5, 0.9, t)], [], t)
    t += 0.05
    pt.update([], [("cam0", 7)], t)
    t += 0.50  # past hand-off window
    pt.update([], [], t)
    lost = pt.drain_lost_gids()
    _check(
        "(c) hand-off window expires -> /lost emitted",
        len(lost) == 1,
        f"lost={lost}",
    )

    # (d) Drain is idempotent (second call returns empty).
    second = pt.drain_lost_gids()
    _check("(d) drain_lost_gids is idempotent", second == [])

    # (e) Cross-projection events do not stitch.
    pt = PersonTracker(hand_off_window_s=0.4, match_uv_radius=0.05)
    pt.update([PersonEvent("corridor", "cam0", 1, 0.3, 0.5, 0.9, 0.0)], [], 0.0)
    pt.update([], [("cam0", 1)], 0.05)
    persons = pt.update(
        [PersonEvent("lobby", "camL", 2, 0.31, 0.5, 0.9, 0.10)],
        [],
        0.10,
    )
    _check(
        "(e) cross-projection match is rejected",
        len({p.gid for p in persons}) == 1
        and persons[0].projection_id == "lobby",
        f"persons={[(p.gid, p.projection_id) for p in persons]}",
    )

    # (f) Dispatch boundary jitter: a track that exits dispatch but is still
    # tracked produces no lost_source from CamWorker, so the same gid is
    # kept across silent gaps without spawning a new one.
    pt = PersonTracker(hand_off_window_s=1.0, match_uv_radius=0.05)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 5, 0.49, 0.5, 0.9, t)], [], t)
    t += 0.05
    # Frame 1: out of dispatch, no event, no lost (caller knows track still alive).
    silent_active = pt.update([], [], t)
    t += 0.05
    # Frame 2: back in dispatch, same source.
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 5, 0.49, 0.5, 0.9, t)],
        [],
        t,
    )
    _check(
        "(f) boundary jitter keeps same gid; silent gap empties active list",
        len(silent_active) == 0
        and pt._next_gid == 2
        and {p.gid for p in persons} == {1},
        f"silent={silent_active}, next_gid={pt._next_gid}, "
        f"persons={[p.gid for p in persons]}",
    )

    # (g) Same camera ID swap stitches via pending: track 5 ends, track 8
    # appears at the same spot within window — gid is preserved.
    pt = PersonTracker(hand_off_window_s=1.0, match_uv_radius=0.05)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 5, 0.30, 0.5, 0.9, t)], [], t)
    t += 0.05
    pt.update([], [("cam0", 5)], t)  # YOLO drops tid=5
    t += 0.10
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 8, 0.31, 0.5, 0.85, t)],
        [],
        t,
    )
    _check(
        "(g) same-camera ID swap inherits gid",
        {p.gid for p in persons} == {1} and pt._next_gid == 2,
        f"persons={[p.gid for p in persons]}, next_gid={pt._next_gid}",
    )

    # (h) Short detection drop swallowed by CamWorker's miss buffer: caller
    # never emits lost_source, so the gid stays alive and no /lost fires
    # when the same tid returns a few frames later.
    pt = PersonTracker(hand_off_window_s=2.5, match_uv_radius=0.05)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 5, 0.30, 0.5, 0.9, t)], [], t)
    for _ in range(5):
        t += 0.05
        pt.update([], [], t)  # silent — under miss_buffer threshold
    t += 0.05
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 5, 0.31, 0.5, 0.9, t)],
        [],
        t,
    )
    lost = pt.drain_lost_gids()
    _check(
        "(h) short silent drop keeps gid; no /lost",
        {p.gid for p in persons} == {1} and lost == [] and pt._next_gid == 2,
        f"persons={[p.gid for p in persons]}, lost={lost}, next_gid={pt._next_gid}",
    )

    # (i) Longer drop: CamWorker's miss buffer expires and fires lost_source,
    # but a new tid appearing within the (now larger) hand-off window at the
    # same UV stitches back into the original gid.
    pt = PersonTracker(hand_off_window_s=2.5, match_uv_radius=0.05)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 5, 0.30, 0.5, 0.9, t)], [], t)
    # Simulate ~8-frame silent stretch before CamWorker emits the loss.
    for _ in range(8):
        t += 0.05
        pt.update([], [], t)
    pt.update([], [("cam0", 5)], t)
    # Stay quiet for another ~1.5 s (well past the legacy 1.0 s window) and
    # then a new tid appears at the same place.
    t += 1.50
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 9, 0.31, 0.5, 0.85, t)],
        [],
        t,
    )
    lost = pt.drain_lost_gids()
    _check(
        "(i) long drop -> hand-off window stitches new tid",
        {p.gid for p in persons} == {1} and lost == [] and pt._next_gid == 2,
        f"persons={[p.gid for p in persons]}, lost={lost}, next_gid={pt._next_gid}",
    )

    # (j) Static occlusion regression: a person standing still (vx, vy ≈ 0)
    # vanishes for 1.5 s and reappears within match_uv_radius — velocity prior
    # must not push the prediction off the last seen UV, so the gid is kept.
    pt = PersonTracker(hand_off_window_s=2.5, match_uv_radius=0.05)
    t = 0.0
    # Two stationary frames so velocity EMA settles to ~0.
    pt.update([PersonEvent("corridor", "cam0", 5, 0.50, 0.50, 0.9, t)], [], t)
    t += 0.05
    pt.update([PersonEvent("corridor", "cam0", 5, 0.50, 0.50, 0.9, t)], [], t)
    t += 0.05
    pt.update([], [("cam0", 5)], t)
    t += 1.50  # well inside hand_off_window_s
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 9, 0.54, 0.50, 0.85, t)],  # +0.04 < 0.05
        [],
        t,
    )
    lost = pt.drain_lost_gids()
    _check(
        "(j) static occlusion: still person reappears nearby -> same gid",
        {p.gid for p in persons} == {1} and lost == [] and pt._next_gid == 2,
        f"persons={[p.gid for p in persons]}, lost={lost}, next_gid={pt._next_gid}",
    )

    # (k) Moving occlusion: a person walking at vx=0.3/s gets Z-occluded for
    # 1.0 s and reappears at the predicted forward position (~+0.30 in u).
    # Pure last-UV matching would miss this because 0.30 ≫ match_uv_radius;
    # velocity prior pulls the candidate disc with them so gid is preserved.
    # Ten frames so the EMA (alpha=0.3, starting from 0) converges close to
    # the true 0.3 — after 9 EMA updates vx ≈ 0.288, leaving ~0.012 residual
    # vs. the 0.30 advance over 1 s, well inside the 0.05 match radius.
    pt = PersonTracker(hand_off_window_s=2.5, match_uv_radius=0.05)
    t = 0.0
    u = 0.20
    for _ in range(10):
        pt.update([PersonEvent("corridor", "cam0", 5, u, 0.50, 0.9, t)], [], t)
        t += 0.05
        u += 0.015
    pt.update([], [("cam0", 5)], t)
    t += 1.0  # 1 s of occlusion -> predicted advance = ~0.288, true = 0.30
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 9, u + 0.30, 0.50, 0.85, t)],
        [],
        t,
    )
    lost = pt.drain_lost_gids()
    _check(
        "(k) moving occlusion: predicted-position match keeps gid",
        {p.gid for p in persons} == {1} and lost == [] and pt._next_gid == 2,
        f"persons={[p.gid for p in persons]}, lost={lost}, next_gid={pt._next_gid}",
    )

    # (l) Prediction guard: after velocity_predict_max_dt_s the predicted
    # position freezes at the last UV, so a moving person who reappears far
    # ahead after a long gap does NOT erroneously claim the old gid.
    pt = PersonTracker(
        hand_off_window_s=2.5,
        match_uv_radius=0.05,
        velocity_predict_max_dt_s=1.0,
    )
    t = 0.0
    u = 0.20
    for _ in range(5):
        pt.update([PersonEvent("corridor", "cam0", 5, u, 0.50, 0.9, t)], [], t)
        t += 0.05
        u += 0.015
    pt.update([], [("cam0", 5)], t)
    t += 2.0  # 2 s gap, well past predict cap; 0.3*2=0.60 would over-extrapolate
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 9, u + 0.60, 0.50, 0.85, t)],
        [],
        t,
    )
    _check(
        "(l) prediction guard prevents stale-velocity over-extrapolation",
        {p.gid for p in persons} == {2} and pt._next_gid == 3,
        f"persons={[p.gid for p in persons]}, next_gid={pt._next_gid}",
    )

    sys.exit(1 if failed else 0)
