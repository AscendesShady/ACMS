# vehicle_assets.py: appearance choices (no Qt)

- `style(vehicle)` uses the published vehicle class, preserved through renames: three passenger-car variants
  selected by stable ID hash; bus/coach/tram, ambulance (emergency class or dispatch), bicycle,
  motorcycle/moped, and delivery/truck/trailer. Legacy frames with no class remain plain.
- Actual geometry is shared in `asset_mesh`: tapered bodies, raised sloped cabins, wheels and open bike
  frames, with class-specific opaque material faces. Both renderers keep physical length/width and pose
  interpolation independent of appearance. There are no external sprites, directional snapping or packages.
- `paint_colour(vehicle, palette)` uses fixed `colour` or a deterministic BLAKE2s hash of vehicle ID when
  `random_colour` is true. It never draws from simulation RNGs or uses Python's salted hash.
- `theme.VEHICLE_PAINTS` and `VEHICLE_INKS` supply body paints and material inks. The colour chooser is saved
  and undoable, while categorical charts retain the type colour. The existing type-edit stop-run policy applies.
- `tests/test_vehicle_assets.py` covers geometry, stable random paint, exact footprints, height assumptions,
  validation/persistence, unchanged demand and live height undo. `--gpu` additionally checks actual framebuffer
  silhouettes, open bike gaps, solid glazing, order-independent depth and one pose upload across camera toggles.
- `tools/preview_vehicle_assets.py` captures same-scale overviews, separately fitted geometry details,
  building footprint/height comparisons and the real Qt controls.
