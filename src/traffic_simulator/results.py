"""A run's results (no Qt): network KPIs sampled from the running simulation, kept in a temporary
folder as the run goes, and written to an Excel workbook when it ends or is interrupted.

`RunRecorder.sample(frame)` is called by simulation.Runner on the physics thread every SAMPLE_S
simulated seconds with the run's Frame. From each sample it adds up:
- per vehicle: when it was first seen, the distance it drove (speed × time), its delay, the time
  lost against its road's speed limit, Σ dt · max(0, 1 − v / v_limit) (whatever the vehicle: a bicycle
  at its own 20 km/h loses time on a 50 km/h road), its delay against its own free speed
  (VehicleState.free_speed: the limit × its speed factor, no faster than its top and desired speeds; not
  while calling at a bus stop), and its waiting time, the time at or below 0.1 m/s outside a bus stop
  (SUMO's tripinfo waitingTime); the people on board (VehicleState.people: a bus's simulated load) times
  that own delay is added up every sample, unfinished trips too; a vehicle gone from the next
  frame has arrived, and its trip (type, origin, destination, times, distance, delays, wait,
  people on board) is kept, its passengers' delay (delay × people) added up. Its times are exact, from the
  frame's `exits` (simulation.Frame): on the road (inserted) to off it, the road travel time, and from its
  requested departure to off it, the system travel time (the wait to enter included);
- vehicles generated but still waiting to enter (spawned − arrived − on the road) times the time: the
  entry wait, kept apart from the delay of those on the road;
- per road: vehicles entering it (a vehicle seen on it after another road), vehicle-seconds and
  vehicle-metres on it, and its queue: the vehicles on it slower than QUEUE_KMH, as a count and as
  a length per lane (their lengths plus 2.5 m gaps, over its lanes per direction);
- per simulated minute: a row of the network's figures (on the road, arrivals, throughput, people
  carried, mean speed, queued vehicles, delay, distance).
Every minute the rows are appended to the folder's CSV files and the running totals rewritten
(`totals.json`), so an interruption loses at most a minute; `workbook(folder, path)` turns a folder
into the .xlsx (Summary, Per minute, Roads, By type, Trips, and the sheets the run's extensions
reported, `add_sheets`), named by when it started, its seed and the AI model that decided (`file_name`),
and `recover` does that for folders a crash left behind. Passengers are vehicles × each type's `occupancy` (mode_behavior.VehicleMode),
an assumption the workbook states. Sample-based, apart from trip times: a road crossed between two samples isn't
counted as entered. `measure_from_s` (a warm-up, default 0): the figures and trips count only after it (trips that
entered the road in it are left out); the per-minute rows and spawned and arrived totals cover the whole run.
"""

from __future__ import annotations

import csv
import itertools
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .stats import reliability
from .xlsx import write_xlsx

try:
    import fcntl  # POSIX: an advisory lock, let go when its process ends (a crash too)
except ImportError:  # Windows: an open file can't be deleted, which is lock enough
    fcntl = None

SAMPLE_S = 5.0  # simulated seconds between samples
QUEUE_KMH = 5.0  # slower than this: queued
WAIT_MPS = 0.1  # at or below this, outside a bus stop: waiting (SUMO's tripinfo waitingTime)
GAP_M = 2.5  # between queued vehicles
RUNS = Path(tempfile.gettempdir()) / "ACMS runs"  # each run's working folder while it goes
_numbers = itertools.count(1)  # this process's runs, so runs started in the same second get folders of their own
_naming = threading.Lock()  # runs finishing at once (a batch's) take their workbook names one at a time
MINUTE_COLUMNS = ["Time (s)", "Clock", "Vehicles on the road", "Spawned (total)", "Arrived (total)",
                  "Arrived this minute", "Throughput (veh/h)", "People carried this minute", "People (per hour)",
                  "Mean speed (km/h)", "Queued now (veh)", "Most queued this minute (veh)", "Delay (veh·h)",
                  "Distance (veh·km)"]
TRIP_COLUMNS = ["Vehicle", "Type", "Origin", "Destination", "Departed (s)", "Arrived (s)", "Trip time (s)",
                "Distance (m)", "Delay (s)", "Wait (s)", "People", "Delay vs own free speed (s)",
                "Requested departure (s)", "System travel time (s)"]
ROAD_MINUTE_COLUMNS = ["Time (s)", "Clock", "Road", "Entered (veh)", "Flow (veh/h)", "Mean speed (km/h)",
                       "Most queued (veh)", "Delay (veh·h)"]
# Level of service of a road by its travel speed as a share of its free-flow speed (here its speed limit): HCM 2010
# urban street segments (Exhibit 16-3; HCM 6th ed. Exhibit 18-1), the v/c > 1 test left out
LOS_SPEED = ((85, "A"), (67, "B"), (50, "C"), (40, "D"), (30, "E"))


def level_of_service(percent_of_free_flow: float | None) -> str | None:
    if percent_of_free_flow is None:
        return None
    return next((grade for above, grade in LOS_SPEED if percent_of_free_flow > above), "F")


def road_record(network, road) -> dict:
    """What a road is, for the workbook and its GeoJSON: its ends, length, lanes, limit, direction, type, width,
    tightest curve and the speed that curve allows, whether it is a boundary road, an overpass."""
    from .interchanges import curve_speed_kmh  # noqa: PLC0415 - results stays light to import

    tightest = max((abs(kappa) for kappa in road.curvature), default=0.0)
    return {"from": road.from_node, "to": road.to_node, "length_m": round(road.length_m, 1),
            "lanes": road.lanes_per_direction, "limit_kmh": road.speed_limit_kmh, "direction": road.direction,
            "type": road.road_type, "width_m": round(road.width_m, 1), "boundary": road.id in network.approach_roads,
            "overpass": road.overpass, "tightest_radius_m": round(1 / tightest, 1) if tightest else None,
            "curve_kmh": curve_speed_kmh(1 / tightest) if tightest else None}


def file_name(started: datetime, recovered: bool = False, seed: int | None = None, model: str = "") -> str:
    """A run's workbook, named by when it started, its seed and the AI model that decided:
    "ACMS results 2026-09-30 14-05-33 seed 42 laya.xlsx"."""
    model = re.sub(r"[^\w.+-]+", "-", model).strip("-")  # safe in a file name
    return (f"ACMS results {started:%Y-%m-%d %H-%M-%S}" + (f" seed {seed}" if seed is not None else "")
            + (f" {model}" if model else "") + (" (recovered)" if recovered else "") + ".xlsx")


def clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


class RunRecorder:
    """One run's results, in `folder` (made under RUNS unless given)."""

    sample_s = SAMPLE_S

    def __init__(self, network, modes, folder: Path | None = None, name: str = "",
                 inputs: list[tuple[str, list[list]]] | None = None, run: list[tuple[str, object]] | None = None,
                 seed: int | None = None, model: str = "", measure_from_s: float = 0.0) -> None:
        started = self.started = datetime.now()
        self.seed, self.model = seed, model  # for its workbook's name
        self.folder = Path(folder or RUNS / f"run {started:%Y-%m-%d %H-%M-%S} {os.getpid()} {next(_numbers)}")
        self.folder.mkdir(parents=True, exist_ok=True)
        # In the shared runs folder, held while the run lives, so recover() in another ACMS leaves it alone
        self._live_lock = take_lock(self.folder / "live.lock") if folder is None else None
        self.occupancy = {mode.name: float(getattr(mode, "occupancy", 1.0)) for mode in modes}
        self.roads = {road.id: road_record(network, road) for road in network.all_roads()}
        self._limit = {road_id: max(info["limit_kmh"], 5.0) / 3.6 for road_id, info in self.roads.items()}
        self._lock = threading.Lock()
        # id -> [first seen, type, origin, destination, metres, delay s, road, wait s, link, own delay s, people on board]
        self._alive: dict[str, list] = {}
        self._road = {road_id: dict.fromkeys(("entered", "vehicle_s", "vehicle_m", "delay_s", "queue_sum",
                                              "queue_max", "queue_m_max"), 0.0) for road_id in self.roads}
        # Each direction of a road (a link, "road>" from its from-node, "road<" back): entered, time, distance, delay
        self._link: dict[str, dict] = {}
        self._road_minute: dict[str, list] = {}  # road -> [entered, vehicle s, vehicle m, most queued, delay s]
        self._minute_start = 0.0
        self.totals = {"time_s": 0.0, "samples": 0, "on_road": 0, "spawned": 0, "arrived": 0, "trips": 0,
                       "trip_s": 0.0, "trip_delay_s": 0.0, "people": 0.0, "vehicle_s": 0.0, "vehicle_m": 0.0,
                       "delay_s": 0.0, "queued_now": 0, "queued_max": 0, "by_type": {}, "wait_s": 0.0,
                       "trip_wait_s": 0.0, "people_delay_s": 0.0, "queued_sum": 0, "free_delay_s": 0.0,
                       "trip_free_delay_s": 0.0, "person_delay_s": 0.0, "entry_wait_s": 0.0, "waiting": 0,
                       "system_trip_s": 0.0, "measure_from_s": float(measure_from_s), "spawned_before": 0,
                       "arrived_before": 0}
        self._minute = self._fresh_minute()
        self._trips: list[list] = []  # waiting to be appended
        # For the statistics (observed()): every trip, vehicles first seen per (origin, minute), and each road's
        # queue (metres a lane, rounded) at every sample it had one
        self.trip_log: list[tuple] = []  # (origin, destination, type, trip s, delay s, arrived s)
        self.minute_rows: list[list] = []  # the Per minute rows (MINUTE_COLUMNS)
        self.entries: Counter = Counter()
        self.queues: dict[str, Counter] = defaultdict(Counter)
        self.error = ""
        meta = {"name": name, "started": started.isoformat(timespec="seconds"), "sample_s": SAMPLE_S,
                "seed": seed, "model": model,
                "run": [[key, value] for key, value in (run or [])], "inputs": inputs or [],
                "queue_kmh": QUEUE_KMH, "occupancy": self.occupancy, "roads": self.roads,
                "network": {"junctions": len(network.nodes), "roads": len(network.roads),
                            "boundary_ends": len(network.approach_nodes)},
                # Where everything is, for the GeoJSON beside the workbook (scene metres, y down; WGS84 when placed)
                "geometry": {road.id: [[round(x, 2), round(y, 2)] for x, y in network.road_points(road)]
                             for road in network.all_roads()},
                "nodes": {node.id: [round(node.x, 2), round(node.y, 2), node.control if node.id in network.nodes else "end"]
                          for node in network.all_nodes()},
                "georef": georeference(network)}
        (self.folder / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
        with open(self.folder / "minutes.csv", "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(MINUTE_COLUMNS)
        with open(self.folder / "trips.csv", "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(TRIP_COLUMNS)
        with open(self.folder / "roads_minutes.csv", "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(ROAD_MINUTE_COLUMNS)

    @staticmethod
    def _fresh_minute() -> dict:
        return {"arrived": 0, "people": 0.0, "vehicle_s": 0.0, "vehicle_m": 0.0, "queued_max": 0, "delay_s": 0.0}

    def sample(self, frame) -> None:
        """Add a frame of the run (the physics thread, every SAMPLE_S simulated seconds)."""
        with self._lock:
            warm_up = self.totals["measure_from_s"]
            if self.totals["time_s"] <= warm_up < frame.time and warm_up > 0:
                self._measure_from_here()
            totals, minute = self.totals, self._minute
            dt = max(frame.time - totals["time_s"], 0.0)
            queued_at: dict[str, list[float]] = {}
            seen = set()
            for vehicle in frame.vehicles:
                seen.add(vehicle.id)
                alive = self._alive.get(vehicle.id)
                if alive is None:
                    alive = self._alive[vehicle.id] = [frame.time, vehicle.mode, vehicle.origin, vehicle.destination,
                                                       0.0, 0.0, None, 0.0, None, 0.0, None]
                    self.entries[(vehicle.origin, int(frame.time // 60))] += 1
                limit = self._limit.get(vehicle.road) or self._limit.get(alive[6], 50 / 3.6)
                metres, lost = vehicle.speed * dt, dt * max(0.0, 1.0 - vehicle.speed / limit)
                alive[4] += metres
                alive[5] += lost
                # Against its own free speed (the road's limit for a frame without it), times the people on board
                free, people = getattr(vehicle, "free_speed", None) or limit, getattr(vehicle, "people", None)
                alive[10] = people if people is not None else self.occupancy.get(vehicle.mode, 1.0)
                own = 0.0 if getattr(vehicle, "place", "").startswith("bus stop ") else \
                    dt * max(0.0, 1.0 - vehicle.speed / free)
                alive[9] += own
                totals["free_delay_s"] += own
                totals["person_delay_s"] += own * alive[10]
                if vehicle.speed <= WAIT_MPS and not getattr(vehicle, "place", "").startswith("bus stop "):
                    alive[7] += dt  # waiting (a bus at its stop is calling there, not waiting)
                    totals["wait_s"] += dt
                road = self._road.get(vehicle.road)
                if road is not None:
                    by_minute = self._road_minute.setdefault(vehicle.road, [0, 0.0, 0.0, 0, 0.0])
                    if alive[6] != vehicle.road:
                        road["entered"] += 1
                        by_minute[0] += 1
                    road["vehicle_s"] += dt
                    road["vehicle_m"] += metres
                    road["delay_s"] += lost
                    by_minute[1] += dt
                    by_minute[2] += metres
                    by_minute[4] += lost
                    alive[6] = vehicle.road
                place = getattr(vehicle, "place", "")
                if " lane " in place:  # on a lane: which direction of its road
                    link_id = place.rsplit(" lane ", 1)[0]
                    link = self._link.setdefault(link_id, {"entered": 0, "vehicle_s": 0.0, "vehicle_m": 0.0, "delay_s": 0.0})
                    if alive[8] != link_id:
                        link["entered"] += 1
                        alive[8] = link_id
                    link["vehicle_s"] += dt
                    link["vehicle_m"] += metres
                    link["delay_s"] += lost
                if vehicle.speed * 3.6 < QUEUE_KMH:
                    queued_at.setdefault(vehicle.road, []).append(vehicle.length_m + GAP_M)
                totals["vehicle_s"] += dt
                totals["vehicle_m"] += metres
                totals["delay_s"] += lost
                minute["vehicle_s"] += dt
                minute["vehicle_m"] += metres
                minute["delay_s"] += lost
            gone = [vehicle_id for vehicle_id in self._alive if vehicle_id not in seen]  # arrived
            exits = {trip[0]: trip for trip in getattr(frame, "exits", ())} if gone else {}
            for vehicle_id in gone:
                first, kind, origin, destination, metres, lost, _, waited, _, own, people = self._alive.pop(vehicle_id)
                # Exact (Frame.exits); a frame without them: from when it was first and last seen
                _, requested, inserted, exited = exits.get(vehicle_id) or (vehicle_id, first, first, frame.time)
                if inserted < totals["measure_from_s"]:
                    continue  # on the road in the warm-up: not measured
                trip, system = exited - inserted, exited - requested
                self._trips.append([vehicle_id, kind, origin, destination, round(inserted, 1), round(exited, 1),
                                    round(trip, 1), round(metres, 1), round(lost, 1), round(waited, 1), people,
                                    round(own, 1), round(requested, 1), round(system, 1)])
                self.trip_log.append((origin, destination, kind, trip, lost, exited))
                totals["trips"] += 1
                totals["trip_s"] += trip
                totals["system_trip_s"] = totals.get("system_trip_s", 0.0) + system
                totals["trip_delay_s"] += lost
                totals["people"] += people
                totals["trip_wait_s"] += waited
                totals["people_delay_s"] += lost * people
                totals["trip_free_delay_s"] += own
                minute["people"] += people
                by = totals["by_type"].setdefault(kind, {"trips": 0, "people": 0.0, "trip_s": 0.0, "delay_s": 0.0})
                by["trips"] += 1
                by["people"] += people
                by["trip_s"] += trip
                by["delay_s"] += lost
                by["wait_s"] = by.get("wait_s", 0.0) + waited
                by["people_delay_s"] = by.get("people_delay_s", 0.0) + lost * people
                by["free_delay_s"] = by.get("free_delay_s", 0.0) + own
            # Generated but not yet on the road (vehicles are conserved: spawned = waiting + on the road + arrived)
            waiting = max(frame.spawned - frame.arrived - len(frame.vehicles), 0)
            totals["entry_wait_s"] += waiting * dt
            queued = sum(len(lengths) for lengths in queued_at.values())
            for road_id, road in self._road.items():
                lengths = queued_at.get(road_id, ())
                road["queue_sum"] += len(lengths)
                road["queue_max"] = max(road["queue_max"], len(lengths))
                road["queue_m_max"] = max(road["queue_m_max"], sum(lengths) / max(self.roads[road_id]["lanes"], 1))
                if lengths:
                    self.queues[road_id][round(sum(lengths) / max(self.roads[road_id]["lanes"], 1))] += 1
                    by_minute = self._road_minute.setdefault(road_id, [0, 0.0, 0.0, 0, 0.0])
                    by_minute[3] = max(by_minute[3], len(lengths))
            minute["arrived"] += max(frame.arrived - totals["arrived"], 0)
            minute["queued_max"] = max(minute["queued_max"], queued)
            totals["queued_sum"] += queued
            before = totals["time_s"]
            totals.update(time_s=frame.time, samples=totals["samples"] + 1, on_road=len(frame.vehicles),
                          spawned=frame.spawned, arrived=frame.arrived, queued_now=queued, waiting=waiting,
                          queued_max=max(totals["queued_max"], queued))
            if int(frame.time // 60) > int(before // 60):  # a simulated minute is complete
                self._write_minute(frame.time)

    def _measure_from_here(self) -> None:
        """The warm-up is over: every figure starts again from zero at its end (the per-minute rows, vehicles first
        seen per minute and the spawned and arrived totals carry on; trips that entered in it are left out)."""
        old = self.totals
        self.totals = dict.fromkeys(old, 0)
        self.totals.update(by_type={}, time_s=old["measure_from_s"], measure_from_s=old["measure_from_s"],
                           spawned=old["spawned"], arrived=old["arrived"], spawned_before=old["spawned"],
                           arrived_before=old["arrived"])
        for sums in [*self._road.values(), *self._link.values()]:
            sums.update(dict.fromkeys(sums, 0))
        self.trip_log, self.queues = [], defaultdict(Counter)

    def _write_minute(self, now: float) -> None:
        totals, minute = self.totals, self._minute
        speed = minute["vehicle_m"] / minute["vehicle_s"] * 3.6 if minute["vehicle_s"] else 0.0
        row = [round(now, 1), clock(now), totals["on_road"], totals["spawned"], totals["arrived"], minute["arrived"],
               minute["arrived"] * 60, minute["people"], round(minute["people"] * 60, 1), round(speed, 1),
               totals["queued_now"], minute["queued_max"], round(minute["delay_s"] / 3600, 3),
               round(minute["vehicle_m"] / 1000, 3)]
        self.minute_rows.append(row)
        with open(self.folder / "minutes.csv", "a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(row)
        with open(self.folder / "trips.csv", "a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerows(self._trips)
        span = max(now - self._minute_start, 1e-9)  # a whole minute, or the part one at the end
        rows = [[round(now, 1), clock(now), road, entered, round(entered * 3600 / span),
                 round(metres / seconds * 3.6, 1) if seconds else None, queued, round(late / 3600, 3)]
                for road, (entered, seconds, metres, queued, late) in sorted(self._road_minute.items())]
        with open(self.folder / "roads_minutes.csv", "a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerows(rows)
        self._road_minute, self._minute_start = {}, now
        self._trips = []
        self._minute = self._fresh_minute()
        self._save_totals()

    def _save_totals(self) -> None:
        state = {"totals": self.totals, "roads": self._road, "links": self._link, "saved": time.time()}
        part = self.folder / "totals.part"
        part.write_text(json.dumps(state), encoding="utf-8")
        part.replace(self.folder / "totals.json")

    def kpis(self) -> dict:
        """The run's figures so far (any thread)."""
        with self._lock:
            figures = kpis(self.totals, self._road, self.roads, self.occupancy)
            figures.update(reliability([(trip[3], trip[4]) for trip in self.trip_log]))
            return figures

    def observed(self) -> dict:
        """What the run's statistics are worked from (experiments.analyse_run): the trips, vehicles first seen
        per (origin, minute), each road's queue histogram, the samples taken and the roads."""
        with self._lock:
            return {"trips": list(self.trip_log), "entries": dict(self.entries), "samples": self.totals["samples"],
                    "minute_rows": [list(row) for row in self.minute_rows],
                    "minutes": int(self.totals["time_s"] // 60), "roads": self.roads,
                    "queues": {road: dict(counts) for road, counts in self.queues.items()}}

    def add_sheets(self, reports: list[dict]) -> None:
        """The run's extensions' reports (ExtensionsPanel.reports): their sheets go in its workbook, after Trips;
        their files (name, text: TSP's telemetry.jsonl) beside it, named after it."""
        sheets = [[name, rows, what] for report in reports for name, rows, what in report.get("sheets", [])]
        with self._lock:
            (self.folder / "extra.json").write_text(json.dumps(sheets, default=str), encoding="utf-8")
            files = self.folder / "files"
            for report in reports:
                for name, text in report.get("files", []):
                    files.mkdir(exist_ok=True)
                    (files / Path(name).name).write_text(text, encoding="utf-8")

    def add_analysis(self, sheets: list) -> None:
        """The statistics of what the run observed (experiments.analysis_sheets: detectors, link travel times,
        queue discharge, conflicts, bus headways, reliability…): sheets of its workbook, after the extensions'."""
        with self._lock:
            (self.folder / "analysis.json").write_text(json.dumps(sheets, default=str), encoding="utf-8")

    def _files_beside(self, path: Path) -> None:
        """The extensions' files beside the workbook at `path`: "<its name> telemetry.jsonl"."""
        for found in sorted((self.folder / "files").glob("*")):
            shutil.copyfile(found, path.with_name(f"{path.stem} {found.name}"))

    def export(self, path: str | Path) -> Path:
        """The workbook of the run so far, now (the run carries on), and its extensions' files beside it."""
        with self._lock:
            self._flush()
            path = workbook(self.folder, path)
            self._files_beside(path)
            return path

    def _flush(self) -> None:
        """A part-minute since the last whole one, to the files as a row of its own."""
        if self.totals["samples"] and self.totals["time_s"] % 60 > 1e-6:
            self._write_minute(self.totals["time_s"])

    def finish(self, results: Path) -> Path | None:
        """The run ended (Stop, a new Run, a change to the network, closing): its workbook in `results`, the
        working folder cleared. None if it recorded nothing. Its `error` (it ended early, or lost part of what it
        recorded) is on its Run sheet as "Incomplete"."""
        with self._lock:
            if self._live_lock is not None:
                self._live_lock.close()
            if not self.totals["samples"]:
                shutil.rmtree(self.folder, ignore_errors=True)
                return None
            self._flush()
            if self.error:
                meta = json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))
                meta["run"].append(["Incomplete", self.error])
                (self.folder / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
            name = file_name(self.started, seed=self.seed, model=self.model)
            with _naming:
                path = Path(results) / name
                count = 1
                while path.exists():  # runs started in the same second (an experiment's): numbered, none overwritten
                    count += 1
                    path = path.with_name(f"{name[:-5]} ({count}).xlsx")
                path.touch()  # taken: a run finishing alongside numbers its own
            path = workbook(self.folder, path)
            self._files_beside(path)
        shutil.rmtree(self.folder, ignore_errors=True)
        return path


def kpis(totals: dict, road_totals: dict, roads: dict, occupancy: dict) -> dict:
    """The network's KPIs from a run's running totals (measured after the warm-up, measure_from_s)."""
    warm_up = totals.get("measure_from_s", 0.0)
    seconds = totals["time_s"] - warm_up  # measured
    hours = seconds / 3600
    per_hour = (lambda value: value / hours) if hours > 0 else (lambda value: 0.0)
    rows = []
    for road_id, road in road_totals.items():
        info = roads.get(road_id, {})
        rows.append({"road": road_id, "flow_vph": round(per_hour(road["entered"])),
                     "speed_kmh": round(road["vehicle_m"] / road["vehicle_s"] * 3.6, 1) if road["vehicle_s"] else None,
                     "density_veh_km": round(road["vehicle_s"] / max(seconds, 1e-9) / max(info.get("length_m", 1) / 1000, 1e-6), 2),
                     "queue_max": int(road["queue_max"]),
                     "queue_mean": round(road["queue_sum"] / max(totals["samples"], 1), 2),
                     "queue_m_max": round(road["queue_m_max"], 1), "delay_veh_h": round(road["delay_s"] / 3600, 3),
                     "vehicle_km": round(road["vehicle_m"] / 1000, 3),
                     "entered": int(road["entered"])})
    longest = max(rows, key=lambda row: (row["queue_max"], row["queue_m_max"]), default=None)
    trips, people, samples = totals["trips"], totals["people"], totals["samples"]
    people_delay = totals.get("people_delay_s", 0.0)  # (a crashed run's older totals lack the newer figures)
    entry_wait = totals.get("entry_wait_s", 0.0)
    spawned, unfinished = totals["spawned"], totals["on_road"] + totals.get("waiting", 0)
    return {
        "time_s": totals["time_s"], "measure_from_s": warm_up, "spawned": spawned, "arrived": totals["arrived"],
        "on_road": totals["on_road"], "waiting_to_enter": totals.get("waiting", 0),
        # Of the vehicles spawned in the whole run: arrived, and still on the road or waiting to enter
        "completion_rate": round(totals["arrived"] / spawned, 3) if spawned else None,
        "unfinished_share": round(unfinished / spawned, 3) if spawned else None,
        "throughput_vph": round(per_hour(totals["arrived"] - totals.get("arrived_before", 0))),
        "people": round(totals["people"], 1), "people_per_h": round(per_hour(totals["people"])),
        "mean_speed_kmh": round(totals["vehicle_m"] / totals["vehicle_s"] * 3.6, 1) if totals["vehicle_s"] else None,
        "vehicle_km": round(totals["vehicle_m"] / 1000, 2), "vehicle_h": round(totals["vehicle_s"] / 3600, 2),
        "delay_veh_h": round(totals["delay_s"] / 3600, 2),
        "delay_vs_free_speed_veh_h": round(totals.get("free_delay_s", 0.0) / 3600, 2),
        "entry_wait_veh_h": round(entry_wait / 3600, 2),
        "delay_with_entry_wait_veh_h": round((totals["delay_s"] + entry_wait) / 3600, 2),
        "delay_s_per_km": round(totals["delay_s"] / (totals["vehicle_m"] / 1000), 1) if totals["vehicle_m"] > 1 else None,
        "trips": trips, "mean_trip_s": round(totals["trip_s"] / trips, 1) if trips else None,
        "mean_system_trip_s": round(totals.get("system_trip_s", 0.0) / trips, 1) if trips else None,
        "mean_trip_delay_s": round(totals["trip_delay_s"] / trips, 1) if trips else None,
        "mean_trip_delay_vs_free_speed_s": round(totals.get("trip_free_delay_s", 0.0) / trips, 1) if trips else None,
        "mean_wait_s": round(totals.get("trip_wait_s", 0.0) / trips, 1) if trips else None,
        "people_delay_h": round(people_delay / 3600, 2),
        "mean_person_delay_s": round(people_delay / people, 1) if people else None,
        "person_delay_on_board_h": round(totals.get("person_delay_s", 0.0) / 3600, 2),
        "queued_mean": round(totals.get("queued_sum", 0) / samples, 1) if samples else None,
        "queued_now": totals["queued_now"], "queued_max": totals["queued_max"],
        "longest_queue": longest if longest and longest["queue_max"] else None,
        "roads": sorted(rows, key=lambda row: -row["flow_vph"]),
        "by_type": {kind: {"trips": by["trips"], "people": round(by["people"], 1),
                           "people_per_h": round(per_hour(by["people"])),
                           "mean_trip_s": round(by["trip_s"] / by["trips"], 1) if by["trips"] else None,
                           "mean_delay_s": round(by["delay_s"] / by["trips"], 1) if by["trips"] else None,
                           "mean_wait_s": round(by.get("wait_s", 0.0) / by["trips"], 1) if by["trips"] else None,
                           "people_delay_h": round(by.get("people_delay_s", 0.0) / 3600, 2),
                           "mean_delay_vs_free_speed_s": round(by.get("free_delay_s", 0.0) / by["trips"], 1)
                           if by["trips"] else None}
                    for kind, by in sorted(totals["by_type"].items())},
        "occupancy": occupancy,
    }


def _csv(path: Path) -> list[list]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))

    def number(text):
        try:
            value = float(text)
        except ValueError:
            return text
        return int(value) if value.is_integer() and "." not in text else value

    return rows[:1] + [[number(cell) for cell in row] for row in rows[1:]]


def workbook(folder: Path, path: str | Path) -> Path:
    """A run's working folder to an Excel workbook: Summary, Per minute, Roads, By type, Trips."""
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    try:
        state = json.loads((folder / "totals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    try:
        extra = json.loads((folder / "extra.json").read_text(encoding="utf-8"))  # the extensions' sheets
    except (OSError, ValueError):
        extra = []
    try:
        analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))  # what it observed, analysed
    except (OSError, ValueError):
        analysis = []
    roads = meta["roads"]
    if state is None:  # not a minute recorded
        figures = kpis({"time_s": 0.0, "samples": 0, "on_road": 0, "spawned": 0, "arrived": 0, "trips": 0,
                        "trip_s": 0.0, "trip_delay_s": 0.0, "people": 0.0, "vehicle_s": 0.0, "vehicle_m": 0.0,
                        "delay_s": 0.0, "queued_now": 0, "queued_max": 0, "by_type": {}}, {}, roads, meta["occupancy"])
    else:
        figures = kpis(state["totals"], state["roads"], roads, meta["occupancy"])
    longest = figures["longest_queue"]
    summary = [["Figure", "Value", "Unit", "How"],
               ["Run started", meta["started"], "", meta.get("name", "")],
               ["Simulated time", clock(figures["time_s"]), "h:m:s", ""],
               ["Network", f"{meta['network']['junctions']} junctions, {meta['network']['roads']} roads, "
                           f"{meta['network']['boundary_ends']} boundary ends", "", ""],
               ["Vehicles spawned", figures["spawned"], "veh", ""],
               ["Vehicles arrived", figures["arrived"], "veh", "trips completed"],
               ["Vehicles on the road at the end", figures["on_road"], "veh", ""],
               ["Vehicles waiting to enter at the end", figures["waiting_to_enter"], "veh",
                "spawned, not yet on the road: spawned = waiting + on the road + arrived"],
               ["Completion rate", figures["completion_rate"], "", "arrived ÷ spawned, the whole run"],
               ["Unfinished trips", figures["unfinished_share"], "",
                "(on the road + waiting to enter at the end) ÷ spawned: trips the means below leave out"],
               ["Measured from", clock(figures["measure_from_s"]), "h:m:s",
                "the warm-up before it is left out of the figures below (not of the counts above or Per minute)"],
               ["Throughput", figures["throughput_vph"], "veh/h", "arrived ÷ simulated hours"],
               ["People carried", figures["people"], "people",
                "arrived vehicles × the people on board (a bus's simulated load, else its type's occupancy)"],
               ["Passenger throughput", figures["people_per_h"], "people/h", ""],
               ["Mean speed", figures["mean_speed_kmh"], "km/h", "distance ÷ time on the road, every vehicle"],
               ["Distance driven", figures["vehicle_km"], "veh·km", ""],
               ["Time on the road", figures["vehicle_h"], "veh·h", ""],
               ["Delay", figures["delay_veh_h"], "veh·h",
                "time lost against the roads' speed limits, whatever the vehicle (road-limit time loss)"],
               ["Delay per km", figures["delay_s_per_km"], "s/veh·km", ""],
               ["Delay against own free speed", figures["delay_vs_free_speed_veh_h"], "veh·h",
                "time lost against each vehicle's own free speed where it is (speed factor, top and desired speed; a "
                "turn's speed), not at a bus stop"],
               ["Entry wait", figures["entry_wait_veh_h"], "veh·h",
                "vehicles spawned but waiting to enter × the time (SUMO's insertion delay); not in Delay"],
               ["Delay with entry wait", figures["delay_with_entry_wait_veh_h"], "veh·h", "Delay + Entry wait"],
               ["Mean trip time", figures["mean_trip_s"], "s",
                "completed trips, on the road to off it (exact, to the step)"],
               ["Mean system travel time", figures["mean_system_trip_s"], "s",
                "completed trips, requested departure to off the road: the wait to enter included"],
               ["Mean delay per trip", figures["mean_trip_delay_s"], "s", ""],
               ["Mean delay per trip against own free speed", figures["mean_trip_delay_vs_free_speed_s"], "s", ""],
               ["Mean wait per trip", figures["mean_wait_s"], "s",
                f"time at or below {WAIT_MPS:g} m/s, not at a bus stop (SUMO's waitingTime)"],
               ["Passenger delay", figures["people_delay_h"], "person·h", "each completed trip's delay × its people"],
               ["Mean delay per passenger", figures["mean_person_delay_s"], "s", "passenger delay ÷ people carried"],
               ["Person delay on board", figures["person_delay_on_board_h"], "person·h",
                "each sample's delay against own free speed × the people on board then, unfinished trips too"],
               ["Mean queued at once", figures["queued_mean"], "veh", "whole network, over the samples"],
               ["Queued at the end", figures["queued_now"], "veh", f"slower than {meta['queue_kmh']:g} km/h"],
               ["Most queued at once", figures["queued_max"], "veh", "whole network"],
               ["Longest queue", f"{longest['queue_max']} veh on {longest['road']}" if longest else "none", "",
                f"{longest['queue_m_max']:g} m a lane" if longest else ""],
               ["Sampled every", meta["sample_s"], "s", "trip times are exact, not sampled"],
               ["Occupancy assumed", ", ".join(f"{kind} {people:g}" for kind, people in meta["occupancy"].items()),
                "people a vehicle", "an assumption (Mode Behavior's vehicle types)"]]
    road_rows = [["Road", "From", "To", "Length (m)", "Lanes per direction", "Speed limit (km/h)", "Entered (veh)",
                  "Flow (veh/h)", "Mean speed (km/h)", "Density (veh/km)", "Most queued (veh)", "Mean queued (veh)",
                  "Longest queue a lane (m)", "Delay (veh·h)"]]
    for row in figures["roads"]:
        info = roads.get(row["road"], {})
        road_rows.append([row["road"], info.get("from"), info.get("to"), info.get("length_m"), info.get("lanes"),
                          info.get("limit_kmh"), row["entered"], row["flow_vph"], row["speed_kmh"], row["density_veh_km"],
                          row["queue_max"], row["queue_mean"], row["queue_m_max"], row["delay_veh_h"]])
    type_rows = [["Type", "Trips", "People", "People per hour", "Mean trip time (s)", "Mean delay (s)", "Mean wait (s)",
                  "Passenger delay (person·h)", "Occupancy", "Mean delay vs own free speed (s)"]]
    for kind, by in figures["by_type"].items():
        type_rows.append([kind, by["trips"], by["people"], by["people_per_h"], by["mean_trip_s"], by["mean_delay_s"],
                          by["mean_wait_s"], by["people_delay_h"], meta["occupancy"].get(kind, 1.0),
                          by["mean_delay_vs_free_speed_s"]])
    network = [(name, rows) for name, rows in meta.get("inputs", [])]
    run = [["Setting", "Value"], *meta.get("run", []), ["Simulated", clock(figures["time_s"])],
           ["Measured from", clock(figures["measure_from_s"])],
           ["Workbook written", datetime.now().isoformat(timespec="seconds")]]
    conditions = road_conditions(roads, (state or {}).get("links", {}), figures["time_s"])
    reported = [(name, rows) for name, rows, _ in extra] + [(name, rows) for name, rows, _ in analysis]
    path = write_xlsx(path, [("Read me", read_me(meta, [(name, what) for name, _, what in extra + analysis])),
                             ("Summary", summary), ("Run", run),
                             ("Per minute", _csv(folder / "minutes.csv") or [MINUTE_COLUMNS]),
                             ("Roads", road_rows), ("Road conditions", conditions),
                             ("Roads by minute", _csv(folder / "roads_minutes.csv") or [ROAD_MINUTE_COLUMNS]),
                             ("By type", type_rows), ("Trips", _csv(folder / "trips.csv") or [TRIP_COLUMNS]),
                             *reported, *network])
    if meta.get("geometry"):
        write_geojson(path.with_suffix(".geojson"), meta.get("name", ""), roads, meta["geometry"], meta["nodes"],
                      meta.get("georef"), {row["road"]: row for row in figures["roads"]}, conditions)
    return path


CONDITION_COLUMNS = ["Link", "Road", "From", "To", "Road type", "Length (m)", "Lanes", "Speed limit (km/h)",
                     "Entered (veh)", "Flow (veh/h)", "Flow a lane (veh/h)", "Mean speed (km/h)", "Speed (% of limit)",
                     "Level of service", "Travel time (s)", "Free-flow time (s)", "Travel time index", "Density (veh/km)",
                     "Delay (veh·h)", "Delay a vehicle (s)"]


def road_conditions(roads: dict, links: dict, seconds: float) -> list[list]:
    """Each direction of each road over the run: flow, speed against the limit, its level of service
    (LOS_SPEED), travel time against free flow, density and delay."""
    rows = [CONDITION_COLUMNS]
    hours = seconds / 3600
    for link_id, link in sorted(links.items()):
        road_id, forward = link_id[:-1], link_id.endswith(">")
        info = roads.get(road_id)
        if info is None:
            continue
        speed = link["vehicle_m"] / link["vehicle_s"] * 3.6 if link["vehicle_s"] else None
        percent = 100 * speed / max(info["limit_kmh"], 1) if speed is not None else None
        travel = info["length_m"] / (speed / 3.6) if speed else None
        free = info["length_m"] / (max(info["limit_kmh"], 1) / 3.6)
        flow = link["entered"] / hours if hours else 0.0
        rows.append([link_id, road_id, info["from"] if forward else info["to"], info["to"] if forward else info["from"],
                     info.get("type"), info["length_m"], info["lanes"], info["limit_kmh"], link["entered"], round(flow),
                     round(flow / max(info["lanes"], 1)), None if speed is None else round(speed, 1),
                     None if percent is None else round(percent, 1), level_of_service(percent),
                     None if travel is None else round(travel, 1), round(free, 1),
                     None if travel is None else round(travel / free, 2),
                     round(link["vehicle_s"] / max(seconds, 1e-9) / max(info["length_m"] / 1000, 1e-6), 2),
                     round(link["delay_s"] / 3600, 3), round(link["delay_s"] / link["entered"], 1) if link["entered"] else None])
    return rows


def georeference(network) -> dict | None:
    """The map's place under the network (lat, lon at the anchor, its turn), or None with no map placed."""
    background = network.background
    if background.kind != "map":
        return None
    return {"lat": background.lat, "lon": background.lon, "anchor_x": background.anchor_x,
            "anchor_y": background.anchor_y, "rotation": background.map_rotation_deg}


def write_geojson(path: Path, name: str, roads: dict, geometry: dict, nodes: dict, georef: dict | None,
                  results: dict | None = None, conditions: list[list] | None = None) -> Path:
    """The network for a GIS: each road a LineString with what it is and, after a run, how it went (both ways
    together, and each direction's flow, speed and level of service); each junction and boundary end a Point.
    Longitude and latitude (WGS84) when the map is placed under the network, else local metres east and north
    of the scene's origin (stated in the file's `acms_coordinates`)."""
    from .map_tiles import Frame  # noqa: PLC0415

    frame = Frame(georef["lat"], georef["lon"], georef["anchor_x"], georef["anchor_y"], georef["rotation"]) if georef else None

    def point(x, y):
        if frame is None:
            return [round(x, 2), round(-y, 2)]  # north up
        lat, lon = frame.to_wgs84(x, y)
        return [round(lon, 7), round(lat, 7)]

    by_link = {}
    if conditions:
        head = conditions[0]
        for row in conditions[1:]:
            by_link[row[0]] = dict(zip(head, row))
    features = []
    for road_id, points in geometry.items():
        info, done = roads.get(road_id, {}), (results or {}).get(road_id)
        properties = {"kind": "road", "id": road_id, **{k: v for k, v in info.items() if v is not None}}
        if done:
            properties.update({f"run_{k}": v for k, v in done.items() if k != "road"})
        for arrow, side in ((">", "forward"), ("<", "backward")):
            link = by_link.get(road_id + arrow)
            if link:
                properties.update({f"{side}_flow_vph": link["Flow (veh/h)"], f"{side}_speed_kmh": link["Mean speed (km/h)"],
                                   f"{side}_los": link["Level of service"], f"{side}_tti": link["Travel time index"]})
        features.append({"type": "Feature", "properties": properties,
                         "geometry": {"type": "LineString", "coordinates": [point(x, y) for x, y in points]}})
    for node_id, (x, y, control) in nodes.items():
        features.append({"type": "Feature", "properties": {"kind": "boundary end" if control == "end" else "junction",
                                                           "id": node_id, "control": None if control == "end" else control},
                         "geometry": {"type": "Point", "coordinates": point(x, y)}})
    document = {"type": "FeatureCollection", "name": name or "ACMS network",
                "acms_coordinates": "WGS84 longitude, latitude" if frame else
                "local metres east and north of the scene origin (no map placed)", "features": features}
    path = Path(path)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def take_lock(path: Path):
    """A live run's lock: the open file (Windows won't delete it), flock'd on POSIX. Closing it lets go,
    and so does the process ending, however it ends."""
    handle = open(path, "w")  # noqa: SIM115 - held for the run's life
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def lock_held(path: Path) -> bool:
    """Whether a live run (any process) holds this lock; if not, it is removed."""
    if not path.exists():
        return False
    if fcntl is not None:
        with open(path, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        return True
    return False


def recover(results: Path, root: Path = RUNS) -> list[Path]:
    """Workbooks for the runs a crash left in `root` (their folders then cleared)."""
    made = []
    for folder in sorted(root.glob("run *")) if root.exists() else []:
        if not (folder / "meta.json").exists():
            continue
        if lock_held(folder / "live.lock"):
            continue  # another ACMS's run, still going
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            made.append(workbook(folder, Path(results) / file_name(datetime.fromisoformat(meta["started"]), True,
                                                                   meta.get("seed"), meta.get("model") or "")))
        except (OSError, ValueError, KeyError):
            continue
        shutil.rmtree(folder, ignore_errors=True)
    return made

# --- What went into the run: the workbook's input sheets, for whoever checks it -------------------

def _clock_h(hours: float) -> str:
    return f"{int(hours):02d}:{round(hours % 1 * 60):02d}"


def input_sheets(network, demand, modes, plans: dict) -> list[tuple[str, list[list]]]:
    """Everything the run was made from, as sheets (its settings, seed and speed among them, are the
    recorder's `run` rows, the workbook's Run sheet): the junctions with their signal settings, the roads, the
    boundary ends, the demand pair by pair, the vehicle types with every parameter, the signal plans the
    run worked out, bus stops and institutions. Plain values only (kept as JSON in the run's folder)."""
    names = [mode.name for mode in modes]
    from .roundabouts import design_roundabout  # noqa: PLC0415

    meeting: dict[str, int] = defaultdict(int)
    stops_on: dict[str, int] = defaultdict(int)
    for road in network.all_roads():
        meeting[road.from_node] += 1
        meeting[road.to_node] += 1
    for stop in network.bus_stops.values():
        stops_on[stop.road] += 1
    junctions = [["Junction", "x (m)", "y (m)", "Control", "Roads meeting", "Roundabout diameter (m)", "Interchange",
                  "Signal phases", "Timing method", "Green times (s)", "Cycle min (s)", "Cycle max (s)",
                  "Target saturation", "Stop weight (s)"]]
    for node in network.nodes.values():
        signalised = node.control == "signalised"
        diameter = round(design_roundabout(network, node.id).inscribed_diameter_m, 1) if node.control == "roundabout" else None
        interchange = next((name for name in network.interchanges if node.id.startswith(f"{name}.")), None)
        junctions.append([node.id, round(node.x, 2), round(node.y, 2), node.control, meeting[node.id], diameter,
                          interchange, node.signal_phases if signalised else None, node.signal_method if signalised else None,
                          ", ".join(f"{green:g}" for green in node.green_times_s) if signalised else None,
                          node.cycle_min_s if signalised else None, node.cycle_max_s if signalised else None,
                          node.target_saturation if signalised else None, node.stop_weight_s if signalised else None])
    roads = [["Road", "From", "To", "Length (m)", "Lanes per direction", "Direction", "Speed limit (km/h)",
              "Lane changing", "Road type", "Overpass", "Boundary road", "Width (m)", "Tightest curve radius (m)",
              "Safe curve speed (km/h)", "Posted over curve speed", "Bus stops", "Shape (WKT, scene m, y down)"]]
    for road in network.all_roads():
        record = road_record(network, road)
        shape = "LINESTRING (" + ", ".join(f"{x:.2f} {y:.2f}" for x, y in network.road_points(road)) + ")"
        roads.append([road.id, road.from_node, road.to_node, round(road.length_m, 2), road.lanes_per_direction,
                      road.direction, road.speed_limit_kmh, road.lane_changing, road.road_type, road.overpass,
                      record["boundary"], record["width_m"], record["tightest_radius_m"], record["curve_kmh"],
                      bool(record["curve_kmh"] and road.speed_limit_kmh > record["curve_kmh"]), stops_on[road.id], shape])
    ends = [["Boundary end", "x (m)", "y (m)", "Supply (veh/h)", "Demand (veh/h)"]]
    ends += [[end.id, round(end.x, 2), round(end.y, 2), end.supply_vph, end.demand_vph]
             for end in network.approach_nodes.values()]
    daily = any(pair.hourly is None for pair in demand)
    pairs = [["Origin", "Destination", "Hourly (veh/h)", "Daily (trips/day)", "Arrival pattern",
              *(f"{name} (%)" for name in names), *(f"Share {hour:02d}:00" for hour in range(24) if daily)]]
    for pair in demand:
        shares = list(pair.shares) if pair.hourly is None else []
        pairs.append([pair.origin, pair.destination, pair.hourly, pair.daily, pair.pattern, *pair.split,
                      *([round(share, 6) for share in shares] + [None] * (24 - len(shares)) if daily else [])])
    kinds = [["Vehicle type", "Headway (min; 0: no timetable)", "Occupancy (people)", "Length (m)", "Width (m)",
              "Top speed (km/h)", "Desired top speed (km/h)", "Acceleration (m/s²)", "Comfortable braking (m/s²)",
              "Emergency braking (m/s²)", "Standstill gap (m)", "Reaction time τ (s)", "Imperfection σ", "Speed deviation", "Start-up delay (s)",
              "Colour"]]
    for mode in modes:
        kinds.append([mode.name, mode.headway_min, getattr(mode, "occupancy", 1.0), mode.length_m, mode.width_m,
                      mode.max_speed_kmh, None if mode.desired_max_kmh >= 1e4 else mode.desired_max_kmh, mode.accel,
                      mode.decel, getattr(mode, "emergency_decel", None), mode.min_gap_m, mode.tau_s, mode.sigma, mode.speed_dev, mode.startup_delay_s, mode.colour])
    signals = [["Junction", "Method", "Cycle (s)", "Green times (s)", "Offset (s)", "Group", "Other figures"]]
    for node_id, plan in sorted(plans.items()):
        greens = plan.get("greens_s") or plan.get("green_times_s") or plan.get("greens")
        rest = {key: value for key, value in plan.items()
                if key not in ("method", "cycle_s", "greens_s", "green_times_s", "greens", "offset_s", "group")}
        signals.append([node_id, plan.get("method"), plan.get("cycle_s"),
                        ", ".join(f"{float(green):g}" for green in greens) if isinstance(greens, (list, tuple)) else greens,
                        plan.get("offset_s"), plan.get("group"), json.dumps(rest, default=str) if rest else None])
    stops = [["Bus stop", "Road", "Distance from its from-node (m)", "Serves", "Dwell (s)", "Boarding (passengers/h)",
              "Alighting (% of load)"]]
    stops += [[stop.id, stop.road, round(stop.distance_m, 2), stop.direction, stop.dwell_s, stop.boarding_per_h,
               stop.alighting_percent] for stop in network.bus_stops.values()]
    buildings = [["Institution", "Kind", "x (m)", "y (m)", "Area (m²)", "Circle of influence (m)", "People",
                  "Employees", "Shift", "By car (%)"]]
    buildings += [[item.id, item.kind, round(item.x, 2), round(item.y, 2), item.area_m2, round(item.radius_m, 2),
                   item.people, item.employees, f"{_clock_h(item.shift_start_h)}-{_clock_h(item.shift_end_h)}",
                   item.car_share] for item in network.institutions.values()]
    return [("City", city_sheet(network)), ("Junctions", junctions),
            ("Road network", roads), ("Boundary ends", ends), ("Demand", pairs), ("Vehicle types", kinds),
            ("Signal plans", signals), ("Bus stops", stops), ("Institutions", buildings)]


def network_workbook(path, network, demand, modes, settings: list | None = None) -> Path:
    """The network alone, for analysis before any run: a Read me, the City sheet, the inputs (junctions, roads
    with geometry and curves, ends, demand, vehicle types, bus stops, institutions), its settings, and the
    GeoJSON beside it."""
    sheets = input_sheets(network, demand, modes, {})
    read = [["ACMS network", ""], ["Written", datetime.now().isoformat(timespec="seconds")], ["", ""],
            ["Sheet", "What it holds"], ["City", "The network as a whole: place, extent, road length by type, speed and "
                                                 "lanes, control mix, densities, graph indices, roundabouts, curves."],
            *[[name, text] for name, text in READ_ME if name in dict(sheets) and name != "City"],
            ["Settings", "The editor's settings when it was exported."],
            ["", ""], ["GeoJSON", "Beside this workbook: every road (LineString) and junction (Point) with its "
                                  "attributes, in WGS84 when the map is placed, else local metres."]]
    path = write_xlsx(path, [("Read me", read), *[(n, r) for n, r in sheets if n != "Signal plans"],
                             ("Settings", [["Setting", "Value"], *[[k, v] for k, v in settings or []]])])
    roads = {road.id: road_record(network, road) for road in network.all_roads()}
    write_geojson(path.with_suffix(".geojson"), "ACMS network", roads,
                  {road.id: network.road_points(road) for road in network.all_roads()},
                  {node.id: (node.x, node.y, node.control if node.id in network.nodes else "end") for node in network.all_nodes()},
                  georeference(network))
    return path


def city_sheet(network) -> list[list]:
    """The network as a whole, its style: where it is, its extent, how much road of each kind, speed and width,
    how its junctions are controlled, how dense and connected it is (graph indices; Rodrigue, The Geography of
    Transport Systems), its roundabouts, curves, bus stops and buildings, and its route choice."""
    from statistics import median  # noqa: PLC0415

    from .network_tools import network_indices  # noqa: PLC0415 - network_tools imports nothing from here
    from .roundabouts import design_roundabout  # noqa: PLC0415

    rows = [["Figure", "Value", "Unit", "How"]]

    def add(figure, value, unit="", how=""):
        rows.append([figure, value, unit, how])

    background = network.background
    add("Background", {"map": f"OpenStreetMap at {background.lat:.6f}, {background.lon:.6f}",
                       "image": f"image {background.image}", "colour": "plain colour"}.get(background.kind, background.kind))
    add("Driving side", network.driving_side)
    nodes = list(network.all_nodes())
    if nodes:
        xs, ys = [n.x for n in nodes], [n.y for n in nodes]
        width, height = (max(xs) - min(xs)) / 1000, (max(ys) - min(ys)) / 1000
        add("Extent", f"{width:.2f} x {height:.2f}", "km", "the junctions' and ends' bounding box")
    else:
        width = height = 0.0
    area = width * height
    inner = list(network.roads.values())
    all_roads = list(network.all_roads())
    km = sum(road.length_m for road in all_roads) / 1000
    lane_km = sum(road.length_m * road.lanes_per_direction * (2 if road.direction == "two-way" else 1)
                  for road in all_roads) / 1000
    add("Junctions", len(network.nodes))
    for control in ("signalised", "roundabout", "uncontrolled"):
        count = sum(node.control == control for node in network.nodes.values())
        add(f"Junctions {control}", count, "", f"{100 * count / max(len(network.nodes), 1):.0f} %")
    methods = defaultdict(int)
    for node in network.nodes.values():
        if node.control == "signalised":
            methods[node.signal_method] += 1
    add("Signal timing methods", ", ".join(f"{m} {n}" for m, n in sorted(methods.items())) or "none")
    add("Boundary ends", len(network.approach_nodes))
    add("Roads", len(inner), "", "between junctions (boundary roads apart)")
    add("Road length", round(km, 2), "km", "centre lines, boundary roads included")
    add("Lane length", round(lane_km, 2), "lane-km", "every lane of every direction")
    add("One-way", f"{100 * sum(r.length_m for r in all_roads if r.direction != 'two-way') / max(km * 1000, 1e-9):.0f} %",
        "of road length")
    if inner:
        lengths = sorted(road.length_m for road in inner)
        add("Mean road length (block)", round(sum(lengths) / len(lengths), 1), "m", f"median {median(lengths):.1f} m")
    if area:
        add("Junction density", round(len(network.nodes) / area, 1), "per km²", "over the extent")
        add("Road density", round(km / area, 2), "km per km²", "over the extent")
    for label, key in (("Road length by type", lambda r: r.road_type), ("Road length by speed limit (km/h)",
                                                                          lambda r: f"{r.speed_limit_kmh:g}"),
                       ("Road length by lanes each way", lambda r: str(r.lanes_per_direction))):
        totals = defaultdict(float)
        for road in all_roads:
            totals[key(road)] += road.length_m / 1000
        add(label, ", ".join(f"{k} {v:.2f}" for k, v in sorted(totals.items(), key=lambda item: -item[1])), "km")
    curved = [road for road in all_roads if road.curvature and max(abs(k) for k in road.curvature)]
    add("Curved roads", len(curved), "", "with a stored curvature")
    if curved:
        tightest = min(1 / max(abs(k) for k in road.curvature) for road in curved)
        add("Tightest curve radius", round(tightest, 1), "m")
        records = [road_record(network, road) for road in curved]
        add("Roads posted faster than their curve allows",
            sum(record["curve_kmh"] and record["limit_kmh"] > record["curve_kmh"] for record in records))
    round_ = [design_roundabout(network, n.id).inscribed_diameter_m for n in network.nodes.values() if n.control == "roundabout"]
    if round_:
        add("Roundabout diameters", f"{min(round_):.0f}-{max(round_):.0f}", "m", f"mean {sum(round_) / len(round_):.0f} m")
    add("Interchanges", len(network.interchanges), "", ", ".join(sorted(network.interchanges)))
    add("Overpasses", sum(road.overpass for road in all_roads))
    add("Bus stops", len(network.bus_stops))
    add("Institutions", len(network.institutions))
    choice = network.route_choice
    add("Route choice", "fastest route for every vehicle" if not choice else
        f"path-size logit, {choice.get('routes', 3)} routes, theta {choice.get('theta_per_min', 0.5):g}/min, "
        f"{len(choice.get('link_times_s', {}))} measured link times")
    if network.nodes:
        indices = network_indices(network)
        for key, label, how in (("components", "Connected parts", ""), ("cyclomatic", "Cyclomatic number", "e - v + p"),
                                ("alpha", "Alpha index", "circuits / most possible (planar)"),
                                ("beta", "Beta index", "roads a node"), ("gamma", "Gamma index", "roads / most possible"),
                                ("diameter_links", "Diameter", "most links between two nodes"),
                                ("detour_index", "Detour index", "route km / straight-line km between ends")):
            add(label, indices[key], "", how)
        add("Junction degrees", ", ".join(f"{d} roads: {n}" for d, n in indices["degrees"].items()))
    return rows


READ_ME = [
    ("Summary", "The run's KPIs: each figure, its unit and how it is worked out."),
    ("Run", "The run's settings: when, the random seed, the time step, the speed, what was shown, the extensions "
            "ticked and their settings, the background, the network file."),
    ("Per minute", "The network each simulated minute: vehicles on the road, arrivals and throughput, people, mean "
                   "speed, queued vehicles, delay and distance in that minute."),
    ("Roads", "Each road over the run: vehicles entering, flow, mean speed, density, queues and delay."),
    ("Road conditions", "Each direction of each road (link '>' from its from-node, '<' back): flow, flow a lane, mean "
                        "speed and its share of the limit, level of service, travel time against free flow, density, "
                        "delay. The GeoJSON beside the workbook carries the same on a map."),
    ("Roads by minute", "Each road each simulated minute: vehicles entering, flow, mean speed, most queued, delay."),
    ("By type", "Each vehicle type: trips, people, trip time, delay, wait and its passengers' delay."),
    ("Trips", "Every completed trip measured: vehicle, type, origin, destination, when it got on the road and off it, "
              "time, distance, delay, wait, people, when it asked to depart and its system travel time (exact)."),
    ("City", "Input: the network as a whole: place, extent, road length by type, speed limit and lanes, one-way "
             "share, junction control and timing methods, densities, graph indices, roundabouts, curves, stops."),
    ("Junctions", "Input: each junction's place, control, roads meeting, roundabout diameter, interchange, and its "
                  "signal settings as set in the editor."),
    ("Road network", "Input: each road's ends, length, lanes, direction, speed limit, lane changing, type, width, "
                     "tightest curve and the speed it allows, bus stops, and its shape (WKT, scene metres, y down)."),
    ("Boundary ends", "Input: where traffic enters and leaves, with the supply and demand set on each."),
    ("Demand", "Input: every OD pair with demand at Run: its flow (veh/h) or trips a day with the day's hourly "
               "shares, its arrival pattern and the split between vehicle types."),
    ("Vehicle types", "Input: every parameter of each vehicle type (car following after Krauss, SUMO's defaults), "
                      "and its assumed occupancy."),
    ("Signal plans", "Input: the timing each signalised junction ran with, as its method worked it out at Run."),
    ("Bus stops", "Input: the bus stops and their dwell times."),
    ("Institutions", "Input: buildings placed on the map (their trips are not spawned by a run yet)."),
]
DEFINITIONS = [
    ("Throughput (veh/h)", "vehicles that completed their trip ÷ simulated hours (both after Measured from)"),
    ("Trip time (s)", "a completed trip's road travel time: off its last lane − onto its first (exact, to the 0.5 s step)"),
    ("System travel time (s)", "off its last lane − its requested departure: the trip time plus its wait to enter"),
    ("Completion rate", "vehicles arrived ÷ spawned; 1 − it, the unfinished share, is still on the road or waiting to "
                        "enter (the trip means count completed trips only)"),
    ("People (people/h)", "completed trips × the people on board (a bus's simulated load, else its type's occupancy) "
                          "÷ simulated hours"),
    ("Mean speed (km/h)", "distance driven ÷ time on the road, over every vehicle (space-mean speed)"),
    ("Delay (veh·h)", "Σ over vehicles and samples of dt × max(0, 1 − speed ÷ the road's speed limit): road-limit time "
                      "loss, the same limit for every vehicle (a bicycle at its own 20 km/h loses time on a 50 km/h road)"),
    ("Delay vs own free speed", "Σ over a vehicle's samples of dt × max(0, 1 − speed ÷ its own free speed there: the "
                                "lane's limit or the turn's speed × its speed factor, no faster than its top and desired "
                                "speeds), not while calling at a bus stop (SUMO's timeLoss)"),
    ("Entry wait (veh·h)", "Σ over samples of dt × the vehicles spawned but not yet able to enter (spawned − arrived − "
                           "on the road): demand held off the network, which Delay leaves out"),
    ("Person delay on board (person·h)", "Σ over vehicles and samples of their delay vs own free speed × the people "
                                         "on board then (a bus's simulated load, else its type's occupancy)"),
    ("Wait (s)", f"Σ over a vehicle's samples of dt while at or below {WAIT_MPS:g} m/s, not at a bus stop (SUMO's "
                 "tripinfo waitingTime)"),
    ("Passenger delay (person·h)", "Σ over completed trips of the trip's delay × the people on board as it arrived"),
    ("Queued (veh)", f"vehicles slower than {QUEUE_KMH:g} km/h at a sample"),
    ("Queue length a lane (m)", f"Σ (vehicle length + {GAP_M:g} m) of a road's queued vehicles ÷ its lanes per direction"),
    ("Flow on a road (veh/h)", "vehicles seen on it after another road ÷ simulated hours (a road crossed between two "
                               "samples is missed)"),
    ("Density (veh/km)", "vehicle-seconds on the road ÷ simulated seconds ÷ its length in km"),
    ("Level of service", "a road direction's mean speed as a share of its speed limit (free flow): A over 85 %, B over "
                         "67 %, C over 50 %, D over 40 %, E over 30 %, F at or below (HCM 2010 urban street segments)"),
    ("Travel time index", "travel time ÷ free-flow time (length ÷ speed limit)"),
    ("Sampling", f"every {SAMPLE_S:g} simulated seconds; trip times and totals of spawned and arrived vehicles are exact; detectors, "
                 "link times, conflicts and queue discharge are recorded every 0.5 s step"),
]


def read_me(meta: dict, reported: list[tuple[str, str]] = ()) -> list[list]:
    """The first sheet: what the workbook is, each sheet, the definitions and how to repeat the run.
    `reported`: the extensions' sheets, (name, what it holds)."""
    run = dict(meta.get("run", []))
    rows = [["ACMS run results", ""],
            ["Run started", meta["started"]], ["Workbook written", datetime.now().isoformat(timespec="seconds")],
            ["Random seed", run.get("Random seed", "")],
            ["AI model", meta.get("model") or "none"],
            ["To repeat it", "Open the network (Run sheet: Network file) in ACMS, enter the Demand sheet's pairs, keep the "
                             "vehicle types and signal settings listed, and Run with the same seed: the run repeats "
                             "exactly."], ["", ""], ["Sheet", "What it holds"]]
    rows += [[name, text] for name, text in READ_ME[:6]] + [[name, text] for name, text in reported]
    rows += [[name, text] for name, text in READ_ME[6:]]
    rows += [["", ""], ["Figure", "Definition"]] + [[name, text] for name, text in DEFINITIONS]
    rows += [["", ""], ["Assumptions", "Occupancy (people a vehicle): " + ", ".join(
        f"{kind} {people:g}" for kind, people in meta["occupancy"].items()) + ". The simulation is SUMO-style: Krauss "
        "car following, LC2013 lane changing (see ACMS's ARCHITECTURE.md)."]]
    return rows
