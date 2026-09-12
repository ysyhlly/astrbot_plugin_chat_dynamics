# shared → page-local publish

AstrBot plugin pages cannot load `../shared/*` (path escape blocked → 404).
Keep canonical files here, then copy into each page before packaging:

Run `python scripts/sync_page_assets.py` from the repository root to publish all shared assets: `base.css`, `theme.js`, `theme.css`, `plugin_nav.js`, `plugin_nav.css` go to all six pages (console included); `shell.css`, `shell.js`, `api.js` go to every page except console, which keeps its own workspace chrome. `theme.css` must be the last stylesheet; `theme.js` runs synchronously in the head before styles for the initial local preference. `python scripts/sync_page_assets.py --check` exits non-zero on drift without modifying stale copies, and release validation treats drift as a failure. Page-private files (e.g. `console/style.css`, `console/workspace.js`) are not managed by the script.

The manual equivalent:

```bash
for p in console config today manners memory replay; do
  cp base.css plugin_nav.js plugin_nav.css theme.js theme.css ../$p/
done
for p in config today manners memory replay; do
  cp shell.css api.js shell.js ../$p/   # config included to avoid stray 404s
done
```

HTML loads `./base.css`, `./shell.css` (except console), `./plugin_nav.css`, `./theme.css`, `./style.css`.
Daytime workspace UI is the default (`data-ui-theme="day"`); canonical day/night tokens live in `theme.css`. The shared `theme.js` bootstrap reads `chat_dynamics_ui` when available, then restores the authenticated account preference via `ui_preferences`. Only explicit toggles save the account preference; initialization does not overwrite it. Signed navigation carries `ui` even when iframe storage/history is unavailable.
JS imports `./api.js` / `./shell.js` / `./plugin_nav.js` (never `../shared`).
Config live skin: `./style.css` + `./plugin_nav.css` + `./theme.css` (no base.css, no shell).

The six-page workspace uses the shared `theme.css` for typography, spacing, card surfaces, controls, day/night colors and responsive rules. Keep new visual rules here instead of adding a separate page palette; `base.css` stays a color-free structural primitive layer. The console splits sessions / policy / integrations into `role="tab"` workspace views (page-private `workspace.js`; panels stay mounted so polling never loses input). Shared navigation uses the same decorative SVG icon set and retains the existing signed navigation and theme preference behavior. At phone widths, diagnostic cards form one column and overview metrics form two columns, collapsing to a single column at ≤360px; long provider identifiers wrap rather than overflowing. Page scripts, DOM IDs, forms and API contracts remain the source of existing behavior.
