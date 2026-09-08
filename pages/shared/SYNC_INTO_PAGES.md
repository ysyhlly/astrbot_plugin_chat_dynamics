# shared → page-local publish

AstrBot plugin pages cannot load `../shared/*` (path escape blocked → 404).
Keep canonical files here, then copy into each page before packaging:

Run `python scripts/sync_page_assets.py` from the repository root to publish all shared assets, including `theme.js` and `theme.css`, to the six pages. `theme.css` must be the last stylesheet; `theme.js` runs synchronously in the head before styles for the initial local preference.

```bash
for p in config today manners memory replay; do
  cp base.css ../$p/base.css
  cp shell.css api.js shell.js ../$p/   # config included to avoid stray 404s
done

# Unified side nav (all 6 pages including console):
for p in console config today manners memory replay; do
  cp plugin_nav.js plugin_nav.css ../$p/
done
# Wire pages also need updated shell.js (renderNav → mountPluginSideNav):
for p in today manners memory replay; do
  cp shell.js ../$p/
done
```

HTML should use `./base.css` (wire pages), `./shell.css`, `./plugin_nav.css`, `./style.css`.
Daytime paper UI is the default (`data-theme="day"`); canonical day/night tokens live in `theme.css`. The shared `theme.js` bootstrap reads `chat_dynamics_ui` when available, then restores the authenticated account preference via `ui_preferences`. Only explicit toggles save the account preference; initialization does not overwrite it. Signed navigation carries `ui` even when iframe storage/history is unavailable.
JS imports `./api.js` / `./shell.js` / `./plugin_nav.js` (never `../shared`).
Config live skin: `./style.css` + `./plugin_nav.css` only (no base.css).
