"""Transit signal priority (TSP) controller for one isolated signalised node.

Pure logic, standard library only: a simulator (SUMO/TraCI or custom) feeds bus detections
(check-in upstream, optional live updates, check-out on the far side) before each tick and
renders the (phase_id, interval) that step() returns. The controller owns the timing: a
single-ring fixed phase sequence (2-phase, or 4-phase split) as the base plan, plus active TSP:
green extension (hold), red truncation (early green), check-in/check-out and recovery of green
lost to TSP. Activation is a stub hook (`gate`). Units: s, m, m/s; tick 1 s.
Alongside a plan it doesn't own (a coordinated signal), sync() puts it in that plan's state before a
tick, so it can take over exactly where the plan is, and set_green() sets the green running (a
transition back to the plan); note() logs an event of the caller's (a refusal of its own).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

DT = 1.0  # tick, s
EPS = 1e-9

# ASSUMPTION: the config dataclasses are keyword-only (kw_only=True) so their fields can keep the
# spec's order, which puts defaulted fields before required ones.


@dataclass(kw_only=True)
class PhaseCfg:
    phase_id: str
    planned_green: float  # base-plan green
    min_green: float
    ped_min: float = 0.0  # walk + ped clearance; 0 if no ped phase
    yellow: float
    all_red: float
    skippable: bool = False  # may be skipped if not called


@dataclass(kw_only=True)
class ApproachCfg:
    approach_id: str
    phase_id: str  # the phase serving this approach (all its movements)
    x: float  # check-in detector distance upstream of the stop line
    v_lo: float  # lowest expected bus speed
    v_hi: float  # speed limit
    dwell_max: float = 0.0  # > 0 only if a bus stop lies between check-in and stop line


@dataclass(kw_only=True)
class NodeCfg:
    phases: list[PhaseCfg]  # order = phase sequence
    approaches: list[ApproachCfg]
    h: float = 2.0  # saturation headway
    t_su: float = 2.0  # start-up lost time
    W: float  # intersection box width (crossing path)
    L_bus: float = 12.0
    v_cross: float  # bus crossing speed inside the box
    E_max: float = 15.0  # max green extension
    W_max: float  # max red wait any phase may suffer
    C_max: float = 10.0  # max recovery credit per phase per cycle
    request_timeout: float = 180.0  # s after check-in


@dataclass
class BusRequest:
    approach_id: str
    phase_id: str
    t_checkin: float
    bus_ids: set[str] = field(default_factory=set)  # merged buses on the same phase
    checked_out: set[str] = field(default_factory=set)
    t_upd: float = 0.0  # latest detection: time, distance to stop line, speed, vehicles queued ahead
    d: float = 0.0
    v: float = 0.0
    n: int = 0
    a_e: float = 0.0  # live window
    a_l: float = 0.0
    g_star: float = 0.0
    e_l: float = 0.0


def eff_min(p: PhaseCfg) -> float:
    return max(p.min_green, p.ped_min)


class TSPController:
    def __init__(self, cfg: NodeCfg, gate: Callable[[BusRequest, "TSPController"], bool] = lambda r, c: True) -> None:
        self.cfg, self.gate = cfg, gate
        self.ids = [p.phase_id for p in cfg.phases]
        self.app = {a.approach_id: a for a in cfg.approaches}
        self._validate()
        # ASSUMPTION: the plan starts at t = 0 with the first phase's green; the others red since 0.
        self.cur, self.interval, self.t_interval_start, self.t_green_onset = 0, "G", 0.0, 0.0
        self.green_target, self.ext_used = cfg.phases[0].planned_green, 0.0
        self._due = self.green_target  # the green this phase was due at its onset (before TSP cuts)
        self.active: BusRequest | None = None
        self.queue: deque[BusRequest] = deque()
        self.truncating = False
        self.red_since = {k: 0.0 for k in self.ids}
        self.deficit = {k: 0.0 for k in self.ids}  # green owed TO a phase
        self.owed = {k: 0.0 for k in self.ids}  # green owed BY a priority phase
        self.log: list[tuple[float, str, dict]] = []
        self._by_bus: dict[str, BusRequest] = {}
        self._last_priority: str | None = None
        self._end_now = self._tsp_on = False
        self._t, self._calls = 0.0, None

    def _validate(self) -> None:
        c, ph = self.cfg, self.cfg.phases
        if not ph or min(c.h, c.t_su, c.W, c.L_bus, c.v_cross, c.E_max, c.W_max, c.C_max, c.request_timeout) <= 0:
            raise ValueError("need at least one phase, and every node time and size > 0")
        self.t_clear = (c.W + c.L_bus) / c.v_cross
        for p in ph:
            if min(p.planned_green, p.min_green, p.yellow, p.all_red) <= 0 or p.ped_min < 0:
                raise ValueError(f"phase {p.phase_id}: times must be > 0 (ped_min >= 0)")
            if p.planned_green < eff_min(p):
                raise ValueError(f"phase {p.phase_id}: planned_green {p.planned_green} < eff_min {eff_min(p)}")
            # A bus entering as the green ends clears the box by the end of the change interval (a bus given
            # priority is held in green until it has cleared: _rule_g), so the plan's own intervals do.
            if p.yellow + p.all_red < self.t_clear:
                raise ValueError(f"phase {p.phase_id}: yellow + all_red {p.yellow + p.all_red} < bus clearance "
                                 f"t_clear {self.t_clear:.2f}")
            wait = p.yellow + p.all_red + sum(q.planned_green + q.yellow + q.all_red for q in ph if q is not p)
            if wait > c.W_max:
                raise ValueError(f"phase {p.phase_id}: base plan red wait {wait} > W_max {c.W_max}")
        for a in c.approaches:
            if a.phase_id not in self.ids:
                raise ValueError(f"approach {a.approach_id}: no phase {a.phase_id!r}")
            if not 0 < a.v_lo < a.v_hi or a.x <= 0 or a.dwell_max < 0:
                raise ValueError(f"approach {a.approach_id}: need 0 < v_lo < v_hi, x > 0, dwell_max >= 0")
            P = ph[self.ids.index(a.phase_id)]
            t_sw = P.yellow + P.all_red + sum(eff_min(q) + q.yellow + q.all_red for q in ph if q is not P)
            if a.x < a.v_hi * t_sw:
                raise ValueError(f"approach {a.approach_id}: x {a.x} < v_hi * T_switch_max = {a.v_hi * t_sw:.1f} m")

    # --- events (called before step(t) for that tick) ----------------------------------------

    def on_checkin(self, bus_id: str, approach_id: str, d: float, v: float, n: int, t: float) -> None:
        P = self.app[approach_id].phase_id
        self._log(t, "CHECKIN", bus=bus_id, approach=approach_id, phase=P)
        if self.active is not None and self.active.phase_id == P:
            r = self.active
        else:
            r = next((q for q in self.queue if q.phase_id == P), None)
            if r is None:
                r = BusRequest(approach_id, P, t)
                if self.active is None:
                    self.active = r
                else:
                    self.queue.append(r)
        r.bus_ids.add(bus_id)
        self._by_bus[bus_id] = r
        r.approach_id, r.t_upd, r.d, r.v, r.n = approach_id, t, d, v, n  # the latest detection

    def on_update(self, bus_id: str, d: float, v: float, n: int, t: float) -> None:
        r = self._by_bus.get(bus_id)
        if r is not None and bus_id not in r.checked_out:
            r.t_upd, r.d, r.v, r.n = t, d, v, n

    def on_checkout(self, bus_id: str, t: float) -> None:
        self._log(t, "CHECKOUT", bus=bus_id)
        if bus_id in self._by_bus:
            self._by_bus[bus_id].checked_out.add(bus_id)

    # --- alongside a plan it doesn't own ---------------------------------------------------------

    def sync(self, t: float, phase_id: str, interval: str, began: float, green: float, red_since: dict) -> None:
        """Take the state of a plan running alongside, before the tick at `t`: phase `phase_id` in `interval`
        ("G", "Y" or "AR") since `began`, its green `green` s long; `red_since`: when each other phase's green
        last ended. Requests stay; TSP's own changes (a latched truncation, an extension, credit and debt)
        are cleared."""
        k = self.ids.index(phase_id)
        p = self.cfg.phases[k]
        self.cur, self.interval, self.t_interval_start, self._t = k, interval, began, t
        self.t_green_onset = began - (0.0 if interval == "G" else green if interval == "Y" else green + p.yellow)
        self.green_target = self._due = max(eff_min(p), green)
        self.ext_used, self.truncating, self._end_now = 0.0, False, False
        self.red_since.update(red_since)
        if interval != "G":
            self.red_since[phase_id] = self.t_green_onset + green
        self.deficit = {k: 0.0 for k in self.ids}
        self.owed = {k: 0.0 for k in self.ids}

    def set_green(self, seconds: float) -> None:
        """The green running lasts `seconds` from its onset (at least its minimum), no credit or debt
        counted: a transition back to a plan."""
        if self.interval == "G":
            self.green_target = self._due = max(eff_min(self.cfg.phases[self.cur]), seconds)

    def note(self, t: float, event: str, **info) -> None:
        """Log an event of the caller's (e.g. a priority it refused) beside the controller's own."""
        self._log(t, event, **info)

    # --- one tick -----------------------------------------------------------------------------

    def step(self, t: float, calls: dict[str, bool] | None = None) -> tuple[str, str]:
        self._t, self._calls = t, calls
        self._release(t)
        r = self.active
        if r is not None:
            self._window(r)
        # ASSUMPTION: truncation (latched or not) only acts on ticks the gate allows.
        self._tsp_on = r is not None and self.gate(r, self)
        if self._tsp_on:
            if self._is_green(r.phase_id):
                self._rule_g(r, t)
            else:
                self._rule_r(r, t)
        self._advance(t)
        self._check(t)
        return self.ids[self.cur], self.interval

    def _window(self, r: BusRequest) -> None:
        c, a = self.cfg, self.app[r.approach_id]
        r.a_e = r.t_upd + r.d / a.v_hi
        r.a_l = r.t_upd + r.d / a.v_lo + a.dwell_max
        queue = c.t_su + r.n * c.h if r.n > 0 else 0.0
        r.g_star = r.a_e - queue
        onset = self.t_green_onset if self._is_green(r.phase_id) else self.planned_onset(r.phase_id)
        r.e_l = max(r.e_l, max(r.a_l, onset + queue) + self.t_clear)  # merged buses: the latest need

    def _rule_g(self, r: BusRequest, t: float) -> None:
        c = self.cfg
        if t - self.t_green_onset < self.green_target - EPS:
            return  # the plan already serves the bus
        base = self.green_target - self.ext_used
        if (not r.bus_ids <= r.checked_out and self.ext_used < c.E_max - EPS
                and r.a_e + self.t_clear <= self.t_green_onset + base + c.E_max + EPS and self._wait_ok(t)):
            grant = min(DT, c.E_max - self.ext_used)
            self.ext_used += grant
            self.green_target += grant
            self._log(t, "HOLD", phase=r.phase_id, ext_used=self.ext_used)
        else:
            self._log(t, "END_EXT", phase=r.phase_id, ext_used=self.ext_used)  # green ends; request stays

    def _rule_r(self, r: BusRequest, t: float) -> None:
        P = r.phase_id
        if not self.truncating:
            if self.planned_onset(P) <= r.g_star + EPS or t + self.min_time_to_green(P) < r.g_star - EPS:
                return  # the plan serves the bus, or truncating isn't needed yet
            self.truncating = True  # latched until P turns green
            self._log(t, "TRUNCATE", phase=P, g_star=r.g_star)
        if self.interval == "G" and self.ids[self.cur] != P:
            self.green_target = min(self.green_target, eff_min(self.cfg.phases[self.cur]))  # never earlier

    def _release(self, t: float) -> None:
        r = self.active
        if r is None:
            return
        done, late = r.bus_ids <= r.checked_out, t - r.t_checkin > self.cfg.request_timeout
        if not (done or late):
            return
        if late and not done:
            self._log(t, "TIMEOUT", phase=r.phase_id)
        if self._is_green(r.phase_id) and self.ext_used > 0:
            self._end_now = True
            self._log(t, "RELEASE_END", phase=r.phase_id)
        else:
            self._log(t, "RELEASE", phase=r.phase_id)
        self._last_priority, self.active, self.truncating, self.ext_used = r.phase_id, None, False, 0.0
        for bus in r.bus_ids:
            self._by_bus.pop(bus, None)
        if self.queue:
            self.active = self.queue.popleft()

    # --- timing helpers -------------------------------------------------------------------------

    def _between(self, k: int, P: str):
        """Phase indices strictly between index k and phase P in the sequence (all others if P is k's)."""
        j = (k + 1) % len(self.ids)
        while self.ids[j] != P:
            yield j
            j = (j + 1) % len(self.ids)

    def _left_now(self, green_left: float) -> float:
        """Time until the current phase's red ends, with `green_left` as its green."""
        p, e = self.cfg.phases[self.cur], self._t - self.t_interval_start
        if self.interval == "G":
            return max(0.0, green_left - e) + p.yellow + p.all_red
        return (p.yellow - e + p.all_red) if self.interval == "Y" else p.all_red - e

    def _skip(self, j: int) -> bool:
        p = self.cfg.phases[j]
        return p.skippable and self._calls is not None and not self._calls.get(p.phase_id, True)

    def min_time_to_green(self, P: str, calls: dict[str, bool] | None = None) -> float:
        if calls is not None:
            self._calls = calls
        ph = self.cfg.phases
        return self._left_now(eff_min(ph[self.cur])) + sum(
            0.0 if self._skip(j) else eff_min(ph[j]) + ph[j].yellow + ph[j].all_red for j in self._between(self.cur, P))

    def planned_onset(self, P: str) -> float:
        ph, cap = self.cfg.phases, self.cfg.C_max
        return self._t + self._left_now(self.green_target) + sum(
            ph[j].planned_green + min(self.deficit[ph[j].phase_id], cap) + ph[j].yellow + ph[j].all_red
            for j in self._between(self.cur, P))

    def _wait_ok(self, t: float) -> bool:
        ph, cur = self.cfg.phases, self.cur
        for k, p in enumerate(ph):
            if k != cur:
                between = sum(eff_min(ph[j]) + ph[j].yellow + ph[j].all_red for j in self._between(cur, p.phase_id))
                if t - self.red_since[p.phase_id] + DT + ph[cur].yellow + ph[cur].all_red + between > self.cfg.W_max + EPS:
                    return False
        return True

    def _is_green(self, P: str) -> bool:
        return self.interval == "G" and self.ids[self.cur] == P

    # --- interval state machine and recovery ----------------------------------------------------

    def _advance(self, t: float) -> None:
        p, e = self.cfg.phases[self.cur], t - self.t_interval_start
        if self.interval == "G":
            if self._end_now or e >= self.green_target - EPS:
                self._end_green(t)
        elif self.interval == "Y":
            if e >= p.yellow - EPS:  # yellow runs its full duration
                self.interval, self.t_interval_start = "AR", t
        elif e >= p.all_red - EPS:  # all-red runs its full duration
            self._start_green(t)

    def _end_green(self, t: float) -> None:
        k, p = self.ids[self.cur], self.cfg.phases[self.cur]
        actual = t - self.t_green_onset
        assert actual >= eff_min(p) - EPS, f"green of {k} ended after {actual} s, before eff_min"
        # ASSUMPTION: the shortfall is measured against the green due at onset (planned + credit -
        # payback, or planned for a truncated phase), so a payback isn't counted again as a deficit.
        short = self._due - actual
        priority = self.active.phase_id if self.active else self._last_priority
        if short > EPS:
            self.deficit[k] += short
            if priority is not None and priority != k:
                self.owed[priority] += short
        self.interval, self.t_interval_start, self._end_now = "Y", t, False
        self.red_since[k] = t

    def _start_green(self, t: float) -> None:
        c, n = self.cfg, len(self.ids)
        P = self.active.phase_id if self.active is not None else None
        trunc = self.truncating and self._tsp_on and P is not None
        j = (self.cur + 1) % n
        while trunc and self.ids[j] != P and self._skip(j):
            self.deficit[self.ids[j]] += c.phases[j].planned_green
            self._log(t, "SKIP", phase=self.ids[j])
            j = (j + 1) % n
        assert j == (self.cur + 1) % n or trunc, "phase order broken"
        k, p = self.ids[j], c.phases[j]
        self.cur, self.interval, self.t_interval_start, self.t_green_onset, self.ext_used = j, "G", t, t, 0.0
        if trunc and k != P:
            self.green_target, self._due = eff_min(p), p.planned_green  # truncation: no credit
            return
        if self.truncating and k == P:
            self.truncating = False
        credit = min(self.deficit[k], c.C_max)
        self.deficit[k] -= credit
        payback = max(0.0, min(self.owed[k], c.C_max, p.planned_green - eff_min(p)))
        self.owed[k] -= payback
        self.green_target = self._due = max(eff_min(p), p.planned_green + credit - payback)

    def _check(self, t: float) -> None:
        assert self.ext_used <= self.cfg.E_max + EPS, "extension over E_max"
        for k, since in self.red_since.items():
            if not self._is_green(k):
                assert t - since <= self.cfg.W_max + EPS, f"phase {k} red {t - since:.0f} s > W_max"
        assert self.active is None or self.active not in self.queue, "more than one active request"

    def _log(self, t: float, event: str, **info) -> None:
        self.log.append((t, event, info))
