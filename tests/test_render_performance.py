"""Pan/repaint must reuse geometry and skip offscreen bodies, without altering visible pixels."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPolygonF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsRectItem, QStyleOptionGraphicsItem
from traffic_simulator import asset_mesh, vehicle_assets
from traffic_simulator.models import Institution
from traffic_simulator.network_scene import (BuildingItem, NetworkGraphicsScene, NetworkView, VehicleLayer,
                                             _FaceDrawing, _vehicle_faces, projection_lift, projection_transform)
from traffic_simulator.network_generator import generate_network
from traffic_simulator.simulation import Frame, VehicleState

APP = QApplication.instance() or QApplication([])


def test_pan_reuses_building_and_vehicle_faces_at_new_positions_and_zoom():
    scene = NetworkGraphicsScene()
    scene.isometric = True
    building = BuildingItem(scene, Institution("IN1", "Office", 0., 0., 900., 100.))
    image = QImage(400, 400, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    try:
        painter.setWorldTransform(projection_transform(True, 2.))
        building.paint(painter, None)
        VehicleLayer._paint_mesh(painter, 6, "#aa3322", 10., 10., 1., 0., 1.6, .65,
                                 projection_lift())
        scene.frame = Frame(1., (), {}, 0, 0, walkers=((20., 0., 1., 0., (10., 0., 1., 0.)),))
        layer = VehicleLayer(scene)
        layer.paint(painter, None)
        original = asset_mesh.normal
        def repeated_work(*args):
            raise AssertionError("Panning/zooming rebuilt unchanged mesh normals")
        asset_mesh.normal = repeated_work
        try:
            painter.setWorldTransform(projection_transform(True, 3.))
            building.paint(painter, None)
            VehicleLayer._paint_mesh(painter, 6, "#aa3322", 15., 20., 1., 0., 1.6, .65,
                                     projection_lift())
            scene.frame_alpha = .7
            layer.paint(painter, None)
        finally:
            asset_mesh.normal = original
    finally:
        painter.end()


def test_offscreen_meshes_are_skipped_but_edge_bodies_keep_identical_pixels():
    scene = NetworkGraphicsScene()
    scene.isometric = True
    scene.setSceneRect(-100, -100, 5000, 5000)
    def state(name, x, y, length=5., width=1.8):
        return VehicleState(name, "Car", "#ee7733", length, width, x, y, 1., 0.,
                            (x, y, 1., 0.), 0., "", "", 0., "", "", vehicle_class="passenger")
    scene.frame = Frame(1., (state("here", 50., 50.), state("edge", 130., 50., 40., 20.),
                            state("far", 4000., 4000.)), {}, 3, 0)
    scene.frame_alpha = 1.
    layer = VehicleLayer(scene)
    option = QStyleOptionGraphicsItem()
    option.exposedRect = QRectF(0., 0., 100., 100.)
    images, counts = [], []
    original = VehicleLayer._paint_mesh
    try:
        for exposed in (None, option):
            painted = []
            def record(*args, **kwargs):
                painted.append(args[3:5])
                return original(*args, **kwargs)
            VehicleLayer._paint_mesh = staticmethod(record)
            image = QImage(500, 500, QImage.Format.Format_RGB32)
            image.fill(QColor("#123456"))
            painter = QPainter(image)
            painter.setWorldTransform(projection_transform(True, 2.))
            painter.setClipRect(option.exposedRect)
            layer.paint(painter, exposed)
            painter.end()
            images.append(image)
            counts.append(len(painted))
    finally:
        VehicleLayer._paint_mesh = staticmethod(original)
    assert images[0] == images[1], "Culling removed a visible edge/roof"
    assert counts == [3, 2], counts


def test_vehicle_cache_keeps_exact_headings_dimensions_paints_and_a_fixed_bound():
    _vehicle_faces.cache_clear()
    args = (6, "#bb3322", 1.6, .65, 1., 0., True, True)
    first = _vehicle_faces(*args)
    assert _vehicle_faces(*args) is first
    for index, value in ((1, "#33bb22"), (2, 2.), (3, .9), (4, .999999), (5, .000001),
                         (6, False), (7, False)):
        changed = list(args)
        changed[index] = value
        assert _vehicle_faces(*changed) is not first
    for i in range(2060):
        _vehicle_faces(1, "#bb3322", 5. + i * .001, 1.8, 1., 0., True, False)
    assert _vehicle_faces.cache_info().currsize == 2048


def test_a_frame_at_any_headings_reuses_the_faces_in_2d_and_25d():
    from math import cos, radians, sin

    def car(i, x, y, degrees):  # a passenger car (a skinned mesh) heading `degrees`
        ux, uy = cos(radians(degrees)), sin(radians(degrees))
        return VehicleState(f"v{i}", "Car", "#3987e5", 4.5, 1.8, x, y, ux, uy, (x, y, ux, uy), 0., "", "", 0., "", "",
                            vehicle_class="passenger")

    def painted(scene, isometric, scale):
        image = QImage(800, 500, QImage.Format.Format_RGB32)
        image.fill(QColor("#123456"))
        painter = QPainter(image)
        painter.setWorldTransform(projection_transform(isometric, scale))
        VehicleLayer(scene).paint(painter, None)
        painter.end()
        return image

    scene = NetworkGraphicsScene()
    scene.setSceneRect(-50, -50, 700, 300)
    scene.frame_alpha = 1.
    # An imported network's roads run every way: 300 cars, none at the same heading (1.37 degrees apart)
    scene.frame = Frame(1., tuple(car(i, i % 30 * 20., i // 30 * 12., i * 1.37) for i in range(300)), {}, 300, 0)
    for isometric, most in ((False, 3), (True, 3 * 181)):  # 3 car skins; top-down one drawing turned, 2.5D one a 2°
        scene.isometric = isometric
        _vehicle_faces.cache_clear()
        painted(scene, isometric, 1.5)
        built = _vehicle_faces.cache_info().misses
        painted(scene, isometric, 1.5)
        assert built <= most and _vehicle_faces.cache_info().misses == built, (isometric, built)  # next frame: none
    scene.isometric = False  # the turned drawing covers the car's own footprint
    scene.frame = Frame(1., (car(0, 20., 20., 37.),), {}, 1, 0)
    turned = painted(scene, False, 10.)
    exact = QImage(turned.size(), QImage.Format.Format_RGB32)
    exact.fill(QColor("#123456"))
    painter = QPainter(exact)
    painter.setWorldTransform(projection_transform(False, 10.))
    painter.setPen(Qt.PenStyle.NoPen)
    _vehicle_faces(vehicle_assets.style(scene.frame.vehicles[0]), "#3987e5", 4.5, 1.8, cos(radians(37.)),
                   sin(radians(37.)), False, True).paint(painter, 20., 20.)
    painter.end()
    covered = [{(x, y) for x in range(0, 400) for y in range(0, 400) if image.pixelColor(x, y) != QColor("#123456")}
               for image in (turned, exact)]
    assert covered[1] and len(covered[0] ^ covered[1]) < 0.03 * len(covered[1])


def test_topdown_culling_keeps_small_markers_that_extend_into_the_view():
    scene = NetworkGraphicsScene()
    scene.setSceneRect(-100, -100, 500, 500)
    vehicle = VehicleState("edge", "Car", "#ee7733", 5., 1.8, -12., 50., 1., 0.,
                           (-12., 50., 1., 0.), 0., "", "", 0., "", "")
    scene.frame, scene.frame_alpha = Frame(1., (vehicle,), {}, 1, 0), 1.
    layer = VehicleLayer(scene)
    option = QStyleOptionGraphicsItem()
    option.exposedRect = QRectF(0., 0., 100., 100.)
    images = []
    for exposed in (None, option):
        image = QImage(20, 20, QImage.Format.Format_RGB32)
        image.fill(QColor("#123456"))
        painter = QPainter(image)
        painter.setWorldTransform(projection_transform(False, .1))
        painter.setClipRect(option.exposedRect)
        layer.paint(painter, exposed)
        painter.end()
        images.append(image)
    assert images[0] == images[1]
    assert images[1].pixelColor(0, 5) == QColor(vehicle.colour)


def _viewport_image(view):
    return view.viewport().grab().toImage()


def _move(view, point):
    # Native cursor moves are not delivered to a Windows window deliberately kept off screen.
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(view.viewport().mapToGlobal(point)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier)
    APP.sendEvent(view.viewport(), event)
    APP.processEvents()


def _check_static_drag(map_background=False):
    from copy import deepcopy
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from traffic_simulator.background import BackgroundPainter
    folder = TemporaryDirectory()
    scene = NetworkGraphicsScene(generate_network(3, 3))
    scene.rebuild()
    view = NetworkView(scene)
    view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    view.background = BackgroundPainter(Path(folder.name))  # MainWindow always attaches one, even for plain colour.
    if map_background:
        scene.network.background.kind = "map"
        # Retain the real foreground attribution but keep this deterministic and entirely offline.
        view.background.paint = lambda painter, rect, *args: painter.fillRect(rect, QColor("#075322"))
    before = deepcopy(scene.network.as_records())
    view.resize(720, 500)
    view.show()
    APP.processEvents()
    view.set_isometric(True)
    view.fit_network()
    view.scale(2., 2.)
    APP.processEvents()
    calls, draw = [], view.drawBackground
    view.drawBackground = lambda painter, rect: (calls.append(rect), draw(painter, rect))
    try:
        QTest.mousePress(view.viewport(), Qt.MouseButton.RightButton, pos=QPoint(250, 200))
        _move(view, QPoint(265, 210))
        image = _viewport_image(view)
        assert view._pan_image is not None
        painted = len(calls)
        _move(view, QPoint(275, 215))
        cached = _viewport_image(view)
        assert len(calls) == painted, "Small drag redrew vector scenery"
        assert cached != image
        _move(view, QPoint(490, 225))
        last_cached = _viewport_image(view)
        assert len(calls) > painted, "Crossing the overscan edge did not refresh"
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.RightButton, pos=QPoint(490, 225))
        assert view._pan_image is None
        fresh = _viewport_image(view)
        assert last_cached == fresh, "Drag image differs from fresh vector drawing"
        assert scene.network.as_records() == before and scene.frame is None
        # Objects retain their real scene items and hit targets throughout the cached drag.
        road = next(iter(scene.network.roads.values()))
        a, b = scene.network.road_points(road)[0], scene.network.road_points(road)[-1]
        assert any(item.data(0) == ("road", road.id) for item in scene.items(
            QPointF((a[0]+b[0])/2, (a[1]+b[1])/2)))
        QTest.mousePress(view.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(250, 200))
        _move(view, QPoint(265, 210))
        _viewport_image(view)
        assert view._pan_image is not None
        view.scale(1.1, 1.1)
        _viewport_image(view)
        assert view._pan_transform.m11() == view.viewportTransform().m11()
        scene.rebuild()
        assert view._pan_image is None
        _viewport_image(view)
        assert view._pan_image is not None
        captured = view._pan_image
        overlay = QGraphicsRectItem(0, 0, 10, 10)
        scene.addItem(overlay)
        APP.processEvents()
        assert view._pan_image is not captured, "An extension overlay edit left stale captured pixels"
        scene.show_frame(.5, Frame(1., (), {}, 0, 0))
        _viewport_image(view)
        assert view._pan_image is None, "A live run must never use the frozen editing image"
    finally:
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.MiddleButton, pos=QPoint(265, 210))
        view.close()
        view.background.close()
        folder.cleanup()


def test_static_drag_reuses_pixels_refreshes_at_edges_and_keeps_picking_and_release():
    _check_static_drag()
    _check_static_drag(map_background=True)


def test_compiled_vectors_keep_pixels_clipping_hints_and_world_scale_at_high_dpi():
    drawing = _FaceDrawing(((QColor("#aa5522"), QPolygonF([QPointF(.1, .2), QPointF(7.3, 2.7),
                                                         QPointF(1.4, 8.1)])),))
    for dpi, ratio in ((96., 1.), (144., 1.), (192., 2.)):
        for aa in (False, True):
            images = []
            for compiled in (False, True):
                image = QImage(200, 200, QImage.Format.Format_RGB32)
                image.setDevicePixelRatio(ratio)
                image.setDotsPerMeterX(round(dpi / .0254))
                image.setDotsPerMeterY(round(dpi / .0254))
                image.fill(QColor("#123456"))
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, aa)
                painter.setWorldTransform(projection_transform(True, 7.))
                painter.translate(8.25, .75)
                painter.setClipRect(QRectF(0., 0., 6., 9.))
                painter.setPen(Qt.PenStyle.NoPen)
                before = painter.worldTransform()
                if compiled:
                    drawing.paint(painter)
                else:
                    for colour, polygon in drawing.faces:
                        painter.setBrush(colour)
                        painter.drawPolygon(polygon)
                assert painter.worldTransform() == before
                assert bool(painter.renderHints() & QPainter.RenderHint.Antialiasing) == aa
                painter.end()
                images.append(image)
            assert images[0] == images[1], (dpi, ratio, aa)


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("ok")
