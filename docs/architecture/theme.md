# theme.py

- Fusion style with an explicit grey palette and white text, so a Windows dark or light
  setting can't make text unreadable.
- Colour tokens: `WINDOW`, `SURFACE`, `BUTTON`, `TEXT`, `SECONDARY_TEXT`, `ACCENT`, `BANNER`…
- `READABLE_PAIRS` lists every text/background pair, and a test enforces WCAG contrast ≥ 4.5:1.
- The menu bar (File, Edit and the simulation controls) has 5 px above and below and rounded
  hover highlights, so it doesn't sit tight against the window edge or the panels.
- Tabs: slim, rounded, the open one in the window grey with a blue underline; the sidebar's icon-only
  tab bar (`QTabBar#iconTabs`) keeps no room for a name. The busy bar's chunk is the accent.
- Buttons stand as tall as the input boxes (26 px); an icon button (`glyph` property, `icon_button`) is
  square, 26 × 26; a mini one (`role="mini"`) shows a small flat glyph but is clickable over the same 26 × 26.
- A collapsible section's title (`role="section"`): semibold, left-aligned, a rule under it, filled
  only on hover, a focus outline for the keyboard.
- **Clicks are seen:** every button turns `PRESSED` (#1e40af, a blue-800; a primary one `BANNER`) with a light blue
  edge while held, and for `FLASH_MS` (250 ms) after the click (`ClickFlash`, an application event filter that
  apply_theme installs once: the `flash` property, repolished), so even a quick click shows it was taken; a mini
  button keeps its size. A checked tool button (the menu bar's eye while it watches) is the accent colour.
