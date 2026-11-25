/* Phonos Copilot — chat, tool catalog, context rail and settings.
 *
 * One script drives two surfaces:
 *   - the right-hand panel inside the Phonos SPA (data-agent-mode="panel")
 *   - the dedicated /agent page (data-agent-mode="page")
 *
 * All model text is rendered with textContent. Tool results are JSON shown in
 * <pre>; tool cards are built from escaped argument summaries. Nothing from the
 * model or a tool is ever inserted as HTML.
 */
(function () {
    'use strict';

    const body = document.body;
    const MODE = (body && body.dataset && body.dataset.agentMode) || 'panel';
    const ROOT = document.getElementById('phonos-agent-root');
    const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content
        || (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
    const STORE_CHAT = 'phonos-agent-last-chat';
    const STORE_TAB = 'phonos-agent-tab';

    const state = {
        config: null,
        chats: [],
        chat: null,
        messages: [],
        tools: [],
        toolSummary: null,
        tab: localStorage.getItem(STORE_TAB) || 'chat',
        busy: false,
        abort: null,
        streamText: '',
        streamEl: null,
        context: null,
        localJobCards: [],
        live: false,
        autoSpeak: localStorage.getItem('phonos-agent-speak') === '1',
        recognition: null,
        speaking: false,
        chatListEls: [],
        stageEls: {},
    };

    // ---------------------------------------------------------------------
    // tiny DOM helpers (no innerHTML for anything that came from a model)
    // ---------------------------------------------------------------------
    function h(tag, attrs, children) {
        const node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                const value = attrs[key];
                if (value == null || value === false) return;
                if (key === 'class') node.className = value;
                else if (key === 'text') node.textContent = value;
                else if (key === 'html') node.innerHTML = value; // static markup only
                else if (key === 'dataset') Object.keys(value).forEach(function (k) { node.dataset[k] = value[k]; });
                else if (key.indexOf('on') === 0 && typeof value === 'function') node.addEventListener(key.slice(2), value);
                else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
                else node.setAttribute(key, value);
            });
        }
        (children || []).forEach(function (child) {
            if (child == null) return;
            node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
        });
        return node;
    }

    function clear(node) {
        while (node && node.firstChild) node.removeChild(node.firstChild);
        return node;
    }

    function icon(name, size) {
        const paths = {
            spark: ['M13 2L3 14h7l-1 8 10-12h-7l1-8z'],
            chat: ['M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'],
            tools: ['M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z'],
            close: ['M18 6L6 18', 'M6 6l12 12'],
            plus: ['M12 5v14', 'M5 12h14'],
            send: ['M22 2L11 13', 'M22 2l-7 20-4-9-9-4 20-7z'],
            stop: ['M6 6h12v12H6z'],
            bolt: ['M13 2L3 14h7l-1 8 10-12h-7l1-8z'],
            refresh: ['M23 4v6h-6', 'M1 20v-6h6', 'M3.51 9a9 9 0 0 1 14.85-3.36L23 10', 'M1 14l4.64 4.36A9 9 0 0 0 20.49 15'],
            external: ['M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6', 'M15 3h6v6', 'M10 14L21 3'],
            trash: ['M3 6h18', 'M8 6V4h8v2', 'M19 6l-1 14H6L5 6', 'M10 11v6', 'M14 11v6'],
            mic: ['M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z', 'M19 10v2a7 7 0 0 1-14 0v-2', 'M12 19v4'],
            speaker: ['M11 5L6 9H2v6h4l5 4V5z', 'M19.07 4.93a10 10 0 0 1 0 14.14', 'M15.54 8.46a5 5 0 0 1 0 7.07'],
            chevron: ['M6 9l6 6 6-6'],
            check: ['M20 6L9 17l-5-5'],
            file: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z', 'M14 2v6h6', 'M8 13h8', 'M8 17h5'],
            warn: ['M12 9v4', 'M12 17h.01', 'M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z'],
        };
        const list = paths[name] || paths.spark;
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('width', size || 18);
        svg.setAttribute('height', size || 18);
        list.forEach(function (d) {
            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }

    function button(label, className, onClick, iconName) {
        const node = h('button', { type: 'button', class: className || 'pa-btn' }, []);
        if (iconName) node.appendChild(icon(iconName, 16));
        if (label) node.appendChild(h('span', { text: label }));
        if (onClick) node.addEventListener('click', onClick);
        return node;
    }

    function parseServerDate(value) {
        if (!value) return null;
        let text = String(value);
        if (/^\d{4}-\d\d-\d\d[ T]\d\d:\d\d/.test(text) && !/[zZ]$|[+-]\d\d:?\d\d$/.test(text)) {
            text = text.replace(' ', 'T') + 'Z';
        }
        const date = new Date(text);
        return Number.isNaN(date.getTime()) ? null : date;
    }

    function relativeTime(value) {
        if (!value) return '';
        const date = parseServerDate(value);
        if (!date) return String(value).slice(0, 16);
        const diff = (Date.now() - date.getTime()) / 1000;
        if (diff < 60) return 'just now';
        if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
        if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
        return date.toLocaleDateString();
    }

    function showError(message) {
        const host = state.stageEls.error;
        if (!host) {
            console.error(message);
            return;
        }
        clear(host);
        host.appendChild(h('div', { class: 'pa-error' }, [
            icon('warn', 16),
            h('span', { text: message }),
            button('', 'pa-icon-btn', function () { clear(host); }, 'close'),
        ]));
    }

    // ---------------------------------------------------------------------
    // API
    // ---------------------------------------------------------------------
    async function api(method, path, payload) {
        const options = {
            method: method,
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' },
        };
        if (payload !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(payload);
        }
        if (method !== 'GET' && method !== 'HEAD') options.headers['X-CSRF-Token'] = CSRF;
        const response = await fetch('/api/agent' + path, options);
        let data = null;
        try { data = await response.json(); } catch (err) { data = null; }
        if (!response.ok) {
            const message = (data && (data.message || data.error)) || ('Request failed (' + response.status + ')');
            throw new Error(message);
        }
        return data || {};
    }

    function currentContext() {
        try {
            if (window.PhonosAgentContext) return window.PhonosAgentContext.collect();
        } catch (err) { /* context is optional */ }
        return { route: MODE === 'page' ? 'agent' : (window.Views ? window.Views.current : 'dashboard') };
    }

    function handleAction(action) {
        if (!action || !action.type) return;
        if (action.type === 'confirm_job') {
            showJobConfirmation(action);
            return;
        }
        if (MODE === 'page') {
            try { sessionStorage.setItem('phonos-agent-action', JSON.stringify(action)); } catch (err) { /* ignore */ }
            window.location.href = '/dashboard';
            return;
        }
        try {
            if (action.type === 'open_project' && window.App && App.selectProject) {
                App.selectProject(action.project_id);
            } else if (action.type === 'open_job' && window.App && App.showJobDetails) {
                App.showJobDetails(action.job_id);
            } else if (action.type === 'open_file' && window.App && App.previewFile) {
                App.previewFile(action.file_id, action.filename || '');
            } else if (action.type === 'open_view' && window.Views && Views.load) {
                Views.load(action.view);
            } else if (action.type === 'prepare_job' && window.App && App.selectProject) {
                sessionStorage.setItem('selectedProject', action.project_id);
                window._rerunPrefill = {
                    sequences: [{ name: action.name || 'Copilot input', sequence: action.sequence }],
                    targetModel: action.model_id,
                    sourceJobId: null,
                };
                App.selectProject(action.project_id);
            }
        } catch (err) {
            showError('The action could not be performed: ' + err.message);
        }
    }

    function jobStatusTone(status) {
        const value = String(status || '').toLowerCase();
        if (['completed', 'done', 'success', 'finished'].indexOf(value) >= 0) return 'ok';
        if (['failed', 'error', 'cancelled', 'canceled'].indexOf(value) >= 0) return 'error';
        if (['running', 'processing', 'submitted', 'queued', 'pending'].indexOf(value) >= 0) return 'running';
        return 'idle';
    }

    async function submitJobDraft(action) {
        const payload = { draft: action.draft };
        if (state.chat && state.chat.id) payload.chat_id = state.chat.id;
        if (action.auto_token) payload.auto_token = action.auto_token;
        const data = await api('POST', '/jobs', payload);
        return data.job || {};
    }

    function renderJobOutput(host, job, artifacts, options) {
        clear(host);
        host.appendChild(h('div', { class: 'pa-job-output-head' }, [
            h('span', { class: 'pa-job-status is-' + jobStatusTone(job.status) }, [
                h('span', { class: 'pa-tool-dot' }),
                h('span', { text: String(job.status || 'submitted') }),
            ]),
            h('strong', { text: job.name || job.job_id || 'Run' }),
        ]));
        const rows = [
            ['Model', job.model || ''],
            ['Project', job.project_id != null ? '#' + job.project_id : ''],
            ['Job', job.job_id || ''],
            ['Cost', job.cost != null ? job.cost + ' credits' : ''],
        ];
        const grid = h('div', { class: 'pa-job-output-rows' });
        rows.forEach(function (pair) {
            if (pair[1] === '' || pair[1] == null) return;
            grid.appendChild(h('div', { class: 'pa-context-row' }, [
                h('span', { class: 'pa-context-key', text: pair[0] }),
                h('span', { class: 'pa-context-value', text: String(pair[1]) }),
            ]));
        });
        host.appendChild(grid);
        if (job.message) host.appendChild(h('div', { class: 'pa-job-message', text: String(job.message) }));
        if (job.input_file || job.input_file_id != null) {
            const inputFile = job.input_file || {};
            const input = h('div', { class: 'pa-job-files' }, [
                h('span', { class: 'pa-tool-detail-label', text: 'Used file' }),
                h('div', { class: 'pa-job-artifact' }, [
                    icon('file', 12),
                    h('span', { text: inputFile.name || ('file #' + job.input_file_id) }),
                    h('span', { class: 'pa-muted', text: inputFile.size ? Math.round(inputFile.size / 1024) + ' KB' : '' }),
                ]),
            ]);
            host.appendChild(input);
        }
        if (artifacts && artifacts.length) {
            const list = h('div', { class: 'pa-job-artifacts' });
            list.appendChild(h('span', { class: 'pa-tool-detail-label', text: 'Output files' }));
            artifacts.forEach(function (item) {
                list.appendChild(h('div', { class: 'pa-job-artifact' }, [
                    icon('file', 12),
                    h('span', { text: item.name }),
                    h('span', { class: 'pa-muted', text: item.size ? Math.round(item.size / 1024) + ' KB' : '' }),
                ]));
            });
            host.appendChild(list);
        } else if (jobStatusTone(job.status) === 'running') {
            host.appendChild(h('div', { class: 'pa-muted', text: 'Output files will appear when the job finishes.' }));
        }
        const actions = h('div', { class: 'pa-job-output-actions' }, [
            button('Open job', 'pa-btn pa-btn-sm', function () { handleAction({ type: 'open_job', job_id: job.job_id }); }),
            button('Refresh', 'pa-btn pa-btn-sm', function () { if (options && options.refresh) options.refresh(); }, 'refresh'),
        ]);
        if (options && options.close) actions.appendChild(button('Close', 'pa-btn pa-btn-sm', options.close));
        host.appendChild(actions);
    }

    function showJobOutput(jobId, existingHost) {
        let host = existingHost;
        if (!host) {
            host = h('div', { class: 'pa-job-output' });
            state.localJobCards.push(host);
            const list = state.stageEls.messages;
            if (list) { list.appendChild(host); scrollMessages(); }
        }
        let attempts = 0;
        let timer = null;
        const refresh = async function () {
            attempts += 1;
            if (host && host.isConnected === false) return;
            try {
                const data = await api('GET', '/jobs/' + encodeURIComponent(jobId));
                renderJobOutput(host, data.job || {}, data.artifacts || [], {
                    refresh: refresh,
                    close: existingHost ? null : function () { if (host.parentNode) host.parentNode.removeChild(host); },
                });
                scrollMessages();
                const tone = jobStatusTone((data.job || {}).status);
                if (tone === 'running' || tone === 'idle') {
                    if (attempts < 45) timer = setTimeout(refresh, 4000);
                }
            } catch (err) {
                clear(host);
                host.appendChild(h('div', { class: 'pa-error', text: 'Could not read the job: ' + err.message }));
            }
        };
        if (timer) clearTimeout(timer);
        refresh();
        return host;
    }

    function showJobConfirmation(action) {
        const draft = action.draft || {};
        const auto = !!action.auto && !!action.auto_token;
        const overlay = h('div', { class: 'pa-modal-overlay' });
        const close = function () {
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        };
        const modelName = action.model_name || draft.model;
        const body = h('div', { class: 'pa-modal-body' });
        const projectRow = h('div', { class: 'pa-context-row' }, [
            h('span', { class: 'pa-context-key', text: 'Project' }),
        ]);
        const projectSelect = h('select', { class: 'pa-select pa-project-select', 'aria-label': 'Project' });
        const currentOption = h('option', { value: String(draft.project_id || ''), text: action.project_name || ('Project #' + draft.project_id) });
        projectSelect.appendChild(currentOption);
        projectSelect.value = String(draft.project_id || '');
        projectSelect.disabled = auto;
        projectSelect.addEventListener('change', function () {
            draft.project_id = Number(projectSelect.value);
            action.project_name = projectSelect.options[projectSelect.selectedIndex].text;
        });
        projectRow.appendChild(projectSelect);
        body.appendChild(projectRow);
        const rows = [
            ['Model', modelName],
            ['Name', draft.name || ''],
            ['Input', action.input_summary || (draft.sequence ? draft.sequence.length + ' residues' : 'file #' + draft.file_id)],
            ['Cost', (action.cost != null ? action.cost + ' credits' : '') + (action.credits != null ? ' · ' + action.credits + ' available' : '')],
        ];
        rows.forEach(function (pair) {
            body.appendChild(h('div', { class: 'pa-context-row' }, [
                h('span', { class: 'pa-context-key', text: pair[0] }),
                h('span', { class: 'pa-context-value', text: String(pair[1] == null ? '' : pair[1]) }),
            ]));
        });
        if (action.warning || (action.duplicates && action.duplicates.length)) {
            const note = h('div', { class: 'pa-notice' });
            note.appendChild(h('strong', { text: 'Possible duplicate. ' }));
            note.appendChild(h('span', { text: action.warning || 'A matching job already exists. Confirm only if you want another copy.' }));
            body.appendChild(note);
            (action.duplicates || []).slice(0, 3).forEach(function (dup) {
                body.appendChild(h('div', { class: 'pa-muted', text: (dup.job_id || '') + ' · ' + (dup.status || '') + ' · ' + (dup.created_at || '') }));
            });
        }
        if (action.files && action.files.length) {
            const files = h('div', { class: 'pa-job-files' });
            files.appendChild(h('span', { class: 'pa-tool-detail-label', text: 'Used files' }));
            action.files.forEach(function (file) {
                files.appendChild(h('div', { class: 'pa-job-artifact' }, [
                    icon('file', 12),
                    h('span', { text: file.name || ('file #' + file.id) }),
                    h('span', { class: 'pa-muted', text: file.size ? Math.round(file.size / 1024) + ' KB' : '' }),
                ]));
            });
            body.appendChild(files);
        }
        if (action.cost != null && action.credits != null && action.credits < action.cost) {
            body.appendChild(h('div', { class: 'pa-error', text: 'Not enough credits for this run.' }));
        }
        if (draft.sequence) {
            body.appendChild(h('details', { class: 'pa-tool-args' }, [
                h('summary', { text: 'Input preview' }),
                h('pre', { text: String(draft.sequence).slice(0, 2000) }),
            ]));
        }
        const status = h('div', { class: 'pa-settings-status' });
        let outputHost = null;
        const run = button(auto ? 'Run automatically' : 'Run job', 'pa-btn pa-btn-primary', async function () {
            if (action.cost != null && action.credits != null && action.credits < action.cost) return;
            run.disabled = true;
            run.textContent = 'Submitting…';
            status.textContent = 'Queueing the job…';
            try {
                const job = await submitJobDraft(action);
                clear(body);
                body.appendChild(h('div', { class: 'pa-job-ok' }, [
                    icon('check', 20),
                    h('div', {}, [
                        h('strong', { text: 'Job submitted' }),
                        h('p', { class: 'pa-muted', text: (job.name || job.job_id) + ' · ' + (job.status || 'submitted') }),
                    ]),
                ]));
                if (auto && job.job_id) {
                    close();
                    showJobOutput(job.job_id);
                } else {
                    outputHost = h('div', { class: 'pa-job-output' });
                    body.appendChild(outputHost);
                    if (job.job_id) showJobOutput(job.job_id, outputHost);
                }
                run.remove();
                status.textContent = '';
            } catch (err) {
                status.textContent = err.message;
                run.disabled = false;
                run.textContent = auto ? 'Run automatically' : 'Run job';
            }
        }, 'bolt');
        const cancel = button('Cancel', 'pa-btn', close);
        const modal = h('div', { class: 'pa-modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Confirm job' }, [
            h('div', { class: 'pa-modal-head' }, [
                h('div', {}, [
                    h('h2', { text: auto ? 'Auto-run is enabled' : 'Run a bio model?' }),
                    h('p', { class: 'pa-muted', text: auto
                        ? 'This chat may submit jobs without a second click. The job starts now and output appears here.'
                        : 'Nothing has been queued yet. Check the project, model and input, then confirm.' }),
                ]),
                button('', 'pa-icon-btn', close, 'close'),
            ]),
            body,
            h('div', { class: 'pa-modal-actions' }, [cancel, run]),
            status,
        ]);
        overlay.appendChild(modal);
        overlay.addEventListener('click', function (event) { if (event.target === overlay) close(); });
        document.addEventListener('keydown', function onKey(event) {
            if (event.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }
        });
        document.body.appendChild(overlay);

        // Populate the project picker without blocking the modal.
        api('POST', '/tools/list_projects/run', { arguments: { limit: 100 }, context: currentContext() })
            .then(function (response) {
                const projects = (response.result && response.result.projects) || [];
                const selected = String(draft.project_id || '');
                clear(projectSelect);
                projects.forEach(function (project) {
                    projectSelect.appendChild(h('option', { value: String(project.id), text: project.name }));
                });
                if (selected && !projects.some(function (project) { return String(project.id) === selected; })) {
                    projectSelect.appendChild(currentOption);
                }
                projectSelect.value = selected;
            })
            .catch(function () { /* current project remains */ });

        if (auto) setTimeout(function () { if (overlay.parentNode) run.click(); }, 900);
    }

    function consumePendingAction() {
        if (MODE !== 'panel') return;
        let raw = null;
        try { raw = sessionStorage.getItem('phonos-agent-action'); } catch (err) { raw = null; }
        if (!raw) return;
        try { sessionStorage.removeItem('phonos-agent-action'); } catch (err) { /* ignore */ }
        try {
            const action = JSON.parse(raw);
            setTimeout(function () { handleAction(action); }, 900);
        } catch (err) { /* ignore */ }
    }

    // ---------------------------------------------------------------------
    // chats
    // ---------------------------------------------------------------------
    async function loadConfig() {
        state.config = await api('GET', '/config');
    }

    async function loadModels(refresh) {
        try {
            const data = await api('GET', '/models' + (refresh ? '?refresh=1' : ''));
            state.models = data.models || [];
        } catch (err) {
            state.models = [];
        }
        renderModelPicker();
        return state.models;
    }

    async function loadChats() {
        const data = await api('GET', '/chats');
        state.chats = data.chats || [];
        renderChatLists();
    }

    async function openChat(chatId, options) {
        if (!chatId) return;
        if (state.busy) await stopStream();
        const data = await api('GET', '/chats/' + chatId);
        state.chat = data.chat;
        state.pendingModel = null;
        state.localJobCards = [];
        state.messages = data.messages || [];
        try { localStorage.setItem(STORE_CHAT, String(chatId)); } catch (err) { /* ignore */ }
        renderChatLists();
        renderMessages();
        renderModelPicker();
        renderAutoRun();
        if (!options || options.focus !== false) focusComposer();
    }

    async function newChat() {
        if (state.busy) await stopStream();
        const body = {};
        if (state.pendingModel) body.model = state.pendingModel;
        const data = await api('POST', '/chats', body);
        state.chat = data.chat;
        state.messages = [];
        state.localJobCards = [];
        state.chats.unshift(data.chat);
        try { localStorage.setItem(STORE_CHAT, String(data.chat.id)); } catch (err) { /* ignore */ }
        renderChatLists();
        renderMessages();
        renderModelPicker();
        renderAutoRun();
        focusComposer();
    }

    async function renameChat(chat) {
        const title = window.prompt('Chat title', chat.title || '');
        if (!title) return;
        const data = await api('PATCH', '/chats/' + chat.id, { title: title });
        const index = state.chats.findIndex(function (item) { return item.id === chat.id; });
        if (index >= 0) state.chats[index] = data.chat;
        if (state.chat && state.chat.id === chat.id) state.chat = data.chat;
        renderChatLists();
    }

    async function deleteChat(chat) {
        if (!window.confirm('Delete "' + (chat.title || 'this chat') + '"? The audit trail stays on the server.')) return;
        await api('DELETE', '/chats/' + chat.id);
        state.chats = state.chats.filter(function (item) { return item.id !== chat.id; });
        if (state.chat && state.chat.id === chat.id) {
            state.chat = null;
            state.messages = [];
            renderMessages();
        }
        renderChatLists();
    }

    async function rewindChat(messageId) {
        if (!state.chat) return;
        if (!window.confirm('Hide this question and everything after it, then ask again?')) return;
        await api('POST', '/chats/' + state.chat.id + '/rewind', { message_id: messageId });
        const data = await api('GET', '/chats/' + state.chat.id);
        state.messages = data.messages || [];
        renderMessages();
    }

    // ---------------------------------------------------------------------
    // messages
    // ---------------------------------------------------------------------
    function messageText() {
        const input = state.stageEls.input;
        return input ? input.value.trim() : '';
    }

    function focusComposer() {
        const input = state.stageEls.input;
        if (input) setTimeout(function () { input.focus(); }, 30);
    }

    function toolResultFor(callId) {
        const row = state.messages.find(function (message) {
            return message.role === 'tool' && String(message.tool_call_id) === String(callId);
        });
        if (!row) return null;
        try { return JSON.parse(row.content || '{}'); } catch (err) { return { summary: row.content }; }
    }

    function prettyValue(value) {
        if (value == null || value === '') return '';
        if (typeof value === 'string') {
            const trimmed = value.trim();
            if (trimmed && (trimmed[0] === '{' || trimmed[0] === '[')) {
                try { return JSON.stringify(JSON.parse(trimmed), null, 2); } catch (err) { /* not JSON */ }
            }
            return value;
        }
        try { return JSON.stringify(value, null, 2); } catch (err) { return String(value); }
    }

    function appendInline(parent, text) {
        const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\((https?:\/\/[^)\s]+)\))/g;
        let last = 0;
        let match;
        while ((match = pattern.exec(text)) !== null) {
            if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
            const token = match[0];
            if (token.startsWith('**')) {
                parent.appendChild(h('strong', { text: token.slice(2, -2) }));
            } else if (token.startsWith('`')) {
                parent.appendChild(h('code', { class: 'pa-inline-code', text: token.slice(1, -1) }));
            } else {
                const label = token.slice(1, token.indexOf(']'));
                const url = token.slice(token.indexOf('](') + 2, -1);
                parent.appendChild(h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: label }));
            }
            last = match.index + token.length;
        }
        if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }

    function renderMarkdown(text) {
        const frag = document.createDocumentFragment();
        const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
        let index = 0;
        let paragraph = [];
        let list = null;
        const flushParagraph = function () {
            if (!paragraph.length) return;
            const p = h('p', { class: 'pa-msg-text' });
            appendInline(p, paragraph.join('\n'));
            frag.appendChild(p);
            paragraph = [];
        };
        const flushList = function () {
            if (list) { frag.appendChild(list); list = null; }
        };
        const splitRow = function (line) {
            return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (cell) { return cell.trim(); });
        };
        const isTableRow = function (line) { return /^\s*\|.*\|\s*$/.test(line); };
        const isSeparator = function (line) {
            if (!isTableRow(line)) return false;
            const cells = splitRow(line);
            return cells.length > 0 && cells.every(function (cell) { return /^:?-{3,}:?$/.test(cell); });
        };
        while (index < lines.length) {
            const line = lines[index];
            const fence = line.match(/^```([A-Za-z0-9_+-]*)\s*$/);
            if (fence) {
                flushParagraph(); flushList();
                const codeLines = [];
                index += 1;
                while (index < lines.length && !/^```\s*$/.test(lines[index])) {
                    codeLines.push(lines[index]);
                    index += 1;
                }
                index += 1;
                const pre = h('pre', { class: 'pa-code-block' });
                pre.appendChild(h('code', { text: codeLines.join('\n') }));
                frag.appendChild(pre);
                continue;
            }
            if (isTableRow(line)) {
                const tableLines = [];
                while (index < lines.length && isTableRow(lines[index])) {
                    tableLines.push(lines[index]);
                    index += 1;
                }
                if (tableLines.length >= 2 && isSeparator(tableLines[1])) {
                    flushParagraph(); flushList();
                    const headers = splitRow(tableLines[0]);
                    const wrap = h('div', { class: 'pa-md-table-wrap' });
                    const table = h('table', { class: 'pa-md-table' });
                    const thead = h('thead');
                    const headRow = h('tr');
                    headers.forEach(function (cell) {
                        const th = h('th');
                        appendInline(th, cell);
                        headRow.appendChild(th);
                    });
                    thead.appendChild(headRow);
                    table.appendChild(thead);
                    const tbody = h('tbody');
                    tableLines.slice(2).forEach(function (rowLine) {
                        const cells = splitRow(rowLine);
                        const tr = h('tr');
                        headers.forEach(function (_, cellIndex) {
                            const td = h('td');
                            appendInline(td, cells[cellIndex] || '');
                            tr.appendChild(td);
                        });
                        tbody.appendChild(tr);
                    });
                    table.appendChild(tbody);
                    wrap.appendChild(table);
                    frag.appendChild(wrap);
                    continue;
                }
                // Not a table after all: treat the collected lines as prose.
                lines.splice(index - tableLines.length, tableLines.length);
                index -= tableLines.length;
            }
            const heading = line.match(/^(#{1,4})\s+(.+)$/);
            if (heading) {
                flushParagraph(); flushList();
                const level = heading[1].length;
                const node = h('h' + (level + 1), { class: 'pa-md-heading' });
                appendInline(node, heading[2]);
                frag.appendChild(node);
                index += 1;
                continue;
            }
            const bullet = line.match(/^\s*[-*]\s+(.+)$/);
            const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
            if (bullet || numbered) {
                flushParagraph();
                const type = bullet ? 'ul' : 'ol';
                if (!list || list.tagName.toLowerCase() !== type) {
                    flushList();
                    list = h(type, { class: 'pa-md-list' });
                }
                const li = h('li');
                appendInline(li, (bullet || numbered)[1]);
                list.appendChild(li);
                index += 1;
                continue;
            }
            if (!line.trim()) {
                flushParagraph(); flushList();
                index += 1;
                continue;
            }
            if (/^\s*---+\s*$/.test(line)) {
                flushParagraph(); flushList();
                frag.appendChild(h('hr', { class: 'pa-md-rule' }));
                index += 1;
                continue;
            }
            paragraph.push(line.trim());
            index += 1;
        }
        flushParagraph(); flushList();
        return frag;
    }

    function toolStatus(result) {
        if (!result) return { key: 'running', label: 'running' };
        if (result.ok === false) return { key: 'error', label: 'error' };
        return { key: 'ok', label: 'done' };
    }

    function renderToolTable(calls, resultFor) {
        if (!calls || !calls.length) return null;
        const wrap = h('div', { class: 'pa-tool-table-wrap' });
        const table = h('table', { class: 'pa-tool-table' }, [
            h('thead', {}, [h('tr', {}, [
                h('th', { text: 'Tool' }),
                h('th', { text: 'Status' }),
                h('th', { class: 'pa-tool-col-summary', text: 'Result' }),
                h('th', { class: 'pa-tool-col-toggle' }),
            ])]),
        ]);
        const tbody = h('tbody');
        calls.forEach(function (call, index) {
            const fn = call.function || {};
            const name = fn.name || 'tool';
            const result = resultFor ? resultFor(call.id) : null;
            const status = toolStatus(result);
            const summary = result ? (result.summary || result.error || '') : 'Running…';
            const detail = h('tr', { class: 'pa-tool-detail-row', hidden: true });
            const detailCell = h('td', { colspan: '4' });
            const panel = h('div', { class: 'pa-tool-detail-panel' });
            const grid = h('div', { class: 'pa-tool-detail-grid' });
            const argSection = h('section', {}, [
                h('div', { class: 'pa-tool-detail-label', text: 'Arguments' }),
                h('pre', { class: 'pa-tool-pre', text: prettyValue(fn.arguments) || '{}' }),
            ]);
            const resultSection = h('section', {}, [
                h('div', { class: 'pa-tool-detail-label', text: 'Result' }),
                h('pre', { class: 'pa-tool-pre', text: result ? prettyValue(result.preview || result) : 'Waiting for the tool…' }),
            ]);
            grid.appendChild(argSection);
            grid.appendChild(resultSection);
            panel.appendChild(grid);
            if (result && result.preview) {
                panel.appendChild(h('div', { class: 'pa-tool-detail-actions' }, [
                    button('Copy result', 'pa-link-btn', function () {
                        if (navigator.clipboard) navigator.clipboard.writeText(String(result.preview)).catch(function () { /* ignore */ });
                    }),
                ]));
            }
            detailCell.appendChild(panel);
            detail.appendChild(detailCell);
            const row = h('tr', {
                class: 'pa-tool-row is-' + status.key,
                tabindex: '0',
                title: 'Show arguments and result',
                onclick: function () { detail.hidden = !detail.hidden; row.classList.toggle('is-open', !detail.hidden); },
            }, [
                h('td', { class: 'pa-tool-cell-name' }, [
                    h('span', { class: 'pa-tool-table-icon' }, [icon(status.key === 'error' ? 'warn' : 'tools', 13)]),
                    h('span', { class: 'pa-tool-table-nameblock' }, [
                        h('span', { class: 'pa-tool-table-name', text: name.replace(/_/g, ' ') }),
                        h('code', { class: 'pa-tool-table-raw', text: name }),
                    ]),
                ]),
                h('td', { class: 'pa-tool-cell-status' }, [
                    h('span', { class: 'pa-tool-status is-' + status.key }, [
                        h('span', { class: 'pa-tool-dot' }),
                        h('span', { text: status.label }),
                    ]),
                ]),
                h('td', { class: 'pa-tool-cell-summary', text: summary || 'No result stored.' }),
                h('td', { class: 'pa-tool-cell-toggle' }, [icon('chevron', 13)]),
            ]);
            row.addEventListener('keydown', function (event) {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    detail.hidden = !detail.hidden;
                    row.classList.toggle('is-open', !detail.hidden);
                }
            });
            tbody.appendChild(row);
            tbody.appendChild(detail);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        return wrap;
    }

    function addMessageNode(role, text, options) {
        const wrap = h('div', { class: 'pa-msg pa-msg-' + role });
        if (role === 'assistant') {
            wrap.appendChild(h('div', { class: 'pa-msg-avatar' }, [icon('spark', 16)]));
        }
        const bubble = h('div', { class: 'pa-msg-bubble' });
        if (text) {
            String(text).split(/\n{2,}/).forEach(function (paragraph) {
                bubble.appendChild(h('p', { class: 'pa-msg-text', text: paragraph }));
            });
        }
        if (options && options.node) bubble.appendChild(options.node);
        wrap.appendChild(bubble);
        return { wrap: wrap, bubble: bubble };
    }

    function renderMessages() {
        renderContextBar();
        const list = state.stageEls.messages;
        if (!list) return;
        clear(list);
        if (!state.chat) {
            renderEmptyState(list);
            return;
        }
        if (!state.messages.length) {
            renderEmptyState(list);
            return;
        }
        let lastAssistantId = null;
        state.messages.forEach(function (message) { if (message.role === 'assistant') lastAssistantId = message.id; });

        // One assistant turn can span several model rounds (text → tool calls →
        // text). Group them under one avatar and one bubble so the conversation
        // reads as a single answer instead of a stack of separate messages.
        let group = [];
        const flush = function () {
            if (!group.length) return;
            const combinedText = group
                .map(function (message) { return String(message.content || '').trim(); })
                .filter(Boolean)
                .join('\n\n');
            const wrap = h('div', { class: 'pa-msg pa-msg-assistant' }, [
                h('div', { class: 'pa-msg-avatar' }, [icon('spark', 16)]),
            ]);
            const bubble = h('div', { class: 'pa-msg-bubble' });
            const allCalls = [];
            group.forEach(function (message) {
                (message.tool_calls || []).forEach(function (call) { allCalls.push(call); });
            });
            const toolTable = renderToolTable(allCalls, toolResultFor);
            if (toolTable) bubble.appendChild(toolTable);
            if (combinedText) bubble.appendChild(renderMarkdown(combinedText));
            const interrupted = group.some(function (message) { return message.interrupted; });
            if (interrupted) bubble.appendChild(h('div', { class: 'pa-interrupted', text: 'Stopped' }));
            const actions = h('div', { class: 'pa-msg-actions' });
            if (combinedText) {
                actions.appendChild(button('Read aloud', 'pa-link-btn', function () { speakText(combinedText); }));
                actions.appendChild(button('Copy', 'pa-link-btn', function () {
                    if (navigator.clipboard) navigator.clipboard.writeText(combinedText).catch(function () { /* ignore */ });
                }));
            }
            const containsLast = group.some(function (message) { return message.id === lastAssistantId; });
            if (containsLast) {
                const firstId = group[0] && group[0].id != null ? group[0].id : Infinity;
                const userMessage = state.messages.slice().reverse().find(function (item) {
                    return item.role === 'user' && item.id < firstId;
                });
                if (userMessage) {
                    actions.appendChild(button('Regenerate', 'pa-link-btn', function () {
                        rewindChat(userMessage.id);
                    }));
                }
                const modelMessage = group.slice().reverse().find(function (message) { return message.model; });
                if (modelMessage && modelMessage.model) {
                    actions.appendChild(h('span', { class: 'pa-msg-model', text: modelMessage.model }));
                }
            }
            if (actions.children.length) bubble.appendChild(actions);
            wrap.appendChild(bubble);
            list.appendChild(wrap);
            group = [];
        };

        state.messages.forEach(function (message) {
            if (message.role === 'user') {
                flush();
                const node = addMessageNode('user', message.content || '');
                list.appendChild(node.wrap);
                return;
            }
            if (message.role === 'assistant') {
                group.push(message);
            }
            // Tool rows are results for the cards already shown in the assistant
            // group; they are not separate conversation bubbles.
        });
        flush();
        (state.localJobCards || []).forEach(function (card) {
            if (card && card.parentNode !== list) list.appendChild(card);
        });
        scrollMessages();
    }

    function renderEmptyState(list) {
        const box = h('div', { class: 'pa-empty' }, [
            h('div', { class: 'pa-empty-icon' }, [icon('spark', 26)]),
            h('h2', { text: 'What are we working on?' }),
            h('p', { text: 'It reads your projects, jobs, files, models, pipelines and credits through tools — and keeps every change behind your confirmation.' }),
        ]);
        const suggestions = (state.context && state.context.suggestions) || [];
        if (suggestions.length) {
            const grid = h('div', { class: 'pa-suggestions' });
            suggestions.forEach(function (item) {
                const btn = button('', 'pa-suggestion', function () {
                    if (state.stageEls.input) state.stageEls.input.value = item.text;
                    sendMessage();
                });
                btn.appendChild(h('strong', { text: item.title || 'Ask' }));
                btn.appendChild(h('span', { text: item.text }));
                grid.appendChild(btn);
            });
            box.appendChild(grid);
        }
        const rtBox = h('div', { class: 'pa-routines-inline', style: 'margin-top:16px;width:100%;max-width:760px;' });
        rtBox.appendChild(h('h3', { class: 'pa-section-title', style: 'margin-bottom:8px;text-align:left;', text: 'Discovery Routines' }));
        const rtGrid = h('div', { class: 'pa-suggestions' });
        rtBox.appendChild(rtGrid);
        box.appendChild(rtBox);
        fetch('/api/agent/routines', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (payload) {
                const items = (payload && payload.routines) || [];
                items.forEach(function (rt) {
                    const btn = button('', 'pa-suggestion', function () {
                        if (state.stageEls.input) {
                            state.stageEls.input.value = rt.prompt;
                            focusComposer();
                        }
                    });
                    btn.appendChild(h('strong', { text: '[' + (rt.category || 'Bio') + '] ' + rt.title }));
                    btn.appendChild(h('span', { text: rt.prompt.slice(0, 105) + '…' }));
                    rtGrid.appendChild(btn);
                });
            })
            .catch(function () {});
        list.appendChild(box);
    }

    function scrollMessages() {
        const list = state.stageEls.messages;
        if (list) list.scrollTop = list.scrollHeight;
    }

    async function sendMessage() {
        const prompt = messageText();
        if (!prompt || state.busy) return;
        if (!state.chat) {
            try { await newChat(); } catch (err) { showError(err.message); return; }
        }
        const input = state.stageEls.input;
        if (input) input.value = '';
        if (state.live && state.recognition) { try { state.recognition.stop(); } catch (err) { /* ignore */ } }
        state.context = currentContext();
        const optimistic = { id: 'temp-user', role: 'user', content: prompt, created_at: new Date().toISOString() };
        state.messages.push(optimistic);
        renderMessages();

        state.busy = true;
        state.streamText = '';
        state.streamEl = null;
        state.streamToolCalls = [];
        state.streamToolResults = {};
        setBusy(true);
        const assistantWrap = h('div', { class: 'pa-msg pa-msg-assistant' }, [
            h('div', { class: 'pa-msg-avatar' }, [icon('spark', 16)]),
            h('div', { class: 'pa-msg-bubble' }, [
                h('div', { class: 'pa-stream-tools' }),
                h('div', { class: 'pa-stream-text', text: '' }),
            ]),
        ]);
        const list = state.stageEls.messages;
        if (list) {
            const empty = list.querySelector('.pa-empty');
            if (empty) empty.remove();
            list.appendChild(assistantWrap);
        }
        state.streamEl = assistantWrap.querySelector('.pa-stream-text');
        const streamTools = assistantWrap.querySelector('.pa-stream-tools');

        state.abort = new AbortController();
        try {
            const response = await fetch('/api/agent/chats/' + state.chat.id + '/messages', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream',
                    'X-CSRF-Token': CSRF,
                },
                body: JSON.stringify({ message: prompt, context: state.context }),
                signal: state.abort.signal,
            });
            if (!response.ok) {
                let detail = 'Request failed (' + response.status + ')';
                try { const data = await response.json(); detail = data.message || detail; } catch (err) { /* ignore */ }
                throw new Error(detail);
            }
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const read = await reader.read();
                if (read.done) break;
                buffer += decoder.decode(read.value, { stream: true });
                buffer = buffer.replace(/\r\n/g, '\n');
                let index;
                while ((index = buffer.indexOf('\n\n')) >= 0) {
                    const block = buffer.slice(0, index);
                    buffer = buffer.slice(index + 2);
                    handleSseBlock(block, streamTools);
                }
            }
            if (buffer.trim()) handleSseBlock(buffer, streamTools);
        } catch (err) {
            if (err.name !== 'AbortError') showError(err.message || 'The assistant request failed.');
        } finally {
            state.busy = false;
            state.abort = null;
            setBusy(false);
            if (state.live && !state.speaking) setTimeout(startRecognition, 500);
            try {
                const data = await api('GET', '/chats/' + state.chat.id);
                state.chat = data.chat;
                state.messages = data.messages || [];
                renderMessages();
                loadChats().catch(function () { /* ignore */ });
            } catch (err) { /* keep the streamed text visible */ }
        }
    }

    function handleSseBlock(block, streamTools) {
        const lines = block.split(/\r?\n/);
        let data = '';
        lines.forEach(function (line) {
            if (line.indexOf('data:') === 0) data += line.slice(5).trim();
        });
        if (!data) return;
        let event;
        try { event = JSON.parse(data); } catch (err) { return; }
        if (event.type === 'delta') {
            state.streamText += event.text || '';
            if (state.streamEl) state.streamEl.textContent = state.streamText;
            scrollMessages();
        } else if (event.type === 'reasoning') {
            // Reasoning is intentionally not stored; show nothing to keep the answer clean.
        } else if (event.type === 'tool_call') {
            state.streamToolCalls.push({
                id: event.id,
                function: { name: event.name, arguments: event.arguments },
            });
            if (streamTools) {
                const table = renderToolTable(state.streamToolCalls, function (id) { return state.streamToolResults[id]; });
                clear(streamTools);
                if (table) streamTools.appendChild(table);
            }
            scrollMessages();
        } else if (event.type === 'tool_result') {
            state.streamToolResults[String(event.id)] = {
                ok: event.ok,
                summary: event.summary,
                preview: event.preview,
                error: event.ok ? '' : event.summary,
            };
            if (streamTools) {
                const table = renderToolTable(state.streamToolCalls, function (id) { return state.streamToolResults[id]; });
                clear(streamTools);
                if (table) streamTools.appendChild(table);
            }
            if (event.client_action) handleAction(event.client_action);
        } else if (event.type === 'assistant') {
            if (event.content && state.streamEl) state.streamEl.textContent = event.content;
        } else if (event.type === 'error') {
            showError(event.detail || 'The assistant reported an error.');
        } else if (event.type === 'done') {
            if (state.autoSpeak || state.live) speakText(state.streamText);
        }
    }

    async function stopStream() {
        if (state.abort) {
            try { state.abort.abort(); } catch (err) { /* ignore */ }
        }
        if (state.chat) {
            try { await api('POST', '/chats/' + state.chat.id + '/cancel', {}); } catch (err) { /* ignore */ }
        }
        state.busy = false;
        setBusy(false);
    }

    function setBusy(busy) {
        const input = state.stageEls.input;
        const send = state.stageEls.send;
        const stop = state.stageEls.stop;
        if (input) input.disabled = false;
        if (send) send.hidden = busy;
        if (stop) stop.hidden = !busy;
    }


    function speechRecognitionClass() {
        return window.SpeechRecognition || window.webkitSpeechRecognition || null;
    }

    function startRecognition() {
        const SR = speechRecognitionClass();
        if (!SR) {
            showError('This browser has no speech recognition. Chrome, Edge or Safari support it; Firefox does not.');
            state.live = false;
            updateVoiceButtons();
            return;
        }
        try {
            if (state.recognition) state.recognition.stop();
        } catch (err) { /* ignore */ }
        const recognition = new SR();
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.lang = navigator.language || 'en-US';
        recognition.onresult = function (event) {
            let finalText = '';
            let interim = '';
            for (let i = event.resultIndex; i < event.results.length; i += 1) {
                const result = event.results[i];
                if (result.isFinal) finalText += result[0].transcript;
                else interim += result[0].transcript;
            }
            if (state.stageEls.input) state.stageEls.input.value = (finalText || interim).trim();
            if (finalText.trim() && !state.busy) sendMessage();
        };
        recognition.onerror = function (event) {
            if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
                showError('Microphone access was denied. Allow it in the browser, or type instead.');
                stopLive();
            } else if (event.error === 'no-speech') {
                // Keep listening; the next phrase will arrive.
            } else if (event.error !== 'aborted') {
                showError('Speech recognition: ' + event.error);
            }
        };
        recognition.onend = function () {
            if (state.live && !state.busy) setTimeout(startRecognition, 350);
        };
        state.recognition = recognition;
        try { recognition.start(); } catch (err) { /* already running */ }
    }

    function toggleLive() {
        if (state.live) {
            stopLive();
            return;
        }
        if (!speechRecognitionClass()) {
            showError('This browser has no speech recognition. Try Chrome, Edge or Safari, or type your question.');
            return;
        }
        state.live = true;
        updateVoiceButtons();
        startRecognition();
        if (state.stageEls.input) state.stageEls.input.placeholder = 'Listening… speak your question. Tap Live again to stop.';
    }

    function stopLive() {
        state.live = false;
        if (state.recognition) {
            try { state.recognition.onend = null; state.recognition.stop(); } catch (err) { /* ignore */ }
            state.recognition = null;
        }
        updateVoiceButtons();
        if (state.stageEls.input) state.stageEls.input.placeholder = 'How can I help with your projects, models or 3D structure?';
    }

    function updateVoiceButtons() {
        if (state.stageEls.liveBtn) {
            state.stageEls.liveBtn.classList.toggle('is-live', state.live);
            state.stageEls.liveBtn.querySelector('span').textContent = state.live ? 'Listening…' : 'Live';
        }
    }

    function speakText(text) {
        if (!window.speechSynthesis || !text) return;
        const plain = String(text)
            .replace(/```[\s\S]*?```/g, ' code block ')
            .replace(/`[^`]+`/g, ' ')
            .replace(/[#*_>`]/g, '')
            .replace(/\s+/g, ' ')
            .trim();
        if (!plain) return;
        window.speechSynthesis.cancel();
        const utterance = new SpeechSynthesisUtterance(plain.slice(0, 6000));
        utterance.lang = navigator.language || 'en-US';
        utterance.rate = 1.0;
        state.speaking = true;
        utterance.onend = function () {
            state.speaking = false;
            if (state.live && !state.busy) setTimeout(startRecognition, 300);
        };
        utterance.onerror = function () { state.speaking = false; };
        window.speechSynthesis.speak(utterance);
    }



    function renderModelPicker() {
        const picker = state.stageEls.modelPicker;
        if (!picker) return;
        const current = state.pendingModel || (state.chat && state.chat.model) || (state.config && state.config.settings && state.config.settings.model) || '';
        clear(picker);
        const seen = new Set();
        (state.models || []).forEach(function (model) {
            if (!model.id || seen.has(model.id)) return;
            seen.add(model.id);
            picker.appendChild(h('option', { value: model.id, text: model.id }));
        });
        if (current && !seen.has(current)) picker.appendChild(h('option', { value: current, text: current }));
        if (!picker.children.length) picker.appendChild(h('option', { value: '', text: 'Default model' }));
        picker.value = current;
        const provider = state.config && state.config.settings && state.config.settings.provider_label;
        picker.title = 'Model for this chat' + (provider ? ' · ' + provider : '');
    }

    async function selectChatModel(value) {
        state.pendingModel = value;
        if (!state.chat) return;
        try {
            const data = await api('PATCH', '/chats/' + state.chat.id, { model: value });
            state.chat = data.chat;
        } catch (err) {
            showError(err.message);
        }
    }

    function renderAutoRun() {
        const btn = state.stageEls.autoBtn;
        if (!btn) return;
        const on = !!(state.chat && state.chat.write_mode);
        btn.classList.toggle('is-active', on);
        const label = btn.querySelector('span');
        if (label) label.textContent = on ? 'Auto-run on' : 'Auto-run';
        if (state.stageEls.autoNote) {
            state.stageEls.autoNote.textContent = on
                ? 'Auto-run: this chat submits model jobs without a second click.'
                : 'Tools read your data; changes need your click.';
        }
    }

    async function toggleAutoRun() {
        try {
            if (!state.chat) await newChat();
            const value = !state.chat.write_mode;
            const data = await api('PATCH', '/chats/' + state.chat.id, { write_mode: value });
            state.chat = data.chat;
            renderAutoRun();
        } catch (err) {
            showError(err.message);
        }
    }

    function buildComposer() {
        const input = h('textarea', {
            class: 'pa-input',
            rows: '3',
            placeholder: 'How can I help with your projects, models or 3D structure?',
            'aria-label': 'Message Phonos Copilot',
        });
        input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        });
        const send = button('Send', 'pa-send', function () { sendMessage(); }, 'send');
        const stop = button('Stop', 'pa-send pa-stop', function () { stopStream(); }, 'stop');
        stop.hidden = true;
        const autoNote = h('span', { text: state.chat && state.chat.write_mode ? 'Auto-run: this chat submits model jobs without a second click.' : 'Tools read your data; changes need your click.' });
        state.stageEls.autoNote = autoNote;
        const footer = h('div', { class: 'pa-composer-foot' }, [
            autoNote,
            h('span', { class: 'pa-kbd', text: 'Ctrl+Shift+A' }),
        ]);
        const voiceBar = h('div', { class: 'pa-composer-tools' });
        const modelPicker = h('select', { class: 'pa-model-picker', 'aria-label': 'Model for this chat' });
        modelPicker.addEventListener('change', function () { selectChatModel(modelPicker.value); });
        state.stageEls.modelPicker = modelPicker;
        voiceBar.appendChild(modelPicker);
        const liveBtn = button('Live', 'pa-mini-btn', function () { toggleLive(); }, 'mic');
        const speakBtn = button('Speak', 'pa-mini-btn' + (state.autoSpeak ? ' is-active' : ''), function () {
            state.autoSpeak = !state.autoSpeak;
            try { localStorage.setItem('phonos-agent-speak', state.autoSpeak ? '1' : '0'); } catch (err) { /* ignore */ }
            speakBtn.classList.toggle('is-active', state.autoSpeak);
            if (!state.autoSpeak && window.speechSynthesis) window.speechSynthesis.cancel();
        }, 'speaker');
        const autoBtn = button('Auto-run', 'pa-mini-btn' + (state.chat && state.chat.write_mode ? ' is-active' : ''), function () { toggleAutoRun(); }, 'bolt');
        autoBtn.title = 'Let Copilot submit bio-model jobs without a second confirmation. Jobs still cost credits.';
        voiceBar.appendChild(liveBtn);
        voiceBar.appendChild(speakBtn);
        voiceBar.appendChild(autoBtn);
        voiceBar.appendChild(h('span', { class: 'pa-voice-hint', text: 'Live uses the browser microphone; answers are spoken locally by the browser.' }));
        state.stageEls.liveBtn = liveBtn;
        state.stageEls.speakBtn = speakBtn;
        state.stageEls.autoBtn = autoBtn;
        const box = h('div', { class: 'pa-composer' }, [
            voiceBar,
            h('div', { class: 'pa-composer-row' }, [input, h('div', { class: 'pa-composer-actions' }, [send, stop])]),
            footer,
        ]);
        state.stageEls.input = input;
        state.stageEls.send = send;
        state.stageEls.stop = stop;
        return box;
    }

    function contextBarText(context) {
        const viewer = context && context.viewer;
        if (viewer && viewer.attached) {
            const bits = ['3D'];
            if (viewer.name) bits.push(viewer.name);
            const chains = viewer.chains || [];
            if (chains.length) bits.push(chains.map(function (chain) { return chain.chain + '(' + chain.length + ')'; }).join(', '));
            if (viewer.ligand) bits.push('ligand ' + viewer.ligand);
            if (viewer.selection && viewer.selection.length) bits.push(viewer.selection.length + ' selected');
            return bits.join(' · ');
        }
        if (context && context.entity) {
            return context.entity.type + (context.entity.id != null ? ' #' + context.entity.id : '') +
                (context.entity.label ? ' · ' + context.entity.label : '');
        }
        if (context && context.title) return context.route + ' · ' + context.title;
        return 'No page context';
    }

    function renderContextBar() {
        const bar = state.stageEls.contextBar;
        if (!bar) return;
        const context = state.context || currentContext();
        clear(bar);
        const viewer = context && context.viewer;
        bar.appendChild(h('span', { class: 'pa-context-dot' + (viewer && viewer.attached ? ' is-live' : '') }));
        bar.appendChild(h('span', { class: 'pa-context-bar-text', text: contextBarText(context) }));
        if (viewer && viewer.attached) {
            bar.appendChild(h('span', { class: 'pa-badge', text: 'full data attached' }));
        }
        bar.appendChild(button('Context', 'pa-link-btn', function () { setTab('context'); }));
    }

    function buildChatPanel() {
        const host = h('div', { class: 'pa-chat' });
        state.stageEls.messages = h('div', { class: 'pa-messages', role: 'log', 'aria-live': 'polite' });
        state.stageEls.error = h('div', { class: 'pa-error-host' });
        host.appendChild(state.stageEls.error);
        state.stageEls.contextBar = h('div', { class: 'pa-context-bar' });
        host.appendChild(state.stageEls.contextBar);
        host.appendChild(state.stageEls.messages);
        host.appendChild(buildComposer());
        return host;
    }

    // ---------------------------------------------------------------------
    // tool catalog
    // ---------------------------------------------------------------------
    async function loadTools() {
        const data = await api('GET', '/tools');
        state.tools = data.tools || [];
        state.toolSummary = data.summary || null;
    }

    function toolInputField(name, spec) {
        const type = spec.type || 'string';
        const field = h('label', { class: 'pa-field' });
        field.appendChild(h('span', { class: 'pa-field-label', text: name + (spec.description ? ' — ' + spec.description : '') }));
        let input;
        if (type === 'boolean') {
            input = h('input', { type: 'checkbox' });
        } else if (type === 'integer' || type === 'number') {
            input = h('input', { type: 'number', step: type === 'integer' ? '1' : 'any' });
        } else if (type === 'array' || type === 'object') {
            input = h('textarea', { rows: '3', placeholder: 'JSON' });
        } else {
            input = h('input', { type: 'text' });
        }
        input.dataset.param = name;
        input.dataset.paramType = type;
        if (spec.default != null) input.value = typeof spec.default === 'string' ? spec.default : JSON.stringify(spec.default);
        field.appendChild(input);
        return field;
    }

    function renderToolTest(tool, host) {
        clear(host);
        const props = (tool.parameters && tool.parameters.properties) || {};
        const required = (tool.parameters && tool.parameters.required) || [];
        const form = h('form', { class: 'pa-tool-form' });
        Object.keys(props).forEach(function (name) {
            form.appendChild(toolInputField(name, props[name] || {}));
        });
        if (!Object.keys(props).length) form.appendChild(h('p', { class: 'pa-muted', text: 'No parameters.' }));
        const result = h('pre', { class: 'pa-tool-result', hidden: true });
        const run = button('Run tool', 'pa-btn pa-btn-primary', async function (event) {
            event.preventDefault();
            const args = {};
            form.querySelectorAll('[data-param]').forEach(function (input) {
                const name = input.dataset.param;
                const type = input.dataset.paramType;
                if (type === 'boolean') { args[name] = input.checked; return; }
                if (!input.value.trim()) return;
                if (type === 'integer') args[name] = parseInt(input.value, 10);
                else if (type === 'number') args[name] = parseFloat(input.value);
                else if (type === 'array' || type === 'object') {
                    try { args[name] = JSON.parse(input.value); } catch (err) { throw new Error(name + ' is not valid JSON.'); }
                } else args[name] = input.value;
            });
            run.disabled = true;
            result.hidden = false;
            result.textContent = 'Running…';
            try {
                const response = await api('POST', '/tools/' + encodeURIComponent(tool.name) + '/run', {
                    arguments: args,
                    context: currentContext(),
                });
                result.textContent = JSON.stringify(response.result, null, 2);
            } catch (err) {
                result.textContent = 'Error: ' + err.message;
            } finally {
                run.disabled = false;
            }
        }, 'bolt');
        const actions = h('div', { class: 'pa-tool-form-actions' }, [
            run,
            h('button', { type: 'button', class: 'pa-btn', text: 'Example', onclick: function () {
                Object.keys(props).forEach(function (name) {
                    const input = form.querySelector('[data-param="' + name.replace(/"/g, '\\"') + '"]');
                    if (!input || input.value) return;
                    const spec = props[name] || {};
                    if (spec.enum && spec.enum.length) input.value = spec.enum[0];
                    else if (spec.type === 'boolean') input.checked = false;
                    else if (spec.type === 'integer' || spec.type === 'number') input.value = '1';
                    else if (spec.type === 'array') input.value = '[]';
                    else if (spec.type === 'object') input.value = '{}';
                    else if (/(project|job|file)_id$/.test(name)) {
                        const ctx = currentContext();
                        const entity = ctx && ctx.entity;
                        if (entity && entity.type && name.indexOf(entity.type) === 0) input.value = entity.id;
                    }
                });
            } }),
        ]);
        form.appendChild(actions);
        form.appendChild(result);
        host.appendChild(form);
    }


    function renderRunsPanel(host) {
        clear(host);
        host.appendChild(h('div', { class: 'pa-panel-head' }, [
            h('div', {}, [h('h2', { text: 'Runs' }),
                h('p', { class: 'pa-muted', text: 'Jobs Copilot proposed or that you submitted from this workspace. Output is read live from the job.' })]),
            button('', 'pa-icon-btn', function () { renderRunsPanel(host); }, 'refresh'),
        ]));
        const list = h('div', { class: 'pa-runs-list' });
        host.appendChild(list);
        list.appendChild(h('p', { class: 'pa-muted', text: 'Loading runs…' }));
        api('POST', '/tools/list_jobs/run', { arguments: { limit: 30 }, context: currentContext() })
            .then(function (response) {
                const jobs = (response.result && response.result.jobs) || [];
                clear(list);
                if (!jobs.length) {
                    list.appendChild(h('p', { class: 'pa-muted', text: 'No jobs yet. Ask Copilot to propose a model run.' }));
                    return;
                }
                jobs.forEach(function (job) {
                    const output = h('div', { class: 'pa-job-output', hidden: true });
                    const row = h('article', { class: 'pa-run-row' }, [
                        h('div', { class: 'pa-run-head' }, [
                            h('span', { class: 'pa-job-status is-' + jobStatusTone(job.status) }, [
                                h('span', { class: 'pa-tool-dot' }),
                                h('span', { text: String(job.status || '') }),
                            ]),
                            h('strong', { text: job.name || job.job_id }),
                            h('span', { class: 'pa-run-cost', text: job.cost != null ? job.cost + ' cr' : '' }),
                        ]),
                        h('p', { class: 'pa-run-meta', text: [job.model, job.project_name, job.created_at].filter(Boolean).join(' · ') }),
                        h('div', { class: 'pa-run-actions' }, [
                            button('Show output', 'pa-btn pa-btn-sm', function () {
                                output.hidden = !output.hidden;
                                if (!output.hidden) showJobOutput(job.job_id, output);
                            }, 'file'),
                            button('Open job', 'pa-btn pa-btn-sm', function () {
                                handleAction({ type: 'open_job', job_id: job.job_id });
                            }, 'external'),
                        ]),
                        output,
                    ]);
                    list.appendChild(row);
                });
            })
            .catch(function (err) {
                clear(list);
                list.appendChild(h('p', { class: 'pa-error', text: err.message }));
            });
    }

    function renderModelsPanel(host) {
        clear(host);
        const head = h('div', { class: 'pa-panel-head' }, [
            h('div', {}, [h('h2', { text: 'Bio AI models' }),
                h('p', { class: 'pa-muted', text: 'Active models on this instance. Copilot validates inputs and proposes runs; you confirm them.' })]),
            button('', 'pa-icon-btn', function () { renderModelsPanel(host); }, 'refresh'),
        ]);
        host.appendChild(head);
        const filter = h('label', { class: 'pa-check' }, [
            h('input', { type: 'checkbox' }),
            h('span', { text: 'Show retired models' }),
        ]);
        host.appendChild(filter);
        const list = h('div', { class: 'pa-model-list' });
        host.appendChild(list);
        let models = [];
        const render = function () {
            clear(list);
            const visible = models.filter(function (model) {
                return filter.querySelector('input').checked || model.active;
            });
            if (!visible.length) {
                list.appendChild(h('p', { class: 'pa-muted', text: 'No model matches.' }));
                return;
            }
            visible.forEach(function (model) {
                const ready = model.active;
                const detail = h('div', { class: 'pa-model-detail', hidden: true });
                const card = h('article', { class: 'pa-model-card' + (ready ? '' : ' is-retired') }, [
                    h('div', { class: 'pa-model-head' }, [
                        h('strong', { text: model.short_name || model.name }),
                        h('span', { class: 'pa-badge ' + (ready ? '' : 'is-off'), text: ready ? 'active' : 'retired' }),
                        h('span', { class: 'pa-model-cost', text: (model.credit_cost || 20) + ' cr' }),
                    ]),
                    h('p', { class: 'pa-model-task', text: (model.task || '').replace(/_/g, ' ') + (model.output_extension ? ' → ' + model.output_extension : '') }),
                    h('p', { class: 'pa-tool-desc', text: model.description || '' }),
                    h('div', { class: 'pa-model-kinds' }, (model.accepts_labels || []).map(function (label) {
                        return h('span', { class: 'pa-kind', text: label });
                    })),
                    h('div', { class: 'pa-model-actions' }, [
                        button('Ask Copilot', 'pa-btn pa-btn-sm', function () {
                            state.stageEls.input.value = 'I want to use ' + (model.short_name || model.name) +
                                '. Ask me for the project and the ' + ((model.accepts_labels || ['input'])[0] || 'input') +
                                ', validate it, and then call propose_job so I can confirm the run.';
                            setTab('chat');
                            focusComposer();
                        }, 'spark'),
                        button('Schema', 'pa-btn pa-btn-sm', async function () {
                            detail.hidden = !detail.hidden;
                            if (!detail.hidden) {
                                detail.textContent = 'Loading…';
                                try {
                                    const response = await api('POST', '/tools/model_input_schema/run', {
                                        arguments: { model_id: model.id }, context: currentContext(),
                                    });
                                    detail.textContent = JSON.stringify(response.result, null, 2);
                                } catch (err) { detail.textContent = err.message; }
                            }
                        }),
                        button('Wizard', 'pa-btn pa-btn-sm', function () {
                            handleAction({ type: 'open_view', view: 'submit-job' });
                        }),
                    ]),
                    detail,
                ]);
                if (!ready && model.replacement) {
                    card.appendChild(h('p', { class: 'pa-muted', text: 'Replacement: ' + model.replacement }));
                }
                list.appendChild(card);
            });
        };
        filter.querySelector('input').addEventListener('change', render);
        list.appendChild(h('p', { class: 'pa-muted', text: 'Loading models…' }));
        api('GET', '/model-catalog').then(function (data) {
            models = data.models || [];
            render();
        }).catch(function (err) { clear(list); list.appendChild(h('p', { class: 'pa-error', text: err.message })); });
    }

    function renderToolsPanel(host) {
        clear(host);
        if (!state.tools.length) {
            host.appendChild(h('p', { class: 'pa-muted', text: 'Loading tools…' }));
            loadTools().then(function () { renderToolsPanel(host); }).catch(function (err) { showError(err.message); });
            return;
        }
        const summary = state.toolSummary || {};
        const head = h('div', { class: 'pa-panel-head' }, [
            h('div', {}, [
                h('h2', { text: 'Tool catalog' }),
                h('p', { class: 'pa-muted', text: (summary.count || state.tools.length) + ' tools · ' + (summary.actions || 0) + ' actions · read-only' }),
            ]),
            button('', 'pa-icon-btn', function () {
                loadTools().then(function () { renderToolsPanel(host); });
            }, 'refresh'),
        ]);
        host.appendChild(head);
        const filters = h('div', { class: 'pa-filters' });
        const search = h('input', { type: 'search', placeholder: 'Filter tools…', class: 'pa-filter-input', 'aria-label': 'Filter tools' });
        const chips = h('div', { class: 'pa-chips' });
        let filter = 'all';
        const render = function () {
            const list = host.querySelector('.pa-tool-groups');
            if (!list) return;
            clear(list);
            const needle = search.value.toLowerCase().trim();
            const groups = {};
            state.tools.forEach(function (tool) {
                if (filter === 'actions' && !tool.action) return;
                if (filter === 'read' && (tool.action || tool.network)) return;
                if (filter === 'web' && !tool.network) return;
                if (filter === 'models' && tool.category !== 'Models') return;
                if (filter === 'bio' && tool.category !== 'Bio') return;
                if (filter === 'run' && tool.category !== 'Run') return;
                if (filter === '3d' && tool.category !== '3D') return;
                if (tool.network && tool.available === false) return;
                if (needle && (tool.name + ' ' + tool.description + ' ' + tool.category).toLowerCase().indexOf(needle) < 0) return;
                (groups[tool.category] = groups[tool.category] || []).push(tool);
            });
            Object.keys(groups).sort().forEach(function (category) {
                const group = h('section', { class: 'pa-tool-group' }, [
                    h('h3', { text: category + ' · ' + groups[category].length }),
                ]);
                groups[category].forEach(function (tool) {
                    const bodyHost = h('div', { class: 'pa-tool-body', hidden: true });
                    const card = h('article', { class: 'pa-tool' }, [
                        h('div', { class: 'pa-tool-title-row' }, [
                            h('span', { class: 'pa-tool-name', text: tool.name }),
                            tool.action ? h('span', { class: 'pa-badge', text: 'action' }) : null,
                            tool.network ? h('span', { class: 'pa-badge is-web', text: 'web' }) : null,
                            h('button', { type: 'button', class: 'pa-icon-btn', 'aria-label': 'Test ' + tool.name,
                                onclick: function () {
                                    bodyHost.hidden = !bodyHost.hidden;
                                    if (!bodyHost.hidden) renderToolTest(tool, bodyHost);
                                } }, [icon('chevron', 16)]),
                        ]),
                        h('p', { class: 'pa-tool-desc', text: tool.description }),
                        bodyHost,
                    ]);
                    group.appendChild(card);
                });
                list.appendChild(group);
            });
            if (!list.children.length) list.appendChild(h('p', { class: 'pa-muted', text: 'No tool matches this filter.' }));
        };
        [['all', 'All'], ['models', 'Models'], ['bio', 'Bio'], ['3d', '3D'], ['run', 'Run'],
         ['read', 'Read'], ['actions', 'Actions'], ['web', 'Web']].forEach(function (item) {
            const chip = button(item[1], 'pa-chip' + (filter === item[0] ? ' is-active' : ''), function () {
                filter = item[0];
                chips.querySelectorAll('.pa-chip').forEach(function (node) { node.classList.remove('is-active'); });
                chip.classList.add('is-active');
                render();
            });
            chips.appendChild(chip);
        });
        search.addEventListener('input', render);
        filters.appendChild(search);
        filters.appendChild(chips);
        host.appendChild(filters);
        host.appendChild(h('div', { class: 'pa-tool-groups' }));
        render();
    }

    // ---------------------------------------------------------------------
    // context
    // ---------------------------------------------------------------------
    function contextLines(context) {
        const out = [];
        if (!context) return out;
        if (context.route) out.push(['View', context.route]);
        if (context.title) out.push(['Title', context.title]);
        if (context.entity) out.push(['Open', context.entity.type + (context.entity.id != null ? ' #' + context.entity.id : '') + (context.entity.label ? ' — ' + context.entity.label : '')]);
        if (context.selection && context.selection.length) {
            out.push(['Selected', context.selection.map(function (item) {
                return item.type + ' #' + item.id + (item.label ? ' — ' + item.label : '');
            }).join(', ')]);
        }
        if (context.settings) {
            Object.keys(context.settings).forEach(function (key) { out.push([key, String(context.settings[key])]); });
        }
        (context.facts || []).forEach(function (fact, index) { out.push(['Fact ' + (index + 1), fact]); });
        return out;
    }


    function renderViewerCard(host, context) {
        const viewer = context && context.viewer;
        const attached = !!(viewer && viewer.attached);
        host.appendChild(h('h3', { class: 'pa-section-title', text: 'Live 3D / sequence' }));
        const card = h('div', { class: 'pa-context-card pa-viewer-card' });
        if (!attached) {
            card.appendChild(h('p', { class: 'pa-muted', text: 'Open a structure in the 3D visualizer or a project. Copilot will attach the full structure and sequence automatically.' }));
        } else {
            const rows = [
                ['Structure', viewer.name || 'Live structure'],
                ['Format', (viewer.format || 'pdb').toUpperCase()],
                ['Size', (viewer.structure_chars || 0).toLocaleString() + ' chars'],
                ['Chains', (viewer.chains || []).map(function (chain) { return chain.chain + ' (' + chain.length + ')'; }).join(', ') || '—'],
                ['Selection', (viewer.selection || []).map(function (item) { return item.chain + item.resi + (item.resname ? ' ' + item.resname : ''); }).join(', ') || 'none'],
                ['Ligand', viewer.ligand || 'none'],
            ];
            if (viewer.job_id) rows.push(['Job', viewer.job_id]);
            rows.forEach(function (pair) {
                card.appendChild(h('div', { class: 'pa-context-row' }, [
                    h('span', { class: 'pa-context-key', text: pair[0] }),
                    h('span', { class: 'pa-context-value', text: String(pair[1]) }),
                ]));
            });
        }
        const attach = h('input', { type: 'checkbox', checked: window.PhonosAgentContext ? window.PhonosAgentContext.viewerAttached() : true });
        attach.addEventListener('change', function () {
            if (window.PhonosAgentContext) window.PhonosAgentContext.setViewerAttach(attach.checked);
            if (!attach.checked) {
                api('DELETE', '/viewer-context').catch(function () { /* ignore */ });
            } else if (window.PhonosAgentContext) {
                window.PhonosAgentContext.publishViewer();
            }
            state.context = currentContext();
            renderContextPanel(host);
        });
        card.appendChild(h('label', { class: 'pa-check' }, [attach, h('span', { text: 'Send full structure + sequence to Copilot' })]));
        host.appendChild(card);
        const actions = h('div', { class: 'pa-viewer-actions' }, [
            button('Refresh snapshot', 'pa-btn', function () {
                if (window.PhonosAgentContext) window.PhonosAgentContext.publishViewer();
                state.context = currentContext();
                renderContextPanel(host);
            }, 'refresh'),
            button('Ask about this structure', 'pa-btn', function () {
                if (state.stageEls.input) {
                    state.stageEls.input.value = 'Look at the structure currently open in the 3D viewer. Summarize its chains, ligands and any selected residues, and suggest what to check next.';
                    setTab('chat');
                    focusComposer();
                }
            }, 'spark'),
        ]);
        host.appendChild(actions);
    }

    function renderContextPanel(host) {
        clear(host);
        const ctx = state.context || currentContext();
        host.appendChild(h('div', { class: 'pa-panel-head' }, [
            h('div', {}, [h('h2', { text: 'Current context' }),
                h('p', { class: 'pa-muted', text: 'Sent with every message; the server sanitises it again.' })]),
            button('', 'pa-icon-btn', function () {
                state.context = currentContext();
                renderContextPanel(host);
            }, 'refresh'),
        ]));
        const card = h('div', { class: 'pa-context-card' });
        contextLines(ctx).forEach(function (pair) {
            card.appendChild(h('div', { class: 'pa-context-row' }, [
                h('span', { class: 'pa-context-key', text: pair[0] }),
                h('span', { class: 'pa-context-value', text: pair[1] }),
            ]));
        });
        if (!card.children.length) card.appendChild(h('p', { class: 'pa-muted', text: 'No page context reported yet.' }));
        host.appendChild(card);
        renderViewerCard(host, ctx);
        const suggestions = (ctx && ctx.suggestions) || [];
        if (suggestions.length) {
            host.appendChild(h('h3', { class: 'pa-section-title', text: 'Start here' }));
            const list = h('div', { class: 'pa-suggestions pa-suggestions-stack' });
            suggestions.forEach(function (item) {
                const node = button('', 'pa-suggestion', function () {
                    if (state.stageEls.input) {
                        state.stageEls.input.value = item.text;
                        setTab('chat');
                        focusComposer();
                    }
                });
                node.appendChild(h('strong', { text: item.title || 'Ask' }));
                node.appendChild(h('span', { text: item.text }));
                list.appendChild(node);
            });
            host.appendChild(list);
        }
        const raw = h('details', { class: 'pa-raw-context' }, [
            h('summary', { text: 'JSON sent to the server' }),
            h('pre', { text: JSON.stringify(ctx, null, 2) }),
        ]);
        host.appendChild(raw);
    }

    // ---------------------------------------------------------------------
    // stats
    // ---------------------------------------------------------------------
    async function renderStatsPanel(host) {
        clear(host);
        const head = h('div', { class: 'pa-panel-head' }, [
            h('div', {}, [h('h2', { text: 'Usage' }), h('p', { class: 'pa-muted', text: 'Provider-reported tokens and cost only; nothing is estimated.' })]),
        ]);
        const days = h('select', { class: 'pa-select', 'aria-label': 'Period' }, [
            h('option', { value: '7', text: '7 days' }),
            h('option', { value: '30', text: '30 days', selected: true }),
            h('option', { value: '90', text: '90 days' }),
            h('option', { value: '365', text: '1 year' }),
            h('option', { value: '0', text: 'All time' }),
        ]);
        head.appendChild(days);
        host.appendChild(head);
        const content = h('div', { class: 'pa-stats' });
        host.appendChild(content);
        const load = async function () {
            clear(content);
            content.appendChild(h('p', { class: 'pa-muted', text: 'Loading…' }));
            try {
                const data = await api('GET', '/stats?days=' + days.value);
                clear(content);
                const totals = data.totals || {};
                const cards = h('div', { class: 'pa-stat-grid' });
                [['Rounds', totals.rounds], ['Input tokens', totals.input_tokens], ['Output tokens', totals.output_tokens],
                 ['Total tokens', totals.total_tokens], ['Reported cost', totals.cost]].forEach(function (pair) {
                    cards.appendChild(h('div', { class: 'pa-stat-card' }, [
                        h('span', { class: 'pa-stat-label', text: pair[0] }),
                        h('strong', { text: String(pair[1] == null ? 0 : pair[1]) }),
                    ]));
                });
                content.appendChild(cards);
                const models = data.by_model || [];
                if (models.length) {
                    const table = h('table', { class: 'pa-table' }, [
                        h('thead', {}, [h('tr', {}, [h('th', { text: 'Model' }), h('th', { text: 'Rounds' }), h('th', { text: 'Tokens' }), h('th', { text: 'Cost' })])]),
                    ]);
                    const tbody = h('tbody');
                    models.forEach(function (row) {
                        tbody.appendChild(h('tr', {}, [
                            h('td', { text: row.model || 'unknown' }),
                            h('td', { text: String(row.rounds || 0) }),
                            h('td', { text: String(row.total_tokens || 0) }),
                            h('td', { text: String(row.cost || 0) }),
                        ]));
                    });
                    table.appendChild(tbody);
                    content.appendChild(table);
                }
                const daysRows = data.by_day || [];
                if (daysRows.length) {
                    const max = Math.max.apply(null, daysRows.map(function (row) { return row.tokens || 0; }).concat([1]));
                    const chart = h('div', { class: 'pa-chart' });
                    daysRows.forEach(function (row) {
                        const bar = h('div', { class: 'pa-chart-bar', title: row.day + ': ' + row.tokens + ' tokens' });
                        bar.style.height = Math.max(2, Math.round((row.tokens / max) * 100)) + '%';
                        chart.appendChild(bar);
                    });
                    content.appendChild(h('h3', { class: 'pa-section-title', text: 'Tokens per day' }));
                    content.appendChild(chart);
                }
            } catch (err) {
                clear(content);
                content.appendChild(h('p', { class: 'pa-error', text: err.message }));
            }
        };
        days.addEventListener('change', load);
        load();
    }

    // ---------------------------------------------------------------------
    // settings
    // ---------------------------------------------------------------------
    function field(label, input, hint) {
        const wrap = h('label', { class: 'pa-field' }, [h('span', { class: 'pa-field-label', text: label })]);
        wrap.appendChild(input);
        if (hint) wrap.appendChild(h('span', { class: 'pa-field-hint', text: hint }));
        return wrap;
    }

    function renderSettingsPanel(host) {
        clear(host);
        const config = state.config;
        if (!config) {
            host.appendChild(h('p', { class: 'pa-muted', text: 'Loading settings…' }));
            loadConfig().then(function () { renderSettingsPanel(host); }).catch(function (err) { showError(err.message); });
            return;
        }
        const settings = config.settings || {};
        const isAdmin = config.user && config.user.is_admin;
        host.appendChild(h('div', { class: 'pa-panel-head' }, [
            h('div', {}, [h('h2', { text: 'Assistant settings' }),
                h('p', { class: 'pa-muted', text: isAdmin ? 'Admins can change the provider. Keys are never shown back.' : 'Only an administrator can change the provider.' })]),
        ]));
        if (settings.env_locked) {
            host.appendChild(h('div', { class: 'pa-notice', text: 'The endpoint or key is set in the server environment; environment values win.' }));
        }
        const form = h('form', { class: 'pa-settings-form' });

        let providerLabel = settings.provider_label || '';
        let keyManaged = !!(settings.key_managed || (providerLabel === 'Fyreflysystems Models'));

        const presets = h('select', { class: 'pa-select', disabled: !isAdmin });
        presets.appendChild(h('option', { value: '', text: 'Choose a preset…' }));
        (config.presets || []).forEach(function (preset) {
            presets.appendChild(h('option', { value: preset.id, text: preset.label }));
        });
        const matchingPreset = (config.presets || []).find(function (preset) {
            return preset.label === providerLabel || (preset.base_url && preset.base_url === settings.base_url);
        });
        if (matchingPreset) presets.value = matchingPreset.id;
        form.appendChild(field('Preset', presets));

        const baseUrl = h('input', { type: 'url', value: settings.base_url || '', disabled: !isAdmin });
        const modelsUrl = h('input', { type: 'url', value: settings.models_url || '', disabled: !isAdmin });
        const modelSelect = h('select', { class: 'pa-select', disabled: !isAdmin });
        const customModel = h('input', { type: 'text', placeholder: 'Custom model id', disabled: !isAdmin, hidden: true });
        const apiKey = h('input', { type: 'password', placeholder: settings.api_key_set ? 'Key is set (' + (settings.api_key_source || '') + ')' : 'Paste an API key', disabled: !isAdmin });
        const keyRow = h('div', { class: 'pa-key-row' });
        const keyNote = h('div', { class: 'pa-notice', text: 'The API key for Fyreflysystems Models is managed on the server. It is used by the assistant and never shown or stored from this form.' });
        const temperature = h('input', { type: 'number', min: '0', max: '2', step: '0.1', value: settings.temperature || '0.2', disabled: !isAdmin });
        const maxSteps = h('input', { type: 'number', min: '1', max: '20', value: settings.max_steps || '8', disabled: !isAdmin });
        const timeout = h('input', { type: 'number', min: '15', max: '110', value: settings.timeout_s || '90', disabled: !isAdmin });
        const history = h('input', { type: 'number', min: '4', max: '100', value: settings.history_messages || '24', disabled: !isAdmin });
        const web = h('input', { type: 'checkbox', checked: !!settings.web_enabled, disabled: !isAdmin });
        const systemPrompt = h('textarea', { rows: '4', disabled: !isAdmin, placeholder: 'Optional operator instruction appended to the system prompt.' });
        systemPrompt.value = settings.system_prompt || '';

        const populateModelSelect = function (current) {
            const value = current || (state.pendingModel || settings.model || '');
            clear(modelSelect);
            const seen = new Set();
            (state.models || []).forEach(function (model) {
                if (!model.id || seen.has(model.id)) return;
                seen.add(model.id);
                modelSelect.appendChild(h('option', { value: model.id, text: model.id }));
            });
            if (value && !seen.has(value)) modelSelect.appendChild(h('option', { value: value, text: value }));
            const custom = h('option', { value: '__custom__', text: 'Custom model id…' });
            modelSelect.appendChild(custom);
            if (value) {
                modelSelect.value = value;
                customModel.value = '';
                customModel.hidden = true;
            } else if (modelSelect.options.length) {
                modelSelect.selectedIndex = 0;
            }
        };
        modelSelect.addEventListener('change', function () {
            customModel.hidden = modelSelect.value !== '__custom__';
            if (!customModel.hidden) customModel.focus();
        });
        populateModelSelect(settings.model);

        const updateKeyVisibility = function () {
            clear(keyRow);
            keyNote.hidden = !keyManaged;
            if (keyManaged) {
                keyRow.appendChild(h('p', { class: 'pa-muted', text: settings.api_key_set ? 'A server-side key is available to Copilot.' : 'No server-side key is configured yet.' }));
            } else {
                keyRow.appendChild(apiKey);
            }
        };
        updateKeyVisibility();

        const applyPreset = function (preset) {
            if (!preset) return;
            baseUrl.value = preset.base_url || '';
            modelsUrl.value = preset.models_url || '';
            providerLabel = preset.label || providerLabel;
            keyManaged = !!(preset.hidden_key || preset.label === 'Fyreflysystems Models');
            populateModelSelect(preset.model || '');
            updateKeyVisibility();
            loadModels(true).then(function () { populateModelSelect(preset.model || ''); }).catch(function () { /* ignore */ });
        };
        presets.addEventListener('change', function () {
            const preset = (config.presets || []).find(function (item) { return item.id === presets.value; });
            applyPreset(preset);
        });

        form.appendChild(field('Base URL', baseUrl, 'OpenAI-compatible endpoint, e.g. Fyreflysystems Models, Gemini, OpenAI or a local Ollama.'));
        form.appendChild(field('Models URL', modelsUrl));
        const modelField = field('Model', modelSelect, 'Loads from the provider model endpoint. Choose “Custom model id…” to type one.');
        modelField.appendChild(customModel);
        form.appendChild(modelField);
        form.appendChild(field('API key', keyRow));
        form.appendChild(keyNote);
        keyNote.hidden = !keyManaged;
        const pair = h('div', { class: 'pa-field-row' }, [
            field('Temperature', temperature), field('Max steps', maxSteps),
        ]);
        form.appendChild(pair);
        const pair2 = h('div', { class: 'pa-field-row' }, [
            field('Timeout (s)', timeout), field('History messages', history),
        ]);
        form.appendChild(pair2);
        const webLabel = h('label', { class: 'pa-check' }, [web, h('span', { text: 'Allow public web search and page fetch' })]);
        form.appendChild(webLabel);
        form.appendChild(h('p', { class: 'pa-muted', text: 'Live mode uses the browser microphone and the browser\'s speech service; audio may leave this device. Read-aloud uses the local browser voice. Do not dictate confidential data.' }));
        form.appendChild(field('Additional system instruction', systemPrompt));

        const status = h('div', { class: 'pa-settings-status' });
        const refreshModels = button('Load models', 'pa-btn', async function () {
            status.textContent = 'Loading models…';
            const current = modelSelect.value === '__custom__' ? customModel.value : modelSelect.value;
            try {
                const models = await loadModels(true);
                populateModelSelect(current);
                status.textContent = (models || []).length + ' model(s) loaded.';
            } catch (err) { status.textContent = err.message; }
        }, 'refresh');
        const test = button('Test connection', 'pa-btn', async function () {
            status.textContent = 'Testing…';
            try {
                const data = await api('POST', '/provider/test', {});
                status.textContent = 'OK · ' + data.models + ' models · key: ' + data.key_source;
            } catch (err) { status.textContent = err.message; }
        }, 'check');
        const clearKey = button('Remove stored key', 'pa-btn pa-btn-danger', async function () {
            if (!window.confirm('Remove the stored API key?')) return;
            await api('PUT', '/settings', { clear_api_key: true });
            await loadConfig();
            renderSettingsPanel(host);
        }, 'trash');
        const save = button('Save', 'pa-btn pa-btn-primary', async function () {
            const selectedModel = modelSelect.value === '__custom__' ? customModel.value.trim() : modelSelect.value;
            const payload = {
                provider_label: providerLabel,
                base_url: baseUrl.value,
                models_url: modelsUrl.value,
                model: selectedModel,
                temperature: temperature.value,
                max_steps: maxSteps.value,
                timeout_s: timeout.value,
                history_messages: history.value,
                web_enabled: web.checked,
                system_prompt: systemPrompt.value,
            };
            if (!keyManaged && apiKey.value.trim()) payload.api_key = apiKey.value.trim();
            status.textContent = 'Saving…';
            try {
                const data = await api('PUT', '/settings', payload);
                state.config.settings = data.settings;
                keyManaged = !!data.settings.key_managed;
                providerLabel = data.settings.provider_label || providerLabel;
                status.textContent = 'Saved.';
                await loadConfig();
                await loadModels(true);
                populateModelSelect(data.settings.model || '');
                renderModelPicker();
                status.textContent = 'Saved · ' + (state.models || []).length + ' model(s) available.';
            } catch (err) { status.textContent = err.message; }
        }, 'check');
        save.disabled = !isAdmin;
        clearKey.disabled = !isAdmin;
        form.appendChild(h('div', { class: 'pa-settings-actions' }, [save, test, refreshModels, clearKey]));
        form.appendChild(status);
        host.appendChild(form);
    }

    // ---------------------------------------------------------------------
    // shared navigation
    // ---------------------------------------------------------------------
    function setTab(tab) {
        state.tab = tab;
        try { localStorage.setItem(STORE_TAB, tab); } catch (err) { /* ignore */ }
        Object.keys(state.stageEls.tabs || {}).forEach(function (key) {
            const btn = state.stageEls.tabs[key];
            if (btn) btn.classList.toggle('is-active', key === tab);
        });
        Object.keys(state.stageEls.panels || {}).forEach(function (key) {
            const panel = state.stageEls.panels[key];
            if (panel) panel.hidden = key !== tab;
        });
        const panel = state.stageEls.panels && state.stageEls.panels[tab];
        if (panel) {
            if (tab === 'tools') renderToolsPanel(panel);
            else if (tab === 'runs') renderRunsPanel(panel);
            else if (tab === 'models') renderModelsPanel(panel);
            else if (tab === 'stats') renderStatsPanel(panel);
            else if (tab === 'settings') renderSettingsPanel(panel);
            else if (tab === 'context') renderContextPanel(panel);
        }
    }

    function tabButton(key, label) {
        const btn = button(label, 'pa-tab' + (state.tab === key ? ' is-active' : ''), function () { setTab(key); });
        btn.dataset.tab = key;
        if (!state.stageEls.tabs) state.stageEls.tabs = {};
        state.stageEls.tabs[key] = btn;
        return btn;
    }

    function tabPanel(key, content) {
        if (!state.stageEls.panels) state.stageEls.panels = {};
        const panel = h('div', { class: 'pa-tab-panel' }, [content]);
        panel.hidden = state.tab !== key;
        state.stageEls.panels[key] = panel;
        return panel;
    }

    function renderChatLists() {
        (state.chatListEls || []).forEach(function (host) {
            clear(host);
            if (!state.chats.length) {
                host.appendChild(h('p', { class: 'pa-muted pa-chat-empty', text: 'No conversations yet.' }));
                return;
            }
            state.chats.forEach(function (chat) {
                const item = h('div', { class: 'pa-chat-item' + (state.chat && state.chat.id === chat.id ? ' is-active' : '') }, [
                    h('button', { type: 'button', class: 'pa-chat-open', onclick: function () { openChat(chat.id); } }, [
                        h('span', { class: 'pa-chat-title', text: chat.title || 'New chat' }),
                        h('span', { class: 'pa-chat-meta', text: (chat.message_count || 0) + ' msg · ' + relativeTime(chat.last_message_at || chat.created_at) }),
                    ]),
                    h('div', { class: 'pa-chat-actions' }, [
                        h('button', { type: 'button', class: 'pa-icon-btn', 'aria-label': 'Rename', onclick: function (event) { event.stopPropagation(); renameChat(chat); } }, [icon('chat', 14)]),
                        h('button', { type: 'button', class: 'pa-icon-btn is-danger', 'aria-label': 'Delete', onclick: function (event) { event.stopPropagation(); deleteChat(chat); } }, [icon('trash', 14)]),
                    ]),
                ]);
                host.appendChild(item);
            });
        });
    }

    function openAgentPage() {
        const ctx = currentContext();
        const params = new URLSearchParams();
        if (ctx.route) params.set('from', ctx.route);
        if (ctx.title) params.set('title', ctx.title);
        if (ctx.entity) {
            params.set('entity', ctx.entity.type || '');
            params.set('id', String(ctx.entity.id != null ? ctx.entity.id : ''));
        }
        if (ctx.settings && ctx.settings.project_id) params.set('project', ctx.settings.project_id);
        window.open('/agent?' + params.toString(), '_blank', 'noopener');
    }

    // ---------------------------------------------------------------------
    // panel (inside the SPA)
    // ---------------------------------------------------------------------
    function mountPanel() {
        if (!ROOT) return;
        const launcher = h('button', { type: 'button', class: 'pa-launcher', id: 'pa-launcher',
            'aria-label': 'Open Phonos Copilot', title: 'Phonos Copilot (Ctrl+Shift+A)' }, [icon('spark', 22)]);
        const panel = h('aside', { class: 'pa-panel', id: 'pa-panel', hidden: true, 'aria-label': 'Phonos Copilot' });
        const header = h('header', { class: 'pa-panel-header' }, [
            h('div', { class: 'pa-panel-title' }, [icon('spark', 18), h('strong', { text: 'Copilot' }), h('span', { class: 'pa-beta', text: 'tools' })]),
            h('div', { class: 'pa-panel-header-actions' }, [
                button('', 'pa-icon-btn', openAgentPage, 'external'),
                button('', 'pa-icon-btn', function () { closePanel(); }, 'close'),
            ]),
        ]);
        const tabs = h('nav', { class: 'pa-tabs' }, [
            tabButton('chat', 'Chat'),
            tabButton('models', 'Models'),
            tabButton('chats', 'Chats'),
            tabButton('runs', 'Runs'),
            tabButton('tools', 'Tools'),
            tabButton('context', 'Context'),
            tabButton('stats', 'Usage'),
            tabButton('settings', 'Settings'),
        ]);
        const chatPanel = tabPanel('chat', buildChatPanel());
        const chatsHost = h('div', { class: 'pa-panel-chats' });
        state.chatListEls.push(chatsHost);
        const chatsPanel = tabPanel('chats', h('div', { class: 'pa-panel-chats-wrap' }, [
            h('div', { class: 'pa-panel-head' }, [
                h('div', {}, [h('h2', { text: 'Conversations' }),
                    h('p', { class: 'pa-muted', text: 'Each chat belongs to your account.' })]),
                button('New', 'pa-btn pa-btn-primary pa-btn-sm', newChat, 'plus'),
            ]),
            chatsHost,
        ]));
        const modelsPanel = tabPanel('models', h('div'));
        const runsPanel = tabPanel('runs', h('div'));
        const toolsPanel = tabPanel('tools', h('div'));
        const contextPanel = tabPanel('context', h('div'));
        const statsPanel = tabPanel('stats', h('div'));
        const settingsPanel = tabPanel('settings', h('div'));
        const body = h('div', { class: 'pa-panel-body' }, [chatPanel, chatsPanel, modelsPanel, runsPanel, toolsPanel, contextPanel, statsPanel, settingsPanel]);
        panel.appendChild(header);
        panel.appendChild(tabs);
        panel.appendChild(body);
        ROOT.appendChild(launcher);
        ROOT.appendChild(panel);

        const toggle = function () {
            if (panel.hidden) openPanel(); else closePanel();
        };
        launcher.addEventListener('click', toggle);
        document.querySelectorAll('[data-agent-open]').forEach(function (node) {
            node.addEventListener('click', function (event) {
                event.preventDefault();
                if (MODE === 'panel') toggle(); else window.location.href = '/agent';
            });
        });
        document.addEventListener('keydown', function (event) {
            if (event.ctrlKey && event.shiftKey && (event.key === 'A' || event.key === 'a')) {
                event.preventDefault();
                toggle();
            }
            if (event.key === 'Escape' && !panel.hidden) closePanel();
        });
        window.addEventListener('resize', function () {
            if (window.innerWidth < 720 && !panel.hidden) {
                panel.classList.add('is-mobile');
            } else {
                panel.classList.remove('is-mobile');
            }
        });
        state.stageEls.panel = panel;
        state.stageEls.launcher = launcher;

        // First load in the background so the context/tools are ready when opened.
        Promise.all([loadConfig(), loadChats(), loadTools()]).then(function () {
            state.context = currentContext();
            renderMessages();
            renderModelPicker();
            renderAutoRun();
            loadModels();
            const last = Number(localStorage.getItem(STORE_CHAT));
            if (last) openChat(last, { focus: false }).catch(function () { /* ignore */ });
            renderContextPanel(contextPanel);
        }).catch(function (err) {
            showError(err.message);
        });
        setTab(['chat', 'models', 'runs', 'chats', 'tools', 'context', 'stats', 'settings'].indexOf(state.tab) >= 0 ? state.tab : 'chat');
    }

    function openPanel() {
        const panel = state.stageEls.panel;
        if (!panel) return;
        panel.hidden = false;
        state.stageEls.launcher.classList.add('is-open');
        state.context = currentContext();
        if (state.tab === 'context') renderContextPanel(state.stageEls.panels.context);
        focusComposer();
    }

    function closePanel() {
        const panel = state.stageEls.panel;
        if (!panel) return;
        panel.hidden = true;
        state.stageEls.launcher.classList.remove('is-open');
    }

    // ---------------------------------------------------------------------
    // page (/agent)
    // ---------------------------------------------------------------------
    function mountPage() {
        if (!ROOT) return;
        const sidebar = h('aside', { class: 'pa-side' }, [
            h('div', { class: 'pa-side-head' }, [
                h('div', { class: 'pa-side-brand' }, [icon('spark', 20), h('strong', { text: 'Copilot' })]),
                button('New', 'pa-btn pa-btn-primary pa-btn-sm', newChat, 'plus'),
            ]),
            h('div', { class: 'pa-side-section-title', text: 'Recents' }),
            h('div', { class: 'pa-side-list', id: 'pa-chat-list' }),
            h('div', { class: 'pa-side-foot' }, [
                h('span', { class: 'pa-muted', text: 'Tools are read-only.' }),
                h('a', { href: '/dashboard', class: 'pa-link', text: 'Back to workspace' }),
            ]),
        ]);
        const chatPanel = tabPanel('chat', buildChatPanel());
        const modelsPanel = tabPanel('models', h('div'));
        const runsPanel = tabPanel('runs', h('div'));
        const toolsPanel = tabPanel('tools', h('div'));
        const contextPanel = tabPanel('context', h('div'));
        const statsPanel = tabPanel('stats', h('div'));
        const settingsPanel = tabPanel('settings', h('div'));
        const topbar = h('header', { class: 'pa-topbar' }, [
            h('div', { class: 'pa-topbar-title' }, [h('h1', { text: 'Phonos Copilot' }),
                h('span', { class: 'pa-muted', text: 'Cowork with your projects, models and 3D context' })]),
            h('nav', { class: 'pa-tabs' }, [
                tabButton('chat', 'Chat'), tabButton('models', 'Models'), tabButton('runs', 'Runs'),
                tabButton('tools', 'Tools'), tabButton('context', 'Context'),
                tabButton('stats', 'Usage'), tabButton('settings', 'Settings'),
            ]),
            h('a', { href: '/dashboard', class: 'pa-btn', text: 'Workspace' }),
        ]);
        const main = h('main', { class: 'pa-main' }, [topbar,
            h('div', { class: 'pa-main-body' }, [chatPanel, modelsPanel, runsPanel, toolsPanel, contextPanel, statsPanel, settingsPanel])]);
        const rail = h('aside', { class: 'pa-rail' }, [
            h('div', { class: 'pa-rail-head' }, [h('h2', { text: 'Context' }),
                button('', 'pa-icon-btn', function () {
                    state.context = currentContext();
                    renderContextPanel(contextPanel);
                    renderRail();
                }, 'refresh')]),
            h('div', { class: 'pa-rail-body', id: 'pa-rail-body' }),
        ]);
        const stage = h('div', { class: 'pa-shell' }, [sidebar, main, rail]);
        ROOT.appendChild(stage);
        state.chatListEls.push(document.getElementById('pa-chat-list'));
        setTab(state.tab);
        renderRail();

        Promise.all([loadConfig(), loadChats(), loadTools()]).then(function () {
            state.context = currentContext();
            renderMessages();
            renderModelPicker();
            renderAutoRun();
            loadModels();
            const params = new URLSearchParams(window.location.search);
            const last = Number(params.get('chat') || localStorage.getItem(STORE_CHAT));
            if (last) openChat(last, { focus: false }).catch(function () { /* ignore */ });
            state.context = currentContext();
            renderContextPanel(contextPanel);
            renderRail();
        }).catch(function (err) { showError(err.message); });
    }

    function renderRail() {
        const host = document.getElementById('pa-rail-body');
        if (!host) return;
        clear(host);
        const ctx = state.context || currentContext();
        const card = h('div', { class: 'pa-context-card' });
        contextLines(ctx).forEach(function (pair) {
            card.appendChild(h('div', { class: 'pa-context-row' }, [
                h('span', { class: 'pa-context-key', text: pair[0] }),
                h('span', { class: 'pa-context-value', text: pair[1] }),
            ]));
        });
        if (!card.children.length) card.appendChild(h('p', { class: 'pa-muted', text: 'Open a project, job or file in the workspace, then reopen this page to carry the context over.' }));
        host.appendChild(card);
        if (state.chat) {
            host.appendChild(h('div', { class: 'pa-rail-meta' }, [
                h('span', { text: state.chat.title || 'New chat' }),
                h('span', { text: (state.chat.message_count || state.messages.length) + ' messages' }),
            ]));
        }
        const suggestions = (ctx && ctx.suggestions) || [];
        if (suggestions.length) {
            host.appendChild(h('h3', { class: 'pa-section-title', text: 'Start here' }));
            const list = h('div', { class: 'pa-suggestions pa-suggestions-stack' });
            suggestions.forEach(function (item) {
                const node = button('', 'pa-suggestion', function () {
                    if (state.stageEls.input) {
                        state.stageEls.input.value = item.text;
                        setTab('chat');
                        focusComposer();
                    }
                });
                node.appendChild(h('strong', { text: item.title || 'Ask' }));
                node.appendChild(h('span', { text: item.text }));
                list.appendChild(node);
            });
            host.appendChild(list);
        }
        host.appendChild(h('div', { class: 'pa-rail-actions' }, [
            button('What can you do here?', 'pa-btn', function () {
                if (state.stageEls.input) {
                    state.stageEls.input.value = 'What can you do here? Use the current view as context.';
                    setTab('chat');
                    focusComposer();
                }
            }, 'spark'),
            button('Summarize this page', 'pa-btn', function () {
                if (state.stageEls.input) {
                    state.stageEls.input.value = 'Summarize what is on this page and what I should pay attention to.';
                    setTab('chat');
                    focusComposer();
                }
            }, 'bolt'),
        ]));

        // Gimel-style Discovery Playbooks / Routines
        const routinesWrap = h('div', { class: 'pa-routines-section', style: 'margin-top:12px;' });
        routinesWrap.appendChild(h('h3', { class: 'pa-section-title', text: 'Discovery Routines' }));
        const routinesList = h('div', { class: 'pa-suggestions pa-suggestions-stack' });
        routinesWrap.appendChild(routinesList);
        host.appendChild(routinesWrap);
        fetch('/api/agent/routines', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (payload) {
                const items = (payload && payload.routines) || [];
                items.forEach(function (rt) {
                    const node = button('', 'pa-suggestion', function () {
                        if (state.stageEls.input) {
                            state.stageEls.input.value = rt.prompt;
                            setTab('chat');
                            focusComposer();
                        }
                    });
                    node.appendChild(h('strong', { text: '[' + (rt.category || 'Bio') + '] ' + rt.title }));
                    node.appendChild(h('span', { text: rt.prompt.slice(0, 110) + '…' }));
                    routinesList.appendChild(node);
                });
            })
            .catch(function () {});
    }

    // Global bridge for 3D Viewer & Notebook IDE 1-click Copilot prompts
    window.PhonosCopilotAsk = function (promptText) {
        try {
            const openBtn = document.querySelector('[data-agent-open]');
            if (openBtn && MODE !== 'page') openBtn.click();
            setTimeout(function () {
                if (state.stageEls && state.stageEls.input) {
                    state.stageEls.input.value = String(promptText || '');
                    setTab('chat');
                    focusComposer();
                }
            }, 120);
        } catch (_) {}
    };

    // ---------------------------------------------------------------------
    // boot
    // ---------------------------------------------------------------------
    function boot() {
        if (!ROOT) return;
        if (MODE === 'page') mountPage();
        else mountPanel();
        consumePendingAction();
        if (window.PhonosAgentContext) {
            window.PhonosAgentContext.subscribe(function (context) {
                state.context = context;
                renderContextBar();
                if (!state.chat && !state.messages.length) renderMessages();
                if (state.tab === 'context' && state.stageEls.panels && state.stageEls.panels.context) {
                    renderContextPanel(state.stageEls.panels.context);
                }
            });
        }
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
