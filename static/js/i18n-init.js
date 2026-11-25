/**
 * i18n Initialization
 * Loads translations and paints the DOM once window.i18n is available.
 * Re-paints automatically when the SPA swaps out the content area.
 */

(function() {
    'use strict';

    async function waitFor(predicate, timeoutMs = 5000) {
        const start = Date.now();
        while (!predicate()) {
            if (Date.now() - start > timeoutMs) {
                console.warn('[i18n-init] Timed out waiting for dependency');
                return false;
            }
            await new Promise(r => setTimeout(r, 25));
        }
        return true;
    }

    function watchMutations() {
        if (!('MutationObserver' in window)) return;

        const OBSERVE_OPTS = { childList: true, subtree: true };
        let scheduled = false;
        let observer;

        // Coalesce all mutations within a frame into a single repaint.
        // We disconnect while painting so that our own textContent/innerHTML
        // writes don't re-trigger the observer (which would otherwise spin
        // in a feedback loop and lock up the page).
        function repaint() {
            scheduled = false;
            observer.disconnect();
            try {
                window.i18n.updateUI();
            } finally {
                observer.observe(document.body, OBSERVE_OPTS);
            }
        }

        function schedule() {
            if (scheduled) return;
            scheduled = true;
            (window.requestAnimationFrame || window.setTimeout)(repaint, 0);
        }

        observer = new MutationObserver((mutations) => {
            for (const m of mutations) {
                if (m.addedNodes && m.addedNodes.length) {
                    schedule();
                    return;
                }
            }
        });
        observer.observe(document.body, OBSERVE_OPTS);
    }

    (async function() {
        const ok = await waitFor(() => window.i18n);
        if (!ok) return;

        try {
            await window.i18n.init();
            watchMutations();
        } catch (e) {
            console.error('[i18n-init] init failed', e);
            document.body.setAttribute('data-i18n-loaded', 'error');
        }
    })();
})();
