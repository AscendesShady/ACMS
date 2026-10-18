"""A network made from the background, and the other wholesale tools, as the assistant uses them:
OpenStreetMap's road data (a stand-in answers for the Overpass API: no test reaches the internet)
turned into junctions, roads and ends, with signals, one-way streets, lanes, speeds, a roundabout and
a bridge; an image's roads traced from its pixels; generate_demand through the Demand Generator's
controls, one undo step; and in the window, each as one undo step.
Run with `python tests/test_background_network.py` (or the test monitor)."""

import hashlib
import json
import os
from copy import deepcopy
import tempfile
import time
from math import hypot
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator import background_network, network_tools as tools  # noqa: E402
from traffic_simulator.map_tiles import Frame  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402

APP = QApplication.instance() or QApplication([])
background_network.OSM_CACHE = Path(tempfile.mkdtemp())  # never the user's own cache of road data
background_network.OSM_FRESH_DAYS = 0  # each test's stand-in server answers (the cache test turns it on)
LONDON = (51.5072, -0.1276)


def on_the_map():
    """A 3 x 4 grid over the map, its centre at London; `at(dx, dy)` is (lat, lon) that far from it."""
    network = generate_network(3, 4)
    tools._PREPARED.clear()
    xs, ys = [n.x for n in network.all_nodes()], [n.y for n in network.all_nodes()]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    background = network.background
    background.kind, (background.lat, background.lon), background.anchor_x, background.anchor_y = "map", LONDON, cx, cy
    frame = Frame(*LONDON, cx, cy)
    return network, (cx, cy), lambda dx, dy: frame.to_wgs84(cx + dx, cy + dy)


def overpass(at) -> dict:
    """A small town: a primary road east-west (4 lanes, 30 mph) with a signal 20 m before its junction
    with a one-way residential street coming south from the north edge, a roundabout to the north-east
    joined to it and to the east edge, and a bridge over the main road."""
    nodes, ways, number = [], [], [0]

    def node(dx, dy, **tags):
        number[0] += 1
        lat, lon = at(dx, dy)
        nodes.append({"type": "node", "id": number[0], "lat": lat, "lon": lon, **({"tags": tags} if tags else {})})
        return number[0]

    def way(refs, **tags):
        ways.append({"type": "way", "id": 1000 + len(ways), "nodes": refs, "tags": tags})

    west, signal, middle, east_1, east = node(-500, 0), node(-20, 0, highway="traffic_signals"), node(0, 0), node(100, 0), node(500, 0)
    zebra = node(115, 0, highway="traffic_signals", traffic_signals="crossing")  # a pedestrian crossing's signal
    node(95, 8, highway="traffic_signals", traffic_signals="blinker")  # flashing amber
    way([west, node(-100, 0), signal, middle, east_1, zebra, east], highway="primary", lanes="4", maxspeed="30 mph")
    way([node(0, -500), middle, node(0, 200)], highway="residential", oneway="yes")  # southbound, a dead end
    ring = [node(220, -150), node(200, -130), node(180, -150), node(200, -170)]
    way(ring + ring[:1], highway="tertiary", junction="roundabout")
    way([east_1, ring[2]], highway="tertiary")
    way([ring[0], node(500, -150)], highway="tertiary")
    way([node(-200, -100), node(-200, 100)], highway="residential", bridge="yes")  # over the main road
    way([node(-250, 50), node(-260, 60)], highway="footway")  # not a road for cars: left out
    return {"elements": nodes + ways}


def test_the_maps_roads_come_from_openstreetmap_data():
    network, (cx, cy), at = on_the_map()
    sent = []

    def post(url, data):
        sent.append((url, data))
        return json.dumps(overpass(at)).encode()

    real, background_network.post = background_network.post, post
    try:
        args = {"width_m": 600, "height_m": 600}
        tools.prepare(network, "network_from_background", args)  # off the UI thread: fetched into the cache
        result = tools.run_tool(network, "network_from_background", args)
        tools.run_tool(generate_network(1, 1), "network_summary", {})
    finally:
        background_network.post = real
    assert len(sent) == 1 and sent[0][0] == background_network.OVERPASS_URL  # asked once, for check and change
    query = sent[0][1].decode()
    assert "highway" in query and "residential" in query and "51.50" in query
    network.validate()
    nodes = {n.id: n for n in network.nodes.values()}

    def near(dx, dy, within=15):
        return [n for n in nodes.values() if hypot(n.x - cx - dx, n.y - cy - dy) < within]

    crossing, = near(0, 0)
    assert crossing.control == "signalised"  # its traffic signal stands 20 m up the main road
    branch, = near(100, 0)
    assert branch.control == "uncontrolled"  # a crossing's signal and a flashing amber beside it: it gives way
    assert result["signals_not_junction_signals"] == {"blinker": 1, "crossing": 1} and "Edit" in result["signals_note"]
    ring, = [n for n in nodes.values() if n.control == "roundabout"]
    assert hypot(ring.x - cx - 200, ring.y - cy + 150) < 5  # the ring became one roundabout at its centre
    assert result["roundabouts"] == 1 and result["signalised"] == 1
    ends = {end.id: (round(end.x - cx), round(end.y - cy)) for end in network.approach_nodes.values()}
    assert set(ends.values()) == {(-300, 0), (300, 0), (0, -300), (300, -150)}  # where roads leave the area
    assert ends == {"1W": (-300, 0), "1E": (300, 0), "2E": (300, -150), "1N": (0, -300)}  # numbered along each side
    main = [road for road in network.all_roads() if road.road_type == "primary"]
    assert main and all(road.lanes_per_direction == 2 and road.speed_limit_kmh == 48 for road in main)  # 4 lanes, 30 mph
    north = next(end for end, where in ends.items() if where == (0, -300))
    inbound = network.approach_roads[north]
    assert inbound.direction == "one-way" and inbound.from_node == north  # traffic comes in from the north edge
    bridge = [road for road in network.all_roads() if road.overpass]
    assert len(bridge) == 1 and bridge[0].road_type == "residential"  # crosses the main road without a junction
    assert not near(-255, 55, 30) or all(n.id in {r.from_node for r in bridge} | {r.to_node for r in bridge}
                                         for n in near(-255, 55, 30))  # the footway left out
    assert "OpenStreetMap" in result["source"]
    tools.run_tool(network, "set_junction_control", {"junctions": [branch.id], "control": "signalised"})
    assert network.nodes[branch.id].control == "signalised" and network.nodes[branch.id].signal_method == "webster"
    network.validate()  # overridden by hand (or the assistant): an ordinary signalised junction, Webster to start


def test_a_road_cut_just_past_a_junction_still_has_room_to_enter():
    # Dhanmondi: roads clipped a few metres past a junction left 5-15 m entry stubs holding one car each, so traffic
    # waited off the network and the demand check called any real demand a jam
    network, (cx, cy), at = on_the_map()

    def post(url, data):
        nodes, number = [], [0]

        def node(dx, dy):
            number[0] += 1
            lat, lon = at(dx, dy)
            nodes.append({"type": "node", "id": number[0], "lat": lat, "lon": lon})
            return number[0]
        junction = node(293, 0)  # 7 m inside the east edge of a 600 m square
        ways = [{"type": "way", "id": 1001, "nodes": [node(-500, 0), junction, node(500, 0)], "tags": {"highway": "primary"}},
                {"type": "way", "id": 1002, "nodes": [node(293, -200), junction, node(293, 200)], "tags": {"highway": "residential"}}]
        return json.dumps({"elements": nodes + ways}).encode()

    real, background_network.post = background_network.post, post
    try:
        args = {"width_m": 600, "height_m": 600}
        tools.prepare(network, "network_from_background", args)
        tools.run_tool(network, "network_from_background", args)
    finally:
        background_network.post = real
    network.validate()
    entries = [road for road in network.all_roads() if road.from_node in network.approach_nodes
               or road.to_node in network.approach_nodes]
    assert entries and min(road.length_m for road in entries) >= background_network.ENTRY_ROAD_M - 0.5
    east = max(network.approach_nodes.values(), key=lambda end: end.x)  # carried on out along the road's own line
    assert abs(east.y - cy) < 1 and east.x - cx > 293 + background_network.ENTRY_ROAD_M - 1


def test_a_dual_carriageway_crossing_is_one_junction():
    # OSM maps two dual carriageways crossing as four nodes ~15 m apart; as four junctions, 2-3 cars of room between
    # them gridlocked Dhanmondi. They are one junction now, as SUMO's netconvert joins such clusters.
    network, (cx, cy), at = on_the_map()

    def post(url, data):
        nodes, number = [], [0]

        def node(dx, dy):
            number[0] += 1
            lat, lon = at(dx, dy)
            nodes.append({"type": "node", "id": number[0], "lat": lat, "lon": lon})
            return number[0]
        a, b, c, d = node(-7.5, -7.5), node(7.5, -7.5), node(7.5, 7.5), node(-7.5, 7.5)  # the crossing's four corners
        ways = [([node(-250, -7.5), a, b, node(250, -7.5)], "yes"), ([node(250, 7.5), c, d, node(-250, 7.5)], "yes"),
                ([node(-7.5, 250), d, a, node(-7.5, -250)], "yes"), ([node(7.5, -250), b, c, node(7.5, 250)], "yes")]
        return json.dumps({"elements": nodes + [{"type": "way", "id": 1000 + i, "nodes": refs,
                                                 "tags": {"highway": "primary", "oneway": oneway}}
                                                for i, (refs, oneway) in enumerate(ways)]}).encode()

    real, background_network.post = background_network.post, post
    try:
        args = {"width_m": 400, "height_m": 400}
        tools.prepare(network, "network_from_background", args)
        tools.run_tool(network, "network_from_background", args)
    finally:
        background_network.post = real
    network.validate()
    middle = [node for node in network.nodes.values() if hypot(node.x - cx, node.y - cy) < 30]
    assert len(middle) == 1, [(node.id, round(node.x - cx), round(node.y - cy)) for node in middle]


def test_busy_overpass_servers_are_tried_again_after_a_pause():
    tried, waited = [], []

    def post(url, data):  # every server busy on the first round (a live Dhanmondi run met 504s and timeouts)
        tried.append(url)
        if len(tried) <= len(background_network.OVERPASS_URLS):
            raise OSError("HTTP Error 504: Gateway Timeout")
        return json.dumps({"elements": []}).encode()

    real, sleep = background_network.post, background_network.time.sleep
    background_network.post, background_network.time.sleep = post, waited.append
    background_network.OSM_FRESH_DAYS = 7
    try:
        assert background_network.fetch_osm("query") == {"elements": []}  # and kept
        tried.clear()
        background_network.post = lambda url, data: (tried.append(url), (_ for _ in ()).throw(OSError("timed out")))[1]
        copy = background_network.fetch_osm("query")  # kept this week: used without asking, said so
        assert copy["elements"] == [] and "last 7 days" in copy["cached_why"] and tried == []
        kept = background_network.OSM_CACHE / f"{hashlib.sha256(b'query').hexdigest()[:32]}.json"
        old = time.time() - (background_network.OSM_FRESH_DAYS + 1) * 86400
        os.utime(kept, (old, old))
        copy = background_network.fetch_osm("query")  # older, and none answers: the copy kept, said so
        assert copy["elements"] == [] and "didn't answer" in copy["cached_why"] and             len(tried) == 2 * len(background_network.OVERPASS_URLS)
        try:
            background_network.fetch_osm("another area")
        except ValueError as exc:  # nothing kept for it: the error
            assert "each tried twice" in str(exc)
        else:
            raise AssertionError("no server answered and nothing was kept")
    finally:
        background_network.post, background_network.time.sleep = real, sleep
        background_network.OSM_FRESH_DAYS = 0
    assert waited == [background_network.OVERPASS_RETRY_S] * 3  # one pause before each second round


def test_an_images_roads_are_traced_from_its_pixels():
    network = generate_network(3, 4)
    tools._PREPARED.clear()
    assert "plain colour" in _refused(network, {})
    network.background.kind, network.background.image = "image", "plan.png"
    assert "road_colour" in _refused(network, {})  # the assistant must ask the user for the roads' colour
    width, height = 300, 200
    mask = bytearray(width * height)
    for y in range(95, 102):  # a road across, 7 px (14 m) wide
        mask[y * width:(y + 1) * width] = b"\x01" * width
    for y in range(40, height):  # and one down from a dead end to the bottom edge
        for x in range(148, 155):
            mask[y * width + x] = 1
    for y in range(10, 13):  # a speck of text
        mask[y * width + 20:y * width + 23] = b"\x01\x01\x01"
    tools.image_mask = lambda background, colour, tolerance, metres: (width, height, mask, (2, 0, 0, 2, 100, 50), 2.0)
    try:
        result = tools.run_tool(network, "network_from_background", {"road_colour": "#FFFFFF"})
    finally:
        tools.image_mask = None
    network.validate()
    ends = sorted((round(end.x), round(end.y)) for end in network.approach_nodes.values())
    assert ends == [(100, 246), (402, 448), (698, 246)], ends  # grid pixel (px, py) is scene (100 + 2 px, 50 + 2 py)
    tee = [n for n in network.nodes.values() if abs(n.x - 402) < 6 and abs(n.y - 246) < 6]
    dead = [n for n in network.nodes.values() if abs(n.x - 402) < 6 and abs(n.y - 136) < 12]
    assert tee and dead and len(network.nodes) == 2, {n.id: (n.x, n.y) for n in network.nodes.values()}
    assert all(road.lanes_per_direction == 2 for road in network.all_roads())  # 14 m wide: two lanes each way
    assert "14 m" in result["lanes"] and "Set Scale" in result["note"]  # the image was never scaled


def _refused(network, args) -> str:
    try:
        tools.run_tool(network, "network_from_background", args)
    except tools.ToolError as exc:
        return str(exc)
    raise AssertionError("should have been refused")


def test_the_image_reader_finds_the_road_colour():
    from traffic_simulator.background import BackgroundPainter
    from traffic_simulator.models import Background

    with tempfile.TemporaryDirectory() as folder:
        picture = Path(folder) / "plan.png"
        image = QImage(200, 100, QImage.Format.Format_RGB32)
        image.fill(QColor("#808080"))
        painter = QPainter(image)
        painter.fillRect(0, 40, 200, 10, QColor("#fefefe"))  # a white road across, 10 px (20 m) wide
        painter.end()
        image.save(str(picture))
        reader = BackgroundPainter(Path(folder) / "tiles")
        laid = Background(kind="image", image=str(picture), x_m=10, y_m=20, width_m=400)  # 2 m an image pixel
        width, height, mask, (a, b, c, d, e, f), metres = reader.road_mask(laid, "#ffffff", 20, 4.0)
        assert (width, height, metres) == (100, 50, 4.0)  # 4 m a grid pixel
        rows = [y for y in range(height) if any(mask[y * width:(y + 1) * width])]
        assert rows == list(range(20, 25)) and all(mask[22 * width:23 * width])
        assert (a, d, a * 0 + c * 0 + e, b * 0 + d * 0 + f) == (4.0, 4.0, 12.0, 22.0)  # grid pixel centres, metres
        reader.close()


def test_generate_demand_works_the_demand_generators_controls():
    from traffic_simulator.od_matrix import ODCellPanel, ODMatrix

    network = generate_network(2, 2)
    for end, supply, demand in (("1W", 600, 0), ("1N", 300, 0), ("1E", 0, 500), ("2S", 0, 400)):
        network.approach_nodes[end].supply_vph, network.approach_nodes[end].demand_vph = supply, demand
    matrix = ODMatrix()
    panel = ODCellPanel(matrix)
    ends = sorted(network.approach_nodes)
    matrix.set_ends(ends, ends)
    steps = []
    panel.changed.connect(steps.append)
    try:
        panel.command("generate_demand", {"demand": "hourly"})
    except ValueError as exc:
        assert "no network" in str(exc)  # without a planner (no network) it can't
    panel.planner = lambda pairs, args, *matrix: tools.demand_plan(network, pairs, args, *matrix)
    matrix.cells[("2W", "1S")] = matrix.cell(("2W", "1S"))
    matrix.cells[("2W", "1S")].flow = 99  # old demand in a pair the model gives none: cleared
    before = dict(matrix.cells)
    assert panel.command("generate_demand", {"demand": "hourly"}, check_only=True) == {"ok": True}
    assert matrix.cells == before and not steps  # the check changes nothing
    info = panel.command("generate_demand", {"demand": "hourly", "arrival_pattern": "poisson",
                                             "mode_split": [{"mode": "Car", "percent": 100}]})
    assert steps == [None] and matrix.mode == "hourly"  # one undo step, shown as hourly
    made = {pair: cell.flow for pair, cell in matrix.cells.items() if cell.flow}
    assert sum(value for (o, _), value in made.items() if o == "1W") in range(597, 604)
    assert sum(value for (_, d), value in made.items() if d == "2S") in range(396, 405)
    assert all(o in ("1W", "1N") and d in ("1E", "2S") for o, d in made)
    assert matrix.cells[("2W", "1S")].flow is None
    assert all(matrix.cells[pair].pattern == "Poisson" and matrix.split_of(matrix.cells[pair])[1] == 100 for pair in made)
    assert info["total_made"] in range(895, 906) and info["pairs_with_demand"] == len(made)
    try:
        panel.command("generate_demand", {"demand": "hourly", "arrival_pattern": "sometimes"})
    except ValueError as exc:
        assert "arrival_pattern" in str(exc)
    else:
        raise AssertionError("a bad pattern should be refused")
    panel.scaler = lambda values, pairs, args, shares=None: tools.scale_plan(network, values, pairs, args, shares)
    steps.clear()
    info = panel.command("scale_demand", {"origins": ["1W"], "factor": 2})  # what's entered, doubled
    assert steps == [None] and info["factor"] == 2
    assert {pair: cell.flow for pair, cell in matrix.cells.items() if cell.flow} == \
        {pair: value * 2 if pair[0] == "1W" else value for pair, value in made.items()}
    info = panel.command("scale_demand", {"degree_of_saturation": 0.8})  # every pair, to 80 % of capacity
    x = max(info["degree_of_saturation"].values())  # 0.8, or under it where a link's queue storage binds first
    assert (abs(x - 0.8) <= 0.02 or ("sized_by" in info and 0.6 < x < 0.8)) and len(steps) == 2
    sized = {pair: cell.flow for pair, cell in matrix.cells.items() if cell.flow}
    panel.checker = lambda pairs: (0.5, {"checked": True, "kept": "50%"})  # the run held only half of it
    info = panel.command("scale_demand", {"factor": 1})
    assert "checked_in_a_run" not in info and len(steps) == 2  # a factor isn't checked (and 1 changes nothing)
    info = panel.command("scale_demand", {"degree_of_saturation": 0.8})
    assert info["checked_in_a_run"]["kept"] == "50%" and len(steps) == 3  # still one undo step
    assert all(abs((matrix.cells[pair].flow or 0) - value * 0.5) <= 1.5 for pair, value in sized.items())  # halved
    before = deepcopy(matrix.cells)

    def jams(pairs):
        raise ValueError("The simulation jams even at 6 % of that demand")

    panel.checker = jams
    try:
        panel.command("scale_demand", {"degree_of_saturation": 0.8})
    except ValueError as exc:
        assert "jams" in str(exc)
    else:
        raise AssertionError("a jam should refuse the change")
    assert matrix.cells == before and len(steps) == 3  # put back, no undo step


def test_in_the_window_each_is_one_undo_step():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    tools._PREPARED.clear()
    grid = sorted(window.network.nodes)
    ai = window.ai_mode
    result = ai._execute("deploy_institutions", {"count": 3, "seed": 5})
    assert len(result["placed"]) == 3 and len(window.network.institutions) == 3
    window.undo()
    assert not window.network.institutions
    width, height = 200, 200
    mask = bytearray(width * height)
    for y in range(97, 104):
        mask[y * width:(y + 1) * width] = b"\x01" * width
    for x in range(97, 104):
        for y in range(height):
            mask[y * width + x] = 1
    window.network.background.kind, window.network.background.image = "image", "plan.png"
    tools.image_mask = lambda *args: (width, height, mask, (5, 0, 0, 5, 0, 0), 5.0)
    try:
        result = ai._execute("network_from_background", {"road_colour": "#ffffff"})
    finally:
        tools.image_mask = window.background_painter.road_mask
    assert "error" not in result, result
    assert len(window.network.nodes) == 1 and len(window.network.approach_nodes) == 4  # a crossroads, four ends
    assert window.scene.network is window.network and len(window._od_ends()[0]) == 4  # the demand grid's ends follow
    window.undo()
    assert sorted(window.network.nodes) == grid  # the grid back
    window.close()


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
