"""Orthographic camera and 2.5D skin: world metres, speeds and immutable frames stay authoritative."""

import os
import sys
from copy import deepcopy
from math import hypot, radians, sin, sqrt

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsTextItem  # noqa: E402

from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_scene import (ISOMETRIC_ELEVATION_DEG, ISOMETRIC_YAW_DEG, NetworkGraphicsScene,
                                             NetworkView, projection_lift, projection_transform)  # noqa: E402
from traffic_simulator.simulation import Frame, VehicleState  # noqa: E402

APP = QApplication.instance() or QApplication([])


def settle():
    for _ in range(8):
        APP.processEvents()


def view_with():
    scene = NetworkGraphicsScene(generate_network(2, 2))
    scene.rebuild()
    view = NetworkView(scene)
    view.resize(900, 700)
    view.show()
    settle()
    view.fit_network()
    return scene, view


def distance(a, b):
    return hypot(a.x() - b.x(), a.y() - b.y())


def state(name, x, y, before=None, length=8.0, width=3.0):
    return VehicleState(name, "Car", "#ef5533", length, width, x, y, 1.0, 0.0,
                        before or (x, y, 1.0, 0.0), 12.0, "", "", 0.0, "", "")


def test_projection_is_parallel_translation_invariant_and_zero_roll():
    assert ISOMETRIC_YAW_DEG == 45 and 35 <= ISOMETRIC_ELEVATION_DEG <= 45
    transform = projection_transform(True)
    for direction in (QPointF(100, 0), QPointF(0, 100), QPointF(100, 100)):
        vectors = [transform.map(origin + direction) - transform.map(origin)
                   for origin in (QPointF(), QPointF(1000, 1000), QPointF(-5000, 3000))]
        assert all(distance(vector, vectors[0]) < 1e-9 for vector in vectors)
    upright = transform.map(projection_lift()) - transform.map(QPointF())
    assert abs(upright.x()) < 1e-12 and upright.y() < 0  # height goes straight up, with no roll
    assert transform.isAffine() and transform.m13() == transform.m23() == 0 and transform.m33() == 1


def test_equal_world_speed_keeps_natural_directional_foreshortening():
    transform = projection_transform(True)
    speed, seconds = 12.0, 5.0
    a = QPointF(speed * seconds / sqrt(2), speed * seconds / sqrt(2))
    b = QPointF(speed * seconds / sqrt(2), -speed * seconds / sqrt(2))
    assert abs(distance(a, QPointF()) - distance(b, QPointF())) < 1e-12
    into, across = distance(transform.map(a), QPointF()), distance(transform.map(b), QPointF())
    assert into < across and abs(into / across - sin(radians(ISOMETRIC_ELEVATION_DEG))) < 1e-12


def test_toggle_preserves_world_frame_zoom_selection_and_centre():
    scene, view = view_with()
    snapshot = deepcopy(scene.network)
    frame = Frame(7.0, (state("A", 200, 100, (190, 100, 1, 0)),), {}, 1, 0)
    scene.set_selection("road", next(iter(scene.network.roads)))
    scene.show_frame(0.35, frame)
    view.scale(3, 3)
    view.centerOn(250, 250)
    centre = view.mapToScene(view.viewport().rect().center())
    zoom = view.projection_scale()
    selection = scene.selection
    for on in (True, False, True, False):
        view.set_isometric(on)
        settle()
        assert view.isometric == scene.isometric == on
        assert scene.frame is frame and scene.frame_alpha == 0.35 and scene.selection == selection
        assert scene.network == snapshot
        assert abs(view.projection_scale() - zoom) < 1e-12
        assert distance(view.mapToScene(view.viewport().rect().center()), centre) < 4 / zoom
    view.close()


def test_fit_reset_and_wheel_keep_the_isometric_camera():
    scene, view = view_with()
    view.set_isometric(True)
    view.fit_network()
    settle()
    rect = view.mapFromScene(scene.network_rect).boundingRect()
    assert rect.left() >= 85 and rect.top() >= 85
    assert rect.right() < view.viewport().width() - 85 and rect.bottom() < view.viewport().height() - 85
    before = view.projection_scale()
    point = view.viewport().rect().center()
    event = QWheelEvent(QPointF(point), QPointF(view.viewport().mapToGlobal(point)), QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(event)
    assert abs(view.projection_scale() / before - 1.18) < 1e-12
    assert view.transform().m21() < 0 and view.transform().m12() > 0
    view.reset_view()
    assert view.isometric and abs(view.projection_scale() - 1.0) < 1e-12
    view.close()


def test_clicks_and_pointed_coordinates_are_inverse_projected_metres():
    scene, view = view_with()
    view.set_isometric(True)
    view.fit_network()
    clicked, pointed = [], []
    scene.clicked.connect(lambda *args: clicked.append(args))
    view.pointed.connect(lambda x, y: pointed.append((x, y)))
    road = next(iter(scene.network.roads.values()))
    a, b = scene.network.road_points(road)[0], scene.network.road_points(road)[-1]
    world = QPointF((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    point = view.mapFromScene(world)
    QTest.mouseMove(view.viewport(), point + QPoint(3, 3))
    QTest.mouseMove(view.viewport(), point)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    settle()
    assert clicked and clicked[-1][:2] == ("road", road.id)
    assert distance(QPointF(*clicked[-1][2:4]), world) < 3 / view.projection_scale()
    assert pointed and distance(QPointF(*pointed[-1]), world) < 3 / view.projection_scale()
    # Screen-sized text stays upright when the ground turns and compresses.
    for label in scene.items():
        if isinstance(label, QGraphicsTextItem):
            transform = label.deviceTransform(view.viewportTransform())
            assert abs(transform.m12()) < 1e-12 and abs(transform.m21()) < 1e-12
    view.close()


def test_isometric_vehicle_bodies_keep_exact_footprints_at_every_depth_and_zoom():
    scene, view = view_with()
    frame = Frame(10.0, (state("A", 100, 100), state("B", 300, 300)), {}, 2, 0)
    scene.show_frame(0.5, frame)
    view.set_isometric(True)

    class Painter:
        def __init__(self, transform):
            self.transform, self.polygons = transform, []
        def worldTransform(self): return self.transform
        def setPen(self, value): pass
        def setBrush(self, value): pass
        def drawPolygon(self, points): self.polygons.append(list(points))
        def drawPoints(self, points): raise AssertionError("Isometric bodies must not be enlarged into screen dots")

    old_draw = scene.gpu.draw
    scene.gpu.draw = lambda *args: False  # inspect the CPU geometry without an OpenGL context
    try:
        for zoom in (0.01, 0.1, 1.0, 5.0):
            painter = Painter(projection_transform(True, zoom))
            scene.vehicle_layer.paint(painter, None)
            roofs = painter.polygons[2::3]  # two camera-facing walls followed by the roof
            assert len(roofs) == 2
            for roof in roofs:
                assert abs(distance(roof[0], roof[1]) - frame.vehicles[0].width_m) < 1e-10
                assert abs(distance(roof[1], roof[2]) - frame.vehicles[0].length_m) < 1e-10
            offsets = [b - a for a, b in zip(roofs[0], roofs[1])]
            assert all(distance(offset, QPointF(200, 200)) < 1e-9 for offset in offsets)
    finally:
        scene.gpu.draw = old_draw
        view.close()


def test_both_views_render_the_same_world_interpolation_without_moving_the_frame():
    scene, view = view_with()
    frame = Frame(10.0, (state("A", 140, 100, (100, 100, 1, 0)),), {}, 1, 0)
    scene.show_frame(0.5, frame)
    for on in (False, True):
        view.set_isometric(on)
        view.setTransform(projection_transform(on, 4))
        view.centerOn(120, 100)
        settle()
        image = view.viewport().grab().toImage()
        point = view.mapFromScene(QPointF(120, 100))
        assert image.pixelColor(point).red() > image.pixelColor(point).green()  # footprint, at halfway
        assert scene.frame is frame and frame.vehicles[0].x == 140 and frame.vehicles[0].speed == 12.0
    view.close()


def test_the_map_toggle_does_not_edit_or_stop_the_project():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    before, history = deepcopy(window._snapshot()), len(window.history.undo_stack)
    assert window.isometric_button.toolTip() == "3D View"
    window.isometric_button.click()
    assert window.view.isometric and window.isometric_button.isChecked()
    assert window._snapshot() == before and len(window.history.undo_stack) == history
    window.view.set_isometric(False)
    assert not window.isometric_button.isChecked()
    window.close()


def test_a_running_clock_and_runner_are_kept_when_the_camera_changes():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    try:
        window.run_action.trigger()
        runner = window.runner
        assert runner is not None and window.clock.running
        speed, time_s = window.clock.speed, window.clock.time_s
        for on in (True, False, True):
            window.isometric_button.setChecked(on)
            assert window.runner is runner and window.clock.running
            assert window.clock.speed == speed and window.clock.time_s == time_s
    finally:
        window.close()


def test_rotated_box_crossing_uses_screen_direction_and_reports_world_metres():
    scene, view = view_with()
    view.set_isometric(True)
    view.fit_network()
    settle()
    found = []
    scene.boxed.connect(lambda *args: found.append(args))
    a, b = view.mapFromScene(QPointF(100, 100)), view.mapFromScene(QPointF(150, 300))
    assert b.x() < a.x()  # screen right-to-left, despite increasing world x
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=a)
    QTest.mouseMove(view.viewport(), (a + b) / 2)
    QTest.mouseMove(view.viewport(), b)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=b)
    assert found and found[-1][4] is True
    expected = (100, 100, 150, 300)
    assert all(abs(got - want) < 3 / view.projection_scale() for got, want in zip(found[-1][:4], expected))
    view.close()


def test_bus_stop_arrow_keys_follow_the_projected_road():
    scene, view = view_with()
    stop = scene.network.add_bus_stop("1,1-2,1", "both")
    scene.set_selection("bus_stop", stop.id)
    view.set_isometric(True)
    view.fit_network()
    settle()
    before = stop.distance_m
    # This east-going road appears down/right. Down must advance along it, not be treated as world south.
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert abs(stop.distance_m - before - 0.5) < 1e-9
    QTest.keyClick(view, Qt.Key.Key_Up)
    assert abs(stop.distance_m - before) < 1e-9
    view.close()


def child_gpu():
    """Explicit hardware check: `QT_QPA_PLATFORM=windows python tests/test_projection.py --gpu`."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QTransform
    from test_gpu import _gl_painter

    context, surface, fbo, device = _gl_painter(640, 480)
    scene = NetworkGraphicsScene(generate_network(2, 2))
    scene.rebuild()
    frame = Frame(10.0, (state("A", 140, 100, (100, 100, 1, 0)), state("B", 300, 100)), {}, 2, 0)
    scene.show_frame(0.5, frame)
    scene.isometric = True

    def draw(device, zoom):
        painter = QPainter(device)
        painter.fillRect(0, 0, 640, 480, QColor("#000000"))
        t = projection_transform(True, zoom)
        t = QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 300, 40)
        painter.setWorldTransform(t)
        scene.render(painter, QRectF(0, 0, 400, 400), QRectF(0, 0, 400, 400))
        painter.end()
        return t

    transform = draw(device, 2)
    image = fbo.toImage()
    if "--capture" in sys.argv:
        assert image.save(sys.argv[sys.argv.index("--capture") + 1])
    assert scene.gpu.draws == scene.gpu.uploads == 1 and not scene.gpu.failed, scene.gpu.failed
    raster = QImage(640, 480, QImage.Format.Format_RGB32)
    draw(raster, 2)
    lift = projection_lift() * (frame.vehicles[0].width_m * 0.7)
    for position in (QPointF(120, 100), QPointF(300, 100)):
        roof = transform.map(position + lift).toPoint()
        for rendered in (image, raster):
            colour = rendered.pixelColor(roof)
            assert colour.red() > 180 and colour.green() < 130, (roof, colour.name())
    fbo.bind()
    draw(device, 0.05)
    tiny = fbo.toImage()
    coloured = sum(tiny.pixelColor(x, y).red() > 70 and
                  tiny.pixelColor(x, y).red() > tiny.pixelColor(x, y).green() * 1.5
                  for x in range(300, 330) for y in range(40, 65))
    assert coloured <= 10, coloured  # no 4-pixel-wide minimum body impostors at distant zoom
    assert scene.gpu.uploads == 1 and scene.frame is frame and frame.vehicles[0].x == 140
    print("GPU orthographic poses, roof scale, no body enlargement, shared frame: ok")
    context.doneCurrent()


def test_people_have_metre_scaled_models_and_keep_the_same_interpolated_world_poses():
    from traffic_simulator import asset_mesh
    from traffic_simulator.network_scene import VehicleLayer
    vertices = [p for f in asset_mesh.pedestrian_mesh() for p in f.vertices]
    assert max(p[2] for p in vertices) == 1.75 and min(p[2] for p in vertices) == 0.
    assert max(p[1] for p in vertices) - min(p[1] for p in vertices) == .6
    scene = NetworkGraphicsScene()
    scene.isometric = True
    scene.setSceneRect(-100, -100, 300, 300)
    scene.frame = Frame(10., (), {}, 0, 0, walkers=((20., 0., 1., 0., (0., 0., 1., 0.)),))
    layer = VehicleLayer(scene)
    class Painter:
        def __init__(self, iso): self.polygons, self.points, self.iso = [], [], iso
        def worldTransform(self): return projection_transform(self.iso)
        def setPen(self, value): pass
        def setBrush(self, value): pass
        def drawPolygon(self, points): self.polygons.append(list(points))
        def drawPoints(self, points): self.points.extend(points)
    scene.frame_alpha = 0.
    a = Painter(True)
    layer.paint(a, None)
    scene.frame_alpha = .25
    b = Painter(True)
    layer.paint(b, None)
    assert a.polygons and not a.points and len(a.polygons) == len(b.polygons)
    for pa, pb in zip(a.polygons, b.polygons):
        for p, q in zip(pa, pb):
            assert distance(q - p, QPointF(5., 0.)) < 1e-9
    flat = Painter(False)
    layer.paint(flat, None)
    assert not flat.polygons and flat.points == [QPointF(5., 0.)]
    assert scene.frame.walkers[0][0] == 20. and scene.frame_alpha == .25


def test_pedestrian_overbridge_has_height_pick_bounds_and_does_not_edit_walks_on_toggle():
    from traffic_simulator import asset_mesh
    from traffic_simulator.geometry import point_along
    scene = NetworkGraphicsScene(generate_network(2, 2))
    road = next(iter(scene.network.roads.values()))
    road.footpaths = ['left', 'right']
    along = scene.network.road_geometry_length(road) / 2
    crossing = scene.network.add_crossing(road.id, along, 'overbridge')
    before = deepcopy(scene.network.as_records())
    scene.rebuild()
    bridge = scene._overbridges[0]
    x, y, ux, uy = point_along(scene.network.road_points(road), along)
    assert bridge.height_at(x, y) == asset_mesh.FOOTBRIDGE_DECK_M == 6.
    assert bridge.height_at(x + ux * 10, y + uy * 10) == 0.
    frame = Frame(12., (), {}, 0, 0, walkers=((x, y, -uy, ux, (x, y, -uy, ux)),))
    scene.show_frame(.4, frame)
    view = NetworkView(scene)
    view.resize(900, 700)
    try:
        for on in (True, False, True):
            view.set_isometric(on)
            assert scene.frame is frame and scene.frame_alpha == .4 and scene.network.as_records() == before
            point = QPointF(x, y) + (projection_lift() * 6 if on else QPointF())
            assert bridge.shape().contains(point)
            assert bridge.boundingRect().contains(point) and scene.network_rect.contains(point)
            assert any(item.data(0) == ('crossing', crossing.id) for item in scene.items(point))
        scene.set_buildings_visible(False)
        assert bridge.isVisible()
    finally:
        view.close()


def paving_image(iso, scale, hardware=None):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainterPath, QTransform
    from traffic_simulator.network_scene import PavingItem
    path = QPainterPath()
    path.addRect(QRectF(-2., -1., 4., 2.))
    item = PavingItem(path)
    target = hardware[3] if hardware else QImage(500, 350, QImage.Format.Format_RGB32)
    if hardware:
        hardware[2].bind()
    painter = QPainter(target)
    painter.fillRect(0, 0, target.width(), target.height(), QColor('#075322'))
    t = projection_transform(iso, scale)
    painter.setTransform(QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 250., 175.))
    item.paint(painter, None)
    painter.end()
    return hardware[2].toImage() if hardware else target


def test_brick_paving_keeps_a_warm_colour_at_overview_and_readable_joints_when_zoomed():
    from traffic_simulator.theme import FOOTPATH, FOOTPATH_SHADE, FOOTPATH_MORTAR
    base = QColor(FOOTPATH)
    assert base.red() - base.green() > 40 and base.green() - base.blue() > 10
    for iso in (False, True):
        overview = paving_image(iso, 8.)
        assert overview.pixelColor(250, 175) == base
        close = paving_image(iso, 80.)
        colours = [close.pixelColor(x, y) for x in range(225, 275) for y in range(160, 190)]
        assert all(c.red() > c.green() > c.blue() for c in colours)
        assert len({c.name() for c in colours}) >= 3  # two brick tones and visible mortar
    assert len({FOOTPATH, FOOTPATH_SHADE, FOOTPATH_MORTAR}) == 3


def test_brick_paving_preserves_world_footpath_hit_shapes_and_running_network_records():
    from traffic_simulator.geometry import point_along
    from traffic_simulator.network_scene import PavingItem
    network = generate_network(2, 2)
    road = next(iter(network.roads.values()))
    road.footpaths = ['left', 'right']
    before = deepcopy(network.as_records())
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    paving = [item for item in scene.items() if isinstance(item, PavingItem)]
    assert len(paving) == 2
    for side in road.footpaths:
        line = network.footpath_line(road, side)
        x, y, ux, uy = point_along(line, 50.)
        assert any(item.shape().contains(QPointF(x, y)) for item in paving)
        assert not any(item.shape().contains(QPointF(x - uy * 1.2, y + ux * 1.2)) for item in paving)
    frame = Frame(15., (), {}, 0, 0)
    scene.show_frame(.3, frame)
    view = NetworkView(scene)
    try:
        for on in (True, False):
            view.set_isometric(on)
            assert scene.frame is frame and scene.frame_alpha == .3 and network.as_records() == before
    finally:
        view.close()


def test_spawn_labels_stay_with_screen_sized_pins_in_both_views_at_every_zoom():
    from PySide6.QtWidgets import QGraphicsTextItem
    network = generate_network(2, 2)
    point = network.add_spawn_point(250., 200.)
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    pin = next(item for item in scene.items() if item.data(0) == ('spawn', point.id))
    label = next(item for item in scene.items() if isinstance(item, QGraphicsTextItem) and item.toPlainText() == point.id)
    assert label.pos() == pin.pos() == QPointF(point.x, point.y)
    view = NetworkView(scene)
    view.resize(900, 650)
    try:
        for on in (False, True):
            view.set_isometric(on)
            for scale in (.25, 2., 40.):
                view.setTransform(projection_transform(on, scale))
                t = view.viewportTransform()
                p = pin.deviceTransform(t).mapRect(pin.boundingRect())
                l = label.deviceTransform(t).mapRect(label.boundingRect())
                assert 0 < p.top() - l.bottom() < 5
                assert abs(l.center().x() - p.center().x()) < 1e-9
    finally:
        view.close()


def verify_brick_hardware():
    from test_gpu import _gl_painter
    hardware = _gl_painter(500, 350)
    for iso in (False, True):
        image = paving_image(iso, 80., hardware)
        colours = [image.pixelColor(x, y) for x in range(225, 275) for y in range(160, 190)]
        assert all(c.red() > c.green() > c.blue() for c in colours)
        assert len({c.name() for c in colours}) >= 3
    hardware[0].doneCurrent()


def verify_people_overbridge_hardware():
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPolygonF, QTransform
    from traffic_simulator import asset_mesh
    from traffic_simulator.network_scene import OverbridgeItem, VehicleLayer
    from test_gpu import _gl_painter
    context, surface, fbo, device = _gl_painter(700, 700)
    scene = NetworkGraphicsScene()
    scene.isometric = True
    scene.setSceneRect(-100, -100, 200, 200)
    scene.frame = Frame(10., (), {}, 0, 0, walkers=((0., 0., 0., 1., (0., 0., 0., 1.)),))
    bridge = OverbridgeItem(scene, QPolygonF(QRectF(-1.5, -10, 3, 20)), 0., 0., 1., 0., 10., False)
    scene._overbridges = [bridge]
    scene.vehicle_layer = VehicleLayer(scene)
    t = projection_transform(True, 20.)
    t = QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 350., 500.)
    for target in (QImage(700, 700, QImage.Format.Format_RGB32), device):
        if target is device:
            fbo.bind()
        painter = QPainter(target)
        painter.fillRect(0, 0, 700, 700, QColor('#075322'))
        painter.setWorldTransform(t)
        scene.vehicle_layer.paint(painter, None)
        bridge.paint(painter, None)
        painter.end()
        image = fbo.toImage() if target is device else target
        head = t.map(projection_lift() * (asset_mesh.FOOTBRIDGE_DECK_M + 1.55)).toPoint()
        torso = t.map(projection_lift() * (asset_mesh.FOOTBRIDGE_DECK_M + 1.1)).toPoint()
        assert image.pixelColor(head).red() > 180 and image.pixelColor(head).blue() > 180
        assert image.pixelColor(torso).red() > 180 and image.pixelColor(torso).blue() < 130
    context.doneCurrent()


if __name__ == "__main__":
    if "--gpu" in sys.argv:
        child_gpu()
        verify_people_overbridge_hardware()
        verify_brick_hardware()
        raise SystemExit(0)
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
