/**
 * Phonos i18n - Internationalization System
 * Supports English (EN) and German (DE)
 *
 * Usage:
 *   <span data-i18n="nav.dashboard">Dashboard</span>
 *   <input data-i18n-placeholder="auth.enterEmail" placeholder="...">
 *   <span data-i18n="dashboard.welcome" data-i18n-params='{"username":"Max"}'></span>
 *
 *   const greeting = window.i18n.t('dashboard.welcome', { username: 'Max' });
 *
 * Programmatic language change:
 *   await window.i18n.setLanguage('de');
 */

(function() {
    'use strict';

    const STORAGE_KEY = 'phonos-language';
    const DEFAULT_LANG = 'en';
    const SUPPORTED = ['en', 'de'];

    class I18n {
        constructor() {
            this.currentLang = this._detectLanguage();
            this.translations = {};
            this.fallbackLang = DEFAULT_LANG;
            this.listeners = new Set();
            this._initPromise = null;
        }

        _detectLanguage() {
            try {
                const stored = localStorage.getItem(STORAGE_KEY);
                if (stored && SUPPORTED.includes(stored)) {
                    return stored;
                }
            } catch (e) {
                // localStorage may be blocked; fall through to default
            }
            return DEFAULT_LANG;
        }

        async loadTranslations(lang) {
            if (this.translations[lang]) return this.translations[lang];
            try {
                const response = await fetch(`/static/locales/${lang}.json`, { cache: 'no-cache' });
                if (!response.ok) {
                    throw new Error(`Failed to load translations for ${lang}: ${response.status}`);
                }
                this.translations[lang] = await response.json();
                return this.translations[lang];
            } catch (error) {
                console.error(`[i18n] ${error.message}`);
                if (lang !== this.fallbackLang) {
                    return this.loadTranslations(this.fallbackLang);
                }
                this.translations[lang] = {};
                return this.translations[lang];
            }
        }

        async init() {
            if (this._initPromise) return this._initPromise;
            this._initPromise = (async () => {
                await this.loadTranslations(this.currentLang);
                if (this.currentLang !== this.fallbackLang) {
                    await this.loadTranslations(this.fallbackLang);
                }
                this.updateHtmlLang();
                this.updateUI();
                document.body.setAttribute('data-i18n-loaded', 'true');
                window.dispatchEvent(new CustomEvent('i18n:ready', {
                    detail: { language: this.currentLang }
                }));
                return this.currentLang;
            })();
            return this._initPromise;
        }

        _resolve(keys, dict) {
            let value = dict;
            for (const k of keys) {
                if (value && typeof value === 'object' && k in value) {
                    value = value[k];
                } else {
                    return undefined;
                }
            }
            return value;
        }

        /**
         * Translate a dot-notation key. Falls back to fallback language, then returns the key.
         * @param {string} key
         * @param {object} [params] - placeholder values for {name} style strings
         * @returns {string}
         */
        t(key, params = {}) {
            const keys = key.split('.');
            let translation = this._resolve(keys, this.translations[this.currentLang]);

            if (translation === undefined && this.currentLang !== this.fallbackLang) {
                translation = this._resolve(keys, this.translations[this.fallbackLang]);
            }

            if (translation === undefined) {
                return key;
            }

            if (typeof translation === 'string' && params && Object.keys(params).length > 0) {
                return translation.replace(/\{(\w+)\}/g, (match, name) => {
                    return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match;
                });
            }

            return translation;
        }

        async setLanguage(lang) {
            if (!SUPPORTED.includes(lang)) {
                console.warn(`[i18n] Unsupported language: ${lang}`);
                return;
            }
            if (lang === this.currentLang && this.translations[lang]) {
                this.updateUI();
                this.updateHtmlLang();
                this._notify();
                return;
            }
            await this.loadTranslations(lang);
            if (lang !== this.fallbackLang) {
                await this.loadTranslations(this.fallbackLang);
            }
            this.currentLang = lang;
            try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* ignore */ }
            // Mirror the choice in a cookie so server-side routes (e.g. legal
            // pages) can serve the right language on the next request.
            try {
                const oneYear = 60 * 60 * 24 * 365;
                const secure = location.protocol === 'https:' ? '; Secure' : '';
                document.cookie = `${STORAGE_KEY}=${lang}; max-age=${oneYear}; path=/; SameSite=Lax${secure}`;
            } catch (e) { /* ignore */ }
            document.documentElement.setAttribute('data-lang', lang);
            this.updateUI();
            this.updateHtmlLang();
            this._notify();
        }

        toggleLanguage() {
            return this.setLanguage(this.currentLang === 'en' ? 'de' : 'en');
        }

        getLanguage() { return this.currentLang; }
        getSupportedLanguages() { return SUPPORTED.slice(); }

        updateHtmlLang() {
            document.documentElement.lang = this.currentLang;
            document.documentElement.setAttribute('data-lang', this.currentLang);
        }

        /**
         * Scan the DOM and update all elements tagged with i18n attributes.
         * Called automatically on language change; safe to call after dynamic DOM updates.
         */
        updateUI(root) {
            const scope = root || document;

            scope.querySelectorAll('[data-i18n]').forEach(el => {
                const key = el.getAttribute('data-i18n');
                const params = this._readParams(el);

                // i18next-style targeted syntax, e.g.
                //   data-i18n="[title]notebook.refresh"
                //   data-i18n="[aria-label]viewer.x;[title]viewer.x"
                // A leading "[target]" routes the translation to an attribute
                // (or html/text/prepend/append) instead of textContent. Without
                // it we keep the historical behaviour below. This is what makes
                // icon buttons show a tooltip instead of dumping the raw
                // "[title]notebook.refresh" string into their visible label.
                if (key.indexOf('[') !== -1) {
                    this._applyTargeted(el, key, params);
                    return;
                }

                const value = this.t(key, params);
                // Don't replace fallback text when translation key is not
                // found (t() returns the key itself in that case).
                if (value === key) return;

                // Only write when the value actually changes. This avoids
                // redundant DOM mutations on every repaint, which keeps
                // layout/reflow cost down and prevents the MutationObserver
                // in i18n-init from being re-triggered by our own writes.
                if (el.hasAttribute('data-i18n-html')) {
                    if (el.innerHTML !== value) el.innerHTML = value;
                } else if (el.hasAttribute('data-i18n-attr')) {
                    const attr = el.getAttribute('data-i18n-attr');
                    if (el.getAttribute(attr) !== value) el.setAttribute(attr, value);
                } else {
                    if (el.textContent !== value) el.textContent = value;
                }
            });

            scope.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
                const value = this.t(el.getAttribute('data-i18n-placeholder'));
                if (el.placeholder !== value) el.placeholder = value;
            });

            scope.querySelectorAll('[data-i18n-aria]').forEach(el => {
                const value = this.t(el.getAttribute('data-i18n-aria'));
                if (el.getAttribute('aria-label') !== value) el.setAttribute('aria-label', value);
            });

            scope.querySelectorAll('[data-i18n-title]').forEach(el => {
                const value = this.t(el.getAttribute('data-i18n-title'));
                if (el.title !== value) el.title = value;
            });
        }

        /**
         * Apply an i18next-style targeted data-i18n value to an element.
         * The value is one or more ";"-separated segments, each optionally
         * prefixed with "[target]". Recognised targets: html, text, prepend,
         * append; anything else is treated as an attribute name (title,
         * placeholder, aria-label, content, ...). A bare segment with no
         * prefix updates textContent.
         */
        _applyTargeted(el, raw, params) {
            raw.split(';').forEach(segment => {
                const seg = segment.trim();
                if (!seg) return;

                const match = seg.match(/^\[([^\]]+)\]\s*(.+)$/);
                const key = match ? match[2].trim() : seg;
                const value = this.t(key, params);

                // Don't replace text/attributes when the translation key is
                // not found (t() returns the key itself). Without this guard
                // a missing key like "notebook.wolframSave" would surface to
                // the user as a literal "class.function"-style string.
                if (value === key) return;

                if (!match) {
                    if (el.textContent !== value) el.textContent = value;
                    return;
                }

                const target = match[1].trim();

                switch (target) {
                    case 'html':
                        if (el.innerHTML !== value) el.innerHTML = value;
                        break;
                    case 'text':
                        if (el.textContent !== value) el.textContent = value;
                        break;
                    // prepend/append must be idempotent: updateUI runs on every
                    // DOM mutation, so we stash the original markup once and
                    // rebuild from it rather than stacking on each repaint.
                    case 'prepend': {
                        if (el._i18nOriginalHTML === undefined) el._i18nOriginalHTML = el.innerHTML;
                        const next = value + el._i18nOriginalHTML;
                        if (el.innerHTML !== next) el.innerHTML = next;
                        break;
                    }
                    case 'append': {
                        if (el._i18nOriginalHTML === undefined) el._i18nOriginalHTML = el.innerHTML;
                        const next = el._i18nOriginalHTML + value;
                        if (el.innerHTML !== next) el.innerHTML = next;
                        break;
                    }
                    default:
                        if (el.getAttribute(target) !== value) el.setAttribute(target, value);
                }
            });
        }

        _readParams(el) {
            // Accept both `data-i18n-params` and the `data-i18n-options` alias
            // used across some templates. Without this, {count} style
            // placeholders are never substituted and render literally.
            const merged = {};
            ['data-i18n-options', 'data-i18n-params'].forEach((attr) => {
                const raw = el.getAttribute(attr);
                if (!raw) return;
                try { Object.assign(merged, JSON.parse(raw)); } catch (e) { /* ignore malformed JSON */ }
            });
            return merged;
        }

        onChange(callback) {
            this.listeners.add(callback);
            return () => this.listeners.delete(callback);
        }

        _notify() {
            this.listeners.forEach(cb => {
                try { cb(this.currentLang); } catch (e) { console.error('[i18n] listener error', e); }
            });
            window.dispatchEvent(new CustomEvent('i18n:change', {
                detail: { language: this.currentLang }
            }));
        }
    }

    window.i18n = new I18n();
})();
