# 5. Main flows

1. **Start:** `run.py` → `main()`.
   - It generates the default grid (`main.py`'s `DEFAULT_*`: 3 × 4 corridors, left-hand, 50 km/h, 2 lanes), builds the
     window and applies the theme.
   - The first undo snapshot is taken.
   - The AI panel scans for keys and Ollama models (chat for System 2, decision for System 1).
2. **Edit a road:** click a road → `scene.clicked` → `MainWindow.canvas_clicked` → `select` →
   `PropertiesPanel.show_object`.
   - Apply → `network.set_road_length` / field updates / `reverse_road` → `validate()`.
   - Then `network_changed` → `model_updated` → scene rebuild → `_commit` (an undo step).
3. **Add Edge:** pick the start junction, then the target junction or empty space for an end.
   - `crossings()` runs on the straight line; if the new road crosses others, `ask_crossing` asks.
   - Roundabout: `add_node` + `split_road` at each crossing, then chain the roads.
   - Overpass: add the road with `overpass=True`.
4. **Drag:** select → Drag to Adjust → mouse moves call `move_nodes` (curves follow via
   `_carry_shapes`) → release commits one undo step.
5. **Interchange:** Ctrl+click 3 or 4 junctions → Prepare… → dialog → `build_trumpet` /
   `build_diamond`. Everything is checked before anything is removed.
6. **Delete by box:** drag over empty map → `scene.boxed` → `MainWindow.box_selected` →
   `network.objects_in_box` (Ctrl/Shift adds to the selection) → `show_group`. Del or Delete
   Selected → `delete_group` asks, naming the roads that go with the selected junctions →
   `delete_objects` (an MRT line by `delete_metro_line`) → one undo step.
7. **Save/Load:** JSON through `network_io`, validated both ways; the AI Mode conversation is
   saved in the same file and restored on load.
8. **Ask the assistant:** AI Mode → System 2 → pick Chat with and the model → **Start** →
   warm-up → prompt gate opens → Ask.
   - The reply streams in.
   - Tool calls run on the UI thread: reads at once, edits after **Apply**.
   - Each applied edit → `_ai_edited` → redraw + one undo step.
   - Each message starts with the editor update (your changes since the last reply, selection).
