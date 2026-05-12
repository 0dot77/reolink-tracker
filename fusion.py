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

import heapq
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
    dispatching: bool = True
    relaxed: bool = False
    source_zone: str = "floor"
    # Lower is better. Tracker sets this to distance from the event UV to the
    # center of the camera's dispatch slice so simultaneous cross-camera
    # duplicates can keep the source that is deeper inside its ownership band.
    dispatch_center_distance: float = 0.0
    # Auxiliary observations can confirm that an existing primary gid is still
    # near the hand-off area, but they never spawn or take ownership of a gid.
    auxiliary: bool = False


@dataclass
class Person:
    """Active fused person in shared UV space."""
    gid: int
    projection_id: str
    u: float
    v: float
    raw_u: float = 0.0
    raw_v: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    conf: float = 0.0
    last_t: float = 0.0
    last_seen_t: float = 0.0
    state: str = "fresh"  # "fresh" when coordinates updated this tick, else "held"
    source: tuple[str, int] = ("", -1)  # (cam_name, track_id) currently feeding this gid
    relaxed: bool = False
    source_zone: str = "floor"
    dispatch_center_distance: float = 0.0


@dataclass
class _Pending:
    """A gid whose feeding source just disappeared. Held for hand-off."""
    person: Person
    lost_t: float
    hold_s: float


@dataclass(frozen=True)
class LostPerson:
    """Projection-aware terminal event for a fused gid."""

    projection_id: str
    gid: int


@dataclass(frozen=True)
class ZonePerson:
    """One person sample inside an interaction zone."""

    projection_id: str
    zone_id: str
    gid: int
    u: float
    v: float
    zone_u: float
    zone_v: float
    vx: float
    vy: float
    dwell_s: float
    presence: float
    state_code: int


@dataclass(frozen=True)
class ZoneTransition:
    """One enter/leave transition for an interaction zone."""

    kind: str
    projection_id: str
    zone_id: str
    gid: int
    zone_u: float = 0.0
    zone_v: float = 0.0
    reason_code: int = 0
    dwell_s: float = 0.0


@dataclass
class ZoneUpdate:
    persons: list[ZonePerson] = field(default_factory=list)
    transitions: list[ZoneTransition] = field(default_factory=list)
    counts: dict[tuple[str, str], int] = field(default_factory=dict)


@dataclass
class _ZoneState:
    entered_t: float
    last_fresh_t: float
    last_t: float
    u: float
    v: float
    zone_u: float
    zone_v: float
    vx: float
    vy: float


class InteractionZoneTracker:
    """Tracks fused persons through projection-local rectangular zones."""

    REASON_EXITED = 1
    REASON_STALE = 2
    REASON_REMOVED = 3

    def __init__(self, fresh_grace_s: float = 0.15):
        self.fresh_grace_s = max(0.0, float(fresh_grace_s))
        self._states: dict[tuple[str, str, int], _ZoneState] = {}

    def update(self, zones: list, persons: list[Person], now: float) -> ZoneUpdate:
        zone_by_key = {(z.projection_id, z.id): z for z in zones}
        counts = {(z.projection_id, z.id): 0 for z in zones}
        persons_by_proj_gid = {(p.projection_id, p.gid): p for p in persons}
        out = ZoneUpdate(counts=counts)
        touched: set[tuple[str, str, int]] = set()

        for person in persons:
            for zone in zones:
                if zone.projection_id != person.projection_id:
                    continue
                if not _is_inside_rect((person.u, person.v), zone.uv_rect):
                    continue
                key = (zone.projection_id, zone.id, person.gid)
                zone_u, zone_v = _zone_local((person.u, person.v), zone.uv_rect)
                state = self._states.get(key)
                if state is None:
                    state = _ZoneState(
                        entered_t=now,
                        last_fresh_t=now,
                        last_t=now,
                        u=person.u,
                        v=person.v,
                        zone_u=zone_u,
                        zone_v=zone_v,
                        vx=person.vx,
                        vy=person.vy,
                    )
                    self._states[key] = state
                    out.transitions.append(
                        ZoneTransition(
                            kind="enter",
                            projection_id=zone.projection_id,
                            zone_id=zone.id,
                            gid=person.gid,
                            zone_u=zone_u,
                            zone_v=zone_v,
                        )
                    )

                is_fresh = (
                    person.state == "fresh"
                    and now - person.last_seen_t <= self.fresh_grace_s
                )
                if is_fresh:
                    state.last_fresh_t = now
                    state_code = 1
                    presence = 1.0
                else:
                    state_code = 0
                    presence = _held_presence(
                        now - state.last_fresh_t,
                        float(zone.release_after_s),
                    )
                    if presence <= 0.0:
                        out.transitions.append(
                            ZoneTransition(
                                kind="leave",
                                projection_id=zone.projection_id,
                                zone_id=zone.id,
                                gid=person.gid,
                                reason_code=self.REASON_STALE,
                                dwell_s=max(0.0, now - state.entered_t),
                            )
                        )
                        self._states.pop(key, None)
                        continue

                state.last_t = now
                state.u = person.u
                state.v = person.v
                state.zone_u = zone_u
                state.zone_v = zone_v
                state.vx = person.vx
                state.vy = person.vy
                touched.add(key)
                counts[(zone.projection_id, zone.id)] += 1
                out.persons.append(
                    ZonePerson(
                        projection_id=zone.projection_id,
                        zone_id=zone.id,
                        gid=person.gid,
                        u=person.u,
                        v=person.v,
                        zone_u=zone_u,
                        zone_v=zone_v,
                        vx=person.vx,
                        vy=person.vy,
                        dwell_s=max(0.0, now - state.entered_t),
                        presence=presence,
                        state_code=state_code,
                    )
                )

        for key, state in list(self._states.items()):
            if key in touched:
                continue
            pid, zid, gid = key
            zone = zone_by_key.get((pid, zid))
            if zone is None:
                reason = self.REASON_REMOVED
            else:
                person = persons_by_proj_gid.get((pid, gid))
                if person is not None and not _is_inside_rect((person.u, person.v), zone.uv_rect):
                    reason = self.REASON_EXITED
                elif now - state.last_fresh_t > float(zone.release_after_s):
                    reason = self.REASON_STALE
                else:
                    presence = _held_presence(now - state.last_fresh_t, float(zone.release_after_s))
                    if presence > 0.0:
                        counts[(pid, zid)] = counts.get((pid, zid), 0) + 1
                        out.persons.append(
                            ZonePerson(
                                projection_id=pid,
                                zone_id=zid,
                                gid=gid,
                                u=state.u,
                                v=state.v,
                                zone_u=state.zone_u,
                                zone_v=state.zone_v,
                                vx=state.vx,
                                vy=state.vy,
                                dwell_s=max(0.0, now - state.entered_t),
                                presence=presence,
                                state_code=0,
                            )
                        )
                    continue
            out.transitions.append(
                ZoneTransition(
                    kind="leave",
                    projection_id=pid,
                    zone_id=zid,
                    gid=gid,
                    reason_code=reason,
                    dwell_s=max(0.0, now - state.entered_t),
                )
            )
            self._states.pop(key, None)

        out.counts = counts
        return out


def _is_inside_rect(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    u, v = uv
    u0, v0, u1, v1 = rect
    return u0 <= u <= u1 and v0 <= v <= v1


def _zone_local(
    uv: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> tuple[float, float]:
    u, v = uv
    u0, v0, u1, v1 = rect
    return ((u - u0) / (u1 - u0), (v - v0) / (v1 - v0))


def _held_presence(held_s: float, release_after_s: float) -> float:
    if release_after_s <= 0.0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - held_s / release_after_s))


def _event_source_zone(ev: PersonEvent) -> str:
    """Normalize detector-source zones for downstream lane mapping."""
    if ev.relaxed:
        return "stair_relaxed"
    if ev.source_zone == "body_catch":
        return "body_catch"
    return "floor"


def _source_zone_priority(ev: PersonEvent) -> int:
    zone = _event_source_zone(ev)
    return _source_zone_name_priority(zone)


def _source_zone_name_priority(zone: str) -> int:
    if zone == "stair_relaxed":
        return 2
    if zone == "body_catch":
        return 1
    return 0


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
        position_alpha: float = 1.0,
        velocity_max_dt_s: float = 1.0,
        velocity_predict_max_dt_s: float = 1.0,
        hold_boundary_margin_uv: float = 0.0,
        overlap_duplicate_radius_uv: float = 0.04,
        max_update_jump_uv: float = 0.0,
        relaxed_hold_s: float = 0.0,
        reuse_lost_gids: bool = True,
        aux_match_uv_radius: float = 0.08,
        aux_match_time_window_s: float = 0.5,
        aux_position_alpha: float = 0.25,
    ):
        self.hand_off_window_s = hand_off_window_s
        self.match_uv_radius = match_uv_radius
        self.velocity_alpha = velocity_alpha
        self.position_alpha = max(0.0, min(float(position_alpha), 1.0))
        self.velocity_max_dt_s = velocity_max_dt_s
        # Cap how far we extrapolate the lost-position with the last (vx, vy)
        # when looking for a hand-off match. After this, the predicted point
        # freezes at the last seen UV — the longer the gap, the less we trust
        # the stored velocity, so we fall back to "match where we last saw it".
        self.velocity_predict_max_dt_s = velocity_predict_max_dt_s
        # When > 0, disappeared sources are only exposed as held persons near
        # the projection edge. Interior misses become immediate lost events so
        # downstream visuals do not show a ghost in the middle of the floor.
        self.hold_boundary_margin_uv = max(0.0, min(float(hold_boundary_margin_uv), 0.5))
        # Fresh duplicate guard. General active matching intentionally excludes
        # fresh persons to avoid merging two nearby walkers, but two cameras can
        # report the same boundary walker as fresh in the same tick. This narrow
        # radius suppresses only same-projection, cross-camera dispatch duplicates.
        self.overlap_duplicate_radius_uv = max(0.0, float(overlap_duplicate_radius_uv))
        # Optional teleport guard. When enabled, a fresh observation that would
        # move an existing gid farther than this UV distance is treated as a
        # new person instead of dragging the OSC actor across the floor.
        self.max_update_jump_uv = max(0.0, float(max_update_jump_uv))
        self.relaxed_hold_s = max(0.0, float(relaxed_hold_s))
        self.aux_match_uv_radius = max(0.0, float(aux_match_uv_radius))
        self.aux_match_time_window_s = max(0.0, float(aux_match_time_window_s))
        self.aux_position_alpha = max(0.0, min(float(aux_position_alpha), 1.0))
        # Reuse gids only after they are terminally lost. This keeps OSC
        # address/key cardinality bounded by peak concurrent occupancy instead
        # of total historical visitors.
        self.reuse_lost_gids = bool(reuse_lost_gids)

        self._next_gid = 1
        self._free_gids: list[int] = []
        self._free_gid_set: set[int] = set()
        self._spawned_total = 0
        self._persons: dict[int, Person] = {}
        self._source_to_gid: dict[tuple[str, int], int] = {}
        self._pending: dict[int, _Pending] = {}
        self._just_lost: list[LostPerson] = []  # drained by `drain_lost_gids()`
        self._release_after_update: list[int] = []
        self.handoff_count = 0
        self.lost_count = 0
        self.teleport_reject_count = 0
        self.duplicate_suppressed_count = 0

    def update(
        self,
        events: list[PersonEvent],
        lost_sources: list[tuple[str, int]],
        now: float,
    ) -> list[Person]:
        """Ingest one frame.

        Dispatching events update coordinates and OSC payloads. Projection-only
        events keep an already-known source alive while it is outside dispatch,
        which prevents `/persons` from flickering during A/B hand-off overlap.

        Returns all active persons: fresh coordinate updates plus held persons
        whose last coordinates should remain active for interaction slots.
        """
        events = self._dedupe_events(events)
        aux_events = [ev for ev in events if ev.auxiliary]
        events = [ev for ev in events if not ev.auxiliary]
        for person in self._persons.values():
            person.state = "held"

        # Sources that truly disappeared move to pending only when the hold
        # policy allows it. Edge-gated interior misses emit lost immediately.
        for src in lost_sources:
            gid = self._source_to_gid.pop(src, None)
            if gid is None:
                continue
            person = self._persons.pop(gid, None)
            if person is None:
                continue
            person.state = "held"
            aux_ev = self._best_aux_match(person, aux_events, now)
            if not self._allows_held(person):
                if aux_ev is None:
                    self._just_lost.append(LostPerson(person.projection_id, gid))
                    self._release_gid(gid)
                    self.lost_count += 1
                    continue
                self._apply_aux_sighting(person, aux_ev, now)
                self._pending[gid] = _Pending(
                    person=person,
                    lost_t=now,
                    hold_s=self._aux_pending_hold_s(person),
                )
                continue
            if aux_ev is not None:
                self._apply_aux_sighting(person, aux_ev, now)
            self._pending[gid] = _Pending(
                person=person,
                lost_t=now,
                hold_s=self._pending_hold_s(person),
            )

        fresh: set[int] = set()
        new_events: list[PersonEvent] = []

        # First update known sources. This makes active hand-off matching
        # independent of camera iteration order within a frame.
        for ev in events:
            src = (ev.cam_name, ev.track_id)
            gid = self._source_to_gid.get(src)
            if gid is None:
                new_events.append(ev)
                continue
            if self._can_update_person(gid, ev):
                if ev.dispatching:
                    self._update_person(gid, ev, now)
                    fresh.add(gid)
                else:
                    self._update_held_person(gid, ev, now)
            elif ev.dispatching:
                self._retire_person(gid)
                new_events.append(ev)
            else:
                self._hold_person(gid, ev, now)

        # New projection-only sources may claim an existing gid in overlap
        # zones, but only dispatching sources may spawn a new gid. This lets the
        # best camera feed an existing actor without letting overlap create
        # duplicate actors.
        claimed_pending: set[int] = set()
        claimed_active: set[int] = set()
        for ev in new_events:
            src = (ev.cam_name, ev.track_id)
            duplicate_gid = self._fresh_duplicate_match(ev)
            if duplicate_gid is not None:
                self._suppress_fresh_duplicate(duplicate_gid, src, ev, now)
                continue
            gid = self._best_active_match(ev, fresh | claimed_active)
            if gid is not None:
                old_src = self._persons[gid].source
                self._source_to_gid.pop(old_src, None)
                self._source_to_gid[src] = gid
                self._persons[gid].source = src
                self._persons[gid].relaxed = self._persons[gid].relaxed or ev.relaxed
                if ev.dispatching:
                    self._update_person(gid, ev, now)
                    fresh.add(gid)
                else:
                    self._update_held_person(gid, ev, now)
                self.handoff_count += 1
                claimed_active.add(gid)
                continue
            gid = self._best_pending_match(ev, claimed_pending)
            if gid is not None:
                claimed_pending.add(gid)
                pend = self._pending.pop(gid)
                self._persons[gid] = pend.person
                self._source_to_gid[src] = gid
                self._persons[gid].source = src
                self._persons[gid].relaxed = self._persons[gid].relaxed or ev.relaxed
                if ev.dispatching:
                    self._update_person(gid, ev, now)
                    fresh.add(gid)
                else:
                    self._update_held_person(gid, ev, now)
                self.handoff_count += 1
                continue
            if not ev.dispatching:
                continue
            new_gid = self._spawn_person(src, ev, now)
            fresh.add(new_gid)

        self._refresh_pending_with_aux(aux_events, now)

        evicted = [
            gid for gid, p in self._pending.items()
            if now - p.lost_t > p.hold_s
        ]
        for gid in evicted:
            pending = self._pending.pop(gid, None)
            if pending is not None:
                self._just_lost.append(
                    LostPerson(pending.person.projection_id, gid)
                )
                self._release_gid(gid)
            self.lost_count += 1

        self._release_deferred_gids()

        active = list(self._persons.values())
        active.extend(p.person for p in self._pending.values())
        return sorted(active, key=lambda p: p.gid)

    @property
    def spawned_count(self) -> int:
        return self._spawned_total

    def _dedupe_events(self, events: list[PersonEvent]) -> list[PersonEvent]:
        """Keep one observation per camera-local source per frame.

        Region overlap during calibration can produce multiple events for the
        same `(cam, track_id)`. Fusion identity is source-keyed, so processing
        duplicates as independent new sources can spawn duplicate gids. Prefer
        dispatching observations over projection-only observations, then
        source-zone specificity, then confidence.
        """
        by_src: dict[tuple[str, int], PersonEvent] = {}
        for ev in events:
            src = (ev.cam_name, ev.track_id)
            cur = by_src.get(src)
            if cur is None:
                by_src[src] = ev
                continue
            if ev.dispatching and not cur.dispatching:
                by_src[src] = ev
                continue
            if ev.dispatching == cur.dispatching:
                ev_priority = _source_zone_priority(ev)
                cur_priority = _source_zone_priority(cur)
                if ev_priority > cur_priority or (
                    ev_priority == cur_priority and ev.conf >= cur.conf
                ):
                    by_src[src] = ev
        return list(by_src.values())

    def drain_lost_gids(self) -> list[LostPerson]:
        """Return projection-aware gids that permanently expired since the last
        call, then clear the buffer. Caller emits one `/lost` per record."""
        out = self._just_lost
        self._just_lost = []
        return out

    def _allows_held(self, person: Person) -> bool:
        margin = self.hold_boundary_margin_uv
        if margin <= 0.0:
            return True
        if (
            person.u <= margin
            or person.u >= 1.0 - margin
            or person.v <= margin
            or person.v >= 1.0 - margin
        ):
            return True
        return False

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
            if not self._allows_update_distance(p.person, ev):
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

    def _best_active_match(
        self,
        ev: PersonEvent,
        already_claimed: set[int],
    ) -> Optional[int]:
        best_gid: Optional[int] = None
        best_dist = self.match_uv_radius
        for gid, p in self._persons.items():
            if gid in already_claimed:
                continue
            if p.projection_id != ev.projection_id:
                continue
            if p.source == (ev.cam_name, ev.track_id):
                continue
            if p.state == "fresh":
                continue
            if not self._allows_update_distance(p, ev):
                continue
            du = ev.u - p.u
            dv = ev.v - p.v
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
        gid = self._claim_gid()
        self._spawned_total += 1
        self._persons[gid] = Person(
            gid=gid,
            projection_id=ev.projection_id,
            u=ev.u,
            v=ev.v,
            raw_u=ev.u,
            raw_v=ev.v,
            vx=0.0,
            vy=0.0,
            conf=ev.conf,
            last_t=now,
            last_seen_t=now,
            state="fresh",
            source=src,
            relaxed=ev.relaxed,
            source_zone=_event_source_zone(ev),
            dispatch_center_distance=max(0.0, float(ev.dispatch_center_distance)),
        )
        self._source_to_gid[src] = gid
        return gid

    def _fresh_duplicate_match(self, ev: PersonEvent) -> Optional[int]:
        if not ev.dispatching or self.overlap_duplicate_radius_uv <= 0.0:
            return None
        best_gid: Optional[int] = None
        best_dist = self.overlap_duplicate_radius_uv
        for gid, p in self._persons.items():
            if p.state != "fresh":
                continue
            if p.projection_id != ev.projection_id:
                continue
            if p.source[0] == ev.cam_name:
                continue
            if not self._allows_update_distance(p, ev):
                continue
            du = ev.u - p.u
            dv = ev.v - p.v
            dist = (du * du + dv * dv) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best_gid = gid
        return best_gid

    def _suppress_fresh_duplicate(
        self,
        gid: int,
        src: tuple[str, int],
        ev: PersonEvent,
        now: float,
    ) -> None:
        self.duplicate_suppressed_count += 1
        person = self._persons.get(gid)
        if person is None:
            return
        if not self._duplicate_source_is_better(person, ev):
            return
        self._source_to_gid.pop(person.source, None)
        self._source_to_gid[src] = gid
        person.source = src
        person.relaxed = person.relaxed or ev.relaxed
        self._update_person(gid, ev, now)

    def _duplicate_source_is_better(self, person: Person, ev: PersonEvent) -> bool:
        ev_distance = max(0.0, float(ev.dispatch_center_distance))
        if ev_distance + 1e-9 < person.dispatch_center_distance:
            return True
        if abs(ev_distance - person.dispatch_center_distance) > 1e-9:
            return False
        ev_priority = _source_zone_priority(ev)
        person_priority = _source_zone_name_priority(person.source_zone)
        if ev_priority != person_priority:
            return ev_priority > person_priority
        return ev.conf > person.conf

    def _can_update_person(self, gid: int, ev: PersonEvent) -> bool:
        p = self._persons.get(gid)
        if p is None:
            return False
        return self._allows_update_distance(p, ev)

    def _best_aux_match(
        self,
        person: Person,
        aux_events: list[PersonEvent],
        now: float,
    ) -> Optional[PersonEvent]:
        if self.aux_match_uv_radius <= 0.0 or self.aux_match_time_window_s <= 0.0:
            return None
        best_ev: Optional[PersonEvent] = None
        best_dist = self.aux_match_uv_radius
        for ev in aux_events:
            if ev.projection_id != person.projection_id:
                continue
            if now - ev.t > self.aux_match_time_window_s:
                continue
            if not self._allows_update_distance(person, ev):
                continue
            du = ev.u - person.u
            dv = ev.v - person.v
            dist = (du * du + dv * dv) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best_ev = ev
        return best_ev

    def _apply_aux_sighting(
        self,
        person: Person,
        ev: PersonEvent,
        now: float,
    ) -> None:
        a = self.aux_position_alpha
        if a > 0.0:
            person.u = person.u + (ev.u - person.u) * a
            person.v = person.v + (ev.v - person.v) * a
            person.raw_u = person.u
            person.raw_v = person.v
        person.conf = max(person.conf, ev.conf)
        person.last_t = now
        person.state = "held"

    def _refresh_pending_with_aux(
        self,
        aux_events: list[PersonEvent],
        now: float,
    ) -> None:
        if not aux_events:
            return
        for pending in self._pending.values():
            ev = self._best_aux_match(pending.person, aux_events, now)
            if ev is None:
                continue
            self._apply_aux_sighting(pending.person, ev, now)
            pending.lost_t = now
            pending.hold_s = max(pending.hold_s, self._aux_pending_hold_s(pending.person))

    def _aux_pending_hold_s(self, person: Person) -> float:
        return max(self._pending_hold_s(person), self.aux_match_time_window_s)

    def _allows_update_distance(self, person: Person, ev: PersonEvent) -> bool:
        limit = self.max_update_jump_uv
        if limit <= 0.0:
            return True
        du = ev.u - person.u
        dv = ev.v - person.v
        return (du * du + dv * dv) ** 0.5 <= limit

    def _retire_person(self, gid: int) -> None:
        person = self._persons.pop(gid, None)
        if person is None:
            self._pending.pop(gid, None)
            return
        self._source_to_gid.pop(person.source, None)
        self._just_lost.append(LostPerson(person.projection_id, gid))
        self._release_after_update.append(gid)
        self.lost_count += 1
        self.teleport_reject_count += 1

    def _release_deferred_gids(self) -> None:
        for gid in self._release_after_update:
            self._release_gid(gid)
        self._release_after_update = []

    def _claim_gid(self) -> int:
        if self.reuse_lost_gids and self._free_gids:
            gid = heapq.heappop(self._free_gids)
            self._free_gid_set.discard(gid)
            return gid
        gid = self._next_gid
        self._next_gid += 1
        return gid

    def _release_gid(self, gid: int) -> None:
        if not self.reuse_lost_gids:
            return
        if gid in self._persons or gid in self._pending or gid in self._free_gid_set:
            return
        heapq.heappush(self._free_gids, gid)
        self._free_gid_set.add(gid)

    def _update_person(self, gid: int, ev: PersonEvent, now: float) -> None:
        p = self._persons.get(gid)
        if p is None:
            return
        dt = now - p.last_t
        prev_u = p.u
        prev_v = p.v
        prev_raw_u = p.raw_u
        prev_raw_v = p.raw_v
        a_pos = self.position_alpha
        if a_pos >= 1.0:
            next_u = ev.u
            next_v = ev.v
        else:
            next_u = prev_u + (ev.u - prev_u) * a_pos
            next_v = prev_v + (ev.v - prev_v) * a_pos
        if 0.0 < dt <= self.velocity_max_dt_s:
            inst_vx = (ev.u - prev_raw_u) / dt
            inst_vy = (ev.v - prev_raw_v) / dt
            a = self.velocity_alpha
            p.vx = a * inst_vx + (1.0 - a) * p.vx
            p.vy = a * inst_vy + (1.0 - a) * p.vy
        else:
            # Long gap or first frame on this gid — reset velocity to avoid a
            # huge spike from the hand-off discontinuity.
            p.vx = 0.0
            p.vy = 0.0
        p.u = next_u
        p.v = next_v
        p.raw_u = ev.u
        p.raw_v = ev.v
        p.conf = ev.conf
        p.last_t = now
        p.last_seen_t = now
        p.state = "fresh"
        p.relaxed = p.relaxed or ev.relaxed
        p.source_zone = _event_source_zone(ev)
        p.dispatch_center_distance = max(0.0, float(ev.dispatch_center_distance))

    def _update_held_person(self, gid: int, ev: PersonEvent, now: float) -> None:
        self._update_person(gid, ev, now)
        p = self._persons.get(gid)
        if p is not None:
            p.state = "held"

    def _hold_person(self, gid: int, ev: PersonEvent, now: float) -> None:
        p = self._persons.get(gid)
        if p is None:
            return
        p.last_seen_t = now
        p.conf = ev.conf
        p.vx *= 0.8
        p.vy *= 0.8
        p.state = "held"
        p.relaxed = p.relaxed or ev.relaxed
        p.source_zone = _event_source_zone(ev)

    def _pending_hold_s(self, person: Person) -> float:
        if person.relaxed and self.relaxed_hold_s > 0.0:
            return max(self.hand_off_window_s, self.relaxed_hold_s)
        return self.hand_off_window_s


if __name__ == "__main__":
    import sys
    from types import SimpleNamespace

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

    # (a2) Three-camera traversal through a center camera keeps one gid.
    pt = PersonTracker(hand_off_window_s=0.4, match_uv_radius=0.06)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 5, 0.38, 0.5, 0.9, t)], [], t)
    t += 0.05
    pt.update([], [("cam0", 5)], t)
    t += 0.10
    persons = pt.update(
        [PersonEvent("corridor", "cam2", 8, 0.42, 0.5, 0.85, t)],
        [],
        t,
    )
    t += 0.05
    pt.update([], [("cam2", 8)], t)
    t += 0.10
    persons = pt.update(
        [PersonEvent("corridor", "cam1", 3, 0.47, 0.5, 0.85, t)],
        [],
        t,
    )
    gids = {p.gid for p in persons}
    _check(
        "(a2) cam0->cam2->cam1 traversal keeps one gid",
        len(gids) == 1 and pt._source_to_gid.get(("cam1", 3)) == 1,
        f"gids={gids}, source={pt._source_to_gid.get(('cam1', 3))}",
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
            PersonEvent("corridor", "cam1", 4, 0.54, 0.5, 0.85, t),  # hand-off candidate
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
        and gid_for_handoff == 1
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
        lost == [LostPerson("corridor", 1)],
        f"lost={lost}",
    )

    # (d) Drain is idempotent (second call returns empty).
    second = pt.drain_lost_gids()
    _check("(d) drain_lost_gids is idempotent", second == [])

    # (d2) Once a gid is terminally lost, the next new person reuses the
    # smallest free gid so OSC keys stay bounded by concurrent occupancy.
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 8, 0.4, 0.5, 0.9, t + 0.01)],
        [],
        t + 0.01,
    )
    _check(
        "(d2) terminal lost gid is reused",
        {p.gid for p in persons} == {1}
        and pt._next_gid == 2
        and pt.spawned_count == 2,
        f"persons={[p.gid for p in persons]}, next_gid={pt._next_gid}, spawned={pt.spawned_count}",
    )

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
        any(p.projection_id == "lobby" and p.gid == 2 for p in persons)
        and any(p.projection_id == "corridor" and p.gid == 1 for p in persons),
        f"persons={[(p.gid, p.projection_id) for p in persons]}",
    )

    # (f) Dispatch boundary jitter: a track that exits dispatch but is still
    # tracked produces no lost_source from CamWorker, so the same gid is
    # kept across silent gaps without spawning a new one.
    pt = PersonTracker(hand_off_window_s=1.0, match_uv_radius=0.05)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 5, 0.49, 0.5, 0.9, t)], [], t)
    t += 0.05
    # Frame 1: out of dispatch but still in projection, no lost; same gid stays held.
    silent_active = pt.update(
        [PersonEvent("corridor", "cam0", 5, 0.50, 0.5, 0.8, t, dispatching=False)],
        [],
        t,
    )
    silent_state = silent_active[0].state if silent_active else None
    t += 0.05
    # Frame 2: back in dispatch, same source.
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 5, 0.49, 0.5, 0.9, t)],
        [],
        t,
    )
    _check(
        "(f) boundary jitter keeps same gid active as held",
        {p.gid for p in silent_active} == {1}
        and silent_state == "held"
        and pt._next_gid == 2
        and {p.gid for p in persons} == {1},
        f"silent_state={silent_state}, next_gid={pt._next_gid}, "
        f"persons={[p.gid for p in persons]}",
    )

    # (f2) Projection-overlap hand-off: cam0 is still observed outside dispatch
    # when cam1 starts dispatching nearby, so cam1 inherits the active gid
    # instead of spawning a duplicate.
    pt = PersonTracker(hand_off_window_s=1.0, match_uv_radius=0.05)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 5, 0.49, 0.5, 0.9, t)], [], t)
    t += 0.05
    persons = pt.update(
        [
            PersonEvent("corridor", "cam0", 5, 0.51, 0.5, 0.8, t, dispatching=False),
            PersonEvent("corridor", "cam1", 2, 0.52, 0.5, 0.9, t),
        ],
        [],
        t,
    )
    _check(
        "(f2) projection-overlap hand-off reuses gid",
        {p.gid for p in persons} == {1}
        and pt._source_to_gid.get(("cam1", 2)) == 1
        and pt._next_gid == 2,
        f"persons={[p.gid for p in persons]}, source_map={pt._source_to_gid}",
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
        2 in {p.gid for p in persons} and pt._next_gid == 3,
        f"persons={[p.gid for p in persons]}, next_gid={pt._next_gid}",
    )

    # (m) Duplicate same-source events in one frame should not spawn duplicate
    # gids. This can happen while calibrating overlapping regions on one camera.
    pt = PersonTracker()
    persons = pt.update(
        [
            PersonEvent("corridor", "cam0", 7, 0.20, 0.50, 0.80, 0.0),
            PersonEvent("corridor", "cam0", 7, 0.21, 0.50, 0.90, 0.0),
        ],
        [],
        0.0,
    )
    _check(
        "(m) duplicate same-source events collapse to one gid",
        {p.gid for p in persons} == {1}
        and pt.spawned_count == 1
        and pt._source_to_gid.get(("cam0", 7)) == 1,
        f"persons={[p.gid for p in persons]}, spawned={pt.spawned_count}",
    )

    # (m2) A projection-only overlap source can take over an existing gid, but
    # a brand-new projection-only source cannot create a duplicate actor.
    pt = PersonTracker(match_uv_radius=0.08)
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 1, 0.40, 0.70, 0.90, t)], [], t)
    t += 0.05
    persons = pt.update(
        [PersonEvent("corridor", "cam2", 9, 0.42, 0.70, 0.85, t, dispatching=False)],
        [],
        t,
    )
    _check(
        "(m2) overlap projection-only source claims existing gid",
        {p.gid for p in persons} == {1}
        and pt.spawned_count == 1
        and pt._source_to_gid.get(("cam2", 9)) == 1,
        f"persons={[p.gid for p in persons]}, spawned={pt.spawned_count}, source={pt._source_to_gid}",
    )
    t += 0.05
    persons = pt.update(
        [PersonEvent("corridor", "cam1", 5, 0.90, 0.70, 0.90, t, dispatching=False)],
        [],
        t,
    )
    _check(
        "(m3) unmatched projection-only source does not spawn",
        {p.gid for p in persons} == {1} and pt.spawned_count == 1,
        f"persons={[p.gid for p in persons]}, spawned={pt.spawned_count}",
    )

    # (m3a) Auxiliary sightings are not ownership sources. They can keep a
    # primary gid pending across an interior miss, but cannot spawn or take
    # over source ownership by themselves.
    pt = PersonTracker(
        hold_boundary_margin_uv=0.1,
        aux_match_uv_radius=0.08,
        aux_match_time_window_s=0.5,
        aux_position_alpha=0.25,
    )
    t = 0.0
    pt.update([PersonEvent("corridor", "cam0", 1, 0.40, 0.50, 0.90, t)], [], t)
    t += 0.05
    persons = pt.update(
        [PersonEvent("corridor", "cam2", 9, 0.43, 0.50, 0.70, t, auxiliary=True)],
        [("cam0", 1)],
        t,
    )
    lost = pt.drain_lost_gids()
    _check(
        "(m3a) auxiliary sighting prevents immediate interior lost",
        {p.gid for p in persons} == {1}
        and lost == []
        and pt.spawned_count == 1
        and ("cam2", 9) not in pt._source_to_gid,
        f"persons={[p.gid for p in persons]}, lost={lost}, source={pt._source_to_gid}",
    )
    t += 0.10
    persons = pt.update(
        [PersonEvent("corridor", "cam1", 5, 0.44, 0.50, 0.90, t)],
        [],
        t,
    )
    _check(
        "(m3b) primary camera can claim gid after auxiliary-confirmed gap",
        {p.gid for p in persons} == {1}
        and pt._source_to_gid.get(("cam1", 5)) == 1
        and pt.spawned_count == 1,
        f"persons={[p.gid for p in persons]}, source={pt._source_to_gid}, spawned={pt.spawned_count}",
    )

    pt = PersonTracker(hold_boundary_margin_uv=0.1, aux_match_uv_radius=0.03)
    pt.update([PersonEvent("corridor", "cam0", 1, 0.40, 0.50, 0.90, 0.0)], [], 0.0)
    persons = pt.update(
        [PersonEvent("corridor", "cam2", 9, 0.55, 0.50, 0.70, 0.05, auxiliary=True)],
        [("cam0", 1)],
        0.05,
    )
    lost = pt.drain_lost_gids()
    _check(
        "(m3c) far auxiliary sighting does not suppress lost",
        persons == [] and lost == [LostPerson("corridor", 1)],
        f"persons={persons}, lost={lost}",
    )

    # (m4) Simultaneous cross-camera fresh duplicates inside the narrow
    # overlap radius collapse to one gid. The source deeper inside its
    # dispatch ownership band can take over the existing gid.
    pt = PersonTracker(overlap_duplicate_radius_uv=0.04)
    persons = pt.update(
        [
            PersonEvent(
                "corridor",
                "cam0",
                11,
                0.400,
                0.50,
                0.90,
                0.0,
                dispatch_center_distance=0.18,
            ),
            PersonEvent(
                "corridor",
                "cam2",
                21,
                0.425,
                0.50,
                0.85,
                0.0,
                dispatch_center_distance=0.02,
            ),
        ],
        [],
        0.0,
    )
    _check(
        "(m4) same-frame cross-camera duplicate keeps one gid",
        {p.gid for p in persons} == {1}
        and pt.spawned_count == 1
        and pt.duplicate_suppressed_count == 1
        and pt._source_to_gid.get(("cam2", 21)) == 1,
        f"persons={[(p.gid, p.source) for p in persons]}, spawned={pt.spawned_count}, "
        f"suppressed={pt.duplicate_suppressed_count}, source_map={pt._source_to_gid}",
    )

    # (m5) Far enough simultaneous people still spawn distinct gids.
    pt = PersonTracker(overlap_duplicate_radius_uv=0.04)
    persons = pt.update(
        [
            PersonEvent("corridor", "cam0", 11, 0.40, 0.50, 0.90, 0.0),
            PersonEvent("corridor", "cam2", 21, 0.47, 0.50, 0.85, 0.0),
        ],
        [],
        0.0,
    )
    _check(
        "(m5) separated same-frame cross-camera events stay distinct",
        {p.gid for p in persons} == {1, 2}
        and pt.spawned_count == 2
        and pt.duplicate_suppressed_count == 0,
        f"persons={[p.gid for p in persons]}, spawned={pt.spawned_count}, "
        f"suppressed={pt.duplicate_suppressed_count}",
    )

    # (m6) Duplicate suppression is projection-local.
    pt = PersonTracker(overlap_duplicate_radius_uv=0.04)
    persons = pt.update(
        [
            PersonEvent("corridor", "cam0", 11, 0.40, 0.50, 0.90, 0.0),
            PersonEvent("lobby", "cam2", 21, 0.42, 0.50, 0.85, 0.0),
        ],
        [],
        0.0,
    )
    _check(
        "(m6) fresh duplicate suppression rejects cross-projection events",
        {p.gid for p in persons} == {1, 2}
        and pt.spawned_count == 2
        and pt.duplicate_suppressed_count == 0,
        f"persons={[(p.gid, p.projection_id) for p in persons]}, "
        f"suppressed={pt.duplicate_suppressed_count}",
    )

    # (m7) The teleport guard still wins over the duplicate radius.
    pt = PersonTracker(overlap_duplicate_radius_uv=0.10, max_update_jump_uv=0.03)
    persons = pt.update(
        [
            PersonEvent("corridor", "cam0", 11, 0.40, 0.50, 0.90, 0.0),
            PersonEvent("corridor", "cam2", 21, 0.45, 0.50, 0.85, 0.0),
        ],
        [],
        0.0,
    )
    _check(
        "(m7) max_update_jump_uv prevents far fresh duplicate merge",
        {p.gid for p in persons} == {1, 2}
        and pt.spawned_count == 2
        and pt.duplicate_suppressed_count == 0,
        f"persons={[p.gid for p in persons]}, spawned={pt.spawned_count}, "
        f"suppressed={pt.duplicate_suppressed_count}",
    )

    # (n) Velocity comes from raw observation deltas even when position is
    # smoothed, so TD motion vectors do not get damped twice.
    pt = PersonTracker(position_alpha=0.5, velocity_alpha=1.0)
    pt.update([PersonEvent("corridor", "cam0", 1, 0.0, 0.0, 0.9, 0.0)], [], 0.0)
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 1, 0.2, 0.0, 0.9, 1.0)],
        [],
        1.0,
    )
    p = persons[0]
    _check(
        "(n) velocity uses raw observation delta, not smoothed delta",
        abs(p.u - 0.1) < 1e-6 and abs(p.vx - 0.2) < 1e-6,
        f"u={p.u}, vx={p.vx}",
    )

    # (n2) Teleport guard: a same-source observation that jumps far across the
    # projection should not drag the old gid through OSC. It emits lost for the
    # old gid and spawns a new gid for the far-away observation.
    pt = PersonTracker(max_update_jump_uv=0.10)
    pt.update([PersonEvent("corridor", "cam0", 1, 0.20, 0.50, 0.9, 0.0)], [], 0.0)
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 1, 0.80, 0.50, 0.9, 0.05)],
        [],
        0.05,
    )
    lost = pt.drain_lost_gids()
    _check(
        "(n2) same-source teleport is split into a new gid",
        {p.gid for p in persons} == {2}
        and lost == [LostPerson("corridor", 1)]
        and pt.teleport_reject_count == 1,
        f"persons={[p.gid for p in persons]}, lost={lost}, rejects={pt.teleport_reject_count}",
    )

    # (n3) Pending velocity prediction may match a far-away point, but the
    # teleport guard still protects the displayed actor from a long jump.
    pt = PersonTracker(
        hand_off_window_s=2.5,
        match_uv_radius=0.05,
        velocity_predict_max_dt_s=1.0,
        max_update_jump_uv=0.10,
    )
    t = 0.0
    u = 0.20
    for _ in range(10):
        pt.update([PersonEvent("corridor", "cam0", 5, u, 0.50, 0.9, t)], [], t)
        t += 0.05
        u += 0.015
    pt.update([], [("cam0", 5)], t)
    t += 1.0
    persons = pt.update(
        [PersonEvent("corridor", "cam1", 9, u + 0.30, 0.50, 0.85, t)],
        [],
        t,
    )
    _check(
        "(n3) pending predicted teleport is not stitched when guarded",
        2 in {p.gid for p in persons} and pt.handoff_count == 0,
        f"persons={[p.gid for p in persons]}, handoffs={pt.handoff_count}",
    )

    zone = SimpleNamespace(
        projection_id="corridor",
        id="entry",
        uv_rect=(0.2, 0.2, 0.6, 0.6),
        release_after_s=0.5,
    )
    zt = InteractionZoneTracker(fresh_grace_s=0.2)
    person = Person(
        gid=10,
        projection_id="corridor",
        u=0.3,
        v=0.4,
        raw_u=0.3,
        raw_v=0.4,
        vx=0.1,
        vy=0.0,
        conf=0.9,
        last_t=0.0,
        last_seen_t=0.0,
        state="fresh",
        source=("cam0", 1),
    )
    zu = zt.update([zone], [person], 0.0)
    _check(
        "(o) zone enter emits once with local coordinates",
        len(zu.transitions) == 1
        and zu.transitions[0].kind == "enter"
        and len(zu.persons) == 1
        and abs(zu.persons[0].zone_u - 0.25) < 1e-6
        and abs(zu.persons[0].zone_v - 0.5) < 1e-6
        and zu.counts[("corridor", "entry")] == 1,
        f"update={zu}",
    )
    person.u = 0.4
    person.v = 0.5
    person.raw_u = 0.4
    person.raw_v = 0.5
    person.last_seen_t = 0.1
    zu = zt.update([zone], [person], 0.1)
    _check(
        "(p) zone dwell updates without duplicate enter",
        not zu.transitions and len(zu.persons) == 1 and zu.persons[0].dwell_s > 0.0,
        f"update={zu}",
    )
    person.u = 0.8
    person.v = 0.5
    person.last_seen_t = 0.2
    zu = zt.update([zone], [person], 0.2)
    _check(
        "(q) leaving rect emits one exited leave",
        len(zu.transitions) == 1
        and zu.transitions[0].kind == "leave"
        and zu.transitions[0].reason_code == InteractionZoneTracker.REASON_EXITED
        and zu.counts[("corridor", "entry")] == 0,
        f"update={zu}",
    )
    person.u = 0.3
    person.v = 0.4
    person.state = "fresh"
    person.last_seen_t = 1.0
    zt.update([zone], [person], 1.0)
    person.state = "held"
    zu = zt.update([zone], [person], 1.2)
    held_presence = zu.persons[0].presence if zu.persons else 0.0
    zu2 = zt.update([zone], [person], 1.6)
    _check(
        "(r) held zone presence decays, then stale leaves",
        0.0 < held_presence < 1.0
        and len(zu2.transitions) == 1
        and zu2.transitions[0].reason_code == InteractionZoneTracker.REASON_STALE,
        f"presence={held_presence}, update2={zu2}",
    )

    # (s) Edge-gated hold: a vanished interior person should disappear
    # immediately instead of lingering as a held ghost in the middle.
    pt = PersonTracker(hand_off_window_s=1.0, hold_boundary_margin_uv=0.1)
    pt.update([PersonEvent("corridor", "cam0", 5, 0.50, 0.50, 0.9, 0.0)], [], 0.0)
    persons = pt.update([], [("cam0", 5)], 0.1)
    lost = pt.drain_lost_gids()
    _check(
        "(s) interior miss emits lost immediately when hold is edge-gated",
        persons == [] and lost == [LostPerson("corridor", 1)],
        f"persons={persons}, lost={lost}",
    )

    # (t) The same edge gate still allows held state near projection edges,
    # which is where walk-in/out and boundary hand-off smoothing is useful.
    pt = PersonTracker(hand_off_window_s=1.0, hold_boundary_margin_uv=0.1)
    pt.update([PersonEvent("corridor", "cam0", 5, 0.04, 0.50, 0.9, 0.0)], [], 0.0)
    persons = pt.update([], [("cam0", 5)], 0.1)
    lost = pt.drain_lost_gids()
    _check(
        "(t) edge miss remains held inside hand-off window",
        len(persons) == 1 and persons[0].state == "held" and lost == [],
        f"persons={persons}, lost={lost}",
    )

    # (t2) Internal dispatch boundaries should not create held ghosts. The
    # cam0 -> cam2 -> cam1 hand-off relies on live overlap/fresh matching, not
    # a pending held actor after the source disappears in the projection middle.
    pt = PersonTracker(hand_off_window_s=1.0, hold_boundary_margin_uv=0.1)
    pt.update([PersonEvent("corridor", "cam0", 5, 0.41, 0.50, 0.9, 0.0)], [], 0.0)
    persons = pt.update([], [("cam0", 5)], 0.1)
    lost = pt.drain_lost_gids()
    _check(
        "(t2) internal hand-off edge miss emits lost immediately",
        persons == [] and lost == [LostPerson("corridor", 1)] and pt.handoff_count == 0,
        f"persons={persons}, lost={lost}, source_map={pt._source_to_gid}",
    )

    # (u) Relaxed/stair actors can linger longer than normal walk actors.
    pt = PersonTracker(
        hand_off_window_s=0.5,
        hold_boundary_margin_uv=0.0,
        relaxed_hold_s=2.0,
    )
    pt.update(
        [PersonEvent("corridor", "cam0", 5, 0.50, 0.50, 0.9, 0.0, relaxed=True)],
        [],
        0.0,
    )
    persons = pt.update([], [("cam0", 5)], 0.1)
    still_held = pt.update([], [], 1.0)
    lost_mid = pt.drain_lost_gids()
    gone = pt.update([], [], 2.2)
    lost_end = pt.drain_lost_gids()
    _check(
        "(u) relaxed miss uses relaxed_hold_s before lost",
        len(persons) == 1
        and len(still_held) == 1
        and lost_mid == []
        and gone == []
        and lost_end == [LostPerson("corridor", 1)],
        f"persons={persons}, still_held={still_held}, lost_mid={lost_mid}, gone={gone}, lost_end={lost_end}",
    )

    # (v) Source-zone metadata follows the current detector path while the
    # primary UV payload remains unchanged.
    pt = PersonTracker(hand_off_window_s=0.5)
    persons = pt.update(
        [PersonEvent("corridor", "cam0", 5, 0.30, 0.50, 0.9, 0.0)],
        [],
        0.0,
    )
    floor_zone = persons[0].source_zone if persons else ""
    persons = pt.update(
        [
            PersonEvent(
                "corridor",
                "cam0",
                5,
                0.31,
                0.50,
                0.9,
                0.1,
                relaxed=True,
            )
        ],
        [],
        0.1,
    )
    stair_zone = persons[0].source_zone if persons else ""
    _check(
        "(v) relaxed detector path tags source_zone for TD lanes",
        floor_zone == "floor" and stair_zone == "stair_relaxed",
        f"floor={floor_zone}, stair={stair_zone}",
    )

    sys.exit(1 if failed else 0)
