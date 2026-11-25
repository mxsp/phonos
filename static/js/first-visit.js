/* First-visit notice — shared by fyreflysystems.de (shared/ff/first-visit.js) and Phonos
   (static/js/first-visit.js); keep the two copies identical.

   On the first visit a modal says the site is under development and asks for consent to
   cookies and the terms of service. Two buttons:
     OK     records the acceptance, then lets the page store what it wanted to store and fires
            'ff:first-visit-accepted' (the page's own consent code turns analytics on from there).
     Leave  navigates away and stores nothing.
   Until OK, nothing is written: localStorage, sessionStorage and document.cookie writes are held
   in memory (the page keeps working for this view) and only written after OK. Load this as the
   FIRST script in <head>, before anything that could store something.

   Script-tag attributes:
     data-key        localStorage key that records the acceptance (default 'ff-first-visit')
     data-name       site name for the text (default 'This site')
     data-privacy    href of the privacy policy      data-terms   href of the terms of service
     data-analytics  "1" if OK also turns on Google Analytics (said so in the text)
     data-hide       selector of an older consent banner to keep hidden while the notice is open */
(function () {
  'use strict';
  var VERSION = '2026-10-27';
  var script = document.currentScript;
  function attr(n, d) { return (script && script.getAttribute(n)) || d; }
  var KEY = attr('data-key', 'ff-first-visit');
  var NAME = attr('data-name', '');
  var PRIVACY = attr('data-privacy', '/privacy');
  var TERMS = attr('data-terms', '/terms');
  var ANALYTICS = attr('data-analytics', '0') === '1';
  var HIDE = attr('data-hide', '');

  var SP = window.Storage && Storage.prototype;
  var real = SP ? { get: SP.getItem, set: SP.setItem, remove: SP.removeItem, clear: SP.clear } : null;

  function acceptedNow() {
    try {
      var v = JSON.parse(real.get.call(window.localStorage, KEY) || 'null');
      return !!(v && v.v === VERSION);
    } catch (e) { return false; }
  }

  var api = window.FFFirstVisit = {
    version: VERSION,
    accepted: function () { return real ? acceptedNow() : true; },
    pending: false
  };
  if (!real || acceptedNow()) return;
  api.pending = true;
  document.documentElement.classList.add('fv-pending');

  /* ---- 1. Hold every write until OK ---- */
  var held = { local: {}, session: {} };   // key -> string value, or null for "removed"
  var cleared = { local: false, session: false };
  function area(store) {
    try { if (store === window.localStorage) return 'local'; } catch (e) {}
    try { if (store === window.sessionStorage) return 'session'; } catch (e) {}
    return null;
  }
  SP.setItem = function (k, v) {
    var a = area(this);
    if (!a) return real.set.call(this, k, v);
    held[a][String(k)] = String(v);
  };
  SP.getItem = function (k) {
    var a = area(this);
    if (!a) return real.get.call(this, k);
    k = String(k);
    if (Object.prototype.hasOwnProperty.call(held[a], k)) return held[a][k];
    return cleared[a] ? null : real.get.call(this, k);
  };
  SP.removeItem = function (k) {
    var a = area(this);
    if (!a) return real.remove.call(this, k);
    held[a][String(k)] = null;
  };
  SP.clear = function () {
    var a = area(this);
    if (!a) return real.clear.call(this);
    held[a] = {}; cleared[a] = true;
  };
  var cookieDesc = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
  var heldCookies = [];
  if (cookieDesc && cookieDesc.configurable !== false) {
    try {
      Object.defineProperty(document, 'cookie', {
        configurable: true,
        get: function () { return cookieDesc.get.call(document); },
        set: function (v) { heldCookies.push(String(v)); }
      });
    } catch (e) { /* leave cookies alone if the browser refuses */ }
  }

  function release() {
    SP.setItem = real.set; SP.getItem = real.get; SP.removeItem = real.remove; SP.clear = real.clear;
    try { delete document.cookie; } catch (e) {}
    ['local', 'session'].forEach(function (a) {
      var store; try { store = a === 'local' ? window.localStorage : window.sessionStorage; } catch (e) { return; }
      try {
        if (cleared[a]) store.clear();
        Object.keys(held[a]).forEach(function (k) {
          if (held[a][k] === null) store.removeItem(k); else store.setItem(k, held[a][k]);
        });
      } catch (e) {}
    });
    heldCookies.forEach(function (c) { try { document.cookie = c; } catch (e) {} });
    held = { local: {}, session: {} }; heldCookies = [];
  }

  /* ---- 2. The notice ---- */
  var T = {
    en: {
      label: 'Notice',
      title: 'Still under development',
      body: (NAME || 'This site') + ' is under development. Some features are still missing or unfinished, and things may change without notice.',
      consent: function (p, t) {
        return 'By selecting OK you accept ' + (ANALYTICS ? 'our cookies, including Google Analytics,' : 'the cookies this site needs to work') +
          ' and the ' + t('terms of service') + '. Details are in the ' + p('privacy policy') + '. Leave closes this page and stores nothing.';
      },
      ok: 'OK', leave: 'Leave'
    },
    de: {
      label: 'Hinweis',
      title: 'Noch im Aufbau',
      body: (NAME || 'Diese Website') + ' befindet sich in Entwicklung. Einige Funktionen fehlen noch oder sind unfertig; Änderungen sind jederzeit möglich.',
      consent: function (p, t) {
        return 'Mit „OK“ akzeptieren Sie ' + (ANALYTICS ? 'unsere Cookies, einschließlich Google Analytics,' : 'die Cookies, die diese Website zum Funktionieren braucht,') +
          ' und die ' + t('Nutzungsbedingungen') + '. Einzelheiten stehen in der ' + p('Datenschutzerklärung') + '. „Verlassen“ schließt diese Seite und speichert nichts.';
      },
      ok: 'OK', leave: 'Verlassen'
    }
  };

  var CSS = [
    '.fv-pending ' + (HIDE || '.fv-none') + '{display:none!important}',
    '.fv{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:16px;',
    'background-color:rgba(9,9,11,.72);',
    'background-image:linear-gradient(rgba(250,250,250,.07) 1px,transparent 1px),linear-gradient(90deg,rgba(250,250,250,.07) 1px,transparent 1px);',
    'background-size:var(--ff-cell,32px) var(--ff-cell,32px)}',
    '.fv__box{box-sizing:border-box;width:100%;max-width:640px;max-height:calc(100vh - 32px);overflow:auto;margin:0;',
    'padding:32px;background:var(--stage-100,#0b0b0d);color:var(--ink,#fafafa);',
    'border:1px solid currentColor;box-shadow:8px 8px 0 var(--ff-accent,var(--accent-primary,#10B981));',
    'font:400 16px/1.5 var(--font-sans,var(--font-main,"Inter",system-ui,sans-serif));text-align:left}',
    '.fv__label{display:flex;align-items:center;gap:12px;margin:0 0 16px;min-height:16px;',
    'font:500 11px/16px var(--ff-mono,var(--font-mono,"IBM Plex Mono",ui-monospace,monospace));letter-spacing:.14em;text-transform:uppercase}',
    '.fv__label::before{content:"";width:16px;height:1px;background:var(--ff-accent-ink,var(--ff-accent,var(--accent-primary-light,#34d399)))}',
    '.fv__title{margin:0 0 16px;font-size:24px;line-height:32px;font-weight:500;letter-spacing:-.01em}',
    '.fv__text{margin:0 0 16px;max-width:60ch}',
    '.fv__text--small{font-size:14px;line-height:24px;opacity:.86;margin-bottom:32px}',
    '.fv__text a{color:inherit;text-decoration:underline;text-underline-offset:3px}',
    '.fv__actions{display:flex;gap:16px;flex-wrap:wrap}',
    '.fv__btn{box-sizing:border-box;min-width:128px;min-height:48px;padding:0 24px;border:1px solid currentColor;border-radius:0;cursor:pointer;',
    'font:500 13px/1 var(--ff-mono,var(--font-mono,"IBM Plex Mono",ui-monospace,monospace));letter-spacing:.1em;text-transform:uppercase}',
    '.fv__btn--ok{background:var(--ink,#fafafa);color:var(--stage-100,#0b0b0d);border-color:var(--ink,#fafafa)}',
    '.fv__btn--leave{background:transparent;color:inherit}',
    '.fv__btn:hover{text-decoration:underline;text-underline-offset:4px}',
    '.fv a:focus-visible,.fv__btn:focus-visible{outline:2px solid var(--ff-accent-ink,var(--accent-primary-light,#34d399));outline-offset:2px}',
    '@media (max-width:860px){.fv__box{padding:32px 16px;box-shadow:4px 4px 0 var(--ff-accent,var(--accent-primary,#10B981))}.fv__btn{flex:1 1 120px}}'
  ].join('');

  function lang() { return /^de/i.test(document.documentElement.lang || '') ? 'de' : 'en'; }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }

  function build() {
    var t = T[lang()];
    var style = el('style'); style.textContent = CSS; document.head.appendChild(style);
    var root = el('div', 'fv'); root.setAttribute('data-first-visit', '');
    var box = el('div', 'fv__box');
    box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'fv-title'); box.setAttribute('aria-describedby', 'fv-body');
    box.appendChild(el('p', 'fv__label', t.label));
    var h = el('h2', 'fv__title', t.title); h.id = 'fv-title'; box.appendChild(h);
    var body = el('p', 'fv__text', t.body); body.id = 'fv-body'; box.appendChild(body);
    var consent = el('p', 'fv__text fv__text--small');
    var links = {};
    function link(href, key) { return function (label) { links[key] = { href: href, label: label }; return '\u0000' + key + '\u0000'; }; }
    var parts = t.consent(link(PRIVACY, 'p'), link(TERMS, 't')).split('\u0000');
    parts.forEach(function (part, i) {
      if (i % 2 === 1) { var a = el('a', '', links[part].label); a.href = links[part].href; a.target = '_blank'; a.rel = 'noopener'; consent.appendChild(a); }
      else if (part) consent.appendChild(document.createTextNode(part));
    });
    box.appendChild(consent);
    var actions = el('div', 'fv__actions');
    var ok = el('button', 'fv__btn fv__btn--ok', t.ok); ok.type = 'button';
    var leave = el('button', 'fv__btn fv__btn--leave', t.leave); leave.type = 'button';
    actions.appendChild(ok); actions.appendChild(leave); box.appendChild(actions);
    root.appendChild(box);

    // Everything behind the notice is inert: no focus, no clicks, hidden from screen readers.
    var behind = Array.prototype.filter.call(document.body.children, function (c) { return c.tagName !== 'SCRIPT' && c.tagName !== 'STYLE'; });
    behind.forEach(function (c) { if (!c.hasAttribute('inert')) { c.setAttribute('inert', ''); c.setAttribute('data-fv-inert', ''); } });
    document.body.appendChild(root);
    var prevOverflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = 'hidden';

    // Focus trap (for browsers without inert, and for Shift+Tab out of the first control).
    root.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      var items = box.querySelectorAll('a[href], button');
      var first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    document.addEventListener('focusin', function keep(e) {
      if (!root.isConnected) { document.removeEventListener('focusin', keep); return; }
      if (!root.contains(e.target)) ok.focus();
    });

    function close() {
      root.remove(); style.remove();
      document.querySelectorAll('[data-fv-inert]').forEach(function (c) { c.removeAttribute('inert'); c.removeAttribute('data-fv-inert'); });
      document.documentElement.style.overflow = prevOverflow;
      document.documentElement.classList.remove('fv-pending');
    }
    ok.addEventListener('click', function () {
      release();
      try { real.set.call(window.localStorage, KEY, JSON.stringify({ v: VERSION, at: new Date().toISOString(), cookies: true, terms: true })); } catch (e) {}
      api.pending = false;
      close();
      try { window.dispatchEvent(new CustomEvent('ff:first-visit-accepted')); } catch (e) {}
    });
    leave.addEventListener('click', function () {
      // Back where the visitor came from, else an empty page. Nothing is stored either way.
      var ref = document.referrer, here = location.origin;
      try { if (ref && new URL(ref).origin !== here) { location.replace(ref); return; } } catch (e) {}
      if (history.length > 1) { history.back(); setTimeout(function () { location.replace('about:blank'); }, 600); }
      else location.replace('about:blank');
    });
    ok.focus();
  }
  if (document.body) build(); else document.addEventListener('DOMContentLoaded', build);
})();
