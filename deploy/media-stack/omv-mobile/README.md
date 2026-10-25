# OMV phone table layout

This local extension keeps native OMV tables, row selection, sorting, and
actions. On screens up to 767 CSS pixels (or touch screens up to 1024 pixels),
tables retain readable column widths inside a horizontal scroll area. Headers
and rows scroll together. Native pagination stays at the visible left edge;
toolbar buttons have larger touch targets. Outside these breakpoints no CSS
overrides apply and the script removes its wrappers when resizing to desktop.

Install or restore after copying this folder to `/opt/media-stack/omv-mobile`:

```sh
python3 /opt/media-stack/omv-mobile/install.py
```

The installer copies public CSS/JS to `/usr/local/share/omv-mobile` and writes
`/etc/nginx/openmediavault-webgui.d/90-home-media-mobile.conf`, validates nginx,
and reloads it. OMV's existing server include directory loads this extension;
its packaged HTML, JavaScript, CSS and generated nginx site are untouched.
HTML response filtering adds local CSS/JS links. No remote assets or services
are involved, and the configuration persists across reboot and normal OMV
configuration deployments. Browser refresh is required after installation.

To undo:

```sh
python3 /opt/media-stack/omv-mobile/install.py --remove
```

The check script uses Playwright Chromium and read-only `omv-rpc` responses
to render the real OMV Angular interface without credentials. It does not
use an owner's browser session, write server preferences, or modify storage.
Browser tooling is a development dependency and is not needed to use the UI.
Actual iPhone Safari feedback is still needed; Chromium emulation is not a
physical iPhone test. Recheck the layout following a major OMV frontend update.
