# roundabouts.py: roundabout design (no Qt)

`design_roundabout(network, node_id)` returns a `RoundaboutDesign` whose `category`,
`inscribed_diameter_m`, `circulatory_width_m`, `island_diameter_m` and
`circulating_speed_kmh` follow NCHRP Report 672:
1. **Entry lanes** come from the approaches: one direction of a two-way road, all lanes of
   a one-way road entering.
2. **Category:**
   - Mini: single lane with approaches ≤ 30 km/h.
   - Otherwise single-lane, two-lane or three-lane by entry lanes. More than three (`MAX_ENTRY_LANES`) is
     beyond the guide and refused with a ValueError naming the roundabout: `Network.validate` checks every
     roundabout, so a road edit, a control change (Properties panel, `set_junction_control`) or a load that would
     make one is refused, changing nothing; `build_edge` refuses a crossing roundabout before any road changes.
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
   island is `ICD − 2 × width` (at least 5.6 m, a mini's), so ICD = island + 2 × width always.
6. **Circulating speed:** the fastest-path relation `V = 3.4614 · R^0.3673` (mph, ft; −2 %
   cross slope), with R = island radius + 1.5 m.

`hcm_entry_capacity(circulating_vph, lanes=1)` is the HCM 6th edition's (Ch 22) entry capacity against the
circulating flow, with as many circulating lanes as entry lanes: one lane 1380·e^(−0.00102·v_c) (Eq 22-1), two
1420·e^(−0.00085·v_c) + 1350·e^(−0.00092·v_c) (Eq 22-4); None for three (beyond the HCM). `analysis.junction_los`
reports it beside the capacity measured on the simulated drivers (`simulation.ROUNDABOUT_ENTRY_VPH`), whose ring
is one lane however many enter. `tanner_entry_capacity(circulating_vph, headway_s=2.0, …)` is Tanner's (1962)
against a bunched stream (no gap within Δ: 2.0 s for one circulating lane, Akçelik), with the critical and
follow-up headways the HCM's Eq 22-1 implies (4.98 s, 2.61 s): near the HCM's curve to about 800 veh/h, below it
beyond, as the simulated ring is.

The scene redraws roundabouts at this true diameter. A mini's island is painted grey
(drive-over); others are landscaped green. Entries get a dashed give-way line at the circle.
