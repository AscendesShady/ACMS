"""Vehicle presentation controls keep simulation geometry and random streams unchanged."""

import os
import sys
from copy import deepcopy
from dataclasses import asdict
from math import cos, sin
from pathlib import Path
from tempfile import TemporaryDirectory
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import QApplication
from traffic_simulator import asset_mesh, building_assets, vehicle_assets
from traffic_simulator.mode_behavior import ModeBehaviorPanel, VehicleModes
from traffic_simulator.models import BUILDING_HEIGHTS_M, Institution, institution_problem
from traffic_simulator.network_generator import generate_network
from traffic_simulator.network_io import load_network, project_problem, save_network, saved_project
from traffic_simulator.network_scene import NetworkGraphicsScene, NetworkView, projection_lift, projection_transform
from traffic_simulator.simulation import Frame, PairDemand, Simulation, VehicleState
from traffic_simulator.theme import VEHICLE_INKS, VEHICLE_PAINTS

APP = QApplication.instance() or QApplication([])


def test_colour_random_is_a_saved_undoable_type_choice():
    modes = VehicleModes()
    panel = ModeBehaviorPanel(modes)
    panel._pick(1)
    before = modes.state()
    index = panel.colour.findData(None)
    assert index >= 0
    panel.colour.setCurrentIndex(index)
    assert modes.modes[1].random_colour
    assert modes.modes[1].colour == before[0][1].colour
    saved = modes.state()
    modes.restore(*before)
    assert not modes.modes[1].random_colour
    modes.restore(*saved)
    panel._pick(1)
    assert panel.colour.currentText() == "Random"
    panel.colour.setCurrentIndex(0)
    assert not modes.modes[1].random_colour


def test_buildings_visibility_defaults_to_shown_and_does_not_edit_the_network():
    network = generate_network(2, 2)
    network.institutions["IN1"] = Institution("IN1", "Residential", 500, 500, 400, 100, people=500)
    before = deepcopy(network.as_records())
    scene = NetworkGraphicsScene(network)
    scene.rebuild()
    scene.set_selection("institution", "IN1")
    view = NetworkView(scene)
    view.resize(800, 600)
    view.set_isometric(True)
    view.fit_network()
    frame = Frame(10, (), {}, 0, 0)
    scene.show_frame(.4, frame)  # exercise visibility with the still layer cached during a run
    assert not scene.buildings_visible  # off by default
    scene.set_buildings_visible(True)
    bounds, transform = scene.sceneRect(), view.transform()
    centre = view.mapToScene(view.viewport().rect().center())
    assert scene._building_items and all(item.isVisible() for item in scene._building_items)
    scene.set_buildings_visible(False)
    assert not any(item.isVisible() for item in scene._building_items)
    assert not any(item.data(0) == ("institution", "IN1") for item in scene.items(QPointF(500, 500)))
    assert scene.network is network and scene.sceneRect() == bounds and view.transform() == transform
    assert view.mapToScene(view.viewport().rect().center()) == centre
    assert scene.frame is frame and scene.frame_alpha == .4 and scene.selection == ("institution", "IN1")
    assert network.as_records() == before
    scene.rebuild()
    assert not scene.buildings_visible and not any(item.isVisible() for item in scene._building_items)
    scene.set_hidden(True)
    scene.set_buildings_visible(True)
    assert not any(item.isVisible() for item in scene._building_items)
    scene.set_hidden(False)
    assert all(item.isVisible() for item in scene._building_items) and network.as_records() == before
    view.close()


def sample(identifier="car", kind="passenger", x=0., y=0., angle=0., length=5., width=1.8):
    ux, uy = cos(angle), sin(angle)
    return VehicleState(identifier, "Renamed type", VEHICLE_PAINTS[1], length, width, x, y, ux, uy,
                        (x, y, ux, uy), 10., "", "", 0., "", "", vehicle_class=kind)


def test_random_paint_is_stable_diverse_and_does_not_replace_fixed_paint():
    vehicles = [sample(str(index))._replace(random_colour=True) for index in range(200)]
    colours = [vehicle_assets.paint_colour(v, VEHICLE_PAINTS) for v in vehicles]
    assert len(set(colours)) == len(VEHICLE_PAINTS)
    assert colours == [vehicle_assets.paint_colour(v._replace(x=500, y=100), VEHICLE_PAINTS) for v in vehicles]
    assert vehicle_assets.paint_colour(sample(), VEHICLE_PAINTS) == VEHICLE_PAINTS[1]
    assert len({vehicle_assets.style(v) for v in vehicles}) == 3  # the three car variants


def test_skins_follow_classes_and_dispatch_after_type_renames():
    for kind, skin in (("bus", 4), ("coach", 4), ("emergency", 5), ("bicycle", 6),
                       ("motorcycle", 7), ("moped", 7), ("truck", 8), ("delivery", 8)):
        vehicle = sample(kind=kind)
        assert vehicle_assets.style(vehicle) == skin
        assert vehicle_assets.style(vehicle._replace(mode="Completely different name")) == skin
    assert vehicle_assets.style(sample()._replace(emergency=True)) == 5


def test_saved_random_choice_round_trips_and_invalid_edits_leave_it_untouched():
    modes = VehicleModes()
    modes.set_colour(1, None)
    state = modes.state()
    for bad in ("random", "#bad", "red", 1):
        try:
            modes.set_colour(1, bad)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid fixed colour accepted")
        assert modes.state() == state
    project = {"vehicle_types": {"types": [asdict(m) for m in modes.modes], "default_split": [10, 85, 5]}}
    assert project_problem(project) is None
    with TemporaryDirectory() as directory:
        path = Path(directory) / "colours.json"
        save_network(generate_network(1, 1), path, project=project)
        assert saved_project(path)["vehicle_types"] == project["vehicle_types"]
        before = path.read_bytes()
        project["vehicle_types"]["types"][1]["random_colour"] = "false"
        assert "random_colour" in project_problem(project)
        try:
            save_network(generate_network(1, 1), path, project=project)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid saved flag accepted")
        assert path.read_bytes() == before
    # Projects from before the flag keep fixed colours.
    for mode in project["vehicle_types"]["types"]:
        mode.pop("random_colour")
    assert project_problem(project) is None


def test_random_paint_and_snapshot_metadata_leave_seeded_motion_identical():
    modes = VehicleModes().modes
    random_modes = deepcopy(modes)
    for mode in random_modes:
        mode.random_colour = True
    demand = [PairDemand("1W", "1E", 900, None, [], "Poisson", (10, 85, 5))]
    fixed = Simulation(generate_network(1, 1), demand, modes, seed=13)
    random = Simulation(generate_network(1, 1), demand, random_modes, seed=13)
    seen = 0
    for _ in range(160):
        fixed.step()
        random.step()
        a, b = fixed.frame(), random.frame()
        assert (a.time, a.spawned, a.arrived, a.signals) == (b.time, b.spawned, b.arrived, b.signals)
        assert len(a.vehicles) == len(b.vehicles)
        for left, right in zip(a.vehicles, b.vehicles):
            assert left._replace(random_colour=True) == right
            assert right.vehicle_class == random._classes[right.mode] and right.random_colour
            seen += 1
    assert seen > 100


def test_dispatched_vehicle_publishes_ambulance_skin_without_a_configured_emergency_type():
    simulation = Simulation(generate_network(1, 1), [], VehicleModes().modes)
    simulation.dispatch("1W", "1E")
    for _ in range(10):
        simulation.step()
        vehicles = simulation.frame().vehicles
        if vehicles:
            assert vehicles[0].emergency and vehicle_assets.style(vehicles[0]) == 5
            return
    raise AssertionError("Emergency dispatch never entered the empty road")


def test_styled_models_stay_inside_physical_footprints_and_keep_round_tyres():
    for skin, length, width in ((1,5.,1.8),(2,5.,1.8),(3,5.,1.8),(4,12.,2.5),(5,6.5,2.16),
                                (6,1.6,.65),(7,2.2,.9),(8,6.5,2.16)):
        fit=asset_mesh.wheel_fit(skin,length,width)
        assert fit==1., (skin,fit)  # normal class proportions keep actual circular wheels
        for face in asset_mesh.vehicle_mesh(skin):
            for vertex in face.vertices:
                x,y,z=asset_mesh.expand(vertex,length,width,fit)
                assert abs(x)<=length/2+1e-9 and abs(y)<=width/2+1e-9 and z>=-1e-9,(skin,x,y,z)
        # Extreme edited aspect ratios still never enlarge the collision footprint.
        for length,width in ((.3,5.),(60.,.3)):
            fit=asset_mesh.wheel_fit(skin,length,width)
            points=[asset_mesh.expand(v,length,width,fit) for f in asset_mesh.vehicle_mesh(skin) for v in f.vertices]
            assert all(abs(x)<=length/2+1e-9 and abs(y)<=width/2+1e-9 for x,y,_ in points),skin


def test_window_colour_undo_and_buildings_button_are_wired():
    from traffic_simulator.main import MainWindow
    window = MainWindow()
    window._ask_to_save = False
    try:
        window.behavior_tab._pick(1)
        window.behavior_tab.colour.setCurrentIndex(window.behavior_tab.colour.findText("Random"))
        assert window._project()["vehicle_types"]["types"][1]["random_colour"]
        window.undo()
        assert not window.vehicle_modes.modes[1].random_colour
        window.redo()
        assert window.vehicle_modes.modes[1].random_colour
        assert not window.scene.buildings_visible and not window.buildings_button.isChecked()  # off by default
        window.buildings_button.click()
        assert window.scene.buildings_visible
        window.scene.set_buildings_visible(False)
        assert not window.buildings_button.isChecked()
        window.tabs.setCurrentWidget(window.demand_tab)
        window.od_panel.demand.setCurrentIndex(window.od_panel.demand.findData("institutions"))
        assert window.scene.buildings_visible and window.buildings_button.isChecked()  # placing shows them
    finally:
        window.close()


def draw_samples(scene, device, isometric, scale=18.):
    painter = QPainter(device)
    painter.fillRect(0, 0, device.width(), device.height(), QColor(VEHICLE_INKS[1]))
    t = projection_transform(isometric, scale)
    t = QTransform(t.m11(), t.m12(), t.m21(), t.m22(), 300, 230)
    painter.setWorldTransform(t)
    scene.isometric = isometric
    scene.vehicle_layer.paint(painter, None)
    painter.end()
    return t


def compare_samples(hardware=False, capture=None):
    # Strongly visible semantic marks: a bus window, ambulance cross, and a bike's tyre.
    vehicles = (sample("bus", "bus", 0, 0, length=12, width=2.5),
                sample("ambulance", "emergency", 18, 0, length=6.5, width=2.16),
                sample("bike", "bicycle", 36, 0, length=1.6, width=.65),
                sample("motorbike", "motorcycle", 0, 14, length=2.2, width=.9),
                sample("van", "delivery", 18, 14, length=6.5, width=2.16),
                sample("sedan", "passenger", 36, 14))
    scene = NetworkGraphicsScene()
    from traffic_simulator.network_scene import VehicleLayer
    scene.vehicle_layer = VehicleLayer(scene)
    scene.setSceneRect(-100, -100, 300, 300)
    scene.frame = Frame(10, vehicles, {}, 6, 0)
    for iso in (False, True):
        raster = QImage(1200, 900, QImage.Format.Format_RGB32)
        transform = draw_samples(scene, raster, iso)
        images = [raster]
        if hardware:
            context, surface, fbo, device = hardware
            fbo.bind()
            draw_samples(scene, device, iso)
            assert scene.gpu.failed == "", scene.gpu.failed
            assert scene.gpu.uploads == 1  # camera toggles and display frames reuse the same upload
            images.append(fbo.toImage())
        if capture:
            for index, image in enumerate(images):
                assert image.save(str(Path(capture) / f"assets-{'isometric' if iso else 'top-down'}-{'gpu' if index else 'raster'}.png"))
        # A roof cross remains semantically visible; these parts now live at their true 3D heights.
        vehicle=vehicles[1]
        point=QPointF(vehicle.x-.3*vehicle.length_m/2,vehicle.y)
        if iso:
            point+=projection_lift()*vehicle.width_m*1.12
        for image in images:
            actual=image.pixelColor(transform.map(point).toPoint())
            assert actual.red()>actual.green()*1.8 and actual.red()>actual.blue()*1.8,(iso,actual.name())
        if iso:
            # Every coplanar bus window must be solid, not striped by depth-buffer fighting.
            glass=QColor(VEHICLE_INKS[0])
            expected=QColor.fromRgbF(glass.redF()*.68,glass.greenF()*.68,glass.blueF()*.68)
            for x in (-.695,-.425,-.155,.115,.385,.655):
                p=QPointF(x*12/2,2.5/2)+projection_lift()*(.8*2.5)
                at=transform.map(p).toPoint()
                for image in images:
                    for dx in (-1,0,1):
                        for dy in (-1,0,1):
                            actual=image.pixelColor(at.x()+dx,at.y()+dy)
                            assert max(abs(a-b) for a,b in zip(actual.getRgb()[:3],expected.getRgb()[:3]))<8,(x,actual.name())


def test_raster_assets_show_windows_cross_and_bike_details_in_both_views():
    compare_samples()


def compare_random_paint(hardware=False):
    from traffic_simulator.network_scene import VehicleLayer
    vehicles = tuple(sample(str(i), x=i % 3 * 12, y=i // 3 * 10)._replace(random_colour=True) for i in range(9))
    scene = NetworkGraphicsScene()
    scene.setSceneRect(-100, -100, 300, 300)
    scene.vehicle_layer = VehicleLayer(scene)
    scene.frame = Frame(0, vehicles, {}, len(vehicles), 0)
    for iso in (False, True):
        raster = QImage(1200, 900, QImage.Format.Format_RGB32)
        t = draw_samples(scene, raster, iso)
        images = [raster]
        if hardware:
            _, _, fbo, device = hardware
            fbo.bind()
            draw_samples(scene, device, iso)
            images.append(fbo.toImage())
            assert not scene.gpu.failed and scene.gpu.uploads == 1
        for vehicle in vehicles:
            # Sample the hood, clear of windows and lamps in every car variant.
            point = QPointF(vehicle.x + .7 * vehicle.length_m / 2, vehicle.y)
            if iso:
                point += projection_lift() * vehicle.width_m * .43
            base = QColor(vehicle_assets.paint_colour(vehicle, VEHICLE_PAINTS))
            shade=.68+.32/2**.5
            expected=QColor.fromRgbF(base.redF()*shade,base.greenF()*shade,base.blueF()*shade)
            for image in images:
                actual = image.pixelColor(t.map(point).toPoint())
                assert max(abs(a - b) for a, b in zip(actual.getRgb()[:3], expected.getRgb()[:3])) < 5, (
                    iso, vehicle.id, actual.name(), expected.name())


def test_raster_random_paint_reaches_each_vehicle_body_in_both_views():
    compare_random_paint()


def render_vehicle(vehicle, iso=False, scale=100., hardware=False):
    from traffic_simulator.network_scene import VehicleLayer
    scene=NetworkGraphicsScene()
    scene.setSceneRect(-100,-100,300,300)
    scene.isometric=iso
    scene.frame=Frame(0,(vehicle,),{},1,0)
    layer=VehicleLayer(scene)
    image=QImage(700,700,QImage.Format.Format_RGB32)
    image.fill(QColor(VEHICLE_INKS[1]))
    device=hardware[3] if hardware else image
    if hardware:
        hardware[2].bind()
    painter=QPainter(device)
    painter.fillRect(0,0,device.width(),device.height(),QColor(VEHICLE_INKS[1]))
    t=projection_transform(iso,scale)
    t=QTransform(t.m11(),t.m12(),t.m21(),t.m22(),350,430)
    painter.setWorldTransform(t)
    layer.paint(painter,None)
    painter.end()
    if hardware:
        assert not scene.gpu.failed and scene.gpu.uploads==1
        image=hardware[2].toImage()
    return image,t


def test_bus_tyres_extrude_past_recessed_skirts_with_open_axles_inside_the_physical_width():
    faces = asset_mesh.vehicle_mesh(4)
    body = [asset_mesh.expand(p, 12., 2.5) for f in faces if f.part == 0 and f.ink == -1 for p in f.vertices]
    tyres = [asset_mesh.expand(p, 12., 2.5) for f in faces if f.part == 1 for p in f.vertices]
    assert max(abs(y) for _, y, _ in tyres) == 1.25
    lower = [(x, y, z) for x, y, z in body if z < .39 * 2.5 - 1e-9]
    assert lower and max(abs(y) for _, y, _ in lower) <= .86 * 1.25 + 1e-9
    assert max(z for _, _, z in tyres) < .39 * 2.5  # upper body never swallows the tyre crown
    # The skirt has a real opening at both axle centres, rather than a tyre decal over a flat wall.
    for axle in (-.68 * 6., .68 * 6.):
        for face in faces:
            if face.part or face.ink != -1:
                continue
            points = [asset_mesh.expand(p, 12., 2.5) for p in face.vertices]
            if max(z for _, _, z in points) <= .39 * 2.5 + 1e-9:
                assert not min(x for x, _, _ in points) < axle < max(x for x, _, _ in points)


def check_bus_extrusion_pixels(hardware=False):
    vehicle = sample(kind='bus', length=12., width=2.5)
    image, t = render_vehicle(vehicle, iso=True, scale=40., hardware=hardware)
    for x in (-.68 * 6., .68 * 6.):
        point = t.map(QPointF(x, 1.25) + projection_lift() * (.18 * 2.5)).toPoint()
        patch = [image.pixelColor(point.x() + dx, point.y() + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        assert sum(c.red() > 120 and c.green() > 120 and c.blue() > 120 for c in patch) >= 5


def test_bus_wheel_hubs_are_visible_below_the_body_in_the_isometric_raster_view():
    check_bus_extrusion_pixels()


def test_car_silhouette_has_tapered_empty_corners_and_sloped_raised_cabin():
    vehicle=sample()
    skin=vehicle_assets.style(vehicle)
    mesh=asset_mesh.vehicle_mesh(skin)
    points=[asset_mesh.expand(v,vehicle.length_m,vehicle.width_m) for f in mesh for v in f.vertices]
    assert len({round(z,3) for _,_,z in points})>5  # wheels, body, cabin: genuinely different surfaces
    assert max(z for _,_,z in points)>.7*vehicle.width_m
    assert any(.02<abs(asset_mesh.normal([asset_mesh.expand(v,5.,1.8) for v in f.vertices])[2])<.98
               for f in mesh if f.ink==0),"windscreen must slope"
    image,t=render_vehicle(vehicle)
    # The old brick filled this point near the rectangular footprint's front corner.
    corner=t.map(QPointF(vehicle.length_m*.47,vehicle.width_m*.46)).toPoint()
    assert image.pixelColor(corner)==QColor(VEHICLE_INKS[1])
    hood=t.map(QPointF(vehicle.length_m*.35,0)).toPoint()
    assert image.pixelColor(hood)!=QColor(VEHICLE_INKS[1])


def test_bicycle_has_open_frame_gaps_separate_wheels_and_no_rectangular_body():
    vehicle=sample(kind="bicycle",length=1.6,width=.65)
    mesh=asset_mesh.vehicle_mesh(6)
    assert any(f.part==1 for f in mesh) and any(f.part==0 for f in mesh)
    # Top-down silhouette leaves open space at both ends, beside the thin actual tyre.
    image,t=render_vehicle(vehicle,scale=200.)
    for point in (QPointF(.65,.24),QPointF(-.65,-.24)):
        assert image.pixelColor(t.map(point).toPoint())==QColor(VEHICLE_INKS[1])
    # The frame triangle is not a filled slab: a zoomed side-on orthographic projection reveals its hole.
    points=[asset_mesh.expand(v,1.6,.65) for f in mesh for v in f.vertices]
    assert max(z for _,_,z in points)>1.2 and min(z for _,_,z in points)<=1e-9
    assert {round(v[0],2) for f in mesh if f.part==1 for v in f.vertices}=={-.55,.55}
    tyre=asset_mesh.wheel(.55,.48,-.09,.09,True)
    radii=[((v[3]*.65)**2+((v[2]-.48)*.65)**2)**.5 for f in tyre for v in f.vertices]
    assert abs(max(radii)-.48*.65)<1e-9  # longitudinal radius equals vertical radius in metres
    image,t=render_vehicle(vehicle,iso=True,scale=200.)
    hole=QPointF(-.15,0)+projection_lift()*.46
    assert image.pixelColor(t.map(hole).toPoint())==QColor(VEHICLE_INKS[1]),"frame triangle must remain open"


def test_building_heights_are_real_metres_and_roofs_fit_the_requested_total():
    for kind,default in BUILDING_HEIGHTS_M.items():
        item=Institution("IN1",kind,100,100,900,100)
        assert building_assets.height(item)==default and item.height_m is None
        for wanted in (.5,default,75.,300.):
            item.height_m=wanted
            points=[asset_mesh.expand(v,30.,30.) for f in building_assets.mesh(kind,900,wanted) for v in f.vertices]
            assert min(z for _,_,z in points)>=-1e-9
            assert abs(max(z for _,_,z in points)-wanted)<1e-8
            assert all(abs(x)<=15.+1e-9 and abs(y)<=15.+1e-9 for x,y,_ in points)
    item=Institution("IN1","Office",100,100,900,100,height_m=18.)
    t=projection_transform(True,4.)
    ground=t.map(QPointF(100,100))
    roof=t.map(QPointF(100,100)+projection_lift()*18.)
    from math import cos,radians
    from traffic_simulator.network_scene import ISOMETRIC_ELEVATION_DEG
    assert abs(ground.x()-roof.x())<1e-9
    assert abs(ground.y()-roof.y()-18*cos(radians(ISOMETRIC_ELEVATION_DEG))*4)<1e-9


def test_building_height_changes_only_the_isometric_image_and_keeps_2d_hit_footprint():
    from traffic_simulator.network_scene import BuildingItem
    scene=NetworkGraphicsScene()
    images={}
    for iso in (False,True):
        scene.isometric=iso
        for height in (5.,40.):
            item=BuildingItem(scene,Institution("IN1","Office",0,0,400,100,height_m=height))
            image=QImage(800,800,QImage.Format.Format_RGB32)
            image.fill(QColor(VEHICLE_INKS[1]))
            painter=QPainter(image)
            t=projection_transform(iso,5.)
            painter.setWorldTransform(QTransform(t.m11(),t.m12(),t.m21(),t.m22(),400,400))
            item.paint(painter,None)
            painter.end()
            images[iso,height]=image
            assert item.footprint.width()==item.footprint.height()==20.
            if not iso:
                assert item.shape().boundingRect()==item.footprint
    assert images[False,5.]==images[False,40.]
    assert images[True,5.]!=images[True,40.]


def test_placement_height_defaults_are_labelled_assumptions_until_explicitly_set():
    from traffic_simulator.od_matrix import InstitutionPanel
    panel=InstitutionPanel()
    panel.kind_buttons["Hospital"].click()
    assert panel.height.value()==18. and panel.figures()["height_m"] is None
    assert "Assumed" in panel.height_note.text()
    panel.height.setValue(27.)
    assert panel.figures()["height_m"]==27.
    panel.kind_buttons["Shop"].click()
    assert panel.height.value()==4.5 and panel.figures()["height_m"] is None


def test_height_validation_save_and_old_project_defaults_leave_demand_unchanged():
    from traffic_simulator.institutions import place,run_demand
    network=generate_network(2,2)
    item=place(network,"Office",250,250,900,500,height_m=18.)
    before=deepcopy(network.as_records())
    for bad in (-1.,0.,301.,True,"18",float("inf"),float("nan")):
        candidate=deepcopy(item)
        candidate.height_m=bad
        assert institution_problem(candidate) and "height" in institution_problem(candidate)
        try:
            place(network,"Office",300,300,900,500,height_m=bad)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid height was accepted")
        assert network.as_records()==before
    modes=VehicleModes().modes
    demand=run_demand(network,modes,False)
    with TemporaryDirectory() as directory:
        path=Path(directory)/"height.json"
        save_network(network,path)
        loaded=load_network(path)
        assert loaded.institutions[item.id].height_m==18.
        item.height_m=42.
        assert run_demand(network,modes,False)==demand
        import json
        old=json.loads(path.read_text(encoding="utf-8"))
        old["institutions"][0].pop("height_m")
        path.write_text(json.dumps(old),encoding="utf-8")
        loaded=load_network(path)
        assert loaded.institutions[item.id].height_m is None
        assert building_assets.height(loaded.institutions[item.id])==18.


def test_height_controls_save_undo_and_keep_the_running_clock_camera_and_footprint():
    from traffic_simulator.main import MainWindow
    from traffic_simulator.institutions import place
    from traffic_simulator.network_scene import BuildingItem
    window=MainWindow()
    window._ask_to_save=False
    with TemporaryDirectory() as directory:
        window.results_folder=Path(directory)
        try:
            item=place(window.network,"Residential",250,250,400,500)
            window.scene.rebuild()
            window._commit()
            window.select("institution",item.id)
            window.view.set_isometric(True)
            transform=window.view.transform()
            window.run_action.trigger()
            runner=window.runner
            clock_time=window.clock.time_s
            assert runner is not None
            window.institution_panel.selected_height.setValue(21.)
            assert window.network.institutions[item.id].height_m==21.
            assert window.runner is runner and window.clock.running and window.clock.time_s==clock_time
            assert window.view.transform()==transform
            displayed=next(i for i in window.scene._building_items if isinstance(i,BuildingItem))
            assert displayed.footprint.width()==20. and displayed.height_m==21.
            window.undo()
            assert window.network.institutions[item.id].height_m is None and window.runner is runner
            window.redo()
            assert window.network.institutions[item.id].height_m==21. and window.runner is runner
            window.scene.set_buildings_visible(True)
            window.buildings_button.click()
            assert not any(i.isVisible() for i in window.scene._building_items)
        finally:
            window.close()


def verify_hardware_geometry(hardware):
    for vehicle,gaps in ((sample(),(QPointF(2.35,.828),)),
                         (sample(kind="bicycle",length=1.6,width=.65),(QPointF(.65,.24),QPointF(-.65,-.24)))):
        image,t=render_vehicle(vehicle,scale=150.,hardware=hardware)
        for p in gaps:
            assert image.pixelColor(t.map(p).toPoint())==QColor(VEHICLE_INKS[1])
    bicycle=sample(kind="bicycle",length=1.6,width=.65)
    image,t=render_vehicle(bicycle,iso=True,scale=200.,hardware=hardware)
    hole=QPointF(-.15,0)+projection_lift()*.46
    assert image.pixelColor(t.map(hole).toPoint())==QColor(VEHICLE_INKS[1])


def verify_hardware_depth(hardware):
    from traffic_simulator.network_scene import VehicleLayer
    # Two same-style, differently sized meshes exercise the instance order inside one draw call.
    names=[str(i) for i in range(100) if vehicle_assets.style(sample(str(i)))==1][:2]
    a=sample(names[0])._replace(colour=VEHICLE_PAINTS[0])
    b=sample(names[1],x=.7,y=.7,length=5.4,width=2.1)._replace(colour=VEHICLE_PAINTS[1])
    scene=NetworkGraphicsScene()
    scene.setSceneRect(-100,-100,300,300)
    scene.vehicle_layer=VehicleLayer(scene)
    _,_,fbo,device=hardware
    images=[]
    for vehicles in ((a,b),(b,a)):
        scene.frame=Frame(0,vehicles,{},2,0)
        fbo.bind()
        draw_samples(scene,device,True,scale=80.)
        images.append(fbo.toImage())
    differences=sum(images[0].pixelColor(x,y)!=images[1].pixelColor(x,y)
                    for x in range(80,560) for y in range(30,400))
    assert differences<30,differences  # only exact coplanar intersections may have an undefined tie
    assert scene.gpu.uploads==scene.gpu.draws==2


if __name__ == "__main__":
    if "--gpu" in sys.argv:
        from test_gpu import _gl_painter
        context, surface, fbo, device = _gl_painter(1200, 900)
        capture = sys.argv[sys.argv.index("--capture") + 1] if "--capture" in sys.argv else None
        compare_samples((context, surface, fbo, device), capture)
        compare_random_paint((context, surface, fbo, device))
        verify_hardware_geometry((context,surface,fbo,device))
        verify_hardware_depth((context,surface,fbo,device))
        check_bus_extrusion_pixels((context,surface,fbo,device))
        context.doneCurrent()
        print("GPU and raster vehicle markings, shared frame and one upload: ok")
        raise SystemExit(0)
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("ok")
