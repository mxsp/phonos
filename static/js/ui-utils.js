/**
 * Phonos UI Utilities
 * Enhanced keyboard shortcuts, toast notifications, and UI helpers
 */

(function() {
    'use strict';

    // ============================================
    // KEYBOARD SHORTCUTS
    // ============================================

    const ShortcutManager = {
        shortcuts: new Map(),
        sequenceBuffer: [],
        sequenceTimeout: null,
        enabled: true,

        init() {
            document.addEventListener('keydown', this.handleKeyDown.bind(this));

            const modal = document.getElementById('shortcuts-modal');
            const closeBtn = document.getElementById('shortcuts-close');

            if (closeBtn) {
                closeBtn.addEventListener('click', () => this.hideShortcutsModal());
            }

            if (modal) {
                modal.addEventListener('click', (e) => {
                    if (e.target === modal) this.hideShortcutsModal();
                });
            }

            this.register('?', () => this.showShortcutsModal(), window.i18n.t('common.shortcuts.showShortcuts'));
            this.register('Escape', () => this.closeActiveModal(), window.i18n.t('common.shortcuts.closeModal'));
            this.register('/', () => this.focusSearch(), window.i18n.t('common.shortcuts.focusSearch'));
            this.register('n', () => this.navigateTo('submit-job'), window.i18n.t('common.shortcuts.newJob'));
            this.register('r', () => this.refreshCurrentView(), window.i18n.t('common.shortcuts.refreshData'));
            this.register('g d', () => this.navigateTo('dashboard'), window.i18n.t('common.shortcuts.goToDashboard'));
            this.register('g j', () => this.navigateTo('jobs'), window.i18n.t('common.shortcuts.goToJobs'));
            this.register('g p', () => this.navigateTo('projects'), window.i18n.t('common.shortcuts.goToProjects'));
            this.register('g f', () => this.navigateTo('files'), window.i18n.t('common.shortcuts.goToFiles'));

            document.addEventListener('keydown', (e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                    e.preventDefault();
                    this.openCommandPalette();
                }
            });
        },

        register(keys, callback, description) {
            this.shortcuts.set(keys.toLowerCase(), { callback: callback, description: description || '' });
        },

        handleKeyDown(e) {
            if (!this.enabled) return;
            if (this.isTyping(e.target)) return;

            const key = e.key.toLowerCase();

            if (this.sequenceTimeout) {
                clearTimeout(this.sequenceTimeout);
            }

            this.sequenceBuffer.push(key);
            const sequence = this.sequenceBuffer.join(' ');

            if (this.shortcuts.has(sequence)) {
                e.preventDefault();
                this.shortcuts.get(sequence).callback();
                this.sequenceBuffer = [];
                return;
            }

            if (this.sequenceBuffer.length === 1 && this.shortcuts.has(key)) {
                this.sequenceTimeout = setTimeout(() => {
                    if (this.sequenceBuffer.length === 1 && this.shortcuts.has(key)) {
                        this.shortcuts.get(key).callback();
                    }
                    this.sequenceBuffer = [];
                }, 300);
                return;
            }

            this.sequenceTimeout = setTimeout(() => {
                this.sequenceBuffer = [];
            }, 500);
        },

        isTyping(element) {
            const tagName = element.tagName.toLowerCase();
            return tagName === 'input' || tagName === 'textarea' || tagName === 'select' || element.isContentEditable;
        },

        showShortcutsModal() {
            const modal = document.getElementById('shortcuts-modal');
            if (modal) {
                modal.classList.add('active');
                document.body.style.overflow = 'hidden';
            }
        },

        hideShortcutsModal() {
            const modal = document.getElementById('shortcuts-modal');
            if (modal) {
                modal.classList.remove('active');
                document.body.style.overflow = '';
            }
        },

        closeActiveModal() {
            this.hideShortcutsModal();
            const modalOverlay = document.getElementById('modal-overlay');
            if (modalOverlay && modalOverlay.classList.contains('active')) {
                modalOverlay.classList.remove('active');
            }
            document.querySelectorAll('.dropdown.active').forEach(function(d) { d.classList.remove('active'); });
        },

        navigateTo(view) {
            if (typeof App !== 'undefined' && App.navigateTo) {
                App.navigateTo(view);
            } else {
                var navItem = document.querySelector('[data-view="' + view + '"]');
                if (navItem) navItem.click();
            }
        },

        focusSearch() {
            var searchInput = document.querySelector('.search-input, #search-input, [type="search"]');
            if (searchInput) {
                searchInput.focus();
                searchInput.select();
            }
        },

        refreshCurrentView() {
            if (typeof App !== 'undefined' && App.refreshCurrentView) {
                App.refreshCurrentView();
            } else {
                window.location.reload();
            }
        },

        openCommandPalette() {
            var palette = document.getElementById('command-palette');
            if (palette) {
                palette.classList.add('active');
            }
        }
    };

    // ============================================
    // TOAST NOTIFICATIONS
    // ============================================

    const ToastManager = {
        container: null,
        maxVisible: 5,

        init() {
            this.container = document.getElementById('toast-container');
            if (!this.container) {
                this.container = document.createElement('div');
                this.container.className = 'toast-container';
                this.container.id = 'toast-container';
                document.body.appendChild(this.container);
            }
        },

        escapeHtml(text) {
            var div = document.createElement('div');
            div.textContent = text;
            return div.textContent;
        },

        createIcon(type) {
            var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('width', '16');
            svg.setAttribute('height', '16');
            svg.setAttribute('viewBox', '0 0 24 24');
            svg.setAttribute('fill', 'none');
            svg.setAttribute('stroke', 'currentColor');
            svg.setAttribute('stroke-width', '2');

            if (type === 'success') {
                var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                path.setAttribute('d', 'M20 6L9 17l-5-5');
                svg.appendChild(path);
            } else if (type === 'error') {
                var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', '12');
                circle.setAttribute('cy', '12');
                circle.setAttribute('r', '10');
                svg.appendChild(circle);
                var line1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line1.setAttribute('x1', '15');
                line1.setAttribute('y1', '9');
                line1.setAttribute('x2', '9');
                line1.setAttribute('y2', '15');
                svg.appendChild(line1);
                var line2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line2.setAttribute('x1', '9');
                line2.setAttribute('y1', '9');
                line2.setAttribute('x2', '15');
                line2.setAttribute('y2', '15');
                svg.appendChild(line2);
            } else if (type === 'warning') {
                var path2 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                path2.setAttribute('d', 'M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z');
                svg.appendChild(path2);
            } else {
                var circle2 = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle2.setAttribute('cx', '12');
                circle2.setAttribute('cy', '12');
                circle2.setAttribute('r', '10');
                svg.appendChild(circle2);
            }
            return svg;
        },

        show(options) {
            var type = options.type || 'info';
            var title = options.title || '';
            var message = options.message || '';
            var duration = options.duration !== undefined ? options.duration : 5000;
            var closable = options.closable !== undefined ? options.closable : true;

            var toast = document.createElement('div');
            toast.className = 'toast toast--' + type;
            toast.setAttribute('role', 'alert');
            toast.setAttribute('aria-live', 'polite');

            var iconWrapper = document.createElement('div');
            iconWrapper.className = 'toast__icon';
            iconWrapper.appendChild(this.createIcon(type));
            toast.appendChild(iconWrapper);

            var content = document.createElement('div');
            content.className = 'toast__content';

            if (title) {
                var titleEl = document.createElement('div');
                titleEl.className = 'toast__title';
                titleEl.textContent = title;
                content.appendChild(titleEl);
            }

            if (message) {
                var msgEl = document.createElement('div');
                msgEl.className = 'toast__message';
                msgEl.textContent = message;
                content.appendChild(msgEl);
            }

            toast.appendChild(content);

            if (closable) {
                var closeBtn = document.createElement('button');
                closeBtn.className = 'toast__close';
                closeBtn.setAttribute('aria-label', 'Close notification');
                var closeSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                closeSvg.setAttribute('width', '14');
                closeSvg.setAttribute('height', '14');
                closeSvg.setAttribute('viewBox', '0 0 24 24');
                closeSvg.setAttribute('fill', 'none');
                closeSvg.setAttribute('stroke', 'currentColor');
                closeSvg.setAttribute('stroke-width', '2');
                var l1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                l1.setAttribute('x1', '18');
                l1.setAttribute('y1', '6');
                l1.setAttribute('x2', '6');
                l1.setAttribute('y2', '18');
                closeSvg.appendChild(l1);
                var l2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                l2.setAttribute('x1', '6');
                l2.setAttribute('y1', '6');
                l2.setAttribute('x2', '18');
                l2.setAttribute('y2', '18');
                closeSvg.appendChild(l2);
                closeBtn.appendChild(closeSvg);
                closeBtn.addEventListener('click', () => this.dismiss(toast));
                toast.appendChild(closeBtn);
            }

            if (duration > 0) {
                var progress = document.createElement('div');
                progress.className = 'toast__progress';
                progress.style.animationDuration = duration + 'ms';
                toast.appendChild(progress);
            }

            this.container.appendChild(toast);

            var toasts = this.container.querySelectorAll('.toast:not(.toast--exiting)');
            if (toasts.length > this.maxVisible) {
                this.dismiss(toasts[0]);
            }

            if (duration > 0) {
                setTimeout(() => this.dismiss(toast), duration);
            }

            return toast;
        },

        dismiss(toast) {
            if (!toast || toast.classList.contains('toast--exiting')) return;
            toast.classList.add('toast--exiting');
            setTimeout(function() {
                if (toast.parentNode) {
                    toast.parentNode.removeChild(toast);
                }
            }, 200);
        },

        success(message, title) {
            return this.show({ type: 'success', title: title || window.i18n.t('common.toast.success'), message: message });
        },

        error(message, title) {
            return this.show({ type: 'error', title: title || window.i18n.t('common.toast.error'), message: message, duration: 8000 });
        },

        warning(message, title) {
            return this.show({ type: 'warning', title: title || window.i18n.t('common.toast.warning'), message: message });
        },

        info(message, title) {
            return this.show({ type: 'info', title: title || window.i18n.t('common.toast.info'), message: message });
        }
    };

    // ============================================
    // EMPTY STATE HELPER
    // ============================================

    const EmptyState = {
        createIcon(type) {
            var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('viewBox', '0 0 24 24');
            svg.setAttribute('fill', 'none');
            svg.setAttribute('stroke', 'currentColor');
            svg.setAttribute('stroke-width', '1.5');

            var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            if (type === 'inbox') {
                path.setAttribute('d', 'M4 6h16v12H4zM4 12h16M8 6V4h8v2');
            } else if (type === 'search') {
                var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', '11');
                circle.setAttribute('cy', '11');
                circle.setAttribute('r', '8');
                svg.appendChild(circle);
                path.setAttribute('d', 'M21 21l-4.35-4.35');
            } else if (type === 'folder') {
                path.setAttribute('d', 'M22 19V9a2 2 0 0 0-2-2h-8l-2-2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2z');
            } else {
                path.setAttribute('d', 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6');
            }
            svg.appendChild(path);
            return svg;
        },

        create(options) {
            var icon = options.icon || 'inbox';
            var title = options.title || 'No items found';
            var description = options.description || '';
            var actionText = options.actionText || '';
            var actionCallback = options.actionCallback || null;

            var container = document.createElement('div');
            container.className = 'empty-state';

            var iconWrapper = document.createElement('div');
            iconWrapper.className = 'empty-state__icon';
            iconWrapper.appendChild(this.createIcon(icon));
            container.appendChild(iconWrapper);

            var titleEl = document.createElement('h3');
            titleEl.className = 'empty-state__title';
            titleEl.textContent = title;
            container.appendChild(titleEl);

            if (description) {
                var descEl = document.createElement('p');
                descEl.className = 'empty-state__description';
                descEl.textContent = description;
                container.appendChild(descEl);
            }

            if (actionText) {
                var btn = document.createElement('button');
                btn.className = 'btn-primary empty-state__action';
                btn.textContent = actionText;
                if (actionCallback) {
                    btn.addEventListener('click', actionCallback);
                }
                container.appendChild(btn);
            }

            return container;
        }
    };

    // ============================================
    // INITIALIZATION
    // ============================================

    function init() {
        ShortcutManager.init();
        ToastManager.init();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.PhonosUI = {
        shortcuts: ShortcutManager,
        toast: ToastManager,
        emptyState: EmptyState
    };

})();
