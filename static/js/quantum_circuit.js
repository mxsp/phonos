/**
 * Phonos Quantum Circuit Builder v1.0
 *
 * Visual drag/click circuit editor rendered into the notebook split panel.
 * Generates Qiskit Python code (executed via phonos.quantum.run) into the
 * owning quantum cell. The circuit model is persisted in
 * cell.metadata.quantum so the builder can be reopened and edited later.
 *
 * Public API (window.QuantumCircuitBuilder):
 *   open({container, circuit, onChange})  render the builder
 *   defaultModel()                        fresh 2-qubit model
 *   generateCode(model)                   model -> python source
 *   normalizeModel(raw)                   sanitize a stored model
 */
(function() {
'use strict';

var MAX_QUBITS = 12;
var MIN_COLS = 8;

// Gate catalog. clicks = number of grid picks needed to place.
// For cx/cz/ccx the LAST pick is the target, earlier picks are controls.
var GATES = {
    h:    { label: 'H',  name: 'Hadamard',        clicks: 1 },
    x:    { label: 'X',  name: 'Pauli-X (NOT)',   clicks: 1 },
    y:    { label: 'Y',  name: 'Pauli-Y',         clicks: 1 },
    z:    { label: 'Z',  name: 'Pauli-Z',         clicks: 1 },
    s:    { label: 'S',  name: 'S (√Z)',          clicks: 1 },
    sdg:  { label: 'S†', name: 'S-dagger',        clicks: 1 },
    t:    { label: 'T',  name: 'T (√S)',          clicks: 1 },
    tdg:  { label: 'T†', name: 'T-dagger',        clicks: 1 },
    rx:   { label: 'RX', name: 'X rotation',      clicks: 1, param: true },
    ry:   { label: 'RY', name: 'Y rotation',      clicks: 1, param: true },
    rz:   { label: 'RZ', name: 'Z rotation',      clicks: 1, param: true },
    p:    { label: 'P',  name: 'Phase',           clicks: 1, param: true },
    cx:   { label: 'CX', name: 'CNOT (control, then target)', clicks: 2 },
    cz:   { label: 'CZ', name: 'Controlled-Z',    clicks: 2 },
    swap: { label: 'SW', name: 'SWAP (two qubits)', clicks: 2 },
    ccx:  { label: 'CCX', name: 'Toffoli (2 controls, then target)', clicks: 3 },
    barrier: { label: '▍', name: 'Barrier (full column)', clicks: 1 },
    erase:   { label: '⌫', name: 'Erase gate', clicks: 1 }
};

var PALETTE_GROUPS = [
    ['h', 'x', 'y', 'z', 's', 'sdg', 't', 'tdg'],
    ['rx', 'ry', 'rz', 'p'],
    ['cx', 'cz', 'swap', 'ccx'],
    ['barrier', 'erase']
];

// ---------------------------------------------------------------------------
// Model helpers
// ---------------------------------------------------------------------------

function defaultModel() {
    return {
        version: 1,
        qubits: 2,
        shots: 1024,
        measureAll: true,
        mode: 'settings', // 'settings' | 'simulator' | 'hardware'
        gates: []
    };
}

// Angle expressions may only contain digits, pi, whitespace and + - * / ( ).
// This is both a UX guard and an injection guard: the string is embedded
// verbatim in generated Python.
function isValidParam(expr) {
    if (typeof expr !== 'string' || !expr.trim()) return false;
    if (!/^[0-9pi\s.+\-*/()]+$/i.test(expr)) return false;
    // No letters may remain once 'pi' tokens are removed (rejects 'p', 'ppi'…)
    if (/[a-z]/i.test(expr.replace(/pi/gi, ''))) return false;
    return true;
}

function gateSpan(gate) {
    // All qubit rows a gate touches (endpoints + everything between).
    var qs = [gate.q];
    if (Array.isArray(gate.ctrls)) qs = qs.concat(gate.ctrls);
    if (typeof gate.q2 === 'number') qs.push(gate.q2);
    var lo = Math.min.apply(null, qs), hi = Math.max.apply(null, qs);
    var span = [];
    for (var q = lo; q <= hi; q++) span.push(q);
    return span;
}

function normalizeModel(raw) {
    var m = defaultModel();
    if (!raw || typeof raw !== 'object') return m;
    m.qubits = Math.min(MAX_QUBITS, Math.max(1, parseInt(raw.qubits, 10) || 2));
    m.shots = Math.min(100000, Math.max(1, parseInt(raw.shots, 10) || 1024));
    m.measureAll = raw.measureAll !== false;
    m.mode = ['settings', 'simulator', 'hardware'].indexOf(raw.mode) !== -1 ? raw.mode : 'settings';
    var gates = Array.isArray(raw.gates) ? raw.gates : [];
    m.gates = [];
    for (var i = 0; i < gates.length; i++) {
        var g = gates[i];
        if (!g || !GATES[g.g] || g.g === 'erase') continue;
        var col = parseInt(g.col, 10);
        if (isNaN(col) || col < 0) continue;
        var gate = { g: g.g, col: col };
        if (g.g !== 'barrier') {
            var q = parseInt(g.q, 10);
            if (isNaN(q) || q < 0 || q >= m.qubits) continue;
            gate.q = q;
            if (Array.isArray(g.ctrls)) {
                gate.ctrls = g.ctrls.map(function(c) { return parseInt(c, 10); })
                    .filter(function(c) { return !isNaN(c) && c >= 0 && c < m.qubits && c !== q; });
                var need = g.g === 'ccx' ? 2 : (g.g === 'cx' || g.g === 'cz' ? 1 : 0);
                if (gate.ctrls.length !== need) continue;
            }
            if (typeof g.q2 === 'number') {
                var q2 = parseInt(g.q2, 10);
                if (isNaN(q2) || q2 < 0 || q2 >= m.qubits || q2 === q) continue;
                gate.q2 = q2;
            }
            if (g.g === 'swap' && typeof gate.q2 !== 'number') continue;
            if (GATES[g.g].param) {
                gate.param = isValidParam(g.param) ? String(g.param).trim() : 'pi/2';
            }
        }
        m.gates.push(gate);
    }
    return m;
}

function occupiedMap(model) {
    // "col:q" -> gate for every row a gate blocks in its column.
    var map = {};
    model.gates.forEach(function(gate) {
        if (gate.g === 'barrier') {
            for (var q = 0; q < model.qubits; q++) map[gate.col + ':' + q] = gate;
        } else {
            gateSpan(gate).forEach(function(q) { map[gate.col + ':' + q] = gate; });
        }
    });
    return map;
}

function columnCount(model) {
    var maxCol = -1;
    model.gates.forEach(function(g) { if (g.col > maxCol) maxCol = g.col; });
    return Math.max(MIN_COLS, maxCol + 2);
}

// ---------------------------------------------------------------------------
// Code generation
// ---------------------------------------------------------------------------

function pyParam(expr) {
    // 'pi' is imported from math in the generated preamble.
    return String(expr).trim();
}

function generateCode(model) {
    var m = normalizeModel(model);
    var n = m.qubits;
    var lines = [];
    lines.push('# ⚛ Generated by the Phonos Quantum Circuit Builder.');
    lines.push('# Visual edits in the circuit panel regenerate this cell.');
    lines.push('from math import pi');
    lines.push('from qiskit import QuantumCircuit');
    lines.push('');
    lines.push('qc = QuantumCircuit(' + n + ', ' + n + ')');

    var ordered = m.gates.slice().sort(function(a, b) {
        return a.col - b.col || (a.q || 0) - (b.q || 0);
    });
    ordered.forEach(function(g) {
        switch (g.g) {
            case 'barrier': lines.push('qc.barrier()'); break;
            case 'cx': lines.push('qc.cx(' + g.ctrls[0] + ', ' + g.q + ')'); break;
            case 'cz': lines.push('qc.cz(' + g.ctrls[0] + ', ' + g.q + ')'); break;
            case 'ccx': lines.push('qc.ccx(' + g.ctrls[0] + ', ' + g.ctrls[1] + ', ' + g.q + ')'); break;
            case 'swap': lines.push('qc.swap(' + g.q + ', ' + g.q2 + ')'); break;
            case 'rx': case 'ry': case 'rz':
                lines.push('qc.' + g.g + '(' + pyParam(g.param) + ', ' + g.q + ')'); break;
            case 'p':
                lines.push('qc.p(' + pyParam(g.param) + ', ' + g.q + ')'); break;
            default:
                lines.push('qc.' + g.g + '(' + g.q + ')');
        }
    });

    if (m.measureAll) {
        lines.push('qc.measure(range(' + n + '), range(' + n + '))');
    }
    lines.push('');
    lines.push("print(qc.draw(output='text'))");
    lines.push('');
    lines.push('import phonos');
    var runArgs = 'qc, shots=' + m.shots;
    if (m.mode === 'simulator' || m.mode === 'hardware') {
        runArgs += ", mode='" + m.mode + "'";
    }
    lines.push('counts = phonos.quantum.run(' + runArgs + ')');
    lines.push('print(counts)');
    lines.push('');
    lines.push('try:');
    lines.push('    from qiskit.visualization import plot_histogram');
    lines.push('    display(plot_histogram(counts))');
    lines.push('except Exception:');
    lines.push('    pass');
    return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Builder UI
// ---------------------------------------------------------------------------

function Builder(opts) {
    this.container = opts.container;
    this.model = normalizeModel(opts.circuit);
    this.onChange = opts.onChange || function() {};
    this.tool = 'h';
    this.pending = null; // {g, col, picks:[q...]}
    this.history = [];
    this._els = {};
    this._render();
    // Do NOT fire onChange on open: opening the builder must not rewrite the
    // cell until the user actually edits the circuit.
}

Builder.prototype._snapshot = function() {
    this.history.push(JSON.stringify(this.model));
    if (this.history.length > 50) this.history.shift();
};

Builder.prototype.undo = function() {
    if (!this.history.length) return;
    this.model = normalizeModel(JSON.parse(this.history.pop()));
    this.pending = null;
    this._render();
    this._emit();
};

Builder.prototype._emit = function() {
    this.onChange(this.model, generateCode(this.model));
};

Builder.prototype._commit = function(mutator) {
    this._snapshot();
    mutator();
    this.model = normalizeModel(this.model);
    this.pending = null;
    this._render();
    this._emit();
};

Builder.prototype._promptParam = function(current) {
    var msg = 'Angle (radians). Use numbers, pi, + - * / ( ) — e.g. pi/2, 3*pi/4, 1.57';
    for (;;) {
        var v = window.prompt(msg, current || 'pi/2');
        if (v === null) return null;
        if (isValidParam(v)) return v.trim();
        msg = 'Invalid angle. Only numbers, pi and + - * / ( ) are allowed.';
    }
};

Builder.prototype._handleCellClick = function(col, q) {
    var self = this;
    var occ = occupiedMap(this.model);
    var hit = occ[col + ':' + q];

    if (this.tool === 'erase') {
        if (hit) this._commit(function() {
            self.model.gates = self.model.gates.filter(function(g) { return g !== hit; });
        });
        return;
    }

    if (hit) {
        // Clicking a placed rotation/phase gate edits its angle.
        if (GATES[hit.g] && GATES[hit.g].param && hit.q === q) {
            var p = this._promptParam(hit.param);
            if (p !== null && p !== hit.param) this._commit(function() { hit.param = p; });
        }
        return;
    }

    var spec = GATES[this.tool];
    if (!spec) return;

    if (this.tool === 'barrier') {
        this._commit(function() { self.model.gates.push({ g: 'barrier', col: col }); });
        return;
    }

    if (spec.clicks === 1) {
        var gate = { g: this.tool, col: col, q: q };
        if (spec.param) {
            var param = this._promptParam(null);
            if (param === null) return;
            gate.param = param;
        }
        this._commit(function() { self.model.gates.push(gate); });
        return;
    }

    // Multi-qubit gates: collect picks within one column.
    if (!this.pending || this.pending.g !== this.tool || this.pending.col !== col) {
        this.pending = { g: this.tool, col: col, picks: [q] };
        this._render();
        return;
    }
    if (this.pending.picks.indexOf(q) !== -1) return;
    this.pending.picks.push(q);
    if (this.pending.picks.length < spec.clicks) {
        this._render();
        return;
    }

    var picks = this.pending.picks;
    // The span (including rows crossed between endpoints) must be free.
    var lo = Math.min.apply(null, picks), hi = Math.max.apply(null, picks);
    for (var row = lo; row <= hi; row++) {
        if (occ[col + ':' + row]) { this.pending = null; this._render(); return; }
    }
    var g = { g: this.tool, col: col };
    if (this.tool === 'swap') {
        g.q = picks[0]; g.q2 = picks[1];
    } else {
        g.q = picks[picks.length - 1];
        g.ctrls = picks.slice(0, picks.length - 1);
    }
    this._commit(function() { self.model.gates.push(g); });
};

Builder.prototype._setQubits = function(n) {
    var self = this;
    n = Math.min(MAX_QUBITS, Math.max(1, n));
    if (n === this.model.qubits) return;
    this._commit(function() {
        self.model.qubits = n;
        // normalizeModel drops gates that no longer fit.
    });
};

Builder.prototype._render = function() {
    var self = this;
    var m = this.model;
    var root = this.container;
    root.innerHTML = '';

    var wrap = document.createElement('div');
    wrap.className = 'qcb';

    // --- Top controls ------------------------------------------------------
    var bar = document.createElement('div');
    bar.className = 'qcb-bar';
    bar.innerHTML =
        '<div class="qcb-bar-group">' +
            '<span class="qcb-bar-label">Qubits</span>' +
            '<button class="qcb-mini-btn" data-act="qminus" title="Remove qubit">−</button>' +
            '<span class="qcb-qubit-count">' + m.qubits + '</span>' +
            '<button class="qcb-mini-btn" data-act="qplus" title="Add qubit">+</button>' +
        '</div>' +
        '<div class="qcb-bar-group">' +
            '<span class="qcb-bar-label">Shots</span>' +
            '<input type="number" class="qcb-shots" min="1" max="100000" value="' + m.shots + '">' +
        '</div>' +
        '<div class="qcb-bar-group">' +
            '<span class="qcb-bar-label">Run on</span>' +
            '<select class="qcb-mode">' +
                '<option value="settings"' + (m.mode === 'settings' ? ' selected' : '') + '>My settings</option>' +
                '<option value="simulator"' + (m.mode === 'simulator' ? ' selected' : '') + '>Simulator</option>' +
                '<option value="hardware"' + (m.mode === 'hardware' ? ' selected' : '') + '>IBM hardware</option>' +
            '</select>' +
        '</div>' +
        '<div class="qcb-bar-group">' +
            '<label class="qcb-check"><input type="checkbox" class="qcb-measure"' + (m.measureAll ? ' checked' : '') + '> Measure all</label>' +
        '</div>' +
        '<div class="qcb-bar-group qcb-bar-right">' +
            '<button class="qcb-mini-btn" data-act="undo" title="Undo"' + (this.history.length ? '' : ' disabled') + '>↩</button>' +
            '<button class="qcb-mini-btn qcb-danger" data-act="clear" title="Clear circuit">Clear</button>' +
        '</div>';
    wrap.appendChild(bar);

    // --- Palette -----------------------------------------------------------
    var palette = document.createElement('div');
    palette.className = 'qcb-palette';
    PALETTE_GROUPS.forEach(function(group, gi) {
        if (gi > 0) {
            var sep = document.createElement('span');
            sep.className = 'qcb-palette-sep';
            palette.appendChild(sep);
        }
        group.forEach(function(key) {
            var btn = document.createElement('button');
            btn.className = 'qcb-gate-btn qcb-gate-' + key + (self.tool === key ? ' active' : '');
            btn.textContent = GATES[key].label;
            btn.title = GATES[key].name;
            btn.addEventListener('click', function() {
                self.tool = key;
                self.pending = null;
                self._render();
            });
            palette.appendChild(btn);
        });
    });
    wrap.appendChild(palette);

    // --- Hint line ---------------------------------------------------------
    var hint = document.createElement('div');
    hint.className = 'qcb-hint';
    if (this.pending) {
        var need = GATES[this.pending.g].clicks - this.pending.picks.length;
        hint.textContent = GATES[this.pending.g].label + ': pick ' + need + ' more qubit' + (need > 1 ? 's' : '') +
            ' in the same column (' + (this.pending.g === 'swap' ? 'both are swapped' : 'last pick is the target') + ')';
    } else if (this.tool === 'erase') {
        hint.textContent = 'Click a gate to remove it.';
    } else if (GATES[this.tool] && GATES[this.tool].clicks > 1) {
        hint.textContent = GATES[this.tool].name + ' — click ' + GATES[this.tool].clicks + ' qubits in one column.';
    } else {
        hint.textContent = 'Click the grid to place ' + (GATES[this.tool] ? GATES[this.tool].name : 'a gate') + '. Click a placed RX/RY/RZ/P gate to edit its angle. Double-click any gate to delete it.';
    }
    wrap.appendChild(hint);

    // --- Grid --------------------------------------------------------------
    var cols = columnCount(m);
    var occ = occupiedMap(m);
    var grid = document.createElement('div');
    grid.className = 'qcb-grid custom-scroll';

    var table = document.createElement('div');
    table.className = 'qcb-table';
    table.style.gridTemplateColumns = '44px repeat(' + cols + ', 44px)';

    // Barrier columns need full-height markers; collect them first.
    var barrierCols = {};
    m.gates.forEach(function(g) { if (g.g === 'barrier') barrierCols[g.col] = g; });

    for (var q = 0; q < m.qubits; q++) {
        var label = document.createElement('div');
        label.className = 'qcb-qlabel';
        label.textContent = 'q' + q;
        table.appendChild(label);

        for (var c = 0; c < cols; c++) {
            var cell = document.createElement('div');
            cell.className = 'qcb-cell';
            cell.dataset.col = c;
            cell.dataset.q = q;

            var gate = occ[c + ':' + q];
            var isPendingPick = this.pending && this.pending.col === c && this.pending.picks.indexOf(q) !== -1;

            if (barrierCols[c]) {
                cell.classList.add('qcb-barrier');
            } else if (gate && gate.g !== 'barrier') {
                var span = gateSpan(gate);
                var isTop = q === Math.min.apply(null, span);
                var isBottom = q === Math.max.apply(null, span);
                if (span.length > 1) {
                    cell.classList.add('qcb-linked');
                    if (isTop) cell.classList.add('qcb-link-top');
                    if (isBottom) cell.classList.add('qcb-link-bottom');
                }
                var chip = document.createElement('span');
                if (gate.g === 'swap' && (q === gate.q || q === gate.q2)) {
                    chip.className = 'qcb-swap-x';
                    chip.textContent = '✕';
                } else if (Array.isArray(gate.ctrls) && gate.ctrls.indexOf(q) !== -1) {
                    chip.className = 'qcb-ctrl-dot';
                } else if (gate.g === 'cx' && q === gate.q) {
                    chip.className = 'qcb-target-plus';
                    chip.textContent = '⊕';
                } else if (gate.g === 'cz' && q === gate.q) {
                    chip.className = 'qcb-ctrl-dot';
                } else if (q === gate.q) {
                    chip.className = 'qcb-chip qcb-chip-' + gate.g;
                    chip.textContent = GATES[gate.g].label + (gate.param ? '' : '');
                    if (gate.param) {
                        chip.title = GATES[gate.g].name + '(' + gate.param + ') — click to edit angle';
                        var sub = document.createElement('small');
                        sub.textContent = gate.param;
                        chip.appendChild(sub);
                    }
                }
                if (chip.className) cell.appendChild(chip);
            }

            if (isPendingPick) cell.classList.add('qcb-pending');

            cell.addEventListener('click', function(ev) {
                var el = ev.currentTarget;
                self._handleCellClick(parseInt(el.dataset.col, 10), parseInt(el.dataset.q, 10));
            });
            cell.addEventListener('dblclick', function(ev) {
                var el = ev.currentTarget;
                var g = occupiedMap(self.model)[el.dataset.col + ':' + el.dataset.q];
                if (g) self._commit(function() {
                    self.model.gates = self.model.gates.filter(function(x) { return x !== g; });
                });
            });
            table.appendChild(cell);
        }
    }

    grid.appendChild(table);
    wrap.appendChild(grid);

    // --- Footer note -------------------------------------------------------
    var note = document.createElement('div');
    note.className = 'qcb-note';
    note.textContent = 'Code is regenerated into the cell on every edit. Execution follows your Quantum Settings unless overridden above.';
    wrap.appendChild(note);

    root.appendChild(wrap);

    // Control wiring
    bar.querySelector('[data-act="qminus"]').addEventListener('click', function() { self._setQubits(m.qubits - 1); });
    bar.querySelector('[data-act="qplus"]').addEventListener('click', function() { self._setQubits(m.qubits + 1); });
    bar.querySelector('[data-act="undo"]').addEventListener('click', function() { self.undo(); });
    bar.querySelector('[data-act="clear"]').addEventListener('click', function() {
        if (!m.gates.length) return;
        self._commit(function() { self.model.gates = []; });
    });
    bar.querySelector('.qcb-shots').addEventListener('change', function(ev) {
        var v = parseInt(ev.target.value, 10);
        if (isNaN(v)) return;
        self._commit(function() { self.model.shots = v; });
    });
    bar.querySelector('.qcb-mode').addEventListener('change', function(ev) {
        var v = ev.target.value;
        self._commit(function() { self.model.mode = v; });
    });
    bar.querySelector('.qcb-measure').addEventListener('change', function(ev) {
        var v = !!ev.target.checked;
        self._commit(function() { self.model.measureAll = v; });
    });
};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

window.QuantumCircuitBuilder = {
    open: function(opts) { return new Builder(opts); },
    defaultModel: defaultModel,
    generateCode: generateCode,
    normalizeModel: normalizeModel
};

})();
