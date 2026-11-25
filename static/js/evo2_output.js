/*
 * Evo2 job output view (templates/partials/_evo2_overview.html + _evo2_sequence.html).
 *
 * Reads the payload from <script id="evo2-data" type="application/json">, which
 * phonos_app/helpers/evo2_view.py builds: seed, generated, probs (aligned with
 * the generated bases). Positions shown to the user are 1-based over seed +
 * generated, so a generated base at index i sits at position seed.length + i + 1.
 */
(function (global) {
    'use strict';

    var MAX_GROUPS = 6;  // at most 60 bases per row
    var GROUP = 10;      // bases per spaced group
    var BIN_EDGES = [0.3, 0.5, 0.8];

    function tr(key, fallback, params) {
        var i18n = global.i18n;
        if (i18n && typeof i18n.t === 'function') {
            var full = 'viewOutput.evo2.' + key;
            var value = i18n.t(full, params || {});
            if (value && value !== full) return value;
        }
        return String(fallback).replace(/\{(\w+)\}/g, function (m, k) {
            return params && Object.prototype.hasOwnProperty.call(params, k) ? params[k] : m;
        });
    }

    function bin(p) {
        if (p < BIN_EDGES[0]) return 0;
        if (p < BIN_EDGES[1]) return 1;
        if (p < BIN_EDGES[2]) return 2;
        return 3;
    }

    // The palette lives on .evo2-view (static/css/evo2_output.css), theme tokens on :root;
    // reading from the view element resolves both.
    function cssVar(name, fallback) {
        var el = document.querySelector('.evo2-view') || document.documentElement;
        var v = getComputedStyle(el).getPropertyValue(name).trim();
        return v || fallback;
    }

    var Evo2View = {
        data: null,
        full: '',
        selected: null,
        row: GROUP * MAX_GROUPS,
        matches: [],
        matchIndex: -1,

        init: function () {
            var node = document.getElementById('evo2-data');
            if (!node) return;
            try { this.data = JSON.parse(node.textContent); } catch (e) { return; }
            this.full = this.data.seed + this.data.generated;
            this.renderSequence();
            this.renderStrip();
            this.renderChart();
            this.bindToolbar();
            this.bindJumps();
        },

        /* ---- position helpers ---------------------------------------- */

        probAt: function (pos) {
            var i = pos - this.data.seed.length - 1;
            return i >= 0 && i < this.data.probs.length ? this.data.probs[i] : null;
        },

        isSeed: function (pos) { return pos <= this.data.seed.length; },

        /* ---- sequence grid -------------------------------------------- */

        /* As many 10-base groups per row as fit the box (1–6), so phones get no sideways scroll. */
        fitRow: function (host) {
            var width = host.clientWidth;
            if (!width) return this.row;
            var probe = document.createElement('div');
            probe.className = 'evo2-row';
            probe.style.visibility = 'hidden';
            probe.innerHTML = '<span class="evo2-row-pos">0000</span><span class="evo2-row-bases"><span class="evo2-group">'
                + new Array(GROUP + 1).join('<span class="evo2-b">A</span>') + '</span></span><span class="evo2-row-end">0000</span>';
            host.appendChild(probe);
            var bases = probe.querySelector('.evo2-row-bases');
            var groupW = probe.querySelector('.evo2-group').getBoundingClientRect().width;
            var rowGap = parseFloat(getComputedStyle(probe).columnGap) || 0;
            var fixed = probe.querySelector('.evo2-row-pos').getBoundingClientRect().width
                + probe.querySelector('.evo2-row-end').getBoundingClientRect().width + 2 * rowGap;
            var gap = parseFloat(getComputedStyle(bases).columnGap) || 0;
            var style = getComputedStyle(host);
            var inner = width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
            probe.remove();
            var groups = Math.floor((inner - fixed + gap) / (groupW + gap));
            return GROUP * Math.max(1, Math.min(MAX_GROUPS, groups));
        },

        /* Build the rows for the current width, keeping selection and motif marks. */
        layoutRows: function (host) {
            var frag = document.createDocumentFragment();
            var seq = this.full;
            for (var start = 0; start < seq.length; start += this.row) {
                var row = document.createElement('div');
                row.className = 'evo2-row';
                var label = document.createElement('span');
                label.className = 'evo2-row-pos';
                label.textContent = start + 1;
                row.appendChild(label);
                var bases = document.createElement('span');
                bases.className = 'evo2-row-bases';
                for (var g = start; g < Math.min(start + this.row, seq.length); g += GROUP) {
                    var group = document.createElement('span');
                    group.className = 'evo2-group';
                    for (var i = g; i < Math.min(g + GROUP, seq.length); i++) {
                        var pos = i + 1;
                        var b = document.createElement('span');
                        var base = seq[i];
                        b.textContent = base;
                        b.dataset.pos = pos;
                        var cls = 'evo2-b evo2-nt-text-' + base;
                        if (this.isSeed(pos)) {
                            cls += ' is-seed';
                        } else {
                            var p = this.probAt(pos);
                            if (p !== null) cls += ' evo2-bin-' + bin(p);
                        }
                        b.className = cls;
                        group.appendChild(b);
                    }
                    bases.appendChild(group);
                }
                row.appendChild(bases);
                var end = document.createElement('span');
                end.className = 'evo2-row-end';
                end.textContent = Math.min(start + this.row, seq.length);
                row.appendChild(end);
                frag.appendChild(row);
            }
            host.replaceChildren(frag);
            var self = this;
            this.matches.forEach(function (start) {
                for (var j = 0; j < self._motifLen; j++) {
                    var el = self.baseEl(start + j);
                    if (el) el.classList.add('is-match');
                }
            });
            if (this.matchIndex >= 0) {
                for (var j = 0; j < this._motifLen; j++) {
                    var el = this.baseEl(this.matches[this.matchIndex] + j);
                    if (el) el.classList.add('is-match-current');
                }
            }
            if (this.selected) {
                var sel = this.baseEl(this.selected);
                if (sel) sel.classList.add('is-selected');
            }
        },

        renderSequence: function () {
            var host = document.getElementById('evo2-seq');
            if (!host) return;
            this.row = this.fitRow(host);
            this.layoutRows(host);

            var self = this;
            if (global.ResizeObserver) {
                new global.ResizeObserver(function () {
                    var row = self.fitRow(host);
                    if (row !== self.row) {
                        self.row = row;
                        self.layoutRows(host);
                    }
                }).observe(host);
            }

            host.addEventListener('mouseover', function (e) {
                var t = e.target.closest('.evo2-b');
                if (t) self.showInfo(+t.dataset.pos, false);
            });
            host.addEventListener('mouseleave', function () {
                if (self.selected) self.showInfo(self.selected, true); else self.clearInfo();
            });
            host.addEventListener('click', function (e) {
                var t = e.target.closest('.evo2-b');
                if (t) self.select(+t.dataset.pos, false);
            });
            host.addEventListener('keydown', function (e) {
                if (!self.selected) return;
                var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -self.row, ArrowDown: self.row }[e.key];
                if (!step) return;
                e.preventDefault();
                self.select(Math.min(self.full.length, Math.max(1, self.selected + step)), true);
            });
        },

        baseEl: function (pos) {
            return document.querySelector('#evo2-seq .evo2-b[data-pos="' + pos + '"]');
        },

        select: function (pos, scroll) {
            var prev = document.querySelector('#evo2-seq .evo2-b.is-selected');
            if (prev) prev.classList.remove('is-selected');
            var el = this.baseEl(pos);
            if (!el) return;
            el.classList.add('is-selected');
            this.selected = pos;
            this.showInfo(pos, true);
            if (scroll) el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' });
        },

        showInfo: function (pos, pinned) {
            var info = document.getElementById('evo2-info');
            if (!info) return;
            var base = this.full[pos - 1];
            var parts = [tr('infoPosition', 'Position {pos}', { pos: pos }), base];
            if (this.isSeed(pos)) {
                parts.push(tr('infoSeed', 'seed (your input)'));
            } else {
                var p = this.probAt(pos);
                parts.push(p === null ? tr('infoGenerated', 'generated')
                    : tr('infoProb', 'probability {p}%', { p: (p * 100).toFixed(1) }));
            }
            var ctxStart = Math.max(0, pos - 6);
            var context = this.full.slice(ctxStart, pos - 1) + '[' + base + ']' + this.full.slice(pos, pos + 5);
            info.replaceChildren();
            var strong = document.createElement('strong');
            strong.textContent = parts.join(' · ');
            info.appendChild(strong);
            var ctx = document.createElement('span');
            ctx.className = 'evo2-mono evo2-info-ctx';
            ctx.textContent = context;
            info.appendChild(ctx);
            info.classList.toggle('is-pinned', !!pinned);
        },

        clearInfo: function () {
            var info = document.getElementById('evo2-info');
            if (!info) return;
            info.replaceChildren();
            var span = document.createElement('span');
            span.className = 'evo2-info-empty';
            span.textContent = tr('infoHint', 'Hover or click a base to see its position and probability.');
            info.appendChild(span);
        },

        /* ---- probability strip (minimap) ------------------------------ */

        renderStrip: function () {
            var canvas = document.getElementById('evo2-strip');
            if (!canvas || !this.data.probs.length) return;
            var self = this;
            var draw = function () {
                var w = canvas.clientWidth;
                if (!w) return;  // tab hidden; redrawn when shown
                var dpr = global.devicePixelRatio || 1;
                canvas.width = Math.round(w * dpr);
                canvas.height = Math.round(14 * dpr);
                var ctx = canvas.getContext('2d');
                var colors = [0, 1, 2, 3].map(function (k) { return cssVar('--evo2-bin-' + k, '#888'); });
                var seedColor = cssVar('--evo2-seed', '#555');
                var n = self.full.length;
                var cw = canvas.width / n;
                for (var i = 0; i < n; i++) {
                    var pos = i + 1;
                    var p = self.probAt(pos);
                    ctx.fillStyle = self.isSeed(pos) ? seedColor : (p === null ? seedColor : colors[bin(p)]);
                    ctx.fillRect(Math.floor(i * cw), 0, Math.ceil(cw) + 1, canvas.height);
                }
            };
            draw();
            // The Sequence tab starts hidden (width 0); redraw whenever the strip gets a size.
            if (global.ResizeObserver) new global.ResizeObserver(draw).observe(canvas);
            canvas.addEventListener('click', function (e) {
                var r = canvas.getBoundingClientRect();
                var pos = Math.min(self.full.length, Math.max(1, Math.ceil((e.clientX - r.left) / r.width * self.full.length)));
                self.select(pos, true);
            });
            if (!global.ResizeObserver) global.addEventListener('resize', draw);
            this._drawStrip = draw;
        },

        /* ---- chart ------------------------------------------------------ */

        renderChart: function () {
            var canvas = document.getElementById('evo2-prob-chart');
            if (!canvas || !global.Chart || !this.data.probs.length) return;
            var probs = this.data.probs;
            var offset = this.data.seed.length;
            var labels = probs.map(function (_, i) { return offset + i + 1; });
            var colors = [0, 1, 2, 3].map(function (k) { return cssVar('--evo2-bin-solid-' + k, '#888'); });
            var rolling = probs.map(function (_, i) {
                var a = Math.max(0, i - 4), b = Math.min(probs.length, i + 6), s = 0;
                for (var k = a; k < b; k++) s += probs[k];
                return s / (b - a);
            });
            var grid = cssVar('--border-color', 'rgba(255,255,255,0.15)');
            var tick = cssVar('--text-tertiary', '#aaa');
            var line = cssVar('--text-primary', '#fff');
            var self = this;
            this.chart = new global.Chart(canvas, {
                data: {
                    labels: labels,
                    datasets: [
                        {
                            type: 'line', label: tr('rollingMean', '10-bp average'), data: rolling,
                            borderColor: line, borderWidth: 1.5, pointRadius: 0, tension: 0.3, order: 0
                        },
                        {
                            type: 'bar', label: tr('probability', 'Probability'), data: probs,
                            backgroundColor: probs.map(function (p) { return colors[bin(p)]; }),
                            barPercentage: 1, categoryPercentage: 1, order: 1
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                title: function (items) {
                                    var pos = +items[0].label;
                                    return tr('infoPosition', 'Position {pos}', { pos: pos }) + ' · ' + self.full[pos - 1];
                                },
                                label: function (item) {
                                    return item.dataset.label + ': ' + (item.raw * 100).toFixed(1) + '%';
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { color: tick, maxTicksLimit: 12, autoSkip: true, maxRotation: 0 },
                            title: { display: true, text: tr('position', 'Position'), color: tick }
                        },
                        y: {
                            min: 0, max: 1,
                            grid: {
                                color: function (c) { return c.tick && c.tick.value === 0.5 ? tick : grid; },
                                lineWidth: function (c) { return c.tick && c.tick.value === 0.5 ? 1.5 : 1; }
                            },
                            ticks: { color: tick, stepSize: 0.25, callback: function (v) { return Math.round(v * 100) + '%'; } }
                        }
                    },
                    onClick: function (evt, elements) {
                        if (!elements.length) return;
                        self.goTo(labels[elements[0].index]);
                    }
                }
            });
        },

        /* ---- toolbar: colour mode, motif search ---------------------- */

        bindToolbar: function () {
            var self = this;
            document.querySelectorAll('#evo2-sequence .evo2-seg [data-color]').forEach(function (btn) {
                btn.addEventListener('click', function () { self.setColorMode(btn.dataset.color); });
            });
            var input = document.getElementById('evo2-motif');
            if (input) {
                var timer;
                input.addEventListener('input', function () {
                    clearTimeout(timer);
                    timer = setTimeout(function () { self.findMotif(input.value); }, 150);
                });
                input.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter') { e.preventDefault(); self.stepMatch(e.shiftKey ? -1 : 1); }
                    if (e.key === 'Escape') { input.value = ''; self.findMotif(''); }
                });
            }
            var prev = document.getElementById('evo2-motif-prev');
            var next = document.getElementById('evo2-motif-next');
            if (prev) prev.addEventListener('click', function () { self.stepMatch(-1); });
            if (next) next.addEventListener('click', function () { self.stepMatch(1); });
        },

        setColorMode: function (mode) {
            var host = document.getElementById('evo2-seq');
            if (host) host.dataset.color = mode;
            document.querySelectorAll('#evo2-sequence .evo2-seg [data-color]').forEach(function (btn) {
                btn.setAttribute('aria-checked', String(btn.dataset.color === mode));
            });
            var lp = document.getElementById('evo2-legend-prob');
            var lb = document.getElementById('evo2-legend-base');
            if (lp) lp.hidden = mode !== 'prob';
            if (lb) lb.hidden = mode !== 'base';
        },

        findMotif: function (raw) {
            document.querySelectorAll('#evo2-seq .is-match, #evo2-seq .is-match-current').forEach(function (el) {
                el.classList.remove('is-match', 'is-match-current');
            });
            var motif = String(raw || '').toUpperCase().replace(/[^ACGTUN]/g, '').replace(/U/g, 'T');
            var count = document.getElementById('evo2-motif-count');
            this.matches = [];
            this.matchIndex = -1;
            if (motif.length >= 2) {
                var re = new RegExp('(?=' + motif.replace(/N/g, '[ACGT]') + ')', 'g');
                var m;
                while ((m = re.exec(this.full)) !== null) {
                    this.matches.push(m.index + 1);
                    re.lastIndex = m.index + 1;
                }
                for (var k = 0; k < this.matches.length; k++) {
                    for (var j = 0; j < motif.length; j++) {
                        var el = this.baseEl(this.matches[k] + j);
                        if (el) el.classList.add('is-match');
                    }
                }
            }
            if (count) {
                count.textContent = motif.length < 2 ? ''
                    : tr('matches', '{n} matches', { n: this.matches.length });
            }
            this._motifLen = motif.length;
            var disabled = this.matches.length === 0;
            ['evo2-motif-prev', 'evo2-motif-next'].forEach(function (id) {
                var b = document.getElementById(id);
                if (b) b.disabled = disabled;
            });
            if (this.matches.length) this.stepMatch(1);
        },

        stepMatch: function (dir) {
            if (!this.matches.length) return;
            document.querySelectorAll('#evo2-seq .is-match-current').forEach(function (el) {
                el.classList.remove('is-match-current');
            });
            this.matchIndex = (this.matchIndex + dir + this.matches.length) % this.matches.length;
            var start = this.matches[this.matchIndex];
            for (var j = 0; j < this._motifLen; j++) {
                var el = this.baseEl(start + j);
                if (el) el.classList.add('is-match-current');
            }
            var count = document.getElementById('evo2-motif-count');
            if (count) {
                count.textContent = tr('matchOf', '{i} of {n}', { i: this.matchIndex + 1, n: this.matches.length });
            }
            this.select(start, true);
        },

        /* ---- cross-tab jumps, copy, download ------------------------- */

        bindJumps: function () {
            var self = this;
            document.querySelectorAll('#evo2-overview .evo2-jump').forEach(function (el) {
                var go = function (e) { e.preventDefault(); self.goTo(+el.dataset.pos); };
                el.addEventListener('click', go);
                el.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') go(e); });
            });
        },

        goTo: function (pos) {
            if (typeof global.switchTab === 'function') global.switchTab('sequence');
            if (this._drawStrip) this._drawStrip();
            var self = this;
            // let the tab become visible before scrolling into it
            requestAnimationFrame(function () { self.select(pos, true); });
        },

        text: function (which) {
            return which === 'generated' ? this.data.generated : this.full;
        },

        copy: function (which) {
            var text = this.text(which);
            var done = function () {
                if (global.Toast && typeof global.Toast.success === 'function') {
                    global.Toast.success(tr('copied', 'Copied {n} bases', { n: text.length }));
                }
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done, function () { Evo2View._fallbackCopy(text); done(); });
            } else {
                this._fallbackCopy(text);
                done();
            }
        },

        _fallbackCopy: function (text) {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch (e) { /* nothing else to try */ }
            ta.remove();
        },

        downloadFasta: function (which) {
            var seq = this.text(which);
            var id = this.data.job_id || 'evo2';
            var header = '>evo2_' + id + (which === 'generated' ? '_generated' : '_full')
                + (this.data.seed && which !== 'generated' ? ' seed=1-' + this.data.seed.length : '')
                + ' len=' + seq.length;
            var body = seq.match(/.{1,80}/g).join('\n');
            var blob = new Blob([header + '\n' + body + '\n'], { type: 'text/plain' });
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'evo2_' + id + (which === 'generated' ? '_generated' : '') + '.fasta';
            document.body.appendChild(a);
            a.click();
            setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
        }
    };

    global.Evo2View = Evo2View;
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { Evo2View.init(); });
    } else {
        Evo2View.init();
    }
})(window);
