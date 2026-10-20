# properties_panel.py: selection editor (Qt)

`PropertiesPanel.show_object(kind, obj)` shows the matching section:
- **Road:**
  - Fields: length, lanes/direction, lane changing, speed, direction (two-way / one-way /
    one-way reversed), "Tightest curve" (from stored curvature) and road type.
  - Turn pockets: a "Pocket to <junction>" row for each junction end its traffic arrives at (none at a
    boundary end): a list (None, Kerb or Offside, 1 or 2 lanes; tooltip "Turn pocket") and its length,
    15–150 m ("Pocket length"). Applied with the other fields (`_pocket_fields`); a pocket at an end the
    traffic no longer arrives at (made one-way) goes, and `Network.validate` refuses one that doesn't fit.
  - Cycle lane: one tick a direction, "To <end>" for each end its traffic arrives at (boundary ends too; tooltip
    "Bikes-only lane this way"), applied with the other fields (`_cycle_fields`); one for traffic that no longer
    arrives (made one-way) goes.
  - Footpath: one list (`footpath_choice`, `FOOTPATH_CHOICES`): None, Both sides, or one side named by the compass
    side it faces ("North side only", taken from the road's ends; tooltip "Footpaths beside this road"). Choosing
    highlights where it would go on the map (`footpath_preview` → the scene's `preview_footpaths`) before Apply;
    applied with the other fields; reversing the road keeps the paving where it is.
  - Apply is enabled only while a field differs from the road; Enter also applies.
  - Changes are validated as a whole and reverted with a message if invalid: the whole network is put back
    (the same object, as `call_tool` does), with no undo step and the saved state as it was.
- **Node:**
  - Control: signalised / roundabout / uncontrolled. Slip roads past the junction then move out where
    it needs more room (`fit_slip_roads`: a roundabout); if they can't, the control stays as it was, with
    a message.
  - Signalised shows phases (2/4), the timing method dropdown (Webster, TRANSYT, Synchro, SCATS,
    SCOOT, Fixed time) and a green time per phase, editable for Fixed
    time only. Only the settings the method uses show: Cycle limits (the coordinated four), Target
    saturation (SCATS, SCOOT), Stop weight (TRANSYT, SCOOT); a minimum past the maximum carries it.
    The note under them says what the run did: Webster's cycle and Y, TRANSYT's and Synchro's cycle,
    greens, offset, group and delay-and-stops before and after optimising, and for SCATS and SCOOT
    what they are doing now (`show_plans` each display frame, from the frame's plans).
  - Roundabout shows its design (category, design speed, ICD, island, circulating speed).
- **End:** supply (entry marker) or demand (exit marker) in veh/h.
- **Road → Add Bus Stop:** a bus icon beside Drag to Adjust. Its menu names the traffic it serves by
  node ID: `1,1 → 2,1`, `2,1 → 1,1` or Both directions (a one-way road: its one direction).
- **Bus stop** (shown when it is clicked on the map): Serves (the same three choices; on a one-way
  road only its direction), **From** each end node (m, to the bay's middle; typing one moves the
  other, arrow steps 0.5 m; applied on Enter or a step), **Dwell time** (s) and a Delete Bus Stop
  icon (Del). Drag to Adjust is hidden: the stop drags itself.
  Each add is an undo step, and the edits and nudges after it merge into one
  (`MainWindow._bus_stop_changed`).
- **Several junctions (Ctrl+click):** 3 junctions offer a trumpet; 4 offer a diamond or SPUI.
- **A selection box** (`show_group`): what it took, counted by kind (`describe_group`: "2 junctions,
  4 roads"), their IDs and a Delete Selected icon (Del).

Every node or road can also use **Drag to Adjust** and **Delete**.

The panel builds on the shared widgets (widgets.md); it still re-exports them for drop-in extensions that import
them from here.
