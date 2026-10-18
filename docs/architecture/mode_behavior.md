# mode_behavior.py: vehicle types

- `VehicleModes` (one per window, `MainWindow.vehicle_modes`) holds the vehicle types in order,
  each a `VehicleMode(name, colour, headway_min, timetabled, …, occupancy, pcu)`. It starts as Bus, Car, Bike, with default split 10/85/5
  and PCU 2, 1 and 0.2 (UK DMRB TA 79/99). Only a `timetabled` type may have a headway: Bus is, Car and Bike
  never are (`set_headway` and `update` refuse one). `update`'s figures keep to `FIGURES`' ranges; τ (`tau_s`) is
  at least the simulation step `DT` (0.5 s), since a driver reacting faster than a step collides (`WHY` says so
  in the refusal); `emergency_decel` (SUMO's emergencyDecel, 0.5-15 m/s²) is no less than `decel` (`update` refuses
  either change that would break it, and `network_io.project_problem` a saved type that does). A type saved before
  it (`emergency_decel` None) gets its class's (`vehicle.VEHICLE_CLASSES`), or its `decel` if that is harder. `update` can set `timetabled` (false clears the headway); the
  assistant's `create_vehicle_types` makes a type timetabled when it gives a headway. A project saved before the flag
  loads a type with a headway, or named like a bus, as timetabled.
  - `vehicle_class`: what the type is, whatever it is called (a `vehicle.VEHICLE_CLASSES` kind): Bus `bus`, Car
    `passenger`, Bike `bicycle`; `create_vehicle_types` sets the kind it makes, `update` changes it only when
    asked (its figures stay), a rename keeps it. A `bus` type calls at bus stops and gets transit priority
    (simulation.py and the TSP extension read it, not the name). A type without one (`__post_init__`: a project
    saved before it, or one added in the tab) is a bus if it is called Bus, else `passenger`. Saved with the
    project; `network_io.project_problem` refuses an unknown one.
  - `filters`: in a lateral run (simulation.md) the type's vehicles, if narrow enough, share lanes side by side and
    filter past queues within their lane. True for a type made as a motorcycle, moped or bicycle (`create`, and the
    default Bike), else false; `update` sets it (true or false only); saved with the project. A type without one (a
    project saved before it: None) takes it from its class (`__post_init__`, `vehicle.FILTERING_CLASSES`);
    `network_io.project_problem` refuses one that isn't true or false. Not shown in the tab.
  - `add` (up to 8), `rename` (unique, not empty), `delete` (at least one stays) and `set_headway`
    (0–120 min; 0 = no timetable), `set_pcu` (0.1–10) and `set_occupancy` (0.01–500 people) emit `changed`. `vehicles_per_hour()` lists the timetabled types.
  - The OD matrix follows every change: a new type gets 0 % in every pair, and a deleted type's
    share goes to the others in proportion (`redistribute`, whole percent adding to 100). The
    Demand Generator rebuilds its slider and % boxes.
  - Each change is one undo step: the snapshot holds the types too.
- Colours come from the dataviz palette's 8 categorical slots, in fixed order. A type keeps its
  colour; a new type takes the first free one.
- The selected type's **Colour** chooser offers twelve paint colours and **Random**. `set_colour(index, colour)`
  validates a hex colour before editing; None sets `random_colour=True` and retains the categorical `colour`.
  Fixed paint clears the flag and sets that type colour. Random paint is display-only, chosen by stable vehicle
  ID in `vehicle_assets`, with no simulation RNG draws or frame-to-frame flicker. Charts retain the type colour.
  The flag defaults false for old projects, is validated on load/save, and follows the normal undo snapshot.
  It also follows the existing type-edit policy: editing a type stops an active run; the next Run uses the choice.
- `ModeBehaviorPanel` (the Mode Behavior tab) shows each type's colour swatch, name box and Delete button only
  (rebuilt on add, delete and undo; the outgoing rows are silenced before any is hidden, so a half-typed name isn't
  applied to the types an undo restored, and the focus passing through them picks nothing),
  plus **Add Vehicle Type**, which first asks whether the new type keeps a headway (Yes: timetabled; No: never;
  Cancel adds nothing) and then shows its values. Clicking a type's swatch or name shows its own values
  under the list (`_pick`): its **Headway** (timetabled types only; minutes, "0 min" for none: a `NumberBox`,
  which selects its number when clicked so typing replaces it), **PCU** (passenger car units) and **Occupancy**
  (people a vehicle). Their arrow clicks merge into one undo step each. PCU is kept with the project but no calculation uses it yet. Each type's other behaviour settings are still a placeholder.
