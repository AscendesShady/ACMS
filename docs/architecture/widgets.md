# widgets.py: the shared widgets (Qt)

The controls every tab, panel and extension uses. Qt; colours from theme.py; no model objects, so importing it
loads neither the model nor the selection editor.

- `styled_button(text, role, tooltip)`: a button tagged for the app stylesheet (role "primary", one per area,
  "danger", "section" or "mini"); keyboard focus only, no focus ring after clicks.
- `icon_button(glyph, name, role)`: one glyph, its name as the tooltip and accessible name; square, property
  `glyph`. Without an icon font it shows its name. `set_glyph` swaps its glyph and name (Start becoming Restart);
  `MINI_ICON_PX` is a mini button's glyph size. `glyph_icon` draws a glyph crisp at 1x, 1.5x and 2x.
- `ICON_FONTS`: Segoe Fluent Icons (Windows 11), else Segoe MDL2 Assets (Windows 10), same code points.
- `hint_label(text)`: secondary explanatory text, word-wrapped, role "hint".
- `align_form_labels(container)`: every form inside gets the same label width, so the fields of all its sections
  start on one line (layout_conventions.md).
- `Collapsible(title, body)`: a section under a title that opens and closes it, a chevron showing which.
- `OpenPageTabs`: tabs as tall as the open page. QTabWidget sizes itself for its tallest page, hidden ones too, in
  its size hints and in its height for a width when pages wrap text; this swaps that page for the open one in all
  three. The sidebar and AI Mode's System 1 / System 2 use it, so the sidebar scrolls only when the open page
  needs it.
- `ChoiceButtons(options)`: radio buttons for a short list (fewer than 5 read better than a dropdown); `changed`
  fires on the user's pick, not on `set_value`.
- `CheckList`: a dropdown of ticks that stays open while ticking and shows what is ticked; a plain list popup
  (10 rows, then it scrolls); a click toggles its row once, on release. `set_ticked` refuses labels it doesn't list.
