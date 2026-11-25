/* Grid snap — shared by Phonos (static/js/grid-snap.js) and fyreflysystems.de
   (shared/ff/grid-snap.js); keep the two copies identical.

   The background grid is drawn in cells of --ff-cell. Widths, gutters and paddings snap to it
   in CSS; heights depend on content, so this rounds the height of each block listed in the
   script tag's data-snap attribute up to whole cells. Blocks that follow one another then keep
   starting on a grid line. Purely visual: without JavaScript nothing breaks, the grid just
   drifts below the first block. Elements with their own inline min-height are left alone. */
(function () {
  'use strict';
  if (!('ResizeObserver' in window) || !document.querySelectorAll) return;

  var script = document.currentScript;
  var SELECTOR = (script && script.getAttribute('data-snap')) || '[data-grid-snap]';
  var SKIP = (script && script.getAttribute('data-snap-skip')) || '';
  var observed = new WeakSet();
  var pending = new Set();
  var scheduled = false;

  function cell() {
    var v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ff-cell'));
    return v > 0 ? v : 32;
  }

  function flush() {
    scheduled = false;
    var c = cell();
    var els = Array.from(pending).filter(function (el) { return el.isConnected; });
    pending.clear();
    // Reset first, then read every height, then write: one layout pass, no thrashing.
    els.forEach(function (el) { el.style.minHeight = ''; });
    var boxes = els.map(function (el) {
      var cs = getComputedStyle(el);
      var extra = cs.boxSizing === 'content-box'
        ? parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth)
        : 0;
      return { h: el.getBoundingClientRect().height, extra: extra, hidden: cs.display === 'none' };
    });
    els.forEach(function (el, i) {
      var b = boxes[i];
      if (b.hidden || b.h < 1) return;
      var snapped = Math.ceil((b.h - 0.05) / c) * c;
      if (snapped - b.h > 0.05) el.style.minHeight = (snapped - b.extra) + 'px';
    });
  }

  var ro = new ResizeObserver(function (entries) {
    entries.forEach(function (e) { pending.add(e.target); });
    if (!scheduled) { scheduled = true; requestAnimationFrame(flush); }
  });

  function scan() {
    document.querySelectorAll(SELECTOR).forEach(function (el) {
      if (observed.has(el) || el.style.minHeight || (SKIP && el.matches(SKIP))) return;
      observed.add(el);
      ro.observe(el);
    });
  }

  // Views rendered later (the Phonos dashboard, opened menus) are picked up as they appear.
  var rescan = false;
  var mo = new MutationObserver(function () {
    if (rescan) return;
    rescan = true;
    requestAnimationFrame(function () { rescan = false; scan(); });
  });

  function start() {
    scan();
    mo.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('resize', function () {
      // The cell changes size at the phone breakpoint: re-measure everything.
      document.querySelectorAll(SELECTOR).forEach(function (el) { if (observed.has(el)) pending.add(el); });
      if (!scheduled) { scheduled = true; requestAnimationFrame(flush); }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
