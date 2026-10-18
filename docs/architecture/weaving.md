# weaving.py: freeway weaving segments (no Qt)

The HCM 2000 Chapter 24 method (SRC_011, p24-1 to 24-27), on the network as drawn and the demand it carries. The
assistant reaches it through `design_audit` (where the weaves are, network_checks.md) and `weaving_los` (how they
operate, `MainWindow._weaving_los`).

- **Finding them** (`segments(graph)`): a link up to `MAX_WEAVE_M` (750 m; longer, the merge and diverge are
  analysed apart, p24-9) from an uncontrolled junction where two roads join it to one where it splits into two.
  U-turns and offside turns don't count (a diamond's at-grade cross movements); the joining road must land on the
  kerbside lane and the leaving road leave from it (ramps on the side traffic keeps to, as interchanges.py lays
  them). The main road is the faster, then the wider (`_main_first`).
- **Lane changes, the HCM's way** (`_lanes_between`): a ramp's lanes are the segment's kerbside ones, the main road's
  its offside ones; a weaving movement needs the lanes between its entry's and its exit's (main road to the exit:
  (N − n_main_in) − (n_exit − 1); entry to the main road: (N − n_main_out) − (n_entry − 1)). The simulation's lanes
  follow the same layout at ramps (`Graph._ramp_lanes`, simulation.md), so its drivers make those changes.
- **Type** (`configuration`, Exhibit 24-5): 1 and 1 → A; 0 and 0 or 1 → B; 0 and 2 or more → C; 1 and 2 or more, or
  2 and 2 → None (not feasible). A segment with no lane beyond both the main road's in and out (no auxiliary lane)
  is `"ramp junctions"`: a merge and a diverge close together, not a weave.
- **Operation** (`operate`): W = a(1 + VR)^b (v/N)^c / (3.28 L)^d (Eq 24-4, Exhibit 24-6's constants), weaving and
  nonweaving speeds S = 24 + (S_FF − 16)/(1 + W) (Eq 24-3), the lanes weaving needs N_w (Exhibit 24-7) against
  N_w(max) 1.4, 3.5, 3.0: constrained beyond it (speeds again with the constrained constants); S = v/(v_w/S_w +
  v_nw/S_nw) (Eq 24-5), D = (v/N)/S (Eq 24-6), LOS by Exhibit 24-2 (freeway: 6, 12, 17, 22, 27 pc/km/ln; multilane
  under a free-flow speed of 90 km/h: 8, 15, 20, 23, 25). S_FF is the mean of the main road's speed limits in and out.
- **Capacity** (`capacity`): the flow, in these movements' proportions, at the LOS E/F density, by bisection (the HCM
  finds it by trial; its Exhibit 24-8 tabulates it), capped by a basic segment's capacity (`basic_capacity`: freeway
  2,400 pc/h/ln at 120 km/h to 2,250 at 90; multilane 2,200 to 1,900 at 100 to 70) and the field limit on weaving
  flow (2,800, 4,000, 3,500 pc/h for A, B, C), with `capacity_bound` naming which. `limits` gives the volume ratios
  each type takes (A 1.00, 0.45, 0.35, 0.20 for 2 to 5 lanes; B 0.80; C 0.50); `report` warns past them, past the
  weaving flow limit, at a Type C weaving ratio over 0.40 and under 70 km/h (outside the calibration).
- **Flows** (`movement_flows`, `report`): each pair's pc/h on the routes a Run takes (`Graph.route_shares`, route choice
  included): main road through (v_o1), ramp to ramp (v_o2), main road to the exit (v_w1), entry to the main road
  (v_w2). `MainWindow._weaving_los` gives each pair's busiest-hour veh/h in PCU by its split (`VehicleMode.pcu`) at
  its peak quarter (4 × its largest quarter share: V / PHF, Eq 24-1).
- **A run's own figures** (`measured`): the link's traversals over the observed time, their space-mean speed and the
  density a lane, beside the HCM's (veh, not pc).
- Checked against the HCM's Example Problems 1 (Type B: W 0.648 and 0.454, 81.0 and 88.6 km/h, N_w 1.64, 85.7 km/h,
  16.3 pc/km/ln, LOS C; capacity 8,393 against the table's 8,421) and 2 (Type A: 79.4 against 79.3 from its rounded
  W, 97.8, N_w 1.02, 13.3, LOS C; 8,436 against 8,474).
