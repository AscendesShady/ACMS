"""The map's background: WGS84 to the scene's metres and OSM's tiles (against the standard tile
formula), the tile cache (fetched once, kept, refreshed when stale, the User-Agent the OSM policy asks
for), each kind drawn (colour, PNG, a PDF page, the map), and the Create tab's section in the window
(placing the map, undo, a run carrying on, Generate keeping it). No test reaches the internet.
Run with `python tests/test_background.py` (or the test monitor)."""

import http.client
import json
import os
import tempfile
from copy import deepcopy
from dataclasses import replace
import time
import urllib.request
from math import asin, asinh, cos, floor, pi, radians, sin, sqrt, tan
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPdfWriter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator import map_tiles  # noqa: E402
from traffic_simulator.background import BackgroundPainter  # noqa: E402
from traffic_simulator.map_tiles import MAX_TILES, TILE_PX, Frame, TileStore  # noqa: E402
from traffic_simulator.models import Background  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_io import load_network, save_network  # noqa: E402

APP = QApplication.instance() or QApplication([])
LONDON = (51.5072, -0.1276)


def ground_m(a, b):
    """Great-circle distance, metres (on Web Mercator's sphere)."""
    (lat1, lon1), (lat2, lon2) = [(radians(lat), radians(lon)) for lat, lon in (a, b)]
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * map_tiles.EARTH_RADIUS_M * asin(sqrt(h))  # haversine


def png(colour: str) -> bytes:
    image = QImage(TILE_PX, TILE_PX, QImage.Format.Format_RGB32)
    image.fill(QColor(colour))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def test_the_scene_is_true_to_scale_round_its_georeference():
    frame = Frame(*LONDON, anchor_x=300.0, anchor_y=-200.0)
    assert all(abs(a - b) < 1e-9 for a, b in zip(frame.to_wgs84(300.0, -200.0), LONDON))  # the anchor is there
    east, south = frame.to_wgs84(1300.0, -200.0), frame.to_wgs84(300.0, 800.0)  # 1 km east, 1 km south
    assert abs(ground_m(LONDON, east) - 1000) < 1 and abs(ground_m(LONDON, south) - 1000) < 5
    assert east[1] > LONDON[1] and south[0] < LONDON[0]  # x east, y down (south)
    assert all(abs(a - b) < 1e-9 for a, b in zip(map_tiles.wgs84(*map_tiles.mercator(*LONDON)), LONDON))


def test_tiles_match_the_standard_tile_numbers():
    frame = Frame(*LONDON)
    for z in (3, 12, 16, 19):
        n = 2 ** z  # OSM's slippy map tile names
        x, y = floor((LONDON[1] + 180) / 360 * n), floor((1 - asinh(tan(radians(LONDON[0]))) / pi) / 2 * n)
        assert frame.tiles(-1, -1, 1, 1, z) == (z, [(x, y)])
        left, top, size = frame.tile_rect(z, x, y)
        assert left <= 0 <= left + size and top <= 0 <= top + size  # it holds the anchor
        assert abs(size - 2 * pi * map_tiles.EARTH_RADIUS_M / n * cos(radians(LONDON[0]))) < 1e-6
    for pixels_per_metre in (0.01, 0.3, 4.0):  # the zoom whose pixels come nearest the screen's
        z = frame.zoom(pixels_per_metre)
        assert 256 / 2 ** 0.5 - 1 <= frame.tile_rect(z, 0, 0)[2] * pixels_per_metre <= 256 * 2 ** 0.5 + 1
    z, tiles = frame.tiles(-50_000, -50_000, 50_000, 50_000, 19)  # a huge area close up: coarser
    assert len(tiles) <= MAX_TILES and z < 19


def test_tiles_are_fetched_once_kept_and_refreshed_when_stale():
    asked, arrived = [], []

    def download(url):
        asked.append(url)
        return png("#3366cc")

    with tempfile.TemporaryDirectory() as folder:
        store = TileStore(Path(folder), ready=lambda *key: arrived.append(key), download=download)
        assert store.get(16, 32744, 21792) is None  # not here yet: asked for in the background
        store.get(16, 32744, 21792)  # asked again while downloading: not twice
        store._pool.shutdown(wait=True)
        assert asked == ["https://tile.openstreetmap.org/16/32744/21792.png"] and arrived == [(16, 32744, 21792)]
        store = TileStore(Path(folder), download=download)
        assert store.get(16, 32744, 21792) == png("#3366cc") and len(asked) == 1  # from disk: kept
        old = time.time() - map_tiles.FRESH_S - 60
        os.utime(store.path(16, 32744, 21792), (old, old))
        assert store.get(16, 32744, 21792) == png("#3366cc")  # stale: shown, and fetched again
        store._pool.shutdown(wait=True)
        assert len(asked) == 2

        def offline(url):
            asked.append(url)
            raise OSError("no network")

        store = TileStore(Path(folder), download=offline)
        store.get(3, 1, 1)
        store._pool.shutdown(wait=True)
        store._pool = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2)
        count = len(asked)
        store.get(3, 1, 1)  # failed a moment ago: not asked again for a minute
        store._pool.shutdown(wait=True)
        assert len(asked) == count
    sent = {}

    class Response:
        left = b"tile"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read1(self, size=-1):  # read a piece at a time (map_tiles.fetch: against a deadline and a size cap)
            piece, self.left = self.left, b""
            return piece

    def urlopen(request, timeout):
        sent.update(request.header_items())
        return Response()

    real, urllib.request.urlopen = urllib.request.urlopen, urlopen
    try:
        assert map_tiles._download("https://tile.openstreetmap.org/0/0/0.png") == b"tile"
    finally:
        urllib.request.urlopen = real
    assert sent.get("User-agent") == map_tiles.USER_AGENT  # identifies the app, as the OSM policy asks


def test_a_tile_cut_short_or_unreadable_is_fetched_again_and_long_unused_tiles_go():
    asked = []

    def download(url):
        asked.append(url)
        return png("#3366cc")

    def torn(path, data):  # a full disk: part of it written, then the error
        with open(path, "wb") as file:
            file.write(data[:len(data) // 3])
        raise OSError(28, "No space left on device")

    with tempfile.TemporaryDirectory() as folder:
        unused, recent = Path(folder) / "3" / "1" / "1.png", Path(folder) / "3" / "1" / "2.png"
        unused.parent.mkdir(parents=True)
        for path in (unused, recent):
            path.write_bytes(png("#3366cc"))
        old = time.time() - map_tiles.KEEP_S - 60
        os.utime(unused, (old, old))
        real, Path.write_bytes = Path.write_bytes, torn
        try:
            painter = BackgroundPainter(Path(folder), download=download)
            assert painter._tile(15, 100, 200) is None
            painter.tiles._pool.shutdown(wait=True)
        finally:
            Path.write_bytes = real
        tile = painter.tiles.path(15, 100, 200)
        assert len(asked) == 1 and not tile.exists() and not list(Path(folder).rglob("*.part"))  # no torn tile
        assert not unused.exists() and recent.exists()  # untouched for KEEP_S: deleted
        tile.write_bytes(png("#3366cc")[:500])  # a tile on disk that can't be decoded (written by an older version)
        painter.tiles._failed.clear()
        painter.tiles._pool = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2)
        assert painter._tile(15, 100, 200) is None  # not shown: deleted, and fetched again
        painter.tiles._pool.shutdown(wait=True)
        assert len(asked) == 2 and painter._tile(15, 100, 200) is not None
        painter.close()


def draw(painter_, background, rect=QRectF(0, 0, 100, 100), pixels_per_metre=1.0) -> QImage:
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    with_painter = QPainter(image)
    with_painter.scale(100 / rect.width(), 100 / rect.height())
    with_painter.translate(-rect.left(), -rect.top())
    painter_.paint(with_painter, rect, background, pixels_per_metre)
    with_painter.end()
    return image


def test_each_kind_of_background_is_drawn():
    with tempfile.TemporaryDirectory() as folder:
        painter = BackgroundPainter(Path(folder) / "tiles", download=lambda url: png("#2266dd"))
        assert draw(painter, Background(colour="#123456")).pixelColor(50, 50).name() == "#123456"
        picture = Path(folder) / "plan.png"
        image = QImage(40, 20, QImage.Format.Format_RGB32)
        image.fill(QColor("#cc2222"))
        image.save(str(picture))
        laid = Background(kind="image", image=str(picture), x_m=0, y_m=0, width_m=100, colour="#000000")
        assert painter.image_rect(laid) == QRectF(0, 0, 100, 50)  # its proportions kept
        shown = draw(painter, laid)
        assert shown.pixelColor(50, 20).name() == "#cc2222" and shown.pixelColor(50, 80).name() == "#000000"
        pdf = Path(folder) / "site.pdf"
        writer = QPdfWriter(str(pdf))
        page = QPainter(writer)
        page.fillRect(page.viewport(), QColor("#22aa22"))
        writer.newPage()
        page.fillRect(page.viewport(), QColor("#aa22aa"))
        page.end()
        second = Background(kind="image", image=str(pdf), page=1, width_m=100)
        assert painter.image(second) is not None and draw(painter, second).pixelColor(50, 10).green() < 100  # page 2
        assert draw(painter, Background(kind="image", image=str(pdf), width_m=100)).pixelColor(50, 10).green() > 120
        assert painter.image(Background(kind="image", image=str(Path(folder) / "none.png"))) is None
        on_map = Background(kind="map", lat=LONDON[0], lon=LONDON[1], colour="#000000")
        assert draw(painter, on_map).pixelColor(50, 50).name() == "#000000"  # tiles on their way: the colour
        painter.tiles._pool.shutdown(wait=True)
        assert draw(painter, on_map).pixelColor(50, 50).name() == "#2266dd"  # arrived: the map
        painter.close()


def test_the_background_is_saved_loaded_and_checked():
    network = generate_network(1, 2)
    network.background = Background(kind="map", lat=-33.8688, lon=151.2093, anchor_x=250.0, opacity=0.6,
                                    zone=[-350.0, -400.0, 1200.0, 900.0])
    with tempfile.TemporaryDirectory() as folder:
        save_network(network, Path(folder) / "sydney.json")
        assert load_network(Path(folder) / "sydney.json").background == network.background
    network.background = Background(kind="image", image="plan.png", locked=True, reference=[10.0, 20.0, 110.0, 20.0, 50.0])
    with tempfile.TemporaryDirectory() as folder:
        save_network(network, Path(folder) / "plan.json")
        assert load_network(Path(folder) / "plan.json").background == network.background
    for bad in (Background(kind="satellite"), Background(colour="green"), Background(lat=89.0), Background(opacity=2.0),
                Background(locked="yes"), Background(reference=[1.0, 2.0, 3.0]), Background(reference=[1, 1, 1, 1, 5]),
                Background(reference=[0, 0, 1, 1, 0]), Background(zone=[0, 0, 100]), Background(zone=[0, 0, 0, 100]),
                Background(zone=[0, 0, float("nan"), 100])):
        network.background = bad
        try:
            network.validate()
        except ValueError:
            continue
        raise AssertionError(f"{bad} must not validate")


def test_the_create_tab_sets_the_background_as_one_undo_step_each():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    tiles = tempfile.TemporaryDirectory()
    window.background_painter.tiles = TileStore(Path(tiles.name), download=lambda url: png("#2266dd"))
    panel, network = window.background_panel, window.network
    assert network.background.kind == "colour" and panel.opacity.isHidden()
    panel.swatches.buttons()[3].click()  # Concrete
    assert network.background.colour == "#9aa1a6"
    panel.kind.group.button(2).click()  # the map: its work zone first, nothing kept until Confirm
    panel.lat.setValue(-37.8136)
    panel.lon.setValue(144.9631)
    panel.go_button.click()  # Melbourne under the box, which is on the network's centre
    panel.confirm_button.click()
    centre = window._junction_bounds().center()
    background = window.network.background
    assert (background.kind, round(background.lat, 6), round(background.lon, 6)) == ("map", -37.8136, 144.9631)
    assert (background.anchor_x, background.anchor_y) == (centre.x(), centre.y()) and background.zone
    assert panel.map_rows[0][1].isVisibleTo(panel) and not panel.image_rows[0][0].isVisibleTo(panel)
    frame = Frame(background.lat, background.lon, background.anchor_x, background.anchor_y)
    assert abs(frame.to_wgs84(centre.x(), centre.y())[0] + 37.8136) < 1e-9
    window.undo()  # back to Concrete
    assert (window.network.background.kind, window.network.background.colour) == ("colour", "#9aa1a6")
    window.redo()
    with tempfile.TemporaryDirectory() as folder:
        picture = Path(folder) / "aerial.jpg"
        image = QImage(300, 200, QImage.Format.Format_RGB32)
        image.fill(QColor("#888888"))
        image.save(str(picture))
        assert panel.use_image(str(picture))
        laid, bounds = window.network.background, window._junction_bounds()
        assert laid.kind == "image" and abs(laid.width_m - bounds.width()) < 1e-6  # as wide as the network
        assert abs(laid.x_m + laid.width_m / 2 - bounds.center().x()) < 1e-6  # and centred on it
        assert not panel.use_image(str(Path(folder) / "missing.png")) and window.network.background.kind == "image"
        opened = []  # a network share: refused before it is opened (no connection to that computer), saying why
        panel.painter.image = lambda background: opened.append(background.image)
        try:
            assert not panel.use_image("\\\\server\\share\\aerial.jpg") and not opened
        finally:
            del panel.painter.image
        assert "network share" in panel.file_label.text() and window.network.background.image == str(picture)
        assert panel.tool == "scale"  # a new image asks for its scale first
        panel.end_tool()  # skipped: as laid
        assert panel.tool is None and abs(window.network.background.width_m - bounds.width()) < 1e-6
        rect = window.scene.sceneRect()
        assert rect.contains(window.background_painter.image_rect(laid))  # the view can scroll over it
    window.run_action.trigger()
    runner = window.runner
    panel.swatches.buttons()[0].click()  # a background change during a run: it carries on
    assert window.runner is runner and window.network.background.colour == "#075322"
    window.undo()
    assert window.runner is runner  # so does undoing one
    window._stop_run()
    window.generate_button.click()  # a new grid keeps its backdrop
    assert window.network.background.kind == "image"
    window.close()
    window.background_painter.tiles._pool.shutdown(wait=True)  # its downloads done before the folder goes
    tiles.cleanup()


def test_two_points_fit_the_image_exactly():
    from traffic_simulator.background import fit_two_points

    # An image 200 px wide laid 400 m wide (2 m a pixel), turned 30° clockwise about its corner at (100, 50).
    a = 2 * complex(cos(radians(30)), sin(radians(30)))
    pixels = [complex(10, 20), complex(150, 90)]
    targets = [complex(100, 50) + a * p for p in pixels]
    picked = [QPointF(pixels[0].real, pixels[0].imag), QPointF(targets[0].real, targets[0].imag),
              QPointF(pixels[1].real, pixels[1].imag), QPointF(targets[1].real, targets[1].imag)]
    x, y, width, turn = fit_two_points(picked, 200)
    assert all(abs(got - want) < 1e-9 for got, want in zip((x, y, width, turn), (100, 50, 400, 30)))


def test_a_turned_image_and_a_turned_map_are_drawn_where_they_lie():
    with tempfile.TemporaryDirectory() as folder:
        painter = BackgroundPainter(Path(folder) / "tiles", download=lambda url: png("#2266dd"))
        picture = Path(folder) / "strip.png"
        image = QImage(40, 20, QImage.Format.Format_RGB32)
        image.fill(QColor("#cc2222"))
        image.save(str(picture))
        # 40 m wide, turned 90° clockwise about its top-left corner at (50, 0): it hangs down and to the left.
        turned = Background(kind="image", image=str(picture), x_m=50, y_m=0, width_m=40, rotation_deg=90, colour="#000000")
        rect = painter.image_rect(turned)
        assert all(abs(a - b) < 1e-6 for a, b in zip((rect.left(), rect.top(), rect.width(), rect.height()), (30, 0, 20, 40)))
        shown = draw(painter, turned)
        assert shown.pixelColor(40, 20).name() == "#cc2222" and shown.pixelColor(60, 20).name() == "#000000"
        on_map = Background(kind="map", lat=LONDON[0], lon=LONDON[1], anchor_x=50, anchor_y=50, map_rotation_deg=37,
                            colour="#000000")
        draw(painter, on_map)
        painter.tiles._pool.shutdown(wait=True)
        shown = draw(painter, on_map)  # turned, the tiles still cover the whole view: no corner left bare
        assert {shown.pixelColor(x, y).name() for x in (1, 50, 98) for y in (1, 50, 98)} == {"#2266dd"}
        painter.close()


def test_a_turned_map_keeps_its_ground_distances():
    frame = Frame(*LONDON, anchor_x=0.0, anchor_y=0.0, rotation=90.0)  # north turned to the right
    east = map_tiles.mercator(*frame.to_wgs84(0.0, 1000.0))
    assert abs(ground_m(LONDON, map_tiles.wgs84(*east)) - 1000) < 1  # 1 km down the screen is 1 km east
    x, y = frame.to_scene(*east)
    assert abs(x) < 1e-6 and abs(y - 1000) < 1e-6
    x, y = frame.to_scene(*frame.to_mercator(123.0, -456.0))
    assert abs(x - 123.0) < 1e-6 and abs(y + 456.0) < 1e-6


def test_the_tools_line_the_background_up_and_the_readout_follows_the_mouse():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from traffic_simulator.background import map_frame
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    tiles = tempfile.TemporaryDirectory()
    window.background_painter.tiles = TileStore(Path(tiles.name), download=lambda url: png("#2266dd"))
    window.resize(1400, 900)
    window.show()
    QTest.qWaitForWindowExposed(window)
    panel, view, scene = window.background_panel, window.view, window.scene
    view.resetTransform()
    view.scale(0.5, 0.5)
    folder = tempfile.TemporaryDirectory()
    picture = Path(folder.name) / "plan.png"
    image = QImage(300, 200, QImage.Format.Format_RGB32)
    image.fill(QColor("#888888"))
    image.save(str(picture))
    assert panel.use_image(str(picture))
    background = window.network.background
    view.centerOn(background.x_m + 200, background.y_m + 150)

    panel.move_button.click()  # Move: drag the image 40 px right and 20 px down at 0.5 px a metre
    assert panel.tool == "move" and panel.step.isVisibleTo(panel)
    start = view.mapFromScene(QPointF(background.x_m + 100, background.y_m + 100))
    before = (background.x_m, background.y_m)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), start + QPoint(40, 20))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(40, 20))
    background = window.network.background
    assert abs(background.x_m - before[0] - 80) < 2.5 and abs(background.y_m - before[1] - 40) < 2.5
    assert scene.selection is None  # the network didn't take the click
    window.undo()
    assert (window.network.background.x_m, window.network.background.y_m) == before
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert panel.tool is None and not panel.step.isVisibleTo(panel)

    panel.align_button.click()  # Two Points: pixel (30, 40) to (0, 0), pixel (270, 160) to (600, 300)
    background = window.network.background
    transform = window.background_painter.image_transform(background)
    targets = [QPointF(0, 0), QPointF(600, 300)]
    for pixel, target in zip((QPointF(30, 40), QPointF(270, 160)), targets):
        for spot in (transform.map(pixel), target):
            panel.press(spot.x(), spot.y())
            panel.release(spot.x(), spot.y())
        assert len(window.background_painter.marks) in (2, 0)
    background = window.network.background
    fitted = window.background_painter.image_transform(background)
    for pixel, target in zip((QPointF(30, 40), QPointF(270, 160)), targets):
        assert (fitted.map(pixel) - target).manhattanLength() < 0.5  # exactly where they belong (to the click)
    assert panel.tool is None and not window.background_painter.marks and abs(panel.rotation.value() - background.rotation_deg) < 0.05
    window.undo()
    assert window.network.background.rotation_deg == 0.0  # the fit was one step
    panel.align_button.click()
    for _ in range(2):  # the same image point twice: asked again
        spot = transform.map(QPointF(50, 50))
        panel.press(spot.x(), spot.y())
        panel.press(0.0, 0.0)
    assert "must differ" in panel.step.text() and panel.tool == "align"
    panel.end_tool()

    _map_at(window, *LONDON)
    assert window.pointer.isVisibleTo(window)
    centre = window._junction_bounds().center()
    spot = view.mapFromScene(QPointF(centre.x() + 300, centre.y()))
    QTest.mouseMove(view.viewport(), spot)
    where = view.mapToScene(spot)  # the scene point under that pixel, as the view saw it
    lat, lon = map_frame(window.network.background).to_wgs84(where.x(), where.y())
    assert window.pointer.text() == f"{lat:.6f}°, {lon:.6f}°" and lon > LONDON[1]  # east of the centre
    panel.map_rotation.setValue(90)  # north to the right: the same spot is now north of the centre
    QTest.mouseMove(view.viewport(), view.mapFromScene(QPointF(centre.x() + 300, centre.y() + 1)))
    assert float(window.pointer.text().split("°")[0]) > LONDON[0]
    assert panel.move_button.text() == "Drag Map to Adjust" and not panel.swatches_row.isVisibleTo(panel)
    panel.move_button.click()  # Move on the map: the network's centre is somewhere else now
    assert panel.move_button.text() == "Done Adjusting"
    start = view.mapFromScene(centre)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), start + QPoint(0, 50))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(0, 50))
    assert abs(window.network.background.anchor_y - centre.y() - 100) < 2.5
    # the point now under the centre: with north to the right, 100 m west of before
    under = map_frame(window.network.background).to_wgs84(centre.x(), centre.y())
    assert abs(ground_m(under, LONDON) - 100) < 5 and under[1] < LONDON[1]
    window.tabs.setCurrentWidget(window.edit_tab)  # leaving the tab ends the tool
    assert panel.tool is None
    panel.swatches.buttons()[0].click()
    assert _switch(panel, 0)  # a colour: no readout
    assert not window.pointer.isVisibleTo(window)
    window.close()
    window.background_painter.tiles._pool.shutdown(wait=True)  # its downloads done before the folder goes
    tiles.cleanup()
    folder.cleanup()


def test_hide_background_shows_the_roads_alone_without_changing_the_project():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    panel, painter = window.background_panel, window.background_painter
    folder = tempfile.TemporaryDirectory()
    picture = Path(folder.name) / "plan.png"
    image = QImage(40, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("#cc2222"))
    image.save(str(picture))
    window.network.background = Background(kind="image", image=str(picture), x_m=0, y_m=0, width_m=100, colour="#000000")
    window._commit()
    panel.show_background(window.network.background)
    assert not panel.hide_button.isHidden() and panel.hide_button.toolTip() == "Hide Background"
    before, undo, unsaved = deepcopy(window.network.background), len(window.history.undo_stack), window.unsaved()
    assert draw(painter, window.network.background).pixelColor(50, 20).name() == "#cc2222"  # the plan shows
    panel.hide_button.click()
    assert painter.hidden and draw(painter, window.network.background).pixelColor(50, 20).name() == "#000000"
    assert window.network.background == before and len(window.history.undo_stack) == undo and window.unsaved() == unsaved
    panel.hide_button.click()  # and back
    assert not painter.hidden and draw(painter, window.network.background).pixelColor(50, 20).name() == "#cc2222"
    panel.show_background(Background(colour="#123456"))
    assert panel.hide_button.isHidden()  # a plain colour has nothing to hide
    window._ask_to_save = False
    window.close()
    folder.cleanup()


def test_lock_pins_the_background_and_measure_sets_an_image_to_scale():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QInputDialog

    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.resize(1400, 900)
    window.show()
    QTest.qWaitForWindowExposed(window)
    panel, view, painter = window.background_panel, window.view, window.background_painter
    view.resetTransform()
    view.scale(0.5, 0.5)
    folder = tempfile.TemporaryDirectory()
    picture = Path(folder.name) / "plan.png"
    image = QImage(300, 200, QImage.Format.Format_RGB32)
    image.fill(QColor("#888888"))
    image.save(str(picture))
    assert panel.use_image(str(picture))
    background = window.network.background
    view.centerOn(background.x_m + 200, background.y_m + 150)
    # an image: no colour swatches, and dragging it is the section's primary button
    assert not panel.swatches_row.isVisibleTo(panel) and not panel.colour_label.isVisibleTo(panel)
    assert panel.move_button.text() == "Drag Image to Adjust" and panel.move_button.property("role") == "primary"

    panel.lock_button.click()  # Lock: nothing moves it
    background = window.network.background
    assert background.locked and not panel.move_button.isEnabled() and not panel.align_button.isEnabled()
    assert not panel.width_m.isEnabled() and not panel.open_button.isEnabled()
    panel.start_tool("move")
    assert panel.tool is None
    window.undo()
    assert not window.network.background.locked and panel.move_button.isEnabled()
    panel.lock_button.click()

    panel.measure_button.click()  # Measure, through the view's own mouse events
    assert panel.tool == "measure" and panel.measure_button.isChecked() and not panel.scale_button.isVisibleTo(panel)
    first = view.mapFromScene(QPointF(background.x_m + 100, background.y_m + 100))
    second = first + QPoint(150, 80)
    a, b = view.mapToScene(first), view.mapToScene(second)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=first)
    QTest.mouseMove(view.viewport(), second)
    measured = ((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5  # 340 m at 0.5 px a metre
    assert painter.line is not None and painter.line[4] == f"{measured:.2f} m"  # the line follows the mouse
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=second)
    assert abs(panel.measured() - measured) < 1e-6 and panel.step.text().startswith(f"{measured:.2f} m")
    assert panel.scale_button.isVisibleTo(panel) and window.scene.selection is None and window.group == []

    transform = painter.image_transform(background)
    inverse, _ = transform.inverted()
    pixel_a, pixel_b = inverse.map(a), inverse.map(b)
    width = background.width_m
    real, QInputDialog.getDouble = QInputDialog.getDouble, lambda *args: (170.0, True)
    try:
        panel.scale_button.click()  # Set Scale: the line is really 170 m, half what it measured
    finally:
        QInputDialog.getDouble = real
    background = window.network.background
    scaled = painter.image_transform(background)
    assert (scaled.map(pixel_a) - a).manhattanLength() < 1e-6  # the first point stays put
    moved = scaled.map(pixel_b)
    assert abs(((moved.x() - a.x()) ** 2 + (moved.y() - a.y()) ** 2) ** 0.5 - 170) < 1e-6
    assert abs(background.width_m - width * 170 / measured) < 1e-6 and background.locked and panel.tool is None
    assert background.reference == [pixel_a.x(), pixel_a.y(), pixel_b.x(), pixel_b.y(), 170.0]
    assert painter.reference_line(background)[4] == "Reference 170.00 m"
    window.undo()  # one step
    assert window.network.background.reference == [] and abs(window.network.background.width_m - width) < 1e-6
    window.redo()
    background = window.network.background
    panel.lock_button.click()  # unlocked, the width typed: the reference moves with it and says so
    panel.width_m.setValue(background.width_m * 2)
    assert window.network.background.reference and \
        painter.reference_line(window.network.background)[4] == "Reference 170.00 m, now 340.00 m"

    assert "image and its scale will be discarded" in _switch(panel, 0)[0]  # over a colour: Measure still works,
    assert not window.network.background.image  # Set Scale doesn't show, and the image is gone
    assert panel.measure_button.isVisibleTo(panel) and not panel.lock_button.isVisibleTo(panel)
    assert panel.swatches_row.isVisibleTo(panel) and not panel.move_button.isVisibleTo(panel)
    panel.measure_button.click()
    for x, y in ((0.0, 0.0), (30.0, 40.0)):
        panel.press(x, y)
        panel.release(x, y)
    assert panel.measured() == 50.0 and not panel.scale_button.isVisibleTo(panel)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert panel.tool is None and painter.line is None and not painter.marks
    window.close()
    folder.cleanup()


def _window_on_tiles():
    from PySide6.QtTest import QTest

    from traffic_simulator.main import MainWindow

    window = MainWindow()
    tiles = tempfile.TemporaryDirectory()
    window.background_painter.tiles = TileStore(Path(tiles.name), download=lambda url: png("#2266dd"))
    window.resize(1400, 900)
    window.show()
    QTest.qWaitForWindowExposed(window)
    window.view.resetTransform()
    window.view.scale(0.5, 0.5)
    return window, tiles


def _switch(panel, index, answer=True):
    """Choose a kind of background; leaving an image or the map asks first, answered Yes or No: what it asked."""
    from PySide6.QtWidgets import QMessageBox

    asked, real = [], QMessageBox.question
    QMessageBox.question = lambda *args: asked.append(args[2]) or (
        QMessageBox.StandardButton.Yes if answer else QMessageBox.StandardButton.No)
    try:
        panel.kind.group.button(index).click()
    finally:
        QMessageBox.question = real
    return asked


def _map_at(window, lat, lon):
    """The map under the network, (lat, lon) at its centre, as one edit (no work zone: the tools' tests)."""
    background, centre = window.network.background, window._junction_bounds().center()
    background.kind, background.lat, background.lon = "map", lat, lon
    background.anchor_x, background.anchor_y = centre.x(), centre.y()
    window._background_changed(("background", "place"))
    window.view.centerOn(centre)


def _close(window, tiles):
    window.close()
    window.background_painter.tiles._pool.shutdown(wait=True)
    tiles.cleanup()


def _drag(view, start, by, button=None):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    button = button or Qt.MouseButton.LeftButton
    QTest.mousePress(view.viewport(), button, pos=start)
    QTest.mouseMove(view.viewport(), start + by)
    QTest.mouseRelease(view.viewport(), button, pos=start + by)


def _wheel(view, notches):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QWheelEvent

    middle = view.viewport().rect().center()
    for _ in range(abs(notches)):
        QApplication.sendEvent(view.viewport(), QWheelEvent(
            QPointF(middle), QPointF(view.viewport().mapToGlobal(middle)), QPoint(), QPoint(0, 120 if notches > 0 else -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))


def test_choosing_the_map_starts_with_its_work_zone_and_the_view_keeps_to_it():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from traffic_simulator.background import map_frame

    window, tiles = _window_on_tiles()
    panel, view, scene, painter = window.background_panel, window.view, window.scene, window.background_painter
    corner = window.fit_button.parentWidget()
    panel.kind.group.button(2).click()  # Map: its work zone comes first
    assert panel.tool == "zone" and panel.bar.isVisible() and corner.isHidden()  # the bar in the corner's place
    assert not any(item.isVisible() for item in scene.items())  # the network is out of the way
    assert not window.tabs.tabBar().isEnabled() and not window.generate_section.isEnabled()
    assert not window.grid_toggles[0].isEnabled() and not panel.move_button.isVisibleTo(panel)
    # the whole world across the view, under a box fixed on screen: far too big for a work zone
    background = window.network.background
    world = 2 * pi * map_tiles.EARTH_RADIUS_M * map_frame(background).scale
    assert abs(view.projection_scale() * world - view.viewport().width()) < 1
    assert min(panel._zone_m()) > 1e6 and not panel.confirm_button.isEnabled() and "Zoom in" in panel.zone_note.text()
    assert painter.framing.endswith("km")  # the box's size, drawn on it

    # Nothing the mouse does loses it: the scene is the network's centre alone, so the view can't pan off it,
    # and until Locate Work Zone the map neither moves nor zooms
    centre, middle = window._junction_bounds().center(), view.viewport().rect().center()
    assert scene.sceneRect().center() == centre and scene.sceneRect().width() == 1
    before, scale = (background.anchor_x, background.anchor_y), view.projection_scale()
    _drag(view, middle, QPoint(-200, 0))
    _drag(view, middle, QPoint(150, 90), Qt.MouseButton.RightButton)
    _wheel(view, 3)
    assert (background.anchor_x, background.anchor_y) == before and view.projection_scale() == scale
    assert (view.mapFromScene(centre) - middle).manhattanLength() <= 1

    panel.lat.setValue(LONDON[0])  # coordinates: Go puts them under the box, framed 1.2 km
    panel.lon.setValue(LONDON[1])
    panel.go_button.click()
    assert abs(min(panel._zone_m()) - 1200) < 1 and panel.confirm_button.isEnabled()
    panel.locate_button.click()  # now the mouse moves the map under the box
    scale = view.projection_scale()
    _drag(view, middle, QPoint(-200, 0))
    under = (panel.lat.value(), panel.lon.value())  # the bar follows: 200 px west, the place under the box east
    assert abs(ground_m(under, LONDON) - 200 / scale) < 0.01 * 200 / scale and under[1] > LONDON[1]
    width = panel._zone_m()[0]
    _wheel(view, -1)  # and the wheel zooms it, about the box
    assert abs(panel._zone_m()[0] / width - 1.18) < 1e-6 and (view.mapFromScene(centre) - middle).manhattanLength() <= 1
    _wheel(view, 22)  # in to a street: too small, and Confirm waits
    assert min(panel._zone_m()) < 100 and not panel.confirm_button.isEnabled()
    _wheel(view, -22)
    assert panel.confirm_button.isEnabled()

    width, height = panel._zone_m()  # Confirm: what the box covers, centred on the network, true to scale there
    panel.confirm_button.click()
    APP.processEvents()  # the view fits the zone once the tool has ended
    background = window.network.background
    assert panel.tool is None and not panel.bar.isVisible() and not corner.isHidden()
    assert any(item.isVisible() for item in scene.items()) and window.tabs.tabBar().isEnabled()
    assert (background.anchor_x, background.anchor_y) == (centre.x(), centre.y())
    assert all(abs(a - b) < 1e-6 for a, b in zip(background.zone, (centre.x() - width / 2, centre.y() - height / 2,
                                                                    width, height)))
    assert (round(background.lat, 6), round(background.lon, 6)) == tuple(round(value, 6) for value in under)
    assert panel.zone_label.text().startswith(f"{width / 1000:.2f} km × ")
    zone = QRectF(*background.zone)
    # the view scrolls over the zone alone (with whatever is drawn outside it), and shows the zone whole
    assert scene.sceneRect() == zone.united(scene.network_rect)
    assert scene.sceneRect().contains(window._junction_bounds())
    assert abs(view.projection_scale() - view.fit_scale(zone)) < 1e-6
    fitted = view.fit_scale(scene.sceneRect())
    _wheel(view, -10)  # zooming out stops with all of that filling the view
    assert fitted - 1e-9 <= view.projection_scale() < fitted * 1.18
    _wheel(view, 2)
    assert view.projection_scale() > fitted * 1.18

    window.undo()  # the whole setup was one step
    assert window.network.background.kind == "colour" and not window.network.background.zone
    window.redo()
    placed = deepcopy(window.network.background)
    panel.zone_button.click()  # Change Work Zone starts on the zone it has; Esc keeps it
    assert panel.tool == "zone" and abs(panel._zone_m()[0] - placed.zone[2]) < 0.01 * placed.zone[2]
    panel.locate_button.click()
    _drag(view, middle, QPoint(0, 90))
    assert window.network.background != placed
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert panel.tool is None and window.network.background == placed and window.tabs.tabBar().isEnabled()

    window.horizontal_spin.setValue(5)  # a bigger grid: the place and its zone move under its centre
    window.generate_button.click()
    middle_m = window._junction_bounds().center()
    assert middle_m != centre and QRectF(*window.network.background.zone).center() == middle_m
    lat, lon = map_frame(window.network.background).to_wgs84(middle_m.x(), middle_m.y())
    assert abs(lat - placed.lat) < 1e-9 and abs(lon - placed.lon) < 1e-9

    zone = list(window.network.background.zone)  # leaving the map asks first: No keeps it as it is
    assert "map and its work zone will be discarded" in _switch(panel, 0, answer=False)[0]
    assert window.network.background.kind == "map" and window.network.background.zone == zone
    colour = window.network.background.colour
    assert _switch(panel, 0)  # Yes: a colour, started afresh (only the colour kept), and the view free again
    assert window.network.background == Background(colour=colour) and window._zoom_range() == (0.0005, 100.0)
    window.undo()  # the discarded map comes back with undo
    assert window.network.background.zone == zone
    assert _switch(panel, 0)  # the map again and Esc: the colour back
    panel.kind.group.button(2).click()
    assert panel.tool == "zone"
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert window.network.background.kind == "colour" and panel.kind.value() == "colour"
    _close(window, tiles)


def test_a_new_image_asks_for_its_scale_first():
    from PySide6.QtWidgets import QInputDialog

    window, tiles = _window_on_tiles()
    panel, scene = window.background_panel, window.scene
    folder = tempfile.TemporaryDirectory()
    picture = Path(folder.name) / "plan.png"
    image = QImage(300, 200, QImage.Format.Format_RGB32)
    image.fill(QColor("#888888"))
    image.save(str(picture))
    assert panel.use_image(str(picture))
    assert panel.tool == "scale" and panel.step.text().startswith("Set the image's scale")
    assert not any(item.isVisible() for item in scene.items()) and not window.tabs.tabBar().isEnabled()
    laid = replace(window.network.background)
    a, b = (laid.x_m + 10, laid.y_m + 10), (laid.x_m + 60, laid.y_m + 10)  # 50 m apart as laid
    answers = iter([(0.0, False), (100.0, True)])  # cancelled first, then 100 m
    real, QInputDialog.getDouble = QInputDialog.getDouble, lambda *args: next(answers)
    try:
        for _ in range(2):
            for x, y in (a, b):
                panel.press(x, y)
                panel.release(x, y)
            APP.processEvents()  # the length is asked once the click is over
            if panel.tool == "scale":
                assert panel.step.text().startswith("Not set.") and panel.measured() is None  # click it again
    finally:
        QInputDialog.getDouble = real
    background = window.network.background
    assert panel.tool is None and abs(background.width_m - 2 * laid.width_m) < 1e-6  # 50 m was really 100 m
    assert background.reference[4] == 100.0 and any(item.isVisible() for item in scene.items())
    assert window.tabs.tabBar().isEnabled() and window.generate_section.isEnabled()
    window.undo()  # the scale is one step
    assert abs(window.network.background.width_m - laid.width_m) < 1e-6
    _close(window, tiles)
    folder.cleanup()


def _wait(check, seconds=8.0) -> bool:
    """Let the event loop run until check() holds (a search's answer crossing from its thread)."""
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)
    return check()


PLACES = {  # a stand-in for Nominatim: query -> its answer
    "Dhaka": [{"display_name": "Dhaka, Bangladesh", "lat": "23.8103", "lon": "90.4125"},
              {"display_name": "North Pole", "lat": "89.9", "lon": "0"}],  # beyond Web Mercator: left out
    "Springfield": [{"display_name": f"Springfield, {state}, United States", "lat": lat, "lon": lon}
                    for state, lat, lon in (("Illinois", "39.7990", "-89.6440"), ("Missouri", "37.2090", "-93.2923"),
                                            ("Massachusetts", "42.1015", "-72.5898"))],
    "Atlantis": [],
    "Bangladesh": [{"display_name": "Bangladesh", "lat": "23.6943", "lon": "90.3444",
                    "boundingbox": ["20.74", "26.63", "88.01", "92.67"]}],
}


def _nominatim(url: str) -> bytes:
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)["q"][0]
    if query == "broken":
        return b"<html>not json</html>"
    if query == "offline":
        raise OSError("no network")
    return json.dumps(PLACES[query]).encode()


def test_place_search_follows_nominatims_usage_policy():
    from traffic_simulator.map_tiles import SEARCH_GAP_S, PlaceSearch

    asked, answers = [], []

    def download(url):
        asked.append((time.monotonic(), url))
        return _nominatim(url)

    search = PlaceSearch(ready=lambda query, found: answers.append((query, found)), download=download)
    assert PlaceSearch().download is map_tiles._download  # the tiles' identifying User-Agent (tested above)
    assert not search.search("   ") and not asked  # nothing to look up
    assert search.search("  Dhaka  ")
    assert _wait(lambda: len(answers) == 1)
    assert answers[0] == ("Dhaka", [("Dhaka, Bangladesh", 23.8103, 90.4125, 0.0)])  # the pole left out; no box
    url = asked[0][1]
    assert url.startswith("https://nominatim.openstreetmap.org/search?") and "format=jsonv2" in url and "q=Dhaka" in url
    assert "countrycodes" not in url
    search.search("dhaka")  # asked before: from the cache, at once, not sent again
    assert answers[1] == ("dhaka", answers[0][1]) and len(asked) == 1
    for query in ("Atlantis", "broken", "offline"):
        search.search(query)
    assert _wait(lambda: len(answers) == 5, 10)
    assert answers[2] == ("Atlantis", []) and "couldn't be read" in answers[3][1] and "Couldn't reach" in answers[4][1]
    search.search("Bangladesh", "BD")  # within a country: asked of Nominatim, and its bounding box measured
    assert _wait(lambda: len(answers) == 6) and "countrycodes=bd" in asked[-1][1]
    name, lat, lon, span = answers[5][1][0]
    south, north = 20.74, 26.63  # its box: 5.89° of latitude, the larger side
    assert name == "Bangladesh" and abs(span - ground_m((south, lon), (north, lon))) < 1000
    gaps = [later - earlier for (earlier, _), (later, _) in zip(asked, asked[1:])]
    assert all(gap >= SEARCH_GAP_S - 0.01 for gap in gaps), gaps  # never more than one request a second
    search.close()


def test_the_place_search_always_answers_and_shares_one_request_a_second_with_the_assistant():
    from traffic_simulator import network_tools
    from traffic_simulator.map_tiles import SEARCH_GAP_S, PlaceSearch

    asked, answers = [], []

    def download(url):
        asked.append(time.monotonic())
        if "Cut" in url:
            raise http.client.IncompleteRead(b"[")  # the answer cut short: http.client's own error
        return _nominatim(url)

    search = PlaceSearch(ready=lambda query, found: answers.append((query, found)), download=download)
    search.search("Cut short")
    assert _wait(lambda: len(answers) == 1, 5) and "couldn't be read" in answers[0][1]  # not "Searching..." for ever
    real, network_tools.place_download = network_tools.place_download, download
    network_tools._PREPARED.clear()
    try:
        assert network_tools._places("Dhaka")[0][0] == "Dhaka, Bangladesh"  # the assistant's place_map, straight after
    finally:
        network_tools.place_download = real
    assert asked[1] - asked[0] >= SEARCH_GAP_S - 0.01  # one clock for the whole app: Nominatim's one a second
    search.close()


def test_a_country_and_a_place_found_by_name_go_under_the_box():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window, tiles = _window_on_tiles()
    panel = window.background_panel
    asked = []
    panel.painter.places.download = lambda url: asked.append(url) or _nominatim(url)
    assert not panel.bar.isVisible()  # a colour: no place to find
    panel.kind.group.button(2).click()  # the map, and its work zone
    assert panel.bar.isVisible() and panel.place.isEnabled() and panel.tool == "zone"
    centre = window._junction_bounds().center()

    panel.country.lineEdit().setText("Bangladesh")  # chosen from the list (typed and Enter, or picked)
    QTest.keyClick(panel.country.lineEdit(), Qt.Key.Key_Return)
    assert panel.search_note.text().startswith("Finding Bangladesh")
    assert _wait(lambda: not panel.search_note.text())  # gone there, framed: its box's 656 km across the box
    background = window.network.background
    assert (round(background.lat, 4), round(background.lon, 4)) == (23.6943, 90.3444)
    assert abs(min(panel._zone_m()) - 655_700) < 2000 and "countrycodes=bd" in asked[-1]

    panel.place.setEditText("Dhaka")  # a place: within that country; one found, gone to at once, 1.2 km
    QTest.keyClick(panel.place.lineEdit(), Qt.Key.Key_Return)
    assert _wait(lambda: "Nominatim" in panel.search_note.text()) and "countrycodes=bd" in asked[-1]
    assert (round(background.lat, 4), round(background.lon, 4)) == (23.8103, 90.4125)
    assert (background.anchor_x, background.anchor_y) == (centre.x(), centre.y()) and panel.tool == "zone"
    assert abs(min(panel._zone_m()) - 1200) < 1 and panel.search_note.text().startswith("Dhaka, Bangladesh")

    panel.country.lineEdit().clear()  # no country: anywhere
    panel.place.setEditText("Springfield")  # three: offered in the place list
    QTest.keyClick(panel.place.lineEdit(), Qt.Key.Key_Return)
    assert _wait(lambda: panel.place.count() == 3) and "countrycodes" not in asked[-1]
    assert panel.search_note.text().startswith("3 places match") and window.network.background.lat == background.lat
    panel.place.hidePopup()
    panel.place.activated.emit(1)  # Missouri
    assert (round(window.network.background.lat, 4), round(window.network.background.lon, 4)) == (37.209, -93.2923)

    panel.place.setEditText("Atlantis")
    QTest.keyClick(panel.place.lineEdit(), Qt.Key.Key_Return)
    assert _wait(lambda: panel.search_note.text().startswith("Nothing found"))
    assert round(window.network.background.lat, 4) == 37.209  # nothing moved
    panel.confirm_button.click()  # Springfield, Missouri it is
    assert panel.tool is None and round(window.network.background.lat, 4) == 37.209
    _close(window, tiles)


def test_a_work_zone_is_kept_only_once_confirmed_and_the_bar_tracks_the_box():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    window, tiles = _window_on_tiles()
    panel, view, scene = window.background_panel, window.view, window.scene
    asked = []
    panel.painter.places.download = lambda url: asked.append(url) or _nominatim(url)
    middle = view.viewport().rect().center()

    def go(lat, lon):
        panel.lat.setValue(lat)
        panel.lon.setValue(lon)
        panel.go_button.click()

    panel.swatches.buttons()[3].click()  # Concrete: a step to undo
    kept = window.history.current
    panel.kind.group.button(2).click()  # while choosing, the section is off: the bar is the only way in or out
    assert panel.tool == "zone" and not panel.isEnabled() and panel.bar.isEnabled()
    go(*LONDON)
    assert window.network.background.kind == "map" and window.history.current == kept  # nothing kept yet
    scale = view.projection_scale()
    QTest.keyClick(view, Qt.Key.Key_F)  # Fit and Reset leave the zoom alone before Locate Work Zone
    QTest.keyClick(view, Qt.Key.Key_R)
    assert view.projection_scale() == scale
    window.undo()  # undo while choosing: the setup put back, then the step before it undone
    assert panel.tool is None and window.network.background == Background()
    window.redo()
    assert (window.network.background.kind, window.network.background.colour) == ("colour", "#9aa1a6")

    panel.kind.group.button(2).click()  # Save while choosing: the setup cancelled first, never written
    go(*LONDON)
    with tempfile.TemporaryDirectory() as folder:
        window.network_path = str(Path(folder) / "project.json")
        assert window.save_file()
        assert load_network(Path(folder) / "project.json").background.kind == "colour" and panel.tool is None

    panel.kind.group.button(2).click()
    background = window.network.background
    panel.show_found("Dhaka", [("Dhaka, Bangladesh", 23.8103, 90.4125, 0.0)])  # an answer nobody asked for now
    assert round(background.lat, 4) != 23.8103
    for _ in range(2):  # a country chosen again comes from the cache, at once, and is framed all the same
        panel.country.lineEdit().setText("Bangladesh")
        QTest.keyClick(panel.country.lineEdit(), Qt.Key.Key_Return)
        assert _wait(lambda: not panel.search_note.text()) and (round(background.lat, 4), round(background.lon, 4)) \
            == (23.6943, 90.3444)
        go(0.0, 0.0)
    assert len(asked) == 1

    covered = min(panel._zone_m())  # Go keeps the ground the box covers, at any latitude
    assert abs(covered - 1200) < 1
    go(60.0, 10.0)
    assert abs(min(panel._zone_m()) - covered) < 0.01 * covered

    go(0.0, 179.999)  # dragged over the antimeridian: the bar still says where the box is
    panel.locate_button.click()
    _drag(view, middle, QPoint(-300, 0))
    assert -180 <= panel.lon.value() < -179.99
    anchor = (background.anchor_x, background.anchor_y)
    panel._place_if_moved()  # leaving a box unchanged moves nothing
    assert (background.anchor_x, background.anchor_y) == anchor
    panel.confirm_button.click()
    window.network.validate()
    assert panel.tool is None and -180 <= window.network.background.lon < -179.99

    with tempfile.TemporaryDirectory() as folder:  # Set Scale, then the map: pinned to the network's centre all the same
        picture = Path(folder) / "plan.png"
        image = QImage(300, 200, QImage.Format.Format_RGB32)
        image.fill(QColor("#888888"))
        image.save(str(picture))
        assert panel.use_image(str(picture)) and panel.tool == "scale"
        assert _switch(panel, 2)
        assert panel.tool == "zone" and scene.sceneRect().width() == 1
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert panel.tool is None and window.network.background.kind == "image"
    _close(window, tiles)


def test_the_assistants_map_shows_with_its_work_zone_as_the_users_confirm_does():
    from traffic_simulator import network_tools

    window, tiles = _window_on_tiles()
    view, scene, panel = window.view, window.scene, window.background_panel
    network_tools.run_tool(window.network, "place_map", {"lat": LONDON[0], "lon": LONDON[1]})
    window._ai_edited("place_map")
    zone = QRectF(*window.network.background.zone)
    assert scene.sceneRect() == zone.united(scene.network_rect) and window._zoom_range()[0] > 0.0005  # kept to it
    assert abs(view.projection_scale() - view.fit_scale(zone)) < 1e-6  # in view, as after Confirm
    assert panel.zone_label.text().startswith("1.20 km × 1.20 km at 51.50720°")
    window.undo()  # one step
    assert window.network.background.kind == "colour"

    # an answer arriving while the user chooses a work zone: the setup is cancelled first, so the assistant never
    # works on a half-made map, nor is its change put back by the setup's Esc
    panel.kind.group.button(2).click()
    panel.lat.setValue(48.8566)
    panel.lon.setValue(2.3522)
    panel.go_button.click()
    network_tools.run_tool(window._assistant_network(), "place_map", {"lat": LONDON[0], "lon": LONDON[1]})
    window._ai_edited("place_map")
    assert panel.tool is None and window.network.background.lat == LONDON[0]
    assert window.history.current[0].background == window.network.background
    from traffic_simulator import scenario  # a study's place: its area is the work zone, and the zone is built
    spec, _, problems = scenario.prepare({"place": "Dhanmondi", "area_km": 3},
                                         {"has_network": False, "has_demand": False, "bus_type": "Bus"})
    steps = {step["tool"]: step["args"] for step in scenario.plan(spec)}
    assert not problems
    assert steps["place_map"]["width_m"] == steps["network_from_background"]["width_m"] == 3000
    _close(window, tiles)


def test_an_image_loads_once_whichever_thread_asks():
    """The assistant traces roads off the GUI thread while the map draws: a PDF page (PDFium isn't thread-safe)
    is rendered once, one load at a time."""
    import threading

    from traffic_simulator import background as module

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "plan.pdf"
        path.write_bytes(b"%PDF")  # only stat()ed: the renderer is stood in for
        rendered, render = [], module._pdf_page

        def slow(file, page):
            rendered.append(threading.current_thread().name)
            time.sleep(0.2)
            image = QImage(40, 20, QImage.Format.Format_RGB32)
            image.fill(QColor("#ffffff"))
            return image

        module._pdf_page = slow
        try:
            painter, background = BackgroundPainter(), Background(kind="image", image=str(path), width_m=100)
            got = []
            threads = [threading.Thread(target=lambda: got.append(painter.image(background))) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert len(rendered) == 1 and len(got) == 4 and all(image is got[0] for image in got)
            for page in range(2, 6):  # paged through: each decoded page goes when the next comes
                painter.image(replace(background, page=page))
            painter.image(background)
            assert len(rendered) == 6 and painter._image[0][1] == background.page  # the first was let go: only the page shown is held
        finally:
            module._pdf_page = render


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
