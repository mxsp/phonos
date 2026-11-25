/**
 * Oversight launcher behaviour + the cross-product app switcher.
 *
 * Two jobs:
 *   1. On the launcher, let a user pin one surface as their default landing
 *      page. The choice is stored client-side and applied by the login
 *      redirect handler, so it survives across the three products without
 *      needing a schema change.
 *   2. On every surface, drive the header app-switcher menu.
 *
 * Loaded standalone on the launcher and alongside app.js elsewhere, so it must
 * not assume Toast/App exist.
 */
(function (global) {
    'use strict';

    var STORAGE_KEY = 'phonosDefaultSurface';
    var SURFACES = {
        phonos: '/dashboard',
        notebook: '/notebook',
        analytics: '/analytics'
    };

    function readDefaultSurface() {
        try {
            var value = global.localStorage.getItem(STORAGE_KEY);
            return Object.prototype.hasOwnProperty.call(SURFACES, value) ? value : null;
        } catch (e) {
            return null;
        }
    }

    function writeDefaultSurface(surface) {
        try {
            if (surface) global.localStorage.setItem(STORAGE_KEY, surface);
            else global.localStorage.removeItem(STORAGE_KEY);
            return true;
        } catch (e) {
            return false;
        }
    }

    function notify(message) {
        if (global.Toast && typeof global.Toast.success === 'function') {
            global.Toast.success(message);
            return;
        }
        // The launcher loads no app.js, so fall back to a self-removing toast
        // built against the shared .toast-container markup.
        var host = document.getElementById('toast-container');
        if (!host) return;
        var toast = document.createElement('div');
        toast.className = 'toast toast-success';
        toast.textContent = message;
        host.appendChild(toast);
        setTimeout(function () {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 2600);
    }

    /** Reflect the stored default on the launcher cards. */
    function paintDefaultState() {
        var current = readDefaultSurface();
        document.querySelectorAll('.oversight-card[data-surface]').forEach(function (card) {
            var isDefault = card.getAttribute('data-surface') === current;
            card.classList.toggle('is-default', isDefault);

            var badge = card.querySelector('[data-default-badge]');
            if (badge) badge.hidden = !isDefault;

            var button = card.querySelector('[data-set-default]');
            if (button) {
                button.textContent = isDefault ? 'Default ✓' : 'Set as default';
                button.setAttribute('aria-pressed', isDefault ? 'true' : 'false');
            }
        });
    }

    function initLauncher() {
        if (!document.querySelector('.oversight-grid')) return;
        paintDefaultState();

        document.querySelectorAll('[data-set-default]').forEach(function (button) {
            button.addEventListener('click', function (event) {
                // The button lives inside the card's <a>; without this the click
                // would navigate away instead of setting the preference.
                event.preventDefault();
                event.stopPropagation();

                var surface = button.getAttribute('data-set-default');
                // Clicking the current default clears it, so there is a way back
                // to "always show the launcher".
                var next = readDefaultSurface() === surface ? null : surface;
                if (!writeDefaultSurface(next)) {
                    notify('Could not save your preference — browser storage is unavailable.');
                    return;
                }
                paintDefaultState();
                notify(next
                    ? 'Opening ' + surface.replace(/^\w/, function (c) { return c.toUpperCase(); })
                        + ' by default after sign-in.'
                    : 'Cleared — you will land here after sign-in.');
            });
        });
    }

    function initSwitcher() {
        var root = document.querySelector('[data-app-switcher]');
        if (!root) return;
        // Only the caret opens the menu - the Center half is a plain link and
        // must navigate rather than toggle.
        var button = root.querySelector('#app-switcher-btn');
        var menu = root.querySelector('#app-switcher-menu');
        if (!button || !menu) return;

        var setOpen = function (open) {
            menu.hidden = !open;
            button.setAttribute('aria-expanded', open ? 'true' : 'false');
        };

        button.addEventListener('click', function (event) {
            event.stopPropagation();
            setOpen(menu.hidden);
        });

        document.addEventListener('click', function (event) {
            if (!menu.hidden && !root.contains(event.target)) setOpen(false);
        });

        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && !menu.hidden) {
                setOpen(false);
                button.focus();
            }
        });
    }

    /**
     * Honour the stored default when the launcher was reached automatically.
     *
     * The server cannot see a client-side preference, so it always sends users
     * to the launcher and tags that redirect with `?auto=1`. Only then is it
     * safe to forward on - a user who deliberately clicked "Overview" in the
     * switcher arrives without the marker and stays put, so the launcher never
     * becomes unreachable for whoever has set a default.
     */
    function applyDefaultLandingRedirect() {
        var params = new URLSearchParams(global.location.search);
        if (params.get('auto') !== '1') return;

        var surface = readDefaultSurface();
        var target = surface ? SURFACES[surface] : null;
        if (!target) return;
        if (global.location.pathname === target) return;

        global.location.replace(target);
    }

    function init() {
        applyDefaultLandingRedirect();
        initLauncher();
        initSwitcher();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    global.PhonosOversight = {
        getDefaultSurface: readDefaultSurface,
        setDefaultSurface: function (surface) {
            var ok = writeDefaultSurface(surface);
            paintDefaultState();
            return ok;
        },
        SURFACES: SURFACES
    };
})(window);
