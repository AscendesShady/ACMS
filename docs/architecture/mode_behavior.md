# mode_behavior.py: vehicle types

- `VehicleModes` (one per window, `MainWindow.vehicle_modes`) holds the vehicle types in order,
  each a `VehicleMode(name, colour, headway_min, timetabled, …, occupancy, pcu)`. It starts as Bus, Car, Bike, with default split 10/85/5
  and PCU 2, 1 and 0.2 (UK DMRB TA 79/99). Only a `timetabled` type may have a headway: Bus is, Car and Bike
  never are (`set_headway` and `update` refuse one). `update` can set `timetabled` (false clears the headway); the
  assistant's `create_vehicle_types` makes a type timetabled when it gives a headway. A project saved before the flag
  loads a type with a headway, or named like a bus, as timetabled.
  - `add` (up to 8), `rename` (unique, not empty), `delete` (at least one stays) and `set_headway`
    (0–120 min; 0 = no timetable), `set_pcu` (0.1–10) and `set_occupancy` (0.01–500 people) emit `changed`. `vehicles_per_hour()` lists the timetabled types.
  - The OD matrix follows every change: a new type gets 0 % in every pair, and a deleted type's
    share goes to the others in proportion (`redistribute`, whole percent adding to 100). The
    Demand Generator rebuilds its slider and % boxes.
  - Each change is one undo step: the snapshot holds the types too.
- Colours come from the dataviz palette's 8 categorical slots, in fixed order. A type keeps its
  colour; a new type takes the first free one.
- `ModeBehaviorPanel` (the Mode Behavior tab) shows each type's colour swatch, name box and Delete button only,
  plus **Add Vehicle Type**, which first asks whether the new type keeps a headway (Yes: timetabled; No: never;
  Cancel adds nothing) and then shows its values. Clicking a type's swatch or name shows its own values
  under the list (`_pick`): its **Headway** (timetabled types only; minutes, "0 min" for none: a `NumberBox`,
  which selects its number when clicked so typing replaces it), **PCU** (passenger car units) and **Occupancy**
  (people a vehicle). Their arrow clicks merge into one undo step each. PCU is kept with the project but no calculation uses it yet. Each type's other behaviour settings are still a placeholder.
