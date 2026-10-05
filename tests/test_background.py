"""The map's background: WGS84 to the scene's metres and OSM's tiles (against the standard tile
formula), the tile cache (fetched once, kept, refreshed when stale, the User-Agent the OSM policy asks
for), each kind drawn (colour, PNG, a PDF page, the map), and the Create tab's section in the window
(placing the map, undo, a run carrying on, Generate keeping it). No test reaches the internet.
Run with `python tests/test_background.py` (or the test monitor)."""

import json
import os
import tempfile
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
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"tile"

    def urlopen(request, timeout):
        sent.update(request.header_items())
        return Response()

    real, urllib.request.urlopen = urllib.request.urlopen, urlopen
    try:
        assert map_tiles._download("https://tile.openstreetmap.org/0/0/0.png") == b"tile"
    finally:
        urllib.request.urlopen = real
    assert sent.get("User-agent") == map_tiles.USER_AGENT  # identifies the app, as the OSM policy asks


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
    network.background = Background(kind="map", lat=-33.8688, lon=151.2093, anchor_x=250.0, opacity=0.6)
    with tempfile.TemporaryDirectory() as folder:
        save_network(network, Path(folder) / "sydney.json")
        assert load_network(Path(folder) / "sydney.json").background == network.background
    network.background = Background(kind="image", image="plan.png", locked=True, reference=[10.0, 20.0, 110.0, 20.0, 50.0])
    with tempfile.TemporaryDirectory() as folder:
        save_network(network, Path(folder) / "plan.json")
        assert load_network(Path(folder) / "plan.json").background == network.background
    for bad in (Background(kind="satellite"), Background(colour="green"), Background(lat=89.0), Background(opacity=2.0),
                Background(locked="yes"), Background(reference=[1.0, 2.0, 3.0]), Background(reference=[1, 1, 1, 1, 5]),
                Background(reference=[0, 0, 1, 1, 0])):
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
    panel.lat.setValue(-37.8136)
    panel.lon.setValue(144.9631)
    panel.place_map()  # Melbourne under the network's centre
    centre = window._junction_bounds().center()
    background = window.network.background
    assert (background.kind, background.lat, background.lon) == ("map", -37.8136, 144.9631)
    assert (background.anchor_x, background.anchor_y) == (centre.x(), centre.y())
    assert panel.place_row.isVisibleTo(panel) and not panel.image_rows[0][0].isVisibleTo(panel)
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
        assert panel.tool == "scale"  # a new image asks for its scale first
        panel.cancel_tool()  # skipped: as laid
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

    panel.lat.setValue(LONDON[0])
    panel.lon.setValue(LONDON[1])
    panel.place_map()
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
    # the boxes show the point now under the centre: with north to the right, 100 m west of before
    assert abs(ground_m((panel.lat.value(), panel.lon.value()), LONDON) - 100) < 5 and panel.lon.value() < LONDON[1]
    window.tabs.setCurrentWidget(window.edit_tab)  # leaving the tab ends the tool
    assert panel.tool is None
    panel.swatches.buttons()[0].click()
    panel.kind.group.button(0).click()  # a colour: no readout
    assert not window.pointer.isVisibleTo(window)
    window.close()
    window.background_painter.tiles._pool.shutdown(wait=True)  # its downloads done before the folder goes
    tiles.cleanup()
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

    panel.kind.group.button(0).click()  # over a colour: Measure still works, Set Scale doesn't show
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


def _close(window, tiles):
    window.close()
    window.background_painter.tiles._pool.shutdown(wait=True)
    tiles.cleanup()


def test_choosing_the_map_starts_by_finding_the_networks_place():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest

    from traffic_simulator.background import map_frame

    window, tiles = _window_on_tiles()
    panel, view, scene, painter = window.background_panel, window.view, window.scene, window.background_painter
    panel.kind.group.button(2).click()  # Map: its place comes first
    assert panel.tool == "locate" and panel.step.text().startswith("Find the network's place")
    assert not any(item.isVisible() for item in scene.items())  # the network is out of the way
    assert not window.tabs.tabBar().isEnabled() and not window.generate_section.isEnabled()
    assert not window.grid_toggles[0].isEnabled()  # nothing else meanwhile
    assert panel.use_button.isVisibleTo(panel) and not panel.move_button.isVisibleTo(panel)
    bounds = window._junction_bounds()
    centre = bounds.center()
    assert painter.outline == bounds and painter.marks[-1] == (centre.x(), centre.y(), "Network centre")

    before = (panel.lat.value(), panel.lon.value())
    start = view.mapFromScene(centre)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), start + QPoint(-200, 0))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(-200, 0))
    # dragged 200 px west at 0.5 px a metre: the place under the cross is 400 m east of before
    assert abs(ground_m((panel.lat.value(), panel.lon.value()), before) - 400) < 10 and panel.lon.value() > before[1]
    assert not any(item.isVisible() for item in scene.items())  # still hidden after the redraw
    corner = QPoint(40, 40)
    wheel = QWheelEvent(QPointF(corner), QPointF(view.viewport().mapToGlobal(corner)), QPoint(), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.viewport(), wheel)
    assert view.transform().m11() < 0.5 and (view.mapFromScene(centre) - start).manhattanLength() <= 2  # about the cross

    chosen = (panel.lat.value(), panel.lon.value())
    panel.use_button.click()
    background = window.network.background
    assert panel.tool is None and any(item.isVisible() for item in scene.items()) and window.tabs.tabBar().isEnabled()
    assert (background.anchor_x, background.anchor_y) == (centre.x(), centre.y())  # georeferenced right there
    assert (round(background.lat, 6), round(background.lon, 6)) == chosen and not panel.use_button.isVisibleTo(panel)

    panel.locate_button.click()  # again, then Esc: the place it had
    placed = replace(background)
    start = view.mapFromScene(centre)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), start + QPoint(0, 90))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(0, 90))
    assert window.network.background != placed
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert panel.tool is None and window.network.background == placed and window.tabs.tabBar().isEnabled()

    window.horizontal_spin.setValue(5)  # a bigger grid: the chosen place stays under its centre
    window.generate_button.click()
    middle = window._junction_bounds().center()
    assert middle != centre
    lat, lon = map_frame(window.network.background).to_wgs84(middle.x(), middle.y())
    assert abs(lat - placed.lat) < 1e-9 and abs(lon - placed.lon) < 1e-9

    panel.kind.group.button(0).click()  # a colour; then the map again and Esc: the colour back
    panel.kind.group.button(2).click()
    assert panel.tool == "locate"
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
    assert answers[0] == ("Dhaka", [("Dhaka, Bangladesh", 23.8103, 90.4125)])  # the pole left out
    url = asked[0][1]
    assert url.startswith("https://nominatim.openstreetmap.org/search?") and "format=jsonv2" in url and "q=Dhaka" in url
    search.search("dhaka")  # asked before: from the cache, at once, not sent again
    assert answers[1] == ("dhaka", answers[0][1]) and len(asked) == 1
    for query in ("Atlantis", "broken", "offline"):
        search.search(query)
    assert _wait(lambda: len(answers) == 5, 10)
    assert answers[2] == ("Atlantis", []) and "couldn't be read" in answers[3][1] and "Couldn't reach" in answers[4][1]
    gaps = [later - earlier for (earlier, _), (later, _) in zip(asked, asked[1:])]
    assert all(gap >= SEARCH_GAP_S - 0.01 for gap in gaps), gaps  # never more than one request a second
    search.close()


def test_a_place_found_by_name_goes_under_the_networks_centre():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window, tiles = _window_on_tiles()
    panel = window.background_panel
    panel.painter.places.download = _nominatim
    assert not panel.search.isVisibleTo(panel)  # a colour: no place to find
    panel.kind.group.button(2).click()  # the map, and Choose Location
    assert panel.search.isVisibleTo(panel) and panel.search.isEnabled() and panel.tool == "locate"
    centre = window._junction_bounds().center()
    panel.search.setText("Dhaka")
    QTest.keyClick(panel.search, Qt.Key.Key_Return)
    assert panel.search_note.text().startswith("Searching for “Dhaka”")
    assert _wait(lambda: "Nominatim" in panel.search_note.text())  # one place: gone there at once
    background = window.network.background
    assert (round(background.lat, 4), round(background.lon, 4)) == (23.8103, 90.4125)
    assert (background.anchor_x, background.anchor_y) == (centre.x(), centre.y()) and panel.tool == "locate"
    assert panel.search_note.text().startswith("Dhaka, Bangladesh") and panel.search_note.isVisibleTo(panel)

    panel.search.setText("Springfield")  # three: offered in a menu under the box
    QTest.keyClick(panel.search, Qt.Key.Key_Return)
    assert _wait(lambda: len(panel.results.actions()) == 3)
    assert panel.search_note.text().startswith("3 places match") and window.network.background.lat == background.lat
    panel.results.actions()[1].trigger()  # Missouri
    assert (round(window.network.background.lat, 4), round(window.network.background.lon, 4)) == (37.209, -93.2923)
    panel.results.hide()

    panel.search.setText("Atlantis")
    QTest.keyClick(panel.search, Qt.Key.Key_Return)
    assert _wait(lambda: panel.search_note.text().startswith("Nothing found"))
    assert round(window.network.background.lat, 4) == 37.209  # nothing moved
    panel.use_button.click()  # Springfield, Missouri it is
    assert panel.tool is None and round(window.network.background.lat, 4) == 37.209
    _close(window, tiles)


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
