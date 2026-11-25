/* Phonos Copilot — what the user is looking at.
 *
 * Pages report a deliberately small slice of the screen: the active view, the
 * open project/job/file, a selection, visible filters and a few short facts.
 * No table contents, no file contents, no secrets. The server sanitises this
 * again and gives it to the model as data, never as instructions.
 *
 * The module is intentionally dependency-free: it can wrap the existing SPA
 * (dashboard) and also work on the standalone /agent page, where it reads the
 * handoff from the query string.
 */
(function () {
    'use strict';

    const MAX_TEXT = 160;
    const MAX_FACTS = 10;
    const MAX_SELECTION = 12;
    const LIMIT_VERSION = 1;
    const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content
        || (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
    let lastViewerSignature = '';
    let viewerAttach = localStorage.getItem('phonos-agent-viewer-attach') !== '0';

    function clip(value, limit) {
        return String(value == null ? '' : value)
            .replace(/\s+/g, ' ')
            .trim()
            .slice(0, limit || MAX_TEXT);
    }

    function numberOrNull(value) {
        if (value == null || value === '') return null;
        const n = Number(value);
        return Number.isFinite(n) ? n : null;
    }

    function isVisible(el) {
        if (!el || !el.getBoundingClientRect) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = window.getComputedStyle(el);
        return style.visibility !== 'hidden' && style.display !== 'none';
    }

    function route() {
        if (window.Views && window.Views.current) return window.Views.current;
        const body = document.body;
        if (body && body.dataset && body.dataset.page) return body.dataset.page;
        return 'dashboard';
    }

    function pageTitle() {
        const title = document.getElementById('page-title');
        if (title && title.textContent.trim()) return clip(title.textContent, 120);
        const h1 = document.querySelector('main h1, .content-area h1, .page-header h1');
        if (h1 && h1.textContent.trim()) return clip(h1.textContent, 120);
        return clip(document.title.replace(/·.*$/, ''), 120) || route();
    }

    function breadcrumb() {
        const active = document.querySelector('.breadcrumb-current, .breadcrumb-item.active');
        return active ? clip(active.textContent, 80) : '';
    }

    function projectEntity() {
        const detail = document.querySelector('.project-detail[data-project-id], [data-project-workspace][data-project-id]');
        let id = detail ? numberOrNull(detail.dataset.projectId) : null;
        if (id == null) id = numberOrNull(sessionStorage.getItem('selectedProject'));
        if (id == null) return null;
        const nameEl = document.querySelector('.project-title, .project-header-title, .pw-project-name');
        return {
            type: 'project',
            id: id,
            label: nameEl ? clip(nameEl.textContent, 100) : undefined,
        };
    }

    function jobEntity() {
        const modal = document.querySelector('.job-details-modal, [data-job-details-modal]');
        if (!modal) return null;
        const value = modal.querySelector('.job-id-value, [data-job-id]');
        let id = value ? (value.dataset && value.dataset.jobId ? value.dataset.jobId : clip(value.textContent, 80)) : null;
        if (!id) return null;
        const nameEl = modal.querySelector('.job-details-title, .job-name');
        return {
            type: 'job',
            id: id,
            label: nameEl ? clip(nameEl.textContent, 100) : undefined,
        };
    }

    function fileEntity() {
        const preview = window.App && window.App._currentPreviewFile;
        if (!preview) return null;
        const id = preview.id != null ? preview.id : (preview.fileId != null ? preview.fileId : null);
        if (id == null) return null;
        return {
            type: 'file',
            id: id,
            label: clip(preview.filename || preview.name || '', 100) || undefined,
        };
    }

    function openEntity() {
        return jobEntity() || fileEntity() || projectEntity();
    }

    function selection() {
        const out = [];
        const seen = new Set();
        const add = function (type, id, label) {
            if (id == null || out.length >= MAX_SELECTION) return;
            const key = type + ':' + id;
            if (seen.has(key)) return;
            seen.add(key);
            out.push({ type: type, id: id, label: label || undefined });
        };
        const checked = document.querySelectorAll(
            '#content-area input[type="checkbox"]:checked, .file-manager input[type="checkbox"]:checked'
        );
        checked.forEach(function (box) {
            const row = box.closest('tr[data-job-id], [data-file-id], [data-project-id]');
            if (!row) return;
            if (row.dataset.jobId) add('job', row.dataset.jobId, row.dataset.name || '');
            else if (row.dataset.fileId) add('file', numberOrNull(row.dataset.fileId), '');
            else if (row.dataset.projectId) add('project', numberOrNull(row.dataset.projectId), '');
        });
        document.querySelectorAll(
            '#content-area tr.selected[data-job-id], #content-area .selected[data-file-id], #content-area .selected[data-project-id]'
        ).forEach(function (el) {
            if (el.dataset.jobId) add('job', el.dataset.jobId, el.dataset.name || '');
            else if (el.dataset.fileId) add('file', numberOrNull(el.dataset.fileId), '');
            else if (el.dataset.projectId) add('project', numberOrNull(el.dataset.projectId), '');
        });
        return out;
    }

    function settings() {
        const out = {};
        const activeTab = document.querySelector('.tab-v2.active, .tabs .active, [role="tab"][aria-selected="true"]');
        if (activeTab && activeTab.textContent.trim()) out.tab = clip(activeTab.textContent, 60);
        const inputs = document.querySelectorAll(
            '#content-area select, #content-area input[type="search"], #content-area input[type="text"]'
        );
        let count = 0;
        inputs.forEach(function (input) {
            if (count >= 8 || !isVisible(input)) return;
            const value = clip(input.value, 60);
            if (!value) return;
            const key = clip(input.id || input.name || '', 40) || ('field' + count);
            out[key] = value;
            count += 1;
        });
        const countBadge = document.querySelector('.results-count, .pagination-info');
        if (countBadge && countBadge.textContent.trim()) out.visible = clip(countBadge.textContent, 60);
        return out;
    }

    function facts() {
        const out = [];
        document.querySelectorAll('.dash-v4-stat-card').forEach(function (card) {
            if (out.length >= 6 || !isVisible(card)) return;
            const title = card.querySelector('.dash-v4-stat-title');
            const value = card.querySelector('.dash-v4-stat-value');
            if (title && value) out.push(clip(title.textContent, 60) + ': ' + clip(value.textContent, 60));
        });
        const crumb = breadcrumb();
        if (crumb) out.push('Breadcrumb: ' + crumb);
        const rows = document.querySelectorAll('#content-area tbody tr, #content-area .project-card-v2, #content-area .job-card');
        if (rows.length) out.push(rows.length + ' row(s) visible');
        const empty = document.querySelector('#content-area .empty-state-title');
        if (empty && empty.textContent.trim()) out.push('Empty state: ' + clip(empty.textContent, 80));
        return out.slice(0, MAX_FACTS);
    }

    const SUGGESTIONS = {
        dashboard: [
            { title: 'What needs attention?', text: 'What needs my attention right now? Check running jobs, failed jobs and low credits.' },
            { title: 'Explain my workspace', text: 'Summarize my projects, recent jobs and credit usage.' },
        ],
        jobs: [
            { title: 'Running jobs', text: 'Which jobs are running or queued right now?' },
            { title: 'Recent failures', text: 'Show my recent failed jobs and explain the likely cause.' },
        ],
        projects: [
            { title: 'Summarize projects', text: 'Which of my projects have activity in the last 30 days, and what are they about?' },
            { title: 'This project', text: 'Summarize the project I have open, including job counts and latest activity.' },
        ],
        files: [
            { title: 'Find a file', text: 'Help me find a file. Ask me for a name or project if it is unclear.' },
            { title: 'This file', text: 'What is the file I have open, and which job produced it?' },
        ],
        'submit-job': [
            { title: 'Choose a model', text: 'I want to predict a protein structure. Which active model should I use and what input does it need?' },
            { title: 'Prepare a run', text: 'Help me prepare a job. Ask for the project, model and sequence before calling prepare_job.' },
        ],
        usage: [
            { title: 'Credit forecast', text: 'How many credits have I used in the last 30 days, and what does the trend look like?' },
        ],
        settings: [
            { title: 'Provider check', text: 'Is the assistant provider configured correctly? Summarize the endpoint and model without revealing the key.' },
        ],
    };

    function suggestions() {
        const list = SUGGESTIONS[route()] || [
            { title: 'What can you do?', text: 'What can you help me with in Phonos?' },
        ];
        return list.slice(0, 4);
    }

    function fromQuery() {
        const params = new URLSearchParams(window.location.search);
        const entityType = params.get('entity') || '';
        const entityId = params.get('id');
        const projectId = params.get('project');
        const context = {
            route: clip(params.get('from') || 'agent', 120),
            title: clip(params.get('title') || 'Phonos Copilot', 140),
        };
        if (entityType && entityId) {
            context.entity = { type: clip(entityType, 40), id: numberOrNull(entityId) != null ? numberOrNull(entityId) : clip(entityId, 80) };
        } else if (projectId) {
            context.entity = { type: 'project', id: numberOrNull(projectId) };
        }
        const job = params.get('job');
        if (job) context.entity = { type: 'job', id: clip(job, 80) };
        const file = params.get('file');
        if (file) context.entity = { type: 'file', id: numberOrNull(file) != null ? numberOrNull(file) : clip(file, 80) };
        return context;
    }


    // ---- live 3D / sequence snapshot ------------------------------------
    function extractViewer() {
        const app = window.App;
        if (!app) return null;
        const candidates = [
            window.Views && window.Views.molecularVisualizerState,
            app.molecularVisualizerState,
            app.projectVisState,
        ];
        let state = null;
        for (const candidate of candidates) {
            if (candidate && (candidate.currentStructureText || candidate.currentAnalysisData)) {
                state = candidate;
                break;
            }
        }
        if (!state && candidates[0]) state = candidates[0];
        if (!state) return null;
        const text = String(state.currentStructureText || '');
        const chains = [];
        try {
            const vis = window.Views || app;
            const built = typeof vis.buildMolVisSequenceData === 'function' ? vis.buildMolVisSequenceData() : [];
            (built || []).forEach(function (chain) {
                const sequence = (chain.residues || []).map(function (residue) { return residue.aa || 'X'; }).join('');
                if (sequence) chains.push({ chain: chain.chain || 'A', sequence: sequence });
            });
        } catch (err) { /* sequence extraction is best-effort */ }
        const analysis = state.currentAnalysisData && typeof state.currentAnalysisData === 'object'
            ? {
                residueCount: state.currentAnalysisData.residueCount,
                atomCount: state.currentAnalysisData.atomCount,
                ligands: (state.currentAnalysisData.ligands || []).slice(0, 30).map(function (ligand) {
                    return typeof ligand === 'string' ? ligand : (ligand.name || ligand.resName || ligand.key || '');
                }).filter(Boolean),
                chains: Object.keys(state.currentAnalysisData.sequences || {}),
            }
            : {};
        const selection = [];
        const seen = new Set();
        const addSelection = function (chain, resi, resname) {
            if (resi == null || seen.has(chain + ':' + resi)) return;
            seen.add(chain + ':' + resi);
            selection.push({ chain: chain || 'A', resi: Number(resi), resname: resname || '' });
        };
        if (state.seqActive) addSelection(state.seqActive.chain, state.seqActive.resi, state.seqActive.resName);
        (Array.isArray(state.selectedResidues) ? state.selectedResidues : []).forEach(function (residue) {
            if (residue && typeof residue === 'object') addSelection(residue.chain, residue.resi, residue.resname || residue.resName);
        });
        document.querySelectorAll('#mol-vis-seq-body .mol-vis-seq-cell.active, .mol-vis-seq-cell.active').forEach(function (cell) {
            addSelection(cell.dataset.chain, cell.dataset.resi, cell.title ? cell.title.split(' ')[1] : '');
        });
        const ligandKey = state.interactionLigandKey || state.currentLigandKey || state.selectedLigand || '';
        const name = state.currentStructureName || '';
        if (!text && !chains.length) return null;
        return {
            text: text,
            name: name,
            format: /\.(cif|mmcif)$/i.test(name) ? 'cif' : 'pdb',
            chains: chains,
            selection: selection,
            ligand: String(ligandKey || ''),
            project_id: state.currentProjectId || null,
            job_id: state.currentJobId || '',
            file_id: state.currentFileId || null,
            analysis: analysis,
        };
    }

    function viewerSummary(viewer) {
        if (!viewer) return null;
        return {
            attached: true,
            name: viewer.name,
            format: viewer.format,
            structure_chars: viewer.text ? viewer.text.length : 0,
            chains: viewer.chains.map(function (chain) { return { chain: chain.chain, length: chain.sequence.length }; }),
            selection: viewer.selection.slice(0, 200),
            ligand: viewer.ligand,
            project_id: viewer.project_id,
            job_id: viewer.job_id,
            file_id: viewer.file_id,
        };
    }

    function publishViewerContext() {
        const viewer = viewerAttach ? extractViewer() : null;
        if (!viewer) return;
        const sig = [
            viewer.name, viewer.text.length, viewer.project_id, viewer.job_id,
            viewer.selection.map(function (item) { return item.chain + ':' + item.resi; }).join(','),
            viewer.ligand, viewer.chains.map(function (chain) { return chain.chain + ':' + chain.sequence.length; }).join(',')
        ].join('|');
        if (sig === lastViewerSignature) return;
        lastViewerSignature = sig;
        fetch('/api/agent/viewer-context', {
            method: 'PUT',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
            body: JSON.stringify({
                structure_text: viewer.text,
                name: viewer.name,
                format: viewer.format,
                chains: viewer.chains,
                selection: viewer.selection,
                ligand: viewer.ligand,
                project_id: viewer.project_id,
                job_id: viewer.job_id,
                file_id: viewer.file_id,
                analysis: viewer.analysis,
            }),
        }).catch(function () { /* the assistant still has the summary */ });
    }

    function collect() {
        const mode = document.body && document.body.dataset ? document.body.dataset.agentMode : '';
        const base = mode === 'page'
            ? fromQuery()
            : {
                route: route(),
                title: pageTitle(),
            };
        const entity = mode === 'page' ? base.entity : openEntity();
        const selected = mode === 'page' ? [] : selection();
        const viewSettings = mode === 'page' ? {} : settings();
        const visibleFacts = mode === 'page' ? [] : facts();
        const context = {
            route: base.route,
            title: base.title,
        };
        const viewer = viewerAttach ? extractViewer() : null;
        if (viewer) {
            context.viewer = viewerSummary(viewer);
            context.entity = {
                type: 'structure',
                id: viewer.job_id || viewer.file_id || viewer.name || 'live',
                label: viewer.name || 'Live structure',
            };
        } else if (entity) context.entity = entity;
        if (selected.length) context.selection = selected;
        if (Object.keys(viewSettings).length) context.settings = viewSettings;
        if (visibleFacts.length) context.facts = visibleFacts;
        const suggest = suggestions();
        if (suggest.length) context.suggestions = suggest;
        return context;
    }

    const listeners = new Set();
    let timer = null;

    function notify() {
        if (timer) return;
        timer = window.setTimeout(function () {
            timer = null;
            listeners.forEach(function (fn) {
                try { fn(collect()); } catch (err) { /* a listener must not break context */ }
            });
        }, 80);
    }

    function wrap(obj, name) {
        if (!obj || typeof obj[name] !== 'function' || obj[name].__phonosAgentWrapped) return;
        const original = obj[name];
        const wrapped = function () {
            const result = original.apply(this, arguments);
            notify();
            if (result && typeof result.then === 'function') result.then(notify, notify);
            return result;
        };
        wrapped.__phonosAgentWrapped = true;
        obj[name] = wrapped;
    }

    function install() {
        if (window.Views) wrap(window.Views, 'load');
        if (window.App) {
            wrap(window.App, 'showJobDetails');
            wrap(window.App, 'previewFile');
            wrap(window.App, 'selectProject');
            wrap(window.App, 'renderProject');
        }
        // The Vite bundle may finish after this deferred script; retry the
        // wrappers briefly instead of requiring a reload.
        if (!window.Views || !window.App) {
            [400, 1200, 3000].forEach(function (delay) {
                window.setTimeout(function () {
                    if (window.Views) wrap(window.Views, 'load');
                    if (window.App) {
                        wrap(window.App, 'showJobDetails');
                        wrap(window.App, 'previewFile');
                        wrap(window.App, 'selectProject');
                        wrap(window.App, 'renderProject');
                    }
                }, delay);
            });
        }
        const target = document.getElementById('content-area');
        if (target && window.MutationObserver) {
            const observer = new MutationObserver(notify);
            observer.observe(target, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'data-project-id'] });
        }
        window.setInterval(publishViewerContext, 4000);
        window.addEventListener('hashchange', notify);
        window.addEventListener('popstate', notify);
        document.addEventListener('change', notify, true);
        document.addEventListener('click', notify, true);
    }

    window.PhonosAgentContext = {
        version: LIMIT_VERSION,
        collect: collect,
        viewer: function () { return viewerAttach ? extractViewer() : null; },
        viewerAttached: function () { return viewerAttach; },
        setViewerAttach: function (value) {
            viewerAttach = !!value;
            try { localStorage.setItem('phonos-agent-viewer-attach', viewerAttach ? '1' : '0'); } catch (err) { /* ignore */ }
            notify();
            if (viewerAttach) publishViewerContext();
        },
        publishViewer: publishViewerContext,
        subscribe: function (fn) {
            listeners.add(fn);
            return function () { listeners.delete(fn); };
        },
        refresh: notify,
        query: fromQuery,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', install);
    } else {
        install();
    }
})();
