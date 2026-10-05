# Layout conventions

- Section titles (`QGroupBox`) are semibold and white, with room above; their contents stay at
  normal weight.
- `properties_panel.align_form_labels(page)` gives every form in a tab the same label width, so
  the fields of all its sections start on one line. It is called for each tab in `MainWindow._build_ui`.
