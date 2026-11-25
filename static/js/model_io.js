/**
 * PhonosModelIO — chain a model's output into another model's input.
 *
 * Any output file a model produces (FASTA, PDB, CIF, SDF, SMILES, A3M, TXT,
 * JSON) is classified server-side by /api/.../as-input, which also returns the
 * models that can consume it. This module renders the picker and hands the
 * chosen payload to the job wizard.
 *
 * It works from two very different places:
 *   - the job output page (a plain server-rendered page), and
 *   - the Phonos SPA (app.js), where the wizard already lives.
 * The handoff therefore goes through sessionStorage, which app.js drains when
 * it renders the target project. Same code path either way.
 *
 * Security: every node is built with createElement + textContent. No innerHTML.
 */
(function (global) {
    'use strict';

    var PREFILL_KEY = 'phonos:modelIoPrefill';
    var MAX_PREVIEW = 48;

    function t(key, fallback) {
        return (global.i18n && global.i18n.t) ? global.i18n.t('modelIo.' + key) : fallback;
    }

    function mk(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                if (key === 'class') node.className = attrs[key];
                else if (key === 'text') node.textContent = attrs[key];
                else if (key === 'style') node.style.cssText = attrs[key];
                else if (key.indexOf('data-') === 0 || key === 'title' || key === 'type') node.setAttribute(key, attrs[key]);
                else node[key] = attrs[key];
            });
        }
        (Array.isArray(children) ? children : (children ? [children] : [])).forEach(function (child) {
            if (child) node.appendChild(child);
        });
        return node;
    }

    function notify(message, kind) {
        if (global.Toast && typeof global.Toast[kind || 'info'] === 'function') {
            global.Toast[kind || 'info'](message);
        } else if (global.NotebookModule && typeof NotebookModule.showToast === 'function') {
            NotebookModule.showToast(message, 3000);
        } else {
            console.log('[ModelIO]', message);
        }
    }

    var KIND_LABELS = {
        protein_sequence: 'Protein sequence',
        nucleotide_sequence: 'DNA/RNA sequence',
        structure: 'Structure',
        ligand: 'Ligand',
        smiles: 'SMILES',
        msa: 'Alignment'
    };

    /* Task -> human grouping for the target list. Unknown tasks fall through to
       "Other models" rather than being hidden. */
    var TASK_GROUPS = [
        { key: 'fold', label: 'Predict structure', tasks: ['protein_folding', 'structure_prediction', 'enhanced_structure_prediction', 'protein_complex_folding'] },
        { key: 'design', label: 'Design sequences', tasks: ['protein_design'] },
        { key: 'dock', label: 'Dock & bind', tasks: ['molecular_docking', 'fold_and_dock', 'metal_binding_prediction'] },
        { key: 'molecule', label: 'Generate molecules', tasks: ['molecule_generation'] },
        { key: 'nucleic', label: 'DNA / RNA', tasks: ['dna_generation', 'dna_sequence_generation'] },
        { key: 'align', label: 'Alignment', tasks: ['sequence_alignment'] }
    ];

    function groupTargets(targets) {
        var groups = [];
        var used = {};
        TASK_GROUPS.forEach(function (group) {
            var members = targets.filter(function (target) {
                return group.tasks.indexOf(target.task) !== -1;
            });
            members.forEach(function (m) { used[m.id] = true; });
            if (members.length) groups.push({ label: group.label, targets: members });
        });
        var rest = targets.filter(function (target) { return !used[target.id]; });
        if (rest.length) groups.push({ label: t('otherModels', 'Other models'), targets: rest });
        return groups;
    }

    var PhonosModelIO = {
        _overlay: null,
        _state: null,

        /* ---- Entry points ------------------------------------------------ */

        /** Chain a job output. `artifactName` is the filename as listed by the job. */
        openForArtifact: function (jobId, artifactName) {
            return this._open('/api/jobs/' + encodeURIComponent(jobId) + '/artifacts/' +
                encodeURIComponent(artifactName) + '/as-input');
        },

        /** Chain a project file (an upload, or a registered job output). */
        openForFile: function (fileId) {
            return this._open('/api/files/' + encodeURIComponent(fileId) + '/as-input');
        },

        _open: function (url) {
            var self = this;
            this._renderLoading();
            return fetch(url, { credentials: 'same-origin' })
                .then(function (res) {
                    return res.json().catch(function () { return {}; }).then(function (data) {
                        if (!res.ok || data.status !== 'success') {
                            throw new Error(data.message || ('HTTP ' + res.status));
                        }
                        return data;
                    });
                })
                .then(function (data) { self._render(data); })
                .catch(function (e) {
                    self.close();
                    notify(t('classifyFailed', 'Could not inspect this file: ') + e.message, 'error');
                });
        },

        /* ---- Modal ------------------------------------------------------- */

        _ensureOverlay: function () {
            if (this._overlay && this._overlay.isConnected) return this._overlay;
            var self = this;
            var overlay = mk('div', { 'class': 'mio-overlay' });
            overlay.addEventListener('click', function (e) {
                if (e.target === overlay) self.close();
            });
            this._escHandler = function (e) {
                if (e.key === 'Escape') self.close();
            };
            document.addEventListener('keydown', this._escHandler);
            document.body.appendChild(overlay);
            this._overlay = overlay;
            return overlay;
        },

        close: function () {
            if (this._escHandler) {
                document.removeEventListener('keydown', this._escHandler);
                this._escHandler = null;
            }
            if (this._overlay && this._overlay.parentNode) this._overlay.remove();
            this._overlay = null;
            this._state = null;
        },

        _renderLoading: function () {
            var overlay = this._ensureOverlay();
            overlay.replaceChildren(mk('div', { 'class': 'mio-dialog' }, [
                mk('div', { 'class': 'mio-loading', text: t('inspecting', 'Inspecting file…') })
            ]));
        },

        _render: function (data) {
            var self = this;
            var payload = data.payload || {};
            var targets = data.targets || [];
            var source = data.source || {};

            this._state = {
                payload: payload,
                source: source,
                selectedSequences: {},
                selectedSmiles: null
            };
            // Default: everything selected, so the common "fold all designs"
            // case is one click.
            (payload.sequences || []).forEach(function (_, i) { self._state.selectedSequences[i] = true; });
            if ((payload.smiles || []).length) this._state.selectedSmiles = 0;

            var overlay = this._ensureOverlay();
            var dialog = mk('div', { 'class': 'mio-dialog' });

            // --- Header -------------------------------------------------
            var head = mk('div', { 'class': 'mio-head' });
            var titles = mk('div', { 'class': 'mio-titles' });
            titles.appendChild(mk('h3', { 'class': 'mio-title', text: t('title', 'Use as input') }));
            var subtitleParts = [source.artifact_name || source.filename || payload.filename || ''];
            if (source.produced_by) subtitleParts.push(t('producedBy', 'from ') + source.produced_by);
            titles.appendChild(mk('div', { 'class': 'mio-subtitle', text: subtitleParts.filter(Boolean).join(' · ') }));
            head.appendChild(titles);
            var closeBtn = mk('button', { 'class': 'mio-close', type: 'button', text: '×', title: t('close', 'Close') });
            closeBtn.addEventListener('click', function () { self.close(); });
            head.appendChild(closeBtn);
            dialog.appendChild(head);

            var body = mk('div', { 'class': 'mio-body' });

            // --- Detected content ---------------------------------------
            var kinds = payload.kinds || [];
            if (kinds.length) {
                var chips = mk('div', { 'class': 'mio-chips' });
                kinds.forEach(function (kind) {
                    chips.appendChild(mk('span', { 'class': 'mio-chip', text: KIND_LABELS[kind] || kind }));
                });
                body.appendChild(chips);
            }

            if (!targets.length) {
                body.appendChild(this._renderNoTargets(data));
                dialog.appendChild(body);
                overlay.replaceChildren(dialog);
                return;
            }

            // --- Payload picker -----------------------------------------
            var sequences = payload.sequences || [];
            if (sequences.length) body.appendChild(this._renderSequencePicker(sequences));

            var smiles = payload.smiles || [];
            if (smiles.length) body.appendChild(this._renderSmilesPicker(smiles));

            if (payload.ligand_codes && payload.ligand_codes.length) {
                body.appendChild(mk('div', {
                    'class': 'mio-note',
                    text: t('embeddedLigands', 'Embedded ligands: ') + payload.ligand_codes.join(', ')
                }));
            }

            // --- Targets --------------------------------------------------
            body.appendChild(mk('div', { 'class': 'mio-section-title', text: t('sendTo', 'Send to') }));
            groupTargets(targets).forEach(function (group) {
                body.appendChild(mk('div', { 'class': 'mio-group-label', text: group.label }));
                var grid = mk('div', { 'class': 'mio-target-grid' });
                group.targets.forEach(function (target) {
                    grid.appendChild(self._renderTarget(target));
                });
                body.appendChild(grid);
            });

            var unavailable = data.unavailable_targets || [];
            if (unavailable.length) {
                body.appendChild(mk('div', {
                    'class': 'mio-note',
                    text: t('offlineModels', 'Currently offline: ') +
                          unavailable.map(function (m) { return m.short_name || m.name; }).join(', ')
                }));
            }

            dialog.appendChild(body);
            overlay.replaceChildren(dialog);
        },

        _renderNoTargets: function (data) {
            var wrap = mk('div', { 'class': 'mio-empty' });
            wrap.appendChild(mk('div', {
                'class': 'mio-empty-title',
                text: t('noTargets', 'No model can take this file directly')
            }));
            wrap.appendChild(mk('div', {
                'class': 'mio-empty-hint',
                text: t('noTargetsHint', 'Phonos could not read a sequence, structure or molecule out of it. You can still download it and feed it in manually.')
            }));
            var url = (data.source || {}).download_url;
            if (url) {
                var link = mk('a', { 'class': 'mio-btn', text: t('download', 'Download file') });
                link.href = url;
                link.setAttribute('download', '');
                wrap.appendChild(link);
            }
            return wrap;
        },

        _renderSequencePicker: function (sequences) {
            var self = this;
            var section = mk('div', { 'class': 'mio-section' });

            var header = mk('div', { 'class': 'mio-section-head' });
            header.appendChild(mk('span', {
                'class': 'mio-section-title',
                text: t('sequences', 'Sequences') + ' (' + sequences.length + ')'
            }));
            if (sequences.length > 1) {
                var toggleAll = mk('button', { 'class': 'mio-link', type: 'button', text: t('toggleAll', 'Toggle all') });
                toggleAll.addEventListener('click', function () {
                    var anyOff = sequences.some(function (_, i) { return !self._state.selectedSequences[i]; });
                    sequences.forEach(function (_, i) { self._state.selectedSequences[i] = anyOff; });
                    section.querySelectorAll('input[type="checkbox"]').forEach(function (cb, i) {
                        cb.checked = !!self._state.selectedSequences[i];
                    });
                    self._updateSelectionSummary();
                });
                header.appendChild(toggleAll);
            }
            section.appendChild(header);

            var list = mk('div', { 'class': 'mio-list' });
            sequences.forEach(function (record, index) {
                var row = mk('label', { 'class': 'mio-row' });
                var cb = mk('input', { type: 'checkbox' });
                cb.checked = true;
                cb.addEventListener('change', function () {
                    self._state.selectedSequences[index] = cb.checked;
                    self._updateSelectionSummary();
                });
                row.appendChild(cb);

                var info = mk('div', { 'class': 'mio-row-info' });
                info.appendChild(mk('span', { 'class': 'mio-row-name', text: record.name || ('sequence ' + (index + 1)) }));
                var preview = record.sequence.length > MAX_PREVIEW
                    ? record.sequence.slice(0, MAX_PREVIEW) + '…'
                    : record.sequence;
                info.appendChild(mk('span', { 'class': 'mio-row-seq', text: preview }));
                info.appendChild(mk('span', {
                    'class': 'mio-row-meta',
                    text: record.length + ' aa/nt · ' + (KIND_LABELS[record.kind] || record.kind)
                }));
                row.appendChild(info);

                var copy = mk('button', { 'class': 'mio-copy', type: 'button', text: t('copy', 'Copy'), title: t('copySequence', 'Copy this sequence') });
                copy.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    self._copy(record.sequence);
                });
                row.appendChild(copy);
                list.appendChild(row);
            });
            section.appendChild(list);
            section.appendChild(mk('div', { 'class': 'mio-summary', id: 'mio-selection-summary' }));
            return section;
        },

        _renderSmilesPicker: function (smiles) {
            var self = this;
            var section = mk('div', { 'class': 'mio-section' });
            section.appendChild(mk('div', { 'class': 'mio-section-title', text: t('molecules', 'Molecules') + ' (' + smiles.length + ')' }));
            var list = mk('div', { 'class': 'mio-list' });
            smiles.forEach(function (record, index) {
                var row = mk('label', { 'class': 'mio-row' });
                var radio = mk('input', { type: 'radio' });
                radio.name = 'mio-smiles';
                radio.checked = index === 0;
                radio.addEventListener('change', function () { self._state.selectedSmiles = index; });
                row.appendChild(radio);
                var info = mk('div', { 'class': 'mio-row-info' });
                info.appendChild(mk('span', { 'class': 'mio-row-name', text: record.name || ('molecule ' + (index + 1)) }));
                info.appendChild(mk('span', { 'class': 'mio-row-seq', text: record.smiles }));
                row.appendChild(info);
                var copy = mk('button', { 'class': 'mio-copy', type: 'button', text: t('copy', 'Copy') });
                copy.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    self._copy(record.smiles);
                });
                row.appendChild(copy);
                list.appendChild(row);
            });
            section.appendChild(list);
            return section;
        },

        _renderTarget: function (target) {
            var self = this;
            var btn = mk('button', { 'class': 'mio-target', type: 'button' });
            btn.appendChild(mk('span', { 'class': 'mio-target-name', text: target.short_name || target.name }));
            var receives = (target.matched_kinds || []).map(function (k) { return KIND_LABELS[k] || k; }).join(' + ');
            btn.appendChild(mk('span', { 'class': 'mio-target-meta', text: t('receives', 'receives ') + receives }));
            btn.title = target.name;
            btn.addEventListener('click', function () { self._handoff(target); });
            return btn;
        },

        _updateSelectionSummary: function () {
            var el = document.getElementById('mio-selection-summary');
            if (!el || !this._state) return;
            var count = Object.keys(this._state.selectedSequences).filter(function (k) {
                return this._state.selectedSequences[k];
            }, this).length;
            el.textContent = count
                ? (count + ' ' + t('selected', 'selected'))
                : t('noneSelected', 'Nothing selected — pick at least one');
        },

        _copy: function (text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function () {
                    notify(t('copied', 'Copied to clipboard'), 'success');
                }, function () {
                    notify(t('copyFailed', 'Could not copy'), 'error');
                });
            } else {
                notify(t('copyFailed', 'Could not copy'), 'error');
            }
        },

        /* ---- Handoff ----------------------------------------------------- */

        _selectedSequences: function () {
            var state = this._state;
            var all = state.payload.sequences || [];
            return all.filter(function (_, i) { return state.selectedSequences[i]; });
        },

        _handoff: function (target) {
            var state = this._state;
            if (!state) return;
            var source = state.source || {};
            var projectId = source.project_id;
            if (!projectId) {
                notify(t('noProject', 'Could not determine which project this file belongs to'), 'error');
                return;
            }

            var delivery = target.delivery;
            var prefill = {
                targetModel: target.id,
                projectId: projectId,
                source: source,
                createdAt: Date.now()
            };

            if (delivery === 'sequence') {
                var picked = this._selectedSequences();
                if (!picked.length) {
                    notify(t('noneSelected', 'Nothing selected — pick at least one'), 'warning');
                    return;
                }
                // The target may only accept one of the two sequence kinds
                // (Evo2 takes nucleotides, ESMFold takes protein) — drop the rest
                // rather than submitting something that will fail validation.
                var accepted = picked.filter(function (record) {
                    return (target.accepts || []).indexOf(record.kind) !== -1;
                });
                if (!accepted.length) {
                    notify(t('kindMismatch', 'The selected sequences are not the type this model accepts'), 'warning');
                    return;
                }
                prefill.sequences = accepted.map(function (record) {
                    return { name: record.name, sequence: record.sequence };
                });
            } else if (delivery === 'smiles') {
                var molecules = state.payload.smiles || [];
                var chosen = molecules[state.selectedSmiles || 0];
                if (!chosen) {
                    notify(t('noMolecule', 'No molecule selected'), 'warning');
                    return;
                }
                prefill.smiles = chosen.smiles;
                prefill.sequences = [];
            } else {
                // File-delivered payloads (structures, ligand files): the wizard
                // picks the file from the project, so pass the name as a hint and
                // let the user confirm.
                prefill.fileHint = source.artifact_name || source.filename || state.payload.filename;
                prefill.downloadUrl = source.download_url;
                prefill.sequences = [];
            }

            try {
                sessionStorage.setItem(PREFILL_KEY, JSON.stringify(prefill));
                sessionStorage.setItem('selectedProject', String(projectId));
            } catch (e) {
                notify(t('handoffFailed', 'Could not carry the input over'), 'error');
                return;
            }

            this.close();

            // Inside the SPA we can jump straight to the project; from a
            // server-rendered page we navigate and let app.js drain the handoff.
            if (global.Views && typeof Views.load === 'function' && global.App) {
                if (typeof App.consumeModelIoPrefill === 'function') App.consumeModelIoPrefill();
                Views.load('project');
            } else {
                global.location.href = '/dashboard#project';
            }
        },

        /** Read (and clear) a pending handoff. Used by app.js. */
        takePrefill: function (projectId) {
            var raw;
            try { raw = sessionStorage.getItem(PREFILL_KEY); } catch (e) { return null; }
            if (!raw) return null;
            var data;
            try { data = JSON.parse(raw); } catch (e) {
                try { sessionStorage.removeItem(PREFILL_KEY); } catch (e2) {}
                return null;
            }
            // A handoff is a one-shot intent; anything older than 10 minutes is
            // a stale tab, not something the user is still waiting on.
            if (!data || (Date.now() - (data.createdAt || 0)) > 10 * 60 * 1000) {
                try { sessionStorage.removeItem(PREFILL_KEY); } catch (e) {}
                return null;
            }
            if (projectId !== undefined && String(data.projectId) !== String(projectId)) return null;
            try { sessionStorage.removeItem(PREFILL_KEY); } catch (e) {}
            return data;
        }
    };

    /* ---- Delegated triggers -------------------------------------------- *
     * Markup opts in with data attributes rather than an inline onclick, so a
     * filename never has to survive a trip through a JS string literal inside
     * an HTML attribute:
     *
     *   <button class="mio-trigger" data-mio-job="..." data-mio-artifact="...">
     *   <button class="mio-trigger" data-mio-file="123">
     *
     * Registered on the CAPTURE phase: several of the rows these buttons live
     * in sit inside a container with onclick="event.stopPropagation()", which
     * would swallow the click before it ever bubbled up to document.
     */
    document.addEventListener('click', function (event) {
        var target = event.target;
        var trigger = (target && target.closest)
            ? target.closest('.mio-trigger[data-mio-job], .mio-trigger[data-mio-file]')
            : null;
        if (!trigger) return;
        event.preventDefault();
        event.stopPropagation();
        if (trigger.dataset.mioFile) {
            PhonosModelIO.openForFile(trigger.dataset.mioFile);
        } else if (trigger.dataset.mioJob && trigger.dataset.mioArtifact) {
            PhonosModelIO.openForArtifact(trigger.dataset.mioJob, trigger.dataset.mioArtifact);
        }
    }, true);

    global.PhonosModelIO = PhonosModelIO;
})(window);
