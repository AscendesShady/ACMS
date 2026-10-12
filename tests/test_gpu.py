"""The map draws on the GPU: the renderer is a real GPU, never a CPU stand-in; the vehicles come out
of one instanced draw where they are (moved part way, turned, and still visible zoomed far out); a
frame's poses go to the GPU once and each display frame after that costs a fraction of a millisecond
for 5,000 vehicles; and without OpenGL, QPainter draws them instead.

The GPU checks run in a child process on the Windows display driver, drawing into a hidden
framebuffer, so they stay headless: no window opens. They fail on a PC without a usable GPU, which is
the point. Run with `python tests/test_gpu.py` (or the test monitor)."""

import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator.gpu import software  # noqa: E402
from traffic_simulator.network_generator import generate_network  # noqa: E402
from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView  # noqa: E402
from traffic_simulator.simulation import Frame, VehicleState  # noqa: E402

APP = QApplication.instance() or QApplication([])


def vehicle(name, colour, before, now, length=20.0, width=10.0):
    return VehicleState(name, "Car", colour, length, width, *now, before, 0.0, "", "", 0.0, "", "")


def sample_frame():
    """Three vehicles on grass between the grid's roads, at 1 px a metre over (0, 0)-(400, 200)."""
    return Frame(0.0, (
        vehicle("A", "#ff0000", (100.0, 100.0, 1.0, 0.0), (140.0, 100.0, 1.0, 0.0)),  # moving east: drawn half way
        vehicle("B", "#0000ff", (300.0, 60.0, 0.0, 1.0), (300.0, 60.0, 0.0, 1.0), 30.0, 8.0),  # heading south
        vehicle("C", "#00ff00", (620.0, 250.0, 1.0, 0.0), (620.0, 250.0, 1.0, 0.0)),  # off the view, on grass
    ), {}, 3, 0)


def near(image, x, y, colour):
    got = image.pixelColor(x, y)
    want = QColor(colour)
    return max(abs(got.red() - want.red()), abs(got.green() - want.green()), abs(got.blue() - want.blue())) < 40


def render(scene, source: QRectF, size=(400, 200), painter_device=None):
    """The scene's `source` rectangle drawn onto a raster image (the offscreen, CPU path)."""
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(0)  # where the source's aspect leaves it uncovered
    painter = QPainter(image)
    scene.render(painter, QRectF(0, 0, *size), source)
    painter.end()
    return image


def test_cpu_stand_ins_are_told_from_gpus():
    for name in ("llvmpipe (LLVM 15.0.7, 256 bits)", "GDI Generic", "Microsoft Basic Render Driver",
                 "Google SwiftShader", "Mesa Offscreen"):
        assert software(name), name
    for name in ("NVIDIA GeForce RTX 3070/PCIe/SSE2", "AMD Radeon(TM) Graphics", "Intel(R) UHD Graphics 630"):
        assert not software(name), name


def test_without_opengl_qpainter_draws_the_vehicles():
    scene = NetworkGraphicsScene(generate_network(2, 2))
    scene.rebuild()
    scene.show_frame(0.5, sample_frame())
    image = render(scene, QRectF(0, 0, 400, 200))
    assert near(image, 120, 100, "#ff0000") and not near(image, 100, 100, "#ff0000")  # half way, 20 m long
    assert near(image, 300, 48, "#0000ff") and not near(image, 290, 60, "#0000ff")  # turned to run south
    assert scene.gpu.draws == 0 and not scene.gpu.failed  # a raster painter: the GPU path wasn't tried
    view = NetworkView(scene)
    assert not view.on_gpu and view.renderer == ""  # offscreen: raster, the driver not asked


def differing(a, b):
    """How many pixels (every other one each way) of two same-size images aren't near each other."""
    return sum(not near(a, x, y, b.pixelColor(x, y).name()) for x in range(0, a.width(), 2) for y in range(0, a.height(), 2))


def test_a_run_draws_the_still_network_once_and_keeps_it():
    from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsView

    from traffic_simulator.network_scene import STILL

    no_contents = QGraphicsItem.GraphicsItemFlag.ItemHasNoContents
    network = generate_network(2, 2)
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    view = NetworkView(scene)
    view.resize(420, 320)
    view.show()  # offscreen: a hidden view resizes (and drops) its cache each paint
    APP.processEvents()
    view.fit_network()
    editing, editing_render = view.viewport().grab().toImage(), render(scene, scene.network_rect)
    still = [item for item in scene.items() if item.data(STILL)]
    lamps = [lamp for lamp, _ in scene.signal_lamps.values()]
    assert still and lamps and not any(lamp.data(STILL) for lamp in lamps)
    assert view.cacheMode() == QGraphicsView.CacheModeFlag.CacheNone and not any(item.flags() & no_contents for item in still)

    scene.show_frame(1.0, Frame(0.0, (), {}, 0, 0))  # a run: the still layer goes into the view's cache
    assert scene.still_cached and view.cacheMode() == QGraphicsView.CacheModeFlag.CacheBackground
    assert all(item.flags() & no_contents for item in still) and not any(lamp.flags() & no_contents for lamp in lamps)
    running = view.viewport().grab().toImage()
    assert differing(editing, running) == 0  # the same picture, drawn once
    painted, paint_still = [], scene.paint_still
    scene.paint_still = lambda painter, rect: (painted.append(rect), paint_still(painter, rect))
    scene.show_frame(0.5, Frame(0.5, (), {}, 0, 0))
    view.viewport().grab()
    assert not painted  # the next frame: from the cache
    view.scale(2, 2)  # zoomed: drawn again at the new scale
    zoomed_running = view.viewport().grab().toImage()
    assert painted
    del scene.paint_still
    assert differing(editing_render, render(scene, scene.network_rect)) == 0  # scene.render too (screenshots)
    road = next(iter(network.roads.values()))
    (x0, y0), *_, (x1, y1) = network.road_points(road)
    x, y = (x0 + x1) / 2, (y0 + y1) / 2  # half way along the (straight) road
    hits = [item.data(0) for item in scene.items(QPointF(x, y)) if item.data(0)]
    assert ("road", road.id) in [tuple(hit) for hit in hits]  # still clickable

    scene.set_selection("road", road.id)  # an edit mid-run: rebuilt, its new items cached too, the cache redrawn
    assert all(item.flags() & no_contents for item in scene.items() if item.data(STILL))
    selected_running = view.viewport().grab().toImage()
    extra = QGraphicsRectItem(x - 5, y - 5, 10, 10)  # as an extension adds: drawn live, never cached
    extra.setBrush(QColor("#ff00ff"))
    scene.addItem(extra)
    assert not extra.data(STILL) and not extra.flags() & no_contents
    centre = view.mapFromScene(QPointF(x, y))
    assert near(view.viewport().grab().toImage(), centre.x(), centre.y(), "#ff00ff")
    scene.removeItem(extra)

    scene.show_frame(1.0, None)  # the run is over: edit mode paints every item, as before
    assert view.cacheMode() == QGraphicsView.CacheModeFlag.CacheNone
    assert not any(item.flags() & no_contents for item in scene.items())
    assert differing(selected_running, view.viewport().grab().toImage()) == 0
    scene.set_selection("")
    assert differing(zoomed_running, view.viewport().grab().toImage()) == 0


def test_power_saver_draws_half_as_often():
    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window._ask_to_save = False
    assert window.power_button.toolTip() == "Power Saver" and window.clock._timer.interval() == 16
    window.power_button.setChecked(True)
    assert window.clock._timer.interval() == 32
    window.power_button.setChecked(False)
    assert window.clock._timer.interval() == 16
    window.close()


def _child(name: str) -> str:
    result = subprocess.run([sys.executable, __file__, "--child", name], capture_output=True, text=True, timeout=300,
                            env={**os.environ, "QT_QPA_PLATFORM": "windows"})
    assert result.returncode == 0, f"GPU check {name!r} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def test_the_map_draws_on_a_real_gpu():
    _child("renderer")


def test_the_gpu_draws_the_vehicles_where_they_are():
    _child("vehicles")


def test_a_display_frame_costs_no_work_per_vehicle():
    _child("speed")


def test_a_run_in_the_real_window_draws_on_the_gpu():
    _child("window")


# --- the child process: the real display driver, a hidden framebuffer --------------------------------

def _gl_painter(width=400, height=200):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
    from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLPaintDevice

    context = QOpenGLContext()
    assert context.create(), "no OpenGL context"
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    assert context.makeCurrent(surface)
    form = QOpenGLFramebufferObjectFormat()
    form.setAttachment(QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
    form.setSamples(4)  # as the map's viewport
    fbo = QOpenGLFramebufferObject(QSize(width, height), form)
    fbo.bind()
    device = QOpenGLPaintDevice(QSize(width, height))
    return context, surface, fbo, device


def child_renderer():
    from traffic_simulator.gpu import renderer

    name = renderer()
    assert name, "no OpenGL driver answered"
    assert not software(name), f"OpenGL would draw on the CPU: {name}"
    print(name)


def child_vehicles():
    context, surface, fbo, device = _gl_painter()
    scene = NetworkGraphicsScene(generate_network(2, 2))
    scene.rebuild()
    scene.show_frame(0.5, sample_frame())
    painter = QPainter(device)
    scene.render(painter, QRectF(0, 0, 400, 200), QRectF(0, 0, 400, 200))
    painter.end()
    image = fbo.toImage()
    assert scene.gpu.failed == "", scene.gpu.failed
    assert scene.gpu.draws == 1 and scene.gpu.uploads == 1  # all of them in one instanced call
    assert near(image, 120, 100, "#ff0000") and not near(image, 100, 100, "#ff0000")  # half way, 20 m long
    assert near(image, 300, 48, "#0000ff") and not near(image, 290, 60, "#0000ff")  # turned, the right way up
    fbo.bind()  # ending a painter unbinds it
    painter = QPainter(device)  # zoomed far out: 20 m a pixel, a 20 m car still a few pixels across
    painter.fillRect(0, 0, 400, 200, QColor("#000000"))
    scene.render(painter, QRectF(0, 0, 400, 200), QRectF(0, 0, 8000, 4000))
    painter.end()
    image = fbo.toImage()
    assert near(image, 31, 12, "#00ff00") and near(image, 32, 13, "#00ff00")
    assert scene.gpu.uploads == 1  # the same frame: not sent again


def child_speed():
    from PySide6.QtWidgets import QStyleOptionGraphicsItem

    context, surface, fbo, device = _gl_painter(1600, 900)
    scene = NetworkGraphicsScene(generate_network(4, 4))
    scene.rebuild()
    rng = random.Random(1)
    vehicles = []
    for index in range(5000):
        x, y, angle = rng.uniform(0, 2000), rng.uniform(0, 2000), rng.uniform(0, 6.283)
        ux, uy = __import__("math").cos(angle), __import__("math").sin(angle)
        vehicles.append(vehicle(str(index), ("#3b82f6", "#f97316", "#22c55e")[index % 3], (x - ux, y - uy, ux, uy),
                                (x, y, ux, uy), 4.5, 1.8))
    frame = Frame(0.0, tuple(vehicles), {}, 5000, 0)
    scene.show_frame(0.0, frame)
    painter = QPainter(device)
    painter.scale(0.45, 0.45)  # 2 km across 900 px
    layer, option = scene.vehicle_layer, QStyleOptionGraphicsItem()
    layer.paint(painter, option)  # the first: builds the shader and sends the poses
    functions = context.functions()
    functions.glFinish()
    start = time.perf_counter()
    for step in range(120):  # two seconds of display frames between two physics steps
        scene.frame_alpha = step / 120
        layer.paint(painter, option)
    functions.glFinish()  # the GPU's work counted too
    on_gpu = (time.perf_counter() - start) / 120 * 1000
    assert scene.gpu.uploads == 1 and scene.gpu.draws == 121, (scene.gpu.uploads, scene.gpu.draws)
    scene.gpu.failed = "timing the QPainter path"
    start = time.perf_counter()
    for step in range(5):
        layer.paint(painter, option)
    functions.glFinish()
    on_cpu = (time.perf_counter() - start) / 5 * 1000
    painter.end()
    print(f"5000 vehicles a display frame: GPU {on_gpu:.3f} ms, QPainter {on_cpu:.1f} ms")
    assert on_gpu < 2.0, f"{on_gpu:.2f} ms a frame on the GPU"
    assert on_gpu * 20 < on_cpu, (on_gpu, on_cpu)


def samples(view) -> int:
    """The map's OpenGL framebuffer's multisampling (QOpenGLWidget.format() doesn't say: it reports the context's)."""
    viewport = view.viewport()
    viewport.makeCurrent()
    try:
        return viewport.context().functions().glGetIntegerv(0x80A9)  # GL_SAMPLES
    finally:
        viewport.doneCurrent()


def child_window():
    """The editor itself, created but never put on screen, running traffic at 10×."""
    from PySide6.QtCore import Qt
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
    from PySide6.QtTest import QTest

    from traffic_simulator.main import MainWindow

    window = MainWindow()
    window.results_folder = Path(tempfile.mkdtemp())  # its run's workbook: not into the user's Documents
    window._ask_to_save = False  # a real display, but nobody to answer "Save the changes?" on close
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(1400, 900)
    window.show()
    QTest.qWait(200)
    view, scene = window.view, window.scene
    assert view.on_gpu and isinstance(view.viewport(), QOpenGLWidget), view.renderer
    assert "Drawing on the GPU" in window.map_controls
    origins, _ = window._od_ends()
    window._od_command("set_od_demand", {"origins": origins, "demand": "hourly", "value": 120})
    window.speed_combo.setCurrentIndex(window.speed_combo.findData(10))
    window.run_action.trigger()
    draws, uploads = scene.gpu.draws, scene.gpu.uploads
    QTest.qWait(6000)  # a minute of traffic
    frame, runner = window.runner.frame, window.runner
    draws, uploads = scene.gpu.draws - draws, scene.gpu.uploads - uploads
    print(f"{len(frame.vehicles)} vehicles; {draws} display frames drawn on the GPU, {uploads} physics frames sent; "
          f"physics at {runner.speed_now:.1f}x of 10x")
    assert frame.vehicles and not scene.gpu.failed, scene.gpu.failed
    assert draws > 150, draws  # the vehicles went through the GPU every display frame (about 60 a second)...
    assert 0 < uploads <= draws  # ...and each physics frame was sent to it once, however often it was drawn
    assert runner.speed_now > 8, runner.speed_now  # drawing leaves the physics its pace
    assert view.cacheMode() == view.CacheModeFlag.CacheBackground  # the still network: drawn once, kept
    assert samples(view) == 4
    window.power_button.setChecked(True)  # Power Saver: a new viewport without multisampling, still the GPU's
    QTest.qWait(200)
    draws = scene.gpu.draws
    QTest.qWait(2000)
    draws = scene.gpu.draws - draws
    print(f"Power Saver: {draws} display frames in 2 s")
    assert isinstance(view.viewport(), QOpenGLWidget) and samples(view) == 0
    corner = window.power_button
    assert view.childAt(corner.mapTo(view, corner.rect().center())) is corner  # still over the new viewport
    assert 0 < draws <= 70 and not scene.gpu.failed, (draws, scene.gpu.failed)  # about 30 a second
    window.power_button.setChecked(False)
    QTest.qWait(200)
    assert samples(view) == 4
    window._stop_run()
    window.close()


if __name__ == "__main__":
    if "--child" in sys.argv:
        globals()["child_" + sys.argv[sys.argv.index("--child") + 1]]()
        raise SystemExit(0)
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
    print("ok")
