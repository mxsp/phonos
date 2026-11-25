/**
 * multimer-batch.js
 * -----------------------------------------------------------------------------
 * Shared multimer batch builder used by the OpenFold3 and Boltz2 pages.
 *
 * Introduces a `Structure` class that sits ABOVE chains: a single Structure is
 * one named complex made up of one or more chains (Chain A, Chain B, ...).  A
 * batch is simply a list of Structures, where each Structure becomes one
 * multimer prediction job submitted through /api/batch/jobs.
 *
 *     Batch
 *       └─ Structure 1 ("Complex AB")
 *            ├─ Chain A (protein, SEQ...)
 *            └─ Chain B (protein, SEQ...)
 *       └─ Structure 2 ("Complex CD")
 *            ├─ Chain A ...
 *            └─ Chain B ...
 *
 * The module is dependency-free and exposes `Structure` and `MultimerBatch` as
 * globals so the existing plain-script pages can use them directly.
 */
(function (global) {
    'use strict';

    var CHAIN_COLORS = ['#0d9488', '#8b5cf6', '#f59e0b', '#ef4444', '#3b82f6', '#ec4899',
                        '#06b6d4', '#f97316', '#84cc16', '#a855f7', '#14b8a6', '#e11d48'];

    var MOLECULE_TYPES = ['protein', 'dna', 'rna'];

    function chainLabel(index) {
        // A, B, ... Z, then AA, AB, ... (handles >26 chains gracefully)
        var label = '';
        index += 1;
        while (index > 0) {
            var rem = (index - 1) % 26;
            label = String.fromCharCode(65 + rem) + label;
            index = Math.floor((index - 1) / 26);
        }
        return label;
    }

    function uid(prefix) {
        return (prefix || 'id') + '-' + Date.now().toString(36) + '-' +
            Math.random().toString(36).slice(2, 7);
    }

    function defaultParseSequence(text) {
        if (!text) return '';
        var lines = String(text).split('\n');
        var seq = '';
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i].trim();
            if (!line || line.charAt(0) === '>' || line.charAt(0) === ';' || line.charAt(0) === '#') {
                continue;
            }
            seq += line.replace(/[^A-Za-z]/g, '').toUpperCase();
        }
        return seq;
    }

    // ========================================================================
    // Structure: a named container of chains (one multimer complex / one job)
    // ========================================================================
    function Structure(opts) {
        opts = opts || {};
        this.id = opts.id || uid('struct');
        this.name = opts.name || 'Structure';
        this.chains = [];
        var initial = opts.chains;
        if (initial && initial.length) {
            for (var i = 0; i < initial.length; i++) this.addChain(initial[i]);
        }
    }

    Structure.prototype.addChain = function (chain) {
        chain = chain || {};
        this.chains.push({
            id: chain.id || chain.chain_id || chainLabel(this.chains.length),
            moleculeType: (chain.moleculeType || chain.molecule_type || 'protein').toLowerCase(),
            sequence: chain.sequence || ''
        });
        return this.chains[this.chains.length - 1];
    };

    Structure.prototype.removeChain = function (chainId) {
        this.chains = this.chains.filter(function (c) { return c.id !== chainId; });
        this._relabel();
    };

    Structure.prototype._relabel = function () {
        for (var i = 0; i < this.chains.length; i++) {
            this.chains[i].id = chainLabel(i);
        }
    };

    /** Convert to the per-item payload understood by /api/batch/jobs. */
    Structure.prototype.toItem = function (parseSequence) {
        var parse = parseSequence || defaultParseSequence;
        return {
            name: this.name,
            chains: this.chains.map(function (c) {
                return {
                    molecule_type: c.moleculeType,
                    sequence: parse(c.sequence),
                    chain_id: c.id
                };
            })
        };
    };

    // ========================================================================
    // MultimerBatch: controller for a list of Structures + its editor UI
    // ========================================================================
    function MultimerBatch(opts) {
        opts = opts || {};
        this.model = opts.model;                                  // job model id, e.g. 'type-9' / 'openfold3'
        this.batchName = opts.batchName || 'Batch';
        this.containerId = opts.containerId || 'batch-items-container';
        this.maxStructures = opts.maxStructures || 50;
        this.maxChains = opts.maxChains || 12;
        this.moleculeTypes = opts.moleculeTypes || MOLECULE_TYPES;
        this.parseSequence = opts.parseSequence || defaultParseSequence;
        this.onError = opts.onError || function () {};
        this.onSuccess = opts.onSuccess || function () {};
        this.structures = [];
    }

    MultimerBatch.prototype.count = function () { return this.structures.length; };

    MultimerBatch.prototype._find = function (id) {
        for (var i = 0; i < this.structures.length; i++) {
            if (this.structures[i].id === id) return this.structures[i];
        }
        return null;
    };

    MultimerBatch.prototype.addStructure = function (name, chains) {
        if (this.structures.length >= this.maxStructures) {
            this.onError('Maximum ' + this.maxStructures + ' structures per batch');
            return null;
        }
        var s = new Structure({
            name: name || ('Structure ' + (this.structures.length + 1)),
            chains: chains || [
                { id: 'A', moleculeType: 'protein', sequence: '' },
                { id: 'B', moleculeType: 'protein', sequence: '' }
            ]
        });
        this.structures.push(s);
        this.render();
        return s;
    };

    MultimerBatch.prototype.removeStructure = function (id) {
        this.structures = this.structures.filter(function (s) { return s.id !== id; });
        this.render();
    };

    MultimerBatch.prototype.addChainTo = function (structureId) {
        var s = this._find(structureId);
        if (!s) return;
        if (s.chains.length >= this.maxChains) {
            this.onError('Maximum ' + this.maxChains + ' chains per structure');
            return;
        }
        s.addChain();
        this.render();
    };

    MultimerBatch.prototype.removeChainFrom = function (structureId, chainId) {
        var s = this._find(structureId);
        if (!s) return;
        if (s.chains.length <= 1) {
            this.onError('A structure needs at least one chain');
            return;
        }
        s.removeChain(chainId);
        this.render();
    };

    MultimerBatch.prototype.clear = function () {
        this.structures = [];
        this.render();
    };

    // ---- File import (Multi-FASTA or CSV) ----------------------------------
    MultimerBatch.prototype.loadFile = function (file) {
        if (!file) return;
        var self = this;
        var reader = new FileReader();
        reader.onload = function (e) {
            var content = e.target.result;
            var name = (file.name || '').toLowerCase();
            var loaded = name.endsWith('.csv') ? self._parseCSV(content) : self._parseFASTA(content);
            if (loaded > 0) {
                self.render();
                self.onSuccess('Loaded ' + loaded + ' structure(s) from ' + file.name);
            } else {
                self.onError('No structures found in ' + file.name);
            }
        };
        reader.readAsText(file);
    };

    /**
     * Multi-FASTA: each `>header` starts a new chain. A blank header line, or a
     * header containing "structure"/"complex", can be used to separate
     * structures; otherwise consecutive records are grouped 2-per-structure is
     * NOT assumed — instead each record becomes its own single-chain structure
     * unless headers share a name prefix before a "|" or ":" delimiter.
     */
    MultimerBatch.prototype._parseFASTA = function (content) {
        var records = [];
        var current = null;
        var lines = String(content).split('\n');
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i].trim();
            if (line.charAt(0) === '>') {
                current = { header: line.substring(1).trim(), seq: '' };
                records.push(current);
            } else if (current && line) {
                current.seq += line;
            }
        }
        if (!records.length) return 0;

        // Group records into structures by the part of the header before a
        // "|" / ":" delimiter (so ">cplx1|A" and ">cplx1|B" form one complex).
        var groups = {};
        var order = [];
        for (var r = 0; r < records.length; r++) {
            var header = records[r].header || ('Structure ' + (r + 1));
            var sep = header.search(/[|:]/);
            var groupKey = sep > -1 ? header.slice(0, sep).trim() : header;
            if (!groups[groupKey]) { groups[groupKey] = []; order.push(groupKey); }
            groups[groupKey].push(records[r]);
        }

        this.structures = [];
        for (var g = 0; g < order.length; g++) {
            var key = order[g];
            var recs = groups[key];
            var chains = recs.map(function (rec) {
                return { moleculeType: 'protein', sequence: this.parseSequence(rec.seq) };
            }, this);
            this.structures.push(new Structure({ name: key, chains: chains }));
        }
        return this.structures.length;
    };

    /** CSV: `name,chainA,chainB,...` per line (header row optional). */
    MultimerBatch.prototype._parseCSV = function (content) {
        var lines = String(content).split('\n');
        var parsed = [];
        for (var i = 0; i < lines.length; i++) {
            var raw = lines[i].trim();
            if (!raw) continue;
            var parts = raw.split(',');
            if (parts.length < 2) continue;
            // Skip an obvious header row
            if (i === 0 && /name/i.test(parts[0]) && /chain|seq/i.test(parts[1])) continue;
            var name = parts[0].trim() || ('Structure ' + (parsed.length + 1));
            var chains = [];
            for (var c = 1; c < parts.length; c++) {
                var seq = parts[c].trim();
                if (seq) chains.push({ moleculeType: 'protein', sequence: seq });
            }
            if (chains.length) parsed.push(new Structure({ name: name, chains: chains }));
        }
        if (!parsed.length) return 0;
        this.structures = parsed;
        return parsed.length;
    };

    // ---- Rendering ----------------------------------------------------------
    MultimerBatch.prototype.render = function () {
        var container = document.getElementById(this.containerId);
        if (!container) return;
        container.innerHTML = '';
        var self = this;

        this.structures.forEach(function (structure, sIdx) {
            var card = document.createElement('div');
            card.className = 'chain-entry';
            card.style.border = '1px solid #27272a';
            card.style.marginBottom = '16px';
            card.dataset.structureId = structure.id;

            // Header: editable name + remove
            var header = document.createElement('div');
            header.className = 'chain-entry-header';
            header.style.background = '#18181b';

            var title = document.createElement('input');
            title.type = 'text';
            title.value = structure.name;
            title.title = 'Structure name';
            title.style.cssText = 'background: transparent; border: none; color: #fafafa; font-size: 14px; font-weight: 600; width: 220px;';
            title.addEventListener('change', function (e) { structure.name = e.target.value; });

            var right = document.createElement('div');
            right.style.cssText = 'display: flex; gap: 8px; align-items: center;';

            var count = document.createElement('span');
            count.textContent = structure.chains.length + ' chain' + (structure.chains.length === 1 ? '' : 's');
            count.style.cssText = 'font-size: 11px; color: #71717a;';
            right.appendChild(count);

            if (self.structures.length > 1) {
                var removeStruct = document.createElement('button');
                removeStruct.type = 'button';
                removeStruct.className = 'chain-remove-btn';
                removeStruct.title = 'Remove structure';
                removeStruct.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
                removeStruct.addEventListener('click', function () { self.removeStructure(structure.id); });
                right.appendChild(removeStruct);
            }

            header.appendChild(title);
            header.appendChild(right);

            // Body: chain rows
            var body = document.createElement('div');
            body.style.padding = '12px';

            structure.chains.forEach(function (chain, cIdx) {
                var row = document.createElement('div');
                row.style.cssText = 'display: flex; gap: 8px; margin-bottom: 8px; align-items: flex-start;';

                var badge = document.createElement('span');
                badge.className = 'chain-id-badge';
                badge.style.background = CHAIN_COLORS[cIdx % CHAIN_COLORS.length];
                badge.style.marginTop = '12px';
                badge.textContent = chain.id;

                var textarea = document.createElement('textarea');
                textarea.className = 'form-textarea';
                textarea.style.cssText = 'min-height: 40px; height: 40px; font-size: 12px; padding: 8px; flex: 1;';
                textarea.value = chain.sequence;
                textarea.placeholder = 'Chain ' + chain.id + ' sequence...';
                textarea.addEventListener('input', function (e) { chain.sequence = e.target.value; });

                var typeSel = document.createElement('select');
                typeSel.className = 'chain-type-select';
                typeSel.style.marginTop = '4px';
                self.moleculeTypes.forEach(function (t) {
                    var opt = document.createElement('option');
                    opt.value = t;
                    opt.textContent = t.charAt(0).toUpperCase() + t.slice(1);
                    if (chain.moleculeType === t) opt.selected = true;
                    typeSel.appendChild(opt);
                });
                typeSel.addEventListener('change', function (e) { chain.moleculeType = e.target.value; });

                row.appendChild(badge);
                row.appendChild(textarea);
                row.appendChild(typeSel);

                if (structure.chains.length > 1) {
                    var rmChain = document.createElement('button');
                    rmChain.type = 'button';
                    rmChain.className = 'chain-remove-btn';
                    rmChain.title = 'Remove chain';
                    rmChain.style.marginTop = '4px';
                    rmChain.innerHTML = '&times;';
                    rmChain.addEventListener('click', function () {
                        self.removeChainFrom(structure.id, chain.id);
                    });
                    row.appendChild(rmChain);
                }

                body.appendChild(row);
            });

            var addChain = document.createElement('button');
            addChain.type = 'button';
            addChain.className = 'add-chain-btn';
            addChain.style.cssText = 'padding: 4px 8px; font-size: 11px;';
            addChain.innerHTML = '+ Add Chain';
            addChain.addEventListener('click', function () { self.addChainTo(structure.id); });

            body.appendChild(addChain);
            card.appendChild(header);
            card.appendChild(body);
            container.appendChild(card);
        });
    };

    // ---- Validation + payload ----------------------------------------------
    /** Returns { ok: bool, error: string|null, items: [...] }. */
    MultimerBatch.prototype.validate = function () {
        if (!this.structures.length) {
            return { ok: false, error: 'No structures defined for batch', items: [] };
        }
        var items = [];
        for (var s = 0; s < this.structures.length; s++) {
            var structure = this.structures[s];
            var item = structure.toItem(this.parseSequence);
            if (!item.chains.length) {
                return { ok: false, error: 'Structure "' + structure.name + '" has no chains', items: [] };
            }
            for (var c = 0; c < item.chains.length; c++) {
                if (!item.chains[c].sequence) {
                    return {
                        ok: false,
                        error: 'Structure "' + structure.name + '", chain ' + item.chains[c].chain_id + ': missing sequence',
                        items: []
                    };
                }
            }
            items.push(item);
        }
        return { ok: true, error: null, items: items };
    };

    /**
     * Build the full /api/batch/jobs payload.
     * @param {object} config  shared per-job config (ligand, sampling, ...)
     * @param {number} projectId
     * @returns {object|null}  payload, or null if validation failed (onError fired)
     */
    MultimerBatch.prototype.toPayload = function (config, projectId) {
        var result = this.validate();
        if (!result.ok) {
            this.onError(result.error);
            return null;
        }
        return {
            project_id: projectId,
            model: this.model,
            batch_name: this.batchName + ' ' + new Date().toLocaleString(),
            items: result.items,
            config: config || {}
        };
    };

    // Expose
    global.Structure = Structure;
    global.MultimerBatch = MultimerBatch;
    global.MULTIMER_CHAIN_COLORS = CHAIN_COLORS;

})(typeof window !== 'undefined' ? window : this);
