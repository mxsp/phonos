/*!
 * Phonos cookie-consent module — the SINGLE source of truth for consent state.
 *
 * Every surface that reads or writes consent (the consent banner, the in-app
 * Settings panel, and the Google Analytics loader in _google_analytics.html)
 * goes through window.PhonosConsent. This removes the historical drift risk of
 * three independent stores being written by three independent code paths.
 *
 * Stores written atomically by set():
 *   - localStorage 'cookie-consent-v2'      : 'accepted' | 'rejected'
 *   - localStorage 'cookie-preferences'     : {"analytics": <bool>}
 *   - localStorage 'cookie-consent-date'    : ISO timestamp (freshness)
 *   - localStorage 'cookie-consent-version' : policy version the user agreed to
 *   - cookie       'analytics_consent'      : '1' | '0' (server-side detection)
 *
 * GDPR/TTDSG notes:
 *   - Analytics is opt-in; nothing non-essential is written before set(true).
 *   - Consent is re-requested after MAX_AGE_DAYS (freshness) or when
 *     CONSENT_VERSION is bumped (policy change) — see needsPrompt().
 *   - Withdrawal (set(false)) actively deletes Google's _ga/_gid cookies.
 *   - set() best-effort records a server-side proof of consent (Art. 7(1)).
 *
 * This file must load BEFORE the analytics logic in _google_analytics.html, so
 * it is included as a plain (non-deferred) <script src> at the top of that
 * partial. It defines no analytics cookies on its own.
 */
(function (w) {
    'use strict';

    // Bump this when the cookie/privacy policy materially changes; every user is
    // then re-prompted on their next visit (see needsPrompt()).
    var CONSENT_VERSION = 1;
    // Re-request consent after this many days so a stored choice never goes stale.
    var MAX_AGE_DAYS = 365;

    var LS_STATE = 'cookie-consent-v2';
    var LS_PREFS = 'cookie-preferences';
    var LS_DATE = 'cookie-consent-date';
    var LS_VER = 'cookie-consent-version';
    var COOKIE = 'analytics_consent';

    function lsGet(k) { try { return w.localStorage.getItem(k); } catch (e) { return null; } }
    function lsSet(k, v) { try { w.localStorage.setItem(k, v); } catch (e) { /* private mode / blocked */ } }

    function readCookie(name) {
        var out = null;
        try {
            String(document.cookie).split(';').forEach(function (c) {
                var p = c.trim().split('=');
                if (p[0] === name) out = p.slice(1).join('=');
            });
        } catch (e) { /* document.cookie blocked */ }
        return out;
    }

    // Strict+Secure only over HTTPS so the cookie still functions on a plain
    // http://localhost during development.
    function cookieFlags() {
        return (w.location && w.location.protocol === 'https:')
            ? '; SameSite=Strict; Secure'
            : '; SameSite=Lax';
    }

    var PhonosConsent = {
        VERSION: CONSENT_VERSION,
        MAX_AGE_DAYS: MAX_AGE_DAYS,

        /**
         * Current stored decision: 'accepted' | 'rejected' | null.
         * localStorage is authoritative; the cookie is a fallback so the choice
         * survives a localStorage wipe (and lets the server detect it too).
         */
        get: function () {
            var state = lsGet(LS_STATE);
            if (state === 'accepted' || state === 'rejected') return state;
            var c = readCookie(COOKIE);
            if (c === '1') return 'accepted';
            if (c === '0') return 'rejected';
            return null;
        },

        /** True only when the user actively opted into analytics AND it is fresh. */
        analyticsAllowed: function () {
            return this.get() === 'accepted' && !this.needsPrompt();
        },

        /**
         * Whether the consent banner must be (re)shown:
         *   - no decision recorded yet, or
         *   - the recorded decision is stale (older than MAX_AGE_DAYS / undatable), or
         *   - the user agreed to an older policy than CONSENT_VERSION.
         * Existing dated consents that predate versioning are grandfathered in
         * (a missing version alone does not force a re-prompt).
         */
        needsPrompt: function () {
            var state = this.get();
            if (state !== 'accepted' && state !== 'rejected') return true;

            var date = lsGet(LS_DATE);
            if (!date) return true;
            var ageDays = (Date.now() - new Date(date).getTime()) / 86400000;
            if (isNaN(ageDays) || ageDays > MAX_AGE_DAYS) return true;

            var verStr = lsGet(LS_VER);
            if (verStr != null && parseInt(verStr, 10) < CONSENT_VERSION) return true;

            return false;
        },

        /**
         * The ONLY writer of consent state. Persists every store atomically,
         * applies the analytics decision immediately (no page reload required),
         * and records a server-side proof of consent.
         * @param {boolean} analytics  whether optional analytics cookies are allowed
         * @returns {'accepted'|'rejected'}
         */
        set: function (analytics) {
            analytics = !!analytics;
            var state = analytics ? 'accepted' : 'rejected';

            lsSet(LS_STATE, state);
            lsSet(LS_PREFS, JSON.stringify({ analytics: analytics }));
            lsSet(LS_DATE, new Date().toISOString());
            lsSet(LS_VER, String(CONSENT_VERSION));

            var expiry = new Date();
            expiry.setFullYear(expiry.getFullYear() + 1);
            document.cookie = COOKIE + '=' + (analytics ? '1' : '0') +
                '; expires=' + expiry.toUTCString() + '; path=/' + cookieFlags();

            if (analytics) { this.enableGA(); } else { this.disableGA(); }
            this.log(analytics);
            return state;
        },

        /** Load + configure GA via the single consent-gated loader. */
        enableGA: function () {
            if (typeof w.phonosLoadGtag === 'function') {
                w.phonosLoadGtag();
            } else if (typeof w.gtag === 'function') {
                w.gtag('consent', 'update', { 'analytics_storage': 'granted' });
            }
        },

        /** Deny analytics storage and clear any GA identifiers already set. */
        disableGA: function () {
            if (typeof w.gtag === 'function') {
                w.gtag('consent', 'update', { 'analytics_storage': 'denied' });
            }
            this.clearGA();
        },

        /** Delete Google's _ga / _gid / _ga_* / _gat cookies across host scopes. */
        clearGA: function () {
            try {
                var host = w.location.hostname;
                var domains = ['', host, '.' + host];
                var dot = host.indexOf('.');
                if (dot > -1) domains.push('.' + host.slice(dot + 1));
                String(document.cookie).split(';').forEach(function (c) {
                    var name = c.split('=')[0].trim();
                    if (name === '_ga' || name === '_gid' || name === '_gat' || name.indexOf('_ga_') === 0) {
                        domains.forEach(function (d) {
                            document.cookie = name + '=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT' +
                                (d ? '; domain=' + d : '');
                        });
                    }
                });
            } catch (e) { /* ignore */ }
        },

        /**
         * Best-effort server-side proof of consent (GDPR Art. 7(1) accountability).
         * Fire-and-forget; never blocks or breaks the consent flow if it fails.
         */
        log: function (analytics) {
            try {
                var body = JSON.stringify({ analytics: !!analytics, version: CONSENT_VERSION });
                if (w.navigator && typeof w.navigator.sendBeacon === 'function') {
                    var blob = new Blob([body], { type: 'application/json' });
                    w.navigator.sendBeacon('/api/consent', blob);
                } else if (typeof w.fetch === 'function') {
                    w.fetch('/api/consent', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: body,
                        credentials: 'same-origin',
                        keepalive: true
                    }).catch(function () { /* ignore */ });
                }
            } catch (e) { /* ignore */ }
        }
    };

    w.PhonosConsent = PhonosConsent;

    // The first-visit notice (static/js/first-visit.js): its OK accepts cookies including analytics.
    w.addEventListener('ff:first-visit-accepted', function () { PhonosConsent.set(true); });
})(window);
