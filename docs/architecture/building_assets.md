# building_assets.py: institution structures (no Qt)

- `height(item)` returns explicit `height_m` or `models.BUILDING_HEIGHTS_M[kind]`. Defaults are visual
  assumptions: Residential 9 m, School 7.2 m, Hospital/Office 18 m, Shop 4.5 m. They are labelled as assumed in
  the UI; measured heights can replace them. They do not infer storeys or trip production from floor area.
- `mesh(kind, area_m2, height_m)` preserves the square ground footprint of side `sqrt(area_m2)` and contains
  every vertex within that footprint and [0, height_m]. Total height includes roofs, penthouses and details.
  Residential structures have hipped roofs; other types have recessed roof/service volumes, with hospital
  signage or a shop awning. Facade bays and storeys use metre-based spacing with bounded detail counts.
- `network_scene.BuildingItem` paints the original flat footprint in 2D and these shaded opaque faces in
  orthographic 2.5D, with a small contact shadow. It exposes the footprint/structure for picking, and reserves
  extrusion bounds so camera toggles require no network rebuild. Labels lift with the roof. The existing
  building toggle hides the whole visual, including labels/catchment; records and demand remain.
- Height controls appear for both placement and selected institutions. An untouched placement default saves
  None, retaining its assumed status; editing saves a numeric height. MainWindow validates before mutating,
  rebuilds the scenery/cache, and commits an undo step without stopping a run. Undo/Redo preserve a live Runner.
- Vehicles and signals retain priority over scenery in the Qt scene; this is not a shared terrain/vehicle
  depth-buffer renderer. Hiding buildings remains available for analytical traffic inspection.
