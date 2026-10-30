# Layout conventions

- One left column: the brand, the hint under it, the tab row's first tile, inner tab rows (Roads | MRT,
  System 1 | System 2), section titles and each page's text all start at the sidebar's content edge; inner
  tab pages have no side margins (AI Mode's System pages: 0, 8, 0, 0, as the others). Tab rows put their first
  tile flush left and their last flush right.
- Section titles (`QGroupBox`) are semibold and white, with room above, on that column (no title indent);
  their contents stay at normal weight.
- The menu bar is padded 8 px above and below: the run controls' glass bar sits as far under the window's
  top as above the panels (the window's layout keeps no top margin), and File and Edit are padded to the
  bar's height so their text centres on it.
- `widgets.align_form_labels(page)` gives every form in a tab the same label width, so
  the fields of all its sections start on one line. It is called for each tab in `MainWindow._build_ui`.
