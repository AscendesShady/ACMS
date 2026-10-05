# roundabouts.py: roundabout design (no Qt)

`design_roundabout(network, node_id)` returns a `RoundaboutDesign` whose `category`,
`inscribed_diameter_m`, `circulatory_width_m`, `island_diameter_m` and
`circulating_speed_kmh` follow NCHRP Report 672:
1. **Entry lanes** come from the approaches: one direction of a two-way road, all lanes of
   a one-way road entering.
2. **Category:**
   - Mini: single lane with approaches ≤ 30 km/h.
   - Otherwise single-lane, two-lane or three-lane by entry lanes.
3. **Design speed:** the fastest approach speed limit, held inside the category's
   recommended maximum entry speed range:

   | Category | Entry speed range | Typical ICD |
   |---|---|---|
   | Mini | 25–30 km/h | 14–27 m |
   | Single-lane | 30–40 km/h | 27–55 m |
   | Two-lane | 40–50 km/h | 46–67 m |
   | Three-lane | 40–50 km/h | 61–91 m |
4. **Inscribed circle diameter (ICD):** placed within the category's typical range in
   proportion to where the design speed sits in its range. Faster approaches get larger roundabouts.
5. **Circulatory roadway:** 120 % of the widest entry (NCHRP 672: 100–120 %). The central
   island is `ICD − 2 × width`, at least 2 m.
6. **Circulating speed:** the fastest-path relation `V = 3.4614 · R^0.3673` (mph, ft; −2 %
   cross slope), with R = island radius + 1.5 m.

The scene redraws roundabouts at this true diameter. A mini's island is painted grey
(drive-over); others are landscaped green. Entries get a dashed give-way line at the circle.
