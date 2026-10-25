/* OMV phone tables: retain native controls and Angular data/selection behavior. */
(() => {
  'use strict';
  const phone = matchMedia('(max-width: 767px), (max-width: 1024px) and (hover: none) and (pointer: coarse)');
  const tables = new Map();
  let frame = 0;
  let observer;
  const schedule = () => {
    if (phone.matches && !frame) frame = requestAnimationFrame(refresh);
  };
  function refresh() {
    frame = 0;
    if (!phone.matches) return;
    let changed = false;
    for (const [table, state] of tables) {
      if (!table.isConnected) {
        state.hint.remove(); state.wrapper.remove(); tables.delete(table);
      }
    }
    document.querySelectorAll('omv-datatable ngx-datatable').forEach(table => {
      let state = tables.get(table);
      if (!state) {
        const wrapper = document.createElement('div');
        wrapper.className = 'omv-phone-table-scroll';
        wrapper.tabIndex = 0;
        wrapper.setAttribute('role', 'region');
        wrapper.setAttribute('aria-label', 'Scrollable table');
        const hint = document.createElement('p');
        hint.className = 'omv-phone-table-hint';
        hint.textContent = 'Swipe the table sideways to see all columns.';
        table.before(hint, wrapper);
        wrapper.append(table);
        state = {wrapper, hint, width: 0, viewport: 0};
        tables.set(table, state);
      }
      const viewport = state.wrapper.clientWidth;
      if (!viewport) return;
      const columns = table.querySelectorAll('.datatable-header-cell').length || 4;
      const width = Math.max(viewport, 560, columns * 160);
      if (width !== state.width || viewport !== state.viewport) {
        state.wrapper.style.setProperty('--omv-phone-table-width', width + 'px');
        state.wrapper.style.setProperty('--omv-phone-viewport', viewport + 'px');
        state.width = width; state.viewport = viewport;
        changed = true;
      }
      state.hint.hidden = width <= viewport;
    });
    // ngx-datatable recalculates its native flex columns against the wider host.
    if (changed) dispatchEvent(new Event('resize'));
  }
  function mode() {
    if (phone.matches) {
      if (!observer) observer = new MutationObserver(schedule);
      observer.observe(document.body, {childList: true, subtree: true});
      schedule();
    } else {
      if (observer) observer.disconnect();
      let changed = false;
      for (const [table, {wrapper, hint}] of tables) {
        if (table.isConnected) wrapper.before(table);
        wrapper.remove(); hint.remove(); changed = true;
      }
      tables.clear();
      if (changed) dispatchEvent(new Event('resize'));
    }
  }
  phone.addEventListener('change', mode);
  addEventListener('resize', schedule);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mode, {once: true});
  else mode();
})();
