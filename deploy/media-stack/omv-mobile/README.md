# OMV phone table layout

This local extension keeps native OMV tables, row selection, sorting, and
actions. On screens up to 767 CSS pixels (or touch screens up to 1024 pixels),
each normal table row becomes a full-width card with a subtle border, spacing,
a label and complete
value for each column. Long paths wrap instead of being truncated. A **Show
table** button restores the native column layout for sorting or comparison;
**Show cards** returns to the full details. Native controls, row selection and
pagination remain in place, and toolbar buttons have larger touch targets.
Virtualized tables keep the scroll layout because their row positions depend
on fixed heights. In table view, headers and rows scroll together and native
pagination stays at the visible left edge. Outside these breakpoints the phone table overrides do not apply, and the script removes its table wrappers when resizing to desktop. Dashboard-specific presentation is described below.

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
The owner confirmed the phone layout and card borders work. Automated checks
use Chromium emulation, so they do not establish every Safari/browser version.
Recheck the layout following a major OMV frontend update.

When Media verification is enabled, it spans the first dashboard row at full width. Other enabled widgets follow underneath in a responsive 1/2/3/4-column grid at the native OMV breakpoints. Cards retain native compact height, padding and text size, with a 220px desktop target width and 140px phone target width. Hover popups use larger 14px text, extra padding and up to 480px width, bounded to the viewport; unrelated tooltips are unchanged. Full titles, values and details remain available on hover. Metrics are presented in four sections: Library & progress, Workers & current activity, Discovery & metadata, and Results & diagnostics. Values and RPCs are unchanged; native cards stay owned by OMV and the presentation adds no source requests. The existing tooltip retains full detail. No enabled-widget preferences, worker settings or phone table behavior are modified; dashboards without Media verification retain their native layout.
