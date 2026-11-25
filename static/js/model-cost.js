/* Cost and hosting strip for model pages (roadmap task 19, step 2).
 * Reads the model id from <meta name="phonos-model-id">, asks /api/models/<id>/estimate and shows
 * "Estimated cost: 20 credits · Balance after: 980" above the Run button plus "Runs at <provider> (<region>)".
 * Also fills every #user-credits with the real balance. Retired models show their replacement instead. */
(function () {
    'use strict';
    var meta = document.querySelector('meta[name="phonos-model-id"]');
    var modelId = (meta && meta.content) || window.PHONOS_MODEL_ID;
    if (!modelId) return;

    var last = null;

    function t(key, fallback, opts) {
        try {
            if (window.i18n && typeof window.i18n.t === 'function') {
                var v = window.i18n.t(key, opts || {});
                if (v && v !== key) return v;
            }
        } catch (e) { /* use fallback */ }
        var out = fallback;
        Object.keys(opts || {}).forEach(function (k) { out = out.split('{' + k + '}').join(opts[k]); });
        return out;
    }

    function fmt(n) { return Number(n).toLocaleString(); }

    function anchor() {
        var btn = document.getElementById('submit-btn') || document.getElementById('generate-btn');
        if (!btn) return null;
        var parent = btn.parentElement;
        if (parent && parent.tagName !== 'FORM' && window.getComputedStyle(parent).display.indexOf('flex') !== -1) {
            return parent;
        }
        return btn;
    }

    function strip() {
        var el = document.getElementById('model-cost-strip');
        if (el) return el;
        var a = anchor();
        if (!a || !a.parentNode) return null;
        el = document.createElement('div');
        el.id = 'model-cost-strip';
        el.className = 'model-cost-strip';
        el.setAttribute('role', 'status');
        el.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline;margin:0 0 10px;font-size:13px;line-height:1.4;color:var(--text-secondary,#a1a1aa);';
        a.parentNode.insertBefore(el, a);
        return el;
    }

    function line(text, strong) {
        var s = document.createElement('span');
        if (strong) { s.style.fontWeight = '600'; s.style.color = strong; }
        s.textContent = text;
        return s;
    }

    function render(d) {
        last = d;
        document.querySelectorAll('#user-credits').forEach(function (el) { el.textContent = String(d.balance); });
        document.querySelectorAll('[data-model-cost]').forEach(function (el) { el.textContent = fmt(d.credit_cost); });
        var el = strip();
        if (!el) return;
        el.textContent = '';
        if (d.ui_state === 'retired') {
            el.appendChild(line(t('models.retired', 'Retired'), 'var(--accent-warning,#f59e0b)'));
            return;
        }
        el.appendChild(line(t('models.estimatedCost', 'Estimated cost: {cost} credits', { cost: fmt(d.credit_cost) })));
        el.appendChild(line('·'));
        el.appendChild(d.sufficient
            ? line(t('models.balanceAfter', 'Balance after: {balance}', { balance: fmt(d.balance_after) }))
            : line(t('models.insufficient', 'Not enough credits'), 'var(--accent-error,#ef4444)'));
        if (d.hosted_by) {
            var run = line(t('models.runsAt', 'Runs at {provider} ({region})', { provider: d.hosted_by.provider, region: d.hosted_by.region }));
            run.style.flexBasis = '100%';
            run.style.fontSize = '12px';
            el.appendChild(run);
        }
    }

    function refresh(extra) {
        var opts = { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } };
        var url = '/api/models/' + encodeURIComponent(modelId) + '/estimate';
        if (extra && (extra.sequence || extra.params)) {
            opts.method = 'POST';
            opts.body = JSON.stringify(extra);
        }
        return fetch(url, opts)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d && d.status === 'success') render(d); return d; })
            .catch(function () { return null; });
    }

    window.ModelCost = { refresh: refresh, last: function () { return last; } };
    window.addEventListener('i18n:change', function () { if (last) render(last); });
    window.addEventListener('i18n:ready', function () { if (last) render(last); });
    window.addEventListener('focus', function () { refresh(); });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { refresh(); });
    else refresh();
})();
