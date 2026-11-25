/**
 * Phonos UI — Panel (right-side drawer primitive)
 * ------------------------------------------------
 * A self-contained, dependency-free right-side panel that replaces centered
 * modals and intrusive native dialogs. It builds its own DOM on demand, so it
 * needs NO template markup and works on every page (dashboard, notebook,
 * configure pages) just by including this script + ui-panel.css.
 *
 * This is the foundation for the UX & notification refactor described in
 * docs/architecture/05-refactoring-roadmap.md §A. It is additive and opt-in:
 * including it changes nothing until code calls Panel.open()/confirm()/form().
 *
 * API
 *   Panel.open({ title, content, actions, width, dismissible, onClose }) -> instance
 *       content : HTML string OR a DOM Node
 *       actions : [{ label, variant, value, autofocus, onClick }]
 *                 variant ∈ 'primary' | 'danger' | 'secondary' (default)
 *       width   : 'default' | 'wide' | 'full'
 *       returns : { el, close(value), promise } — promise resolves with the
 *                 action value (or the value passed to close()).
 *
 *   Panel.confirm({ title, message, confirmText, cancelText, danger }) -> Promise<boolean>
 *       Non-blocking replacement for window.confirm().
 *
 *   Panel.form({ title, fields, submitText, cancelText }) -> Promise<object|null>
 *       fields : [{ name, label, type, value, placeholder, required, options }]
 *       resolves with { name: value, ... } on submit, or null on cancel.
 *       Non-blocking replacement for a series of window.prompt() calls.
 *
 *   Panel.alert({ title, message, okText }) -> Promise<void>
 *       Non-blocking replacement for window.alert() for non-critical notices.
 */
(function (global) {
    'use strict';

    var stack = [];

    function h(tag, cls, attrs) {
        var el = document.createElement(tag);
        if (cls) el.className = cls;
        if (attrs) Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
        return el;
    }

    function lockBodyScroll(lock) {
        if (lock && stack.length === 1) {
            document.body.dataset._uiPanelScroll = document.body.style.overflow || '';
            document.body.style.overflow = 'hidden';
        } else if (!lock && stack.length === 0) {
            document.body.style.overflow = document.body.dataset._uiPanelScroll || '';
            delete document.body.dataset._uiPanelScroll;
        }
    }

    function focusablesIn(root) {
        return Array.prototype.slice.call(root.querySelectorAll(
            'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
        ));
    }

    var Panel = {
        /** Open a right-side panel. Returns { el, close, promise }. */
        open: function (opts) {
            opts = opts || {};
            var resolveFn;
            var promise = new Promise(function (res) { resolveFn = res; });

            var overlay = h('div', 'ui-panel-overlay', { 'data-stack': String(stack.length) });
            var widthCls = opts.width === 'wide' ? ' ui-panel--wide'
                : opts.width === 'full' ? ' ui-panel--full' : '';
            var panel = h('div', 'ui-panel' + widthCls, { role: 'dialog', 'aria-modal': 'true' });

            // Header
            var header = h('div', 'ui-panel-header');
            var titleEl = h('h2', 'ui-panel-title');
            titleEl.textContent = opts.title || '';
            var closeBtn = h('button', 'ui-panel-close', { type: 'button', 'aria-label': 'Close' });
            closeBtn.innerHTML = '&times;';
            header.appendChild(titleEl);
            header.appendChild(closeBtn);

            // Body
            var body = h('div', 'ui-panel-body');
            if (opts.content instanceof Node) {
                body.appendChild(opts.content);
            } else if (typeof opts.content === 'string') {
                body.innerHTML = opts.content;
            }

            // Footer / actions
            var footer = h('div', 'ui-panel-footer');
            var instance = { el: panel, promise: promise };

            function close(value) {
                if (instance._closed) return;
                instance._closed = true;
                overlay.classList.remove('ui-panel-open');
                var idx = stack.indexOf(instance);
                if (idx !== -1) stack.splice(idx, 1);
                document.removeEventListener('keydown', onKey, true);
                var done = function () {
                    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
                    lockBodyScroll(false);
                    if (typeof opts.onClose === 'function') { try { opts.onClose(value); } catch (e) { /* noop */ } }
                    resolveFn(value);
                    if (instance._prevFocus && instance._prevFocus.focus) {
                        try { instance._prevFocus.focus(); } catch (e) { /* noop */ }
                    }
                };
                // Wait for the slide-out transition, but never hang.
                var settled = false;
                panel.addEventListener('transitionend', function te() {
                    if (settled) return; settled = true;
                    panel.removeEventListener('transitionend', te); done();
                });
                setTimeout(function () { if (!settled) { settled = true; done(); } }, 400);
            }
            instance.close = close;

            (opts.actions || []).forEach(function (a) {
                var variant = a.variant === 'primary' ? 'ui-panel-btn--primary'
                    : a.variant === 'danger' ? 'ui-panel-btn--danger'
                    : 'ui-panel-btn--secondary';
                var btn = h('button', 'ui-panel-btn ' + variant, { type: 'button' });
                btn.textContent = a.label;
                if (a.autofocus) instance._autofocus = btn;
                btn.addEventListener('click', function () {
                    var proceed = true;
                    if (typeof a.onClick === 'function') {
                        var r = a.onClick(instance);
                        if (r === false) proceed = false;
                    }
                    if (proceed !== false) close(a.value !== undefined ? a.value : a.label);
                });
                footer.appendChild(btn);
            });

            var dismissible = opts.dismissible !== false;
            closeBtn.addEventListener('click', function () { close(undefined); });
            overlay.addEventListener('mousedown', function (e) {
                if (e.target === overlay && dismissible) close(undefined);
            });

            function onKey(e) {
                if (stack[stack.length - 1] !== instance) return; // only top panel
                if (e.key === 'Escape' && dismissible) { e.stopPropagation(); close(undefined); }
                if (e.key === 'Tab') {
                    var f = focusablesIn(panel);
                    if (!f.length) return;
                    var first = f[0], last = f[f.length - 1];
                    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
                    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
                }
            }

            panel.appendChild(header);
            panel.appendChild(body);
            panel.appendChild(footer);
            overlay.appendChild(panel);
            document.body.appendChild(overlay);

            instance._prevFocus = document.activeElement;
            stack.push(instance);
            lockBodyScroll(true);
            document.addEventListener('keydown', onKey, true);

            // Animate in on next frame.
            requestAnimationFrame(function () {
                overlay.classList.add('ui-panel-open');
                var toFocus = instance._autofocus || focusablesIn(panel)[0] || closeBtn;
                if (toFocus && toFocus.focus) { try { toFocus.focus(); } catch (e) { /* noop */ } }
            });

            return instance;
        },

        /** Non-blocking confirm. Resolves true/false. */
        confirm: function (opts) {
            opts = opts || {};
            return this.open({
                title: opts.title || 'Confirm',
                width: opts.width,
                content: '<p class="ui-panel-message">' +
                    escapeHtml(opts.message || 'Are you sure?') + '</p>',
                actions: [
                    { label: opts.cancelText || 'Cancel', variant: 'secondary', value: false },
                    { label: opts.confirmText || 'Confirm', variant: opts.danger ? 'danger' : 'primary', value: true, autofocus: true }
                ]
            }).promise.then(function (v) { return v === true; });
        },

        /** Non-blocking alert for non-critical notices. */
        alert: function (opts) {
            opts = opts || {};
            return this.open({
                title: opts.title || 'Notice',
                content: '<p class="ui-panel-message">' + escapeHtml(opts.message || '') + '</p>',
                actions: [{ label: opts.okText || 'OK', variant: 'primary', value: true, autofocus: true }]
            }).promise.then(function () { return undefined; });
        },

        /** Non-blocking form. Resolves the field map on submit, or null on cancel. */
        form: function (opts) {
            opts = opts || {};
            var wrap = h('form', null);
            wrap.setAttribute('novalidate', 'novalidate');
            (opts.fields || []).forEach(function (f) {
                var field = h('div', 'ui-panel-field');
                if (f.label) {
                    var lab = h('label'); lab.textContent = f.label;
                    lab.setAttribute('for', 'uipf_' + f.name); field.appendChild(lab);
                }
                var input;
                if (f.type === 'textarea') {
                    input = h('textarea'); if (f.rows) input.rows = f.rows;
                } else if (f.type === 'select') {
                    input = h('select');
                    (f.options || []).forEach(function (o) {
                        var opt = h('option'); opt.value = o.value; opt.textContent = o.label; input.appendChild(opt);
                    });
                } else {
                    input = h('input'); input.type = f.type || 'text';
                }
                input.id = 'uipf_' + f.name; input.name = f.name;
                if (f.placeholder) input.placeholder = f.placeholder;
                if (f.required) input.required = true;
                if (f.value !== undefined && f.value !== null) input.value = f.value;
                field.appendChild(input);
                wrap.appendChild(field);
            });

            var instance = this.open({
                title: opts.title || 'Details',
                width: opts.width,
                content: wrap,
                actions: [
                    { label: opts.cancelText || 'Cancel', variant: 'secondary', value: null },
                    {
                        label: opts.submitText || 'Save', variant: 'primary', autofocus: true,
                        onClick: function (inst) {
                            if (!wrap.checkValidity()) { wrap.reportValidity(); return false; }
                            var data = {};
                            (opts.fields || []).forEach(function (f) {
                                var node = wrap.querySelector('[name="' + f.name + '"]');
                                data[f.name] = node ? node.value : undefined;
                            });
                            inst.close(data);
                            return false; // we've already closed with the data
                        }
                    }
                ]
            });
            return instance.promise;
        },

        /** True if any panel is currently open. */
        isOpen: function () { return stack.length > 0; },

        /** Close the topmost panel. */
        closeTop: function (value) { if (stack.length) stack[stack.length - 1].close(value); }
    };

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    global.Panel = Panel;
})(typeof window !== 'undefined' ? window : this);
