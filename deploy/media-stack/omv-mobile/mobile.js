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
        state.hint.remove(); state.button.remove(); state.wrapper.remove(); tables.delete(table);
      }
    }
    document.querySelectorAll('omv-datatable ngx-datatable').forEach(table => {
      let state = tables.get(table);
      if (!state) {
        const wrapper = document.createElement('div');
        wrapper.className = 'omv-phone-table-scroll';
        wrapper.tabIndex = 0;
        wrapper.setAttribute('role', 'region');
        wrapper.setAttribute('aria-label', 'Table details');
        const hint = document.createElement('p');
        hint.className = 'omv-phone-table-hint';
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'omv-phone-view-button';
        table.before(button, hint, wrapper);
        wrapper.append(table);
        state = {wrapper, hint, button, width: 0, viewport: 0, cards: !table.matches('.scroll-vertical.virtualized')};
        button.addEventListener('click', () => {
          state.cards = !state.cards;
          dispatchEvent(new Event('resize')); schedule();
        });
        tables.set(table, state);
      }
      const viewport = state.wrapper.clientWidth;
      if (!viewport) return;
      const headers = [...table.querySelectorAll('.datatable-header-cell')];
      const columns = headers.length || 4;
      const labels = headers.map(cell => (cell.querySelector('.datatable-header-cell-label')?.textContent || cell.textContent).trim());
      table.querySelectorAll('.datatable-body-row').forEach(row => {
        row.querySelectorAll('.datatable-body-cell').forEach((cell, index) => {
          cell.setAttribute('data-omv-label', labels[index] || (cell.querySelector('input[type="checkbox"]') ? 'Select' : 'Details'));
        });
      });
      state.wrapper.classList.toggle('omv-phone-card-view', state.cards);
      state.button.hidden = table.matches('.scroll-vertical.virtualized');
      const buttonText = state.cards ? 'Show table' : 'Show cards';
      if (state.button.textContent !== buttonText) state.button.textContent = buttonText;
      state.button.setAttribute('aria-label', state.cards ? 'Switch to table view with column sorting' : 'Switch to details that fit the screen');
      const hint = state.cards ? '' : 'Swipe the table sideways to see all columns.';
      if (state.hint.textContent !== hint) state.hint.textContent = hint;
      const width = Math.max(viewport, 560, columns * 160);
      if (width !== state.width || viewport !== state.viewport) {
        state.wrapper.style.setProperty('--omv-phone-table-width', width + 'px');
        state.wrapper.style.setProperty('--omv-phone-viewport', viewport + 'px');
        state.width = width; state.viewport = viewport;
        changed = true;
      }
      state.hint.hidden = state.cards || width <= viewport;
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
      for (const [table, {wrapper, hint, button}] of tables) {
        table.querySelectorAll('[data-omv-label]').forEach(cell => cell.removeAttribute('data-omv-label'));
        if (table.isConnected) wrapper.before(table);
        wrapper.remove(); hint.remove(); button.remove(); changed = true;
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


/* Organize only the Media verification grid. Native Angular cards/data remain
   owned by OMV; ordering uses CSS, never reparents cards or rewrites values. */
(() => {
  'use strict';
  const groups = [
    {name: 'Library & progress', metrics: [
      'V6 verified sources', 'Verified movies', 'Verified shows',
      'Initial V6 first sweep', 'V6 passes in last hour',
      'First sweep completed in last hour', 'Not yet V6 verified',
      'Unavailable / retry', 'Partial provider coverage retries',
      'V6 speed audit', 'Verification overview']},
    {name: 'Workers & current activity', metrics: [
      'Windows worker', 'Windows lanes', 'Windows decode queue',
      'Windows working on', 'Windows batch', 'Current torrent provider',
      'Windows local torrent', 'Windows requested topology']},
    {name: 'Discovery & metadata', metrics: [
      'Torrentio discovery', 'MediaFusion discovery',
      'Waiting for catalogue metadata', 'Metadata preparation pool',
      'New metadata admission', 'Metadata fetch retries (24h)',
      'Provider recovery waits in last hour', 'Preparation waits in last hour']},
    {name: 'Results & diagnostics', metrics: [
      'Windows titles attempted in last hour', 'Windows final passes',
      'Windows local V6 passes', 'Worker operations in last hour',
      'Windows torrent metadata deferred', 'Windows Debian fallback admitted',
      'Windows time per reported attempt', 'Windows speed checkpoint reasons',
      'Windows recent failure categories', 'Windows cancellation reasons',
      'Latest PC operation issue', 'Windows installed client versions']}
  ];
  let frame = 0;
  function refresh() {
    frame = 0;
    document.querySelectorAll('.home-media-verification-grid').forEach(grid => {
      const counts = [0, 0, 0, 0];
      [...grid.children].filter(card => card.classList.contains('item')).forEach((card, index) => {
        const title = (card.querySelector('.title')?.textContent || '').trim();
        let group = groups.findIndex(entry => entry.metrics.includes(title));
        if (group < 0) group = title.endsWith(' discovery') ? 2 : 3;
        let rank = groups[group].metrics.indexOf(title);
        if (group === 2 && title.startsWith('Jackett ') && title.endsWith(' discovery')) rank = 2;
        else if (group === 2 && rank >= 2) rank += 3;
        const order = String(group * 1000 + 1 + (rank < 0 ? 100 + index : rank));
        if (card.style.order !== order) card.style.order = order;
        counts[group]++;
      });
      groups.forEach((group, index) => {
        let heading = grid.querySelector('[data-home-media-section="' + index + '"]');
        if (!counts[index]) { if (heading) heading.remove(); return; }
        if (!heading) {
          heading = document.createElement('div');
          heading.className = 'home-media-section-heading';
          heading.dataset.homeMediaSection = String(index);
          heading.setAttribute('role', 'heading');
          heading.setAttribute('aria-level', '3');
          heading.style.order = String(index * 1000);
          heading.textContent = group.name;
          grid.append(heading);
        }
      });
    });
  }
  const schedule = () => { if (!frame) frame = requestAnimationFrame(refresh); };
  function start() {
    new MutationObserver(schedule).observe(document.body, {childList: true, subtree: true});
    schedule();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
