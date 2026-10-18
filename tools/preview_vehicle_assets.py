r"""Capture procedural vehicle skins and the real Qt colour/building controls, offscreen.

    .venv\Scripts\python.exe tools\preview_vehicle_assets.py --output reports/vehicle-assets

Gallery vehicles retain their actual metre dimensions; this is a raster layout preview.
Use tests/test_vehicle_assets.py --gpu separately for the hardware framebuffer check.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"))

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QTransform  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from traffic_simulator import theme, vehicle_assets  # noqa: E402
from traffic_simulator.main import MainWindow  # noqa: E402
from traffic_simulator.institutions import footprint_problem  # noqa: E402
from traffic_simulator.models import Institution  # noqa: E402
from traffic_simulator.network_scene import BuildingItem, NetworkGraphicsScene, VehicleLayer, projection_transform  # noqa: E402
from traffic_simulator.simulation import Frame, VehicleState  # noqa: E402


def gallery(path: Path) -> None:
    specs = [("Sedan", "passenger", 5., 1.8, 1), ("Hatchback", "passenger", 5., 1.8, 2),
             ("Utility car", "passenger", 5., 1.8, 3), ("Bus / coach", "bus", 12., 2.5, 4),
             ("Ambulance", "emergency", 6.5, 2.16, 5), ("Bicycle", "bicycle", 1.6, .65, 6),
             ("Motorbike", "motorcycle", 2.2, .9, 7), ("Van / truck", "delivery", 6.5, 2.16, 8)]
    vehicles = []
    for index, (name, kind, length, width, wanted) in enumerate(specs):
        x, y = 12 + index % 4 * 20, 12 + index // 4 * 20
        colour = theme.VEHICLE_PAINTS[(index + 1) % 12]
        vehicle = VehicleState(name, name, colour, length, width, x, y, 1., 0., (x, y, 1., 0.),
                               0., "", "", 0., "", "", vehicle_class=kind)
        if kind == "passenger":
            suffix = next(i for i in range(100) if vehicle_assets.style(vehicle._replace(id=f"{name}{i}")) == wanted)
            vehicle = vehicle._replace(id=f"{name}{suffix}")
        vehicles.append(vehicle)
    scene = NetworkGraphicsScene()
    scene.setSceneRect(-100, -100, 300, 300)
    scene.frame = Frame(0., tuple(vehicles), {}, 8, 0)
    scene.vehicle_layer = VehicleLayer(scene)
    for iso in (False, True):
        image = QImage(1440, 900, QImage.Format.Format_RGB32)
        image.fill(QColor(theme.WINDOW))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = projection_transform(iso, 16.)
        t = QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 380 if iso else 40, 110)
        painter.setWorldTransform(t)
        scene.isometric = iso
        scene.vehicle_layer.paint(painter, None)
        painter.resetTransform()
        painter.setPen(QColor(theme.TEXT))
        painter.setFont(QFont("Segoe UI", 20, QFont.Weight.DemiBold))
        painter.drawText(40, 45, "ACMS  /  Low-poly vehicle models  /  " + ("Orthographic 2.5D" if iso else "Top-down"))
        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor(theme.SECONDARY_TEXT))
        painter.drawText(40, 76, "One shared scale: vehicle footprints use physical metres. Body height is cosmetic.")
        for vehicle, (name, _, length, width, _) in zip(vehicles, specs):
            at = t.map(QPointF(vehicle.x, vehicle.y))
            painter.setPen(QColor(theme.TEXT))
            below=40+(vehicle.length_m+vehicle.width_m)/2*16*.4083
            painter.drawText(int(at.x() - 80), int(at.y() + below), name)
            painter.setPen(QColor(theme.SECONDARY_TEXT))
            painter.drawText(int(at.x() - 80), int(at.y() + below + 22), f"{length:g} × {width:g} m")
        painter.end()
        assert image.save(str(path / ("gallery-isometric.png" if iso else "gallery-top-down.png")))
    details = QImage(1440, 520, QImage.Format.Format_RGB32)
    details.fill(QColor(theme.WINDOW))
    painter = QPainter(details)
    for i, at in enumerate((0, 5)):
        vehicle = vehicles[at]._replace(x=0., y=0., prev=(0., 0., 1., 0.))
        scene.frame = Frame(0., (vehicle,), {}, 1, 0)
        scene.isometric = True
        t = projection_transform(True, 300 / max(vehicle.length_m, vehicle.width_m))
        painter.setWorldTransform(QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 360 + 720 * i, 350))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scene.vehicle_layer.paint(painter, None)
        painter.resetTransform()
        painter.setPen(QColor(theme.TEXT))
        painter.setFont(QFont("Segoe UI", 18))
        painter.drawText(40 + 720 * i, 45, specs[at][0] + "  /  Geometry detail")
        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor(theme.SECONDARY_TEXT))
        painter.drawText(40 + 720 * i, 485, "Detail fitted per type; overview gallery uses one shared physical scale.")
    painter.end()
    assert details.save(str(path / "car-bike-details.png"))


def building_gallery(path):
    scene = NetworkGraphicsScene()
    specs = (("Residential", 20.), ("School", 40.), ("Hospital", 40.), ("Office", 30.), ("Shop", 25.))
    for iso in (False, True):
        scene.isometric = iso
        image = QImage(1440, 900, QImage.Format.Format_RGB32)
        image.fill(QColor(theme.WINDOW))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = projection_transform(iso, 6.)
        t = QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 450 if iso else 160, 160)
        labels = []
        for i, (kind, side) in enumerate(specs):
            x, y = i % 3 * 75., i // 3 * 75.
            institution = Institution(f"IN{i+1}", kind, x, y, side * side, 100.)
            item = BuildingItem(scene, institution)
            painter.setWorldTransform(t)
            painter.translate(x, y)
            item.paint(painter, None)
            at = t.map(QPointF(x, y))
            bottom=max(t.map(QPointF(x+dx*side/2,y+dy*side/2)).y() for dx in (-1,1) for dy in (-1,1))
            labels.append((at.x() - 105, bottom+32, kind, side, item.height_m))
        painter.resetTransform()
        painter.setPen(QColor(theme.TEXT))
        painter.setFont(QFont("Segoe UI", 20, QFont.Weight.DemiBold))
        painter.drawText(40, 45, "ACMS  /  Institution structures  /  " + ("Orthographic 2.5D" if iso else "2D footprints"))
        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(QColor(theme.SECONDARY_TEXT))
        painter.drawText(40, 76, "Footprints and heights use metres. Heights shown here are assumed type defaults, editable in the app.")
        for x, y, kind, side, height in labels:
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(int(x), int(y), f"{kind}  /  {height:g} m high")
            painter.setPen(QColor(theme.SECONDARY_TEXT))
            painter.drawText(int(x), int(y + 23), f"{side:g} × {side:g} m footprint")
        painter.end()
        assert image.save(str(path / ("buildings-isometric.png" if iso else "buildings-top-down.png")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    gallery(args.output)
    building_gallery(args.output)
    window = MainWindow()
    window._ask_to_save = False
    window.resize(1440, 900)
    window.show()
    try:
        window.tabs.setCurrentWidget(window.behavior_tab)
        window.behavior_tab._pick(1)
        window.vehicle_modes.set_colour(1, None)
        window.view.set_isometric(True)
        assert footprint_problem(window.network, 250, 250, 10000) is None
        window.network.institutions["IN1"] = Institution("IN1", "Residential", 250, 250, 10000, 400, people=200)
        window.scene.rebuild()
        window.view.fit_network()
        for on, name in ((True, "buildings-shown.png"), (False, "buildings-hidden.png")):
            window.scene.set_buildings_visible(on)
            for _ in range(12):
                app.processEvents()
            window.repaint()
            app.processEvents()
            assert window.grab().save(str(args.output / name))
        window.behavior_tab.colour.showPopup()
        app.processEvents()
        assert window.grab().save(str(args.output / "colour-controls.png"))
        assert window.behavior_tab.colour.view().window().grab().save(str(args.output / "colour-menu.png"))
        window.behavior_tab.colour.hidePopup()
        window.tabs.setCurrentIndex(3)
        window.od_panel.demand.setCurrentIndex(window.od_panel.demand.findData("institutions"))
        window.scene.set_buildings_visible(True)
        window.select("institution","IN1")
        window.institution_panel.selected_height.setValue(21.)
        window.view.setTransform(projection_transform(True,4.))
        window.view.centerOn(250,250)
        for _ in range(12):
            app.processEvents()
        window.repaint()
        app.processEvents()
        assert window.grab().save(str(args.output / "height-controls.png"))
    finally:
        window.close()
    print("Vehicle galleries and native Qt controls captured")


if __name__ == "__main__":
    main()
