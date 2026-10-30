# metro_assets.py: overhead MRT scenery (no Qt)

- Render dimensions are assumptions in metres, not surveyed geometry or engineering clearance checks:
  viaduct underside 8 m, track level 9 m, platform 9.4 m, canopy top 14 m; a train starts at 9.15 m and is
  3.4 m tall. Piers meet the ground and repeat at up to 40 m spacing. Twin tracks use a 1.435 m visual gauge.
- `line_mesh(stops, cars, path)` consumes `Network.metro_track` exactly, including its curves and shared
  interchange stations. It never substitutes stop-to-stop chords or alters the model's curve construction.
  End extensions support terminal platforms only in the display. Side platforms and canopies follow the track.
  Cross-sections are shared by adjacent deck/rail faces; internal caps are omitted. Every saved path vertex is
  retained while faces longer than 3 m are subdivided for sorting. Piers are spaced by world arc distance,
  independent of curve tessellation. An old project without saved track falls back through `metro_track`.
  Platform length is at least `models.METRO_PLATFORM_M` (currently 120 m), matching the current straight station
  section. Longer trains get their own required length: 18 m cars, 2.8 m wide, 0.6 m gaps and 8 m extra length.
  Canopy shells have pitched roofs, cross-beams and muted yellow tactile edges. Their ridges remain at 14 m.
- One train is parked at the first station as a labelled **train preview**. No published train poses exist;
  the preview neither moves nor claims to depict occupancy, headway, dispatches or simulation state.
  Trains have sloped noses with matching windscreen planes, cab glass only at the two terminal carriages, and
  a thin hull livery band. Side glass replaces opaque facade panels; it is not a decal over an opaque wall.
- Faces are in absolute world metres, use `theme.METRO_INKS`, and are shared by Qt raster/OpenGL painting.
  Long surfaces are divided into short sections for painter sorting, including the carriage facades behind
  coplanar windows. `placed` rotates world geometry continuously with its track tangent; each preview carriage
  uses its own pose along the track. Interchanges keep each line's track and height, without inventing grade separation.
- Heights affect drawing only. Network coordinates, mode choice, line length, service, capacity, passenger
  loads and road collision dimensions are unchanged. Supports are illustrative and do not check road conflicts.
