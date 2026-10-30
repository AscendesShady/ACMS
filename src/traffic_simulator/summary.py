"""View Summary tab: the project at a glance, in four tabs.

- Network: layout, boundary flows and the design check, whose findings link to the map.
- Demand: totals, the network's 24-hour profile of daily demand (a bar chart), mode splits,
  the OD matrix checked against the ends' supply and demand, and the busiest pairs.
- Results: the network's KPIs from the run going on, or the last one (results.py): throughput,
  people carried, speed, delay, trip times, queues, the busiest roads and longest queues, by type,
  and where its Excel workbook is.
- AI Usage: what System 2 is doing, and this session's requests: timings and the token
  counts the providers report; for local models, what Ollama holds in memory and VRAM.

Each page is Markdown built by a plain function (network_markdown, demand_markdown,
results_markdown, ai_markdown); SummaryPanel rebuilds the open page whenever it's shown or the project changes.
"""

from __future__ import annotations

import time
from collections import Counter
from urllib.parse import quote

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QTabWidget, QTextBrowser, QVBoxLayout, QWidget

from . import assistant, theme
from .daily_profile import HOURS
from .models import Network
from .design_checks import analyse_network

INTERCHANGES = {"TR": "trumpet", "DI": "diamond", "SP": "SPUI"}  # node ID prefixes from interchanges.py
FINDINGS = {  # analyse_network key -> how the summary names it
    "junctions_traffic_cannot_reach": "Junctions traffic can't reach",
    "junctions_traffic_cannot_leave": "Junctions traffic can't leave",
    "dead_end_junctions": "Dead-end junctions",
    "unconnected_junctions": "Unconnected junctions",
    "roads_under_15_m": "Roads under 15 m",
    "curves_posted_too_fast": "Curves posted faster than their radius allows",
    "roundabouts_crowding_their_roads": "Roundabouts too big for their roads",
}
RECENT = 8  # requests listed on the AI Usage page


def _lines(rows: list[tuple[str, str]]) -> list[str]:
    """Labelled lines: they read better than a table in the narrow sidebar."""
    return [*(f"- **{name}:** {value}" for name, value in rows), ""]


def _link(object_id: str) -> str:
    """A link that selects the object on the map (see _Page)."""
    return f"[{object_id}](select:{quote(object_id, safe='')})"


def _split(cells: list, value, names: list[str]) -> str:
    """Mode shares weighted by each pair's trips."""
    total = sum(value(cell) for cell in cells)
    if not total:
        return "—"
    shares = [sum(value(cell) * cell.split[i] for cell in cells) / total for i in range(len(names))]
    return " · ".join(f"{name} {share:.0f}%" for name, share in zip(names, shares))


def network_markdown(network: Network) -> str:
    roads, ends = list(network.all_roads()), list(network.approach_nodes.values())
    controls = Counter(node.control for node in network.nodes.values())
    length_km = sum(road.length_m for road in roads) / 1000
    lane_km = sum(road.length_m * road.lanes for road in roads) / 1000
    speeds = sorted({road.speed_limit_kmh for road in roads})
    built = sorted({node_id.split(".")[0] for node_id in network.nodes
                    if "." in node_id and node_id[:2] in INTERCHANGES})
    points = [(node.x, node.y) for node in network.all_nodes()]
    width = (max(x for x, _ in points) - min(x for x, _ in points)) / 1000 if points else 0
    height = (max(y for _, y in points) - min(y for _, y in points)) / 1000 if points else 0
    supply, demand = sum(end.supply_vph for end in ends), sum(end.demand_vph for end in ends)
    lines = [f"**{len(network.nodes)} junctions · {len(network.roads)} roads · {length_km:.1f} km · "
             f"{len(ends)} boundary ends**", "", "### Layout", "", *_lines([
                 ("Grid", f"{network.horizontal_corridors} × {network.vertical_corridors} corridors, "
                          f"{network.default_spacing:g} m apart, driving on the {network.driving_side}"),
                 ("Junctions", f"{controls['signalised']} signalised, {controls['roundabout']} roundabout, "
                               f"{controls['uncontrolled']} uncontrolled"),
                 ("Roads", f"{len(network.roads)} connecting and {len(network.approach_roads)} boundary: "
                           f"{sum(road.direction == 'one-way' for road in roads)} one-way, "
                           f"{sum(bool(road.shape) for road in roads)} curved, "
                           f"{sum(road.overpass for road in roads)} overpass"),
                 ("Length", f"{length_km:.2f} km of road, {lane_km:.1f} lane-km"),
                 ("Speed limits", "—" if not speeds else f"{speeds[0]:g} km/h" if len(speeds) == 1
                  else f"{speeds[0]:g}–{speeds[-1]:g} km/h"),
                 ("Interchanges", ", ".join(f"{name} ({INTERCHANGES[name[:2]]})" for name in built) or "none"),
                 ("Extent", f"{width:.2f} × {height:.2f} km"),
             ]),
             "### Boundary ends", "", *_lines([
                 ("Ends", f"{len(ends)}: {', '.join(sorted(end.id for end in ends)) or 'none'}"),
                 ("Supply (entering)", f"{supply:g} veh/h"),
                 ("Demand (leaving)", f"{demand:g} veh/h" + (f", {abs(supply - demand):g} veh/h off balance"
                                                             if supply != demand else "")),
             ])]

    findings = analyse_network(network)
    problems = []
    if findings["valid"] is not True:
        problems.append(f"- **Invalid network:** {findings['valid']}")
    if findings["separate_parts"] > 1:
        problems.append(f"- **The network is in {findings['separate_parts']} separate parts**")
    for key, label in FINDINGS.items():
        items = findings.get(key) or []
        names = [item if isinstance(item, str) else item.get("road") or item.get("roundabout") for item in items]
        if names:
            problems.append(f"- **{label}:** {', '.join(_link(name) for name in names)}")
    count = len(problems)
    lines += [f"### Design check: {count or 'no'} problem{'' if count == 1 else 's'}", ""]
    lines += ["Click a name to select it on the map.", "", *problems] if problems else ["Nothing to fix."]
    if findings.get("note"):
        lines += ["", f"*{findings['note']}*"]
    return "\n".join(lines)


def demand_markdown(network: Network, cells: dict, origins: list[str], destinations: list[str],
                    names: list[str] = ("Bus", "Car", "Bike")) -> tuple[str, list[float] | None]:
    """The Demand page, and the daily demand in each hour of the day (None without any).
    `names` are the vehicle types (Mode Behavior's list) the mode splits refer to."""
    pairs = [(o, d) for o in origins for d in destinations if o != d]
    hourly = {pair: cells[pair] for pair in pairs if pair in cells and cells[pair].flow is not None}
    daily = {pair: cells[pair] for pair in pairs if pair in cells and cells[pair].daily is not None}
    lines = [f"**Hourly: {len(hourly)} of {len(pairs)} pairs · {sum(c.flow for c in hourly.values()):g} veh/h**",
             "", f"**Daily: {len(daily)} of {len(pairs)} pairs · {sum(c.daily for c in daily.values()):g} veh/day**",
             "", "### Daily demand over 24 hours", ""]
    by_hour = None
    if daily:
        by_hour = [sum(cell.daily * cell.profile.shares()[hour] for cell in daily.values()) for hour in range(HOURS)]
        peak = max(range(HOURS), key=by_hour.__getitem__)
        low = min(range(HOURS), key=by_hour.__getitem__)
        lines += ["![Daily demand in each hour](chart:hourly)", "",
                  f"Busiest hour **{peak:02d}:00–{peak + 1:02d}:00 ≈ {by_hour[peak]:.0f} veh/h** · quietest "
                  f"{low:02d}:00–{low + 1:02d}:00 ≈ {by_hour[low]:.0f} veh/h", ""]
    else:
        lines += ["*No daily demand yet: pick Daily demand in the Demand Generator tab and enter trips "
                  "per day.*", ""]
    lines += ["### Mode split", "", *_lines([
        ("Hourly", _split(list(hourly.values()), lambda cell: cell.flow, names)),
        ("Daily", _split(list(daily.values()), lambda cell: cell.daily, names)),
    ])]

    lines += ["### OD matrix vs boundary ends", "",
              "Each end's hourly OD totals against the supply and demand set on its arrows.", ""]
    rows = []
    for end in sorted(network.approach_nodes.values(), key=lambda node: node.id):
        out = sum(cell.flow for (origin, _), cell in hourly.items() if origin == end.id)
        into = sum(cell.flow for (_, destination), cell in hourly.items() if destination == end.id)
        if out or into or end.supply_vph or end.demand_vph:
            matches = abs(out - end.supply_vph) < 0.5 and abs(into - end.demand_vph) < 0.5
            rows.append(f"| {end.id} | {out:g} / {end.supply_vph:g} | {into:g} / {end.demand_vph:g} | "
                        f"{'OK' if matches else '**Check**'} |")
    if rows:
        off = sum("**Check**" in row for row in rows)
        lines += [f"**{off} of {len(rows)} ends don't match.**" if off else f"All {len(rows)} ends match.", "",
                  "| End | OD out / supply | OD in / demand | |", "|---|---|---|---|", *rows, ""]
    else:
        lines += ["*No hourly OD demand or end flows yet.*", ""]

    def busiest(group: dict, value, unit: str) -> str:
        top = sorted(group.items(), key=lambda item: -value(item[1]))[:3]
        return " · ".join(f"{o} → {d} {value(cell):g} {unit}" for (o, d), cell in top) or "—"

    patterns = Counter(cell.pattern for cell in [*hourly.values(), *daily.values()])
    lines += ["### Busiest pairs", "", *_lines([
        ("Hourly", busiest(hourly, lambda cell: cell.flow, "veh/h")),
        ("Daily", busiest(daily, lambda cell: cell.daily, "veh/day")),
        ("Arrival patterns", ", ".join(f"{name} {count}" for name, count in patterns.most_common()) or "—"),
    ]), "*Placeholder: the OD matrix isn't saved or simulated yet.*"]
    return "\n".join(lines), by_hour


def _value(value, unit: str = "") -> str:
    if value is None:
        return "—"
    shown = f"{value:,.1f}".rstrip("0").rstrip(".") if isinstance(value, float) else f"{value:,}"
    return f"{shown} {unit}".strip()


def results_markdown(results: dict | None) -> str:
    """The Results page: a run's KPIs (results.kpis), live while it runs."""
    if not results:
        return ("# Results\n\nNo run yet. Press **Run** (the menu bar's right corner): the network's figures "
                "appear here as it goes, and when it ends (Stop, a new Run, a change to the network, closing) "
                "they are saved as an Excel workbook in Documents\\ACMS results. **Export** saves one at any "
                "time.\n")
    k = results["kpis"]
    from .results import clock  # noqa: PLC0415 - results.py has no Qt; kept local to this page
    state = "Running" if results["running"] else "Last run"
    lines = ["# Results", "", f"**{state}: {clock(k['time_s'])} simulated · {_value(k['throughput_vph'], 'veh/h')} · "
             f"{_value(k['people_per_h'], 'people/h')}**", ""]
    if results.get("error"):
        lines += [f"**{results['error']}**", ""]
    longest = k["longest_queue"]
    lines += ["## Traffic", ""] + _lines([
        ("Vehicles spawned", _value(k["spawned"])), ("Trips completed", _value(k["arrived"])),
        ("On the road now" if results["running"] else "On the road at the end", _value(k["on_road"])),
        ("Throughput", _value(k["throughput_vph"], "veh/h")),
        ("People carried", f"{_value(k['people'])} ({_value(k['people_per_h'], 'people/h')})"),
        ("Mean speed", _value(k["mean_speed_kmh"], "km/h")),
        ("Distance driven", _value(k["vehicle_km"], "veh·km")), ("Time on the road", _value(k["vehicle_h"], "veh·h")),
        ("Delay", f"{_value(k['delay_veh_h'], 'veh·h')} ({_value(k['delay_s_per_km'], 's/veh·km')})"),
        ("Mean trip", f"{_value(k['mean_trip_s'], 's')}, of which delay {_value(k['mean_trip_delay_s'], 's')}"),
    ])
    lines += ["## Queues", ""] + _lines([
        ("Queued now" if results["running"] else "Queued at the end", _value(k["queued_now"], "veh")),
        ("Most queued at once", _value(k["queued_max"], "veh")),
        ("Longest queue", f"{longest['queue_max']} veh on {_link(longest['road'])}, {_value(longest['queue_m_max'], 'm')} a lane"
         if longest else "none"),
    ])
    queues = sorted((row for row in k["roads"] if row["queue_max"]), key=lambda row: -row["queue_max"])[:8]
    if queues:
        lines += ["**Longest queues**", ""] + [
            f"{n}. {_link(row['road'])}: {row['queue_max']} veh at most, {_value(row['queue_mean'])} on average, "
            f"{_value(row['queue_m_max'], 'm')} a lane" for n, row in enumerate(queues, 1)] + [""]
    busiest = [row for row in k["roads"] if row["flow_vph"]][:8]
    if busiest:
        lines += ["## Busiest roads", ""] + [
            f"{n}. {_link(row['road'])}: {_value(row['flow_vph'], 'veh/h')} at {_value(row['speed_kmh'], 'km/h')}, "
            f"{_value(row['density_veh_km'], 'veh/km')}" for n, row in enumerate(busiest, 1)] + [""]
    if k["by_type"]:
        lines += ["## By vehicle type", ""] + [
            f"- **{kind}:** {_value(by['trips'])} trips, {_value(by['people'])} people "
            f"({_value(by['people_per_h'], 'people/h')}), trip {_value(by['mean_trip_s'], 's')}, delay "
            f"{_value(by['mean_delay_s'], 's')}" for kind, by in k["by_type"].items()] + [""]
    lines += ["People are vehicles × each type's occupancy, an assumption: " +
              ", ".join(f"{kind} {people:g}" for kind, people in k["occupancy"].items()) +
              ". Sampled every 5 s: trip times are within a sample, and delay is time lost against the roads' "
              "speed limits.", ""]
    lines += [f"Saved: `{results['file']}`" if results.get("file") else
              "When the run ends its workbook is saved in Documents\\ACMS results; Export saves one now.", ""]
    return "\n".join(lines)


def ai_markdown(status: list[str], usage: list[dict], loaded: list[dict] | None) -> str:
    """The AI Usage page: status lines, this session's requests (assistant.USAGE) and the
    models Ollama holds in memory (assistant.ollama_loaded)."""
    lines = [f"**{status[0]}**" if status else "", "", *(f"- {line}" for line in status[1:]), "",
             "### This session", ""]
    if usage:
        by_provider = Counter(assistant.PROVIDER_NAMES.get(r["provider"], r["provider"]) for r in usage)

        def total(key: str) -> int:
            return sum(record[key] or 0 for record in usage)

        cached = total("cached_tokens")
        lines += _lines([
            ("Requests", f"{len(usage)} ({', '.join(f'{name} {n}' for name, n in by_provider.most_common())})"),
            ("Tokens", f"{total('input_tokens'):,} in" + (f" ({cached:,} cached)" if cached else "")
             + f" · {total('output_tokens'):,} out"),
            ("First words after", f"{sum(r['first_s'] for r in usage) / len(usage):.1f} s on average "
                                  f"(fastest {min(r['first_s'] for r in usage):.1f} s)"),
            ("Whole reply", f"{sum(r['total_s'] for r in usage) / len(usage):.1f} s on average"),
        ])
        lines += ["### Recent requests", "", "| Time | Model | First words | Total | Tokens in → out |",
                  "|---|---|---|---|---|"]
        for record in reversed(usage[-RECENT:]):
            tokens = " → ".join("?" if record[key] is None else f"{record[key]:,}"
                                for key in ("input_tokens", "output_tokens"))
            lines.append(f"| {time.strftime('%H:%M:%S', time.localtime(record['at']))} | {record['model']} | "
                         f"{record['first_s']:.1f} s | {record['total_s']:.1f} s | {tokens} |")
        lines.append("")
    else:
        lines += ["*No model requests yet this session: start a model in AI Mode → System 2.*", ""]

    lines += ["### Local models in memory", ""]
    if loaded is None:
        lines.append("*Ollama isn't running.*")
    elif not loaded:
        lines.append("*Ollama has no model loaded right now (it unloads idle models after a few minutes).*")
    else:
        lines += ["| Model | Memory | In VRAM | Context |", "|---|---|---|---|"]
        for model in loaded:
            size, vram = model.get("size", 0), model.get("size_vram", 0)
            share = f" ({vram / size:.0%})" if size else ""
            context = model.get("context_length")
            lines.append(f"| {model.get('name', '?')} | {size / 1e9:.1f} GB | {vram / 1e9:.1f} GB{share} | "
                         f"{f'{context:,} tokens' if context else '—'} |")
    lines += ["", "*Grok and Gemini run on the provider's servers, so there's no memory to show for them; "
                  "tokens are what they bill.*"]
    return "\n".join(lines)


def hourly_chart(values: list[float], width: int, height: int = 150) -> QImage:
    """Bars for the 24 hours, the busiest one highlighted, hours marked every 3."""
    ratio = 2  # drawn at twice the size, shown at 1x: sharp on high-DPI screens
    image = QImage(width * ratio, height * ratio, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(ratio)
    image.fill(QColor(theme.SURFACE))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont("Segoe UI")
    font.setPixelSize(11)
    painter.setFont(font)
    left, top, bottom = 14, 18, height - 18  # room for the first and last hour labels
    slot = (width - 2 * left) / HOURS
    peak = max(range(HOURS), key=values.__getitem__)
    most = values[peak] or 1
    painter.setPen(QColor(theme.BORDER))  # the axis
    painter.drawLine(left, bottom, width - left, bottom)
    for hour, value in enumerate(values):
        bar = (bottom - top) * value / most
        painter.fillRect(QRectF(left + hour * slot + 1, bottom - bar, slot - 2, bar),
                         QColor(theme.ACCENT_EDGE if hour == peak else theme.ACCENT))
    painter.setPen(QColor(theme.SECONDARY_TEXT))
    for hour in range(0, HOURS + 1, 3):
        x = left + hour * slot
        painter.drawText(QRectF(x - 14, bottom + 2, 28, 14), Qt.AlignmentFlag.AlignCenter, f"{hour:02d}")
    painter.setPen(QColor(theme.TEXT))
    x = left + (peak + 0.5) * slot
    painter.drawText(QRectF(x - 40, 0, 80, top - 2), Qt.AlignmentFlag.AlignCenter, f"{values[peak]:.0f} veh/h")
    painter.end()
    return image


class _Page(QTextBrowser):
    """A summary page: Markdown with chart images, and links that select objects on the map."""

    def __init__(self, show) -> None:
        super().__init__()
        self.images: dict[str, QImage] = {}
        self.setOpenLinks(False)
        self.anchorClicked.connect(lambda url: show(url.path()) if url.scheme() == "select" else None)

    def loadResource(self, kind, url):  # type intentionally inherited from Qt
        return self.images.get(url.toString()) or super().loadResource(kind, url)

    def show_markdown(self, text: str) -> None:
        bar = self.verticalScrollBar()
        position = bar.value()  # keep the reader's place
        self.setMarkdown(text)
        bar.setValue(position)


class SummaryPanel(QWidget):
    """The View Summary tab. `gather()` returns a dict with network, cells, origins,
    destinations and ai (status lines); `show(id)` selects an object on the map."""

    def __init__(self, gather, show) -> None:
        super().__init__()
        self.gather = gather
        self._charts = 0  # a new chart URL each time: the document caches images by URL
        self.pages = {"Network": _Page(show), "Demand": _Page(show), "Results": _Page(show), "AI Usage": _Page(show)}
        self.tabs = QTabWidget()
        for name, page in self.pages.items():
            page.setMinimumHeight(540)
            self.tabs.addTab(page, name)
        self.tabs.currentChanged.connect(self.refresh)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        self._live = QTimer(self, interval=2000)  # the Results page follows a run as it goes
        self._live.timeout.connect(self._follow)
        self._live.start()

    def _follow(self) -> None:
        if self.isVisible() and self.tabs.tabText(self.tabs.currentIndex()) == "Results":
            results = self.gather().get("results")
            if results and results["running"]:
                self.pages["Results"].show_markdown(results_markdown(results))

    def refresh(self) -> None:
        """Rebuild the page on show (the others rebuild when opened)."""
        data = self.gather()
        name = self.tabs.tabText(self.tabs.currentIndex())
        page = self.pages[name]
        if name == "Network":
            page.show_markdown(network_markdown(data["network"]))
        elif name == "Demand":
            text, by_hour = demand_markdown(data["network"], data["cells"], data["origins"], data["destinations"],
                                            data["modes"])
            if by_hour:
                self._charts += 1
                page.images = {f"chart:hourly-{self._charts}": hourly_chart(
                    by_hour, max(280, page.viewport().width() - 24))}
                text = text.replace("(chart:hourly)", f"(chart:hourly-{self._charts})")
            page.show_markdown(text)
        elif name == "Results":
            page.show_markdown(results_markdown(data.get("results")))
        else:
            page.show_markdown(ai_markdown(data["ai"], assistant.USAGE, assistant.ollama_loaded()))
