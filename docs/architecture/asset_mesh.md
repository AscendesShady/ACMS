# asset_mesh.py: shared low-poly geometry (no Qt)

- `Face(vertices, ink, part)` describes opaque convex polygons. A vertex is `(x, y, z, offset)`: x times half
  length, y times half width, z times width, plus offset times width along x. `expand` produces actual metres.
  That offset keeps tyre radius equal vertically and longitudinally at ordinary class proportions, instead of
  stretching circles with vehicle length. `wheel_fit` contracts the longitudinal radius only for impossible
  edited aspect ratios that would otherwise draw beyond the physical footprint.
- `vehicle_mesh(skin)` caches original models: chamfered car bodies, separate raised cabins and sloped glazed
  faces, wheels with hubs; bus side windows and roof equipment; ambulance body/cab, cross and lightbar;
  bicycle/motorbike wheel rings, spokes, open frame bars, handlebars and riders. The three car variants have
  distinct roof profiles. Style zero is the old plain-frame box. Inks and body paint come from theme tokens.
- `vehicle_low_mesh` omits subpixel detail below five projected pixels of group/body width. It retains a shaped
  silhouette and actual isometric dimensions. The GPU keeps a static high/low mesh atlas and reuses pose data
  when zoom or camera changes. Top-down retains its minimum-size markers.
- `solid`, `box`, `wheel`, `tube`, `side_panel` and `normal` are geometry helpers also used by institution
  structures. The raster path sorts visible faces within each model, hides far-side vehicle tyres, and paints
  coplanar glazing/markings after their support surfaces. The GPU uses a depth buffer for vehicle self-occlusion
  and occlusion between instances. Global building/traffic layering remains managed by the Qt scene.
- These are render meshes, never collision hulls, route geometry or a source of driving behaviour.
- Bus tyres stay within physical half-width and protrude beyond a recessed lower skirt. The passenger body
  starts above the tyre crown; skirt sections leave open spaces at both axles. `wheel_fit` includes those
  openings when fitting unusually short custom buses, identically in raster expansion and the GPU shader.
- `pedestrian_mesh` is a display-only 1.75 m person, with separate legs, arms, torso and head in metre coordinates.
  Clothing reuses WALKER/vehicle ink tokens. `footbridge_mesh` supplies a 6 m high deck with supports, parapets
  and illustrative stairs. These heights are assumptions; neither changes the walking model or its climb cost.
