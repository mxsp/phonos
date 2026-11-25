/**
 * Phonos Notebook IDE v8.0
 * Features: CodeMirror Integration, Markdown Rendering, Zinc UI, Robust Kernel,
 *           Command Palette, Outline Panel, Live Execution Timer, Enhanced Status Bar
 */

(function() {
'use strict';

// ============================================================================ 
// CONFIG & CONSTANTS
// ============================================================================ 

const CONFIG = {
    autoSaveInterval: 30000,
    codeMirrorDefaults: {
        theme: 'dracula',
        lineNumbers: true,
        lineWrapping: true,
        indentUnit: 4,
        smartIndent: true,
        tabSize: 4,
        indentWithTabs: false,
        matchBrackets: true,
        autoCloseBrackets: true,
        styleActiveLine: true,
        scrollbarStyle: 'simple',
        extraKeys: {
            'Tab': function(cm) {
                if (cm.somethingSelected()) {
                    cm.indentSelection("add");
                } else {
                    cm.replaceSelection("    ", "end");
                }
            },
            'Shift-Tab': function(cm) {
                cm.indentSelection("subtract");
            }
        }
    }
};

// ============================================================================
// MODELS
// ============================================================================

// Every cell type that executes on Run / Run All (everything except
// markdown/raw). C/C++/Rust/R run inside the kernel container via phonos.lang.
const NB_EXEC_TYPES = ['code', 'wolfram', 'quantum', 'c', 'cpp', 'rust', 'r'];
function isExecutableType(type) { return NB_EXEC_TYPES.includes(type); }

// Wolfram Language CodeMirror mode (simple syntax highlighting)
try {
    if (typeof CodeMirror !== 'undefined' && CodeMirror.defineSimpleMode) {
        CodeMirror.defineSimpleMode('wolfram', {
            start: [
                {regex: /"(?:[^\\"]|\\.)*"/, token: 'string'},
                {regex: /'(?:[^'\\]|\\.)*'/, token: 'string'},
                {regex: /\(\*[\s\S]*?\*\)/, token: 'comment'},
                {regex: /\/\/.*/, token: 'comment'},
                {regex: /\b(True|False|None|Null|All|Automatic|Infinity)\b/, token: 'builtin'},
                {regex: /\b(If|Then|Else|While|For|Do|Module|Block|With|Function|Map|Apply|Table|Range|Length|Part|Select|Cases|Sort|Flatten|Join|Append|Prepend|Insert|Delete|Take|Drop|Position|Count|MemberQ|FreeQ|MatchQ|ToString|ToExpression|Import|Export|Print|Return|Break|Continue|Catch|Throw|Check|Abort)\b/, token: 'keyword'},
                {regex: /\b(Plot|Plot3D|ListPlot|ListLinePlot|BarChart|PieChart|Histogram|ContourPlot|DensityPlot|Show|Graphics|Image|Manipulate|Animate|ParametricPlot|RegionPlot|StreamPlot)\b/, token: 'keyword'},
                {regex: /\b(Sin|Cos|Tan|ArcSin|ArcCos|ArcTan|Sinh|Cosh|Tanh|Exp|Log|Log10|Sqrt|Abs|Round|Floor|Ceiling|Max|Min|Total|Mean|Median|Variance|StandardDeviation|Norm|Conjugate|Re|Im|Arg|Mod|Quotient|GCD|LCM|Power|Times|Plus|Subtract|Divide|Derivative|Integrate|D|DSolve|NDSolve|Solve|NSolve|FindRoot|Minimize|Maximize|LinearProgramming|Fit|FindFit|Interpolation|Simplify|FullSimplify|Expand|Factor|Together|Apart|Cancel|TrigReduce|TrigExpand)\b/, token: 'atom'},
                {regex: /\b(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)\b/, token: 'number'},
                {regex: /[a-zA-Z_$][\w$]*/, token: 'variable'},
                {regex: /<-|->|:=|==|=|!=|<|>|<=|>=|\+|-|\*|\/|\^|\|\||&&|!|;|,|\.\./, token: 'operator'},
                {regex: /[{}()\[\]]/, token: 'bracket'},
            ]
        });
    }
} catch(e) {
    // Wolfram mode not available, fallback to plain text
}

class Notebook {
    constructor(data) {
        this.id = data.id || null;
        this.name = data.name || 'Untitled Notebook';
        this.project_id = data.project_id || null;
        this.cells = (data.cells || []).map(c => new Cell(c));
        this.settings = data.settings || {};
        this.dirty = false;
    }

    addCell(type, source = '', afterId = null) {
        const cell = new Cell({ type, source });
        if (afterId) {
            const idx = this.cells.findIndex(c => c.id === afterId);
            if (idx !== -1) this.cells.splice(idx + 1, 0, cell);
            else this.cells.push(cell);
        } else {
            this.cells.push(cell);
        }
        this.dirty = true;
        return cell;
    }

    removeCell(id) {
        const idx = this.cells.findIndex(c => c.id === id);
        if (idx !== -1 && this.cells.length > 1) {
            this.cells.splice(idx, 1);
            this.dirty = true;
            return true;
        }
        return false;
    }

    getCell(id) { return this.cells.find(c => c.id === id); } 
    
    toJSON() {
        return {
            id: this.id,
            name: this.name,
            project_id: this.project_id,
            cells: this.cells.map(c => c.toJSON()),
            settings: this.settings
        };
    }
}

class Cell {
    constructor(data) {
        this.id = data.id || 'cell-' + Date.now() + Math.random().toString(36).substr(2, 5);
        this.type = data.type || 'code'; // 'code' | 'markdown' | 'wolfram' | 'quantum' | 'c' | 'cpp' | 'rust' | 'r'
        this.source = data.source || '';
        this.outputs = data.outputs || [];
        this.metadata = data.metadata || {};
        this.execution_count = data.execution_count || null;
        this.cm = null; 
    }

    toJSON() {
        if (this.cm) this.source = this.cm.getValue();
        return {
            id: this.id,
            type: this.type,
            source: this.source,
            outputs: this.outputs,
            execution_count: this.execution_count,
            metadata: this.metadata
        };
    }

    addOutput(out) { this.outputs.push(out); } 
    clearOutputs() { this.outputs = []; this.execution_count = null; } 
}

// ============================================================================ 
// NOTEBOOK MODULE
// ============================================================================ 

// Translate a notebook.* key. i18n.t() returns the bare key when the key is
// missing or the locale has not finished loading, which used to leak raw text
// such as "notebook.kernelDisconnected" into the UI. Fall back to the English text.
function NbT(key, fallback) {
    var full = 'notebook.' + key;
    try {
        if (window.i18n && typeof window.i18n.t === 'function') {
            var v = window.i18n.t(full);
            if (v && v !== full) return v;
        }
    } catch (e) { /* fall through */ }
    return fallback == null ? '' : String(fallback);
}
window.NbT = NbT;

window.NotebookModule = {
    notebook: null,
    kernel: null,
    activeCellId: null,
    projects: [],
    notebookSearchTerm: '',
    _sidebarPanelCache: {},
    _sidebarPanelInFlight: {},
    _sidebarPanelTtlMs: {
        files: 15000,
        notebooks: 30000,
        kernels: 8000,
        // models: 30000, // hidden for now
        settings: 45000
    },
    
    // Tab system state
    _tabs: [],
    _activeTabId: null,

    // File / notebook manager selection state
    _selectedFiles: new Map(), // fileId -> file object
    _selectedNotebooks: new Map(), // notebookId -> notebook object
    _lastSelectedFileIndex: -1,
    _lastSelectedNotebookIndex: -1,

    init: async function(opts) {
        console.log('Initializing Notebook v8.0', opts);
        this.projectId = opts.projectId;
        this.notebookId = opts.notebookId;

        // Eagerly hide the welcome screen and close any leftover split panel
        // the moment we know a notebook will load — BEFORE the network
        // awaits below. Previously #welcome-state stayed visible (CSS
        // display:flex) through loadProjects()+loadNotebook() round-trips
        // until render() wiped it, causing a visible welcome/notebook
        // overlap on load. Also defends against bfcache-restored split panels.
        if (this.notebookId && this.notebookId !== 'undefined' && this.notebookId !== 'null') {
            var ws = document.getElementById('welcome-state');
            if (ws) ws.style.display = 'none';
            if (typeof this.closeSplitPanel === 'function') this.closeSplitPanel();
        }

        // Restore persisted project selection if not specified in URL
        if (!this.projectId) {
            try {
                var saved = localStorage.getItem('phonos-notebook-project');
                if (saved) this.projectId = saved;
            } catch (e) {}
        }
        
        this.kernel = new KernelManager(this);
        this.terminal = new TerminalConnection(this);

        // Initialise the kernel button so the dot has the correct class and
        // the text reads "Disconnected" instead of the template default
        // "No Kernel" (the template's data-i18n default).
        if (this.kernel && typeof this.kernel.updateUI === 'function') {
            this.kernel.updateUI('disconnected');
        }

        // Load Projects
        await this.loadProjects();
        this.setupProjectSelector();
        
        // Load Notebook
        if (this.notebookId && this.notebookId !== 'undefined' && this.notebookId !== 'null') {
            await this.loadNotebook(this.notebookId);
            this._registerPrimaryTab();
            // Make sure the welcome screen is never shown once a notebook is
            // loaded, and that no leftover split-panel preview from a previous
            // session steals layout space.
            const welcomeState = document.getElementById('welcome-state');
            if (welcomeState) welcomeState.style.display = 'none';
            if (typeof this.closeSplitPanel === 'function') this.closeSplitPanel();
        } else {
            // Show welcome screen (template starts hidden)
            var ws = document.getElementById('welcome-state');
            if (ws) ws.style.display = 'flex';
        }

        this.setupUI();
        this._restoreActivityOrder();
        this.setupEventListeners();

        // Initialise education mode if this is an education project
        if (this.notebook && this.notebook.project_id) {
            this._initEducationMode();
        }

        // Initialize the full-screen Documentation reader
        if (window.NotebookDocs) {
            NotebookDocs.init();
        }

        // Initialize History Manager
        if (this.notebookId && this.notebookId !== 'undefined' && this.notebookId !== 'null') {
            HistoryManager.init(this.notebookId);
        }

        // Initialize Collaboration Manager
        CollaborationManager.init(this.notebookId);

        // Re-render the Wolfram account panel once i18n translations finish
        // loading — otherwise badges / status text fall through to literal
        // "notebook.wolframBadgeConfigured"-style strings.
        if (!this._wolframI18nBound) {
            this._wolframI18nBound = true;
            var self = this;
            var rerenderWolfram = function() { self.refreshWolframAccount(true); };
            window.addEventListener('i18n:ready', rerenderWolfram);
            window.addEventListener('i18n:change', rerenderWolfram);
            if (window.i18n && typeof window.i18n.onChange === 'function') {
                window.i18n.onChange(rerenderWolfram);
            }
        }

        // Wolfram account gating: the account panel starts collapsed (see
        // templates/notebook.html #nb-rightbar). Fetch the configured state
        // on load (no auto-open: the drawer is opened by the user).
        // Fire-and-forget so it never blocks switchTab / CodeMirror refresh.
        if (typeof this.toggleAccountPanel === 'function' && typeof this.refreshWolframAccount === 'function') {
            this._initWolframAccountGate();
            this.updateStatusBar();
        }
        // Quantum: load settings on init (fire-and-forget). No auto-prompt.
        if (typeof this.refreshQuantumAccount === 'function') {
            this._initQuantumAccountGate();
        }

        // Activate the notebook tab — switchTab() manages container display,
        // refreshes CodeMirror editors (fixes zero-width layout on first load),
        // and updates the URL.  Must run after setupUI() so tab bar exists.
        if (this._activeTabId) {
            this.switchTab(this._activeTabId);
        }

        // AutoSave
        setInterval(() => this.autoSave(), CONFIG.autoSaveInterval);
    },

    loadProjects: async function() {
        try {
            const res = await fetch('/api/projects');
            const data = await res.json();
            // API returns an array directly
            this.projects = Array.isArray(data) ? data : (data.projects || []);
        } catch (e) {
            console.error('Failed to load projects', e);
            this.projects = [];
        }
    },

    setupProjectSelector: function() {
        const select = document.getElementById('project-selector');
        if (!select) return;

        select.innerHTML = '<option value="">' + (window.i18n ? window.i18n.t('notebook.selectProject') : 'Select Project...') + '</option>';
        this.projects.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.name;
            select.appendChild(opt);
        });

        // Auto-select first project if none selected
        if (!this.projectId && this.projects.length > 0) {
            this.projectId = this.projects[0].id;
        }

        if (this.projectId) {
            select.value = this.projectId;
            this.updateMetaProject();
        }

        // Persist initial project selection
        try { localStorage.setItem('phonos-notebook-project', this.projectId || ''); } catch (e) {}

        select.addEventListener('change', (e) => {
            this.projectId = e.target.value;
            this.updateMetaProject();
            this._invalidateSidebarPanelCache();
            this.refreshFileExplorer(true);
            try { localStorage.setItem('phonos-notebook-project', this.projectId || ''); } catch (e) {}
        });
        
        // Trigger initial refresh
        if (this.projectId) {
            this.refreshFileExplorer(true);
        }
    },

    updateMetaProject: function() {
        const el = document.getElementById('meta-project');
        if (!el) return;
        if (!this.projectId) {
            el.textContent = window.i18n ? window.i18n.t('notebook.personal') : 'Personal';
            return;
        }
        const p = this.projects.find(x => x.id == this.projectId);
        if (p) el.textContent = p.name;
    },

    _getSidebarPanelContextKey: function(panelKey) {
        if (['files', 'notebooks', 'kernels'/*, 'models'*/].includes(panelKey)) {
            return String(this.projectId || '');
        }
        return 'global';
    },

    _getSidebarPanelCache: function(panelKey) {
        const cache = this._sidebarPanelCache[panelKey];
        if (!cache) return null;
        if (cache.contextKey !== this._getSidebarPanelContextKey(panelKey)) return null;
        const ttl = this._sidebarPanelTtlMs[panelKey] || 15000;
        if ((Date.now() - cache.updatedAt) > ttl) return null;
        return cache.payload;
    },

    _setSidebarPanelCache: function(panelKey, payload) {
        this._sidebarPanelCache[panelKey] = {
            contextKey: this._getSidebarPanelContextKey(panelKey),
            updatedAt: Date.now(),
            payload: payload
        };
    },

    _invalidateSidebarPanelCache: function(panelKey) {
        if (!panelKey) {
            this._sidebarPanelCache = {};
            return;
        }
        delete this._sidebarPanelCache[panelKey];
    },

    loadNotebook: async function(id) {
        try {
            console.log('Loading notebook:', id);
            const res = await fetch(`/api/notebooks/${id}`);
            
            if (!res.ok) {
                const contentType = res.headers.get('content-type');
                let errorMsg = window.i18n ? window.i18n.t('notebook.failedToLoadNotebook') : 'Failed to load notebook';
                
                if (contentType && contentType.includes('application/json')) {
                    const errorData = await res.json().catch(() => ({}));
                    errorMsg = errorData.error || errorData.message || errorMsg;
                } else {
                    errorMsg = `HTTP ${res.status}: ${res.statusText}`;
                }
                
                throw new Error(errorMsg);
            }
            
            const contentType = res.headers.get('content-type');
            if (!contentType || !contentType.includes('application/json')) {
                const text = await res.text();
                console.error('Unexpected response:', text.substring(0, 200));
                throw new Error(window.i18n ? window.i18n.t('notebook.invalidResponse') : 'Invalid response from server');
            }
            
            const data = await res.json();
            console.log('Notebook data received:', data);
            
            if (!data.notebook) {
                throw new Error(window.i18n ? window.i18n.t('notebook.notebookDataMissing') : 'Notebook data is missing');
            }
            
            this.notebook = new Notebook(data.notebook);
            
            const nameInput = document.getElementById('notebook-name');
            if (nameInput) nameInput.value = this.notebook.name;
            
            this.render();

            this.notebookId = id;

            // Re-initialise education mode after a reload (e.g. after a teacher
            // saves a problem) so badges, the education toolbar, the task cache
            // and student status panels are rebuilt for the freshly-rendered cells.
            if (this.notebook && this.notebook.project_id) {
                this._initEducationMode();
            }

            // Auto-select the notebook's project in the sidebar so the
            // file/notebook panels don't show empty "select a project" states.
            var nbProjectId = data.notebook && data.notebook.project_id;
            if (nbProjectId && nbProjectId !== this.projectId) {
                this.projectId = String(nbProjectId);
                var select = document.getElementById('project-select');
                if (select) select.value = this.projectId;
                try { localStorage.setItem('phonos-notebook-project', this.projectId); } catch (e) {}
                if (typeof this.refreshNotebooksList === 'function') this.refreshNotebooksList();
                if (typeof this.refreshFileExplorer === 'function') this.refreshFileExplorer();
            }
            
            // Check for existing active session
            if (data.active_session) {
                console.log('Found active session, auto-connecting...', data.active_session);
                // We can just call connect() which will hit start() and get the existing session
                // Or we could manually set it if we wanted to avoid the API call, but connect() sets up the socket.
                // connect() handles the UI updates too.
                this.kernel.connect(id).then(() => {
                    this.showToast(window.i18n ? window.i18n.t('notebook.resumedSession') : 'Resumed active session', 2000);
                });
            }

            // Check if this notebook is linked to a source .nb file on disk
            this._checkSourceStatus(id);
            
        } catch (e) {
            console.error('Error loading notebook:', e);
            this.showToast((window.i18n ? window.i18n.t('notebook.errorLoadingNotebook') : 'Error loading notebook: ') + e.message, 5000);
            
            const container = document.getElementById('cells-container');
            if (container) {
                var errorTitle = window.i18n ? window.i18n.t('notebook.failedToLoadNotebookTitle') : 'Failed to Load Notebook';
                var retryText = window.i18n ? window.i18n.t('notebook.retry') : 'Retry';
                var backText = window.i18n ? window.i18n.t('notebook.backToDashboard') : 'Back to Dashboard';
                container.innerHTML = `
                    <div class="nb-empty-state" style="padding: 60px 20px;">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:48px;height:48px;margin-bottom:16px;color:#ef4444;">
                            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                        </svg>
                        <h3 style="color:#f4f4f5;margin-bottom:8px;">${errorTitle}</h3>
                        <p style="color:#a1a1aa;margin-bottom:16px;">${this.escapeHtml(e.message)}</p>
                        <div style="display:flex;gap:8px;justify-content:center;">
                            <button class="nb-btn secondary" onclick="window.location.reload()">${retryText}</button>
                            <a href="/dashboard" class="nb-btn secondary">${backText}</a>
                        </div>
                    </div>
                `;
            }
        }
    },

    render: function() {
        var container = this._getActiveCellContainer();
        if (!container) container = document.getElementById('cells-container');
        const welcomeState = document.getElementById('welcome-state');
        
        if (welcomeState) {
            welcomeState.style.display = 'none';
        }
        
        if (!container) return;
        container.innerHTML = '';

        if (this.notebook.cells.length === 0) {
            var emptyStartText = window.i18n ? window.i18n.t('notebook.startNotebook') : 'Start your notebook by adding a cell';
            var emptyUseToolbar = window.i18n ? window.i18n.t('notebook.useToolbar') : 'Use the toolbar buttons or keyboard shortcuts';
            var codeCellText = window.i18n ? window.i18n.t('notebook.codeCell') : 'Code Cell';
            var textCellText = window.i18n ? window.i18n.t('notebook.textCell') : 'Text Cell';
            var shortcutsLabel = window.i18n ? window.i18n.t('notebook.keyboardShortcuts') : 'Keyboard shortcuts: ';
            var codeShortcut = window.i18n ? window.i18n.t('notebook.code') : ' code ';
            var mdShortcut = window.i18n ? window.i18n.t('notebook.markdown') : ' markdown';
            container.innerHTML = `
                <div class="cell-empty-state" style="padding: 32px; text-align: center;">
                    <div style="color: #71717a; margin-bottom: 20px;">
                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="margin-bottom: 12px;">
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                            <polyline points="14 2 14 8 20 8"/>
                            <line x1="12" y1="18" x2="12" y2="12"/>
                            <line x1="9" y1="15" x2="15" y2="15"/>
                        </svg>
                        <p style="margin: 0 0 8px 0; font-size: 14px;">${emptyStartText}</p>
                        <p style="margin: 0; font-size: 12px; color: #52525b;">${emptyUseToolbar}</p>
                    </div>

                    <div class="cell-empty-actions" style="display: flex; gap: 12px; justify-content: center; margin-bottom: 24px;">
                        <button class="nb-btn primary small" onclick="NotebookModule.addStarterCell('code')">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;margin-right:6px;">
                                <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
                            </svg>
                            ${codeCellText}
                        </button>
                        <button class="nb-btn secondary small" onclick="NotebookModule.addStarterCell('markdown')">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;margin-right:6px;">
                                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
                            </svg>
                            ${textCellText}
                        </button>
                    </div>

                    <div style="display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; max-width: 500px; margin: 0 auto;">
                        <button class="nb-chip" onclick="NotebookModule.addStarterCell('code', 'import phonos')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                            phonos SDK
                        </button>
                        <button class="nb-chip" onclick="NotebookModule.addStarterCell('code', 'import pandas as pd')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                            pandas
                        </button>
                        <button class="nb-chip" onclick="NotebookModule.addStarterCell('markdown', '# My Analysis\\n\\nStart writing here...')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg>
                            markdown
                        </button>
                        <button class="nb-chip" onclick="NotebookModule.addStarterCell('code', 'job = phonos.esmfold.run(\"MVLSPADKTN...\")')">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                            ESMFold
                        </button>
                    </div>

                    <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #3f3f46; font-size: 11px; color: #52525b;">
                        <p style="margin: 0;">${shortcutsLabel}<kbd style="background:#27272a;padding:2px 6px;border-radius: var(--r-ui, 0px);margin:0 4px;">B</kbd>${codeShortcut}<kbd style="background:#27272a;padding:2px 6px;border-radius: var(--r-ui, 0px);margin:0 4px;">M</kbd>${mdShortcut}</p>
                    </div>
                </div>
            `;
            return;
        }

        // Render each cell. Wrap in per-cell try/catch (mirroring
        // _renderNotebookInContainer) so a single bad cell or a missing
        // CodeMirror global (CDN blocked) cannot abort the whole render and
        // leave a half-drawn / "distorted" notebook.
        var self = this;
        this.notebook.cells.forEach(cell => {
            try {
                const el = this.createCellElement(cell);
                container.appendChild(el);
                this.initCodeMirror(cell, el);
            } catch (cellErr) {
                console.error('[Notebook] Failed to render cell', cell && cell.id, cellErr);
                // Best-effort fallback: still show the cell source as a plain
                // textarea so content is never lost when CodeMirror is unavailable.
                try {
                    const el = this.createCellElement(cell);
                    el.classList.add('cm-fallback');
                    const ta = document.createElement('textarea');
                    ta.className = 'cell-fallback-textarea';
                    ta.value = (cell && cell.source) || '';
                    ta.style.cssText = 'width:100%;min-height:44px;background:var(--bg-secondary,#1c2630);color:var(--text-primary,#F5F8FA);border:1px solid var(--border-color,#1f2d36);border-radius: var(--r-ui, 0px);padding:8px;font-family:var(--font-mono,monospace);font-size:13px;resize:vertical;';
                    const wrap = el.querySelector('.cell-input') || el;
                    wrap.appendChild(ta);
                    container.appendChild(el);
                } catch (e2) { /* truly broken; skip */ }
            }
        });

        // Refresh CodeMirror after layout settles. The initial-load render()
        // path previously relied solely on switchTab()'s 100ms refresh, which
        // never ran if _activeTabId wasn't set. Schedule two refreshes so
        // both early and late (CDN CSS / font) layout changes are picked up.
        setTimeout(function() {
            self.notebook.cells.forEach(function(c) { if (c.cm) { try { c.cm.refresh(); } catch (e) {} } });
        }, 100);
        setTimeout(function() {
            self.notebook.cells.forEach(function(c) { if (c.cm) { try { c.cm.refresh(); } catch (e) {} } });
        }, 350);

        this.updateAllCellActions();
        this.updateStatusBar();
    },

    // Alias used by the collaboration layer (remote cursors / cell-edit
    // suppression). Keeps the active cell id exposed under the name the
    // CollaborationManager already references.
    get currentCellId() { return this.activeCellId; },

    // Returns the CodeMirror instance for a given cell id, or null.
    // Used by CollaborationManager to apply remote edits and position
    // remote cursors. Previously this did not exist, so remote edits and
    // cursors were silently dropped (optional chaining no-op).
    getCodeMirrorInstance: function(cellId) {
        if (!this.notebook || !this.notebook.cells) return null;
        var cell = this.notebook.cells.find(function(c) { return c.id === cellId; });
        return cell ? cell.cm || null : null;
    },

    // Sets the source of a non-active cell from a remote collaboration
    // update. Never touches the currently-focused cell (the sender is
    // skipped server-side via skip_sid, and the active cell is also
    // guarded here) so two users editing different cells don't fight.
    setCellSource: function(cellId, source) {
        if (!this.notebook || !this.notebook.cells) return;
        var cell = this.notebook.cells.find(function(c) { return c.id === cellId; });
        if (!cell) return;
        var src = Array.isArray(source) ? source.join('') : (source || '');
        cell.source = src;
        if (cell.cm && cellId !== this.activeCellId) {
            // Preserve scroll/cursor where possible; operation batches the
            // re-layout so CodeMirror doesn't flicker per line.
            var cursor = cell.cm.getCursor();
            var scrollInfo = cell.cm.getScrollInfo();
            cell.cm.operation(function() {
                cell.cm.setValue(src);
                try { cell.cm.setCursor(cursor); } catch (e) {}
                try { cell.cm.scrollTo(scrollInfo.left, scrollInfo.top); } catch (e) {}
            });
        }
    },

    // Replaces the current notebook's cells from a history snapshot and
    // re-renders. Snapshot cells may be stored in either the frontend
    // format ({type, source}) or ipynb format ({cell_type, source[]}),
    // so both are normalized. Fixes history "Restore this version" which
    // previously called a non-existent NotebookModule.loadCells.
    loadCells: function(cells) {
        if (!this.notebook) return;
        var self = this;
        var normalized = (cells || []).map(function(c) {
            var src = c.source;
            if (Array.isArray(src)) src = src.join('');
            return new Cell({
                id: c.id,
                type: c.type || c.cell_type || 'code',
                source: src || '',
                outputs: c.outputs || [],
                metadata: c.metadata || {},
                execution_count: c.execution_count || null
            });
        });
        this.notebook.cells = normalized;
        this.notebook.dirty = false;
        this.activeCellId = normalized.length ? normalized[0].id : null;
        this.render();
        // Refresh CodeMirror once layout settles (mirrors switchTab logic).
        setTimeout(function() {
            normalized.forEach(function(c) { if (c.cm) { try { c.cm.refresh(); } catch (e) {} } });
        }, 100);
        if (typeof this.updateSaveStatus === 'function') {
            this.updateSaveStatus(window.i18n ? window.i18n.t('notebook.saved') : 'Saved');
        }
    },

    createCellElement: function(cell) {
        const div = document.createElement('div');
        div.className = `cell ${cell.type} ${this.activeCellId === cell.id ? 'active' : ''}`;
        div.id = cell.id;
        div.dataset.cellId = cell.id;
        div.draggable = true;

        const typeIcons = {
            'code': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
            'markdown': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
            'wolfram': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:#e11d48;"><path d="M3 20h18"/><path d="M6 20l3-12 3 6 3-12 3 12"/><path d="M3 8c2 2 4 3 6 3s4-1 6-3"/></svg>',
            'quantum': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:#22d3ee;"><circle cx="12" cy="12" r="2"/><ellipse cx="12" cy="12" rx="10" ry="4"/><ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(60 12 12)"/><ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(120 12 12)"/></svg>',
            'c': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>',
            'cpp': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/><line x1="18" y1="4" x2="23" y2="4"/><line x1="20.5" y1="1.5" x2="20.5" y2="6.5"/></svg>',
            'rust': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/><circle cx="12" cy="12" r="3"/></svg>',
            'r': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="20" x2="20" y2="20"/><line x1="4" y1="20" x2="4" y2="4"/><polyline points="7 15 11 10 14 13 19 6"/></svg>'
        };
        const typeLabels = {
            'code': window.i18n ? window.i18n.t('notebook.typeCode') : 'Code',
            'markdown': window.i18n ? window.i18n.t('notebook.typeText') : 'Text',
            'wolfram': 'Wolfram',
            'quantum': 'Quantum',
            'c': window.i18n ? window.i18n.t('notebook.typeC') : 'C',
            'cpp': window.i18n ? window.i18n.t('notebook.typeCpp') : 'C++',
            'rust': window.i18n ? window.i18n.t('notebook.typeRust') : 'Rust',
            'r': window.i18n ? window.i18n.t('notebook.typeR') : 'R'
        };
        const typeIcon = typeIcons[cell.type] || typeIcons['code'];
        const runtimeLabels = {
            'code': 'Python 3.11',
            'markdown': 'Text',
            'wolfram': 'Wolfram Cloud',
            'quantum': 'Qiskit',
            'c': 'gcc',
            'cpp': 'g++',
            'rust': 'rustc',
            'r': 'R 4.2 (rpy2)'
        };
        const runtimeIcons = {
            'code': '<svg viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" width="10" height="10"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
            'wolfram': '<svg viewBox="0 0 24 24" fill="none" stroke="#f87171" stroke-width="2" width="10" height="10"><path d="M3 20h18"/><path d="M6 20l3-12 3 6 3-12 3 12"/></svg>',
            'quantum': '<svg viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" width="10" height="10"><circle cx="12" cy="12" r="2"/><ellipse cx="12" cy="12" rx="10" ry="4"/></svg>',
            'markdown': '',
            'c': '',
            'cpp': '',
            'rust': '',
            'r': ''
        };
        const typeLabel = typeLabels[cell.type] || (window.i18n ? window.i18n.t('notebook.typeCode') : 'Code');

        // For convert button - cycle through types
        const typeOrder = ['code', 'markdown', 'wolfram', 'quantum', 'c', 'cpp', 'rust', 'r'];
        const currentIndex = typeOrder.indexOf(cell.type);
        const otherType = typeOrder[(currentIndex + 1) % typeOrder.length];
        const otherTypeIcon = typeIcons[otherType] || typeIcons['code'];

        const cellsArr = (this.notebook && this.notebook.cells) || [];
        const isFirstCell = cellsArr.length > 0 && cell.id === cellsArr[0].id;
        const isLastCell = cellsArr.length > 0 && cell.id === cellsArr[cellsArr.length - 1].id;
        const executionDisplay = cell.execution_count ? `[${cell.execution_count}]` : '[ ]';

        div.innerHTML = `
            <div class="cell-layout">
                <div class="cell-drag-handle" title="${window.i18n ? window.i18n.t('notebook.dragToMove') : 'Drag to move'}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="5" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="19" r="1"/></svg>
                </div>
                <div class="cell-main">
                    <div class="cell-header">
                        <div class="cell-header-left">
                            <div class="cell-status-indicator"></div>
                            <span class="cell-type-badge ${cell.type}">
                                ${typeIcon}
                                ${typeLabel}
                                <span class="cell-runtime-badge">${runtimeLabels[cell.type] || ''}</span>
                                ${cell.metadata && cell.metadata.education_role === 'problem' ? '<span class="nb-edu-badge problem">Problem</span>' : ''}
                                ${cell.metadata && cell.metadata.education_role === 'answer' ? '<span class="nb-edu-badge answer">Answer</span>' : ''}
                            </span>
                            <span class="execution-count">${isExecutableType(cell.type) ? executionDisplay : ''}</span>
                        </div>
                        <div class="cell-actions">
                            ${cell.type === 'quantum' ? `<button class="cell-action-btn circuit" onclick="NotebookModule.openQuantumBuilder('${cell.id}')" title="Edit Circuit" style="color:#22d3ee;">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="2"/><ellipse cx="12" cy="12" rx="10" ry="4"/><ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(60 12 12)"/></svg>
                            </button>` : ''}
                            <button class="cell-action-btn run" onclick="NotebookModule.runCell('${cell.id}')" title="${window.i18n ? window.i18n.t('notebook.runCellTooltip') : 'Run (Ctrl+Enter)'}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                            </button>
                            <button class="cell-action-btn duplicate" onclick="NotebookModule.duplicateCell('${cell.id}')" title="${window.i18n ? window.i18n.t('notebook.duplicateCell') : 'Duplicate (Ctrl+D)'}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                            </button>
                            <button class="cell-action-btn collapse" onclick="NotebookModule.toggleCellCollapse('${cell.id}')" title="${window.i18n ? window.i18n.t('notebook.collapseExpand') : 'Collapse/Expand'}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="collapse-icon"><polyline points="6 9 12 15 18 9"/></svg>
                            </button>
                            <button class="cell-action-btn convert" onclick="NotebookModule.toggleCellType('${cell.id}')" title="${window.i18n ? window.i18n.t('notebook.convertTo') + ' ' + typeLabels[otherType] : 'Convert to ' + typeLabels[otherType]}">
                                ${otherTypeIcon}
                            </button>
                            <button class="cell-action-btn" onclick="NotebookModule.moveCell('${cell.id}', -1)" title="${window.i18n ? window.i18n.t('notebook.moveUp') : 'Move Up'}" style="${isFirstCell ? 'visibility:hidden' : ''}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
                            </button>
                            <button class="cell-action-btn" onclick="NotebookModule.moveCell('${cell.id}', 1)" title="${window.i18n ? window.i18n.t('notebook.moveDown') : 'Move Down'}" style="${isLastCell ? 'visibility:hidden' : ''}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                            </button>
                            <button class="cell-action-btn delete" onclick="NotebookModule.deleteCell('${cell.id}')" title="${window.i18n ? window.i18n.t('notebook.deleteCell') : 'Delete'}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                            </button>
                            <button class="cell-action-btn annotate" onclick="NotebookModule.toggleAnnotation('${cell.id}')" title="${window.i18n ? window.i18n.t('notebook.annotate') : 'Annotate'}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                            </button>
                            ${cell.metadata && cell.metadata.education_role && (NotebookModule._educationInfo && NotebookModule._educationInfo.is_teacher) ? `<button class="cell-action-btn" onclick="NotebookModule.openProblemEditor('${cell.id}')" title="Edit Problem" style="color:#137CBD;">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                            </button>` : ''}
                            ${cell.metadata && cell.metadata.education_role === 'answer' && (NotebookModule._educationInfo && NotebookModule._educationInfo.is_student) ? `<button class="cell-action-btn run" onclick="NotebookModule._submitStudentAnswer('${cell.id}')" title="Submit Answer" style="color:#C53030;">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                            </button>` : ''}
                        </div>
                    </div>
                    <div class="cell-annotation-area" style="display:${(cell.metadata && cell.metadata.annotation) ? 'flex' : 'none'}">
                        <input type="text" class="cell-annotation-input" placeholder="${window.i18n ? window.i18n.t('notebook.addAnnotation') : 'Add annotation...'}" value="${this.escapeHtml((cell.metadata && cell.metadata.annotation) || '')}">
                    </div>
                    <div class="cell-body">
                        <div class="cell-input-wrapper">
                            <div class="cell-input"></div>
                            <div class="cell-markdown-rendered" style="display:none;"></div>
                        </div>
                        <div class="cell-output-area ${cell.outputs.length ? 'has-output' : ''}">
                            ${this.renderOutputs(cell.outputs)}
                        </div>
                    </div>
                    ${cell.metadata && cell.metadata.education_role === 'answer' ? `<div class="nb-edu-result" id="nb-edu-result-${cell.id}" style="display:none;"></div>` : ''}
                    <div class="cell-collapsed-preview">${cell.source.substring(0, 80)}${cell.source.length > 80 ? '...' : ''}</div>
                </div>
            </div>
        `;

        div.addEventListener('click', (e) => {
            if (!e.target.closest('button') && !e.target.closest('.cell-drag-handle')) {
                this.setActiveCell(cell.id);
            }
        });

        div.addEventListener('dblclick', (e) => {
            if (!e.target.closest('.cell-actions') && !e.target.closest('.cell-drag-handle')) {
                const cellData = this.notebook.getCell(cell.id);
                if (cellData && cellData.cm) {
                    cellData.cm.focus();
                }
            }
        });

        if (cell.type === 'markdown') {
            const rendered = div.querySelector('.cell-markdown-rendered');
            rendered.addEventListener('dblclick', () => this.toggleMarkdownEdit(cell, true));
        }

        this.setupCellDragEvents(div, cell);
        this.setupCellContextMenu(div, cell);

        return div;
    },

    duplicateCell: function(id) {
        const cell = this.notebook.getCell(id);
        if (!cell) return;

        const cellData = cell.cm ? cell.cm.getValue() : cell.source;
        const newCell = this.addCell(cell.type, cellData, id);
        this.showToast(window.i18n ? window.i18n.t('notebook.cellDuplicated') : 'Cell duplicated');
    },

    toggleCellCollapse: function(id) {
        const el = document.getElementById(id);
        if (!el) return;

        el.classList.toggle('collapsed');
        const isCollapsed = el.classList.contains('collapsed');
        this.updateCollapsedPreview(id);

        this.notebook.dirty = true;
    },

    toggleAnnotation: function(id) {
        var el = document.getElementById(id);
        if (!el) return;
        var area = el.querySelector('.cell-annotation-area');
        if (!area) return;
        var isVisible = area.style.display !== 'none';
        area.style.display = isVisible ? 'none' : 'flex';
        if (!isVisible) {
            var input = area.querySelector('.cell-annotation-input');
            if (input) {
                input.focus();
                var cell = this.notebook.getCell(id);
                // Save annotation on change
                input.onchange = function() {
                    if (cell) {
                        if (!cell.metadata) cell.metadata = {};
                        cell.metadata.annotation = input.value;
                        NotebookModule.notebook.dirty = true;
                    }
                };
                input.onblur = function() {
                    if (cell) {
                        if (!cell.metadata) cell.metadata = {};
                        cell.metadata.annotation = input.value;
                        NotebookModule.notebook.dirty = true;
                    }
                };
            }
        }
    },

    updateCollapsedPreview: function(id) {
        const cell = this.notebook.getCell(id);
        if (!cell) return;

        const el = document.getElementById(id);
        if (!el) return;

        const preview = el.querySelector('.cell-collapsed-preview');
        if (!preview) return;

        const source = cell.cm ? cell.cm.getValue() : cell.source;
        preview.textContent = source.substring(0, 80) + (source.length > 80 ? '...' : '');
    },

    setupCellContextMenu: function(element, cell) {
        let menu = document.getElementById('cell-context-menu');
        if (!menu) {
            menu = document.createElement('div');
            menu.id = 'cell-context-menu';
            menu.className = 'cell-context-menu';
            document.body.appendChild(menu);

            // Close on outside click
            document.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                if (!menu.contains(e.target)) {
                    menu.classList.remove('active');
                }
            });

            // Delegated click handler — run the action then close the menu
            menu.addEventListener('click', function(ev) {
                var item = ev.target.closest('.menu-item');
                if (!item) return;
                ev.stopPropagation();
                var action = item.getAttribute('data-action');
                var cellId = item.getAttribute('data-cell');
                switch (action) {
                    case 'runCell':            NotebookModule.runCell(cellId); break;
                    case 'runAllAbove':        NotebookModule.runAllAbove(cellId); break;
                    case 'runAllBelow':        NotebookModule.runAllBelow(cellId); break;
                    case 'duplicateCell':      NotebookModule.duplicateCell(cellId); break;
                    case 'toggleCellCollapse': NotebookModule.toggleCellCollapse(cellId); break;
                    case 'moveUp':             NotebookModule.moveCell(cellId, -1); break;
                    case 'moveDown':           NotebookModule.moveCell(cellId, 1); break;
                    case 'deleteCell':         NotebookModule.deleteCell(cellId); break;
                }
                menu.classList.remove('active');
            });
        }

        element.addEventListener('contextmenu', (e) => {
            e.preventDefault();

            var runCellText = window.i18n ? window.i18n.t('notebook.runCell') : 'Run Cell';
            var runAboveText = window.i18n ? window.i18n.t('notebook.runAllAbove') : 'Run All Above';
            var runBelowText = window.i18n ? window.i18n.t('notebook.runAllBelow') : 'Run All Below';
            var duplicateText = window.i18n ? window.i18n.t('notebook.duplicate') : 'Duplicate';
            var collapseText = window.i18n ? window.i18n.t('notebook.collapseExpand') : 'Collapse/Expand';
            var moveUpText = window.i18n ? window.i18n.t('notebook.moveUp') : 'Move Up';
            var moveDownText = window.i18n ? window.i18n.t('notebook.moveDown') : 'Move Down';
            var deleteText = window.i18n ? window.i18n.t('notebook.delete') : 'Delete';
            var cid = cell.id;
            menu.innerHTML = `
                <div class="menu-item" data-action="runCell" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                    ${runCellText}
                </div>
                <div class="menu-item" data-action="runAllAbove" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/><polygon points="5 3 19 12 5 21 5 3" transform="scale(0.5) translate(12,6)"/></svg>
                    ${runAboveText}
                </div>
                <div class="menu-item" data-action="runAllBelow" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/><polygon points="5 3 19 12 5 21 5 3" transform="scale(0.5) translate(12,18)"/></svg>
                    ${runBelowText}
                </div>
                <div class="separator"></div>
                <div class="menu-item" data-action="duplicateCell" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                    ${duplicateText}
                    <span class="shortcut">Ctrl+D</span>
                </div>
                <div class="menu-item" data-action="toggleCellCollapse" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                    ${collapseText}
                </div>
                <div class="separator"></div>
                <div class="menu-item" data-action="moveUp" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
                    ${moveUpText}
                    <span class="shortcut">Alt+Up</span>
                </div>
                <div class="menu-item" data-action="moveDown" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                    ${moveDownText}
                    <span class="shortcut">Alt+Down</span>
                </div>
                <div class="separator"></div>
                <div class="menu-item danger" data-action="deleteCell" data-cell="${cid}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                    ${deleteText}
                    <span class="shortcut">Ctrl+Shift+D</span>
                </div>
            `;

            menu.style.left = `${e.pageX}px`;
            menu.style.top = `${e.pageY}px`;
            menu.classList.add('active');
        });
    },

    toggleCellType: function(id) {
        const cell = this.notebook.getCell(id);
        if (!cell) return;

        const typeOrder = ['code', 'markdown', 'wolfram', 'quantum', 'c', 'cpp', 'rust', 'r'];
        const currentIndex = typeOrder.indexOf(cell.type);
        cell.type = typeOrder[(currentIndex + 1) % typeOrder.length];
        this.notebook.dirty = true;
        this.render();
        this.setActiveCell(id);

        const newCell = this.notebook.getCell(id);
        if (newCell && newCell.cm) {
            setTimeout(() => {
                const modeMap = {
                    'code': 'python',
                    'markdown': 'markdown',
                    'c': 'text/x-csrc',
                    'cpp': 'text/x-c++src',
                    'rust': 'text/x-rustsrc',
                    'r': 'text/x-rsrc',
                    'wolfram': 'wolfram'
                };
                newCell.cm.setOption('mode', modeMap[cell.type] || 'python');
                newCell.cm.focus();
            }, 10);
        }
    },

    setupCellDragEvents: function(element, cell) {
        let dragTimeout = null;

        element.addEventListener('dragstart', (e) => {
            e.dataTransfer.setData('text/plain', cell.id);
            element.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });

        element.addEventListener('dragend', () => {
            element.classList.remove('dragging');
            document.querySelectorAll('.cell').forEach(c => c.classList.remove('drag-over'));
        });

        element.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';

            clearTimeout(dragTimeout);
            element.classList.add('drag-over');
        });

        element.addEventListener('dragleave', () => {
            dragTimeout = setTimeout(() => {
                element.classList.remove('drag-over');
            }, 100);
        });

        element.addEventListener('drop', (e) => {
            e.preventDefault();
            const sourceId = e.dataTransfer.getData('text/plain');
            if (sourceId && sourceId !== cell.id) {
                this.moveCellTo(sourceId, cell.id);
            }
            element.classList.remove('drag-over');
        });
    },

    moveCellTo: function(sourceId, targetId) {
        const sourceIdx = this.notebook.cells.findIndex(c => c.id === sourceId);
        const targetIdx = this.notebook.cells.findIndex(c => c.id === targetId);
        if (sourceIdx === -1 || targetIdx === -1 || sourceIdx === targetIdx) return;

        const [removed] = this.notebook.cells.splice(sourceIdx, 1);
        const insertIdx = sourceIdx < targetIdx ? targetIdx - 1 : targetIdx;
        this.notebook.cells.splice(insertIdx >= 0 ? insertIdx : 0, 0, removed);
        this.notebook.dirty = true;

        this.render();
        this.setActiveCell(sourceId);
    },

    initCodeMirror: function(cell, element) {
        const inputDiv = element.querySelector('.cell-input');
        if (!inputDiv) return;

        // Guard against CodeMirror failing to load from the CDN. Throw so the
        // per-cell try/catch in render() can install a plain textarea fallback
        // instead of leaving the cell blank ("distorted cells" after reload).
        if (typeof CodeMirror === 'undefined') {
            throw new Error('CodeMirror global is not available (CDN blocked?)');
        }

        inputDiv.style.minHeight = '44px';

        const modeMap = {
            'code': 'python',
            'markdown': 'markdown',
            'c': 'text/x-csrc',
            'cpp': 'text/x-c++src',
            'rust': 'text/x-rustsrc',
            'r': 'text/x-rsrc',
            'wolfram': 'wolfram',
            'quantum': 'python'
        };

        const isLocked = this._educationInfo && this._educationInfo.is_student &&
            cell.metadata && cell.metadata.education_role === 'problem';

        const cm = CodeMirror(inputDiv, {
            ...CONFIG.codeMirrorDefaults,
            mode: modeMap[cell.type] || 'python',
            value: cell.source,
            autofocus: false,
            viewportMargin: Infinity,
            readOnly: isLocked,
        });

        cm.on('change', () => {
            cell.source = cm.getValue();
            this.notebook.dirty = true;
            this.updateSaveStatus(window.i18n ? window.i18n.t('notebook.unsaved') : 'Unsaved');
            if (window.CollaborationManager && CollaborationManager.enabled) {
                CollaborationManager.sendTyping(cell.id, true);
                CollaborationManager.sendCellEdit(cell.id, cm.getValue());
                clearTimeout(this._typingStopTimer);
                var self = this;
                this._typingStopTimer = setTimeout(function() {
                    if (window.CollaborationManager) {
                        CollaborationManager.sendTyping(cell.id, false);
                    }
                }, 1500);
            }
        });

        cm.on('focus', () => {
            this.setActiveCell(cell.id);
            element.classList.add('focused');
            element.classList.add('current-active');
            if (window.CollaborationManager && CollaborationManager.enabled) {
                CollaborationManager.sendCellSelect(cell.id);
            }
        });

        cm.on('blur', () => {
            element.classList.remove('focused');
            element.classList.remove('current-active');
            if (window.CollaborationManager && CollaborationManager.enabled) {
                CollaborationManager.sendTyping(cell.id, false);
            }
        });

        cm.on('cursorActivity', () => {
            this.updateStatusBar();
            if (window.CollaborationManager && CollaborationManager.enabled) {
                if (!this._lastCursorSyncTime || Date.now() - this._lastCursorSyncTime > 500) {
                    var pos = cm.getCursor();
                    var sels = cm.listSelections();
                    var sel = null;
                    if (sels && sels.length) {
                        var s = sels[0];
                        // Only send a range when there is an actual selection
                        if (s.anchor.line !== s.head.line || s.anchor.ch !== s.head.ch) {
                            sel = { anchor: s.anchor, head: s.head };
                        }
                    }
                    CollaborationManager.sendCursorSync(cell.id, pos, sel);
                    this._lastCursorSyncTime = Date.now();
                }
            }
        });

        cm.addKeyMap({
            'Shift-Enter': () => {
                this.runCell(cell.id);
                this.selectNextCell(cell.id) || this.addCell('code', '', cell.id);
            },
            'Ctrl-Enter': () => this.runCell(cell.id),
            'Ctrl-B': () => {
                this.addCell('code', '', cell.id);
            },
            'Ctrl-D': () => {
                if (confirm(window.i18n ? window.i18n.t('notebook.deleteCellConfirm') : 'Delete this cell?')) {
                    this.deleteCell(cell.id);
                }
            },
            'Alt-Up': () => {
                this.moveCell(cell.id, -1);
            },
            'Alt-Down': () => {
                this.moveCell(cell.id, 1);
            }
        });

        cell.cm = cm;

        cm.refresh();

        if (cell.type === 'markdown' && cell.source.trim() && cell.id !== this.activeCellId) {
            this.toggleMarkdownEdit(cell, false);
        }

        const gutter = element.querySelector('.cell-gutter');
        if (gutter && isExecutableType(cell.type)) {
            var countText = cell.execution_count ? '[' + cell.execution_count + ']' : '[ ]';
            var countSpan = document.createElement('span');
            countSpan.className = 'execution-count';
            countSpan.textContent = countText;
            gutter.textContent = '';
            gutter.appendChild(countSpan);
        }
    },

    toggleMarkdownEdit: function(cell, isEditing) {
        const el = document.getElementById(cell.id);
        if (!el) return;
        const inputDiv = el.querySelector('.cell-input');
        const renderedDiv = el.querySelector('.cell-markdown-rendered');
        
        if (isEditing) {
            inputDiv.style.display = 'block';
            renderedDiv.style.display = 'none';
            cell.cm.refresh();
            cell.cm.focus();
        } else {
            inputDiv.style.display = 'none';
            renderedDiv.style.display = 'block';
            renderedDiv.innerHTML = DOMPurify.sanitize(marked.parse(cell.source));
        }
    },

    runCell: async function(id) {
        const cell = this.notebook.getCell(id);
        if (!cell) return;

        if (cell.type === 'markdown') {
            this.toggleMarkdownEdit(cell, false);
            return;
        }

        if (!this.kernel || !this.kernel.isConnected()) {
            const connected = await this.ensureKernelReady();
            if (!connected) return;
        }

        const el = document.getElementById(id);
        if (!el) return;

        el.classList.add('running');

        const countEl = el.querySelector('.execution-count');
        if (countEl) countEl.textContent = '[*]';

        // Clearing here is only a visual "ready for a new run" reset, not a
        // real discard: if the execute call never actually reaches the
        // kernel (not connected, socket error, thrown before any output
        // event fires) there is nothing to replace it, so we restore the
        // prior output below instead of leaving the cell permanently blank.
        const previousOutputs = cell.outputs;
        const previousExecutionCount = cell.execution_count;
        cell.clearOutputs();
        this.updateCellOutputUI(cell);

        try {
            // Resolves with the execute_reply payload ({status: 'ok'|'error',
            // interrupted, restarted, ...}) so batch runners can stop the
            // queue when a cell fails or the kernel is halted.
            return await this.kernel.execute(cell.id, cell.source, cell.type);
        } catch (e) {
            cell.outputs = previousOutputs;
            cell.execution_count = previousExecutionCount;
            this.updateCellOutputUI(cell);
            el.classList.remove('running');
            el.classList.add('error');
            console.error('Execution error:', e);
            return { status: 'error', dispatchFailed: true };
        }
    },

    // --- Interactive Output Rendering (DOM-based) ---

    renderOutputs: function(outputs) {
        // Legacy compat: returns HTML string. Prefer renderOutputDOM for new code.
        if (!outputs || outputs.length === 0) return '';
        var frag = this.renderOutputDOM(outputs);
        var tmp = document.createElement('div');
        tmp.appendChild(frag);
        return tmp.innerHTML;
    },

    renderOutputDOM: function(outputs) {
        var frag = document.createDocumentFragment();
        if (!outputs || outputs.length === 0) return frag;
        var self = this;
        outputs.forEach(function(o, idx) {
            var el = self._renderSingleOutput(o, idx);
            if (el) frag.appendChild(el);
        });
        return frag;
    },

    _renderSingleOutput: function(o, idx) {
        if (o.output_type === 'stream') return this._buildStreamOutput(o, idx);
        if (o.output_type === 'error') return this._buildErrorOutput(o, idx);
        if (o.output_type === 'execute_result' || o.output_type === 'display_data') return this._buildDataOutput(o, idx);
        if (o.output_type === 'update_display_data') return this._buildUpdateDisplayOutput(o);
        return null;
    },

    _buildStreamOutput: function(o, idx) {
        if (o.name === 'stdout' && o.text && o.text.trim() === '') return null;
        var text = o.text || '';
        var lines = text.split('\n');
        var COLLAPSE_THRESHOLD = 30;
        var VISIBLE_LINES = 20;

        var wrapper = this._buildOutputHeader(idx, 'stream');
        var contentDiv = document.createElement('div');
        contentDiv.className = 'output-content';

        if (lines.length > COLLAPSE_THRESHOLD) {
            var visibleDiv = document.createElement('div');
            visibleDiv.className = 'cell-output-text';
            visibleDiv.textContent = lines.slice(0, VISIBLE_LINES).join('\n');
            contentDiv.appendChild(visibleDiv);

            var hiddenDiv = document.createElement('div');
            hiddenDiv.className = 'cell-output-text output-collapsed-section';
            hiddenDiv.style.display = 'none';
            hiddenDiv.textContent = lines.slice(VISIBLE_LINES).join('\n');
            contentDiv.appendChild(hiddenDiv);

            var toggleBtn = document.createElement('button');
            toggleBtn.className = 'output-collapse-toggle';
            var nHidden = lines.length - VISIBLE_LINES;
            var showText = window.i18n ? window.i18n.t('notebook.showNLines').replace('{n}', nHidden) : 'Show ' + nHidden + ' more lines';
            var hideText = window.i18n ? window.i18n.t('notebook.hideNLines').replace('{n}', nHidden) : 'Hide ' + nHidden + ' lines';
            toggleBtn.textContent = showText;
            toggleBtn.addEventListener('click', function() {
                var isHidden = hiddenDiv.style.display === 'none';
                hiddenDiv.style.display = isHidden ? 'block' : 'none';
                toggleBtn.textContent = isHidden ? hideText : showText;
            });
            contentDiv.appendChild(toggleBtn);
        } else {
            var textDiv = document.createElement('div');
            textDiv.className = 'cell-output-text';
            textDiv.textContent = text;
            contentDiv.appendChild(textDiv);
        }

        wrapper.appendChild(contentDiv);
        return wrapper;
    },

    _buildErrorOutput: function(o, idx) {
        var wrapper = this._buildOutputHeader(idx, 'error');
        wrapper.classList.add('error');
        var contentDiv = document.createElement('div');
        contentDiv.className = 'output-content';

        // Error summary always visible
        var summaryDiv = document.createElement('div');
        summaryDiv.className = 'cell-output-error-summary';
        var nameSpan = document.createElement('b');
        nameSpan.textContent = o.ename || (window.i18n ? window.i18n.t('notebook.error') : 'Error');
        summaryDiv.appendChild(nameSpan);
        summaryDiv.appendChild(document.createTextNode(': ' + (o.evalue || '')));
        contentDiv.appendChild(summaryDiv);

        // Collapsible traceback
        var tb = (o.traceback || []).join('\n');
        if (tb) {
            // Strip ANSI escape codes
            var cleanTb = tb.replace(/\x1b\[[0-9;]*m/g, '');
            var tbDiv = document.createElement('div');
            tbDiv.className = 'cell-output-text cell-output-traceback';
            tbDiv.style.display = 'none';
            var preTb = document.createElement('pre');
            preTb.textContent = cleanTb;
            tbDiv.appendChild(preTb);
            contentDiv.appendChild(tbDiv);

            var tbToggle = document.createElement('button');
            tbToggle.className = 'output-collapse-toggle';
            var showTbText = window.i18n ? window.i18n.t('notebook.showTraceback') : 'Show traceback';
            var hideTbText = window.i18n ? window.i18n.t('notebook.hideTraceback') : 'Hide traceback';
            tbToggle.textContent = showTbText;
            tbToggle.addEventListener('click', function() {
                var isHidden = tbDiv.style.display === 'none';
                tbDiv.style.display = isHidden ? 'block' : 'none';
                tbToggle.textContent = isHidden ? hideTbText : showTbText;
            });
            contentDiv.appendChild(tbToggle);
        }

        wrapper.appendChild(contentDiv);
        return wrapper;
    },

    _buildDataOutput: function(o, idx) {
        var d = o.data || {};
        var transient = o.transient || {};

        // Image types
        if (d['image/png'] || d['image/jpeg'] || d['image/gif']) {
            return this._buildImageOutput(d, idx, transient);
        }
        // SVG
        if (d['image/svg+xml']) {
            var svgWrapper = this._buildOutputHeader(idx, 'image');
            if (transient.display_id) svgWrapper.setAttribute('data-display-id', transient.display_id);
            var svgContent = document.createElement('div');
            svgContent.className = 'output-content cell-output-image';
            var sanitizedSvg = DOMPurify.sanitize(d['image/svg+xml']);
            svgContent.innerHTML = sanitizedSvg;
            svgWrapper.appendChild(svgContent);
            return svgWrapper;
        }
        // HTML (DataFrame detection)
        if (d['text/html']) {
            return this._buildHtmlOutput(d['text/html'], idx, transient);
        }
        // JSON tree
        if (d['application/json']) {
            return this._buildJsonTreeOutput(d['application/json'], idx);
        }
        // LaTeX
        if (d['text/latex']) {
            var latexWrapper = this._buildOutputHeader(idx, 'latex');
            var latexContent = document.createElement('div');
            latexContent.className = 'output-content cell-output-latex';
            latexContent.textContent = d['text/latex'];
            latexWrapper.appendChild(latexContent);
            return latexWrapper;
        }
        // Markdown
        if (d['text/markdown']) {
            var mdWrapper = this._buildOutputHeader(idx, 'markdown');
            var mdContent = document.createElement('div');
            mdContent.className = 'output-content cell-output-markdown';
            var sanitizedMd = DOMPurify.sanitize(marked.parse(d['text/markdown']));
            mdContent.innerHTML = sanitizedMd;
            mdWrapper.appendChild(mdContent);
            return mdWrapper;
        }
        // Plain text fallback
        if (d['text/plain']) {
            var txtWrapper = this._buildOutputHeader(idx, 'text');
            if (transient.display_id) txtWrapper.setAttribute('data-display-id', transient.display_id);
            var txtContent = document.createElement('div');
            txtContent.className = 'output-content';
            var txtDiv = document.createElement('div');
            txtDiv.className = 'cell-output-text';
            txtDiv.textContent = d['text/plain'];
            txtContent.appendChild(txtDiv);
            txtWrapper.appendChild(txtContent);
            return txtWrapper;
        }
        return null;
    },

    _buildUpdateDisplayOutput: function(o) {
        // For tqdm-style progress: find existing element by display_id and update
        var displayId = (o.transient || {}).display_id;
        if (!displayId) return null;
        var existing = document.querySelector('[data-display-id="' + displayId + '"]');
        if (existing) {
            // Update in place
            var d = o.data || {};
            if (d['text/html']) {
                var contentEl = existing.querySelector('.output-content');
                if (contentEl) {
                    var sanitized = DOMPurify.sanitize(d['text/html']);
                    contentEl.innerHTML = sanitized;
                }
            } else if (d['text/plain']) {
                var contentEl2 = existing.querySelector('.cell-output-text');
                if (contentEl2) contentEl2.textContent = d['text/plain'];
            }
            return null; // Already updated in DOM, don't append new
        }
        // If no existing element, render as display_data
        return this._buildDataOutput(o, 0);
    },

    _buildImageOutput: function(d, idx, transient) {
        var wrapper = this._buildOutputHeader(idx, 'image');
        if (transient && transient.display_id) wrapper.setAttribute('data-display-id', transient.display_id);
        var contentDiv = document.createElement('div');
        contentDiv.className = 'output-content cell-output-image';

        // Determine mime type and data
        var mimeType, imgData;
        if (d['image/png']) { mimeType = 'image/png'; imgData = d['image/png']; }
        else if (d['image/jpeg']) { mimeType = 'image/jpeg'; imgData = d['image/jpeg']; }
        else if (d['image/gif']) { mimeType = 'image/gif'; imgData = d['image/gif']; }

        var imgContainer = document.createElement('div');
        imgContainer.className = 'image-output-container';

        var img = document.createElement('img');
        img.src = 'data:' + mimeType + ';base64,' + imgData;
        img.alt = window.i18n ? window.i18n.t('notebook.outputImage') : 'Output Image';
        img.className = 'output-image';
        imgContainer.appendChild(img);

        // Image toolbar: zoom in, zoom out, reset, download
        var toolbar = document.createElement('div');
        toolbar.className = 'image-output-toolbar';
        var scale = 1;

        var zoomInBtn = document.createElement('button');
        zoomInBtn.className = 'image-tool-btn';
        zoomInBtn.title = window.i18n ? window.i18n.t('notebook.zoomIn') : 'Zoom in';
        zoomInBtn.textContent = '+';
        zoomInBtn.addEventListener('click', function() {
            scale = Math.min(scale * 1.25, 5);
            img.style.transform = 'scale(' + scale + ')';
        });

        var zoomOutBtn = document.createElement('button');
        zoomOutBtn.className = 'image-tool-btn';
        zoomOutBtn.title = window.i18n ? window.i18n.t('notebook.zoomOut') : 'Zoom out';
        zoomOutBtn.textContent = '\u2212';
        zoomOutBtn.addEventListener('click', function() {
            scale = Math.max(scale * 0.8, 0.2);
            img.style.transform = 'scale(' + scale + ')';
        });

        var resetBtn = document.createElement('button');
        resetBtn.className = 'image-tool-btn';
        resetBtn.title = window.i18n ? window.i18n.t('notebook.resetZoom') : 'Reset zoom';
        resetBtn.textContent = '1:1';
        resetBtn.addEventListener('click', function() {
            scale = 1;
            img.style.transform = 'scale(1)';
        });

        var dlBtn = document.createElement('button');
        dlBtn.className = 'image-tool-btn';
        dlBtn.title = window.i18n ? window.i18n.t('notebook.download') : 'Download';
        dlBtn.textContent = '\u2B73';
        dlBtn.addEventListener('click', function() {
            var a = document.createElement('a');
            a.href = img.src;
            a.download = 'output.' + mimeType.split('/')[1];
            a.click();
        });

        toolbar.appendChild(zoomInBtn);
        toolbar.appendChild(zoomOutBtn);
        toolbar.appendChild(resetBtn);
        toolbar.appendChild(dlBtn);

        contentDiv.appendChild(toolbar);
        contentDiv.appendChild(imgContainer);
        wrapper.appendChild(contentDiv);
        return wrapper;
    },

    _buildHtmlOutput: function(htmlContent, idx, transient) {
        var wrapper = this._buildOutputHeader(idx, 'html');
        if (transient && transient.display_id) wrapper.setAttribute('data-display-id', transient.display_id);
        var contentDiv = document.createElement('div');
        contentDiv.className = 'output-content cell-output-html';

        var sanitized = DOMPurify.sanitize(htmlContent);
        contentDiv.innerHTML = sanitized;

        // Detect DataFrame tables and enhance
        var table = contentDiv.querySelector('table.dataframe');
        if (table) {
            table.classList.add('nb-dataframe');
            // Add sortable headers
            var headers = table.querySelectorAll('thead th');
            var self = this;
            headers.forEach(function(th, colIdx) {
                th.style.cursor = 'pointer';
                th.title = window.i18n ? window.i18n.t('notebook.clickToSort') : 'Click to sort';
                th.addEventListener('click', function() {
                    self._sortTable(table, colIdx);
                });
            });
            // Pagination for large tables
            var rows = table.querySelectorAll('tbody tr');
            if (rows.length > 50) {
                self._paginateTable(table, contentDiv, 50);
            }
        }

        wrapper.appendChild(contentDiv);
        return wrapper;
    },

    _buildJsonTreeOutput: function(jsonData, idx) {
        var wrapper = this._buildOutputHeader(idx, 'json');
        var contentDiv = document.createElement('div');
        contentDiv.className = 'output-content json-tree-output';
        var tree = this._buildJsonNode(jsonData, 0);
        contentDiv.appendChild(tree);
        wrapper.appendChild(contentDiv);
        return wrapper;
    },

    _buildJsonNode: function(data, depth) {
        var MAX_DEPTH_EXPAND = 2;
        var container = document.createElement('div');
        container.className = 'json-node';
        container.style.paddingLeft = (depth > 0 ? 12 : 0) + 'px';

        if (data === null) {
            var nullSpan = document.createElement('span');
            nullSpan.className = 'json-null';
            nullSpan.textContent = 'null';
            container.appendChild(nullSpan);
            return container;
        }

        if (typeof data !== 'object') {
            var valSpan = document.createElement('span');
            if (typeof data === 'string') {
                valSpan.className = 'json-string';
                valSpan.textContent = '"' + data + '"';
            } else if (typeof data === 'number') {
                valSpan.className = 'json-number';
                valSpan.textContent = String(data);
            } else if (typeof data === 'boolean') {
                valSpan.className = 'json-boolean';
                valSpan.textContent = String(data);
            }
            container.appendChild(valSpan);
            return container;
        }

        var isArray = Array.isArray(data);
        var keys = isArray ? data.map(function(_, i) { return i; }) : Object.keys(data);
        var self = this;

        var toggle = document.createElement('span');
        toggle.className = 'json-toggle';
        toggle.textContent = depth < MAX_DEPTH_EXPAND ? '\u25BC' : '\u25B6';
        container.appendChild(toggle);

        var braceOpen = document.createElement('span');
        braceOpen.className = 'json-brace';
        braceOpen.textContent = isArray ? '[' : '{';
        container.appendChild(braceOpen);

        var countSpan = document.createElement('span');
        countSpan.className = 'json-count';
        var itemsText = window.i18n ? window.i18n.t('notebook.items') : ' items';
        var keysText = window.i18n ? window.i18n.t('notebook.keys') : ' keys';
        countSpan.textContent = ' ' + keys.length + (isArray ? itemsText : keysText);
        countSpan.style.display = depth < MAX_DEPTH_EXPAND ? 'none' : 'inline';
        container.appendChild(countSpan);

        var childrenDiv = document.createElement('div');
        childrenDiv.className = 'json-children';
        childrenDiv.style.display = depth < MAX_DEPTH_EXPAND ? 'block' : 'none';

        keys.forEach(function(key) {
            var entry = document.createElement('div');
            entry.className = 'json-entry';
            if (!isArray) {
                var keySpan = document.createElement('span');
                keySpan.className = 'json-key';
                keySpan.textContent = '"' + key + '"';
                entry.appendChild(keySpan);
                entry.appendChild(document.createTextNode(': '));
            }
            var val = isArray ? data[key] : data[key];
            entry.appendChild(self._buildJsonNode(val, depth + 1));
            childrenDiv.appendChild(entry);
        });

        container.appendChild(childrenDiv);

        var braceClose = document.createElement('span');
        braceClose.className = 'json-brace';
        braceClose.textContent = isArray ? ']' : '}';
        container.appendChild(braceClose);

        toggle.addEventListener('click', function() {
            var isExpanded = childrenDiv.style.display !== 'none';
            childrenDiv.style.display = isExpanded ? 'none' : 'block';
            countSpan.style.display = isExpanded ? 'inline' : 'none';
            toggle.textContent = isExpanded ? '\u25B6' : '\u25BC';
        });

        return container;
    },

    _sortTable: function(table, colIdx) {
        var tbody = table.querySelector('tbody');
        if (!tbody) return;
        var rows = Array.from(tbody.querySelectorAll('tr'));
        var ascending = table.getAttribute('data-sort-col') !== String(colIdx) ||
                        table.getAttribute('data-sort-dir') === 'desc';

        rows.sort(function(a, b) {
            var aVal = (a.cells[colIdx] || {}).textContent || '';
            var bVal = (b.cells[colIdx] || {}).textContent || '';
            var aNum = parseFloat(aVal);
            var bNum = parseFloat(bVal);
            if (!isNaN(aNum) && !isNaN(bNum)) {
                return ascending ? aNum - bNum : bNum - aNum;
            }
            return ascending ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
        });

        rows.forEach(function(row) { tbody.appendChild(row); });
        table.setAttribute('data-sort-col', String(colIdx));
        table.setAttribute('data-sort-dir', ascending ? 'asc' : 'desc');
    },

    _paginateTable: function(table, container, pageSize) {
        var tbody = table.querySelector('tbody');
        if (!tbody) return;
        var allRows = Array.from(tbody.querySelectorAll('tr'));
        var totalPages = Math.ceil(allRows.length / pageSize);
        var currentPage = 0;

        function showPage(page) {
            allRows.forEach(function(row, i) {
                row.style.display = (i >= page * pageSize && i < (page + 1) * pageSize) ? '' : 'none';
            });
            currentPage = page;
            var pageText = window.i18n ? window.i18n.t('notebook.pageOf').replace('{current}', page + 1).replace('{total}', totalPages).replace('{rows}', allRows.length) : 'Page ' + (page + 1) + ' of ' + totalPages + ' (' + allRows.length + ' rows)';
            if (pageInfo) pageInfo.textContent = pageText;
            if (prevBtn) prevBtn.disabled = page === 0;
            if (nextBtn) nextBtn.disabled = page >= totalPages - 1;
        }

        var paginationDiv = document.createElement('div');
        paginationDiv.className = 'table-pagination';

        var prevBtn = document.createElement('button');
        prevBtn.className = 'pagination-btn';
        prevBtn.textContent = (window.i18n ? window.i18n.t('notebook.prev') : '\u2190 Prev');
        prevBtn.addEventListener('click', function() { if (currentPage > 0) showPage(currentPage - 1); });

        var pageInfo = document.createElement('span');
        pageInfo.className = 'pagination-info';

        var nextBtn = document.createElement('button');
        nextBtn.className = 'pagination-btn';
        nextBtn.textContent = (window.i18n ? window.i18n.t('notebook.next') : 'Next \u2192');
        nextBtn.addEventListener('click', function() { if (currentPage < totalPages - 1) showPage(currentPage + 1); });

        paginationDiv.appendChild(prevBtn);
        paginationDiv.appendChild(pageInfo);
        paginationDiv.appendChild(nextBtn);
        container.appendChild(paginationDiv);

        showPage(0);
    },

    _createCopySvgIcon: function() {
        var svgNS = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('width', '14');
        svg.setAttribute('height', '14');
        var rect = document.createElementNS(svgNS, 'rect');
        rect.setAttribute('x', '9'); rect.setAttribute('y', '9');
        rect.setAttribute('width', '13'); rect.setAttribute('height', '13');
        rect.setAttribute('rx', '2'); rect.setAttribute('ry', '2');
        var path = document.createElementNS(svgNS, 'path');
        path.setAttribute('d', 'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1');
        svg.appendChild(rect);
        svg.appendChild(path);
        return svg;
    },

    _createCheckSvgIcon: function() {
        var svgNS = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('width', '14');
        svg.setAttribute('height', '14');
        var polyline = document.createElementNS(svgNS, 'polyline');
        polyline.setAttribute('points', '20 6 9 17 4 12');
        svg.appendChild(polyline);
        return svg;
    },

    _buildOutputHeader: function(idx, type) {
        var outputId = 'output-' + Date.now() + '-' + idx;
        var wrapper = document.createElement('div');
        wrapper.className = 'cell-output-cell';
        wrapper.id = outputId;
        if (type) wrapper.setAttribute('data-output-type', type);

        var header = document.createElement('div');
        header.className = 'output-header';

        var copyBtn = document.createElement('button');
        copyBtn.className = 'copy-output-btn';
        copyBtn.title = window.i18n ? window.i18n.t('notebook.copyOutput') : 'Copy output';
        copyBtn.appendChild(this._createCopySvgIcon());
        var copyText = document.createElement('span');
        copyText.className = 'copy-text';
        copyText.textContent = window.i18n ? window.i18n.t('notebook.copy') : 'Copy';
        copyBtn.appendChild(copyText);

        var self = this;
        var copyLabel = window.i18n ? window.i18n.t('notebook.copy') : 'Copy';
        var copiedLabel = window.i18n ? window.i18n.t('notebook.copied') : 'Copied!';
        copyBtn.addEventListener('click', function() {
            var contentEl = wrapper.querySelector('.output-content');
            if (!contentEl) return;
            var textToCopy = '';
            var preEl = contentEl.querySelector('pre');
            if (preEl) {
                textToCopy = preEl.textContent;
            } else {
                var textEl = contentEl.querySelector('.cell-output-text');
                textToCopy = textEl ? textEl.textContent : contentEl.textContent;
            }
            navigator.clipboard.writeText(textToCopy).then(function() {
                copyBtn.textContent = '';
                copyBtn.appendChild(self._createCheckSvgIcon());
                var copiedText = document.createElement('span');
                copiedText.className = 'copy-text';
                copiedText.textContent = copiedLabel;
                copyBtn.appendChild(copiedText);
                copyBtn.classList.add('copied');
                setTimeout(function() {
                    copyBtn.textContent = '';
                    copyBtn.appendChild(self._createCopySvgIcon());
                    var resetText = document.createElement('span');
                    resetText.className = 'copy-text';
                    resetText.textContent = copyLabel;
                    copyBtn.appendChild(resetText);
                    copyBtn.classList.remove('copied');
                }, 2000);
            }).catch(function(err) {
                console.error('Failed to copy:', err);
                self.showToast(window.i18n ? window.i18n.t('notebook.failedToCopy') : 'Failed to copy', 2000);
            });
        });

        header.appendChild(copyBtn);
        wrapper.appendChild(header);
        return wrapper;
    },

    copyOutput: function(btnId, outputId) {
        // Legacy compat: kept for any existing onclick refs
        var btn = document.getElementById(btnId);
        var outputEl = document.getElementById(outputId);
        if (!btn || !outputEl) return;
        var contentEl = outputEl.querySelector('.output-content');
        if (!contentEl) return;
        var textToCopy = '';
        var preEl = contentEl.querySelector('pre');
        if (preEl) {
            textToCopy = preEl.textContent;
        } else {
            var textEl = contentEl.querySelector('.cell-output-text');
            textToCopy = textEl ? textEl.textContent : contentEl.textContent;
        }
        navigator.clipboard.writeText(textToCopy).then(function() {
            btn.classList.add('copied');
            setTimeout(function() { btn.classList.remove('copied'); }, 2000);
        }).catch(function(err) {
            console.error('Failed to copy:', err);
        });
    },

    updateCellOutputUI: function(cell) {
        var el = document.getElementById(cell.id);
        if (!el) return;

        el.classList.remove('running', 'error');

        var countEl = el.querySelector('.execution-count');
        if (countEl) {
            if (cell.execution_count !== null) {
                countEl.textContent = '[' + cell.execution_count + ']';
                el.classList.add('completed');
            } else {
                countEl.textContent = '[ ]';
            }
        }

        var existingDurationBadge = el.querySelector('.cell-duration-badge');
        if (existingDurationBadge) existingDurationBadge.remove();

        var outArea = el.querySelector('.cell-output-area');
        if (outArea) {
            var hasOutputs = cell.outputs && cell.outputs.length > 0;
            outArea.textContent = '';
            if (hasOutputs) {
                outArea.appendChild(this.renderOutputDOM(cell.outputs));
            }
            outArea.className = 'cell-output-area' + (hasOutputs ? ' has-output' : '');
        }
    },

    setActiveCell: function(id) {
        if (this.activeCellId === id) return;
        const oldEl = document.getElementById(this.activeCellId);
        if (oldEl) oldEl.classList.remove('active');

        this.activeCellId = id;
        const newEl = document.getElementById(id);
        if (newEl) {
            newEl.classList.add('active');
            // Ensure CM is refreshed when becoming active (fixes layout bugs)
            const cell = this.notebook.getCell(id);
            if (cell && cell.cm) setTimeout(() => cell.cm.refresh(), 10);
        }
        this.updateStatusBar();
    },

    addStarterCell: function(type, starterCode = null) {
        const defaultStarters = {
            'code': '# Your code here\nimport phonos\n\n# List available models\nmodels = phonos.list_models()\nprint("Models:", [m.name for m in models])',
            'markdown': '# New Section\n\nWrite your documentation here.',
            'c': '// C cell — compiled with gcc inside the kernel container.\n// Snippets without main() are wrapped automatically.\nprintf("Hello from C!\\n");',
            'cpp': '// C++ cell — compiled with g++ (C++20).\nstd::cout << "Hello from C++!" << std::endl;',
            'rust': '// Rust cell — compiled with rustc (2021 edition).\nprintln!("Hello from Rust!");',
            'r': '# R cell — persistent R session via rpy2 (state carries across cells).\nx <- rnorm(100)\nsummary(x)'
        };

        const source = starterCode || defaultStarters[type] || '';
        this.addCell(type, source);
    },

    addCell: function(type, source, afterId) {
        // Guard: a notebook must be loaded before we can append cells. The
        // file-tree panel can render before a notebook is selected (e.g.
        // during the welcome screen), so a stray double-click on a file
        // would otherwise crash with `this.notebook is null`. Show a toast
        // asking the user to open or create a notebook first.
        if (!this.notebook) {
            this.showToast && this.showToast(
                window.i18n ? window.i18n.t('notebook.openNotebookFirst') : 'Open or create a notebook first',
                3000
            );
            return null;
        }
        const cell = this.notebook.addCell(type, source, afterId);
        const newEl = this.createCellElement(cell);

        // Hide welcome/empty states when first cell is added
        const welcomeState = document.getElementById('welcome-state');
        if (welcomeState) welcomeState.style.display = 'none';
        const cellEmptyState = document.querySelector('.cell-empty-state');
        if (cellEmptyState) cellEmptyState.remove();

        var container = this._getActiveCellContainer();
        if (!container) container = document.getElementById('cells-container');

        if (afterId) {
            const afterElement = document.getElementById(afterId);
            if (afterElement) {
                afterElement.after(newEl);
            } else {
                container.appendChild(newEl);
            }
        } else {
            container.appendChild(newEl);
        }

        this.initCodeMirror(cell, newEl);
        this.setActiveCell(cell.id);
        if (cell.cm) {
            cell.cm.focus();
            cell.cm.refresh();
        }
        this.updateAllCellActions();
        this.updateStatusBar();
        return cell;
    },

    deleteCell: function(id) {
        if (confirm(window.i18n ? window.i18n.t('notebook.deleteCellConfirm') : 'Delete this cell?')) {
            if (this.notebook.removeCell(id)) {
                document.getElementById(id).remove();
                this.notebook.dirty = true;
                this.updateAllCellActions();
                this.updateStatusBar();
            } else {
                this.showToast(window.i18n ? window.i18n.t('notebook.cannotDeleteOnlyCell') : 'Cannot delete the only cell', 2000);
            }
        }
    },

    updateAllCellActions: function() {
        const cells = this.notebook.cells;
        cells.forEach((cell, index) => {
            const el = document.getElementById(cell.id);
            if (!el) return;
            const upBtns = el.querySelectorAll(`button[title="${window.i18n ? window.i18n.t('notebook.moveUp') : 'Move Up'}"]`);
            const downBtns = el.querySelectorAll(`button[title="${window.i18n ? window.i18n.t('notebook.moveDown') : 'Move Down'}"]`);
            upBtns.forEach(btn => {
                btn.style.visibility = index === 0 ? 'hidden' : 'visible';
            });
            downBtns.forEach(btn => {
                btn.style.visibility = index === cells.length - 1 ? 'hidden' : 'visible';
            });
        });
    },

    moveCell: function(id, dir) {
        const idx = this.notebook.cells.findIndex(c => c.id === id);
        if (idx === -1) return;

        const newIdx = idx + dir;
        if (newIdx < 0 || newIdx >= this.notebook.cells.length) return;

        const temp = this.notebook.cells[idx];
        this.notebook.cells[idx] = this.notebook.cells[newIdx];
        this.notebook.cells[newIdx] = temp;
        this.notebook.dirty = true;

        const el = document.getElementById(id);
        if (dir === -1) el.previousElementSibling.before(el);
        else el.nextElementSibling.after(el);

        const cell = this.notebook.getCell(id);
        if (cell.cm) {
            cell.cm.focus();
            cell.cm.refresh();
        }

        this.updateAllCellActions();
    },
    
    selectNextCell: function(id) {
        const idx = this.notebook.cells.findIndex(c => c.id === id);
        if (idx < this.notebook.cells.length - 1) {
            const nextId = this.notebook.cells[idx + 1].id;
            this.setActiveCell(nextId);
            const cell = this.notebook.getCell(nextId);
            if (cell.cm) cell.cm.focus();
            return true;
        }
        return false;
    },

    saveNotebook: async function() {
        if (!this.notebook) return;
        // Check if the active tab is a text file; if so, save it instead
        var activeTab = null;
        for (var ti = 0; ti < this._tabs.length; ti++) {
            if (this._tabs[ti].id === this._activeTabId) {
                activeTab = this._tabs[ti];
                break;
            }
        }
        if (activeTab && activeTab.type === 'text' && activeTab.cm) {
            this.saveTextTab(activeTab);
            return;
        }
        this.updateSaveStatus(window.i18n ? window.i18n.t('notebook.saving') : 'Saving...');
        try {
            var url = this.notebook.id ? '/api/notebooks/' + this.notebook.id : '/api/notebooks';
            var method = this.notebook.id ? 'PUT' : 'POST';
            var res = await fetch(url, {
                method: method,
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(this.notebook.toJSON())
            });
            if (res.ok) {
                var data = await res.json();
                if (!this.notebook.id) {
                    this.notebook.id = data.id;
                }
                // Only update URL bar for the primary notebook tab
                if (activeTab && activeTab.containerId === 'cells-container') {
                    history.replaceState(null, '', '?id=' + data.id);
                }
                this.notebook.dirty = false;
                this.updateSaveStatus(window.i18n ? window.i18n.t('notebook.saved') : 'Saved');

                // Mirror: if this notebook is linked to a source .nb file on
                // disk, push changes back to the file so both stay in sync.
                var srcFile = this._sourceFilePath();
                if (srcFile && this.notebook.id) {
                    this._syncToSource(this.notebook.id);
                }
            } else {
                this.updateSaveStatus(window.i18n ? window.i18n.t('notebook.errorSaving') : 'Error saving');
            }
        } catch (e) {
            this.updateSaveStatus(window.i18n ? window.i18n.t('notebook.errorSaving') : 'Error saving');
        }
    },

    autoSave: function() {
        if (this.notebook && this.notebook.dirty) this.saveNotebook();
    },

    // Cell execution results only lived in memory until the next 30s
    // autosave tick (or an unrelated edit happened to mark the notebook
    // dirty) — a page refresh shortly after running a cell lost the output
    // even though it rendered fine in the browser. Called right after a
    // cell finishes executing so results are persisted promptly instead of
    // waiting on the timer; debounced so "Run All Cells" finishing several
    // cells in quick succession triggers one save, not one per cell.
    scheduleOutputSave: function() {
        if (this._outputSaveTimer) clearTimeout(this._outputSaveTimer);
        this._outputSaveTimer = setTimeout(() => {
            this._outputSaveTimer = null;
            this.autoSave();
        }, 400);
    },

    updateSaveStatus: function(msg) {
        const el = document.getElementById('meta-saved') || document.getElementById('save-status');
        if (el) el.textContent = msg;
        var savedText = window.i18n ? window.i18n.t('notebook.saved') : 'Saved';
        var savingText = window.i18n ? window.i18n.t('notebook.saving') : 'Saving...';
        var unsavedText = window.i18n ? window.i18n.t('notebook.unsaved') : 'Unsaved';
        var errorSavingText = window.i18n ? window.i18n.t('notebook.errorSaving') : 'Error saving';
        if (msg === savedText) this.updateStatusBarSave('saved');
        else if (msg === savingText) this.updateStatusBarSave('saving');
        else if (msg === unsavedText || msg === errorSavingText) this.updateStatusBarSave('unsaved');
    },

    _sourceFilePath: function() {
        if (!this.notebook || !this.notebook.metadata) return null;
        var phonos = this.notebook.metadata.phonos;
        return phonos ? (phonos.source_file || null) : null;
    },

    _syncToSource: async function(notebookId) {
        try {
            await fetch('/api/notebooks/' + notebookId + '/sync-to-source', {
                method: 'POST',
                credentials: 'same-origin'
            });
        } catch (e) {
            // Silent — the sync is best-effort; the DB save went through.
        }
    },

    _checkSourceStatus: async function(notebookId) {
        try {
            var res = await fetch('/api/notebooks/' + notebookId + '/source-status', {
                credentials: 'same-origin'
            });
            if (!res.ok) return;
            var data = await res.json();
            if (data.linked && data.exists) {
                this._updateSyncIndicator(true);
            } else {
                this._updateSyncIndicator(false);
            }
        } catch (e) {
            // Silent
        }
    },

    _updateSyncIndicator: function(linked) {
        var el = document.getElementById('sync-status');
        if (!el) return;
        if (linked) {
            el.className = 'nb-sync-status linked';
            el.textContent = 'Source';
            el.title = 'Mirrored to source .nb file';
        } else {
            el.className = 'nb-sync-status';
            el.textContent = '';
            el.title = '';
        }
    },

    escapeHtml: function(text) {
        if (!text) return '';
        return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    },

    // --- Settings ---

    _settingsDefaults: {
        fontSize: 14,
        lineNumbers: true,
        lineWrap: false,
        tabSize: 4,
        autoSaveInterval: 30,
        cellTimeout: 300,
        defaultRuntime: 'python',
        quantum: {
            execution_mode: 'simulator',
            credential_source: 'platform',
            preferred_backend: 'automatic',
            auto_start_session: false,
            session_timeout_sec: 60,
        },
    },

    _getEffectiveSettings: function() {
        var defaults = Object.assign({}, this._settingsDefaults);
        // Layer user preferences
        if (this._userPreferences) {
            Object.assign(defaults, this._userPreferences);
        }
        // Layer notebook-level settings
        if (this.notebook && this.notebook.settings) {
            Object.assign(defaults, this.notebook.settings);
        }
        // Deep-merge quantum settings so nested defaults survive partial saves
        if (defaults.quantum && this._settingsDefaults.quantum) {
            defaults.quantum = Object.assign({}, this._settingsDefaults.quantum, defaults.quantum);
        }
        return defaults;
    },

    refreshSettingsPanel: function(force = false) {
        var self = this;
        var cached = !force ? this._getSidebarPanelCache('settings') : null;
        if (cached) {
            this._applySettingsPanelData(cached.preferences, cached.runtimes);
            return Promise.resolve();
        }

        if (!force && this._sidebarPanelInFlight.settings) {
            return this._sidebarPanelInFlight.settings;
        }

        var loadPromise = Promise.all([
            fetch('/api/notebooks/preferences', { credentials: 'same-origin' })
                .then(function(r) { return r.ok ? r.json() : { preferences: {} }; })
                .catch(function() { return { preferences: {} }; }),
            fetch('/api/notebooks/runtimes', { credentials: 'same-origin' })
                .then(function(r) { return r.ok ? r.json() : { runtimes: [] }; })
                .catch(function() { return { runtimes: [] }; })
        ]).then(function(results) {
            var preferences = results[0].preferences || {};
            var runtimes = Array.isArray(results[1].runtimes) ? results[1].runtimes : [];
            self._setSidebarPanelCache('settings', { preferences: preferences, runtimes: runtimes });
            self._applySettingsPanelData(preferences, runtimes);
        }).catch(function() {
            self._populateSettingsUI();
        }).finally(function() {
            self._sidebarPanelInFlight.settings = null;
        });

        this._sidebarPanelInFlight.settings = loadPromise;
        return loadPromise;
    },

    _applySettingsPanelData: function(preferences, runtimes) {
        this._userPreferences = preferences || {};
        this._populateSettingsUI();
        this._bindWolframAccountButtons();
        this.refreshWolframAccount(false);
        this._bindQuantumAccountButtons();
        this.refreshQuantumAccount(false);
        this.refreshEducationDefaults();
        var sel = document.getElementById('setting-default-runtime');
        if (!sel) return;
        var hasCustomRuntimes = (runtimes || []).some(function(rt) { return rt.name !== 'basic'; });
        if (hasCustomRuntimes) {
            sel.textContent = '';
            (runtimes || []).forEach(function(rt) {
                var opt = document.createElement('option');
                opt.value = rt.name;
                opt.textContent = rt.display_name;
                sel.appendChild(opt);
            });
        }
        // Always ensure the built-in quantum + hybrid options exist
        var qOpt = sel.querySelector('option[value="quantum"]');
        if (!qOpt) {
            qOpt = document.createElement('option');
            qOpt.value = 'quantum';
            qOpt.textContent = window.i18n ? window.i18n.t('notebook.runtimeQuantum') : 'Quantum Python 3.11';
            sel.appendChild(qOpt);
        }
        var hOpt = sel.querySelector('option[value="hybrid"]');
        if (!hOpt) {
            hOpt = document.createElement('option');
            hOpt.value = 'hybrid';
            hOpt.textContent = window.i18n ? window.i18n.t('notebook.runtimeHybrid') : 'Quantum + Wolfram (Experimental)';
            sel.appendChild(hOpt);
        }
        var s = this._getEffectiveSettings();
        sel.value = s.defaultRuntime || 'python';
    },

    _populateSettingsUI: function() {
        var s = this._getEffectiveSettings();
        var el;
        el = document.getElementById('setting-font-size');
        if (el) el.value = s.fontSize;
        el = document.getElementById('setting-line-numbers');
        if (el) el.checked = s.lineNumbers;
        el = document.getElementById('setting-line-wrap');
        if (el) el.checked = s.lineWrap;
        el = document.getElementById('setting-tab-size');
        if (el) el.value = s.tabSize;
        el = document.getElementById('setting-autosave');
        if (el) el.value = s.autoSaveInterval;
        el = document.getElementById('setting-cell-timeout');
        if (el) el.value = s.cellTimeout;
        el = document.getElementById('setting-default-runtime');
        if (el) el.value = s.defaultRuntime || 'python';
        // Quantum settings (from user quantum prefs)
        var qs = s.quantum || {};
        el = document.getElementById('setting-quantum-mode');
        if (el) el.value = qs.execution_mode || 'simulator';
        el = document.getElementById('setting-quantum-cred-source');
        if (el) el.value = qs.credential_source || 'platform';
        el = document.getElementById('setting-quantum-backend');
        if (el) el.value = qs.preferred_backend || 'automatic';
        this._populateQuantumBackends(qs.preferred_backend);
        el = document.getElementById('setting-quantum-auto-session');
        if (el) el.value = qs.auto_start_session ? '1' : '0';
        el = document.getElementById('setting-quantum-timeout');
        if (el) el.value = qs.session_timeout_sec || 60;
    },

    _readSettingsFromUI: function() {
        var el;
        var settings = {};
        el = document.getElementById('setting-font-size');
        if (el) settings.fontSize = parseInt(el.value) || 14;
        el = document.getElementById('setting-line-numbers');
        if (el) settings.lineNumbers = el.checked;
        el = document.getElementById('setting-line-wrap');
        if (el) settings.lineWrap = el.checked;
        el = document.getElementById('setting-tab-size');
        if (el) settings.tabSize = parseInt(el.value) || 4;
        el = document.getElementById('setting-autosave');
        if (el) settings.autoSaveInterval = parseInt(el.value) || 30;
        el = document.getElementById('setting-cell-timeout');
        if (el) settings.cellTimeout = parseInt(el.value) || 300;
        el = document.getElementById('setting-default-runtime');
        if (el) settings.defaultRuntime = el.value || 'python';
        // Quantum settings
        var qs = {};
        el = document.getElementById('setting-quantum-mode');
        if (el) qs.execution_mode = el.value;
        el = document.getElementById('setting-quantum-cred-source');
        if (el) qs.credential_source = el.value;
        el = document.getElementById('setting-quantum-backend');
        if (el) qs.preferred_backend = el.value;
        el = document.getElementById('setting-quantum-auto-session');
        if (el) qs.auto_start_session = el.value === '1';
        el = document.getElementById('setting-quantum-timeout');
        if (el) qs.session_timeout_sec = parseInt(el.value) || 60;
        settings.quantum = qs;
        return settings;
    },

    saveSettings: function() {
        var settings = this._readSettingsFromUI();
        var self = this;
        // Save to user preferences
        fetch('/api/notebooks/preferences', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ preferences: settings })
        }).then(function(r) {
            if (r.ok) {
                self._userPreferences = settings;
                self._applySettings(settings);
                var cachedSettings = self._getSidebarPanelCache('settings') || {};
                self._setSidebarPanelCache('settings', {
                    preferences: settings,
                    runtimes: Array.isArray(cachedSettings.runtimes) ? cachedSettings.runtimes : []
                });
                self.showToast(window.i18n ? window.i18n.t('notebook.settingsSaved') : 'Settings saved', 2000);
            } else {
                self.showToast(window.i18n ? window.i18n.t('notebook.failedToSaveSettings') : 'Failed to save settings', 2000);
            }
        }).catch(function() {
            self.showToast(window.i18n ? window.i18n.t('notebook.failedToSaveSettings') : 'Failed to save settings', 2000);
        });

        // Also save quantum settings (from the Quantum section in Settings panel)
        if (settings.quantum) {
            var qs = settings.quantum;
            fetch('/api/quantum/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({
                    execution_mode: qs.execution_mode || 'simulator',
                    credential_source: qs.credential_source || 'platform',
                    preferred_backend: qs.preferred_backend || 'automatic',
                    auto_start_session: qs.auto_start_session || false,
                    session_timeout_sec: qs.session_timeout_sec || 60,
                })
            }).catch(function() {});
        }

        // Also save education assignment defaults (from the Education
        // Defaults section in Settings panel) — see refreshEducationDefaults.
        var eduMaxSubsEl = document.getElementById('setting-edu-max-submissions');
        if (eduMaxSubsEl) {
            var eduLatePolicyEl = document.getElementById('setting-edu-late-policy');
            var eduTimeLimitEl = document.getElementById('setting-edu-time-limit');
            fetch('/api/education/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this._eduCsrfToken() },
                credentials: 'same-origin',
                body: JSON.stringify({
                    default_max_submissions_per_problem: parseInt(eduMaxSubsEl.value) || 1,
                    default_late_policy: eduLatePolicyEl ? eduLatePolicyEl.value : 'none',
                    default_time_limit_minutes: (eduTimeLimitEl && eduTimeLimitEl.value) ? parseInt(eduTimeLimitEl.value) : null,
                })
            }).catch(function() {});
        }

        // Also save to notebook-level settings if notebook is open
        if (this.notebook && this.notebook.id) {
            this.notebook.settings = settings;
            this.notebook.dirty = true;
        }
    },

    resetSettings: function() {
        this._userPreferences = {};
        if (this.notebook) this.notebook.settings = {};
        this._invalidateSidebarPanelCache('settings');
        this._populateSettingsUI();
        this._applySettings(this._settingsDefaults);
        this.showToast(window.i18n ? window.i18n.t('notebook.settingsReset') : 'Settings reset to defaults', 2000);
        // Also reset quantum settings via API
        fetch('/api/quantum/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({
                execution_mode: 'simulator',
                credential_source: 'platform',
                preferred_backend: 'automatic',
                auto_start_session: false,
                session_timeout_sec: 60,
            })
        }).catch(function() {});
        // Also reset education assignment defaults via API
        var self = this;
        fetch('/api/education/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this._eduCsrfToken() },
            credentials: 'same-origin',
            body: JSON.stringify({
                default_max_submissions_per_problem: 1,
                default_late_policy: 'none',
                default_time_limit_minutes: null,
            })
        }).then(function() { self.refreshEducationDefaults(); }).catch(function() {});
    },

    _applySettings: function(settings) {
        // Apply font size and editor options to all CodeMirror instances
        if (this.notebook) {
            this.notebook.cells.forEach(function(cell) {
                if (cell.cm) {
                    cell.cm.setOption('lineNumbers', settings.lineNumbers);
                    cell.cm.setOption('lineWrapping', settings.lineWrap);
                    cell.cm.setOption('tabSize', settings.tabSize);
                    cell.cm.setOption('indentUnit', settings.tabSize);
                    cell.cm.getWrapperElement().style.fontSize = settings.fontSize + 'px';
                    cell.cm.refresh();
                }
            });
        }

        // Update CONFIG for new cells
        CONFIG.codeMirrorDefaults.lineNumbers = settings.lineNumbers;
        CONFIG.codeMirrorDefaults.lineWrapping = settings.lineWrap;
        CONFIG.codeMirrorDefaults.tabSize = settings.tabSize;
        CONFIG.codeMirrorDefaults.indentUnit = settings.tabSize;

        // Update auto-save interval
        if (settings.autoSaveInterval && settings.autoSaveInterval > 0) {
            var newInterval = settings.autoSaveInterval * 1000;
            if (this._autoSaveTimer) clearInterval(this._autoSaveTimer);
            this._autoSaveTimer = setInterval(function() { NotebookModule.autoSave(); }, newInterval);
            CONFIG.autoSaveInterval = newInterval;
        }
    },

    // ---------- Wolfram Cloud account (per-user credentials) ----------------

    _wolframAccountState: { loading: false, meta: null, fetchedAt: 0 },
    _WOLFRAM_ACCOUNT_CACHE_TTL_MS: 30000,

    refreshWolframAccount: async function(force) {
        if (this._wolframAccountState.loading && !force) return this._wolframAccountState.meta;

        // Reuse a fresh cached copy so the panel doesn't flicker/populate
        // with "Not configured" on every open or i18n update.
        var now = Date.now();
        var cached = this._wolframAccountState.meta;
        var cacheAge = now - this._wolframAccountState.fetchedAt;
        if (!force && cached && cacheAge >= 0 && cacheAge < this._WOLFRAM_ACCOUNT_CACHE_TTL_MS) {
            this._renderWolframAccount(cached);
            return cached;
        }

        this._wolframAccountState.loading = true;
        try {
            var r = await fetch('/api/notebooks/wolfram/credentials', { credentials: 'same-origin' });
            var data = r.ok ? await r.json() : { configured: false, is_admin: false };
            this._wolframAccountState.meta = data;
            this._wolframAccountState.fetchedAt = Date.now();
            this._renderWolframAccount(data);
            return data;
        } catch (e) {
            // On error, keep any existing cached configured state so we don't
            // flash the "Not configured" prompt when the network hiccups.
            var fallback = cached || { configured: false, is_admin: false, error: String(e) };
            this._renderWolframAccount(fallback);
            return null;
        } finally {
            this._wolframAccountState.loading = false;
        }
    },

    _renderWolframAccount: function(meta) {
        var section = document.getElementById('wolfram-account-section');
        if (!section) return;
        var adminNote = document.getElementById('wolfram-account-admin-note');
        var userForm = document.getElementById('wolfram-account-user-form');
        var status = document.getElementById('wolfram-account-status');
        var badge = document.getElementById('wolfram-account-badge');
        var clearBtn = document.getElementById('wolfram-clear-btn');
        var usernameEl = document.getElementById('setting-wolfram-username');
        var passwordEl = document.getElementById('setting-wolfram-password');
        var message = document.getElementById('wolfram-account-message');
        if (message) { message.textContent = ''; message.className = 'nb-account-message'; }

        // Helper: translate a key, but only use the translation when it
        // actually differs from the requested key. i18n.t() returns the key
        // unchanged when translations haven't loaded yet, so falling back to
        // a literal English string keeps the UI readable in that window.
        function _t(key, fallback) {
            if (window.i18n && typeof window.i18n.t === 'function') {
                var v = window.i18n.t(key);
                if (v && v !== key) return v;
            }
            return fallback;
        }

        if (meta.is_admin) {
            if (adminNote) adminNote.style.display = '';
            if (userForm) userForm.style.display = 'none';
            if (badge) {
                badge.textContent = _t('notebook.wolframBadgeAdmin', 'Server account');
                badge.className = 'nb-account-section-badge ok';
            }
            if (status) status.textContent = '';
            return;
        }
        if (adminNote) adminNote.style.display = 'none';
        if (userForm) userForm.style.display = '';

        if (badge) {
            if (meta.configured) {
                badge.textContent = _t('notebook.wolframBadgeConfigured', 'Configured');
                badge.className = 'nb-account-section-badge ok';
            } else {
                badge.textContent = _t('notebook.wolframBadgeNotSet', 'Not configured');
                badge.className = 'nb-account-section-badge';
            }
        }

        if (status) {
            if (meta.configured) {
                var when = meta.last_verified_at ? new Date(meta.last_verified_at).toLocaleString() : 'never';
                var emailPart = meta.wolfram_id_email ? ' (' + meta.wolfram_id_email + ')' : '';
                status.textContent = _t('notebook.wolframStatusConfigured', 'Account saved')
                    + emailPart + '. ' + _t('notebook.wolframLastVerified', 'Last verified') + ': ' + when + '.';
                if (meta.last_error) {
                    status.textContent += ' ' + _t('notebook.wolframLastError', 'Last error') + ': ' + meta.last_error;
                }
            } else {
                status.textContent = _t('notebook.wolframStatusNotConfigured', 'No Wolfram Cloud account configured. Wolfram cells will fail until you save one.');
            }
        }
        if (clearBtn) clearBtn.style.display = meta.configured ? '' : 'none';
        if (usernameEl) {
            // Always sync the email field with the server state so a stale
            // value left over from a previous session is overwritten.
            if (meta.wolfram_id_email) {
                usernameEl.value = meta.wolfram_id_email;
            } else if (!meta.configured) {
                usernameEl.value = '';
            }
        }
        if (passwordEl) passwordEl.value = '';
    },

    _setWolframLoading: function(loading) {
        var btns = document.querySelectorAll('#wolfram-save-btn, #wolfram-verify-btn, #wolfram-clear-btn');
        btns.forEach(function(b) { b.disabled = loading; });
        var inputs = document.querySelectorAll('#setting-wolfram-username, #setting-wolfram-password');
        inputs.forEach(function(i) { i.disabled = loading; });
    },

    _validateEmail: function(email) {
        return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    },

    saveWolframAccount: async function() {
        var message = document.getElementById('wolfram-account-message');
        var username = (document.getElementById('setting-wolfram-username') || {}).value || '';
        var password = (document.getElementById('setting-wolfram-password') || {}).value || '';
        if (!username.trim() || !password) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Wolfram ID and password are required.'; }
            return;
        }
        if (!this._validateEmail(username.trim())) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Please enter a valid email address as Wolfram ID.'; }
            return;
        }
        this._setWolframLoading(true);
        if (message) { message.className = 'nb-account-message pending'; message.textContent = 'Saving...'; }
        try {
            var r = await fetch('/api/notebooks/wolfram/credentials', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ username: username.trim(), password: password }),
            });
            var data = await r.json().catch(function() { return {}; });
            if (r.ok) {
                if (message) { message.className = 'nb-account-message success'; message.textContent = 'Account saved.'; }
                var pwEl = document.getElementById('setting-wolfram-password'); if (pwEl) pwEl.value = '';
                // Optimistically mark the account as configured so the UI
                // immediately stops prompting even before the server round-trip.
                var existing = this._wolframAccountState.meta || {};
                this._wolframAccountState.meta = Object.assign({}, existing, {
                    configured: true,
                    is_admin: existing.is_admin || false,
                    wolfram_id_email: username.trim(),
                    source: 'user_saved'
                });
                this._wolframAccountState.fetchedAt = Date.now();
                this._renderWolframAccount(this._wolframAccountState.meta);
                await this.refreshWolframAccount(true);
                var self = this;
                setTimeout(function() {
                    if (message) message.textContent = '';
                }, 5000);
            } else {
                if (message) { message.className = 'nb-account-message error'; message.textContent = data.error || ('Save failed (HTTP ' + r.status + ')'); }
            }
        } catch (e) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Network error: ' + e; }
        } finally {
            this._setWolframLoading(false);
        }
    },

    verifyWolframAccount: async function() {
        var message = document.getElementById('wolfram-account-message');
        this._setWolframLoading(true);
        if (message) { message.className = 'nb-account-message pending'; message.textContent = 'Testing connection...'; }
        try {
            var r = await fetch('/api/notebooks/wolfram/credentials/verify', {
                method: 'POST', credentials: 'same-origin',
            });
            var data = await r.json().catch(function() { return {}; });
            if (r.ok && data.success) {
                if (message) { message.className = 'nb-account-message success'; message.textContent = 'Verified. Output: ' + (data.result || '(ok)'); }
                await this.refreshWolframAccount(true);
            } else {
                if (message) { message.className = 'nb-account-message error'; message.textContent = data.error || ('Verification failed (HTTP ' + r.status + ')'); }
                await this.refreshWolframAccount(true);
            }
        } catch (e) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Network error: ' + e; }
        } finally {
            this._setWolframLoading(false);
        }
    },

    clearWolframAccount: async function() {
        var message = document.getElementById('wolfram-account-message');
        if (!confirm('Remove your Wolfram Cloud account from Phonos? You can re-add it any time.')) return;
        this._setWolframLoading(true);
        if (message) { message.className = 'nb-account-message pending'; message.textContent = 'Removing...'; }
        try {
            var r = await fetch('/api/notebooks/wolfram/credentials', {
                method: 'DELETE', credentials: 'same-origin',
            });
            var data = await r.json().catch(function() { return {}; });
            if (r.ok) {
                if (message) { message.className = 'nb-account-message success'; message.textContent = 'Removed.'; }
                var u = document.getElementById('setting-wolfram-username'); if (u) u.value = '';
                var p = document.getElementById('setting-wolfram-password'); if (p) p.value = '';
                await this.refreshWolframAccount(true);
            } else {
                if (message) { message.className = 'nb-account-message error'; message.textContent = data.error || ('Remove failed (HTTP ' + r.status + ')'); }
            }
        } catch (e) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Network error: ' + e; }
        } finally {
            this._setWolframLoading(false);
        }
    },

    _toggleWolframPassword: function() {
        var pwEl = document.getElementById('setting-wolfram-password');
        if (!pwEl) return;
        var isPassword = pwEl.type === 'password';
        pwEl.type = isPassword ? 'text' : 'password';
        var btn = document.getElementById('wolfram-pw-toggle');
        if (btn) {
            btn.innerHTML = isPassword
                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
        }
    },

    _bindWolframAccountButtons: function() {
        // Buttons are bound once at init (see NotebookModule init code). This
        // method is kept as a no-op so callers (e.g. _applySettingsPanelData,
        // toggleAccountPanel) can remain idempotent. We re-bind defensively in
        // case the right sidebar content is ever replaced.
        var self = this;
        var saveBtn = document.getElementById('wolfram-save-btn');
        var verifyBtn = document.getElementById('wolfram-verify-btn');
        var clearBtn = document.getElementById('wolfram-clear-btn');
        var closeBtn = document.getElementById('nb-rightbar-close');
        // Use a custom marker instead of addEventListener's idempotency, so
        // re-binding doesn't accumulate listeners on re-rendered elements.
        var ensure = function(el, handler, marker) {
            if (!el || el.dataset[marker]) return;
            el.dataset[marker] = '1';
            el.addEventListener('click', handler);
        };
        ensure(saveBtn, function() { self.saveWolframAccount(); }, 'wSaveBound');
        ensure(verifyBtn, function() { self.verifyWolframAccount(); }, 'wVerifyBound');
        ensure(clearBtn, function() { self.clearWolframAccount(); }, 'wClearBound');
        ensure(closeBtn, function() { self.toggleAccountPanel(false); }, 'wCloseBound');
    },

    // ---------- IBM Quantum account (per-user credentials + settings) --------

    _quantumAccountState: { loading: false, meta: null, fetchedAt: 0, settings: null },
    _QUANTUM_ACCOUNT_CACHE_TTL_MS: 30000,

    refreshQuantumAccount: async function(force) {
        if (this._quantumAccountState.loading && !force) return this._quantumAccountState;
        var now = Date.now();
        var cached = this._quantumAccountState;
        var cacheAge = now - cached.fetchedAt;
        if (!force && cached.meta && cacheAge >= 0 && cacheAge < this._QUANTUM_ACCOUNT_CACHE_TTL_MS) {
            this._renderQuantumAccount(cached.meta, cached.settings);
            return cached;
        }
        this._quantumAccountState.loading = true;
        try {
            var r = await fetch('/api/quantum/settings', { credentials: 'same-origin' });
            var data = r.ok ? await r.json() : { settings: null, status: null };
            var r2 = await fetch('/api/quantum/credentials', { credentials: 'same-origin' });
            var credMeta = r2.ok ? await r2.json() : { has_key: false };
            this._quantumAccountState.meta = credMeta;
            this._quantumAccountState.settings = data.settings;
            this._quantumAccountState.status = data.status;
            this._quantumAccountState.fetchedAt = Date.now();
            this._renderQuantumAccount(credMeta, data.settings, data.status);
            return { meta: credMeta, settings: data.settings, status: data.status };
        } catch (e) {
            var fallbackMeta = (cached && cached.meta) || { has_key: false };
            var fallbackSettings = (cached && cached.settings) || {};
            this._renderQuantumAccount(fallbackMeta, fallbackSettings, null);
            return null;
        } finally {
            this._quantumAccountState.loading = false;
        }
    },

    _renderQuantumAccount: function(meta, settings, status) {
        meta = meta || {};
        settings = settings || {};
        status = status || {};
        var badge = document.getElementById('quantum-account-badge');
        var message = document.getElementById('quantum-account-message');
        if (message) { message.textContent = ''; message.className = 'nb-account-message'; }

        if (badge) {
            var mode = settings.execution_mode || 'simulator';
            badge.textContent = mode === 'hardware' ? 'Hardware' : 'Simulator';
            badge.className = mode === 'hardware' ? 'nb-account-section-badge ok' : 'nb-account-section-badge';
        }
        // Populate mode radios
        var modeRadios = document.querySelectorAll('input[name="nb-quantum-mode"]');
        for (var i = 0; i < modeRadios.length; i++) {
            modeRadios[i].checked = (modeRadios[i].value === (settings.execution_mode || 'simulator'));
        }
        // Populate cred source radios
        var credRadios = document.querySelectorAll('input[name="nb-quantum-cred-source"]');
        for (var i = 0; i < credRadios.length; i++) {
            credRadios[i].checked = (credRadios[i].value === (settings.credential_source || 'platform'));
        }
        // Show/hide personal section
        var personalSection = document.getElementById('quantum-account-personal-section');
        if (personalSection) {
            personalSection.style.display = (settings.credential_source === 'personal') ? '' : 'none';
        }
        // Credentials meta
        var hasKey = meta.has_key || false;
        var clearBtn = document.getElementById('quantum-clear-creds-btn');
        var keyLabel = document.getElementById('quantum-key-saved-label');
        if (hasKey) {
            if (clearBtn) clearBtn.style.display = '';
            if (keyLabel) keyLabel.style.display = '';
            var when = meta.last_verified_at ? new Date(meta.last_verified_at).toLocaleString() : 'never';
            var credStatus = document.getElementById('quantum-cred-status');
            if (credStatus) {
                credStatus.textContent = 'Credentials saved. Last verified: ' + when + (meta.last_error ? ' Error: ' + meta.last_error : '');
            }
        } else {
            if (clearBtn) clearBtn.style.display = 'none';
            if (keyLabel) keyLabel.style.display = 'none';
        }
        // Upgrade the backend picker with live IBM data (queue depth, qubits).
        this._populateQuantumBackends(settings.preferred_backend);
    },

    // Load this teacher's saved assignment defaults (submission limit, late
    // policy, time limit) into the Settings panel. These apply whenever they
    // create an assignment without explicitly overriding a field — in
    // particular the notebook IDE's one-click "Make Assignment" button,
    // which only sends notebook_id + title and otherwise always fell back to
    // a hardcoded submission limit of 1.
    refreshEducationDefaults: async function() {
        try {
            var r = await fetch('/api/education/settings', { credentials: 'same-origin' });
            if (!r.ok) return;
            var s = await r.json();
            var el;
            el = document.getElementById('setting-edu-max-submissions');
            if (el) el.value = s.default_max_submissions_per_problem || 1;
            el = document.getElementById('setting-edu-late-policy');
            if (el) el.value = s.default_late_policy || 'none';
            el = document.getElementById('setting-edu-time-limit');
            if (el) el.value = s.default_time_limit_minutes || '';
        } catch (e) { /* non-fatal — fields keep their HTML defaults */ }
    },

    // Fetch live backend info (/api/quantum/backends) and rebuild the two
    // backend <select>s with queue depth + qubit count, least busy first.
    // Falls back silently to the static hardcoded options on any failure.
    _quantumBackendsFetch: null,
    _quantumBackendsFetchedAt: 0,
    _populateQuantumBackends: async function(preferredValue) {
        try {
            var now = Date.now();
            if (!this._quantumBackendsFetch || (now - this._quantumBackendsFetchedAt) > 300000) {
                this._quantumBackendsFetchedAt = now;
                this._quantumBackendsFetch = fetch('/api/quantum/backends', { credentials: 'same-origin' })
                    .then(function(r) { return r.ok ? r.json() : null; })
                    .catch(function() { return null; });
            }
            var data = await this._quantumBackendsFetch;
            if (!data || !Array.isArray(data.hardware) || data.hardware.length === 0) return;
            ['setting-quantum-backend', 'quantum-backend-select'].forEach(function(id) {
                var sel = document.getElementById(id);
                if (!sel) return;
                var current = preferredValue || sel.value || 'automatic';
                sel.textContent = '';
                var auto = document.createElement('option');
                auto.value = 'automatic';
                auto.textContent = 'Automatic (Recommended)';
                sel.appendChild(auto);
                data.hardware.forEach(function(b) {
                    var o = document.createElement('option');
                    o.value = b.id;
                    var extra = [];
                    if (b.num_qubits) extra.push(b.num_qubits + ' qubits');
                    if (typeof b.pending_jobs === 'number' && b.pending_jobs >= 0) extra.push('queue ' + b.pending_jobs);
                    // Mirror the IBM dashboard state (paused / maintenance):
                    // jobs submitted there wait until the device resumes.
                    var msg = String(b.status_msg || 'active').toLowerCase();
                    if (b.operational === false) { extra.push('offline'); o.disabled = true; }
                    else if (msg !== 'active') { extra.push(msg); }
                    o.textContent = (b.label || b.id) + (extra.length ? ' — ' + extra.join(', ') : '');
                    sel.appendChild(o);
                });
                sel.value = current;
                if (sel.selectedIndex < 0) sel.value = 'automatic';
            });
        } catch (e) { /* static options remain */ }
    },

    _setQuantumLoading: function(loading) {
        var btns = document.querySelectorAll('#quantum-save-btn, #quantum-save-creds-btn, #quantum-verify-creds-btn, #quantum-clear-creds-btn');
        btns.forEach(function(b) { if (b) b.disabled = loading; });
    },

    saveQuantumSettings: async function() {
        var message = document.getElementById('quantum-account-message');
        var modeRadios = document.querySelectorAll('input[name="nb-quantum-mode"]');
        var credRadios = document.querySelectorAll('input[name="nb-quantum-cred-source"]');
        var mode = 'simulator', credSource = 'platform';
        for (var i = 0; i < modeRadios.length; i++) {
            if (modeRadios[i].checked) { mode = modeRadios[i].value; break; }
        }
        for (var i = 0; i < credRadios.length; i++) {
            if (credRadios[i].checked) { credSource = credRadios[i].value; break; }
        }
        var payload = {
            execution_mode: mode,
            credential_source: credSource,
            preferred_backend: (document.getElementById('quantum-backend-select') || {}).value || 'automatic',
            auto_start_session: false,
            session_timeout_sec: 60,
        };
        this._setQuantumLoading(true);
        if (message) { message.className = 'nb-account-message pending'; message.textContent = 'Saving...'; }
        try {
            var r = await fetch('/api/quantum/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify(payload),
            });
            var data = await r.json().catch(function() { return {}; });
            if (r.ok) {
                if (message) { message.className = 'nb-account-message success'; message.textContent = 'Settings saved.'; }
                await this.refreshQuantumAccount(true);
                var self = this;
                setTimeout(function() { if (message) message.textContent = ''; }, 5000);
            } else {
                if (message) { message.className = 'nb-account-message error'; message.textContent = data.error || ('Save failed (HTTP ' + r.status + ')'); }
            }
        } catch (e) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Network error: ' + e; }
        } finally {
            this._setQuantumLoading(false);
        }
    },

    saveQuantumCredentials: async function() {
        var message = document.getElementById('quantum-cred-message');
        var apiKey = (document.getElementById('quantum-api-key') || {}).value || '';
        if (!apiKey.trim()) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'API key is required.'; }
            return;
        }
        var payload = { api_key: apiKey.trim() };
        var instanceEl = document.getElementById('quantum-instance');
        if (instanceEl && instanceEl.value.trim()) payload.instance_crn = instanceEl.value.trim();
        var backendEl = document.getElementById('quantum-backend-select');
        if (backendEl && backendEl.value) payload.preferred_backend = backendEl.value;
        var statusEl = document.getElementById('quantum-cred-status');
        this._setQuantumLoading(true);
        if (message) { message.className = 'nb-account-message pending'; message.textContent = 'Saving credentials...'; }
        try {
            var r = await fetch('/api/quantum/credentials', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify(payload),
            });
            var data = await r.json().catch(function() { return {}; });
            if (r.ok) {
                if (message) { message.className = 'nb-account-message success'; message.textContent = 'Credentials saved (encrypted at rest).'; }
                if (document.getElementById('quantum-api-key')) document.getElementById('quantum-api-key').value = '';
                await this.refreshQuantumAccount(true);
                var self = this;
                setTimeout(function() { if (message) message.textContent = ''; }, 5000);
            } else {
                if (message) { message.className = 'nb-account-message error'; message.textContent = data.error || ('Save failed (HTTP ' + r.status + ')'); }
            }
        } catch (e) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Network error: ' + e; }
        } finally {
            this._setQuantumLoading(false);
        }
    },

    verifyQuantumCredentials: async function() {
        var message = document.getElementById('quantum-cred-message');
        this._setQuantumLoading(true);
        if (message) { message.className = 'nb-account-message pending'; message.textContent = 'Testing connection...'; }
        try {
            var r = await fetch('/api/quantum/credentials/verify', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ test_personal: true }),
            });
            var data = await r.json().catch(function() { return {}; });
            if (r.ok && data.success) {
                if (message) { message.className = 'nb-account-message success'; message.textContent = 'Connected. ' + (data.backend_count || '') + ' backends available.'; }
                await this.refreshQuantumAccount(true);
            } else {
                if (message) { message.className = 'nb-account-message error'; message.textContent = data.error || ('Verification failed (HTTP ' + r.status + ')'); }
                await this.refreshQuantumAccount(true);
            }
        } catch (e) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Network error: ' + e; }
        } finally {
            this._setQuantumLoading(false);
        }
    },

    clearQuantumCredentials: async function() {
        var message = document.getElementById('quantum-cred-message');
        if (!confirm('Remove your personal IBM Quantum credentials?')) return;
        this._setQuantumLoading(true);
        if (message) { message.className = 'nb-account-message pending'; message.textContent = 'Removing...'; }
        try {
            var r = await fetch('/api/quantum/credentials', {
                method: 'DELETE', credentials: 'same-origin',
            });
            var data = await r.json().catch(function() { return {}; });
            if (r.ok) {
                if (message) { message.className = 'nb-account-message success'; message.textContent = 'Removed.'; }
                await this.refreshQuantumAccount(true);
            } else {
                if (message) { message.className = 'nb-account-message error'; message.textContent = data.error || ('Remove failed (HTTP ' + r.status + ')'); }
            }
        } catch (e) {
            if (message) { message.className = 'nb-account-message error'; message.textContent = 'Network error: ' + e; }
        } finally {
            this._setQuantumLoading(false);
        }
    },

    _toggleQuantumPassword: function() {
        var pwEl = document.getElementById('quantum-api-key');
        if (!pwEl) return;
        var isPassword = pwEl.type === 'password';
        pwEl.type = isPassword ? 'text' : 'password';
        var btn = document.getElementById('quantum-pw-toggle');
        if (btn) btn.textContent = isPassword ? 'Hide' : 'Show';
    },

    _bindQuantumAccountButtons: function() {
        var self = this;
        var saveBtn = document.getElementById('quantum-save-btn');
        var saveCredsBtn = document.getElementById('quantum-save-creds-btn');
        var verifyCredsBtn = document.getElementById('quantum-verify-creds-btn');
        var clearCredsBtn = document.getElementById('quantum-clear-creds-btn');
        var toggleKeyBtn = document.getElementById('quantum-pw-toggle');
        var closeBtn = document.getElementById('nb-rightbar-close');
        var refreshBtn = document.getElementById('quantum-refresh-btn');
        var ensure = function(el, handler, marker) {
            if (!el || el.dataset[marker]) return;
            el.dataset[marker] = '1';
            el.addEventListener('click', handler);
        };
        ensure(saveBtn, function() { self.saveQuantumSettings(); }, 'qSaveBound');
        ensure(saveCredsBtn, function() { self.saveQuantumCredentials(); }, 'qSaveCredsBound');
        ensure(verifyCredsBtn, function() { self.verifyQuantumCredentials(); }, 'qVerifyBound');
        ensure(clearCredsBtn, function() { self.clearQuantumCredentials(); }, 'qClearBound');
        ensure(toggleKeyBtn, function() { self._toggleQuantumPassword(); }, 'qToggleKeyBound');
        ensure(refreshBtn, function() { self.refreshQuantumAccount(true); }, 'qRefreshBound');
        ensure(closeBtn, function() { self.toggleAccountPanel(false); }, 'qCloseBound');
    },

    _initQuantumAccountGate: function() {
        var self = this;
        this.refreshQuantumAccount(false).then(function(result) {
            // No auto-prompt for quantum — it's opt-in through settings.
        }).catch(function() {});
    },

    // UI Helpers
    setupUI: function() {
        // Initialize tab bar
        this.initTabBar();

        // Toggle Sidebar
        // Phones: the sidebar is an overlay and starts closed; the desktop notice can be dismissed.
        if (window.matchMedia && window.matchMedia('(max-width: 768px)').matches) {
            document.getElementById('nb-sidebar')?.classList.add('collapsed');
        }
        (function () {
            var notice = document.getElementById('nb-mobile-notice');
            if (!notice) return;
            try { if (sessionStorage.getItem('nbMobileNoticeDismissed') === '1') notice.hidden = true; } catch (e) { /* storage blocked */ }
            document.getElementById('nb-mobile-notice-close')?.addEventListener('click', function () {
                notice.hidden = true;
                try { sessionStorage.setItem('nbMobileNoticeDismissed', '1'); } catch (e) { /* storage blocked */ }
            });
        })();

        document.getElementById('toggle-sidebar')?.addEventListener('click', () => {
            document.getElementById('nb-sidebar').classList.toggle('collapsed');
        });

        // Activity Bar
        document.querySelectorAll('.nb-activity-item[data-tab]').forEach(btn => {
            btn.addEventListener('click', () => {
                const tab = btn.dataset.tab;
                // Selecting any sidebar tab dismisses the full-screen docs reader.
                if (window.NotebookDocs && NotebookDocs.isOpen()) NotebookDocs.close();
                const currentTab = document.querySelector('.nb-activity-item.active')?.dataset.tab;
                if (currentTab === tab && tab !== 'account' && tab !== 'settings') return;

                // Account and Settings toggle right sidebars ADDITIONALLY —
                // the left sidebar keeps whatever tab was open, so these
                // branches must run before the active-state reset below.
                if (tab === 'account') {
                    this.toggleAccountPanel();
                    return;
                }
                if (tab === 'settings') {
                    this.toggleSettingsPanel();
                    return;
                }

                document.querySelectorAll('.nb-panel').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.nb-activity-item').forEach(b => b.classList.remove('active'));

                document.getElementById(`panel-${tab}`)?.classList.add('active');
                btn.classList.add('active');

                if (tab === 'files') this.refreshFileExplorer();
                if (tab === 'notebooks') this.refreshNotebooksList();
                if (tab === 'kernels') this.refreshKernelsList();
                if (tab === 'projects' && window.BureauProjects) BureauProjects.refresh();
                // if (tab === 'models') this.refreshModelsPanel(); // hidden for now
                if (tab === 'outline') this.refreshOutline();
                if (tab === 'history') {
                    if (!HistoryManager.notebookId && this.notebookId) {
                        HistoryManager.init(this.notebookId);
                    }
                    HistoryManager.loadHistory().then(() => {
                        HistoryManager.renderHistoryPanel(document.getElementById('history-commits'));
                    });
                }
                if (tab === 'terminal') {
                    if (!this.terminal.term) this.terminal.init('terminal-container');
                    this.terminal.connect();
                    setTimeout(() => this.terminal.fit(), 100);
                }
            });
        });

        // Panel Refresh Buttons
        document.getElementById('refresh-files-btn')?.addEventListener('click', () => this.refreshFileExplorer(true));

        // File sort button — cycles name / modified / size and re-renders.
        // The chosen order is a per-user preference, so restore it before the
        // first render instead of silently resetting to "name" every reload.
        const sortBtn = document.getElementById('nb-file-sort-btn');
        if (sortBtn && !sortBtn.dataset.bound) {
            sortBtn.dataset.bound = '1';
            try {
                const savedSort = localStorage.getItem('phonos-notebook-file-sort');
                if (['name', 'modified', 'size'].includes(savedSort)) this._fileSort = savedSort;
            } catch (e) {}
            this._syncFileSortButton();
            sortBtn.addEventListener('click', () => this.cycleFileSort());
        }

        // New Folder button
        const newFolderBtn = document.getElementById('new-folder-btn');
        if (newFolderBtn && !newFolderBtn.dataset.bound) {
            newFolderBtn.dataset.bound = '1';
            newFolderBtn.addEventListener('click', () => this._createNewFolder());
        }

        // Upload File button - opens native file picker, uploads selected files to current project.
        const uploadBtn = document.getElementById('upload-file-btn');
        if (uploadBtn && !uploadBtn.dataset.bound) {
            uploadBtn.dataset.bound = '1';
            uploadBtn.addEventListener('click', () => this._openFileUploadPicker());
        }
        document.getElementById('refresh-notebooks-btn')?.addEventListener('click', () => this.refreshNotebooksList(true));
        document.getElementById('notebooks-search')?.addEventListener('input', (e) => {
            this.notebookSearchTerm = (e.target.value || '').trim().toLowerCase();
            const cached = this._getSidebarPanelCache('notebooks');
            const container = document.getElementById('notebooks-list');
            if (cached && container) {
                this._renderNotebooksList(container, cached.notebooks || []);
            } else {
                this.refreshNotebooksList();
            }
        });
        document.getElementById('refresh-kernels-btn')?.addEventListener('click', () => this.refreshKernelsList(true));
        // document.getElementById('refresh-models-btn')?.addEventListener('click', () => this.refreshModelsPanel(true)); // hidden for now

        // Split panel buttons
        document.getElementById('split-close-btn')?.addEventListener('click', () => this.closeSplitPanel());
        document.getElementById('split-save-btn')?.addEventListener('click', () => this._saveSplitFile());
        this._initSplitResize();

        // Terminal Buttons
        document.getElementById('new-terminal-btn')?.addEventListener('click', () => this.terminal.connect());
        document.getElementById('kill-terminal-btn')?.addEventListener('click', () => this.terminal.disconnect());

        // Stop All Kernels button
        document.getElementById('stop-all-kernels-btn')?.addEventListener('click', () => this.stopAllSessions());

        // New Notebook Modal
        this.setupNewNotebookModal();

        // Import Modal
        this.setupImportModal();

        // Shortcuts Modal
        document.getElementById('shortcuts-trigger')?.addEventListener('click', () => {
            document.getElementById('shortcuts-modal')?.classList.add('active');
        });

        // Modal close buttons
        document.querySelectorAll('.nb-modal-overlay .modal-close').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.closest('.nb-modal-overlay')?.classList.remove('active');
            });
        });

        // Close modals on overlay click
        document.querySelectorAll('.nb-modal-overlay').forEach(overlay => {
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) overlay.classList.remove('active');
            });
        });

        // Settings buttons
        document.getElementById('save-settings-btn')?.addEventListener('click', () => this.saveSettings());
        document.getElementById('reset-settings-btn')?.addEventListener('click', () => this.resetSettings());

        // Wolfram account buttons (right sidebar) - bind at init so they work
        // even if the right sidebar is opened before settings are loaded.
        document.getElementById('wolfram-save-btn')?.addEventListener('click', () => this.saveWolframAccount());
        document.getElementById('wolfram-verify-btn')?.addEventListener('click', () => this.verifyWolframAccount());
        document.getElementById('wolfram-clear-btn')?.addEventListener('click', () => this.clearWolframAccount());
        document.getElementById('nb-rightbar-close')?.addEventListener('click', () => this.toggleAccountPanel(false));
        document.getElementById('nb-settings-rightbar-close')?.addEventListener('click', () => this.toggleSettingsPanel(false));

        // Outline Panel
        document.getElementById('refresh-outline-btn')?.addEventListener('click', () => this.refreshOutline());

        // Command Palette
        this.setupCommandPalette();

        // Status bar: click active cell indicator to scroll
        document.getElementById('status-active-cell')?.addEventListener('click', () => {
            if (this.activeCellId) this._scrollToCell(this.activeCellId);
        });

        // Input Request Modal Setup
        this.setupInputModal();

        // Activity bar drag-and-drop reordering
        this._initActivityDragDrop();
    },

    // ======================================================================
    // ACTIVITY BAR DRAG & DROP REORDERING
    // ======================================================================

    _initActivityDragDrop: function() {
        var container = document.querySelector('.nb-activity-top');
        if (!container) return;

        var draggedItem = null;
        var self = this;

        container.addEventListener('dragstart', function(e) {
            var item = e.target.closest('.nb-activity-item[data-tab]');
            if (!item) return;
            draggedItem = item;
            item.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', item.dataset.tab);

            // Add drag-over class to container for indicator styling
            container.classList.add('drag-over');
        });

        container.addEventListener('dragover', function(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';

            var target = e.target.closest('.nb-activity-item[data-tab]');
            if (!target || target === draggedItem) return;

            // Determine whether to insert before or after the target
            var rect = target.getBoundingClientRect();
            var midY = rect.top + rect.height / 2;
            var isAbove = e.clientY < midY;

            // Remove previous target classes
            container.querySelectorAll('.drag-target-top, .drag-target-bottom').forEach(function(el) {
                el.classList.remove('drag-target-top', 'drag-target-bottom');
            });

            target.classList.add(isAbove ? 'drag-target-top' : 'drag-target-bottom');
            clearTimeout(container._dragIndicatorTimeout);
            container._dragIndicatorTimeout = setTimeout(function() {
                target.classList.remove('drag-target-top', 'drag-target-bottom');
            }, 150);
        });

        container.addEventListener('dragleave', function(e) {
            if (!container.contains(e.relatedTarget)) {
                container.querySelectorAll('.drag-target-top, .drag-target-bottom').forEach(function(el) {
                    el.classList.remove('drag-target-top', 'drag-target-bottom');
                });
            }
        });

        container.addEventListener('drop', function(e) {
            e.preventDefault();
            container.classList.remove('drag-over');
            container.querySelectorAll('.drag-target-top, .drag-target-bottom').forEach(function(el) {
                el.classList.remove('drag-target-top', 'drag-target-bottom');
            });

            var target = e.target.closest('.nb-activity-item[data-tab]');
            if (!target || !draggedItem || target === draggedItem) return;

            var rect = target.getBoundingClientRect();
            var midY = rect.top + rect.height / 2;
            var isAbove = e.clientY < midY;

            if (isAbove) {
                target.parentNode.insertBefore(draggedItem, target);
            } else {
                target.parentNode.insertBefore(draggedItem, target.nextSibling);
            }

            self._saveActivityOrder();
        });

        container.addEventListener('dragend', function() {
            container.classList.remove('drag-over');
            container.querySelectorAll('.drag-target-top, .drag-target-bottom, .dragging').forEach(function(el) {
                el.classList.remove('drag-target-top', 'drag-target-bottom', 'dragging');
            });
            draggedItem = null;
        });
    },

    _saveActivityOrder: function() {
        var container = document.querySelector('.nb-activity-top');
        if (!container) return;
        var order = [];
        container.querySelectorAll('.nb-activity-item[data-tab]').forEach(function(btn) {
            if (btn.dataset.tab) order.push(btn.dataset.tab);
        });
        try { localStorage.setItem('phonos-activity-order', JSON.stringify(order)); } catch (e) {}
    },

    _restoreActivityOrder: function() {
        try {
            var saved = localStorage.getItem('phonos-activity-order');
            if (!saved) return;
            var order = JSON.parse(saved);
            if (!Array.isArray(order) || order.length === 0) return;

            var container = document.querySelector('.nb-activity-top');
            if (!container) return;

            var items = {};
            container.querySelectorAll('.nb-activity-item[data-tab]').forEach(function(btn) {
                items[btn.dataset.tab] = btn;
            });

            // Reorder items according to saved order
            order.forEach(function(tab) {
                var btn = items[tab];
                if (btn && btn.parentNode === container) {
                    // Don't reorder the logo element
                    container.appendChild(btn);
                }
            });
        } catch (e) {}
    },

    setupInputModal: function() {
        const modal = document.getElementById('input-request-modal');
        const inputField = document.getElementById('input-request-field');
        const submitBtn = document.getElementById('input-submit-btn');
        const cancelBtn = document.getElementById('input-cancel-btn');
        const closeBtn = document.getElementById('input-modal-close');

        if (!modal || !inputField) return;

        // Submit on button click
        submitBtn?.addEventListener('click', () => {
            this.submitInput(inputField.value);
            modal.classList.remove('active');
            inputField.value = '';
        });

        // Submit on Enter key
        inputField?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                this.submitInput(inputField.value);
                modal.classList.remove('active');
                inputField.value = '';
            }
            if (e.key === 'Escape') {
                modal.classList.remove('active');
                inputField.value = '';
                // Send empty string on cancel
                this.submitInput('');
            }
        });

        // Cancel button
        cancelBtn?.addEventListener('click', () => {
            modal.classList.remove('active');
            inputField.value = '';
            this.submitInput('');
        });

        // Close button
        closeBtn?.addEventListener('click', () => {
            modal.classList.remove('active');
            inputField.value = '';
            this.submitInput('');
        });
    },

    showInputDialog: function(data) {
        const cellId = this.activeCellId;
        const cell = cellId ? this.notebook.getCell(cellId) : null;
        const el = cellId ? document.getElementById(cellId) : null;

        if (!el || !cell) {
            console.warn('No active cell found for input request');
            return;
        }

        const outArea = el.querySelector('.cell-output-area');
        if (!outArea) return;

        const promptText = data.prompt || (window.i18n ? window.i18n.t('notebook.enterValue') : 'Enter value:');
        const placeholder = data.password ? '··········' : (window.i18n ? window.i18n.t('notebook.typeYourAnswer') : 'Type your answer...');

        const inputHtml = `
            <div class="cell-input-request">
                <div class="cell-input-prompt">${this.escapeHtml(promptText)}</div>
                <div class="cell-input-row">
                    <input type="${data.password ? 'password' : 'text'}"
                           class="cell-input-field"
                           placeholder="${this.escapeHtml(placeholder)}"
                           autocomplete="off"
                           id="inline-input-${data.msg_id}">
                    <button class="cell-input-submit" id="inline-submit-${data.msg_id}">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="22" y1="2" x2="11" y2="13"/>
                            <polygon points="22 2 15 22 11 13 2 9 22 2"/>
                        </svg>
                    </button>
                </div>
            </div>
        `;

        outArea.classList.add('has-output');
        outArea.insertAdjacentHTML('beforeend', inputHtml);

        const inputField = document.getElementById(`inline-input-${data.msg_id}`);
        const submitBtn = document.getElementById(`inline-submit-${data.msg_id}`);

        if (inputField) {
            inputField.focus();

            const submitHandler = () => {
                const value = inputField.value;
                this.submitInput(value, data.msg_id);
                inputField.closest('.cell-input-request').remove();
                if (outArea.children.length === 0) {
                    outArea.classList.remove('has-output');
                }
            };

            inputField.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    submitHandler();
                }
                if (e.key === 'Escape') {
                    inputField.value = '';
                    this.submitInput('', data.msg_id);
                    inputField.closest('.cell-input-request').remove();
                    if (outArea.children.length === 0) {
                        outArea.classList.remove('has-output');
                    }
                }
            });

            if (submitBtn) {
                submitBtn.addEventListener('click', submitHandler);
            }
        }
    },

    submitInput: function(value, msgId) {
        if (this.kernel && this.kernel.isConnected()) {
            this.kernel.sendInput(value, msgId);
        }
    },

    setupNewNotebookModal: function() {
        const modal = document.getElementById('new-notebook-modal');
        const nameInput = document.getElementById('new-notebook-name');

        // Open modal buttons
        document.getElementById('new-notebook-trigger')?.addEventListener('click', () => {
            this.populateNewNotebookProjects();
            modal?.classList.add('active');
            // Focus the name field so the user can type immediately.
            setTimeout(() => { nameInput?.focus(); nameInput?.select(); }, 50);
        });

        // Template selection
        document.querySelectorAll('.nb-template-opt').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.nb-template-opt').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                // Suggest a name matching the template while the field is empty.
                if (nameInput && !nameInput.value.trim()) {
                    const label = btn.querySelector('.nb-template-name');
                    if (label) nameInput.placeholder = label.textContent.trim() + ' 01';
                }
            });
        });

        // Enter in the name field creates the notebook.
        nameInput?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.createNotebook();
            }
        });

        // Escape closes whichever modal is open.
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape') return;
            const open = document.querySelector('.nb-modal-overlay.active');
            if (open) open.classList.remove('active');
        });

        // Create button
        document.getElementById('create-notebook-btn')?.addEventListener('click', () => this.createNotebook());
    },

    populateNewNotebookProjects: function() {
        const select = document.getElementById('new-notebook-project');
        if (!select) return;

        select.textContent = '';
        this.projects.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.name;
            select.appendChild(opt);
        });

        // Pre-select current project
        if (this.projectId) {
            select.value = this.projectId;
        }
    },

    createNotebook: async function() {
        const nameInput = document.getElementById('new-notebook-name');
        const projectSelect = document.getElementById('new-notebook-project');
        const templateBtn = document.querySelector('.nb-template-opt.active');
        const createBtn = document.getElementById('create-notebook-btn');

        // Default the name to the selected template's label instead of a
        // generic "Untitled Notebook".
        const templateLabel = templateBtn?.querySelector('.nb-template-name')?.textContent.trim();
        const name = nameInput?.value.trim() || (templateLabel ? templateLabel + ' Notebook' : 'Untitled Notebook');
        const projectId = projectSelect?.value;
        const templateId = templateBtn?.dataset.template || 'blank';

        if (!projectId) {
            this.showToast(window.i18n ? window.i18n.t('notebook.selectProjectFirst') : 'Please select a project first', 3000);
            projectSelect?.focus();
            return;
        }

        // Guard against double-submit while the request is in flight.
        if (createBtn?.disabled) return;
        const originalLabel = createBtn ? createBtn.textContent : '';
        if (createBtn) {
            createBtn.disabled = true;
            createBtn.textContent = window.i18n ? window.i18n.t('notebook.creating') : 'Creating…';
        }
        const restoreBtn = () => {
            if (createBtn) {
                createBtn.disabled = false;
                createBtn.textContent = originalLabel;
            }
        };

        try {
            const res = await fetch(`/api/notebooks/templates/${templateId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, project_id: parseInt(projectId) })
            });

            if (res.ok) {
                const data = await res.json();
                document.getElementById('new-notebook-modal')?.classList.remove('active');
                window.location.href = `/notebook?id=${data.id}&project_id=${projectId}`;
            } else {
                const err = await res.json();
                restoreBtn();
                this.showToast((window.i18n ? window.i18n.t('notebook.failedToCreateNotebook') : 'Failed to create notebook: ') + (err.error || (window.i18n ? window.i18n.t('notebook.unknownError') : 'Unknown error')));
            }
        } catch (e) {
            console.error(e);
            restoreBtn();
            this.showToast((window.i18n ? window.i18n.t('notebook.failedToCreateNotebook') : 'Failed to create notebook: ') + e.message);
        }
    },

    setupImportModal: function() {
        const modal = document.getElementById('import-modal');
        const dropzone = document.getElementById('import-dropzone');
        const fileInput = document.getElementById('import-file-input');

        // Open modal from welcome screen
        document.getElementById('welcome-import')?.addEventListener('click', () => {
            modal?.classList.add('active');
        });

        // Click to browse
        dropzone?.querySelector('button')?.addEventListener('click', () => fileInput?.click());

        // File input change (supports multiple files)
        fileInput?.addEventListener('change', (e) => {
            const files = Array.from(e.target.files);
            for (const file of files) {
                this.importFileToNotebook(file);
            }
            e.target.value = '';
        });

        // Drag and drop (supports multiple files)
        dropzone?.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        });

        dropzone?.addEventListener('dragleave', () => {
            dropzone.classList.remove('dragover');
        });

        dropzone?.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            const files = Array.from(e.dataTransfer.files);
            if (files.length > 0) {
                for (const file of files) {
                    this.importFileToNotebook(file);
                }
            } else {
                alert(window.i18n ? window.i18n.t('notebook.dropIpynbFiles') : 'Please drop .ipynb or .nb/.wl/.wls/.m file(s)');
            }
        });
    },

    // Routes a dropped/browsed File to the correct import path: Jupyter .ipynb
    // via /api/notebooks/import, or Wolfram .nb/.wl/.wls/.m (client-parsed with
    // PhonosWolframNb) via /api/notebooks/from-cells.
    importFileToNotebook: function(file) {
        if (!file || !file.name) return;
        var lower = file.name.toLowerCase();
        if (lower.endsWith('.ipynb')) {
            this.importNotebook(file);
        } else if (lower.endsWith('.nb') || lower.endsWith('.wl') || lower.endsWith('.wls') || lower.endsWith('.m')) {
            this.importWolframFileFromBrowser(file);
        } else {
            this.showToast((NbT('unsupportedFileType', 'Unsupported file type. Use .ipynb, .nb, .wl, .wls, or .m')), 3500);
        }
    },

    importWolframFileFromBrowser: async function(file) {
        if (!this.projectId) {
            alert(window.i18n ? window.i18n.t('notebook.selectProjectFirst') : 'Please select a project first');
            return;
        }
        if (!window.PhonosWolframNb) {
            this.showToast('Wolfram renderer not loaded — refresh the page', 3000);
            return;
        }
        try {
            this.showToast((window.i18n ? window.i18n.t('notebook.parsingWolfram') : 'Parsing Wolfram notebook…'), 1500);
            var content = await file.text();

            var parsed = window.PhonosWolframNb.parseNotebook(content);
            var cells = parsed.cells || parsed;
            if (!cells || !cells.length) {
                this.showToast((window.i18n ? window.i18n.t('notebook.noWolframCells') : 'No cells found in this Wolfram file'), 3000);
                return;
            }

            var csrfToken = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
            if (!csrfToken) {
                var meta = document.querySelector('meta[name="csrf-token"]');
                if (meta) csrfToken = meta.getAttribute('content') || '';
            }

            var cleanName = file.name.replace(/\.(nb|wl|wls|m|mx|wxf|wdx)$/i, '');

            var payloadCells = [];
            for (var i = 0; i < cells.length; i++) {
                var converted = this._convertWolframCellToNotebookCell(cells[i]);
                if (!converted) continue;
                if (converted._wolframMulti) {
                    for (var j = 0; j < converted._wolframMulti.length; j++) {
                        payloadCells.push(converted._wolframMulti[j]);
                    }
                } else {
                    payloadCells.push(converted);
                }
            }

            this.showToast((window.i18n ? window.i18n.t('notebook.importingWolfram') : 'Importing Wolfram notebook…'), 0);
            var importRes = await fetch('/api/notebooks/from-cells', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                credentials: 'same-origin',
                body: JSON.stringify({
                    name: cleanName,
                    project_id: this.projectId,
                    kernel_type: 'wolfram',
                    cells: payloadCells
                })
            });
            if (!importRes.ok) {
                var errBody = null;
                try { errBody = await importRes.json(); } catch (e2) {}
                throw new Error((errBody && errBody.error) || ('Import failed (HTTP ' + importRes.status + ')'));
            }
            var importData = await importRes.json();
            this.showToast((window.i18n ? window.i18n.t('notebook.importedWolfram') : 'Wolfram notebook imported'), 2000);
            document.getElementById('import-modal')?.classList.remove('active');
            window.location.href = '/notebook?id=' + importData.id + '&project_id=' + this.projectId;
        } catch (e) {
            console.error('Failed to import Wolfram file:', e);
            this.showToast((window.i18n ? window.i18n.t('notebook.importFailed') : 'Import failed: ') + (e.message || e), 4000);
        }
    },

    exportNotebook: async function(format) {
        if (!this.notebook || !this.notebookId) {
            this.showToast((NbT('noNotebookOpen', 'No notebook is open')), 2500);
            return;
        }
        format = format || 'ipynb';
        try {
            var url = '/api/notebooks/' + this.notebookId + '/export?format=' + encodeURIComponent(format);
            var res = await fetch(url, { credentials: 'same-origin' });
            if (!res.ok) {
                var errBody = null;
                try { errBody = await res.json(); } catch (e2) {}
                throw new Error((errBody && errBody.error) || ('Export failed (HTTP ' + res.status + ')'));
            }
            var blob, defaultExt;
            if (format === 'nb') {
                blob = await res.blob();
                defaultExt = '.nb';
            } else {
                // Default: ipynb (JSON returned by endpoint)
                var json = await res.json();
                blob = new Blob([JSON.stringify(json, null, 2)], { type: 'application/x-ipynb+json' });
                defaultExt = '.ipynb';
            }
            var baseName = (this.notebook && this.notebook.name) ? this.notebook.name : 'notebook';
            var safeBase = baseName.replace(/[^a-zA-Z0-9._-]+/g, '_');
            // Prefer the filename supplied in Content-Disposition if present.
            var cd = res.headers.get('Content-Disposition') || '';
            var m = /filename="?([^";]+)"?/i.exec(cd);
            var filename = m ? m[1] : (safeBase + defaultExt);
            var link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            setTimeout(function() { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
        } catch (e) {
            console.error('Export failed:', e);
            this.showToast((NbT('exportFailed', 'Export failed: ')) + (e.message || e), 3500);
        }
    },

    importNotebook: async function(file) {
        if (!this.projectId) {
            alert(window.i18n ? window.i18n.t('notebook.selectProjectFirst') : 'Please select a project first');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);
        formData.append('project_id', this.projectId);

        try {
            const res = await fetch('/api/notebooks/import', {
                method: 'POST',
                body: formData
            });

            if (res.ok) {
                const data = await res.json();
                document.getElementById('import-modal')?.classList.remove('active');
                window.location.href = `/notebook?id=${data.id}&project_id=${this.projectId}`;
            } else {
                const err = await res.json();
                alert((window.i18n ? window.i18n.t('notebook.importFailed') : 'Import failed: ') + (err.error || (window.i18n ? window.i18n.t('notebook.unknownError') : 'Unknown error')));
            }
        } catch (e) {
            console.error(e);
            alert(window.i18n ? window.i18n.t('notebook.importFailed') : 'Import failed');
        }
    },

    quickCreate: async function(type) {
        if (!this.projectId) {
            this.showToast(window.i18n ? window.i18n.t('notebook.selectProjectFirst') : 'Please select a project first', 3000);
            document.getElementById('project-selector')?.focus();
            return;
        }

        // Map frontend type to backend template ID
        const templateMap = {
            'blank': 'blank',
            'code': 'code',
            'text': 'text',
            'analysis': 'analysis',
            'protein': 'protein',
            'wolfram': 'wolfram',
            'wolfram_scientific': 'wolfram_scientific',
            'wolfram_visualization': 'wolfram_visualization',
            'quantum': 'quantum',
            'vqe_chemistry': 'vqe_chemistry',
            'wolfram_quantum': 'wolfram_quantum'
        };
        const displayNames = {
            'blank': 'Untitled', 'code': 'Code', 'text': 'Text',
            'analysis': 'Analysis', 'protein': 'Protein',
            'wolfram': 'Wolfram', 'wolfram_scientific': 'Wolfram Scientific',
            'wolfram_visualization': 'Wolfram Visualization',
            'quantum': 'Quantum',
            'vqe_chemistry': 'VQE Chemistry',
            'wolfram_quantum': 'Wolfram + Quantum'
        };

        const templateId = templateMap[type];
        if (!templateId) {
            this.showToast((window.i18n ? window.i18n.t('notebook.unknownTemplate') : 'Unknown template type: ') + type);
            return;
        }

        try {
            const res = await fetch(`/api/notebooks/templates/${templateId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: (displayNames[type] || templateId) + ' Notebook',
                    project_id: parseInt(this.projectId)
                })
            });

            if (res.ok) {
                const data = await res.json();
                window.location.href = `/notebook?id=${data.id}&project_id=${this.projectId}`;
            } else {
                const err = await res.json();
                this.showToast((window.i18n ? window.i18n.t('notebook.failedToCreateNotebook') : 'Failed to create notebook: ') + (err.error || (window.i18n ? window.i18n.t('notebook.unknownError') : 'Unknown error')));
            }
        } catch (e) {
            console.error(e);
            this.showToast((window.i18n ? window.i18n.t('notebook.failedToCreateNotebook') : 'Failed to create notebook: ') + e.message);
        }
    },

    // TEMP: relays [EduDebug] events to the server log (see /api/notebooks/debug_log)
    // so we can diagnose the "Add Problem" flow without needing the user to
    // copy-paste browser console output. Remove once the bug is found.
    _eduDebug: function(tag, data) {
        console.log('[EduDebug]', tag, data);
        try {
            var csrfToken = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
            if (!csrfToken) {
                var meta = document.querySelector('meta[name="csrf-token"]');
                if (meta) csrfToken = meta.getAttribute('content') || '';
            }
            fetch('/api/notebooks/debug_log', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({ tag: tag, data: data }),
            }).catch(function() {});
        } catch (e) {}
    },

    setupEventListeners: function() {
        document.getElementById('add-code-btn')?.addEventListener('click', () => this.addCell('code'));
        document.getElementById('add-md-btn')?.addEventListener('click', () => this.addCell('markdown'));
        document.getElementById('add-wolfram-btn')?.addEventListener('click', () => this.addCell('wolfram'));
        document.getElementById('add-quantum-btn')?.addEventListener('click', () => this.addQuantumCell());
        this._eduDebug('setupEventListeners', { addProblemBtnFound: !!document.getElementById('add-problem-btn'), makeAssignmentBtnFound: !!document.getElementById('make-assignment-btn') });
        document.getElementById('add-problem-btn')?.addEventListener('click', () => {
            this._eduDebug('add-problem-btn clicked', {});
            try {
                var r = this.openProblemEditor(null);
                if (r && typeof r.catch === 'function') r.catch((e) => this._eduDebug('openProblemEditor rejected', { message: e && e.message, stack: e && e.stack }));
            } catch (e) {
                this._eduDebug('openProblemEditor threw synchronously', { message: e && e.message, stack: e && e.stack });
            }
        });
        document.getElementById('make-assignment-btn')?.addEventListener('click', () => {
            this._eduDebug('make-assignment-btn clicked', {});
            try {
                var r2 = this.createAssignmentFromNotebook();
                if (r2 && typeof r2.catch === 'function') r2.catch((e) => this._eduDebug('createAssignmentFromNotebook rejected', { message: e && e.message, stack: e && e.stack }));
            } catch (e) {
                this._eduDebug('createAssignmentFromNotebook threw synchronously', { message: e && e.message, stack: e && e.stack });
            }
        });

        // Problem editor rightbar close button is handled inline
        const cellTypeDropdown = document.getElementById('cell-type-dropdown');
        if (cellTypeDropdown) {
            cellTypeDropdown.addEventListener('change', () => {
                if (!this.activeCellId) return;
                const cell = this.notebook.getCell(this.activeCellId);
                if (!cell) return;
                const newType = cellTypeDropdown.value;
                if (cell.type === newType) return;
                cell.type = newType;
                this.notebook.dirty = true;
                this.render();
                this.setActiveCell(this.activeCellId);
                const reCell = this.notebook.getCell(this.activeCellId);
                if (reCell && reCell.cm) {
                    setTimeout(() => {
                        const modeMap = {
                            'code': 'python',
                            'markdown': 'markdown',
                            'c': 'text/x-csrc',
                            'cpp': 'text/x-c++src',
                            'rust': 'text/x-rustsrc',
                            'r': 'text/x-rsrc',
                            'wolfram': 'wolfram',
                            'quantum': 'python'
                        };
                        reCell.cm.setOption('mode', modeMap[reCell.type] || 'python');
                        reCell.cm.focus();
                    }, 10);
                }
            });
        }
        document.getElementById('save-btn')?.addEventListener('click', () => this.saveNotebook());
        document.getElementById('save-notebook-btn')?.addEventListener('click', () => this.saveNotebook());
        document.getElementById('kernel-btn')?.addEventListener('click', () => {
            if (this.kernel.isConnected()) {
                if (confirm(window.i18n ? window.i18n.t('notebook.disconnectKernelConfirm') : 'Disconnect kernel? You can reconnect later.')) this.disconnectKernel();
            } else {
                this.connectKernel();
            }
        });
        document.getElementById('run-btn')?.addEventListener('click', () => {
            if (this.activeCellId) this.runCell(this.activeCellId);
        });
        document.getElementById('notebook-name')?.addEventListener('change', (e) => {
            if (this.notebook) {
                this.notebook.name = e.target.value;
                this.notebook.dirty = true;
            }
        });

        // Toolbar - Stop/Interrupt
        document.getElementById('stop-btn')?.addEventListener('click', () => this.interruptKernel());

        // Toolbar - Restart
        document.getElementById('restart-btn')?.addEventListener('click', () => this.restartKernel());

        // Toolbar - Clear Output
        document.getElementById('clear-output-btn')?.addEventListener('click', () => this.clearOutput());

        // Toolbar - Clear All Outputs
        document.getElementById('clear-all-outputs-btn')?.addEventListener('click', () => this.clearAllOutputs());

        // Toolbar - Run All
        document.getElementById('run-all-btn')?.addEventListener('click', () => this.runAllCells());

        // Toolbar - Restart & Run All
        document.getElementById('restart-run-all-btn')?.addEventListener('click', () => this.restartAndRunAll());

        // Toolbar - Shut Down Kernel
        document.getElementById('shutdown-kernel-btn')?.addEventListener('click', () => this.shutdownKernel());

        // Toolbar - Change Runtime
        document.getElementById('change-runtime-btn')?.addEventListener('click', () => this.changeRuntime());

        // Toolbar - Toggle Wide Layout
        document.getElementById('toggle-wide-btn')?.addEventListener('click', () => this.toggleWideLayout());

        // Toolbar - Find
        document.getElementById('find-btn')?.addEventListener('click', () => this.toggleFindBar());

        // Find bar events
        document.getElementById('find-input')?.addEventListener('input', (e) => this.doFind(e.target.value));
        document.getElementById('find-next')?.addEventListener('click', () => this.findNext());
        document.getElementById('find-prev')?.addEventListener('click', () => this.findPrev());
        document.getElementById('find-close')?.addEventListener('click', () => this.closeFindBar());
        document.getElementById('find-input')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); e.shiftKey ? this.findPrev() : this.findNext(); }
            if (e.key === 'Escape') { e.preventDefault(); this.closeFindBar(); }
        });

        // Kernel Status Widget Click
        document.getElementById('kernel-status-widget')?.addEventListener('click', () => {
            if (this.kernel.isConnected()) {
                if (confirm(window.i18n ? window.i18n.t('notebook.disconnectKernelConfirm') : 'Disconnect kernel? You can reconnect later.')) {
                    this.disconnectKernel();
                }
            } else {
                this.connectKernel();
            }
        });

        // Kernel Action Button
        document.getElementById('kernel-action-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            if (this.kernel.isConnected()) {
                this.disconnectKernel();
            } else {
                this.connectKernel();
            }
        });

        // Welcome screen - Create New Notebook
        document.getElementById('welcome-new-notebook')?.addEventListener('click', () => {
            this.populateNewNotebookProjects();
            document.getElementById('new-notebook-modal')?.classList.add('active');
            const nameInput = document.getElementById('new-notebook-name');
            setTimeout(() => { nameInput?.focus(); nameInput?.select(); }, 50);
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            // Ctrl+Shift+P command palette (works everywhere)
            if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'p' || e.key === 'P')) {
                e.preventDefault();
                this.openCommandPalette();
                return;
            }
            // Ctrl+Shift+S commit dialog
            if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 's' || e.key === 'S')) {
                e.preventDefault();
                if (window.HistoryManager && this.notebook && this.notebook.id) {
                    HistoryManager.notebookId = this.notebook.id;
                    HistoryManager._showCommitDialog();
                }
                return;
            }
            // Ctrl+F find bar works everywhere (even in CodeMirror)
            if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
                e.preventDefault();
                this.toggleFindBar();
                return;
            }
            // Esc in the file filter clears it and restores the full tree —
            // otherwise the only way back is to select the text and delete it.
            if (e.key === 'Escape' && e.target.id === 'file-search-input') {
                e.preventDefault();
                e.target.value = '';
                this._filterFileTree('');
                e.target.blur();
                return;
            }

            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if (e.target.isContentEditable) return;

            // Ctrl+Shift+E: jump to the Explorer and focus its filter. The file
            // tree is the panel people search most, and hunting for the box with
            // the mouse every time is the friction this removes.
            if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'e' || e.key === 'E')) {
                e.preventDefault();
                this._switchPanel('files');
                setTimeout(() => {
                    const search = document.getElementById('file-search-input');
                    if (search) { search.focus(); search.select(); }
                }, 60);
                return;
            }

            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
                e.preventDefault();
                this.saveNotebook();
            }
            if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
                e.preventDefault();
                this.addCell('code', '', this.activeCellId);
            }
            if ((e.ctrlKey || e.metaKey) && e.key === 'm') {
                e.preventDefault();
                this.addCell('markdown', '', this.activeCellId);
            }
            if ((e.ctrlKey || e.metaKey) && e.key === 'd') {
                e.preventDefault();
                if (this.activeCellId) this.duplicateCell(this.activeCellId);
            }
            if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'd') {
                e.preventDefault();
                if (this.activeCellId) this.deleteCell(this.activeCellId);
            }
            if (e.altKey && e.key === 'ArrowUp') {
                e.preventDefault();
                if (this.activeCellId) this.moveCell(this.activeCellId, -1);
            }
            if (e.altKey && e.key === 'ArrowDown') {
                e.preventDefault();
                if (this.activeCellId) this.moveCell(this.activeCellId, 1);
            }
            if (e.altKey && e.key === 'c') {
                e.preventDefault();
                if (this.activeCellId) this.toggleCellCollapse(this.activeCellId);
            }
            if (e.key === 'Escape') {
                this.closeFindBar();
                const contextMenu = document.getElementById('cell-context-menu');
                if (contextMenu) contextMenu.classList.remove('active');
            }
        });
    },

    // ============================================================================
    // TAB SYSTEM
    // ============================================================================

    _registerPrimaryTab: function() {
        if (!this.notebook) return;
        var tabId = 'nb-' + (this.notebook.id || 'new');
        var existing = this._tabs.find(function(t) { return t.id === tabId; });
        if (existing) {
            this._activeTabId = tabId;
            return;
        }
        this._tabs.push({
            id: tabId,
            type: 'notebook',
            title: this.notebook.name,
            notebook: this.notebook,
            cm: null,
            filePath: null,
            fileId: null,
            dirty: false,
            containerId: 'cells-container'
        });
        this._activeTabId = tabId;
        if (this.notebook.id) {
            this.initTabBar();
        }
    },

    _getActiveCellContainer: function() {
        var tab = null;
        for (var i = 0; i < this._tabs.length; i++) {
            if (this._tabs[i].id === this._activeTabId) {
                tab = this._tabs[i];
                break;
            }
        }
        if (tab && tab.type === 'notebook' && tab.containerId) {
            if (tab.containerId === 'cells-container') {
                return document.getElementById('cells-container');
            }
            return document.getElementById(tab.containerId);
        }
        return document.getElementById('cells-container');
    },

    initTabBar: function() {
        this._renderTabBar();
    },

    _renderTabBar: function() {
        var container = document.getElementById('nb-tabs-container');
        if (!container) return;
        container.innerHTML = '';

        for (var i = 0; i < this._tabs.length; i++) {
            var tab = this._tabs[i];
            var btn = document.createElement('button');
            btn.className = 'nb-tab-btn' + (tab.id === this._activeTabId ? ' active' : '');
            btn.dataset.tabId = tab.id;

            var iconSvg = tab.type === 'notebook'
                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="nb-tab-icon"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>'
                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="nb-tab-icon"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';

            btn.innerHTML = iconSvg;
            var label = document.createElement('span');
            label.className = 'nb-tab-label';
            label.textContent = tab.title;
            btn.appendChild(label);

            if (tab.dirty || (tab.notebook && tab.notebook.dirty)) {
                var dot = document.createElement('span');
                dot.className = 'nb-tab-dirty';
                dot.textContent = '\u25CF';
                btn.appendChild(dot);
            }

            var closeBtn = document.createElement('span');
            closeBtn.className = 'nb-tab-close';
            closeBtn.textContent = '\u00D7';
            closeBtn.onclick = (function(tabId) {
                return function(e) {
                    e.stopPropagation();
                    this.closeTab(tabId);
                }.bind(this);
            }).call(this, tab.id);
            btn.appendChild(closeBtn);

            btn.onclick = (function(tabId) {
                return function() {
                    this.switchTab(tabId);
                }.bind(this);
            }).call(this, tab.id);
            container.appendChild(btn);
        }

        this._updateTabBarVisibility();
    },

    _updateTabBarVisibility: function() {
        var bar = document.getElementById('nb-tab-bar');
        if (bar) {
            bar.style.display = this._tabs.length > 0 ? '' : 'none';
        }
    },

    switchTab: function(tabId) {
        var tab = null;
        for (var i = 0; i < this._tabs.length; i++) {
            if (this._tabs[i].id === tabId) {
                tab = this._tabs[i];
                break;
            }
        }
        if (!tab) return;

        // Hide all tab contents
        for (var i = 0; i < this._tabs.length; i++) {
            var t = this._tabs[i];
            if (t.containerId === 'cells-container') {
                var el = document.getElementById('cells-container');
                if (el) el.style.display = 'none';
            } else {
                var el = document.getElementById(t.containerId);
                if (el) el.style.display = 'none';
            }
        }
        document.querySelectorAll('.nb-tab-content').forEach(function(el) {
            el.style.display = 'none';
        });

        // Show the active tab
        var cellsContainerEl = document.getElementById('cells-container');
        if (tab.containerId === 'cells-container') {
            if (cellsContainerEl) cellsContainerEl.style.display = '';
        } else {
            var el = document.getElementById(tab.containerId);
            if (el) el.style.display = 'block';
            // The permanent cells-container is NOT in _tabs when the welcome
            // screen is up (no primary notebook registered yet). Without an
            // explicit hide here it stays visible inside the flex-row
            // #cells-wrapper alongside the just-shown .nb-tab-content,
            // producing the "welcome on the left half, notebook on the right
            // half" split after opening a notebook from the sidebar / reload.
            if (cellsContainerEl) cellsContainerEl.style.display = 'none';
        }

        // No tab — notebook or text file — should ever expose the welcome
        // screen. Previously this only ran for notebook tabs, leaving the
        // welcome visible (left half) when a text-file tab was active.
        var welcomeState = document.getElementById('welcome-state');
        if (welcomeState) welcomeState.style.display = 'none';

        this._activeTabId = tabId;

        // Update URL so page refresh loads the correct notebook
        if (tab.type === 'notebook' && tab.notebook && tab.notebook.id) {
            try { window.history.replaceState({}, '', '/notebook?id=' + tab.notebook.id); } catch (e) {}
        }

        // Update notebook reference and header for notebook tabs
        if (tab.type === 'notebook') {
            this.notebook = tab.notebook;
            this.notebookId = tab.notebook ? tab.notebook.id : null;
            var nameInput = document.getElementById('notebook-name');
            if (nameInput) {
                nameInput.value = tab.notebook ? tab.notebook.name : tab.title;
            }
            // Re-sync education state (toolbar, task cache, assignment) to the
            // tab we just switched to. Without this, the education globals
            // stay pinned to whichever notebook tab initialised them last, so
            // e.g. switching from an assignment notebook to a plain one would
            // keep showing the Problem button, and switching between two
            // different assignment notebooks could submit answers against the
            // wrong assignment id.
            if (this.notebook && this.notebook.project_id) {
                this._initEducationMode();
            } else if (typeof this._updateEducationToolbar === 'function') {
                this._educationInfo = null;
                this._educationAssignment = null;
                this._taskCache = null;
                this._assignmentOpened = false;
                this._updateEducationToolbar();
            }
            this.updateStatusBar();
            this.updateAllCellActions();
            // Refresh CodeMirror instances after display toggle
            if (tab.notebook) {
                setTimeout(function() {
                    tab.notebook.cells.forEach(function(c) {
                        if (c.cm) c.cm.refresh();
                    });
                }, 100);
            }
        } else if (tab.type === 'text') {
            // Hide notebook-specific UI when editing text files
            var nameInput = document.getElementById('notebook-name');
            if (nameInput) nameInput.value = tab.title;
            // No notebook is active on a text tab, so any assignment/problem
            // toolbar state must be cleared rather than left over from the
            // previously active notebook tab.
            this._educationInfo = null;
            this._educationAssignment = null;
            this._taskCache = null;
            this._assignmentOpened = false;
            if (typeof this._updateEducationToolbar === 'function') this._updateEducationToolbar();
        }

        // Always refresh the kernel indicator so it reflects the current state
        // (fixes "kernel online in DB but button shows No Kernel" when returning
        // to a notebook tab from a text-file tab or after a fresh open).
        if (this.kernel && typeof this.kernel.updateUI === 'function') {
            var status = (this.kernel.connectionStatus === 'connected' || this.kernel.isConnected())
                ? 'idle'
                : (this.kernel.connectionStatus || 'disconnected');
            this.kernel.updateUI(status);
        }

        // Refresh CodeMirror for text editor tabs
        if (tab.cm) {
            setTimeout(function() { if (tab.cm) tab.cm.refresh(); }, 50);
        }

        this._renderTabBar();
    },

    closeTab: function(tabId) {
        if (this._tabs.length <= 1) return;

        var tab = null;
        var idx = -1;
        for (var i = 0; i < this._tabs.length; i++) {
            if (this._tabs[i].id === tabId) {
                tab = this._tabs[i];
                idx = i;
                break;
            }
        }
        if (!tab) return;

        // Check dirty state
        if (tab.dirty || (tab.notebook && tab.notebook.dirty)) {
            var msg = 'Save changes to ' + tab.title + ' before closing?';
            if (!confirm(msg)) return;
            if (tab.type === 'text' && tab.cm) {
                this.saveTextTab(tab);
            } else if (tab.type === 'notebook' && tab.notebook) {
                this.saveNotebook();
            }
        }

        // Stop kernel if this is the last notebook tab (kernel is singleton per page)
        if (tab.type === 'notebook' && this.kernel && this.kernel.sessionId) {
            var notebookTabs = this._tabs.filter(function(t) { return t.type === 'notebook'; });
            if (notebookTabs.length <= 1) {
                fetch('/api/notebooks/kernel/' + this.kernel.sessionId + '/stop', { method: 'POST' })
                    .catch(function(e) { console.warn('Failed to stop kernel:', e); });
                this.kernel.sessionId = null;
                this.kernel.connectionStatus = 'disconnected';
            }
        }

        // Destroy CodeMirror for text editors
        if (tab.cm) {
            tab.cm.toTextArea();
            tab.cm = null;
        }

        // Remove the content element (but not cells-container which is permanent)
        if (tab.containerId !== 'cells-container') {
            var el = document.getElementById(tab.containerId);
            if (el) el.remove();
        }

        this._tabs.splice(idx, 1);

        // Switch to adjacent tab
        var newIdx = Math.min(idx, this._tabs.length - 1);
        var newTab = this._tabs[newIdx];
        if (newTab) {
            this.switchTab(newTab.id);
        }

        this._renderTabBar();
    },

    openNotebookAsTab: async function(notebookId, notebookName) {
        var tabId = 'nbt-' + notebookId;

        // Check if already open
        for (var i = 0; i < this._tabs.length; i++) {
            if (this._tabs[i].id === tabId) {
                this.switchTab(tabId);
                return;
            }
        }

        try {
            var res = await fetch('/api/notebooks/' + notebookId);
            if (!res.ok) throw new Error('Failed to load notebook');
            var data = await res.json();
            if (!data.notebook) throw new Error('Notebook data missing');

            var nb = new Notebook(data.notebook);

            var wrapper = document.getElementById('cells-wrapper');
            if (!wrapper) return;

            var contentEl = document.createElement('div');
            contentEl.className = 'nb-tab-content';
            contentEl.id = 'tab-content-' + tabId;
            // Make visible BEFORE rendering so CodeMirror initialises with the
            // correct container width (otherwise display:none gives clientWidth=0
            // and viewportMargin:Infinity bakes in a 0-width layout that
            // subsequent refresh() cannot fully recover from).
            contentEl.style.display = 'block';

            // Immediately retire the welcome screen / permanent cells-container
            // so they cannot share the flex-row (#cells-wrapper) with this new
            // .nb-tab-content and produce a half/half split layout. switchTab()
            // re-asserts this, but hiding now avoids the flash during render.
            var wsEl = document.getElementById('welcome-state');
            if (wsEl) wsEl.style.display = 'none';
            var ccEl = document.getElementById('cells-container');
            if (ccEl) ccEl.style.display = 'none';

            var scrollPadding = wrapper.querySelector('.nb-scroll-padding');
            if (scrollPadding) {
                wrapper.insertBefore(contentEl, scrollPadding);
            } else {
                wrapper.appendChild(contentEl);
            }

            var self = this;
            var tab = {
                id: tabId,
                type: 'notebook',
                title: notebookName || nb.name,
                notebook: nb,
                cm: null,
                filePath: null,
                fileId: null,
                dirty: false,
                containerId: contentEl.id
            };

            // Render cells into the new container (CodeMirror sees real width now)
            this._renderNotebookInContainer(nb, contentEl);

            this._tabs.push(tab);
            this.switchTab(tabId);
            this._renderTabBar();

            // Set active notebook reference so kernel is bound to this tab
            this.notebook = nb;
            this.notebookId = nb.id;
            var nameInput = document.getElementById('notebook-name');
            if (nameInput) nameInput.value = nb.name;

            // Re-initialise education mode for the newly-opened tab so its
            // toolbar/badges/task-cache reflect THIS notebook's assignment (if
            // any) instead of leaking whichever notebook was active before —
            // otherwise submitting an answer here could hit the wrong assignment.
            if (this.notebook && this.notebook.project_id) {
                this._initEducationMode();
            }

            // Clear stale "No Kernel" indicator before deciding connect vs idle
            if (this.kernel && typeof this.kernel.updateUI === 'function') {
                this.kernel.updateUI('disconnected');
            }

            if (data.active_session) {
                console.log('Found active session for opened tab, auto-connecting...', data.active_session);
                this.kernel.connect(notebookId).then(function(ok) {
                    if (ok) {
                        self.showToast(window.i18n ? window.i18n.t('notebook.resumedSession') : 'Resumed active session', 2000);
                    }
                }).catch(function(err) {
                    console.warn('Auto-connect failed for tab:', err);
                });
            } else {
                this.showToast(window.i18n ? window.i18n.t('notebook.notebookLoaded') : 'Notebook loaded. Click the kernel button to start runtime.', 3000);
            }
        } catch (e) {
            console.error('Failed to load notebook tab:', e);
            this.showToast('Failed to load notebook: ' + e.message, 3000);
        }
    },

    _renderNotebookInContainer: function(notebook, container) {
        container.innerHTML = '';

        if (!notebook || !notebook.cells || notebook.cells.length === 0) {
            container.innerHTML = '<div class="cell-empty-state" style="padding: 32px; text-align: center; color: #71717a;"><p>' + (window.i18n ? window.i18n.t('notebook.startNotebook') : 'Start your notebook by adding a cell') + '</p></div>';
            return;
        }

        // Temporarily set this.notebook so createCellElement/initCodeMirror
        // reference the correct notebook's cells array
        var savedNotebook = this.notebook;
        this.notebook = notebook;

        var self = this;
        try {
            notebook.cells.forEach(function(cell) {
                try {
                    var el = self.createCellElement(cell);
                    container.appendChild(el);
                    self.initCodeMirror(cell, el);
                } catch (cellErr) {
                    console.error('Failed to render cell', cell && cell.id, cellErr);
                }
            });
        } finally {
            this.notebook = savedNotebook;
        }

        setTimeout(function() {
            notebook.cells.forEach(function(cell) {
                if (cell.cm) cell.cm.refresh();
            });
        }, 100);
    },

    openFileAsText: async function(file, contentOverride) {
        var fileId = file.id;
        var tabId = 'file-' + fileId;

        // Check if already open
        for (var i = 0; i < this._tabs.length; i++) {
            if (this._tabs[i].id === tabId) {
                this.switchTab(tabId);
                return;
            }
        }

        try {
            var content;
            if (typeof contentOverride === 'string') {
                // Workspace files arrive with their content pre-fetched (they
                // have no /api/files row to fetch from).
                content = contentOverride;
            } else {
                var res = await fetch('/api/files/' + fileId + '/content');
                if (!res.ok) throw new Error('Failed to read file');
                var data = await res.json();
                content = data.content || '';
            }

            var fname = this._resolveFileName(file);

            var wrapper = document.getElementById('cells-wrapper');
            if (!wrapper) return;

            var contentEl = document.createElement('div');
            contentEl.className = 'nb-tab-content nb-tab-text';
            contentEl.id = 'tab-content-' + tabId;
            contentEl.style.display = 'none';

            var scrollPadding = wrapper.querySelector('.nb-scroll-padding');
            if (scrollPadding) {
                wrapper.insertBefore(contentEl, scrollPadding);
            } else {
                wrapper.appendChild(contentEl);
            }

            var self = this;
            var tab = {
                id: tabId,
                type: 'text',
                title: fname,
                notebook: null,
                cm: null,
                filePath: file.container_path || file.path,
                // Workspace files (ws:*) have no /api/files row — leave
                // fileId unset so saveTextTab's guard makes the tab read-only.
                fileId: String(fileId).indexOf('ws:') === 0 ? null : fileId,
                dirty: false,
                containerId: contentEl.id
            };

            this._tabs.push(tab);

            // Initialize CodeMirror in the container
            setTimeout(function() {
                var cm = CodeMirror(contentEl, {
                    value: content,
                    mode: self._getCodeMirrorMode(fname),
                    theme: 'dracula',
                    lineNumbers: true,
                    lineWrapping: true,
                    indentUnit: 4,
                    tabSize: 4,
                    matchBrackets: true,
                    autoCloseBrackets: true,
                    styleActiveLine: true,
                    scrollbarStyle: 'simple',
                    extraKeys: {
                        'Ctrl-S': function() { self.saveTextTab(tab); },
                        'Cmd-S': function() { self.saveTextTab(tab); }
                    }
                });
                tab.cm = cm;

                cm.on('change', function() {
                    if (!tab.dirty) {
                        tab.dirty = true;
                        self._renderTabBar();
                    }
                });

                cm.refresh();
            }, 50);

            this.switchTab(tabId);
            this._renderTabBar();
        } catch (e) {
            console.error('Failed to open file as text:', e);
            this.showToast('Failed to open file: ' + e.message, 3000);
        }
    },

    /**
     * Read a Wolfram ``.nb`` / ``.wl`` / ``.wls`` / ``.m`` file from the
     * project file tree, parse it client-side with PhonosWolframNb, POST the
     * resulting cells to /api/notebooks/from-cells, and open the newly
     * created notebook in a tab in the main editor.
     *
     * User sees a "Parsing… → Importing…" toast and ends up on a tab with the
     * cells laid out as a real notebook (instead of a read-only split preview).
     */
    /**
     * Convert a single cell produced by PhonosWolframNb.parseNotebook into a
     * cell shape suitable for the notebook persistence API.
     *
     * The mapping mirrors what ``.ipynb`` import does and tries to keep the
     * resulting notebook readable:
     *
     *   Input / Code        -> ``code`` cell with the Wolfram source verbatim
     *                          (string escapes already decoded by the parser)
     *   Title               -> markdown cell with ``# <text>``
     *   Section             -> markdown cell with ``## <text>``
     *   Subsection          -> markdown cell with ``### <text>``
     *   Subsubsection       -> markdown cell with ``#### <text>``
     *   Text / Subtitle /   -> markdown cell with the text content
     *   Chapter / etc.
     *   Output              -> DROPPED. Output cells store the raw Wolfram
     *                          expression as a placeholder ("-Graphics-",
     *                          "{1, 2, 3}", etc.) and are confusing when
     *                          rendered as a markdown cell. They'll be
     *                          regenerated automatically when the user runs
     *                          the corresponding Input cell.
     *
     * Section dividers (``(* ::Section:: *)``, ``(* ::Subsection:: *)``)
     * inside an Input cell are extracted to their own markdown heading cell.
     */
    _convertWolframCellToNotebookCell: function (cell) {
        if (!cell || !cell.source) return null;
        var style = (cell.style || 'Input').trim();
        var source = (cell.source || '').replace(/^\s+|\s+$/g, '');
        // If the source is empty after trimming, drop the cell entirely
        // (avoids creating a useless empty code cell).
        if (!source) return null;

        // Drop Output cells — they'll be regenerated when the user runs.
        if (style === 'Output') return null;

        // Skip interactive content cells (Manipulate, Dynamic, Animate, etc.)
        // that require the Wolfram frontend to render properly. These cannot
        // be meaningfully executed in a headless/server-side environment.
        if (style === 'Input' || style === 'Code') {
            var interactiveRe = /^(Manipulate|Animate|DynamicModule|Dynamic)\s*\[/;
            var stripped = source.replace(/\(\*.*?\*\)/g, '').trim();
            if (interactiveRe.test(stripped)) return null;
        }

        // Section dividers written as comments inside an Input cell:
        //   (* ::Section:: *)       -> ## Section heading
        //   (* ::Subsection:: *)    -> ### Subsection heading
        // Extract each as its own heading cell; keep the rest of the code
        // as a normal code cell.
        if (style === 'Input' || style === 'Code' || style === '') {
            var extracted = this._extractWolframSectionDividers(source);
            if (extracted.dividers.length) {
                // Push the code (with dividers stripped) first if non-empty.
                var out = [];
                if (extracted.code.trim()) {
                    out.push({ type: 'wolfram', source: extracted.code });
                }
                for (var i = 0; i < extracted.dividers.length; i++) {
                    out.push(extracted.dividers[i]);
                }
                // The mapping function only returns one cell at a time, but
                // when dividers are present we emit a header list. The caller
                // uses .filter(Boolean) and .map() per-cell, so for the
                // divider case we return the first cell and rely on the
                // caller to flatten — instead we return a wrapper object that
                // the caller recognises via a sentinel property.
                return { _wolframMulti: out };
            }
            return { type: 'wolfram', source: source };
        }

        // Heading-style cells.
        var headingLevel = {
            'Title': 1, 'Chapter': 1,
            'Section': 2,
            'Subsection': 3,
            'Subsubsection': 4
        }[style];
        if (headingLevel) {
            return { type: 'markdown', source: this._mdHeading(headingLevel, source) };
        }

        // Everything else (Text, Subtitle, etc.) is a plain markdown cell.
        return { type: 'markdown', source: source };
    },

    /**
     * Strip Mathematica ``(* ::Section:: *)`` / ``(* ::Subsection:: *)``
     * divider comments from an Input cell's source. Returns the remaining
     * code plus a list of heading cells to insert in place of each divider.
     */
    _extractWolframSectionDividers: function (source) {
        var dividers = [];
        var module = this;
        // Match both forms:
        //   (* ::Section:: *)                — divider alone, no title
        //   (* ::Section:: ::My Title *)      — divider with inline title
        //   (* ::Section:: :: *)              — divider with empty title
        // The trailing `::` separator (when present) is consumed by the regex
        // so the captured title doesn't include a leading colon.
        var levelRe = /\s*\(\*\s*::\s*(Subsubsection|Subsection|Section|Chapter|Title)\s*::\s*(?:[\s:]*([^\)\*]*?))?\s*\*\)\s*/g;
        var cleaned = source.replace(levelRe, function (_m, level, title) {
            var lvl = { Title: 1, Chapter: 1, Section: 2, Subsection: 3, Subsubsection: 4 }[level] || 2;
            var headingText = (title || '').trim() || level;
            dividers.push({ type: 'markdown', source: module._mdHeading(lvl, headingText) });
            return '\n';
        });
        cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim();
        return { code: cleaned, dividers: dividers };
    },

    _mdHeading: function (level, text) {
        var hashes = '';
        for (var i = 0; i < level; i++) hashes += '#';
        return hashes + ' ' + text;
    },

    importWolframFileAsNotebook: async function(file, fname, ext) {
        if (!this.projectId) {
            this.showToast(window.i18n ? window.i18n.t('notebook.selectProjectFirst') : 'Please select a project first', 3000);
            return;
        }
        if (!window.PhonosWolframNb) {
            this.showToast('Wolfram renderer not loaded — refresh the page', 3000);
            return;
        }
        var fileId = file.id;
        var url = '/api/files/' + fileId + '/content';
        try {
            this.showToast((window.i18n ? window.i18n.t('notebook.parsingWolfram') : 'Parsing Wolfram notebook…'), 1500);
            var res = await fetch(url);
            if (!res.ok) throw new Error('Failed to read file (HTTP ' + res.status + ')');
            var data = await res.json();
            var content = data.content || '';

            var parsed = window.PhonosWolframNb.parseNotebook(content);
            var cells = parsed.cells || parsed;
            if (!cells.length) {
                this.showToast((window.i18n ? window.i18n.t('notebook.noWolframCells') : 'No cells found in this Wolfram file'), 3000);
                return;
            }

            var csrfToken = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
            if (!csrfToken) {
                var meta = document.querySelector('meta[name="csrf-token"]');
                if (meta) csrfToken = meta.getAttribute('content') || '';
            }

            var cleanName = (fname || 'Imported notebook').replace(/\.(nb|wl|wls|m|mx|wxf|wdx)$/i, '');
            this.showToast((window.i18n ? window.i18n.t('notebook.importingWolfram') : 'Importing Wolfram notebook…'), 0);

            // Convert each parsed Wolfram cell into one or more notebook-API
            // cells. A single Input cell may contain section dividers like
            //   (* ::Section:: *) My heading
            // which we extract into separate markdown heading cells so the
            // imported notebook has proper structure.
            var payloadCells = [];
            for (var i = 0; i < cells.length; i++) {
                var converted = this._convertWolframCellToNotebookCell(cells[i]);
                if (!converted) continue;
                if (converted._wolframMulti) {
                    for (var j = 0; j < converted._wolframMulti.length; j++) {
                        payloadCells.push(converted._wolframMulti[j]);
                    }
                } else {
                    payloadCells.push(converted);
                }
            }

            // Pass the source file path so the notebook is linked to the
            // original .nb file on disk for bidirectional mirroring.
            var sourceFilePath = file.path || file.container_path || null;

            var importRes = await fetch('/api/notebooks/from-cells', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                credentials: 'same-origin',
                body: JSON.stringify({
                    name: cleanName,
                    project_id: this.projectId,
                    kernel_type: 'wolfram',
                    cells: payloadCells,
                    source_file_path: sourceFilePath
                })
            });
            if (!importRes.ok) {
                var errBody = null;
                try { errBody = await importRes.json(); } catch (e) {}
                var msg = (errBody && errBody.error) || ('Import failed (HTTP ' + importRes.status + ')');
                // If the backend doesn't yet expose the new endpoint (e.g.
                // gunicorn hasn't been restarted since deploy), the right thing
                // to do depends on whether a notebook is already open.
                if (importRes.status === 404) {
                    if (this.notebook) {
                        // Already editing a notebook: fall back to the read-only
                        // split-panel preview so the user can still inspect cells
                        // and use Run All without disrupting their current work.
                        this.showToast((window.i18n ? window.i18n.t('notebook.wolframPreviewOnly') : 'Import endpoint unavailable — showing preview only. Restart gunicorn to enable import.'), 4500);
                        var previewFile = {
                            id: fileId,
                            type: file.type,
                            filename: fname,
                            size: file.size,
                            is_dir: false
                        };
                        this.openFileInSplit(previewFile);
                        return;
                    }
                    // On the welcome screen (no notebook open): we cannot create
                    // a Phonos notebook without the new endpoint, and showing a
                    // preview split alongside the welcome screen would be
                    // confusing. Just fail clearly and tell the user what to do.
                    this.showToast((window.i18n ? window.i18n.t('notebook.wolframImportUnavailable') : 'Wolfram import is unavailable. Restart gunicorn to enable .nb file imports.'), 6000);
                    return;
                }
                throw new Error(msg);
            }
            var importData = await importRes.json();
            this.showToast((window.i18n ? window.i18n.t('notebook.importedWolfram') : 'Wolfram notebook imported'), 2000);

            // Open the new notebook in a tab in the main editor.
            if (typeof this.openNotebookAsTab === 'function') {
                await this.openNotebookAsTab(importData.id, importData.name || cleanName);
            } else {
                // Fallback: navigate to the notebook page
                window.location.href = '/notebook?id=' + importData.id + '&project_id=' + this.projectId;
            }
            // Refresh the file-tree panel so the new notebook is visible alongside the file.
            this.refreshFileExplorer(true);
        } catch (e) {
            console.error('Failed to import Wolfram file:', e);
            this.showToast((window.i18n ? window.i18n.t('notebook.importFailed') : 'Import failed: ') + (e.message || e), 4000);
        }
    },

    // -------------------------------------------------------------------------
    // .nb files are imported directly as Phonos notebooks (no Wolfram Cloud modal)
    // -------------------------------------------------------------------------

    openFileInSplit: async function(file) {
        var panel = document.getElementById('split-panel');
        var contentEl = document.getElementById('split-content');
        var titleEl = document.getElementById('split-file-title');
        var saveBtn = document.getElementById('split-save-btn');
        if (!panel || !contentEl) return;

        // Clear any previously shown model-detail panel
        if (this._splitModelObj) this._splitModelObj = null;
        if (contentEl) contentEl.classList.remove('nb-split-model');

        var fileId = file.id;
        var fname = this._resolveFileName(file);
        var ext = (fname || '').split('.').pop().toLowerCase();
        var isWolframNotebook = ['nb', 'wl', 'wls', 'm', 'mx', 'wxf', 'wdx'].indexOf(ext) !== -1;

        this._splitFileId = fileId;

        try {
            var url;
            if (file.type === 'output' || file.type === 'artifact') {
                if (file.artifact_filename) {
                    url = '/api/jobs/' + file.job_id + '/artifacts/' + encodeURIComponent(file.artifact_filename) + '/content';
                } else {
                    url = '/api/jobs/' + file.job_id + '/output_content';
                }
            } else if (isWolframNotebook) {
                // Wolfram notebooks are mostly plain text; load the whole file
                // up to the server's 50 MB guard. Truncating at 10k lines
                // splits multi-line Cell[] entries across cells in the UI.
                url = '/api/files/' + fileId + '/content';
            } else {
                url = '/api/files/' + fileId + '/content?max_lines=10000';
            }
            var res = await fetch(url);
            if (!res.ok) throw new Error('Failed to read file');
            var data = await res.json();
            var content = data.content || '';

            titleEl.textContent = fname;
            contentEl.innerHTML = '';

            // Tear down any previous CodeMirror / Wolfram render so the panel
            // doesn't leak state from one file open to the next.
            if (this._splitCM) {
                try { this._splitCM.toTextArea(); } catch (e) {}
                this._splitCM = null;
            }
            this._splitWolfram = null;
            this._quantumBuilderCellId = null;
            this._quantumBuilder = null;
            contentEl.classList.remove('nb-split-wolfram');
            contentEl.classList.remove('nb-split-quantum');
            contentEl.style.padding = '';

            if (isWolframNotebook && window.PhonosWolframNb) {
                contentEl.classList.add('nb-split-wolfram');
                contentEl.style.padding = '0';
                var self = this;
                var nbInfo = document.createElement('div');
                nbInfo.className = 'nb-split-wolfram-meta';
                if (data.truncated) {
                    var notice = document.createElement('div');
                    notice.className = 'nb-split-notice';
                    notice.textContent = 'File truncated to first 10,000 lines. Run results after that line will be unavailable until you edit the source.';
                    contentEl.appendChild(notice);
                }
                contentEl.appendChild(nbInfo);
                try {
                    var cells = window.PhonosWolframNb.parseNotebook(content);
                    window.PhonosWolframNb.openInSidebar(contentEl, content, ext).then(function () {
                        self._splitWolfram = { cells: cells };
                        // Persist the cells + source so a future save can serialize
                        // any user-typed edits back to the original .nb format.
                        self._splitWolframContent = content;
                    });
                } catch (e) {
                    console.error('Failed to render Wolfram notebook:', e);
                    this.showToast('Failed to render Wolfram notebook: ' + e.message, 3000);
                    // Fall back to the raw-text CodeMirror view
                    isWolframNotebook = false;
                    contentEl.classList.remove('nb-split-wolfram');
                    contentEl.style.padding = '';
                }
            }

            if (!isWolframNotebook) {
                if (data.truncated) {
                    var rawNotice = document.createElement('div');
                    rawNotice.className = 'nb-split-notice';
                    rawNotice.textContent = 'File truncated to first 10,000 lines. Download to view full file.';
                    contentEl.appendChild(rawNotice);
                }

                var self = this;
                setTimeout(function() {
                    var cm = CodeMirror(contentEl, {
                        value: content,
                        mode: self._getCodeMirrorMode(fname),
                        theme: 'dracula',
                        lineNumbers: true,
                        lineWrapping: true,
                        indentUnit: 4,
                        tabSize: 4,
                        matchBrackets: true,
                        autoCloseBrackets: true,
                        styleActiveLine: true,
                        scrollbarStyle: 'simple',
                        readOnly: false,
                        extraKeys: {
                            'Ctrl-S': function() { self._saveSplitFile(); },
                            'Cmd-S': function() { self._saveSplitFile(); }
                        }
                    });
                    self._splitCM = cm;
                    cm.on('change', function() {
                        saveBtn.style.display = '';
                    });
                    cm.refresh();
                }, 50);
            }

            // Wolfram renders are read-only for now (executing per cell uses
            // its own buttons; raw source edits aren't propagated back).
            saveBtn.style.display = isWolframNotebook ? 'none' : '';
            panel.style.display = '';
            // Reset any inline width set by a previous resize drag so the
            // CSS default width kicks in. Otherwise the panel keeps whatever
            // pixel width the user last dragged it to.
            panel.style.width = '';
            panel.style.minWidth = '';
            panel.style.maxWidth = '';
            this._updateSplitLayout();
        } catch (e) {
            console.error('Failed to open file in split:', e);
            this.showToast('Failed to open file: ' + e.message, 3000);
        }
    },

    _saveSplitFile: async function() {
        if (!this._splitCM || !this._splitFileId) return;
        var csrfToken = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
        if (!csrfToken) {
            var meta = document.querySelector('meta[name="csrf-token"]');
            if (meta) csrfToken = meta.getAttribute('content') || '';
        }
        try {
            var res = await fetch('/api/files/' + this._splitFileId + '/content', {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ content: this._splitCM.getValue() })
            });
            if (res.ok) {
                document.getElementById('split-save-btn').style.display = 'none';
                this.showToast('File saved', 1500);
            } else {
                this.showToast('Failed to save file', 3000);
            }
        } catch (e) {
            this.showToast('Failed to save file: ' + e.message, 3000);
        }
    },

    // -------------------------------------------------------------------------
    // Quantum circuit builder (split panel mode)
    // -------------------------------------------------------------------------

    addQuantumCell: function() {
        if (!window.QuantumCircuitBuilder) {
            this.showToast('Circuit builder failed to load', 3000);
            return;
        }
        var model = QuantumCircuitBuilder.defaultModel();
        var cell = this.addCell('quantum', QuantumCircuitBuilder.generateCode(model), this.activeCellId);
        if (!cell) return;
        if (!cell.metadata) cell.metadata = {};
        cell.metadata.quantum = model;
        cell.metadata.quantum_src = cell.source;
        this.openQuantumBuilder(cell.id);
    },

    // ------------------------------------------------------------------
    // Education mode
    // ------------------------------------------------------------------

    _initEducationMode: async function() {
        // Reset per-notebook education state so switching notebooks in the same
        // session never carries a stale assignment (which would wrongly show the
        // Problem button on a plain notebook).
        this._educationInfo = null;
        this._educationAssignment = null;
        this._taskCache = null;
        this._assignmentOpened = false;
        this._updateEducationToolbar();

        // Notebook switches in this app call loadNotebook() (and therefore
        // _initEducationMode) in-place, without a full page reload. If the
        // user switches again before the previous call's fetches resolve, the
        // stale call must NOT be allowed to overwrite state for the notebook
        // that's on screen now — otherwise the toolbar can get stuck showing
        // "not a teacher" (or the wrong assignment) depending on which
        // in-flight request happens to land last. A simple generation counter
        // makes every call check "am I still the most recent call?" before
        // touching shared state.
        var myGeneration = (this._eduModeGeneration || 0) + 1;
        this._eduModeGeneration = myGeneration;
        var isCurrent = function() { return this._eduModeGeneration === myGeneration; }.bind(this);

        var pid = this.notebook && this.notebook.project_id;
        this._eduDebug('_initEducationMode', { pid: pid, notebookExists: !!this.notebook, notebookProjectId: this.notebook && this.notebook.project_id, notebookId: this.notebook && this.notebook.id });
        if (!pid) { this._eduDebug('_initEducationMode aborting: no pid', {}); return; }
        try {
            var infoResp = await fetch('/api/education/projects/' + pid + '/info');
            this._eduDebug('_initEducationMode infoResp', { status: infoResp.status, ok: infoResp.ok });
            if (!infoResp.ok || !isCurrent()) return;
            var info = await infoResp.json();
            this._eduDebug('_initEducationMode info json', { info: info });
            if (!info.is_education || !isCurrent()) { this._eduDebug('_initEducationMode aborting: not is_education or superseded', {}); return; }
            this._educationInfo = info;

            // Find the assignment for this notebook (if it has been designated one)
            var nbId = this.notebook && this.notebook.id;
            if (nbId) {
                var asgResp = await fetch('/api/education/notebooks/' + nbId + '/assignment');
                if (asgResp.ok && isCurrent()) {
                    var asgData = await asgResp.json();
                    if (asgData.assignment && isCurrent()) {
                        this._educationAssignment = asgData.assignment;
                    }
                }
            }

            if (!isCurrent()) return;

            // Toolbar: teachers get "Make Assignment" on a plain notebook, or the
            // "Problem" button once the notebook is an assignment.
            this._updateEducationToolbar();

            // Warm the task cache up front. Teachers need it so the problem
            // editor pre-fills existing values (and saves via PATCH rather than
            // creating a duplicate); students need it to map answers to problems.
            if (this._educationAssignment) {
                await this._refreshEducationAssignment();
            }

            if (!isCurrent()) return;

            // Re-render cells to show education badges/controls
            if (info.is_teacher || info.is_student) {
                this._applyEducationCellStyles();
            }

            // Students: show their latest score/attempts inline on each answer cell.
            if (info.is_student && this._educationAssignment) {
                this._loadStudentSubmissions();
            }
        } catch (e) {
            console.warn('Education mode init error:', e);
        }
    },

    // Reflect the current role + assignment state in the education toolbar.
    // The Problem button is only available on notebooks that are assignments;
    // plain notebooks show a "Make Assignment" button instead.
    _updateEducationToolbar: function() {
        var info = this._educationInfo;
        var toolbar = document.getElementById('education-toolbar');
        var makeBtn = document.getElementById('make-assignment-btn');
        var probBtn = document.getElementById('add-problem-btn');
        var statusEl = document.getElementById('education-status');
        var isTeacher = !!(info && info.is_teacher);
        this._eduDebug('_updateEducationToolbar', { notebookId: this.notebookId, info: info, isTeacher: isTeacher, hasAssignment: !!this._educationAssignment, toolbarFound: !!toolbar, makeBtnFound: !!makeBtn, probBtnFound: !!probBtn });

        if (toolbar) toolbar.style.display = isTeacher ? 'inline-flex' : 'none';
        if (!isTeacher) {
            if (makeBtn) makeBtn.style.display = 'none';
            if (probBtn) probBtn.style.display = 'none';
            if (statusEl) statusEl.style.display = 'none';
            return;
        }

        var hasAssignment = !!this._educationAssignment;
        if (makeBtn) makeBtn.style.display = hasAssignment ? 'none' : 'inline-flex';
        if (probBtn) probBtn.style.display = hasAssignment ? 'inline-flex' : 'none';
        if (statusEl) {
            if (hasAssignment) {
                statusEl.textContent = this._educationAssignment.published ? 'Assignment · Published' : 'Assignment · Draft';
                statusEl.style.display = 'inline-flex';
            } else {
                statusEl.style.display = 'none';
            }
        }
    },

    // Teacher action: turn the current plain notebook into an assignment so it
    // can hold problems. After this the Problem button appears.
    createAssignmentFromNotebook: async function() {
        if (this._educationAssignment) {
            this.showToast('This notebook is already an assignment', 2500);
            this._updateEducationToolbar();
            return;
        }
        var info = this._educationInfo;
        if (!info || !info.is_teacher) {
            this.showToast('Only the teacher can create assignments', 3000);
            return;
        }
        // Persist any unsaved cells first so that when problems are added (which
        // appends cells server-side) it builds on the latest notebook content.
        try {
            if (this.notebook && this.notebook.dirty && typeof this.saveNotebook === 'function') {
                await this.saveNotebook();
            }
        } catch (e) { /* non-fatal */ }

        var asg = await this._ensureAssignmentForNotebook();
        if (!asg) {
            this.showToast('Could not create the assignment. Make sure you are the teacher for this education project.', 4000);
            return;
        }
        this.showToast('Notebook is now an assignment — use the Problem button to add tasks.', 3500);
        this._updateEducationToolbar();
    },

    _applyEducationCellStyles: function() {
        if (!this.notebook || !this.notebook.cells) return;
        var info = this._educationInfo;
        if (!info) return;
        for (var i = 0; i < this.notebook.cells.length; i++) {
            var cell = this.notebook.cells[i];
            var el = document.getElementById(cell.id);
            if (!el) continue;
            var role = cell.metadata && cell.metadata.education_role;
            if (role === 'problem' && info.is_student) {
                el.classList.add('education-locked');
            }
        }
    },

    openProblemEditor: async function(cellId) {
        this._eduDebug('openProblemEditor called', { cellId: cellId, educationInfo: this._educationInfo, assignment: this._educationAssignment });
        var rightbar = document.getElementById('nb-rightbar-problem');
        if (!rightbar) { this._eduDebug('nb-rightbar-problem element NOT FOUND, aborting', {}); return; }
        this._editingCellId = cellId || null;

        var answerHint = document.getElementById('pe-correct-answer');

        var headingEl = document.getElementById('problem-editor-heading');
        var titleEl = document.getElementById('problem-editor-title');
        if (cellId) {
            if (headingEl) headingEl.textContent = 'Edit Problem';
            if (titleEl) titleEl.textContent = 'EDIT PROBLEM';
            // Make sure we have the freshest task definitions before pre-filling,
            // so edits load the stored values and save via PATCH.
            await this._refreshEducationAssignment();
            var cell = this.notebook && this.notebook.getCell(cellId);
            if (cell) {
                var problemCellId = cell.metadata && cell.metadata.problem_cell_id;
                var targetCellId = problemCellId || cellId;
                var task = this._findTaskForCell(targetCellId);
                if (task) {
                    document.getElementById('pe-problem-type').value = task.problem_type;
                    document.getElementById('pe-title').value = task.config.title || '';
                    document.getElementById('pe-prompt').value = task.config.prompt || '';
                    document.getElementById('pe-points').value = task.points;
                    document.getElementById('pe-time-limit').value = task.time_limit_minutes || '';
                    if (task.problem_type === 'numeric' || task.problem_type === 'formula') {
                        // The correct answer is stored encrypted and never sent
                        // back, so we leave the field blank and hint that leaving
                        // it empty keeps the existing answer.
                        document.getElementById('pe-correct-answer').value = '';
                        if (answerHint) answerHint.placeholder = 'Stored answer hidden — leave blank to keep it';
                        document.getElementById('pe-tolerance').value = task.config.tolerance || 0.01;
                    } else if (task.problem_type === 'mc') {
                        var opts = (task.config.options || []).join('\n');
                        document.getElementById('pe-mc-options-text').value = opts;
                        document.getElementById('pe-mc-correct-index').value = task.config.correct_index || 0;
                    } else if (task.problem_type === 'code') {
                        var tests = (task.config.test_cases || []).map(function(t) { return t.code; }).join('\n');
                        document.getElementById('pe-code-tests').value = tests;
                    }
                } else {
                    // Problem cell exists but has no saved definition yet.
                    this._resetProblemEditor();
                }
            }
        } else {
            if (headingEl) headingEl.textContent = 'Add Problem';
            if (titleEl) titleEl.textContent = 'PROBLEM EDITOR';
            if (answerHint) answerHint.placeholder = 'e.g. 42';
            this._resetProblemEditor();
        }
        this._onProblemTypeChange();
        rightbar.classList.remove('collapsed');
        rightbar.setAttribute('aria-hidden', 'false');
        var _cs = getComputedStyle(rightbar);
        this._eduDebug('openProblemEditor finished', { className: rightbar.className, display: _cs.display, opacity: _cs.opacity, marginRight: _cs.marginRight, width: _cs.width });
    },

    closeProblemEditor: function() {
        var rightbar = document.getElementById('nb-rightbar-problem');
        if (rightbar) {
            rightbar.classList.add('collapsed');
            rightbar.setAttribute('aria-hidden', 'true');
        }
        this._editingCellId = null;
    },

    _resetProblemEditor: function() {
        document.getElementById('pe-problem-type').value = 'numeric';
        document.getElementById('pe-title').value = '';
        document.getElementById('pe-prompt').value = '';
        document.getElementById('pe-correct-answer').value = '';
        document.getElementById('pe-tolerance').value = '0.01';
        document.getElementById('pe-mc-options-text').value = '';
        document.getElementById('pe-mc-correct-index').value = '0';
        document.getElementById('pe-code-tests').value = '';
        document.getElementById('pe-points').value = '1';
        document.getElementById('pe-time-limit').value = '';
    },

    _onProblemTypeChange: function() {
        var pt = document.getElementById('pe-problem-type').value;
        document.getElementById('pe-config-fields').style.display = (pt === 'numeric' || pt === 'formula') ? 'block' : 'none';
        document.getElementById('pe-mc-options').style.display = pt === 'mc' ? 'block' : 'none';
        document.getElementById('pe-code-config').style.display = pt === 'code' ? 'block' : 'none';
        if (pt === 'numeric' || pt === 'formula') {
            document.getElementById('pe-correct-answer').parentElement.style.display = 'block';
        } else {
            document.getElementById('pe-correct-answer').parentElement.style.display = 'none';
            document.getElementById('pe-tolerance').parentElement.style.display = 'none';
        }
    },

    _findTaskForCell: function(cellId) {
        if (!this._educationAssignment) return null;
        // Fetch tasks for this assignment and cache them
        var aid = this._educationAssignment.id;
        var self = this;
        // Use a sync approach — return from cache or fetch
        if (this._taskCache && this._taskCache.aid === aid) {
            return (this._taskCache.tasks || []).find(function(t) { return t.cell_id === cellId; }) || null;
        }
        return null;
    },

    // Turn the current notebook into an assignment on demand. A notebook only
    // becomes an assignment once a row exists in education_assignments; without
    // it there is nothing to attach problems to. The first time a teacher adds a
    // problem we create that link automatically so authoring works entirely
    // inside the notebook IDE (no dashboard round-trip required).
    _ensureAssignmentForNotebook: async function() {
        if (this._educationAssignment) return this._educationAssignment;
        var info = this._educationInfo;
        if (!info || !info.is_teacher) return null;
        var pid = this.notebook && this.notebook.project_id;
        var nbId = this.notebook && this.notebook.id;
        if (!pid || !nbId) return null;
        try {
            var resp = await fetch('/api/education/projects/' + pid + '/assignments', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this._eduCsrfToken() },
                body: JSON.stringify({ notebook_id: nbId, title: (this.notebook.name || 'Assignment') }),
            });
            if (resp.ok) {
                var data = await resp.json();
                this._educationAssignment = { id: data.assignment_id, project_id: pid, title: this.notebook.name, published: false };
                return this._educationAssignment;
            }
            // 409 = this notebook is already an assignment (e.g. created in the
            // dashboard, or a concurrent save). Resolve the existing one.
            var getResp = await fetch('/api/education/notebooks/' + nbId + '/assignment');
            if (getResp.ok) {
                var gd = await getResp.json();
                if (gd.assignment) {
                    this._educationAssignment = gd.assignment;
                    return this._educationAssignment;
                }
            }
        } catch (e) {
            console.warn('Failed to create assignment for notebook:', e);
        }
        return null;
    },

    _saveProblem: async function() {
        this._eduDebug('_saveProblem called', { assignment: this._educationAssignment });
        if (!this._educationAssignment) {
            await this._ensureAssignmentForNotebook();
        }
        if (!this._educationAssignment) {
            this._eduDebug('_saveProblem aborting: no assignment linked', {});
            this.showToast('Could not link this notebook to an assignment. Make sure you are the teacher and this is an education project.', 4000);
            return;
        }
        var aid = this._educationAssignment.id;

        // Adding a problem appends cells to the notebook server-side and then
        // reloads, so flush any unsaved local edits first to avoid losing them.
        try {
            if (this.notebook && this.notebook.dirty && typeof this.saveNotebook === 'function') {
                await this.saveNotebook();
            }
        } catch (e) { /* non-fatal */ }

        this._eduDebug('_saveProblem reading form fields', {
            typeFieldFound: !!document.getElementById('pe-problem-type'),
            titleFieldFound: !!document.getElementById('pe-title'),
            promptFieldFound: !!document.getElementById('pe-prompt'),
            pointsFieldFound: !!document.getElementById('pe-points'),
            timeFieldFound: !!document.getElementById('pe-time-limit'),
        });

        var problemType, title, prompt, points, timeLimit, isEditing, config;
        try {
            problemType = document.getElementById('pe-problem-type').value;
            title = document.getElementById('pe-title').value.trim();
            prompt = document.getElementById('pe-prompt').value.trim();
            points = parseFloat(document.getElementById('pe-points').value) || 1;
            timeLimit = parseInt(document.getElementById('pe-time-limit').value) || null;

            isEditing = !!this._editingCellId;
            config = { points: points };
            if (problemType === 'numeric' || problemType === 'formula') {
                var answerVal = document.getElementById('pe-correct-answer').value.trim();
                // On edit, a blank field means "keep the existing (encrypted) answer" —
                // only send correct_answer when the teacher actually typed one, so we
                // never overwrite a stored answer with an empty string.
                if (answerVal || !isEditing) {
                    config.correct_answer = answerVal;
                }
                config.tolerance = parseFloat(document.getElementById('pe-tolerance').value) || 0.01;
            } else if (problemType === 'mc') {
                var optsText = document.getElementById('pe-mc-options-text').value.trim();
                config.options = optsText ? optsText.split('\n').map(function(s) { return s.trim(); }).filter(Boolean) : [];
                config.correct_index = parseInt(document.getElementById('pe-mc-correct-index').value) || 0;
            } else if (problemType === 'code') {
                var testsText = document.getElementById('pe-code-tests').value.trim();
                config.test_cases = testsText ? testsText.split('\n').map(function(line, idx) {
                    return { name: 'test' + (idx + 1), code: line.trim(), expected: 'None' };
                }).filter(function(t) { return t.code; }) : [];
                config.timeout_sec = 5;
            }
        } catch (e) {
            this._eduDebug('_saveProblem threw while reading form fields', { message: e && e.message, stack: e && e.stack });
            this.showToast('Error reading problem form: ' + e.message, 3000);
            return;
        }

        try {
            var cellId = this._editingCellId;
            var url, method;
            if (cellId) {
                // Update existing — find the problem ID
                var task = this._findTaskForCell(cellId);
                if (task) {
                    url = '/api/education/assignments/' + aid + '/tasks/' + task.id;
                    method = 'PATCH';
                } else {
                    url = '/api/education/assignments/' + aid + '/tasks';
                    method = 'POST';
                }
            } else {
                url = '/api/education/assignments/' + aid + '/tasks';
                method = 'POST';
            }

            var body = {
                problem_type: problemType,
                title: title,
                prompt: prompt,
                config: config,
                points: points,
                time_limit_minutes: timeLimit,
            };

            var csrfToken = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
            if (!csrfToken) {
                var meta = document.querySelector('meta[name="csrf-token"]');
                if (meta) csrfToken = meta.getAttribute('content') || '';
            }

            this._eduDebug('_saveProblem about to fetch', { method: method, url: url, body: body });
            var resp = await fetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify(body),
            });
            this._eduDebug('_saveProblem fetch returned', { status: resp.status, ok: resp.ok });
            if (!resp.ok) {
                var err = await resp.json().catch(function() { return { message: 'Save failed' }; });
                this.showToast(err.message || 'Failed to save problem', 3000);
                return;
            }
            var data = await resp.json();

            // Refresh the assignment to reload notebook cells
            this.showToast('Problem saved', 2000);
            this.closeProblemEditor();

            // Reload the notebook to get the new cells from the server
            if (this.notebook && this.notebook.id) {
                await this.loadNotebook(this.notebook.id);
            }
        } catch (e) {
            console.error('Save problem error:', e);
            this._eduDebug('_saveProblem caught exception', { message: e && e.message, stack: e && e.stack });
            this.showToast('Error saving problem: ' + e.message, 3000);
        }
    },

    _refreshEducationAssignment: async function() {
        // Re-fetch assignment data to keep task cache current
        if (!this._educationAssignment) return;
        try {
            var resp = await fetch('/api/education/assignments/' + this._educationAssignment.id + '/tasks');
            if (resp.ok) {
                var data = await resp.json();
                this._taskCache = { aid: this._educationAssignment.id, tasks: data.tasks || [] };
            }
        } catch (e) {
            console.warn('Failed to refresh assignment tasks:', e);
        }
    },

    openAssignmentSettings: function() {
        // Open the education settings sidebar for this project
        var sid = document.getElementById('nb-settings-rightbar');
        if (sid) sid.classList.toggle('collapsed');
        this.showToast('Configure assignment settings in the project dashboard', 2000);
    },

    _eduCsrfToken: function() {
        var token = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
        if (!token) {
            var m = document.querySelector('meta[name="csrf-token"]');
            if (m) token = m.getAttribute('content') || '';
        }
        return token;
    },

    // POST the assignment /open endpoint. Time-limited assignments must be
    // "opened" (which starts the countdown) before a submission is accepted, so
    // this is called lazily the first time a student submits.
    _openAssignmentForStudent: async function() {
        if (!this._educationAssignment) return null;
        try {
            var resp = await fetch('/api/education/assignments/' + this._educationAssignment.id + '/open', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this._eduCsrfToken() },
            });
            if (resp.ok) {
                this._assignmentOpened = true;
                return await resp.json();
            }
        } catch (e) {
            console.warn('Failed to open assignment:', e);
        }
        return null;
    },

    _submitStudentAnswer: async function(cellId) {
        if (!this._educationAssignment) {
            this.showToast('Not an education assignment', 3000);
            return;
        }
        var cell = this.notebook && this.notebook.getCell(cellId);
        if (!cell) return;
        var answer = cell.cm ? cell.cm.getValue() : cell.source;
        if (!answer || !answer.trim()) {
            this.showToast('Please write an answer first', 3000);
            return;
        }
        var problemCellId = cell.metadata && cell.metadata.problem_cell_id;
        var aid = this._educationAssignment.id;
        var self = this;

        try {
            // Make sure the task cache is current, then resolve this answer cell
            // to its problem.
            if (!this._taskCache || this._taskCache.aid !== aid) {
                await this._refreshEducationAssignment();
            }
            var tasks = (this._taskCache && this._taskCache.tasks) || [];
            var task = tasks.find(function(t) { return t.cell_id === problemCellId; });
            if (!task) {
                this.showToast('Problem not found for this answer cell', 3000);
                return;
            }

            this._renderEduResult(cellId, { status: 'submitting' });

            var doSubmit = function() {
                return fetch('/api/education/assignments/' + aid + '/problems/' + task.id + '/submit', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': self._eduCsrfToken() },
                    body: JSON.stringify({ answer: answer }),
                });
            };

            var submitResp = await doSubmit();
            // A time-limited assignment rejects submissions until it's been
            // opened. Open it transparently and retry once so the student never
            // hits a dead end.
            if (submitResp.status === 400 && !this._assignmentOpened) {
                var errPeek = await submitResp.clone().json().catch(function() { return {}; });
                if (errPeek.message && /open the assignment/i.test(errPeek.message)) {
                    await this._openAssignmentForStudent();
                    submitResp = await doSubmit();
                }
            }

            if (!submitResp.ok) {
                var err = await submitResp.json().catch(function() { return { message: 'Submit failed' }; });
                this._renderEduResult(cellId, { status: 'error', message: err.message || 'Submit failed', points: task.points });
                this.showToast(err.message || 'Submit failed', 3000);
                return;
            }
            var result = await submitResp.json();
            this._renderEduResult(cellId, {
                status: 'done',
                graded: result.graded,
                is_correct: result.is_correct,
                score: result.auto_score,
                points: task.points,
                feedback: result.feedback,
                attempts_remaining: result.attempts_remaining,
            });
        } catch (e) {
            console.error('Submit error:', e);
            this._renderEduResult(cellId, { status: 'error', message: e.message });
            this.showToast('Error submitting answer: ' + e.message, 3000);
        }
    },

    // Render a persistent status band under a student's answer cell so results
    // survive re-renders instead of vanishing with a toast.
    _renderEduResult: function(cellId, state) {
        var box = document.getElementById('nb-edu-result-' + cellId);
        if (!box) return;
        var esc = this.escapeHtml.bind(this);

        if (state.status === 'submitting') {
            box.className = 'nb-edu-result pending';
            box.style.display = 'block';
            box.innerHTML = '<span class="nb-edu-result-label">Submitting…</span>';
            return;
        }
        if (state.status === 'error') {
            box.className = 'nb-edu-result error';
            box.style.display = 'block';
            box.innerHTML = '<span class="nb-edu-result-label">' + esc(state.message || 'Submission failed') + '</span>';
            return;
        }

        var cls, label;
        if (!state.graded) {
            cls = 'pending';
            label = 'Submitted — awaiting manual grading';
        } else if (state.is_correct) {
            cls = 'correct';
            label = 'Correct';
        } else {
            cls = 'incorrect';
            label = 'Incorrect';
        }

        var scoreStr = '';
        if (state.score !== undefined && state.score !== null) {
            var pts = (state.points !== undefined && state.points !== null) ? (' / ' + state.points) : '';
            scoreStr = '<span class="nb-edu-result-score">' + esc(String(state.score)) + pts + ' pts</span>';
        }
        var attemptsStr = '';
        if (state.attempts_remaining !== undefined && state.attempts_remaining !== null) {
            attemptsStr = '<span class="nb-edu-result-attempts">' + state.attempts_remaining + ' attempt(s) left</span>';
        }
        var feedbackStr = state.feedback ? '<div class="nb-edu-result-feedback">' + esc(state.feedback) + '</div>' : '';

        box.className = 'nb-edu-result ' + cls;
        box.style.display = 'block';
        box.innerHTML =
            '<div class="nb-edu-result-row">' +
                '<span class="nb-edu-result-label">' + label + '</span>' +
                scoreStr + attemptsStr +
            '</div>' + feedbackStr;
    },

    // On load, show each answer cell's most recent submission so returning
    // students immediately see where they stand.
    _loadStudentSubmissions: async function() {
        if (!this._educationAssignment) return;
        var aid = this._educationAssignment.id;
        try {
            var resp = await fetch('/api/education/assignments/' + aid + '/submissions');
            if (!resp.ok) return;
            var data = await resp.json();
            var subs = data.submissions || [];
            if (!subs.length) return;

            var tasks = (this._taskCache && this._taskCache.tasks) || [];
            var problemToCell = {};
            tasks.forEach(function(t) { problemToCell[t.id] = t.cell_id; });

            // Keep the latest submission per problem (submissions arrive newest-first
            // for students, but sort defensively by attempt number).
            var latest = {};
            subs.forEach(function(s) {
                var prev = latest[s.problem_id];
                if (!prev || (s.attempt_number || 0) >= (prev.attempt_number || 0)) {
                    latest[s.problem_id] = s;
                }
            });

            var self = this;
            Object.keys(latest).forEach(function(pid) {
                var s = latest[pid];
                var problemCellId = problemToCell[pid];
                if (!problemCellId) return;
                var answerCellId = problemCellId + '-answer';
                var score = (s.final_score != null) ? s.final_score : s.auto_score;
                var task = tasks.find(function(t) { return t.id == pid; });
                // Prefer the server-computed is_correct (captured before any late
                // penalty). Older rows predating that column fall back to the
                // score-vs-points heuristic.
                var isCorrect = (s.is_correct != null) ? s.is_correct : (score != null && task && score >= task.points);
                self._renderEduResult(answerCellId, {
                    status: 'done',
                    graded: s.status === 'graded',
                    is_correct: isCorrect,
                    score: score,
                    points: task ? task.points : null,
                    feedback: s.feedback,
                });
            });
        } catch (e) {
            console.warn('Failed to load student submissions:', e);
        }
    },

    openQuantumBuilder: function(cellId) {
        if (!window.QuantumCircuitBuilder) {
            this.showToast('Circuit builder failed to load', 3000);
            return;
        }
        var cell = this.notebook && this.notebook.getCell(cellId);
        if (!cell) return;
        var panel = document.getElementById('split-panel');
        var contentEl = document.getElementById('split-content');
        var titleEl = document.getElementById('split-file-title');
        var saveBtn = document.getElementById('split-save-btn');
        if (!panel || !contentEl) return;

        // Visual edits regenerate the whole cell — warn if the generated code
        // was edited by hand since the last regeneration.
        var currentSrc = cell.cm ? cell.cm.getValue() : cell.source;
        if (cell.metadata && cell.metadata.quantum_src && currentSrc.trim() &&
                currentSrc !== cell.metadata.quantum_src) {
            var msg = 'This cell was edited by hand. Visual circuit edits regenerate the code and will overwrite those changes. Continue?';
            if (!confirm(msg)) return;
        }

        // Tear down whatever the split panel was showing.
        if (this._splitCM) {
            try { this._splitCM.toTextArea(); } catch (e) {}
            this._splitCM = null;
        }
        this._splitFileId = null;
        this._splitModelObj = null;
        this._splitWolfram = null;
        this._splitWolframContent = null;
        contentEl.classList.remove('nb-split-model');
        contentEl.classList.remove('nb-split-wolfram');
        contentEl.innerHTML = '';
        contentEl.style.padding = '';

        contentEl.classList.add('nb-split-quantum');
        if (titleEl) titleEl.textContent = 'Quantum Circuit';
        if (saveBtn) saveBtn.style.display = 'none';

        var self = this;
        this._quantumBuilderCellId = cellId;
        this._quantumBuilder = QuantumCircuitBuilder.open({
            container: contentEl,
            circuit: (cell.metadata && cell.metadata.quantum) || null,
            onChange: function(model, code) {
                var c = self.notebook && self.notebook.getCell(self._quantumBuilderCellId);
                if (!c) return;
                if (!c.metadata) c.metadata = {};
                c.metadata.quantum = model;
                c.metadata.quantum_src = code;
                c.source = code;
                if (c.cm) c.cm.setValue(code);
                self.notebook.dirty = true;
            }
        });

        panel.style.display = '';
        panel.style.width = '';
        panel.style.minWidth = '';
        panel.style.maxWidth = '';
        this._updateSplitLayout();
        this.setActiveCell(cellId);
    },

    closeSplitPanel: function() {
        var panel = document.getElementById('split-panel');
        if (panel) {
            panel.style.display = 'none';
            // Clear inline width from any prior resize so the panel returns
            // to its CSS default width next time it opens.
            panel.style.width = '';
            panel.style.minWidth = '';
            panel.style.maxWidth = '';
        }
        if (this._splitCM) {
            var cm = this._splitCM;
            cm.toTextArea();
            this._splitCM = null;
        }
        this._splitFileId = null;
        this._splitModelObj = null;
        this._splitWolfram = null;
        this._splitWolframContent = null;
        this._quantumBuilderCellId = null;
        this._quantumBuilder = null;
        var contentEl = document.getElementById('split-content');
        if (contentEl) {
            contentEl.classList.remove('nb-split-model');
            contentEl.classList.remove('nb-split-wolfram');
            contentEl.classList.remove('nb-split-quantum');
            contentEl.style.padding = '';
            contentEl.innerHTML = '';
        }
        var saveBtn = document.getElementById('split-save-btn');
        if (saveBtn) saveBtn.style.display = 'none';
        this._updateSplitLayout();
    },

    _updateSplitLayout: function() {
        var panel = document.getElementById('split-panel');
        var wrapper = document.getElementById('cells-wrapper');
        if (!wrapper) return;
        if (panel && panel.style.display !== 'none') {
            wrapper.classList.add('has-split');
        } else {
            wrapper.classList.remove('has-split');
        }
    },

    _initSplitResize: function() {
        var handle = document.getElementById('split-resize-handle');
        var panel = document.getElementById('split-panel');
        if (!handle || !panel) return;
        var self = this;
        handle.addEventListener('mousedown', function(e) {
            e.preventDefault();
            var startX = e.clientX;
            var startW = panel.offsetWidth;
            function onMove(ev) {
                var dx = startX - ev.clientX;
                var newW = startW + dx;
                var wrapper = document.getElementById('cells-wrapper');
                var maxW = wrapper ? wrapper.offsetWidth * 0.6 : window.innerWidth * 0.6;
                var minW = 260;
                newW = Math.max(minW, Math.min(maxW, newW));
                panel.style.width = newW + 'px';
                panel.style.minWidth = '0';
                panel.style.maxWidth = 'none';
                if (self._splitCM) self._splitCM.refresh();
            }
            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            }
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });
    },

    _getCodeMirrorMode: function(filename) {
        var ext = filename.split('.').pop().toLowerCase();
        var modeMap = {
            'py': 'python', 'js': 'javascript', 'ts': 'typescript',
            'jsx': 'jsx', 'tsx': 'jsx',
            'html': 'xml', 'xml': 'xml', 'svg': 'xml',
            'css': 'css', 'scss': 'css', 'less': 'css',
            'json': 'javascript', 'md': 'markdown',
            'c': 'text/x-csrc', 'cpp': 'text/x-c++src', 'h': 'text/x-csrc',
            'rs': 'rust', 'java': 'text/x-java',
            'yaml': 'yaml', 'yml': 'yaml',
            'sh': 'shell', 'bash': 'shell',
            'sql': 'sql', 'r': 'r', 'go': 'go'
        };
        return modeMap[ext] || 'text';
    },

    saveTextTab: async function(tab) {
        if (!tab || !tab.cm || !tab.fileId) return;
        try {
            var res = await fetch('/api/files/' + tab.fileId + '/content', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ content: tab.cm.getValue() })
            });
            if (res.ok) {
                tab.dirty = false;
                this._renderTabBar();
                this.showToast('File saved', 1500);
            } else {
                this.showToast('Failed to save file', 3000);
            }
        } catch (e) {
            console.error('Failed to save file:', e);
            this.showToast('Failed to save file: ' + e.message, 3000);
        }
    },

    interruptKernel: function() {
        if (!this.kernel || !this.kernel.isConnected()) {
            this.showToast(window.i18n ? window.i18n.t('notebook.noActiveKernel') : 'No active kernel');
            return;
        }

        this.kernel.interrupt();
        this.showToast(window.i18n ? window.i18n.t('notebook.interruptingKernel') : 'Interrupting kernel...');
    },

    restartKernel: async function(skipConfirm) {
        if (!this.notebookId) {
            this.showToast(window.i18n ? window.i18n.t('notebook.noNotebookLoaded') : 'No notebook loaded');
            return false;
        }

        if (!skipConfirm && !confirm(window.i18n ? window.i18n.t('notebook.restartKernelConfirm') : 'Restart kernel? All variables will be lost.')) return false;

        this.showToast(window.i18n ? window.i18n.t('notebook.restartingKernel') : 'Restarting kernel...', 2000);
        return await this.kernel.restart(this.notebookId);
    },

    restartAndRunAll: async function() {
        if (!confirm(window.i18n ? window.i18n.t('notebook.restartRunAllConfirm') : 'Restart kernel and run all cells? All variables will be lost.')) return;
        const ok = await this.restartKernel(true);
        if (!ok) return;
        await this.runAllCells();
    },

    restartAndClearOutputs: async function() {
        if (!confirm(window.i18n ? window.i18n.t('notebook.restartClearConfirm') : 'Restart kernel and clear all outputs?')) return;
        const ok = await this.restartKernel(true);
        if (!ok) return;
        this.clearAllOutputs();
    },

    shutdownKernel: async function() {
        if (!this.kernel || (!this.kernel.isConnected() && !this.kernel.sessionId)) {
            this.showToast(window.i18n ? window.i18n.t('notebook.noActiveKernel') : 'No active kernel');
            return;
        }
        if (!confirm(window.i18n ? window.i18n.t('notebook.shutdownKernelConfirm') : 'Shut down the kernel? The runtime container will be stopped.')) return;
        await this.disconnectKernel();
        this.refreshKernelsList(true);
    },

    // "Change runtime": pick a different runtime level (Python / Quantum /
    // Wolfram / Hybrid / custom) and restart the kernel on it.
    changeRuntime: async function() {
        if (!this.notebookId || !this.kernel) {
            this.showToast(window.i18n ? window.i18n.t('notebook.noNotebookLoaded') : 'No notebook loaded');
            return;
        }

        var current = this.kernel._activeRuntime || 'python';
        var runtimes = [
            { name: 'python', display_name: 'Python 3.11' },
            { name: 'quantum', display_name: 'Python 3.11 Quantum' },
            { name: 'wolfram', display_name: 'Wolfram Language' },
            { name: 'hybrid', display_name: 'Quantum + Wolfram (Experimental)' }
        ];
        // Merge in custom admin-defined runtime levels, if any.
        try {
            var res = await fetch('/api/notebooks/runtimes', { credentials: 'same-origin' });
            if (res.ok) {
                var data = await res.json();
                (data.runtimes || []).forEach(function(rt) {
                    if (!runtimes.some(function(r) { return r.name === rt.name; }) && rt.name !== 'basic') {
                        runtimes.push(rt);
                    }
                });
            }
        } catch (e) { /* offline list is fine */ }

        runtimes.forEach(function(rt) {
            if (rt.name === current) {
                rt.display_name = rt.display_name + ' ✓';
            }
        });

        var chosen = await this.kernel._showRuntimeSelector(runtimes);
        if (!chosen || chosen === current) return;

        if (this.kernel.isConnected() || this.kernel.sessionId) {
            if (!confirm(window.i18n ? window.i18n.t('notebook.changeRuntimeConfirm') : 'Switching runtime restarts the kernel. All variables will be lost.')) return;
            await this.kernel.disconnect();
        }
        this.showToast((window.i18n ? window.i18n.t('notebook.switchingRuntime') : 'Switching runtime to ') + chosen + '...', 2500);
        var ok = await this.kernel.connect(this.notebookId, { runtimeLevel: chosen, forceRestart: true });
        if (ok) {
            this.refreshKernelsList(true);
        }
    },

    disconnectKernel: async function() {
        if (!this.kernel) return;
        
        const wasConnected = this.kernel.isConnected();
        const result = await this.kernel.disconnect();
        
        if (wasConnected && result) {
            this.showToast(NbT('kernelDisconnected', 'Kernel disconnected'), 2000);
        }
    },

    connectKernel: async function() {
        if (!this.notebookId) {
            this.showToast(window.i18n ? window.i18n.t('notebook.noNotebookLoaded') : 'No notebook loaded');
            return false;
        }

        if (!this.kernel) {
            this.showToast(window.i18n ? window.i18n.t('notebook.kernelNotAvailable') : 'Kernel not available');
            return false;
        }

        if (this.kernel.isConnected()) {
            return true;
        }

        if (this.kernel.isConnecting()) {
            this.showToast(window.i18n ? window.i18n.t('notebook.kernelStarting') : 'Kernel is starting...', 1500);
            var result = await this.kernel.waitForConnection();
            if (result) this.terminal.connect();
            return result;
        }

        // Wait for user preferences (which carry defaultRuntime) to finish loading
        // before deciding which runtime to request, otherwise we silently fall back to 'python'.
        if (!this._userPreferences || !this._userPreferences.defaultRuntime) {
            try {
                if (typeof this.refreshSettingsPanel === 'function') {
                    await this.refreshSettingsPanel(true);
                }
            } catch (e) {
                // Non-fatal; kernel.connect will still work, just with default runtime.
            }
        }

        var result = await this.kernel.connect(this.notebookId);
        if (result) this.terminal.connect();
        return result;
    },

    clearOutput: function() {
        if (this.activeCellId && this.notebook) {
            const cell = this.notebook.getCell(this.activeCellId);
            if (cell) {
                cell.clearOutputs();
                this.updateCellOutputUI(cell);
                this.notebook.dirty = true;
            }
        }
    },

    ensureKernelReady: async function() {
        if (!this.notebookId) {
            this.showToast(window.i18n ? window.i18n.t('notebook.noNotebookLoaded') : 'No notebook loaded');
            return false;
        }
        if (this.kernel && this.kernel.isConnected()) {
            return true;
        }
        return this.connectKernel();
    },

    // Shared sequential runner: executes cells in order, stopping the queue
    // when a cell errors or the kernel is halted/restarted mid-run (Jupyter
    // semantics — later cells depending on the failed one must not run).
    _runCellQueue: async function(cells) {
        for (const cell of cells) {
            const result = await this.runCell(cell.id);
            if (result && result.status === 'error') {
                if (result.interrupted || result.restarted) {
                    this.showToast(window.i18n ? window.i18n.t('notebook.runQueueHalted') : 'Run stopped: kernel halted', 3000);
                } else if (!result.dispatchFailed) {
                    this.showToast(window.i18n ? window.i18n.t('notebook.runQueueStopped') : 'Run stopped: a cell raised an error', 3000);
                }
                return false;
            }
            // Small delay between cells
            await new Promise(resolve => setTimeout(resolve, 100));
        }
        return true;
    },

    runAllCells: async function() {
        const connected = await this.ensureKernelReady();
        if (!connected) {
            return;
        }

        const codeCells = this.notebook.cells.filter(c => isExecutableType(c.type));
        await this._runCellQueue(codeCells);
    },

    clearAllOutputs: function() {
        if (!this.notebook) return;
        this.notebook.cells.forEach(cell => {
            cell.clearOutputs();
            this.updateCellOutputUI(cell);
        });
        this.notebook.dirty = true;
        this.showToast(window.i18n ? window.i18n.t('notebook.allOutputsCleared') : 'All outputs cleared', 2000);
    },

    runAllAbove: async function(cellId) {
        const connected = await this.ensureKernelReady();
        if (!connected) {
            return;
        }
        const idx = this.notebook.cells.findIndex(c => c.id === cellId);
        if (idx <= 0) return;
        const cellsAbove = this.notebook.cells.slice(0, idx).filter(c => isExecutableType(c.type));
        await this._runCellQueue(cellsAbove);
    },

    runAllBelow: async function(cellId) {
        const connected = await this.ensureKernelReady();
        if (!connected) {
            return;
        }
        const idx = this.notebook.cells.findIndex(c => c.id === cellId);
        if (idx < 0) return;
        const cellsBelow = this.notebook.cells.slice(idx).filter(c => isExecutableType(c.type));
        await this._runCellQueue(cellsBelow);
    },

    toggleWideLayout: function() {
        const container = document.getElementById('cells-container');
        if (!container) return;
        container.classList.toggle('wide');
        const btn = document.getElementById('toggle-wide-btn');
        if (btn) {
            const isWide = container.classList.contains('wide');
            btn.title = isWide ? (window.i18n ? window.i18n.t('notebook.normalWidth') : 'Normal Width') : (window.i18n ? window.i18n.t('notebook.wideLayout') : 'Wide Layout');
        }
    },

    // Find bar
    _findState: { matches: [], currentIdx: -1, query: '' },

    toggleFindBar: function() {
        const bar = document.getElementById('find-bar');
        if (!bar) return;
        const isActive = bar.classList.toggle('active');
        if (isActive) {
            const input = document.getElementById('find-input');
            if (input) input.focus();
        } else {
            this.clearFindHighlights();
        }
    },

    closeFindBar: function() {
        const bar = document.getElementById('find-bar');
        if (bar) bar.classList.remove('active');
        this.clearFindHighlights();
    },

    doFind: function(query) {
        this.clearFindHighlights();
        this._findState = { matches: [], currentIdx: -1, query: query };
        if (!query || !this.notebook) {
            document.getElementById('find-count').textContent = '';
            return;
        }
        const lowerQ = query.toLowerCase();
        this.notebook.cells.forEach(cell => {
            if (cell.cm) {
                const text = cell.cm.getValue();
                let startPos = 0;
                while (true) {
                    const idx = text.toLowerCase().indexOf(lowerQ, startPos);
                    if (idx < 0) break;
                    const from = cell.cm.posFromIndex(idx);
                    const to = cell.cm.posFromIndex(idx + query.length);
                    const marker = cell.cm.markText(from, to, { className: 'cm-find-highlight' });
                    this._findState.matches.push({ cellId: cell.id, marker: marker, from: from, to: to });
                    startPos = idx + query.length;
                }
            }
        });
        const count = this._findState.matches.length;
        document.getElementById('find-count').textContent = count > 0 ? (window.i18n ? window.i18n.t('notebook.nFound').replace('{n}', count) : count + ' found') : (window.i18n ? window.i18n.t('notebook.noResults') : 'No results');
        if (count > 0) this.findNext();
    },

    findNext: function() {
        const s = this._findState;
        if (s.matches.length === 0) return;
        s.currentIdx = (s.currentIdx + 1) % s.matches.length;
        this._scrollToMatch(s.currentIdx);
    },

    findPrev: function() {
        const s = this._findState;
        if (s.matches.length === 0) return;
        s.currentIdx = (s.currentIdx - 1 + s.matches.length) % s.matches.length;
        this._scrollToMatch(s.currentIdx);
    },

    _scrollToMatch: function(idx) {
        const match = this._findState.matches[idx];
        if (!match) return;
        const cellEl = document.getElementById(match.cellId);
        if (cellEl) cellEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const cell = this.notebook.getCell(match.cellId);
        if (cell && cell.cm) {
            cell.cm.setSelection(match.from, match.to);
            cell.cm.focus();
        }
        document.getElementById('find-count').textContent =
            (idx + 1) + '/' + this._findState.matches.length;
    },

    clearFindHighlights: function() {
        if (this._findState && this._findState.matches) {
            this._findState.matches.forEach(m => { if (m.marker) m.marker.clear(); });
        }
        this._findState = { matches: [], currentIdx: -1, query: '' };
        var countEl = document.getElementById('find-count');
        if (countEl) countEl.textContent = '';
    },

    _resolveFileName: function(file) {
        return file.name || file.filename || 'file';
    },

    _resolveFileContainerPath: function(file) {
        if (file && file.container_path) {
            return file.container_path;
        }
        const candidatePath = (file && (file.path || file.filename || file.name)) || '';
        return this.getNotebookPath(candidatePath);
    },

    _normalizeExplorerFiles: function(rawFiles) {
        const rows = Array.isArray(rawFiles) ? rawFiles : [];
        return rows.map((row) => {
            const normalized = Object.assign({}, row);
            normalized.name = this._resolveFileName(row);
            normalized.type = normalized.type || 'upload';
            normalized.container_path = this._resolveFileContainerPath(row);
            return normalized;
        });
    },

    _getFileType: function(filename) {
        const ext = (filename || '').split('.').pop().toLowerCase();
        const typeMap = {
            'py': 'python', 'js': 'javascript', 'ts': 'typescript', 'jsx': 'react', 'tsx': 'react',
            'csv': 'data', 'tsv': 'data', 'json': 'data', 'xml': 'data', 'yaml': 'data', 'yml': 'data',
            'pdb': 'structure', 'cif': 'structure', 'mmcif': 'structure', 'mol': 'structure', 'sdf': 'structure',
            'png': 'image', 'jpg': 'image', 'jpeg': 'image', 'gif': 'image', 'svg': 'image', 'webp': 'image',
            'zip': 'archive', 'tar': 'archive', 'gz': 'archive', 'bz2': 'archive', 'rar': 'archive',
            'md': 'text', 'txt': 'text', 'rst': 'text', 'log': 'text',
            'html': 'web', 'css': 'web', 'scss': 'web',
            'pdf': 'document', 'doc': 'document', 'docx': 'document',
            'ipynb': 'notebook',
            'nb': 'wolfram', 'wl': 'wolfram', 'wls': 'wolfram',
            'm': 'wolfram', 'mx': 'wolfram', 'wxf': 'wolfram', 'wdx': 'wolfram',
            'pt': 'model', 'pth': 'model', 'h5': 'model', 'onnx': 'model', 'pkl': 'model', 'joblib': 'model'
        };
        return typeMap[ext] || 'file';
    },

    _getFileIcon: function(type, isDir) {
        // Monochrome icons using design tokens instead of hardcoded Tailwind
        // hex colors — keeps the file tree subtle and theme-aware rather
        // than a rainbow. Only notebook/wolfram keep a single accent so
        // they stand out as runnable.
        var cTertiary = 'var(--text-tertiary, #AFBDCC)';
        var cOrange = 'var(--accent-orange, #f97316)';
        var cSecondary = 'var(--accent-secondary, #8b5cf6)';
        if (isDir) {
            return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
        }
        const icons = {
            'python': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
            'javascript': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
            'data': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>',
            'structure': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><path d="M2 15c6.667-6 13.333 0 20-6"/><path d="M2 9c6.667-6 13.333 0 20-6"/><path d="M2 3c6.667-6 13.333 0 20-6"/></svg>',
            'image': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
            'archive': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><path d="M21 8v13H3V8"/><rect x="3" y="3" width="18" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>',
            'text': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
            'web': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
            'document': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
            'notebook': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cOrange + ';"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
            'wolfram': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cOrange + ';"><path d="M3 20h18"/><path d="M6 20l3-12 3 6 3-12 3 12"/><path d="M3 8c2 2 4 3 6 3s4-1 6-3"/></svg>',
            'model': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cSecondary + ';"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>'
        };
        return icons[type] || '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:' + cTertiary + ';"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>';
    },

    _formatFileSize: function(bytes) {
        if (!bytes || bytes === 0) return '';
        const units = ['B', 'KB', 'MB', 'GB'];
        let i = 0;
        let size = bytes;
        while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
        return size.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
    },

    // Cycle the file-tree sort order: name -> modified (newest first) ->
    // size (largest first) -> name. Re-renders the explorer so the new
    // order is visible immediately.
    cycleFileSort: function() {
        var order = ['name', 'modified', 'size'];
        var cur = this._fileSort || 'name';
        var idx = order.indexOf(cur);
        this._fileSort = order[(idx + 1) % order.length];
        try { localStorage.setItem('phonos-notebook-file-sort', this._fileSort); } catch (e) {}
        this._syncFileSortButton();
        this.refreshFileExplorer(true);
    },

    // Reflect the active sort order on the toolbar button. data-sort lets CSS
    // mark the button as "not the default", so the order is discoverable
    // without hovering for the tooltip.
    _syncFileSortButton: function() {
        var btn = document.getElementById('nb-file-sort-btn');
        if (!btn) return;
        var sort = this._fileSort || 'name';
        var labels = {
            name: window.i18n ? window.i18n.t('notebook.sortByName') : 'Sort: Name',
            modified: window.i18n ? window.i18n.t('notebook.sortByModified') : 'Sort: Modified (newest first)',
            size: window.i18n ? window.i18n.t('notebook.sortBySize') : 'Sort: Size (largest first)'
        };
        btn.title = labels[sort] || labels.name;
        btn.dataset.sort = sort;
    },

    // Comparator for the active sort order. Directories always sort first.
    _compareFiles: function(a, b) {
        var sort = this._fileSort || 'name';
        var aDir = !!(a.is_dir || a.type === 'directory');
        var bDir = !!(b.is_dir || b.type === 'directory');
        if (aDir !== bDir) return aDir ? -1 : 1;
        var an = this._resolveFileName(a).toLowerCase();
        var bn = this._resolveFileName(b).toLowerCase();
        if (sort === 'name') return an < bn ? -1 : (an > bn ? 1 : 0);
        if (sort === 'size') {
            var as = a.size || a.file_size || 0;
            var bs = b.size || b.file_size || 0;
            return bs - as; // largest first
        }
        if (sort === 'modified') {
            var at = new Date(a.modified_at || a.updated_at || 0).getTime();
            var bt = new Date(b.modified_at || b.updated_at || 0).getTime();
            return bt - at; // newest first
        }
        return 0;
    },

    // Toggle expand/collapse of a folder row. When collapsing, hide following
    // file items in the same section whose source_path is under the folder.
    // Best-effort: relies on source_path being present; if absent, only the
    // chevron visual toggles.
    _toggleFolder: function(folderItem) {
        if (!folderItem) return;
        var expanded = folderItem.classList.toggle('expanded');
        var folderPath = folderItem.dataset.folderPath;
        if (!folderPath) return;
        var prefix = folderPath + '/';
        // Walk forward until the next section header (or end of container).
        var sib = folderItem.nextElementSibling;
        while (sib && !sib.classList.contains('file-section-header')) {
            if (sib.classList.contains('file-item') && !sib.classList.contains('folder-item')) {
                var childPath = sib.dataset.filePath || '';
                if (childPath && childPath.indexOf(prefix) === 0) {
                    sib.style.display = expanded ? '' : 'none';
                }
            }
            sib = sib.nextElementSibling;
        }
    },

    _filterFileTree: function(query) {
        const tree = document.getElementById('file-tree');
        const items = document.querySelectorAll('#file-tree .file-item');
        query = (query || '').toLowerCase();
        // Remembered so a refresh (upload, delete, sort change) can re-apply the
        // filter instead of dumping the user back into the unfiltered tree.
        this._fileFilter = query;
        let matches = 0;
        let visibleBySection = {}; // sectionEl -> count of visible children
        items.forEach(item => {
            // Match against the file-name text, not the first span (which
            // may be the selection checkbox).
            const nameEl = item.querySelector('.file-name');
            const name = (nameEl ? nameEl.textContent : (item.textContent || '')).toLowerCase();
            const visible = !query || name.includes(query);
            item.style.display = visible ? '' : 'none';
            if (visible) matches++;
            // Track visibility per enclosing section by walking up to the
            // section header that precedes this item.
            let sib = item.previousElementSibling;
            while (sib && !sib.classList.contains('file-section-header')) {
                sib = sib.previousElementSibling;
            }
            if (sib) {
                visibleBySection[sib.dataset.section || ''] = (visibleBySection[sib.dataset.section || ''] || 0) + (visible ? 1 : 0);
                sib._visibleCount = (sib._visibleCount || 0) + (visible ? 1 : 0);
            }
        });
        // Hide section headers that have no visible children (previously this
        // was computed but never applied, leaving empty sections visible).
        const sections = document.querySelectorAll('#file-tree .file-section-header');
        sections.forEach(section => {
            const total = section._visibleCount || 0;
            section.style.display = total > 0 ? '' : 'none';
            section._visibleCount = 0; // reset for next invocation
        });

        // Hiding every section used to leave a blank panel with no explanation.
        // Say so instead.
        if (tree) {
            let empty = tree.querySelector('.nb-filter-empty');
            if (query && matches === 0) {
                if (!empty) {
                    empty = document.createElement('div');
                    empty.className = 'nb-empty-state nb-filter-empty';
                    tree.appendChild(empty);
                }
                empty.textContent = window.i18n
                    ? window.i18n.t('notebook.noFilesMatchFilter')
                    : 'No files match this filter';
            } else if (empty) {
                empty.remove();
            }
        }
    },

    refreshFileExplorer: async function(force = false) {
        const container = document.getElementById('file-tree');
        if (!container) return;

        const cached = !force ? this._getSidebarPanelCache('files') : null;
        if (cached && container.childElementCount > 0) return;
        
        if (!this.projectId) {
            container.innerHTML = '<div class="nb-empty-state">' + (window.i18n ? window.i18n.t('notebook.noProjectSelected') : 'No project selected') + '</div>';
            this._invalidateSidebarPanelCache('files');
            return;
        }

        container.ondragover = (e) => {
            e.preventDefault();
            container.classList.add('nb-drop-zone');
        };
        container.ondragleave = () => container.classList.remove('nb-drop-zone');
        container.ondrop = (e) => {
            e.preventDefault();
            container.classList.remove('nb-drop-zone');
            if (e.dataTransfer.files.length > 0) {
                this.handleFileDrop(e.dataTransfer.files);
            }
        };
        
        container.innerHTML = '<div class="nb-loading">' + (window.i18n ? window.i18n.t('notebook.loading') : 'Loading...') + '</div>';
        try {
            let notebooks = [];
            let files = [];

            const resourcesRes = await fetch(`/api/notebooks/project/${this.projectId}/resources`, {
                credentials: 'same-origin'
            });

            if (resourcesRes.ok) {
                const resourcesData = await resourcesRes.json();
                notebooks = Array.isArray(resourcesData.notebooks) ? resourcesData.notebooks : [];
                files = this._normalizeExplorerFiles(resourcesData.files);
            } else {
                const [filesRes, notebooksRes] = await Promise.all([
                    fetch(`/api/projects/${this.projectId}/files`, { credentials: 'same-origin' }),
                    fetch(`/api/notebooks/project/${this.projectId}`, { credentials: 'same-origin' })
                ]);

                const [filesData, notebooksData] = await Promise.all([
                    filesRes.ok ? filesRes.json() : [],
                    notebooksRes.ok ? notebooksRes.json() : { notebooks: [] }
                ]);

                files = this._normalizeExplorerFiles(Array.isArray(filesData) ? filesData : (filesData.files || []));
                notebooks = Array.isArray(notebooksData.notebooks) ? notebooksData.notebooks : [];
            }
            
            container.innerHTML = '';
            
            // Show file search when files are present
            const searchRow = document.getElementById('file-search-row');
            if (searchRow) {
                searchRow.style.display = (files.length > 0 || notebooks.length > 0) ? 'block' : 'none';
                const searchInput = document.getElementById('file-search-input');
                // Keep whatever the user typed — a refresh triggered by an upload,
                // a delete or a sort change should not silently widen the view
                // back to every file in the project.
                if (searchInput) searchInput.value = this._fileFilter || '';
            }
            
            // Section 1: Notebooks
            if (notebooks.length > 0) {
                const nbHeader = document.createElement('div');
                nbHeader.className = 'file-section-header';
                nbHeader.dataset.section = 'notebooks';
                nbHeader.innerHTML = `
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                    </svg>
                    <span>${window.i18n ? window.i18n.t('notebook.notebooks') : 'Notebooks'}</span>
                    <span class="nb-file-count">${notebooks.length}</span>
                `;
                container.appendChild(nbHeader);
                
                notebooks.forEach((nb, nbIdx) => {
                    const item = document.createElement('div');
                    item.className = 'file-item notebook-item';
                    item.dataset.notebookId = String(nb.id);
                    item.dataset.notebookIndex = String(nbIdx);
                    item.innerHTML = `
                        <span class="nb-select-checkbox" title="Select"></span>
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                        </svg>
                        <span>${this.escapeHtml(nb.name)}</span>
                        ${nb.active_sessions > 0 ? '<span class="nb-active-badge">' + (window.i18n ? window.i18n.t('notebook.active') : 'Active') + '</span>' : ''}
                    `;
                    item.onclick = (e) => {
                        if (e.target.closest('.nb-select-checkbox')) {
                            this._toggleNotebookSelection(nb, item, nbIdx);
                            return;
                        }
                        if (e.ctrlKey || e.metaKey) {
                            e.preventDefault();
                            this._toggleNotebookSelection(nb, item, nbIdx);
                            return;
                        }
                        if (e.shiftKey) {
                            e.preventDefault();
                            this._selectNotebookRange(nbIdx);
                            return;
                        }
                        if (this._selectedNotebooks.size > 0) {
                            if (!this._selectedNotebooks.has(String(nb.id))) {
                                this._clearNotebookSelection(false);
                            } else {
                                this._lastSelectedNotebookIndex = nbIdx;
                                this._updateNotebookSelectionUI();
                                return;
                            }
                        }
                        this.openNotebookAsTab(nb.id, nb.name);
                    };
                    item.oncontextmenu = (e) => {
                        e.preventDefault();
                        if (!this._selectedNotebooks.has(String(nb.id))) {
                            this._clearNotebookSelection(false);
                            this._toggleNotebookSelection(nb, item, nbIdx, true);
                        }
                        this.handleNotebookContextMenu(e, nb);
                    };
                    container.appendChild(item);
                });
            }

            const uploads = files.filter(f => f.type !== 'output' && f.type !== 'artifact');
            const outputs = files.filter(f => f.type === 'output' || f.type === 'artifact');

            // Track flat file index across sections for shift+click range selection.
            let fileIndex = -1;

            const renderFileSection = (title, sectionFiles, sectionKey) => {
                if (sectionFiles.length === 0) return;

                // Apply the active sort order (name / modified / size).
                const sortedFiles = sectionFiles.slice().sort((a, b) => this._compareFiles(a, b));

                const header = document.createElement('div');
                header.className = 'file-section-header';
                header.dataset.section = sectionKey;
                header.innerHTML = `
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                    </svg>
                    <span>${title}</span>
                    <span class="nb-file-count">${sectionFiles.length}</span>
                `;
                container.appendChild(header);

                sortedFiles.forEach((f) => {
                    fileIndex += 1;
                    const idx = fileIndex;
                    const item = document.createElement('div');
                    item.className = 'file-item';
                    const isDirectory = !!f.is_dir || f.type === 'directory';
                    const fname = this._resolveFileName(f);
                    const fileType = this._getFileType(fname);
                    const fileSize = this._formatFileSize(f.size || f.file_size);
                    const fileDate = f.modified_at || f.updated_at ? new Date(f.modified_at || f.updated_at).toLocaleDateString() : '';
                    const icon = this._getFileIcon(fileType, isDirectory);
                    const ext = fname.split('.').pop().toLowerCase();
                    const fileId = f.id;
                    const isManagedUpload = f.type === 'upload' && /^\d+$/.test(String(fileId || ''));

                    item.dataset.type = fileType;
                    item.dataset.fileIndex = String(idx);
                    if (fileId !== undefined && fileId !== null) item.dataset.fileId = String(fileId);
                    if (isManagedUpload) item.dataset.managedUpload = 'true';
                    // Store the file's path so folder collapse can match
                    // descendants by path prefix.
                    var itemPath = (f.source_path || f.path || fname || '').replace(/\/$/, '');
                    if (itemPath) item.dataset.filePath = itemPath;
                    if (isDirectory) {
                        item.classList.add('folder-item', 'expanded');
                        if (itemPath) item.dataset.folderPath = itemPath;
                    }
                    const chevron = isDirectory ? '<svg class="nb-folder-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><polyline points="6 9 12 15 18 9"/></svg>' : '<span style="width:12px;display:inline-block;"></span>';
                    item.innerHTML = `
                        <span class="nb-select-checkbox" title="Select"></span>
                        ${chevron}
                        ${icon}
                        <span class="file-name" title="${this.escapeHtml(fname)}${fileSize ? ' (' + fileSize + ')' : ''}">${this.escapeHtml(fname)}</span>
                        ${fileSize ? '<span class="file-size-tag">' + fileSize + '</span>' : ''}
                        ${fileDate ? '<span class="file-date-tag">' + fileDate + '</span>' : ''}
                        <span class="file-type-badge">${ext}</span>
                    `;

                    const checkbox = item.querySelector('.nb-select-checkbox');

                    var clickTimer = null;
                    const isNb = ext === 'nb';
                    const isWolfram = ['nb', 'wl', 'wls', 'm', 'mx', 'wxf', 'wdx'].indexOf(ext) !== -1;

                    const openFileAction = () => {
                        if (isWolfram) {
                            this.importWolframFileAsNotebook(f, fname, ext);
                        } else {
                            this.openFileInSplit(f);
                        }
                    };

                    const doubleClickAction = () => {
                        if (isWolfram) return;
                        const notebookPath = this._resolveFileContainerPath(f);
                        let snippet = '';
                        if (['csv', 'tsv'].includes(ext)) snippet = `# Load ${fname}\nimport pandas as pd\ndf = pd.read_csv('${notebookPath}')\ndf.head()`;
                        else if (['json'].includes(ext)) snippet = `# Load ${fname}\nimport json\nwith open('${notebookPath}') as f:\n    data = json.load(f)\nprint(data)`;
                        else snippet = `# Read ${fname}\nwith open('${notebookPath}') as f:\n    print(f.read())`;
                        this.addCell('code', snippet);
                    };

                    item.onclick = (e) => {
                        // Directories toggle expand/collapse instead of being
                        // dead ends. Collapsing hides following file items in
                        // the same section whose source_path is under this
                        // folder; expanding shows them again.
                        if (isDirectory) {
                            if (e.target.closest('.nb-select-checkbox')) {
                                this._toggleFileSelection(f, item, idx);
                                return;
                            }
                            this._toggleFolder(item);
                            return;
                        }
                        // Checkbox click toggles selection only.
                        if (e.target.closest('.nb-select-checkbox')) {
                            this._toggleFileSelection(f, item, idx);
                            return;
                        }
                        // Modifier click toggles selection.
                        if (e.ctrlKey || e.metaKey) {
                            e.preventDefault();
                            this._toggleFileSelection(f, item, idx);
                            return;
                        }
                        if (e.shiftKey) {
                            e.preventDefault();
                            this._selectFileRange(idx);
                            return;
                        }
                        if (this._selectedFiles.size > 0) {
                            // If other items are selected, a normal click clears
                            // selection and opens the clicked file (unless the
                            // clicked item is itself selected).
                            if (!this._selectedFiles.has(String(fileId))) {
                                this._clearFileSelection(false);
                            } else {
                                // Click on a selected item when multiple are
                                // selected: just update last anchor.
                                this._lastSelectedFileIndex = idx;
                                this._updateFileSelectionUI();
                                return;
                            }
                        }
                        if (clickTimer) {
                            clearTimeout(clickTimer);
                            clickTimer = null;
                            return;
                        }
                        // Reduced from 250ms to 180ms: the wait exists only
                        // to disambiguate single (open in split) from double
                        // (insert code cell) clicks. 180ms is below most
                        // browsers' dblclick threshold (~300ms) while feeling
                        // noticeably snappier than 250ms.
                        clickTimer = setTimeout(() => {
                            clickTimer = null;
                            openFileAction();
                        }, 180);
                    };
                    item.ondblclick = (e) => {
                        if (isDirectory) { this._toggleFolder(item); return; }
                        if (clickTimer) {
                            clearTimeout(clickTimer);
                            clickTimer = null;
                        }
                        if (this._selectedFiles.size > 1) return;
                        this._clearFileSelection(false);
                        doubleClickAction();
                    };

                    item.oncontextmenu = (e) => {
                        e.preventDefault();
                        // If right-clicked item is not part of the current
                        // selection, make it the sole selection.
                        if (!this._selectedFiles.has(String(fileId))) {
                            this._clearFileSelection(false);
                            this._toggleFileSelection(f, item, idx, true);
                        }
                        this.handleFileContextMenu(e, f);
                    };

                    container.appendChild(item);
                });
            };

            renderFileSection(window.i18n ? window.i18n.t('notebook.projectFiles') : 'Project Files', uploads, 'files');
            renderFileSection(window.i18n ? window.i18n.t('notebook.jobOutputs') : 'Job Outputs', outputs, 'data');

            // Section 3: files the notebook itself created in /workspace
            // (compiled-cell artifacts, saved plots, exported data, ...).
            await this._renderWorkspaceSection(container);

            if (notebooks.length === 0 && files.length === 0 && !this._lastWorkspaceFileCount) {
                container.innerHTML = '<div class="nb-empty-state">' + (window.i18n ? window.i18n.t('notebook.noNotebooksOrFiles') : 'No notebooks or files in this project') + '</div>';
            }
            this._ensureFileSelectionToolbar();
            this._updateFileSelectionUI();
            this._updateNotebookSelectionUI();
            // Re-apply the filter the user had typed before this re-render.
            if (this._fileFilter) this._filterFileTree(this._fileFilter);
            this._setSidebarPanelCache('files', { notebooks: notebooks, files: files });
        } catch (e) {
            console.error(e);
            container.innerHTML = '<div class="nb-empty-state error">' + (window.i18n ? window.i18n.t('notebook.failedToLoadContent') : 'Failed to load content') + '</div>';
            this._invalidateSidebarPanelCache('files');
        }
    },

    // ---- Workspace files (notebook's own <name>_workspace folder) ----
    //
    // The workspace is a per-notebook folder on disk. It can contain nested
    // directories created either from notebook code (os.makedirs) or via the
    // "New Folder" action. This section renders a mini file-explorer with
    // breadcrumb navigation, multi-select checkboxes, and bulk actions
    // (delete / move / copy / download-as-zip).

    _lastWorkspaceFileCount: 0,
    _wsCurrentDir: '',            // relative path of the folder currently viewed
    _selectedWorkspacePaths: new Set(),  // full relative paths (from ws root)
    _wsRefreshTimer: null,
    _wsUploadInput: null,
    _wsLoadedNotebookId: null,    // last notebook id rendered (to reset nav state on switch)

    _renderWorkspaceSection: async function(container) {
        var old = document.getElementById('nb-ws-files-section');
        if (old) old.remove();
        if (!this.notebook || !this.notebook.id) return;

        // Reset navigation + selection when switching to a different notebook
        // so a stale _wsCurrentDir from the previous notebook doesn't leak.
        var nbId = String(this.notebook.id);
        if (this._wsLoadedNotebookId !== nbId) {
            this._wsLoadedNotebookId = nbId;
            this._wsCurrentDir = '';
            this._selectedWorkspacePaths.clear();
        }

        var dir = this._wsCurrentDir || '';
        var files = [];
        var workspaceName = '';
        try {
            var url = '/api/notebooks/' + this.notebook.id + '/files?path=' + encodeURIComponent(dir);
            var res = await fetch(url, { credentials: 'same-origin' });
            if (res.ok) {
                var data = await res.json();
                files = data.files || [];
                workspaceName = data.workspace_name || '';
            }
        } catch (e) { /* workspace listing is best-effort */ }

        // Only count files (not dirs) for the header badge, matching old behaviour.
        this._lastWorkspaceFileCount = files.filter(function(f) { return !f.is_dir; }).length;
        if (files.length === 0 && !dir) return;

        var section = document.createElement('div');
        section.id = 'nb-ws-files-section';
        section.className = 'nb-ws-section';

        var header = document.createElement('div');
        header.className = 'file-section-header';
        header.dataset.section = 'workspace';
        header.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
            '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>' +
            '<span>' + (window.i18n ? window.i18n.t('notebook.workspaceFiles') : 'Workspace') + '</span>' +
            '<span class="nb-file-count">' + this._lastWorkspaceFileCount + '</span>';
        // Collapse/expand the workspace section by clicking the header.
        header.addEventListener('click', () => {
            section.classList.toggle('collapsed');
        });
        section.appendChild(header);

        // Breadcrumb: Workspace / dir1 / dir2  (segments clickable to navigate up)
        var crumbs = document.createElement('div');
        crumbs.className = 'nb-ws-breadcrumb';
        var rootCrumb = document.createElement('span');
        rootCrumb.className = 'nb-ws-crumb';
        rootCrumb.textContent = (window.i18n ? window.i18n.t('notebook.workspaceFiles') : 'Workspace');
        rootCrumb.title = workspaceName ? ('~/' + workspaceName) : '';
        rootCrumb.addEventListener('click', () => { this._wsNavigate(''); });
        crumbs.appendChild(rootCrumb);
        if (dir) {
            var parts = dir.split('/');
            var acc = '';
            parts.forEach((part, idx) => {
                acc = acc ? acc + '/' + part : part;
                var sep = document.createElement('span');
                sep.className = 'nb-ws-crumb-sep';
                sep.textContent = '/';
                crumbs.appendChild(sep);
                var crumb = document.createElement('span');
                crumb.className = 'nb-ws-crumb';
                crumb.textContent = part;
                const target = acc;
                if (idx < parts.length - 1) {
                    crumb.addEventListener('click', () => { this._wsNavigate(target); });
                } else {
                    crumb.classList.add('current');
                }
                crumbs.appendChild(crumb);
            });
        }
        section.appendChild(crumbs);

        // Toolbar: New Folder + Upload; bulk actions appear when items selected.
        var toolbar = document.createElement('div');
        toolbar.className = 'nb-ws-toolbar';
        var newFolderBtn = document.createElement('button');
        newFolderBtn.className = 'nb-btn mini secondary';
        newFolderBtn.type = 'button';
        newFolderBtn.textContent = window.i18n ? window.i18n.t('notebook.newFolder') : 'New Folder';
        newFolderBtn.addEventListener('click', () => this._createWorkspaceFolder());
        toolbar.appendChild(newFolderBtn);

        var uploadBtn = document.createElement('button');
        uploadBtn.className = 'nb-btn mini secondary';
        uploadBtn.type = 'button';
        uploadBtn.textContent = window.i18n ? window.i18n.t('notebook.uploadFile') : 'Upload';
        uploadBtn.addEventListener('click', () => this._pickWorkspaceUpload());
        toolbar.appendChild(uploadBtn);

        var bulkBar = document.createElement('div');
        bulkBar.className = 'nb-ws-bulk-bar';
        bulkBar.id = 'nb-ws-bulk-bar';
        bulkBar.style.display = 'none';
        var countSpan = document.createElement('span');
        countSpan.className = 'nb-ws-bulk-count';
        bulkBar.appendChild(countSpan);
        var actions = [
            { label: window.i18n ? window.i18n.t('notebook.delete') : 'Delete', action: 'delete', danger: true },
            { label: NbT('move', 'Move'), action: 'move' },
            { label: window.i18n ? window.i18n.t('notebook.copy') : 'Copy', action: 'copy' },
            { label: window.i18n ? window.i18n.t('notebook.download') : 'Download', action: 'download' },
            { label: NbT('clearSelection', 'Clear'), action: 'clear' }
        ];
        actions.forEach((a) => {
            var b = document.createElement('button');
            b.className = 'nb-btn mini ' + (a.danger ? 'danger' : 'secondary');
            b.type = 'button';
            b.textContent = a.label;
            b.dataset.action = a.action;
            b.addEventListener('click', () => this._wsBulkAction(a.action));
            bulkBar.appendChild(b);
        });
        toolbar.appendChild(bulkBar);
        section.appendChild(toolbar);

        // File/dir list
        var list = document.createElement('div');
        list.className = 'nb-ws-list';
        if (files.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'nb-ws-empty';
            empty.textContent = NbT('emptyFolder', 'Empty folder');
            list.appendChild(empty);
        } else {
            var self = this;
            files.forEach(function(f) {
                list.appendChild(self._buildWorkspaceRow(f));
            });
        }
        section.appendChild(list);

        container.appendChild(section);
        this._updateWsBulkBar();
    },

    _buildWorkspaceRow: function(f) {
        var self = this;
        var row = document.createElement('div');
        row.className = 'file-item ws-file-item' + (f.is_dir ? ' folder-item' : '');
        row.dataset.path = f.path;
        row.title = f.path + ' · ' + this._formatFileSize(f.size || 0) +
            (f.modified ? ' · ' + f.modified.replace('T', ' ').slice(0, 19) : '');

        // Checkbox (stops propagation so it doesn't trigger navigation/open)
        var cb = document.createElement('span');
        cb.className = 'nb-select-checkbox';
        cb.setAttribute('role', 'checkbox');
        cb.addEventListener('click', function(e) {
            e.stopPropagation();
            self._toggleWsSelection(f.path, cb);
        });
        row.appendChild(cb);

        var icon = document.createElement('span');
        icon.className = 'nb-ws-icon';
        if (f.is_dir) {
            icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
        } else {
            icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
        }
        row.appendChild(icon);

        var name = document.createElement('span');
        name.className = 'file-name';
        name.textContent = f.name;
        row.appendChild(name);

        if (!f.is_dir) {
            var size = document.createElement('span');
            size.className = 'nb-ws-file-size';
            size.textContent = this._formatFileSize(f.size || 0);
            row.appendChild(size);
        }

        // Click row: navigate into folder, or open file.
        row.addEventListener('click', function() {
            if (f.is_dir) {
                self._wsNavigate(f.path);
            } else {
                self.openWorkspaceFile(f);
            }
        });

        // Right-click context menu for single items.
        row.addEventListener('contextmenu', function(e) {
            e.preventDefault();
            self._showWsContextMenu(e, f);
        });

        return row;
    },

    _wsNavigate: function(dir) {
        this._wsCurrentDir = dir || '';
        var container = document.getElementById('file-tree');
        if (!container) return;
        this._renderWorkspaceSection(container);
    },

    _toggleWsSelection: function(path, cbEl) {
        if (this._selectedWorkspacePaths.has(path)) {
            this._selectedWorkspacePaths.delete(path);
        } else {
            this._selectedWorkspacePaths.add(path);
        }
        // Refresh checkbox visuals across all rows matching this path.
        document.querySelectorAll('#nb-ws-files-section .ws-file-item[data-path="' + CSS.escape(path) + '"] .nb-select-checkbox').forEach(function(cb) {
            cb.classList.toggle('checked', this._selectedWorkspacePaths.has(path));
        }.bind(this));
        this._updateWsBulkBar();
    },

    _clearWsSelection: function() {
        this._selectedWorkspacePaths.clear();
        document.querySelectorAll('#nb-ws-files-section .nb-select-checkbox.checked').forEach(function(cb) {
            cb.classList.remove('checked');
        });
        this._updateWsBulkBar();
    },

    _updateWsBulkBar: function() {
        var bar = document.getElementById('nb-ws-bulk-bar');
        if (!bar) return;
        var count = this._selectedWorkspacePaths.size;
        bar.style.display = count > 0 ? 'flex' : 'none';
        var countSpan = bar.querySelector('.nb-ws-bulk-count');
        if (countSpan) {
            countSpan.textContent = count + (count === 1 ? ' item selected' : ' items selected');
        }
    },

    _wsBulkAction: function(action) {
        if (action === 'clear') { this._clearWsSelection(); return; }
        if (action === 'delete') { this._wsBulkDelete(); return; }
        if (action === 'download') { this._wsBulkDownload(); return; }
        if (action === 'move') { this._wsPromptDestAndDo('move'); return; }
        if (action === 'copy') { this._wsPromptDestAndDo('copy'); return; }
    },

    _wsSelectedPaths: function() {
        return Array.from(this._selectedWorkspacePaths);
    },

    _wsBulkDelete: async function() {
        var paths = this._wsSelectedPaths();
        if (paths.length === 0) return;
        var msg = paths.length === 1
            ? (window.i18n ? window.i18n.t('notebook.deleteFileConfirm') : 'Delete this item?')
            : (paths.length + ' items will be deleted. Continue?');
        if (!confirm(msg)) return;
        try {
            var res = await fetch('/api/notebooks/' + this.notebook.id + '/files/bulk-delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this._getCsrfToken() },
                body: JSON.stringify({ paths: paths })
            });
            var data = await res.json();
            if (res.ok) {
                this.showToast('Deleted ' + (data.deleted_count || 0) + ' item(s)');
                this._clearWsSelection();
                this._refreshWorkspaceOnly();
            } else {
                throw new Error(data.error || 'Delete failed');
            }
        } catch (e) {
            alert('Delete failed: ' + e.message);
        }
    },

    _wsPromptDestAndDo: function(kind) {
        var paths = this._wsSelectedPaths();
        if (paths.length === 0) return;
        var self = this;
        this._showInputModal({
            title: kind === 'move' ? 'Move to folder' : 'Copy to folder',
            label: 'Destination folder (relative to workspace root, blank = root)',
            value: this._wsCurrentDir || '',
            confirm: kind === 'move' ? 'Move' : 'Copy',
            onConfirm: async function(dest) {
                try {
                    var res = await fetch('/api/notebooks/' + self.notebook.id + '/files/bulk-' + kind, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': self._getCsrfToken() },
                        body: JSON.stringify({ paths: paths, dest_path: dest || '' })
                    });
                    var data = await res.json();
                    if (res.ok) {
                        var n = kind === 'move' ? (data.moved_count || 0) : (data.copied_count || 0);
                        self.showToast((kind === 'move' ? 'Moved ' : 'Copied ') + n + ' item(s)');
                        if (kind === 'move') self._clearWsSelection();
                        self._refreshWorkspaceOnly();
                    } else {
                        throw new Error(data.error || 'Operation failed');
                    }
                } catch (e) {
                    alert((kind === 'move' ? 'Move' : 'Copy') + ' failed: ' + e.message);
                }
            }
        });
    },

    _wsBulkDownload: async function() {
        var paths = this._wsSelectedPaths();
        if (paths.length === 0) return;
        try {
            var res = await fetch('/api/notebooks/' + this.notebook.id + '/files/bulk-download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this._getCsrfToken() },
                body: JSON.stringify({ paths: paths })
            });
            if (!res.ok) {
                var err = await res.json().catch(() => ({}));
                throw new Error(err.error || 'Download failed');
            }
            var blob = await res.blob();
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            var cd = res.headers.get('Content-Disposition') || '';
            var m = cd.match(/filename="?([^"]+)"?/i);
            a.download = m ? m[1] : 'workspace.zip';
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            this._clearWsSelection();
        } catch (e) {
            alert('Download failed: ' + e.message);
        }
    },

    _createWorkspaceFolder: function() {
        if (!this.notebook || !this.notebook.id) return;
        var self = this;
        this._showInputModal({
            title: window.i18n ? window.i18n.t('notebook.newFolder') : 'New Folder',
            label: NbT('folderName', 'Folder name'),
            value: '',
            confirm: window.i18n ? window.i18n.t('common.create') : 'Create',
            onConfirm: async function(name) {
                if (!name) return;
                var rel = self._wsCurrentDir ? (self._wsCurrentDir + '/' + name) : name;
                try {
                    var res = await fetch('/api/notebooks/' + self.notebook.id + '/files/directory', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': self._getCsrfToken() },
                        body: JSON.stringify({ path: rel })
                    });
                    var data = await res.json();
                    if (res.ok && (data.created || data.exists)) {
                        self.showToast(NbT('folderCreated', 'Folder created'));
                        self._refreshWorkspaceOnly();
                    } else {
                        throw new Error(data.error || 'Create folder failed');
                    }
                } catch (e) {
                    alert((NbT('folderCreateFailed', 'Create folder failed')) + ': ' + e.message);
                }
            }
        });
    },

    _pickWorkspaceUpload: function() {
        if (!this.notebook || !this.notebook.id) return;
        var self = this;
        if (!this._wsUploadInput) {
            var input = document.createElement('input');
            input.type = 'file';
            input.multiple = true;
            input.style.position = 'fixed';
            input.style.left = '-9999px';
            document.body.appendChild(input);
            input.addEventListener('change', function() {
                if (input.files && input.files.length > 0) {
                    self._uploadWorkspaceFiles(input.files);
                }
                input.value = '';
            });
            this._wsUploadInput = input;
        }
        this._wsUploadInput.click();
    },

    _uploadWorkspaceFiles: async function(files) {
        var self = this;
        var csrf = this._getCsrfToken();
        var failed = [];
        for (var i = 0; i < files.length; i++) {
            var fd = new FormData();
            fd.append('file', files[i]);
            fd.append('path', this._wsCurrentDir || '');
            try {
                var res = await fetch('/api/notebooks/' + this.notebook.id + '/files', {
                    method: 'POST',
                    headers: { 'X-CSRF-Token': csrf },
                    body: fd
                });
                if (!res.ok) {
                    var d = await res.json().catch(() => ({}));
                    throw new Error(d.error || 'HTTP ' + res.status);
                }
            } catch (e) {
                failed.push(files[i].name + ': ' + e.message);
            }
        }
        if (failed.length) {
            alert('Some uploads failed:\n' + failed.join('\n'));
        } else {
            this.showToast('Uploaded ' + files.length + ' file(s)');
        }
        this._refreshWorkspaceOnly();
    },

    _showWsContextMenu: function(e, f) {
        let menu = document.getElementById('nb-context-menu');
        if (!menu) {
            menu = document.createElement('div');
            menu.id = 'nb-context-menu';
            menu.className = 'nb-context-menu';
            document.body.appendChild(menu);
            document.addEventListener('mousedown', function(ev) {
                if (ev.button !== 0) return;
                if (!menu.contains(ev.target)) menu.classList.remove('active');
            });
        }
        menu.textContent = '';
        var self = this;
        var add = function(text, onClick, danger) {
            var item = document.createElement('div');
            item.className = 'nb-context-item' + (danger ? ' danger' : '');
            item.textContent = text;
            item.addEventListener('click', function() { onClick(); menu.classList.remove('active'); });
            menu.appendChild(item);
        };
        var sep = function() {
            var s = document.createElement('div');
            s.className = 'nb-context-sep';
            menu.appendChild(s);
        };

        // If multiple items are selected, treat the menu as a bulk menu.
        if (this._selectedWorkspacePaths.size > 1) {
            add('Delete ' + this._selectedWorkspacePaths.size + ' selected', function() { self._wsBulkDelete(); }, true);
            sep();
            add('Download Selected', function() { self._wsBulkDownload(); });
            add('Clear Selection', function() { self._clearWsSelection(); });
        } else {
            // Ensure the right-clicked item is the only selection.
            this._selectedWorkspacePaths.clear();
            this._selectedWorkspacePaths.add(f.path);
            this._updateWsBulkBar();
            document.querySelectorAll('#nb-ws-files-section .nb-select-checkbox.checked').forEach(function(cb) {
                cb.classList.remove('checked');
            });
            var cb = document.querySelector('#nb-ws-files-section .ws-file-item[data-path="' + CSS.escape(f.path) + '"] .nb-select-checkbox');
            if (cb) cb.classList.add('checked');

            add('Insert Path', function() {
                self.insertPath(f.name, '/workspace/' + f.path);
            });
            add('Copy Path', function() {
                navigator.clipboard.writeText('/workspace/' + f.path).then(function() {
                    self.showToast('Copied: /workspace/' + f.path);
                });
            });
            sep();
            add('Rename', function() { self._renameWorkspaceFile(f); });
            add('Download', function() { self._wsDownloadSingle(f); });
            add('Delete', function() { self._wsDeleteSingle(f); }, true);
        }

        menu.style.left = e.pageX + 'px';
        menu.style.top = e.pageY + 'px';
        menu.classList.add('active');
    },

    _renameWorkspaceFile: function(f) {
        var self = this;
        this._showInputModal({
            title: NbT('renameFile', 'Rename'),
            label: NbT('newName', 'New name'),
            value: f.name,
            confirm: window.i18n ? window.i18n.t('notebook.rename') : 'Rename',
            onConfirm: async function(newName) {
                if (!newName || newName === f.name) return;
                try {
                    var res = await fetch('/api/notebooks/' + self.notebook.id + '/files/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': self._getCsrfToken() },
                        body: JSON.stringify({ path: f.path, new_name: newName })
                    });
                    var data = await res.json();
                    if (res.ok) {
                        self._clearWsSelection();
                        self._refreshWorkspaceOnly();
                    } else {
                        throw new Error(data.error || 'Rename failed');
                    }
                } catch (e) {
                    alert('Rename failed: ' + e.message);
                }
            }
        });
    },

    _wsDownloadSingle: function(f) {
        // Reuse the bulk-download endpoint with a single path (yields a zip),
        // or the direct content download for files.
        if (f.is_dir) {
            this._selectedWorkspacePaths.clear();
            this._selectedWorkspacePaths.add(f.path);
            this._wsBulkDownload();
            return;
        }
        window.open('/api/notebooks/' + this.notebook.id + '/files/content?path=' + encodeURIComponent(f.path) + '&download=1', '_blank');
    },

    _wsDeleteSingle: function(f) {
        var self = this;
        if (!confirm('Delete "' + f.name + '"?')) return;
        fetch('/api/notebooks/' + this.notebook.id + '/files?path=' + encodeURIComponent(f.path), {
            method: 'DELETE',
            headers: { 'X-CSRF-Token': this._getCsrfToken() }
        }).then(function(res) {
            if (res.ok) {
                self._clearWsSelection();
                self._refreshWorkspaceOnly();
            } else {
                return res.json().then(function(d) { throw new Error(d.error || 'Delete failed'); });
            }
        }).catch(function(e) { alert('Delete failed: ' + e.message); });
    },

    _refreshWorkspaceOnly: function() {
        var container = document.getElementById('file-tree');
        if (!container) return;
        this._renderWorkspaceSection(container);
    },

    openWorkspaceFile: async function(f) {
        if (!this.notebook || !this.notebook.id) return;
        var base = '/api/notebooks/' + this.notebook.id + '/files/content?path=' + encodeURIComponent(f.path);
        try {
            var res = await fetch(base, { credentials: 'same-origin' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            var data = await res.json();
            if (data.binary) {
                window.open(base + '&download=1', '_blank');
                return;
            }
            var content = data.content || '';
            if (data.truncated) content += '\n\n… (preview truncated — use download for the full file)';
            this.openFileAsText({ id: 'ws:' + f.path, name: f.name, path: f.path }, content);
        } catch (e) {
            this.showToast((window.i18n ? window.i18n.t('notebook.failedToLoadContent') : 'Failed to load content') + ': ' + f.name, 3000);
        }
    },

    // Debounced refresh after cell executions so files written by notebook
    // code show up without a manual refresh. Only touches the workspace
    // section (a full explorer refresh would wipe selection state).
    _scheduleWorkspaceRefresh: function() {
        var self = this;
        if (this._wsRefreshTimer) clearTimeout(this._wsRefreshTimer);
        this._wsRefreshTimer = setTimeout(function() {
            self._wsRefreshTimer = null;
            var container = document.getElementById('file-tree');
            if (!container || !container.offsetParent) return;  // files panel hidden
            self._renderWorkspaceSection(container);
        }, 1200);
    },

    _openFileUploadPicker: function() {
        if (!this.projectId) {
            this.showToast && this.showToast(NbT('noProjectSelectedUpload', 'Select a project first'), 3000);
            return;
        }
        // Reuse / lazily create a hidden file input so back-to-back uploads work.
        let input = document.getElementById('nb-file-upload-input');
        if (!input) {
            input = document.createElement('input');
            input.type = 'file';
            input.id = 'nb-file-upload-input';
            input.multiple = true;
            // Explicit attributes prevent iOS Safari from trying to take a photo.
            input.accept = '*/*';
            input.style.position = 'fixed';
            input.style.left = '-9999px';
            input.style.top = '0';
            document.body.appendChild(input);
            input.addEventListener('change', () => {
                if (input.files && input.files.length > 0) {
                    this.handleFileDrop(input.files);
                }
                // Reset so picking the same file twice still triggers 'change'.
                input.value = '';
            });
        }
        input.click();
    },

    handleFileDrop: async function(files) {
        if (!this.projectId) return;
        // Resolve CSRF token once for the bulk request (the endpoint is @csrf_protect).
        // Prefer PHONOS_CONFIG.csrfToken (set by notebook.html); fall back to the
        // <meta name="csrf-token"> tag if the SPA global hasn't been initialised yet.
        let csrfToken = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
        if (!csrfToken) {
            const meta = document.querySelector('meta[name="csrf-token"]');
            if (meta) csrfToken = meta.getAttribute('content') || '';
        }

        // If CSRF token is still empty, the session may have expired.
        // Reload the page to re-establish the session and get a new token.
        if (!csrfToken) {
            const reloadMsg = window.i18n ? window.i18n.t('notebook.csrfMissing') : 'Session expired. Please reload the page and try again.';
            if (confirm(reloadMsg + '\n\nReload now?')) {
                window.location.reload();
            }
            return;
        }

        try {
            // Reconstruct FormData for bulk upload
            const bulkData = new FormData();
            for (let i = 0; i < files.length; i++) {
                bulkData.append('files', files[i]);
            }

            const res = await fetch(`/api/projects/${this.projectId}/files/bulk`, {
                method: 'POST',
                headers: {
                    'X-CSRF-Token': csrfToken
                },
                body: bulkData
            });

            if (res.ok) {
                this.refreshFileExplorer(true);
            } else {
                let errMsg = (window.i18n ? window.i18n.t('notebook.unknownError') : 'Unknown error');
                try {
                    const err = await res.json();
                    errMsg = err.message || errMsg;
                } catch (_) {}
                if (res.status === 403 && csrfToken === '') {
                    errMsg = (window.i18n ? window.i18n.t('notebook.csrfMissing') : 'Session expired. Please reload the page and try again.');
                }
                alert((window.i18n ? window.i18n.t('notebook.uploadFailed') : 'Upload failed: ') + errMsg);
            }
        } catch (e) {
            console.error(e);
            alert(window.i18n ? window.i18n.t('notebook.uploadError') : 'Upload error');
        }
    },

    handleFileContextMenu: function(e, file) {
        // Create or reuse menu
        let menu = document.getElementById('nb-context-menu');
        if (!menu) {
            menu = document.createElement('div');
            menu.id = 'nb-context-menu';
            menu.className = 'nb-context-menu';
            document.body.appendChild(menu);

            document.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                if (!menu.contains(e.target)) menu.classList.remove('active');
            });
        }

        const filename = this._resolveFileName(file);
        const fullPath = this._resolveFileContainerPath(file);
        const fileId = file.id;
        const fileType = file.type || 'upload';
        const isManagedUpload = fileType === 'upload' && /^\d+$/.test(String(fileId || ''));
        const selectedCount = this._selectedFiles.size;
        const isMulti = selectedCount > 1;

        // Clear menu and build with safe DOM methods
        menu.textContent = '';

        const createMenuItem = (text, onClick, isDanger = false) => {
            const item = document.createElement('div');
            item.className = 'nb-context-item' + (isDanger ? ' danger' : '');
            item.textContent = text;
            item.addEventListener('click', () => { onClick(); menu.classList.remove('active'); });
            return item;
        };

        const createSeparator = () => {
            const sep = document.createElement('div');
            sep.className = 'nb-context-sep';
            return sep;
        };

        if (isMulti) {
            menu.appendChild(createMenuItem(window.i18n ? NbT('deleteNSelected', 'Delete {n} selected').replace('{n}', selectedCount) : `Delete ${selectedCount} selected`, () => this._bulkDeleteSelectedFiles(), true));
            menu.appendChild(createSeparator());
            menu.appendChild(createMenuItem(NbT('downloadSelected', 'Download Selected'), () => this._downloadSelectedFiles()));
            menu.appendChild(createMenuItem(NbT('clearSelection', 'Clear Selection'), () => this._clearFileSelection(true)));
        } else {
            menu.appendChild(createMenuItem(window.i18n ? window.i18n.t('notebook.insertPath') : 'Insert Path', () => this.insertPath(filename, fullPath)));
            menu.appendChild(createMenuItem(window.i18n ? window.i18n.t('notebook.copyPath') : 'Copy Path', () => this.copyPath(filename, fullPath)));

            if (isManagedUpload) {
                menu.appendChild(createSeparator());
                menu.appendChild(createMenuItem(window.i18n ? window.i18n.t('notebook.rename') : 'Rename', () => this._showRenameModal('file', file)));
                menu.appendChild(createMenuItem(window.i18n ? window.i18n.t('notebook.copy') : 'Copy', () => this._copySelectedFile()));
                menu.appendChild(createMenuItem(window.i18n ? window.i18n.t('notebook.download') : 'Download', () => this.downloadFile(fileId)));
                menu.appendChild(createMenuItem(window.i18n ? window.i18n.t('notebook.delete') : 'Delete', () => this.deleteFile(fileId), true));
            } else if (file.job_id) {
                menu.appendChild(createSeparator());
                menu.appendChild(createMenuItem(window.i18n ? window.i18n.t('notebook.openOutputPage') : 'Open Output Page', () => {
                    window.open(`/view_output/${file.job_id}`, '_blank');
                }));
            }
        }

        menu.style.left = `${e.pageX}px`;
        menu.style.top = `${e.pageY}px`;
        menu.classList.add('active');
    },

    insertPath: function(filename, fullPath) {
        // Insert the notebook-compatible path at cursor in active cell
        // Convert to absolute path that works inside the container
        const relativePath = fullPath || filename;
        const notebookPath = (typeof relativePath === 'string' && relativePath.startsWith('/project/'))
            ? relativePath
            : this.getNotebookPath(relativePath);

        if (this.activeCellId && this.notebook) {
            const cell = this.notebook.getCell(this.activeCellId);
            if (cell && cell.cm) {
                cell.cm.replaceSelection(`"${notebookPath}"`);
                cell.cm.focus();
            }
        }
    },

    copyPath: function(filename, fullPath) {
        // Copy the notebook-compatible path to clipboard
        const relativePath = fullPath || filename;
        const notebookPath = (typeof relativePath === 'string' && relativePath.startsWith('/project/'))
            ? relativePath
            : this.getNotebookPath(relativePath);
        navigator.clipboard.writeText(notebookPath).then(() => {
            this.showToast((window.i18n ? window.i18n.t('notebook.copiedPrefix') : 'Copied: ') + notebookPath);
        }).catch(err => {
            console.error('Failed to copy path:', err);
            // Fallback: show alert
            alert((window.i18n ? window.i18n.t('notebook.pathPrefix') : 'Path: ') + notebookPath);
        });
    },

    getNotebookPath: function(relativePath) {
        // Convert a relative path to notebook-compatible absolute path
        // Files in project are mounted at /project in the container
        // Jobs are at /project/jobs/{job_id}/
        if (!relativePath) return '/project/';
        const rawPath = String(relativePath);

        if (rawPath.startsWith('/project/')) {
            return rawPath;
        }

        const normalized = rawPath.replace(/\\/g, '/');
        if (normalized.includes('/uploads/')) {
            const uploadSuffix = normalized.split('/uploads/', 2)[1].replace(/^\/+/, '');
            return '/project/uploads/' + uploadSuffix;
        }
        if (normalized.includes('/jobs/')) {
            const jobsSuffix = normalized.split('/jobs/', 2)[1].replace(/^\/+/, '');
            return '/project/jobs/' + jobsSuffix;
        }

        // Remove leading slash if present
        const cleanPath = rawPath.startsWith('/') ? rawPath.slice(1) : rawPath;

        return '/project/' + cleanPath;
    },

    // ========================================================================
    // COMMAND PALETTE
    // ========================================================================

    _commandPaletteCommands: null,

    _getCommands: function() {
        if (this._commandPaletteCommands) return this._commandPaletteCommands;
        var self = this;
        this._commandPaletteCommands = [
            // Cell operations
            { label: window.i18n ? window.i18n.t('notebook.cmdAddCodeCell') : 'Add Code Cell', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', shortcut: 'Ctrl+B', icon: 'plus', action: function() { self.addCell('code', '', self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdAddMarkdownCell') : 'Add Markdown Cell', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', shortcut: 'Ctrl+M', icon: 'text', action: function() { self.addCell('markdown', '', self.activeCellId); } },
            { label: 'Add Quantum Circuit Cell', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', icon: 'plus', action: function() { self.addQuantumCell(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdDuplicateCell') : 'Duplicate Cell', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', shortcut: 'Ctrl+D', icon: 'copy', action: function() { if (self.activeCellId) self.duplicateCell(self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdDeleteCell') : 'Delete Cell', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', shortcut: 'Ctrl+Shift+D', icon: 'trash', action: function() { if (self.activeCellId) self.deleteCell(self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdMoveCellUp') : 'Move Cell Up', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', shortcut: 'Alt+Up', icon: 'up', action: function() { if (self.activeCellId) self.moveCell(self.activeCellId, -1); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdMoveCellDown') : 'Move Cell Down', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', shortcut: 'Alt+Down', icon: 'down', action: function() { if (self.activeCellId) self.moveCell(self.activeCellId, 1); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdCollapseExpand') : 'Collapse/Expand Cell', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', shortcut: 'Alt+C', icon: 'collapse', action: function() { if (self.activeCellId) self.toggleCellCollapse(self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdConvertCellType') : 'Convert Cell Type', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', icon: 'convert', action: function() { if (self.activeCellId) self.toggleCellType(self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdClearCellOutput') : 'Clear Cell Output', category: window.i18n ? window.i18n.t('notebook.catCell') : 'Cell', icon: 'clear', action: function() { self.clearOutput(); } },

            // Execution
            { label: window.i18n ? window.i18n.t('notebook.cmdRunCell') : 'Run Cell', category: window.i18n ? window.i18n.t('notebook.catRun') : 'Run', shortcut: 'Ctrl+Enter', icon: 'play', action: function() { if (self.activeCellId) self.runCell(self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdRunCellAndAdvance') : 'Run Cell and Advance', category: window.i18n ? window.i18n.t('notebook.catRun') : 'Run', shortcut: 'Shift+Enter', icon: 'play', action: function() { if (self.activeCellId) { self.runCell(self.activeCellId); self.selectNextCell(self.activeCellId) || self.addCell('code', '', self.activeCellId); } } },
            { label: window.i18n ? window.i18n.t('notebook.cmdRunAllCells') : 'Run All Cells', category: window.i18n ? window.i18n.t('notebook.catRun') : 'Run', icon: 'play-all', action: function() { self.runAllCells(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdRunAllAbove') : 'Run All Above', category: window.i18n ? window.i18n.t('notebook.catRun') : 'Run', icon: 'play', action: function() { if (self.activeCellId) self.runAllAbove(self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdRunAllBelow') : 'Run All Below', category: window.i18n ? window.i18n.t('notebook.catRun') : 'Run', icon: 'play', action: function() { if (self.activeCellId) self.runAllBelow(self.activeCellId); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdClearAllOutputs') : 'Clear All Outputs', category: window.i18n ? window.i18n.t('notebook.catRun') : 'Run', icon: 'clear', action: function() { self.clearAllOutputs(); } },

            // Kernel
            { label: window.i18n ? window.i18n.t('notebook.cmdConnectKernel') : 'Connect Kernel', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'power', action: function() { self.connectKernel(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdDisconnectKernel') : 'Disconnect Kernel', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'power', action: function() { self.disconnectKernel(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdRestartKernel') : 'Restart Kernel', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'restart', action: function() { self.restartKernel(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdInterruptKernel') : 'Interrupt Kernel', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'stop', action: function() { self.interruptKernel(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdRestartRunAll') : 'Restart Kernel & Run All Cells', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'restart', action: function() { self.restartAndRunAll(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdRestartClear') : 'Restart Kernel & Clear Outputs', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'restart', action: function() { self.restartAndClearOutputs(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShutdownKernel') : 'Shut Down Kernel', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'stop', action: function() { self.shutdownKernel(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdChangeRuntime') : 'Change Runtime...', category: window.i18n ? window.i18n.t('notebook.catKernel') : 'Kernel', icon: 'kernel', action: function() { self.changeRuntime(); } },

            // Navigation
            { label: NbT('cmdCommitChanges', 'Commit Changes'), category: window.i18n ? window.i18n.t('notebook.catFile') : 'File', shortcut: 'Ctrl+Shift+S', icon: 'save', action: function() { if (window.HistoryManager && self.notebook && self.notebook.id) { HistoryManager.notebookId = self.notebook.id; HistoryManager._showCommitDialog(); } } },
            { label: window.i18n ? window.i18n.t('notebook.cmdSaveNotebook') : 'Save Notebook', category: window.i18n ? window.i18n.t('notebook.catFile') : 'File', shortcut: 'Ctrl+S', icon: 'save', action: function() { self.saveNotebook(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdFindInNotebook') : 'Find in Notebook', category: window.i18n ? window.i18n.t('notebook.catFile') : 'File', shortcut: 'Ctrl+F', icon: 'search', action: function() { self.toggleFindBar(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdToggleWideLayout') : 'Toggle Wide Layout', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'expand', action: function() { self.toggleWideLayout(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdToggleSidebar') : 'Toggle Sidebar', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'sidebar', action: function() { document.getElementById('nb-sidebar').classList.toggle('collapsed'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShowExplorer') : 'Show Explorer', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'folder', action: function() { self._switchPanel('files'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShowNotebooks') : 'Show Notebooks', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'notebook', action: function() { self._switchPanel('notebooks'); } },
            { label: window.i18n ? window.i18n.t('bureau.cmdShowProjects') : 'Show Projects', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'folder', action: function() { self._switchPanel('projects'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShowRuntimes') : 'Show Runtimes', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'kernel', action: function() { self._switchPanel('kernels'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShowOutline') : 'Show Outline', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'outline', action: function() { self._switchPanel('outline'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShowTerminal') : 'Show Terminal', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'terminal', action: function() { self._switchPanel('terminal'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShowSettings') : 'Show Settings', category: window.i18n ? window.i18n.t('notebook.catView') : 'View', icon: 'settings', action: function() { self._switchPanel('settings'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdShowDocumentation') : 'Show Documentation', category: window.i18n ? window.i18n.t('notebook.catHelp') : 'Help', icon: 'docs', action: function() { if (window.NotebookDocs) NotebookDocs.open(); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdKeyboardShortcuts') : 'Keyboard Shortcuts', category: window.i18n ? window.i18n.t('notebook.catHelp') : 'Help', icon: 'shortcuts', action: function() { document.getElementById('shortcuts-modal')?.classList.add('active'); } },
            { label: window.i18n ? window.i18n.t('notebook.cmdBackToDashboard') : 'Back to Dashboard', category: window.i18n ? window.i18n.t('notebook.catNavigation') : 'Navigation', icon: 'home', action: function() { window.location.href = '/dashboard'; } }
        ];
        return this._commandPaletteCommands;
    },

    _switchPanel: function(tab) {
        // Account and Settings toggle right sidebars (not the left panels).
        if (tab === 'account') {
            this.toggleAccountPanel(true);
            return;
        }
        if (tab === 'settings') {
            this.toggleSettingsPanel(true);
            return;
        }

        var currentTab = document.querySelector('.nb-activity-item.active')?.dataset.tab;
        // Ensure sidebar is visible even when the same panel is selected again.
        document.getElementById('nb-sidebar')?.classList.remove('collapsed');
        if (currentTab === tab) return;

        document.querySelectorAll('.nb-panel').forEach(function(p) { p.classList.remove('active'); });
        document.querySelectorAll('.nb-activity-item').forEach(function(b) { b.classList.remove('active'); });
        var panel = document.getElementById('panel-' + tab);
        if (panel) panel.classList.add('active');
        var btn = document.querySelector('.nb-activity-item[data-tab="' + tab + '"]');
        if (btn) btn.classList.add('active');
        // Trigger refresh for specific panels
        if (tab === 'outline') this.refreshOutline();
        if (tab === 'files') this.refreshFileExplorer();
        if (tab === 'notebooks') this.refreshNotebooksList();
        if (tab === 'kernels') this.refreshKernelsList();
        if (tab === 'projects' && window.BureauProjects) BureauProjects.refresh();
        // if (tab === 'models') this.refreshModelsPanel(); // hidden for now
        // Right sidebars (Account/Settings) are additive overlays — switching
        // left panels no longer force-closes them.
    },

    toggleAccountPanel: function(show) {
        var rightbar = document.getElementById('nb-rightbar');
        if (!rightbar) return;
        var btn = document.querySelector('.nb-activity-item[data-tab="account"]');
        if (show === undefined) {
            show = rightbar.classList.contains('collapsed');
        }
        if (show) {
            // Only one right sidebar at a time.
            this.toggleSettingsPanel(false);
            rightbar.classList.remove('collapsed');
            rightbar.setAttribute('aria-hidden', 'false');
            if (btn) btn.classList.add('active');
            // Populate user info on open
            this._populateAccountHeader();
            this._bindWolframAccountButtons();
            this.refreshWolframAccount(true);
            this._bindQuantumAccountButtons();
            this.refreshQuantumAccount(true);
        } else {
            rightbar.classList.add('collapsed');
            rightbar.setAttribute('aria-hidden', 'true');
            if (btn) btn.classList.remove('active');
        }
    },

    // Notebook Settings now lives in a right sidebar (same slide-out pattern
    // as the Account panel) instead of a left sidebar panel.
    toggleSettingsPanel: function(show) {
        var rightbar = document.getElementById('nb-settings-rightbar');
        if (!rightbar) return;
        var btn = document.querySelector('.nb-activity-item[data-tab="settings"]');
        if (show === undefined) {
            show = rightbar.classList.contains('collapsed');
        }
        if (show) {
            // Only one right sidebar at a time.
            this.toggleAccountPanel(false);
            rightbar.classList.remove('collapsed');
            rightbar.setAttribute('aria-hidden', 'false');
            if (btn) btn.classList.add('active');
            this.refreshSettingsPanel();
        } else {
            rightbar.classList.add('collapsed');
            rightbar.setAttribute('aria-hidden', 'true');
            if (btn) btn.classList.remove('active');
        }
    },

    // On notebook load, keep the account panel closed and prefetch the Wolfram
    // account state so it is ready when the panel is opened. It used to pop
    // open over the editor for every non-admin without a Wolfram account.
    _initWolframAccountGate: function() {
        var self = this;
        // Ensure collapsed by default (defensive in case template changes)
        this.toggleAccountPanel(false);
        this.refreshWolframAccount(false).then(function(meta) {
            if (!meta) return;
            // The account drawer stays closed by default: auto-opening it covered the
            // editor for every user without a Wolfram account. It opens from the
            // account button, or when a Wolfram cell reports missing credentials.
        }).catch(function() { /* silent: keep panel hidden */ });
    },

    _populateAccountHeader: function() {
        var usernameEl = document.getElementById('nb-account-username');
        var roleEl = document.getElementById('nb-account-role');
        var emailEl = document.getElementById('nb-account-email');
        if (!usernameEl || !roleEl) return;

        // Pull from window.PHONOS_USER (embedded server-side by Flask via
        // _current_user_info() in routes/pages.py). Fall back to empty strings
        // so we never accidentally render JS internals (e.g. "notebook.function").
        var user = window.PHONOS_USER || {};
        var meta = this._wolframAccountState && this._wolframAccountState.meta;
        var isAdmin = !!(user.is_admin || (meta && meta.is_admin));

        var username = (typeof user.username === 'string' && user.username) ? user.username : '';
        var email = (typeof user.email === 'string' && user.email) ? user.email : '';

        // Only set once unless we have nothing (avoids overwriting if user starts typing).
        if (!usernameEl.dataset.set || usernameEl.textContent.trim() === '\u00a0') {
            usernameEl.textContent = username || (NbT('userFallback', 'User'));
            usernameEl.dataset.set = '1';
        }
        roleEl.textContent = isAdmin
            ? (NbT('roleAdmin', 'Administrator'))
            : (NbT('roleMember', 'Member'));
        // Optional email line (hidden if no element or no email)
        if (emailEl && email) {
            emailEl.textContent = email;
            emailEl.style.display = '';
        } else if (emailEl) {
            emailEl.style.display = 'none';
        }
    },

    _cpSelectedIdx: 0,
    _cpFilteredCommands: [],

    openCommandPalette: function() {
        var overlay = document.getElementById('command-palette-overlay');
        var input = document.getElementById('command-palette-input');
        if (!overlay || !input) return;

        overlay.classList.add('active');
        input.value = '';
        input.focus();
        this._cpSelectedIdx = 0;
        this._renderCommandPaletteResults('');
    },

    closeCommandPalette: function() {
        var overlay = document.getElementById('command-palette-overlay');
        if (overlay) overlay.classList.remove('active');
    },

    _createCpIcon: function(name) {
        // Create SVG icon elements safely via DOM API (no innerHTML)
        var svgNS = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');

        var iconDefs = {
            'plus': function(s) { var l1 = document.createElementNS(svgNS,'line'); l1.setAttribute('x1','12'); l1.setAttribute('y1','5'); l1.setAttribute('x2','12'); l1.setAttribute('y2','19'); s.appendChild(l1); var l2 = document.createElementNS(svgNS,'line'); l2.setAttribute('x1','5'); l2.setAttribute('y1','12'); l2.setAttribute('x2','19'); l2.setAttribute('y2','12'); s.appendChild(l2); },
            'play': function(s) { var p = document.createElementNS(svgNS,'polygon'); p.setAttribute('points','5 3 19 12 5 21 5 3'); s.appendChild(p); },
            'stop': function(s) { var r = document.createElementNS(svgNS,'rect'); r.setAttribute('x','6'); r.setAttribute('y','6'); r.setAttribute('width','12'); r.setAttribute('height','12'); s.appendChild(r); },
            'search': function(s) { var c = document.createElementNS(svgNS,'circle'); c.setAttribute('cx','11'); c.setAttribute('cy','11'); c.setAttribute('r','8'); s.appendChild(c); var l = document.createElementNS(svgNS,'line'); l.setAttribute('x1','21'); l.setAttribute('y1','21'); l.setAttribute('x2','16.65'); l.setAttribute('y2','16.65'); s.appendChild(l); }
        };

        // Use specific builder if exists, else generic circle icon
        if (iconDefs[name]) {
            iconDefs[name](svg);
        } else {
            // Fallback: simple dot icon
            var c = document.createElementNS(svgNS, 'circle');
            c.setAttribute('cx', '12'); c.setAttribute('cy', '12'); c.setAttribute('r', '3');
            svg.appendChild(c);
        }
        return svg;
    },

    _renderCommandPaletteResults: function(query) {
        var container = document.getElementById('command-palette-results');
        if (!container) return;

        var commands = this._getCommands();
        var lowerQ = query.toLowerCase().trim();

        var filtered;
        if (!lowerQ) {
            filtered = commands;
        } else {
            filtered = commands.filter(function(cmd) {
                return cmd.label.toLowerCase().indexOf(lowerQ) !== -1 ||
                       cmd.category.toLowerCase().indexOf(lowerQ) !== -1;
            });
        }

        this._cpFilteredCommands = filtered;
        if (this._cpSelectedIdx >= filtered.length) this._cpSelectedIdx = 0;

        container.textContent = '';

        if (filtered.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'nb-cp-empty';
            empty.textContent = window.i18n ? window.i18n.t('notebook.noMatchingCommands') : 'No matching commands';
            container.appendChild(empty);
            return;
        }

        // Group by category
        var groups = {};
        filtered.forEach(function(cmd) {
            if (!groups[cmd.category]) groups[cmd.category] = [];
            groups[cmd.category].push(cmd);
        });

        var self = this;
        var globalIdx = 0;
        Object.keys(groups).forEach(function(category) {
            var label = document.createElement('div');
            label.className = 'nb-cp-group-label';
            label.textContent = category;
            container.appendChild(label);

            groups[category].forEach(function(cmd) {
                var item = document.createElement('div');
                item.className = 'nb-cp-item' + (globalIdx === self._cpSelectedIdx ? ' selected' : '');
                item.dataset.idx = String(globalIdx);

                // Icon (safe DOM creation)
                item.appendChild(self._createCpIcon(cmd.icon));

                // Label
                var labelSpan = document.createElement('span');
                labelSpan.className = 'nb-cp-item-label';
                labelSpan.textContent = cmd.label;
                item.appendChild(labelSpan);

                // Shortcut
                if (cmd.shortcut) {
                    var scSpan = document.createElement('span');
                    scSpan.className = 'nb-cp-item-shortcut';
                    cmd.shortcut.split('+').forEach(function(key, i) {
                        if (i > 0) scSpan.appendChild(document.createTextNode('+'));
                        var kbd = document.createElement('kbd');
                        kbd.textContent = key.trim();
                        scSpan.appendChild(kbd);
                    });
                    item.appendChild(scSpan);
                }

                item.addEventListener('click', function() {
                    self.closeCommandPalette();
                    cmd.action();
                });
                item.addEventListener('mouseenter', function() {
                    self._cpSelectedIdx = parseInt(item.dataset.idx);
                    container.querySelectorAll('.nb-cp-item').forEach(function(el) { el.classList.remove('selected'); });
                    item.classList.add('selected');
                });

                container.appendChild(item);
                globalIdx++;
            });
        });
    },

    _executeSelectedCommand: function() {
        var cmd = this._cpFilteredCommands[this._cpSelectedIdx];
        if (cmd) {
            this.closeCommandPalette();
            cmd.action();
        }
    },

    setupCommandPalette: function() {
        var self = this;
        var overlay = document.getElementById('command-palette-overlay');
        var input = document.getElementById('command-palette-input');
        if (!overlay || !input) return;

        // Close on overlay click
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) self.closeCommandPalette();
        });

        // Filter on input
        input.addEventListener('input', function() {
            self._cpSelectedIdx = 0;
            self._renderCommandPaletteResults(input.value);
        });

        // Keyboard navigation
        input.addEventListener('keydown', function(e) {
            var results = document.getElementById('command-palette-results');
            if (e.key === 'Escape') {
                e.preventDefault();
                self.closeCommandPalette();
            } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                self._cpSelectedIdx = Math.min(self._cpSelectedIdx + 1, self._cpFilteredCommands.length - 1);
                self._updateCPSelection(results);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                self._cpSelectedIdx = Math.max(self._cpSelectedIdx - 1, 0);
                self._updateCPSelection(results);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                self._executeSelectedCommand();
            }
        });
    },

    _updateCPSelection: function(container) {
        if (!container) return;
        var self = this;
        var items = container.querySelectorAll('.nb-cp-item');
        items.forEach(function(el) {
            el.classList.toggle('selected', parseInt(el.dataset.idx) === self._cpSelectedIdx);
        });
        // Scroll selected into view
        var sel = container.querySelector('.nb-cp-item.selected');
        if (sel) sel.scrollIntoView({ block: 'nearest' });
    },

    // ========================================================================
    // OUTLINE PANEL
    // ========================================================================

    refreshOutline: function() {
        var container = document.getElementById('outline-container');
        if (!container) return;

        container.textContent = '';

        if (!this.notebook || this.notebook.cells.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'nb-empty-state';
            empty.textContent = window.i18n ? window.i18n.t('notebook.noHeadingsFound') : 'No headings found';
            container.appendChild(empty);
            return;
        }

        var headings = [];
        var self = this;

        this.notebook.cells.forEach(function(cell, idx) {
            if (cell.type === 'markdown') {
                var source = cell.cm ? cell.cm.getValue() : cell.source;
                var lines = source.split('\n');
                lines.forEach(function(line) {
                    var match = line.match(/^(#{1,4})\s+(.+)/);
                    if (match) {
                        headings.push({
                            level: match[1].length,
                            text: match[2].replace(/[*_`#]/g, '').trim(),
                            cellId: cell.id,
                            cellIndex: idx
                        });
                    }
                });
            }
            // Also show code cells with section comment blocks
            if (isExecutableType(cell.type)) {
                var source = cell.cm ? cell.cm.getValue() : cell.source;
                var firstLine = source.split('\n')[0];
                var commentMatch = firstLine.match(/^#\s*={3,}\s*$/);
                if (commentMatch) {
                    var secondLine = source.split('\n')[1];
                    if (secondLine) {
                        var headerMatch = secondLine.match(/^#\s+(.+)/);
                        if (headerMatch) {
                            headings.push({
                                level: 2,
                                text: headerMatch[1].trim(),
                                cellId: cell.id,
                                cellIndex: idx,
                                isCode: true
                            });
                        }
                    }
                }
            }
        });

        if (headings.length === 0) {
            // Show cell list as fallback
            var hint = document.createElement('div');
            hint.className = 'nb-empty-state';
            hint.style.paddingBottom = '8px';
            hint.textContent = window.i18n ? window.i18n.t('notebook.addHeadingsHint') : 'Add markdown headings (# Title) to create an outline';
            container.appendChild(hint);

            // Show all cells as a flat list
            this.notebook.cells.forEach(function(cell, idx) {
                var item = document.createElement('div');
                item.className = 'nb-outline-item h2';
                if (cell.id === self.activeCellId) item.classList.add('active');

                var label = document.createElement('span');
                var preview = (cell.cm ? cell.cm.getValue() : cell.source).substring(0, 40);
                label.textContent = preview || (isExecutableType(cell.type) ? (window.i18n ? window.i18n.t('notebook.codeCell') : 'Code cell') : (window.i18n ? window.i18n.t('notebook.textCell') : 'Text cell'));
                label.style.overflow = 'hidden';
                label.style.textOverflow = 'ellipsis';
                label.style.whiteSpace = 'nowrap';
                label.style.flex = '1';
                item.appendChild(label);

                var badge = document.createElement('span');
                badge.className = 'nb-outline-cell-badge';
                badge.textContent = isExecutableType(cell.type) ? '[' + (idx + 1) + ']' : 'md';
                item.appendChild(badge);

                item.addEventListener('click', function() {
                    self._scrollToCell(cell.id);
                });
                container.appendChild(item);
            });
            return;
        }

        headings.forEach(function(h) {
            var item = document.createElement('div');
            item.className = 'nb-outline-item h' + h.level;
            if (h.cellId === self.activeCellId) item.classList.add('active');

            var label = document.createElement('span');
            label.textContent = h.text;
            label.style.overflow = 'hidden';
            label.style.textOverflow = 'ellipsis';
            label.style.whiteSpace = 'nowrap';
            label.style.flex = '1';
            item.appendChild(label);

            var badge = document.createElement('span');
            badge.className = 'nb-outline-cell-badge';
            badge.textContent = h.isCode ? 'code' : 'md';
            item.appendChild(badge);

            item.addEventListener('click', function() {
                self._scrollToCell(h.cellId);
            });
            container.appendChild(item);
        });
    },

    _scrollToCell: function(cellId) {
        var el = document.getElementById(cellId);
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            this.setActiveCell(cellId);
            var cell = this.notebook.getCell(cellId);
            if (cell && cell.cm) {
                setTimeout(function() { cell.cm.focus(); }, 100);
            }
        }
    },

    // ========================================================================
    // LIVE CELL EXECUTION TIMER
    // ========================================================================

    _liveTimers: {},

    startLiveTimer: function(cellId) {
        // Cell timers are intentionally disabled in the notebook IDE.
        this.stopLiveTimer(cellId);
    },

    stopLiveTimer: function(cellId) {
        if (this._liveTimers[cellId]) {
            clearInterval(this._liveTimers[cellId]);
            delete this._liveTimers[cellId];
        }
    },

    // ========================================================================
    // ENHANCED STATUS BAR
    // ========================================================================

    updateStatusBar: function() {
        // Cell count
        var countEl = document.getElementById('status-cell-count');
        if (countEl) {
            var total = this.notebook ? this.notebook.cells.length : 0;
            countEl.textContent = NbT('nCells', '{n} cell(s)').replace('{n}', total);
        }

        // Active cell indicator
        var activeCellEl = document.getElementById('status-active-cell');
        if (activeCellEl && this.notebook && this.activeCellId) {
            var idx = this.notebook.cells.findIndex(function(c) { return c.id === this.activeCellId; }.bind(this));
            if (idx !== -1) {
                var cell = this.notebook.cells[idx];
                activeCellEl.textContent = window.i18n ? window.i18n.t('notebook.cellPosition').replace('{n}', idx + 1).replace('{total}', this.notebook.cells.length).replace('{type}', cell.type) : 'Cell ' + (idx + 1) + '/' + this.notebook.cells.length + ' (' + cell.type + ')';
                // Update language badge based on active cell type
                var langEl = document.getElementById('status-language');
                if (langEl) {
                    var langMap = { 'code': 'Python 3.11', 'wolfram': 'Wolfram Cloud', 'markdown': 'Markdown', 'c': 'C (gcc)', 'rust': 'Rust (rustc)' };
                    langEl.textContent = langMap[cell.type] || 'Python 3.11';
                }
            }
        } else if (activeCellEl) {
            activeCellEl.textContent = '';
        }

        // Cursor position from active CodeMirror
        var posEl = document.getElementById('status-position');
        if (posEl) {
            var curLine = 0, curCol = 0;
            var activeCell = (this.activeCellId && this.notebook) ? this.notebook.getCell(this.activeCellId) : null;
            if (activeCell && activeCell.cm) {
                var cursor = activeCell.cm.getCursor();
                curLine = cursor.line; curCol = cursor.ch;
            }
            posEl.textContent = NbT('lineColumn', 'Ln {line}, Col {col}').replace('{line}', curLine + 1).replace('{col}', curCol + 1);
        }

        // Runtime composition
        var rtEl = document.getElementById('status-runtime');
        if (rtEl && this.notebook) {
            var runtimes = {};
            var rtLetters = { 'wolfram': 'W', 'quantum': 'Q', 'c': 'C', 'cpp': 'C++', 'rust': 'Rs', 'r': 'R' };
            this.notebook.cells.forEach(function(c) {
                var rt = rtLetters[c.type] || 'Py';
                runtimes[rt] = (runtimes[rt] || 0) + 1;
            });
            var parts = Object.keys(runtimes).sort().map(function(k) { return k + ':' + runtimes[k]; });
            var activeRtEl = document.getElementById('status-runtime');
            if (activeRtEl) activeRtEl.textContent = parts.join(' | ') || '--';
        }
    },

    updateStatusBarKernel: function(status) {
        var dot = document.getElementById('sb-kernel-dot');
        if (dot) {
            dot.className = 'nb-sb-kernel-dot ' + status;
        }
        var execEl = document.getElementById('status-execution');
        if (execEl) {
            var labels = {
                'idle': window.i18n ? window.i18n.t('notebook.kernelReady') : 'Ready',
                'busy': window.i18n ? window.i18n.t('notebook.kernelRunning') : 'Running',
                'connecting': window.i18n ? window.i18n.t('notebook.kernelConnecting') : 'Connecting...',
                'error': window.i18n ? window.i18n.t('notebook.kernelError') : 'Kernel Error',
                'disconnected': NbT('kernelDisconnected', 'Disconnected'),
                'restarting': window.i18n ? window.i18n.t('notebook.kernelRestarting') : 'Restarting...'
            };
            execEl.textContent = labels[status] || status;
        }
    },

    updateStatusBarSave: function(state) {
        var el = document.getElementById('status-save-indicator');
        if (!el) return;
        el.className = 'nb-sb-saved ' + state;
        var labels = { 'saved': window.i18n ? window.i18n.t('notebook.saved') : 'Saved', 'unsaved': window.i18n ? window.i18n.t('notebook.unsaved') : 'Unsaved', 'saving': window.i18n ? window.i18n.t('notebook.saving') : 'Saving...' };
        el.textContent = labels[state] || state;
    },

    showToast: function(message, duration = 2000) {
        let toast = document.getElementById('nb-toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'nb-toast';
            toast.className = 'nb-toast';
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.classList.add('visible');
        if (duration > 0) {
            setTimeout(() => toast.classList.remove('visible'), duration);
        }
        return toast;
    },

    dismissToast: function(toast) {
        if (toast && toast.parentNode) {
            toast.classList.remove('visible');
        }
    },

    renameFile: async function(fileId) {
        const newName = prompt(window.i18n ? window.i18n.t('notebook.newFilename') : "New filename:");
        if (!newName) return;
        try {
            await fetch(`/api/files/${fileId}/rename`, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: newName})
            });
            this.refreshFileExplorer(true);
        } catch (e) { alert(window.i18n ? window.i18n.t('notebook.renameFailed') : 'Rename failed'); }
    },

    deleteFile: async function(fileId) {
        if (!confirm(window.i18n ? window.i18n.t('notebook.deleteFileConfirm') : "Delete this file?")) return;
        try {
            await fetch(`/api/files/${fileId}`, { method: 'DELETE' });
            this.refreshFileExplorer(true);
        } catch (e) { alert(window.i18n ? window.i18n.t('notebook.deleteFailed') : 'Delete failed'); }
    },

    downloadFile: function(fileId) {
        window.open(`/download_file/${fileId}`, '_blank');
    },

    // ========================================================================
    // FILE / NOTEBOOK MANAGER - SELECTION & BULK ACTIONS
    // ========================================================================

    _getCsrfToken: function() {
        let token = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.csrfToken) || '';
        if (!token) {
            const meta = document.querySelector('meta[name="csrf-token"]');
            if (meta) token = meta.getAttribute('content') || '';
        }
        return token;
    },

    _ensureFileSelectionToolbar: function() {
        let toolbar = document.getElementById('nb-file-selection-toolbar');
        if (!toolbar) {
            toolbar = document.createElement('div');
            toolbar.id = 'nb-file-selection-toolbar';
            toolbar.className = 'nb-selection-toolbar';
            toolbar.innerHTML = `
                <span class="nb-selection-count" id="nb-file-selection-count"></span>
                <div class="nb-selection-actions">
                    <button class="nb-btn small secondary" data-action="rename" title="Rename selected file">Rename</button>
                    <button class="nb-btn small secondary" data-action="copy" title="Copy selected file">Copy</button>
                    <button class="nb-btn small secondary" data-action="download" title="Download selected file">Download</button>
                    <button class="nb-btn small danger" data-action="delete" title="Delete selected files">Delete</button>
                    <button class="nb-btn small ghost" data-action="clear" title="Clear selection">Clear</button>
                </div>
            `;
            toolbar.addEventListener('click', (e) => {
                const btn = e.target.closest('button[data-action]');
                if (!btn) return;
                const action = btn.dataset.action;
                if (action === 'clear') {
                    this._clearFileSelection(true);
                } else if (action === 'delete') {
                    this._bulkDeleteSelectedFiles();
                } else if (action === 'rename') {
                    this._renameSelectedFile();
                } else if (action === 'copy') {
                    this._copySelectedFile();
                } else if (action === 'download') {
                    this._downloadSelectedFiles();
                }
            });
            const panel = document.getElementById('panel-files');
            if (panel) panel.insertBefore(toolbar, panel.firstChild);
        }
        return toolbar;
    },

    _ensureNotebookSelectionToolbar: function() {
        let toolbar = document.getElementById('nb-notebook-selection-toolbar');
        if (!toolbar) {
            toolbar = document.createElement('div');
            toolbar.id = 'nb-notebook-selection-toolbar';
            toolbar.className = 'nb-selection-toolbar';
            toolbar.innerHTML = `
                <span class="nb-selection-count" id="nb-notebook-selection-count"></span>
                <div class="nb-selection-actions">
                    <button class="nb-btn small secondary" data-action="rename" title="Rename selected notebook">Rename</button>
                    <button class="nb-btn small secondary" data-action="duplicate" title="Duplicate selected notebook">Duplicate</button>
                    <button class="nb-btn small secondary" data-action="move" title="Move to another project">Move</button>
                    <button class="nb-btn small danger" data-action="delete" title="Delete selected notebooks">Delete</button>
                    <button class="nb-btn small ghost" data-action="clear" title="Clear selection">Clear</button>
                </div>
            `;
            toolbar.addEventListener('click', (e) => {
                const btn = e.target.closest('button[data-action]');
                if (!btn) return;
                const action = btn.dataset.action;
                if (action === 'clear') {
                    this._clearNotebookSelection(true);
                } else if (action === 'delete') {
                    this._bulkDeleteSelectedNotebooks();
                } else if (action === 'rename') {
                    this._renameSelectedNotebook();
                } else if (action === 'duplicate') {
                    this._duplicateSelectedNotebook();
                } else if (action === 'move') {
                    this._moveSelectedNotebook();
                }
            });
            const panel = document.getElementById('panel-notebooks');
            if (panel) panel.insertBefore(toolbar, panel.firstChild);
        }
        return toolbar;
    },

    _toggleFileSelection: function(file, item, index, forceSelect) {
        const id = String(file.id);
        const select = forceSelect ? true : !this._selectedFiles.has(id);
        if (select) {
            this._selectedFiles.set(id, file);
        } else {
            this._selectedFiles.delete(id);
        }
        this._lastSelectedFileIndex = index;
        this._updateFileSelectionUI();
    },

    _selectFileRange: function(endIndex) {
        const items = Array.from(document.querySelectorAll('#file-tree .file-item[data-file-index]'));
        const start = this._lastSelectedFileIndex >= 0 ? this._lastSelectedFileIndex : 0;
        const minIdx = Math.min(start, endIndex);
        const maxIdx = Math.max(start, endIndex);
        items.forEach((item) => {
            const idx = parseInt(item.dataset.fileIndex, 10);
            if (idx >= minIdx && idx <= maxIdx) {
                const fileId = item.dataset.fileId;
                // Reconstruct minimal file object from cached data if possible.
                const file = this._findFileInCache(fileId) || { id: fileId, name: item.querySelector('.file-name')?.textContent || '' };
                this._selectedFiles.set(String(fileId), file);
            }
        });
        this._lastSelectedFileIndex = endIndex;
        this._updateFileSelectionUI();
    },

    _findFileInCache: function(fileId) {
        const cached = this._getSidebarPanelCache('files');
        if (!cached || !cached.files) return null;
        return cached.files.find(f => String(f.id) === String(fileId)) || null;
    },

    _findNotebookInCache: function(notebookId) {
        const cached = this._getSidebarPanelCache('notebooks');
        if (!cached || !cached.notebooks) return null;
        return cached.notebooks.find(n => String(n.id) === String(notebookId)) || null;
    },

    _clearFileSelection: function(refresh) {
        this._selectedFiles.clear();
        this._lastSelectedFileIndex = -1;
        this._updateFileSelectionUI();
        if (refresh) this.refreshFileExplorer(true);
    },

    _updateFileSelectionUI: function() {
        const count = this._selectedFiles.size;
        const items = document.querySelectorAll('#file-tree .file-item[data-file-index]');
        items.forEach(item => {
            const id = item.dataset.fileId;
            item.classList.toggle('selected', this._selectedFiles.has(String(id)));
            const cb = item.querySelector('.nb-select-checkbox');
            if (cb) cb.classList.toggle('checked', this._selectedFiles.has(String(id)));
        });
        const toolbar = document.getElementById('nb-file-selection-toolbar');
        if (toolbar) {
            toolbar.classList.toggle('active', count > 0);
            const countEl = document.getElementById('nb-file-selection-count');
            if (countEl) countEl.textContent = count + ' selected';
        }
    },

    _toggleNotebookSelection: function(nb, item, index, forceSelect) {
        const id = String(nb.id);
        const select = forceSelect ? true : !this._selectedNotebooks.has(id);
        if (select) {
            this._selectedNotebooks.set(id, nb);
        } else {
            this._selectedNotebooks.delete(id);
        }
        this._lastSelectedNotebookIndex = index;
        this._updateNotebookSelectionUI();
    },

    _selectNotebookRange: function(endIndex) {
        const items = Array.from(document.querySelectorAll('#file-tree .file-item[data-notebook-index]'));
        const start = this._lastSelectedNotebookIndex >= 0 ? this._lastSelectedNotebookIndex : 0;
        const minIdx = Math.min(start, endIndex);
        const maxIdx = Math.max(start, endIndex);
        items.forEach((item) => {
            const idx = parseInt(item.dataset.notebookIndex, 10);
            if (idx >= minIdx && idx <= maxIdx) {
                const notebookId = item.dataset.notebookId;
                const nb = this._findNotebookInCache(notebookId) || { id: notebookId, name: item.querySelector('span')?.textContent || '' };
                this._selectedNotebooks.set(String(notebookId), nb);
            }
        });
        this._lastSelectedNotebookIndex = endIndex;
        this._updateNotebookSelectionUI();
    },

    _clearNotebookSelection: function(refresh) {
        this._selectedNotebooks.clear();
        this._lastSelectedNotebookIndex = -1;
        this._updateNotebookSelectionUI();
        if (refresh) this.refreshNotebooksList(true);
    },

    _updateNotebookSelectionUI: function() {
        const count = this._selectedNotebooks.size;
        const items = document.querySelectorAll('#file-tree .file-item[data-notebook-index], #notebooks-list .notebook-item');
        items.forEach(item => {
            const id = item.dataset.notebookId;
            item.classList.toggle('selected', this._selectedNotebooks.has(String(id)));
            const cb = item.querySelector('.nb-select-checkbox');
            if (cb) cb.classList.toggle('checked', this._selectedNotebooks.has(String(id)));
        });
        const toolbar = document.getElementById('nb-notebook-selection-toolbar');
        if (toolbar) {
            toolbar.classList.toggle('active', count > 0);
            const countEl = document.getElementById('nb-notebook-selection-count');
            if (countEl) countEl.textContent = count + ' selected';
        }
    },

    _getSelectedFileIds: function() {
        return Array.from(this._selectedFiles.keys()).map(id => parseInt(id, 10)).filter(id => !isNaN(id));
    },

    _getSelectedNotebookIds: function() {
        return Array.from(this._selectedNotebooks.keys()).map(id => parseInt(id, 10)).filter(id => !isNaN(id));
    },

    _bulkDeleteSelectedFiles: async function() {
        const ids = this._getSelectedFileIds();
        if (ids.length === 0) return;
        const msg = ids.length === 1
            ? (window.i18n ? window.i18n.t('notebook.deleteFileConfirm') : 'Delete this file?')
            : (window.i18n ? NbT('deleteNFilesConfirm', 'Delete {n} files?').replace('{n}', ids.length) : `Delete ${ids.length} files?`);
        if (!confirm(msg)) return;
        try {
            const csrfToken = this._getCsrfToken();
            const res = await fetch(`/api/projects/${this.projectId}/files/bulk-delete`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ file_ids: ids })
            });
            const data = await res.json();
            if (res.ok && data.status === 'success') {
                this.showToast(window.i18n ? NbT('deletedNFiles', 'Deleted {n} file(s)').replace('{n}', ids.length) : `Deleted ${ids.length} file(s)`);
                this._clearFileSelection(true);
            } else {
                throw new Error(data.message || 'Delete failed');
            }
        } catch (e) {
            console.error(e);
            alert((window.i18n ? window.i18n.t('notebook.deleteFailed') : 'Delete failed') + ': ' + e.message);
        }
    },

    _bulkDeleteSelectedNotebooks: async function() {
        const ids = this._getSelectedNotebookIds();
        if (ids.length === 0) return;
        const msg = ids.length === 1
            ? (window.i18n ? window.i18n.t('notebook.deleteNotebookConfirm') : 'Delete this notebook?')
            : (window.i18n ? NbT('deleteNNotebooksConfirm', 'Delete {n} notebooks?').replace('{n}', ids.length) : `Delete ${ids.length} notebooks?`);
        if (!confirm(msg)) return;
        try {
            for (const id of ids) {
                await fetch(`/api/notebooks/${id}`, { method: 'DELETE' });
            }
            this.showToast(window.i18n ? NbT('deletedNNotebooks', 'Deleted {n} notebook(s)').replace('{n}', ids.length) : `Deleted ${ids.length} notebook(s)`);
            this._clearNotebookSelection(true);
            if (this.notebook && ids.includes(parseInt(this.notebook.id, 10))) {
                window.location.href = '/notebook';
            }
        } catch (e) {
            console.error(e);
            alert(window.i18n ? window.i18n.t('notebook.deleteFailed') : 'Delete failed');
        }
    },

    _renameSelectedFile: function() {
        if (this._selectedFiles.size !== 1) return;
        const file = Array.from(this._selectedFiles.values())[0];
        this._showRenameModal('file', file);
    },

    _renameSelectedNotebook: function() {
        if (this._selectedNotebooks.size !== 1) return;
        const nb = Array.from(this._selectedNotebooks.values())[0];
        this._showRenameModal('notebook', nb);
    },

    _copySelectedFile: function() {
        if (this._selectedFiles.size !== 1) return;
        const file = Array.from(this._selectedFiles.values())[0];
        const currentName = this._resolveFileName(file);
        const base = currentName.replace(/\.[^.]+$/, '');
        const ext = currentName.includes('.') ? currentName.slice(currentName.lastIndexOf('.')) : '';
        const newName = base + ' (Copy)' + ext;
        this._showInputModal({
            title: NbT('copyFile', 'Copy File'),
            label: window.i18n ? window.i18n.t('notebook.newFilename') : 'New filename',
            value: newName,
            confirm: window.i18n ? window.i18n.t('notebook.copy') : 'Copy',
            onConfirm: async (name) => {
                if (!name || name === currentName) return;
                try {
                    const csrfToken = this._getCsrfToken();
                    const res = await fetch(`/api/projects/${this.projectId}/files/copy`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRF-Token': csrfToken
                        },
                        body: JSON.stringify({ file_id: file.id, new_name: name })
                    });
                    const data = await res.json();
                    if (res.ok && data.status === 'success') {
                        this.showToast(NbT('fileCopied', 'File copied'));
                        this._clearFileSelection(true);
                    } else {
                        throw new Error(data.message || 'Copy failed');
                    }
                } catch (e) {
                    console.error(e);
                    alert((NbT('copyFailed', 'Copy failed')) + ': ' + e.message);
                }
            }
        });
    },

    _downloadSelectedFiles: function() {
        var count = 0;
        this._selectedFiles.forEach(file => {
            const id = file.id;
            if (id && /^\d+$/.test(String(id))) {
                window.open(`/download_file/${id}`, '_blank');
                count++;
            }
        });
        if (count === 0) {
            this.showToast(NbT('noDownloadableFiles', 'No downloadable files selected'), 3000);
        }
    },

    _duplicateSelectedNotebook: function() {
        if (this._selectedNotebooks.size !== 1) return;
        const nb = Array.from(this._selectedNotebooks.values())[0];
        this._showInputModal({
            title: NbT('duplicateNotebook', 'Duplicate Notebook'),
            label: NbT('newName', 'New name'),
            value: nb.name + ' (Copy)',
            confirm: window.i18n ? window.i18n.t('notebook.duplicate') : 'Duplicate',
            onConfirm: async (name) => {
                if (!name) return;
                try {
                    const csrfToken = this._getCsrfToken();
                    const res = await fetch(`/api/notebooks/${nb.id}/duplicate`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRF-Token': csrfToken
                        },
                        body: JSON.stringify({ name: name, project_id: this.projectId })
                    });
                    const data = await res.json();
                    if (res.ok) {
                        this.showToast(NbT('notebookDuplicated', 'Notebook duplicated'));
                        this._clearNotebookSelection(true);
                    } else {
                        throw new Error(data.error || 'Duplicate failed');
                    }
                } catch (e) {
                    console.error(e);
                    alert((NbT('duplicateFailed', 'Duplicate failed')) + ': ' + e.message);
                }
            }
        });
    },

    _moveSelectedNotebook: function() {
        if (this._selectedNotebooks.size !== 1) return;
        const nb = Array.from(this._selectedNotebooks.values())[0];
        this._showMoveNotebookModal(nb);
    },

    _createNewFolder: function() {
        if (!this.projectId) {
            this.showToast(NbT('noProjectSelectedUpload', 'Select a project first'), 3000);
            return;
        }
        this._showInputModal({
            title: window.i18n ? window.i18n.t('notebook.newFolder') : 'New Folder',
            label: NbT('folderName', 'Folder name'),
            value: '',
            confirm: window.i18n ? window.i18n.t('common.create') : 'Create',
            onConfirm: async (name) => {
                if (!name) return;
                try {
                    const csrfToken = this._getCsrfToken();
                    const res = await fetch(`/api/projects/${this.projectId}/folders`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRF-Token': csrfToken
                        },
                        body: JSON.stringify({ name: name })
                    });
                    const data = await res.json();
                    if (res.ok && data.status === 'success') {
                        this.showToast(NbT('folderCreated', 'Folder created'));
                        this.refreshFileExplorer(true);
                    } else {
                        throw new Error(data.message || 'Create folder failed');
                    }
                } catch (e) {
                    console.error(e);
                    alert((NbT('folderCreateFailed', 'Create folder failed')) + ': ' + e.message);
                }
            }
        });
    },

    _showRenameModal: function(kind, item) {
        const currentName = kind === 'file' ? this._resolveFileName(item) : item.name;
        this._showInputModal({
            title: kind === 'file'
                ? (NbT('renameFile', 'Rename File'))
                : (NbT('renameNotebook', 'Rename Notebook')),
            label: NbT('newName', 'New name'),
            value: currentName,
            confirm: window.i18n ? window.i18n.t('notebook.rename') : 'Rename',
            onConfirm: async (name) => {
                if (!name || name === currentName) return;
                try {
                    if (kind === 'file') {
                        const csrfToken = this._getCsrfToken();
                        const res = await fetch(`/api/files/${item.id}/rename`, {
                            method: 'PATCH',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-CSRF-Token': csrfToken
                            },
                            body: JSON.stringify({ name: name })
                        });
                        const data = await res.json();
                        if (res.ok && data.status === 'success') {
                            this.showToast(NbT('fileRenamed', 'File renamed'));
                            this._clearFileSelection(true);
                        } else {
                            throw new Error(data.message || 'Rename failed');
                        }
                    } else {
                        const csrfToken = this._getCsrfToken();
                        const res = await fetch(`/api/notebooks/${item.id}`, {
                            method: 'PUT',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-CSRF-Token': csrfToken
                            },
                            body: JSON.stringify({ name: name })
                        });
                        const data = await res.json();
                        if (res.ok) {
                            this.showToast(NbT('notebookRenamed', 'Notebook renamed'));
                            this._clearNotebookSelection(true);
                            if (this.notebook && this.notebook.id == item.id) {
                                const nameInput = document.getElementById('notebook-name');
                                if (nameInput) nameInput.value = name;
                            }
                        } else {
                            throw new Error(data.error || 'Rename failed');
                        }
                    }
                } catch (e) {
                    console.error(e);
                    alert((window.i18n ? window.i18n.t('notebook.renameFailed') : 'Rename failed') + ': ' + e.message);
                }
            }
        });
    },

    _showInputModal: function(opts) {
        let overlay = document.getElementById('nb-input-modal');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'nb-input-modal';
            overlay.className = 'nb-modal-overlay';
            overlay.innerHTML = `
                <div class="nb-modal" style="width: 420px;">
                    <div class="nb-modal-header">
                        <h3 id="nb-input-modal-title"></h3>
                        <button class="nb-modal-close modal-close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
                    </div>
                    <div class="nb-modal-body">
                        <div class="nb-form-group">
                            <label id="nb-input-modal-label"></label>
                            <input type="text" id="nb-input-modal-field" class="nb-input" autocomplete="off">
                        </div>
                    </div>
                    <div class="nb-modal-footer">
                        <button class="nb-btn secondary modal-close" id="nb-input-modal-cancel">${window.i18n ? window.i18n.t('common.cancel') : 'Cancel'}</button>
                        <button class="nb-btn primary" id="nb-input-modal-ok">${window.i18n ? window.i18n.t('common.confirm') : 'OK'}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            overlay.querySelector('.modal-close')?.addEventListener('click', () => overlay.classList.remove('active'));
            overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.classList.remove('active'); });
            const okBtn = document.getElementById('nb-input-modal-ok');
            const field = document.getElementById('nb-input-modal-field');
            okBtn?.addEventListener('click', () => {
                if (overlay._onConfirm) overlay._onConfirm(field.value);
                overlay.classList.remove('active');
            });
            field?.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    if (overlay._onConfirm) overlay._onConfirm(field.value);
                    overlay.classList.remove('active');
                } else if (e.key === 'Escape') {
                    overlay.classList.remove('active');
                }
            });
        }
        document.getElementById('nb-input-modal-title').textContent = opts.title || '';
        document.getElementById('nb-input-modal-label').textContent = opts.label || '';
        const field = document.getElementById('nb-input-modal-field');
        field.value = opts.value || '';
        const okBtn = document.getElementById('nb-input-modal-ok');
        okBtn.textContent = opts.confirm || 'OK';
        overlay._onConfirm = opts.onConfirm;
        overlay.classList.add('active');
        setTimeout(() => field.focus(), 50);
        setTimeout(() => field.select(), 60);
    },

    _showMoveNotebookModal: function(nb) {
        let overlay = document.getElementById('nb-move-notebook-modal');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'nb-move-notebook-modal';
            overlay.className = 'nb-modal-overlay';
            overlay.innerHTML = `
                <div class="nb-modal" style="width: 420px;">
                    <div class="nb-modal-header">
                        <h3>${NbT('moveNotebook', 'Move Notebook')}</h3>
                        <button class="nb-modal-close modal-close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
                    </div>
                    <div class="nb-modal-body">
                        <div class="nb-form-group">
                            <label>${NbT('targetProject', 'Target project')}</label>
                            <select id="nb-move-notebook-project" class="nb-select"></select>
                        </div>
                    </div>
                    <div class="nb-modal-footer">
                        <button class="nb-btn secondary modal-close">${window.i18n ? window.i18n.t('common.cancel') : 'Cancel'}</button>
                        <button class="nb-btn primary" id="nb-move-notebook-ok">${NbT('move', 'Move')}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            overlay.querySelector('.modal-close')?.addEventListener('click', () => overlay.classList.remove('active'));
            overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.classList.remove('active'); });
            document.getElementById('nb-move-notebook-ok')?.addEventListener('click', () => {
                const select = document.getElementById('nb-move-notebook-project');
                const targetId = select ? select.value : '';
                if (targetId && overlay._onMove) overlay._onMove(targetId);
                overlay.classList.remove('active');
            });
        }
        const select = document.getElementById('nb-move-notebook-project');
        select.innerHTML = '';
        this.projects.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.name;
            if (String(p.id) === String(nb.project_id)) opt.selected = true;
            select.appendChild(opt);
        });
        overlay._onMove = async (targetId) => {
            if (String(targetId) === String(nb.project_id)) return;
            try {
                const csrfToken = this._getCsrfToken();
                const res = await fetch(`/api/notebooks/${nb.id}/project`, {
                    method: 'PATCH',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': csrfToken
                    },
                    body: JSON.stringify({ project_id: targetId })
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    this.showToast(NbT('notebookMoved', 'Notebook moved'));
                    this._clearNotebookSelection(true);
                } else {
                    throw new Error(data.error || 'Move failed');
                }
            } catch (e) {
                console.error(e);
                alert((NbT('moveFailed', 'Move failed')) + ': ' + e.message);
            }
        };
        overlay.classList.add('active');
    },

    _renderNotebooksList: function(container, allNotebooks) {
        if (!container) return;
        container.innerHTML = '';
        let notebooks = Array.isArray(allNotebooks) ? allNotebooks.slice() : [];

        if (this.notebookSearchTerm) {
            notebooks = notebooks.filter(nb => (nb.name || '').toLowerCase().includes(this.notebookSearchTerm));
        }

        if (notebooks.length === 0) {
            container.innerHTML = '<div class="nb-empty-state">' + (window.i18n ? window.i18n.t('notebook.noNotebooksFound') : 'No notebooks found') + '</div>';
            return;
        }

        this._ensureNotebookSelectionToolbar();

        notebooks.forEach((nb, nbIdx) => {
            const item = document.createElement('div');
            const isActive = this.notebook && this.notebook.id == nb.id;
            item.className = `notebook-item ${isActive ? 'active' : ''}`;
            item.dataset.notebookId = String(nb.id);
            item.dataset.notebookIndex = String(nbIdx);
            item.innerHTML = `
                <span class="nb-select-checkbox" title="Select"></span>
                <div class="notebook-item-name">${this.escapeHtml(nb.name)}</div>
                <div style="font-size:11px; color:var(--nb-text-muted)">${new Date(nb.updated_at).toLocaleDateString()}</div>
            `;
            item.style.cursor = 'pointer';
            item.onclick = (e) => {
                if (e.target.closest('.nb-select-checkbox')) {
                    this._toggleNotebookSelection(nb, item, nbIdx);
                    return;
                }
                if (e.ctrlKey || e.metaKey) {
                    e.preventDefault();
                    this._toggleNotebookSelection(nb, item, nbIdx);
                    return;
                }
                if (e.shiftKey) {
                    e.preventDefault();
                    this._selectNotebookRangeInList(nbIdx);
                    return;
                }
                if (this._selectedNotebooks.size > 0) {
                    if (!this._selectedNotebooks.has(String(nb.id))) {
                        this._clearNotebookSelection(false);
                    } else {
                        this._lastSelectedNotebookIndex = nbIdx;
                        this._updateNotebookSelectionUI();
                        return;
                    }
                }
                window.location.href = `/notebook?id=${nb.id}&project_id=${nb.project_id}`;
            };
            item.oncontextmenu = (e) => {
                e.preventDefault();
                if (!this._selectedNotebooks.has(String(nb.id))) {
                    this._clearNotebookSelection(false);
                    this._toggleNotebookSelection(nb, item, nbIdx, true);
                }
                this.handleNotebookContextMenu(e, nb);
            };
            container.appendChild(item);
        });
        this._updateNotebookSelectionUI();
    },

    _selectNotebookRangeInList: function(endIndex) {
        const items = Array.from(document.querySelectorAll('#notebooks-list .notebook-item'));
        const start = this._lastSelectedNotebookIndex >= 0 ? this._lastSelectedNotebookIndex : 0;
        const minIdx = Math.min(start, endIndex);
        const maxIdx = Math.max(start, endIndex);
        items.forEach((item) => {
            const idx = parseInt(item.dataset.notebookIndex, 10);
            if (idx >= minIdx && idx <= maxIdx) {
                const notebookId = item.dataset.notebookId;
                const nb = this._findNotebookInCache(notebookId) || { id: notebookId, name: item.querySelector('.notebook-item-name')?.textContent || '' };
                this._selectedNotebooks.set(String(notebookId), nb);
            }
        });
        this._lastSelectedNotebookIndex = endIndex;
        this._updateNotebookSelectionUI();
    },

    refreshNotebooksList: async function(force = false) {
        const container = document.getElementById('notebooks-list');
        if (!container) return;

        const cached = !force ? this._getSidebarPanelCache('notebooks') : null;
        if (cached) {
            this._renderNotebooksList(container, cached.notebooks || []);
            return;
        }

        if (!force && this._sidebarPanelInFlight.notebooks) {
            return this._sidebarPanelInFlight.notebooks;
        }

        container.innerHTML = '<div class="nb-loading">' + (window.i18n ? window.i18n.t('notebook.loading') : 'Loading...') + '</div>';
        const loadPromise = (async () => {
            const params = new URLSearchParams();
            if (this.projectId) params.set('project_id', this.projectId);
            params.set('limit', '300');
            const url = `/api/notebooks?${params.toString()}`;

            const res = await fetch(url);
            const data = await res.json();
            const notebooks = Array.isArray(data.notebooks) ? data.notebooks : [];
            this._setSidebarPanelCache('notebooks', { notebooks: notebooks });
            this._renderNotebooksList(container, notebooks);
        })().catch((e) => {
            container.innerHTML = '<div class="nb-empty-state error">' + (window.i18n ? window.i18n.t('notebook.failedToLoadNotebooks') : 'Failed to load notebooks') + '</div>';
            this._invalidateSidebarPanelCache('notebooks');
        }).finally(() => {
            this._sidebarPanelInFlight.notebooks = null;
        });

        this._sidebarPanelInFlight.notebooks = loadPromise;
        return loadPromise;
    },

    handleNotebookContextMenu: function(e, nb) {
        let menu = document.getElementById('nb-context-menu');
        if (!menu) {
            menu = document.createElement('div');
            menu.id = 'nb-context-menu';
            menu.className = 'nb-context-menu';
            document.body.appendChild(menu);
            document.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                if (!menu.contains(e.target)) menu.classList.remove('active');
            });
        }

        const selectedCount = this._selectedNotebooks.size;
        const isMulti = selectedCount > 1;
        const createItem = (text, action, isDanger) => {
            const item = document.createElement('div');
            item.className = 'nb-context-item' + (isDanger ? ' danger' : '');
            item.textContent = text;
            item.addEventListener('click', () => { action(); menu.classList.remove('active'); });
            return item;
        };
        const createSeparator = () => {
            const sep = document.createElement('div');
            sep.className = 'nb-context-sep';
            return sep;
        };

        menu.textContent = '';

        if (isMulti) {
            menu.appendChild(createItem(window.i18n ? NbT('deleteNSelected', 'Delete {n} selected').replace('{n}', selectedCount) : `Delete ${selectedCount} selected`, () => this._bulkDeleteSelectedNotebooks(), true));
            menu.appendChild(createSeparator());
            menu.appendChild(createItem(NbT('clearSelection', 'Clear Selection'), () => this._clearNotebookSelection(true)));
        } else {
            menu.appendChild(createItem(NbT('open', 'Open'), () => {
                window.location.href = `/notebook?id=${nb.id}&project_id=${nb.project_id}`;
            }));
            menu.appendChild(createItem(window.i18n ? window.i18n.t('notebook.rename') : 'Rename', () => this._renameSelectedNotebook()));
            menu.appendChild(createItem(window.i18n ? window.i18n.t('notebook.duplicate') : 'Duplicate', () => this._duplicateSelectedNotebook()));
            menu.appendChild(createItem(NbT('moveToProject', 'Move to Project'), () => this._moveSelectedNotebook()));
            menu.appendChild(createSeparator());
            menu.appendChild(createItem(window.i18n ? window.i18n.t('notebook.delete') : 'Delete', () => this.deleteNotebook(nb.id), true));
        }

        menu.style.left = `${e.pageX}px`;
        menu.style.top = `${e.pageY}px`;
        menu.classList.add('active');
    },

    deleteNotebook: async function(id) {
        if (!confirm(window.i18n ? window.i18n.t('notebook.deleteNotebookConfirm') : 'Delete this notebook?')) return;
        try {
            await fetch(`/api/notebooks/${id}`, { method: 'DELETE' });
            this.refreshNotebooksList(true);
            if (this.notebook && this.notebook.id == id) {
                // Reload page if deleted current notebook
                window.location.href = '/notebook';
            }
        } catch (e) { alert(window.i18n ? window.i18n.t('notebook.deleteFailed') : 'Delete failed'); }
    },

    refreshKernelsList: async function(force = false) {
        const container = document.getElementById('kernels-list');
        if (!container) return;

        const cached = !force ? this._getSidebarPanelCache('kernels') : null;
        if (cached && container.childElementCount > 0) return;

        container.textContent = '';
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'nb-loading';
        loadingDiv.textContent = window.i18n ? window.i18n.t('notebook.loading') : 'Loading...';
        container.appendChild(loadingDiv);

        try {
            const res = await fetch('/api/notebooks/sessions');
            const data = await res.json();

            container.textContent = '';
            let sessions = data.sessions || [];

            // Client-side filter by project
            if (this.projectId) {
                sessions = sessions.filter(s => s.project_id == this.projectId);
            }
            this._setSidebarPanelCache('kernels', { sessions: sessions });

            if (sessions.length === 0) {
                const emptyDiv = document.createElement('div');
                emptyDiv.className = 'nb-empty-state';
                emptyDiv.textContent = window.i18n ? window.i18n.t('notebook.noActiveRuntimes') : 'No active runtimes';
                container.appendChild(emptyDiv);
                return;
            }

            sessions.forEach(s => {
                const item = document.createElement('div');
                const isActive = this.kernel && this.kernel.sessionId === s.session_id;
                item.className = `kernel-item ${isActive ? 'active' : ''}`;

                // Info section
                const infoDiv = document.createElement('div');
                infoDiv.className = 'kernel-item-info';

                const nameDiv = document.createElement('div');
                nameDiv.className = 'kernel-item-name';
                nameDiv.textContent = s.notebook_name || (window.i18n ? window.i18n.t('notebook.notebook') : 'Notebook ') + s.notebook_id;

                const statusDiv = document.createElement('div');
                statusDiv.style.cssText = 'font-size:11px; color:var(--nb-text-muted); display:flex; align-items:center; gap:4px;';

                const statusDot = document.createElement('span');
                statusDot.className = `nb-status-dot ${s.status}`;

                const statusText = document.createTextNode(s.status);

                statusDiv.appendChild(statusDot);
                statusDiv.appendChild(statusText);
                infoDiv.appendChild(nameDiv);
                infoDiv.appendChild(statusDiv);

                // Actions section
                const actionsDiv = document.createElement('div');
                actionsDiv.className = 'kernel-item-actions';
                actionsDiv.style.cssText = 'display:flex; gap:4px; margin-left:auto;';

                const mkIconBtn = (title, svgBuild, onClick) => {
                    const btn = document.createElement('button');
                    btn.className = 'nb-icon-btn small';
                    btn.title = title;
                    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                    svg.setAttribute('viewBox', '0 0 24 24');
                    svg.setAttribute('fill', 'none');
                    svg.setAttribute('stroke', 'currentColor');
                    svg.setAttribute('stroke-width', '1.5');
                    svgBuild(svg);
                    btn.appendChild(svg);
                    btn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        onClick();
                    });
                    return btn;
                };

                // Interrupt (halt running cell) — only useful while busy.
                if (s.status === 'busy') {
                    actionsDiv.appendChild(mkIconBtn(
                        window.i18n ? window.i18n.t('notebook.interruptRuntime') : 'Interrupt (halt running cell)',
                        (svg) => {
                            const bar1 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                            bar1.setAttribute('x', '8'); bar1.setAttribute('y', '6');
                            bar1.setAttribute('width', '2.5'); bar1.setAttribute('height', '12');
                            const bar2 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                            bar2.setAttribute('x', '13.5'); bar2.setAttribute('y', '6');
                            bar2.setAttribute('width', '2.5'); bar2.setAttribute('height', '12');
                            svg.appendChild(bar1); svg.appendChild(bar2);
                        },
                        () => this.interruptSession(s.session_id)
                    ));
                }

                // Restart runtime
                actionsDiv.appendChild(mkIconBtn(
                    window.i18n ? window.i18n.t('notebook.restartRuntime') : 'Restart Runtime',
                    (svg) => {
                        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                        path.setAttribute('d', 'M4 12a8 8 0 1 1 2.34 5.66M4 12V7m0 5h5');
                        path.setAttribute('stroke-linecap', 'round');
                        path.setAttribute('stroke-linejoin', 'round');
                        svg.appendChild(path);
                    },
                    () => this.restartSession(s.session_id)
                ));

                // Stop runtime
                actionsDiv.appendChild(mkIconBtn(
                    window.i18n ? window.i18n.t('notebook.stopRuntime') : 'Stop Runtime',
                    (svg) => {
                        const stopRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                        stopRect.setAttribute('x', '6');
                        stopRect.setAttribute('y', '6');
                        stopRect.setAttribute('width', '12');
                        stopRect.setAttribute('height', '12');
                        stopRect.setAttribute('rx', '1');
                        svg.appendChild(stopRect);
                    },
                    () => this.stopSession(s.session_id)
                ));

                item.appendChild(infoDiv);
                item.appendChild(actionsDiv);

                item.addEventListener('click', () => {
                    // Switch to this session's notebook
                    window.location.href = `/notebook?id=${s.notebook_id}&project_id=${s.project_id}`;
                });

                container.appendChild(item);
            });
        } catch (e) {
            container.textContent = '';
            const errorDiv = document.createElement('div');
            errorDiv.className = 'nb-empty-state error';
            errorDiv.textContent = window.i18n ? window.i18n.t('notebook.failedToLoadRuntimes') : 'Failed to load runtimes';
            container.appendChild(errorDiv);
            this._invalidateSidebarPanelCache('kernels');
        }
    },

    interruptSession: async function(sessionId) {
        try {
            const res = await fetch(`/api/notebooks/kernel/${sessionId}/interrupt`, { method: 'POST' });
            if (res.ok) {
                this.showToast(window.i18n ? window.i18n.t('notebook.runtimeInterrupted') : 'Runtime interrupted');
                this.refreshKernelsList(true);
            } else {
                const err = await res.json().catch(() => ({}));
                this.showToast((window.i18n ? window.i18n.t('notebook.failedToInterrupt') : 'Failed to interrupt: ') + (err.error || ''), 4000);
            }
        } catch (e) {
            console.error(e);
            this.showToast(window.i18n ? window.i18n.t('notebook.failedToInterrupt') : 'Failed to interrupt runtime', 3000);
        }
    },

    restartSession: async function(sessionId) {
        if (!confirm(window.i18n ? window.i18n.t('notebook.restartRuntimeConfirm') : 'Restart this runtime? All variables will be lost.')) return;
        // Restarting the runtime this tab is connected to goes through the
        // kernel client so pending executions and the socket room stay sane.
        if (this.kernel && this.kernel.sessionId === sessionId && this.notebookId) {
            await this.kernel.restart(this.notebookId);
            this.refreshKernelsList(true);
            return;
        }
        try {
            const res = await fetch(`/api/notebooks/kernel/${sessionId}/restart`, { method: 'POST' });
            if (res.ok) {
                this.showToast(window.i18n ? window.i18n.t('notebook.runtimeRestarted') : 'Runtime restarted');
            } else {
                const err = await res.json().catch(() => ({}));
                this.showToast((window.i18n ? window.i18n.t('notebook.failedToRestart') : 'Failed to restart: ') + (err.error || ''), 4000);
            }
        } catch (e) {
            console.error(e);
            this.showToast(window.i18n ? window.i18n.t('notebook.failedToRestart') : 'Failed to restart runtime', 3000);
        }
        this.refreshKernelsList(true);
    },

    stopSession: async function(sessionId) {
        if (!confirm(window.i18n ? window.i18n.t('notebook.stopRuntimeConfirm') : 'Stop this runtime?')) return;
        try {
            const res = await fetch(`/api/notebooks/kernel/${sessionId}/stop`, { method: 'POST' });
            if (res.ok) {
                this.showToast(window.i18n ? window.i18n.t('notebook.runtimeStopped') : 'Runtime stopped');
                this.refreshKernelsList(true);
                // If we stopped our own session, update UI
                if (this.kernel && this.kernel.sessionId === sessionId) {
                    this.kernel.sessionId = null;
                    this.kernel.updateUI('disconnected');
                }
            } else {
                const err = await res.json();
                alert((window.i18n ? window.i18n.t('notebook.failedToStop') : 'Failed to stop: ') + (err.error || (window.i18n ? window.i18n.t('notebook.unknownError') : 'Unknown error')));
            }
        } catch (e) {
            console.error(e);
            alert(window.i18n ? window.i18n.t('notebook.failedToStopRuntime') : 'Failed to stop runtime');
        }
    },

    stopAllSessions: async function() {
        if (!confirm(window.i18n ? window.i18n.t('notebook.stopAllRuntimes') : 'Stop all active runtimes?')) return;
        try {
            const res = await fetch('/api/notebooks/sessions/stop-all', { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                this.showToast(window.i18n ? window.i18n.t('notebook.stoppedNRuntimes').replace('{n}', data.stopped) : `Stopped ${data.stopped} runtime(s)`);
                this.refreshKernelsList(true);
                if (this.kernel) {
                    this.kernel.sessionId = null;
                    this.kernel.updateUI('disconnected');
                }
            } else {
                alert(window.i18n ? window.i18n.t('notebook.failedToStopRuntimes') : 'Failed to stop runtimes');
            }
        } catch (e) {
            console.error(e);
            alert(window.i18n ? window.i18n.t('notebook.failedToStopRuntimes') : 'Failed to stop runtimes');
        }
    },

    // ========================================================================
    // AI MODELS PANEL
    // ========================================================================


    _createModelItem: function(name, task, credits, color, isActive, encBadge, statusBadge, modelObj) {
        var item = document.createElement('div');
        var baseStyle = 'padding:8px 12px;display:flex;align-items:center;gap:10px;border-radius: var(--r-ui, 0px);margin:2px 6px;transition:background 0.15s;';
        if (!isActive) {
            baseStyle += 'opacity:0.5;cursor:default;';
        } else {
            baseStyle += 'cursor:pointer;';
        }
        item.style.cssText = baseStyle;
        if (isActive) {
            item.addEventListener('mouseenter', function() { this.style.background = 'rgba(255,255,255,0.05)'; });
            item.addEventListener('mouseleave', function() { this.style.background = 'transparent'; });
        }

        var dot = document.createElement('div');
        dot.style.cssText = 'width:8px;height:8px;border-radius: var(--r-ui, 0px);flex-shrink:0;background:' + color;
        item.appendChild(dot);

        var info = document.createElement('div');
        info.style.cssText = 'min-width:0;flex:1;';
        var nameEl = document.createElement('div');
        nameEl.style.cssText = 'font-size:13px;font-weight:600;color:#e4e4e7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
        nameEl.textContent = name;
        var metaEl = document.createElement('div');
        metaEl.style.cssText = 'font-size:11px;color:#71717a;';
        metaEl.textContent = task + ' · ' + (window.i18n ? window.i18n.t('notebook.creditsCount').replace('{n}', credits) : credits + ' credits');
        info.appendChild(nameEl);
        info.appendChild(metaEl);
        item.appendChild(info);

        if (!isActive) {
            var offBadge = document.createElement('span');
            offBadge.style.cssText = 'font-size:9px;padding:2px 6px;background:rgba(113,113,122,0.2);border-radius: var(--r-ui, 0px);color:#71717a;font-weight:600;';
            offBadge.textContent = statusBadge || (window.i18n ? window.i18n.t('notebook.off') : 'OFF');
            item.appendChild(offBadge);
        }
        if (encBadge) {
            var enc = document.createElement('span');
            enc.style.cssText = 'font-size:9px;padding:2px 6px;background:rgba(34,197,94,0.15);border-radius: var(--r-ui, 0px);color:#22c55e;font-weight:600;';
            enc.textContent = 'E2E';
            item.appendChild(enc);
        }

        if (isActive && modelObj) {
            item.setAttribute('data-model-id', String(modelObj.id || modelObj.short_name || name));
            item.addEventListener('click', (function(self, m) {
                return function(ev) {
                    ev.stopPropagation();
                    self._openModelInSplit(m);
                };
            })(this, modelObj));
        }

        return item;
    },

    _MODEL_SDK_EXAMPLES: {
        'esmfold': [
            {
                title: 'Predict structure from a sequence',
                code:
                    '# Predict a 3D protein structure from an amino-acid sequence\n' +
                    'import phonos\n' +
                    '\n' +
                    'sequence = "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSH"\n' +
                    'job = phonos.esmfold.run(sequence)\n' +
                    'job.wait()\n' +
                    'pdb_path = phonos.download_output(job.job_id, "structure.pdb")\n' +
                    'print("Saved to:", pdb_path)'
            },
            {
                title: 'Tune recycling iterations',
                code:
                    '# More recycles - usually higher accuracy, slower\n' +
                    'job = phonos.esmfold.run(\n' +
                    '    sequence="MKTAYIAKQRQISFVKSHRSRQHARE...",\n' +
                    '    num_recycles=8,\n' +
                    '    output_format="cif",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'esmfold-re': [
            {
                title: 'Refined ESMFold prediction',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.esmfold_re.run(\n' +
                    '    sequence="MKTAYIAKQRQISFVKSHRSRQHARE",\n' +
                    '    num_recycles=6,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'alphafold2': [
            {
                title: 'Monomer structure prediction',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.alphafold2.run(\n' +
                    '    sequence="MKKLVLAISLVGLLFISHCQAQEKFTF...",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'alphafold2-re': [
            {
                title: 'AlphaFold2 with MSA refinement',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.alphafold2_re.run(\n' +
                    '    sequence="MQTLKDFFAAYLEM...",\n' +
                    '    msa_mode="mmseqs2",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'alphafold2-re-multimer': [
            {
                title: 'Multi-chain complex prediction',
                code:
                    'import phonos\n' +
                    '\n' +
                    'sequences = [\n' +
                    '    "MQTLKDFFAAYLEM...",  # chain A\n' +
                    '    "GSHMSLKDFFAAY...",   # chain B\n' +
                    ']\n' +
                    '\n' +
                    'job = phonos.alphafold2_re_multimer.run(\n' +
                    '    sequences=sequences,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'alphafold2-multimer': [
            {
                title: 'Multi-chain complex prediction',
                code:
                    'import phonos\n' +
                    '\n' +
                    'sequences = [\n' +
                    '    "MQTLKDFFAAYLEM...",  # chain A\n' +
                    '    "GSHMSLKDFFAAY...",   # chain B\n' +
                    ']\n' +
                    '\n' +
                    'job = phonos.alphafold2_re_multimer.run(\n' +
                    '    sequences=sequences,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'rfdiffusion': [
            {
                title: 'De novo design (length range)',
                code:
                    'import phonos\n' +
                    '\n' +
                    '# Design a 60-120 residue protein\n' +
                    'job = phonos.rfdiffusion.run(\n' +
                    '    contigs="60-120",\n' +
                    '    num_designs=4,\n' +
                    ')\n' +
                    'job.wait()'
            },
            {
                title: 'Motif scaffolding around hotspots',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.rfdiffusion.run(\n' +
                    '    sequence="ACPFGH...",          # motif to scaffold around\n' +
                    '    contigs="40-60/0 40-60",      # length windows\n' +
                    '    hotspot_residue="40:A,55:A",  # pins the motif\n' +
                    '    num_designs=8,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'proteinmpnn': [
            {
                title: 'Inverse folding (fixed backbone)',
                code:
                    'import phonos\n' +
                    '\n' +
                    '# Provide a backbone PDB; MPNN proposes sequences\n' +
                    'job = phonos.proteinmpnn.run(\n' +
                    '    sequence="MQTLKDFFAAY...",       # positions to design\n' +
                    '    pdb_file="backbone.pdb",\n' +
                    '    temperature=0.1,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'proteinmpnn-re': [
            {
                title: 'Inverse folding with refinement',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.proteinmpnn_re.run(\n' +
                    '    sequence="MQTLKDFFAAY...",\n' +
                    '    pdb_file="backbone.pdb",\n' +
                    '    refine_cycles=3,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'diffdock': [
            {
                title: 'Dock SMILES ligand to uploaded protein',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.diffdock.run(\n' +
                    '    protein_file="protein.pdb",\n' +
                    '    ligand_smiles="CC(=O)OC1=CC=CC=C1C(=O)O",  # aspirin\n' +
                    ')\n' +
                    'job.wait()'
            },
            {
                title: 'Use a PDB ID and a SMILES ligand',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.diffdock.run(\n' +
                    '    protein_pdb_id="4hhb",\n' +
                    '    ligand_smiles="CCO",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'diffdock-re': [
            {
                title: 'DiffDock with refinement',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.diffdock_re.run(\n' +
                    '    protein_file="protein.pdb",\n' +
                    '    ligand_smiles="CC(=O)OC1=CC=CC=C1C(=O)O",\n' +
                    '    num_poses=20,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'boltz': [
            {
                title: 'Complex structure prediction',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.boltz.run(\n' +
                    '    sequences=[\n' +
                    '        "MQTLKDFFAAYLEM...",  # chain A\n' +
                    '        "GSHMSLKDFFAAY...",   # chain B\n' +
                    '    ],\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'boltz1': [
            {
                title: 'Boltz-1 complex prediction',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.boltz1.run(\n' +
                    '    sequences=["MQTLKDFFAAY..."],\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'boltz2': [
            {
                title: 'Predict a single protein',
                code:
                    'import phonos\n' +
                    '\n' +
                    'sequence = "MKTIIALSYIFCLVAD"\n' +
                    'job = phonos.boltz2.run(sequence)\n' +
                    'job.wait()\n' +
                    '\n' +
                    'out = phonos.download_output(job.job_id, "structure.cif")\n' +
                    'print(out)'
            },
            {
                title: 'Protein + ligand (CCD) with affinity',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.boltz2.run(\n' +
                    '    sequence="GIVEQCCTSICSLYQLENYCN",\n' +
                    '    ligand_input_type="ccd",\n' +
                    '    ligand_ccd="ATP",\n' +
                    '    predict_affinity=True,\n' +
                    '    recycling_steps=3,\n' +
                    ')\n' +
                    'job.wait()'
            },
            {
                title: 'Custom SMILES ligand',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.boltz2.run(\n' +
                    '    sequence="MQTLKDFFAAYLEM...",\n' +
                    '    ligand_input_type="smiles",\n' +
                    '    ligand_smiles="CC(=O)OC1=CC=CC=C1C(=O)O",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'evo': [
            {
                title: 'DNA/protein language model inference',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.evo.run(\n' +
                    '    sequence="ATGCGTACGTAGCTAG...",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'evo2': [
            {
                title: 'Masked-token evolution',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.evo2.run(\n' +
                    '    sequence="MVLSPADKTNVKAAWGKVGAHAGEYGAE...",\n' +
                    '    mask_rate=0.3,\n' +
                    '    num_iterations=2,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'molmim': [
            {
                title: 'Targeted molecular generation',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.molmim.run(\n' +
                    '    smiles="CC(=O)OC1=CC=CC=C1C(=O)O",\n' +
                    '    target_logp=2.5,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'genmol': [
            {
                title: 'Generate analogues of a seed molecule',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.genmol.run(\n' +
                    '    smiles="CCO",\n' +
                    '    num_molecules=16,\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'openfold3': [
            {
                title: 'Multi-chain complex with OpenFold3',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.openfold3.run(\n' +
                    '    sequences=[\n' +
                    '        "MQTLKDFFAAYLEM...",  # chain A\n' +
                    '        "GSHMSLKDFFAAY...",   # chain B\n' +
                    '    ],\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'msa-search': [
            {
                title: 'Homology search for an MSA',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.msa_search.run(\n' +
                    '    sequence="MVLSPADKTNVKAAWGKVGAHAGEYGAE...",\n' +
                    '    database="uniref100",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ],
        'msa search': [
            {
                title: 'Homology search for an MSA',
                code:
                    'import phonos\n' +
                    '\n' +
                    'job = phonos.msa_search.run(\n' +
                    '    sequence="MVLSPADKTNVKAAWGKVGAHAGEYGAE...",\n' +
                    '    database="uniref100",\n' +
                    ')\n' +
                    'job.wait()'
            }
        ]
    },

    _getModelShortName: function(model) {
        if (!model) return null;
        var s = (model.short_name || '').toLowerCase().replace(/[\s]+/g, '-');
        if (s) return s;
        var n = (model.name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-');
        return n;
    },

    _getModelExamples: function(model) {
        var key = this._getModelShortName(model);
        if (key && this._MODEL_SDK_EXAMPLES[key]) {
            return this._MODEL_SDK_EXAMPLES[key];
        }
        // Build a valid Python attribute name: replace hyphens and spaces with underscores
        var pyKey = (key || 'model').replace(/[- ]/g, '_');
        return [{
            title: 'Submit a job with the SDK',
            code:
                'import phonos\n' +
                '\n' +
                'job = phonos.' + pyKey + '.run(\n' +
                '    # model-specific parameters here\n' +
                ')\n' +
                'job.wait()'
        }];
    },

    _mEscapeHtml: function(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    _mHighlightPython: function(code) {
        var s = this._mEscapeHtml(code);
        // comments (#...)
        s = s.replace(/(^|\n)(#[^\n]*)/g, function(m, p1, p2) {
            return p1 + '<span class="m-tok-cmt">' + p2 + '</span>';
        });
        // strings (double or single quoted)
        s = s.replace(/(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;)/g, function(m) {
            return '<span class="m-tok-str">' + m + '</span>';
        });
        // keywords
        s = s.replace(/\b(import|from|as|def|return|for|in|if|elif|else|True|False|None)\b/g, '<span class="m-tok-kw">$1</span>');
        // function calls (phonos.X)
        s = s.replace(/\b(phonos)\.[a-zA-Z_][a-zA-Z0-9_]*/g, '<span class="m-tok-fn">$&</span>');
        return s;
    },

    _openModelInSplit: async function(model) {
        var panel = document.getElementById('split-panel');
        var contentEl = document.getElementById('split-content');
        var titleEl = document.getElementById('split-file-title');
        var saveBtn = document.getElementById('split-save-btn');
        if (!panel || !contentEl) return;

        // Tear down any CodeMirror instance from the file editor
        if (this._splitCM) {
            try { this._splitCM.toTextArea(); } catch (e) {}
            this._splitCM = null;
        }
        this._splitFileId = null;
        this._splitModelObj = model;

        var name = model.name || model.short_name || model.id || 'Model';
        var shortName = model.short_name || '';
        var id = model.id || '';
        var titleText = (shortName ? shortName : name);
        if (id) titleText += '  -  ' + id;

        if (titleEl) titleEl.textContent = titleText;
        if (saveBtn) saveBtn.style.display = 'none';

        // Reset any inline width left over from a previous file-editor resize
        panel.style.width = '';
        panel.style.minWidth = '';
        panel.style.maxWidth = '';

        contentEl.innerHTML = '';
        contentEl.classList.add('nb-split-model');
        contentEl.style.padding = '0';

        var body = document.createElement('div');
        body.className = 'nb-model-detail';
        body.innerHTML = this._renderModelDetailHtml(model);
        contentEl.appendChild(body);

        panel.style.display = '';
        this._updateSplitLayout();
        this._wireModelDetail(body);

        // Show toast so the user knows the panel opened (silent if focused)
        try { this.showToast(shortName ? (shortName + ' opened') : 'Model opened', 1500); } catch (e) {}
    },

    _renderModelDetailHtml: function(model) {
        var esc = this._mEscapeHtml.bind(this);
        var hl = this._mHighlightPython.bind(this);

        // Detect user-defined models: their detail object exposes model_name + project_id
        // but lacks platform-style fields. Render them with the same shell so users get
        // a consistent detail panel inside the IDE.
        var isUserModel = !!(model.model_name || model.project_id || model.model_type);

        var colorMap = {
            'type-0':'#3b82f6','type-2':'#10b981','type-3':'#a855f7','type-4':'#f59e0b',
            'type-5':'#ec4899','type-8':'#f97316','type-9':'#06b6d4','evo2':'#f97316',
            'type-12':'#8b5cf6','alphafold2-re':'#a1a1aa','alphafold2-re-multimer':'#a1a1aa',
            'esmfold':'#3b82f6','esmfold-re':'#60a5fa','boltz':'#ec4899','boltz1':'#ec4899','boltz2':'#ec4899',
            'diffdock':'#a855f7','diffdock-re':'#c084fc','rfdiffusion':'#f59e0b',
            'proteinmpnn':'#22c55e','proteinmpnn-re':'#4ade80','molmim':'#14b8a6','genmol':'#06b6d4',
            'evo':'#f97316','evo2':'#fb923c','openfold3':'#8b5cf6','msa-search':'#0ea5e9'
        };
        var color = isUserModel
            ? '#f97316'
            : (colorMap[model.id] || colorMap[(model.short_name || '').toLowerCase()] || '#6366f1');

        var displayName = model.name || model.short_name || model.model_name || model.id || 'Model';
        var displaySub = isUserModel
            ? ('custom' + (model.project_id ? ' · project ' + model.project_id : '') + (model.id ? ' · id ' + model.id : ''))
            : ((model.short_name || '') + (model.id ? '  -  ' + model.id : ''));
        var task = isUserModel ? 'custom' : ((model.task || 'analysis').replace(/_/g, ' '));
        var desc = model.description || (isUserModel ? 'Custom user-defined model' : '');
        var outExt = model.output_extension || model.output_format || '';
        var inputs = Array.isArray(model.input_modes) ? model.input_modes.join(', ') : '';
        var credit = model.credit_cost != null ? String(model.credit_cost) : '-';
        var status = model.available !== false && model.is_active !== false ? 'online' : 'offline';

        var html = '';
        html += '<div class="nb-md-tabs" role="tablist">'
              +   '<button class="nb-md-tab active" data-md-tab="overview" role="tab" aria-selected="true">Overview</button>'
              +   '<button class="nb-md-tab" data-md-tab="usage" role="tab" aria-selected="false">SDK Usage</button>'
              + '</div>';

        // OVERVIEW
        html += '<div class="nb-md-section active" data-md-section="overview">';

        // hero card with dot + name + short meta
        html += '<div class="nb-md-hero">'
              +   '<div class="nb-md-hero-dot" style="background:' + color + '"></div>'
              +   '<div class="nb-md-hero-info">'
              +     '<div class="nb-md-hero-title">' + esc(displayName) + '</div>'
              +     '<div class="nb-md-hero-sub">'
              +       esc(displaySub)
              +     '</div>'
              +   '</div>'
              + '</div>';

        if (desc) {
            html += '<div class="nb-md-desc">' + esc(desc) + '</div>';
        }

        // meta grid
        html += '<div class="nb-md-meta-grid">';
        var metaRows = [
            ['Task', task],
            ['Status', status],
            ['Credit cost', credit],
            ['Output', outExt || '-'],
            ['Inputs', inputs || '-']
        ];
        metaRows.forEach(function(row) {
            html += '<div class="nb-md-meta">'
                  +   '<div class="nb-md-meta-label">' + esc(row[0]) + '</div>'
                  +   '<div class="nb-md-meta-value">' + esc(row[1]) + '</div>'
                  + '</div>';
        });
        html += '</div>';

        // User-model action: open the dashboard submit-job wizard pre-bound to this model
        if (isUserModel && model.id) {
            var editUrl = '/dashboard#user-models?edit=' + encodeURIComponent(model.id);
            html += '<div class="nb-md-actions" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;">'
                  +   '<button class="nb-md-snippet-btn nb-md-primary" data-md-user-submit="' + parseInt(model.id) + '">'
                  +     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>'
                  +     'Submit Job'
                  +   '</button>'
                  +   '<button class="nb-md-snippet-btn" onclick="window.open(\'' + editUrl + '\', \'_self\')">'
                  +     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'
                  +     'Edit in Custom Models'
                  +   '</button>'
                  + '</div>';
        }

        // Parameters
        if (Array.isArray(model.custom_fields) && model.custom_fields.length) {
            html += '<div class="nb-md-h3">Parameters</div>';
            html += '<ul class="nb-md-param-list">';
            model.custom_fields.forEach(function(f) {
                var type = f.type || 'any';
                var def = f.default !== undefined && f.default !== null && f.default !== ''
                    ? ' = ' + esc(String(f.default))
                    : '';
                html += '<li class="nb-md-param">'
                      +   '<span class="nb-md-param-name">' + esc(f.name) + def + '</span>'
                      +   '<span class="nb-md-param-type">' + esc(type) + '</span>'
                      +   '<span class="nb-md-param-help">' + esc(f.help || f.label || '') + '</span>'
                      + '</li>';
            });
            html += '</ul>';
        }
        html += '</div>'; // /overview

        // USAGE
        html += '<div class="nb-md-section" data-md-section="usage">';
        html += '<div class="nb-md-h3">Getting started</div>';
        html += '<div class="nb-md-snippet">'
              +   '<div class="nb-md-snippet-head">'
              +     '<span class="nb-md-snippet-title">Import the SDK</span>'
              +     '<div class="nb-md-snippet-actions">'
              +       '<button class="nb-md-snippet-btn" data-md-copy="0">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
              +         'Copy'
              +       '</button>'
              +       '<button class="nb-md-snippet-btn nb-md-primary" data-md-insert="0">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>'
              +         'Insert'
              +       '</button>'
              +     '</div>'
              +   '</div>'
              +   '<pre class="nb-md-snippet-code">' + hl('import phonos') + '</pre>'
              + '</div>';

        html += '<div class="nb-md-h3" style="margin-top:16px">SDK Workflow</div>';
        html += '<div class="nb-md-snippet">'
              +   '<div class="nb-md-snippet-head">'
              +     '<span class="nb-md-snippet-title">Submit, wait &amp; download</span>'
              +     '<div class="nb-md-snippet-actions">'
              +       '<button class="nb-md-snippet-btn" data-md-copy="1">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
              +         'Copy'
              +       '</button>'
              +       '<button class="nb-md-snippet-btn nb-md-primary" data-md-insert="1">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>'
              +         'Insert'
              +       '</button>'
              +     '</div>'
              +   '</div>'
              +   '<pre class="nb-md-snippet-code">' + hl(
                  '# Standard workflow: submit -> wait -> download\n' +
                  'job = phonos.esmfold.run("MVLSPADKTNVKAAWGKVGAHAGEYGAE")\n' +
                  'job.wait(progress=True)        # block until done\n' +
                  '\n' +
                  '# Download output files\n' +
                  'phonos.download_output(job.job_id, "result.pdb")\n' +
                  '\n' +
                  '# Inspect job metadata\n' +
                  'print(job.status, job.credits_used, job.execution_time)') + '</pre>'
              + '</div>';

        html += '<div class="nb-md-snippet" style="margin-top:8px">'
              +   '<div class="nb-md-snippet-head">'
              +     '<span class="nb-md-snippet-title">List &amp; inspect outputs</span>'
              +     '<div class="nb-md-snippet-actions">'
              +       '<button class="nb-md-snippet-btn" data-md-copy="2">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
              +         'Copy'
              +       '</button>'
              +       '<button class="nb-md-snippet-btn nb-md-primary" data-md-insert="2">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>'
              +         'Insert'
              +       '</button>'
              +     '</div>'
              +   '</div>'
              +   '<pre class="nb-md-snippet-code">' + hl(
                  '# List all output files for a completed job\n' +
                  'artifacts = phonos.list_outputs(job.job_id)\n' +
                  'for a in artifacts:\n' +
                  '    print(a.filename, a.size, a.format)\n' +
                  '\n' +
                  '# Download every artifact\n' +
                  'for a in artifacts:\n' +
                  '    path = phonos.download_artifact(job.job_id, a.filename)\n' +
                  '    print("Downloaded:", path)') + '</pre>'
              + '</div>';

        html += '<div class="nb-md-snippet" style="margin-top:8px">'
              +   '<div class="nb-md-snippet-head">'
              +     '<span class="nb-md-snippet-title">Job helpers</span>'
              +     '<div class="nb-md-snippet-actions">'
              +       '<button class="nb-md-snippet-btn" data-md-copy="3">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
              +         'Copy'
              +       '</button>'
              +       '<button class="nb-md-snippet-btn nb-md-primary" data-md-insert="3">'
              +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>'
              +         'Insert'
              +       '</button>'
              +     '</div>'
              +   '</div>'
              +   '<pre class="nb-md-snippet-code">' + hl(
                  '# Re-fetch a job by ID later\n' +
                  'job = phonos.get_job("abc123def")\n' +
                  'print(job.status, job.is_complete)\n' +
                  '\n' +
                  '# Cancel a running job\n' +
                  'phonos.cancel_job(job.job_id)\n' +
                  '\n' +
                  '# Batch submit the same model multiple times\n' +
                  'jobs = phonos.batch_submit("esmfold", [\n' +
                  '    {"sequence": "MVLSPADKTNVKAA"},  # job 1\n' +
                  '    {"sequence": "MKTAYIAKQRQISF"},  # job 2\n' +
                  '])\n' +
                  'for j in jobs:\n' +
                  '    j.wait()\n' +
                  '    print(j.job_id, j.status, j.credits_used)') + '</pre>'
              + '</div>';

        var examples = this._getModelExamples(model);
        html += '<div class="nb-md-h3" style="margin-top:16px">Model examples</div>';
        examples.forEach(function(ex, idx) {
            var n = idx + 4; // offset for the 4 fixed snippets (import=0, workflow=1, list=2, helpers=3)
            html += '<div class="nb-md-snippet">'
                  +   '<div class="nb-md-snippet-head">'
                  +     '<span class="nb-md-snippet-title">' + esc(ex.title) + '</span>'
                  +     '<div class="nb-md-snippet-actions">'
                  +       '<button class="nb-md-snippet-btn" data-md-copy="' + n + '">'
                  +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
                  +         'Copy'
                  +       '</button>'
                  +       '<button class="nb-md-snippet-btn nb-md-primary" data-md-insert="' + n + '">'
                  +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>'
                  +         'Insert'
                  +       '</button>'
                  +     '</div>'
                  +   '</div>'
                  +   '<pre class="nb-md-snippet-code">' + hl(ex.code) + '</pre>'
                  + '</div>';
        });
        html += this._renderSdkReferenceHtml();
        html += '</div>'; // /usage

        return html;
    },

    _renderSdkReferenceHtml: function() {
        var esc = this._mEscapeHtml.bind(this);
        var cats = [
            {
                title: 'Job Management',
                fns: [
                    ['submit_job(model, *, name, **params)', 'Submit a job to a model. Returns a Job object.'],
                    ['batch_submit(model, items, *, name)', 'Submit multiple job inputs in one request. Returns a list of Job objects.'],
                    ['get_job(job_id)', 'Fetch the latest state for a job by ID. Returns a Job object.'],
                    ['get_job_info(job)', 'Accept a job_id string or Job object; return a fresh Job.'],
                    ['list_jobs(limit=50, *, status)', 'List recent jobs in the current project (newest first).'],
                    ['cancel_job(job_id)', 'Cancel a running/pending job. Returns True on success.'],
                    ['get_job_logs(job_id, limit=200)', 'Fetch execution log entries for a job.'],
                ]
            },
            {
                title: 'Outputs & Artifacts',
                fns: [
                    ['download_output(job, output_path)', 'Download a job\'s primary output file. Returns the local path.'],
                    ['download_artifact(job, artifact_name, output_path)', 'Download a specific named artifact from a job. Returns the local path.'],
                    ['list_outputs(job)', 'List every artifact (output file) produced by a job.'],
                ]
            },
            {
                title: 'Model Discovery',
                fns: [
                    ['list_models(refresh=False)', 'List all catalog models (cached after first call).'],
                    ['list_online_models(refresh=False)', 'List models currently available and active.'],
                    ['get_model(name_or_id, refresh=False)', 'Look up a single model by id, short_name, or alias.'],
                    ['models()', 'Return the live registry of per-model handles (snake_case keys).'],
                ]
            },
            {
                title: 'Project & User',
                fns: [
                    ['get_project_info()', 'Return the current kernel project context (project_id, api_url, etc.).'],
                    ['list_projects(refresh=False)', 'List projects the current user has access to.'],
                    ['get_user_info()', 'Return the current user\'s profile (credits, plan, storage, job counts).'],
                    ['get_credits()', 'Return current credit balance, or None if unknown.'],
                ]
            },
            {
                title: 'File Management',
                fns: [
                    ['list_project_files(path=\'/workspace\')', 'List files in the notebook workspace.'],
                    ['read_project_file(filename, path=\'/workspace\')', 'Read a file from the notebook workspace.'],
                    ['write_project_file(filename, content, path=\'/workspace\')', 'Write a file into the notebook workspace.'],
                ]
            },
            {
                title: 'Help',
                fns: [
                    ['help(topic=None)', 'Print detailed help for a feature, model, or the SDK in general.'],
                    ['docs(topic=None)', 'Return a structured Doc object with body, sections, and see_also.'],
                    ['api()', 'Return a structured programmatic reference for every public function.'],
                ]
            }
        ];
        var html = '<div class="nb-md-h3" style="margin-top:20px">SDK Reference</div>';
        cats.forEach(function(cat) {
            html += '<div class="nb-md-snippet" style="margin-top:8px">'
                  +   '<div class="nb-md-snippet-head">'
                  +     '<span class="nb-md-snippet-title">' + esc(cat.title) + '</span>'
                  +   '</div>'
                  +   '<div class="nb-md-ref-body">';
            cat.fns.forEach(function(fn) {
                html += '<div class="nb-md-ref-fn">'
                      +   '<code>' + esc(fn[0]) + '</code>'
                      +   '<span class="nb-md-ref-desc">' + esc(fn[1]) + '</span>'
                      + '</div>';
            });
            html += '</div></div>';
        });
        return html;
    },

    _wireModelDetail: function(root) {
        var self = this;
        var examples = this._getModelExamples(this._splitModelObj || {});
        // Build the code list with index 0 = "import phonos"
        var codes = [
            'import phonos',
            '# Standard workflow: submit -> wait -> download\n' +
            'job = phonos.esmfold.run("MVLSPADKTNVKAAWGKVGAHAGEYGAE")\n' +
            'job.wait(progress=True)        # block until done\n' +
            '\n' +
            '# Download output files\n' +
            'phonos.download_output(job.job_id, "result.pdb")\n' +
            '\n' +
            '# Inspect job metadata\n' +
            'print(job.status, job.credits_used, job.execution_time)',
            '# List all output files for a completed job\n' +
            'artifacts = phonos.list_outputs(job.job_id)\n' +
            'for a in artifacts:\n' +
            '    print(a.filename, a.size, a.format)\n' +
            '\n' +
            '# Download every artifact\n' +
            'for a in artifacts:\n' +
            '    path = phonos.download_artifact(job.job_id, a.filename)\n' +
            '    print("Downloaded:", path)',
            '# Re-fetch a job by ID later\n' +
            'job = phonos.get_job("abc123def")\n' +
            'print(job.status, job.is_complete)\n' +
            '\n' +
            '# Cancel a running job\n' +
            'phonos.cancel_job(job.job_id)\n' +
            '\n' +
            '# Batch submit the same model multiple times\n' +
            'jobs = phonos.batch_submit("esmfold", [\n' +
            '    {"sequence": "MVLSPADKTNVKAA"},  # job 1\n' +
            '    {"sequence": "MKTAYIAKQRQISF"},  # job 2\n' +
            '])\n' +
            'for j in jobs:\n' +
            '    j.wait()\n' +
            '    print(j.job_id, j.status, j.credits_used)'
        ].concat(examples.map(function(e) { return e.code; }));

        // Tabs
        root.querySelectorAll('.nb-md-tab').forEach(function(tab) {
            tab.addEventListener('click', function() {
                var name = tab.getAttribute('data-md-tab');
                root.querySelectorAll('.nb-md-tab').forEach(function(t) {
                    var on = t === tab;
                    t.classList.toggle('active', on);
                    t.setAttribute('aria-selected', on ? 'true' : 'false');
                });
                root.querySelectorAll('.nb-md-section').forEach(function(s) {
                    var on = s.getAttribute('data-md-section') === name;
                    s.classList.toggle('active', on);
                });
            });
        });

        // Buttons (delegated)
        root.addEventListener('click', function(ev) {
            var userSubmitBtn = ev.target.closest('[data-md-user-submit]');
            if (userSubmitBtn) {
                var modelId = parseInt(userSubmitBtn.getAttribute('data-md-user-submit'), 10);
                if (modelId && window.App && typeof window.App.submitJobToUserModel === 'function') {
                    window.App.submitJobToUserModel(modelId);
                }
                return;
            }

            var btn = ev.target.closest('[data-md-copy], [data-md-insert]');
            if (!btn) return;
            var idx = parseInt(btn.getAttribute('data-md-copy') || btn.getAttribute('data-md-insert'), 10);
            var code = codes[idx];
            if (code == null) return;

            if (btn.hasAttribute('data-md-copy')) {
                self._mCopy(code).then(function() {
                    var orig = btn.innerHTML;
                    btn.classList.add('copied');
                    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Copied';
                    setTimeout(function() {
                        btn.classList.remove('copied');
                        btn.innerHTML = orig;
                    }, 1400);
                }).catch(function() {});
            } else {
                self._mInsert(code);
            }
        });
    },

    _mCopy: function(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function(resolve, reject) {
            try {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                resolve();
            } catch (e) { reject(e); }
        });
    },

    _mInsert: function(code) {
        if (!this.notebook) return;
        var afterId = this.activeCellId || null;
        var cell = this.addCell('code', code, afterId);
        if (cell && cell.id) {
            this.setActiveCell(cell.id);
            var el = document.getElementById(cell.id);
            if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    },

    refreshModelsPanel: async function(force = false) {
        var container = document.getElementById('models-panel-content');
        if (!container) return;

        var cached = !force ? this._getSidebarPanelCache('models') : null;
        if (cached && container.childElementCount > 0) return;

        container.textContent = '';
        var loadingEl = document.createElement('div');
        loadingEl.style.cssText = 'padding:12px;text-align:center;color:#71717a;font-size:12px;';
        loadingEl.textContent = window.i18n ? window.i18n.t('notebook.loadingModels') : 'Loading models...';
        container.appendChild(loadingEl);

        try {
            var query = this.projectId ? ('?project_id=' + encodeURIComponent(this.projectId)) : '';
            var catalogRes = await fetch('/api/notebooks/models' + query, { credentials: 'same-origin' });
            if (!catalogRes.ok) {
                throw new Error('Notebook model catalog request failed');
            }

            var catalog = await catalogRes.json();
            var platformModels = Array.isArray(catalog.platform_models) ? catalog.platform_models : [];
            var userModels = Array.isArray(catalog.custom_models) ? catalog.custom_models : [];

            // Filter to ONLINE models only: available, not greyed-out, not inactive.
            // Greyed-out / soon / disabled / offline models are excluded from the
            // sidebar so users see only models they can actually run right now.
            var isOnline = function(m) {
                if (!m) return false;
                if (m.available === false) return false;
                if (m.is_active === false) return false;
                if (m.greyed_out) return false;
                if (m.runtime_status && /^(offline|down|unavailable|disabled)$/i.test(String(m.runtime_status))) return false;
                return true;
            };
            var onlinePlatform = platformModels.filter(isOnline);
            var onlineUserModels = userModels.filter(function(um) {
                if (!um) return false;
                if (um.is_active === false) return false;
                return true;
            });

            this._setSidebarPanelCache('models', {
                platform_models: platformModels,
                custom_models: userModels,
                online_platform: onlinePlatform,
                online_custom: onlineUserModels
            });

            container.textContent = '';
            var colorMap = {
                'type-0':'#3b82f6','type-2':'#10b981','type-3':'#a855f7','type-4':'#f59e0b',
                'type-5':'#ec4899','type-8':'#f97316','type-9':'#06b6d4','evo2':'#f97316',
                'type-12':'#8b5cf6','alphafold2-re':'#a1a1aa','alphafold2-re-multimer':'#a1a1aa'
            };

            // "Online Models" header — live status indicator
            if (onlinePlatform.length > 0 || onlineUserModels.length > 0) {
                var statusRow = document.createElement('div');
                statusRow.style.cssText = 'padding:8px 12px 4px;display:flex;align-items:center;justify-content:space-between;';
                var titleWrap = document.createElement('div');
                titleWrap.style.cssText = 'display:flex;align-items:center;gap:6px;';
                var dot = document.createElement('span');
                dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 6px rgba(34,197,94,0.7);display:inline-block;';
                titleWrap.appendChild(dot);
                var headerTitle = document.createElement('span');
                headerTitle.style.cssText = 'font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.05em;';
                headerTitle.textContent = window.i18n ? window.i18n.t('notebook.onlineModels') : 'Online Models';
                titleWrap.appendChild(headerTitle);
                statusRow.appendChild(titleWrap);

                var count = onlinePlatform.length + onlineUserModels.length;
                var countEl = document.createElement('span');
                countEl.style.cssText = 'font-size:10px;color:#52525b;font-weight:600;';
                countEl.textContent = String(count);
                statusRow.appendChild(countEl);
                container.appendChild(statusRow);
            }

            var self = this;
            if (onlinePlatform.length > 0) {
                var header = document.createElement('div');
                header.style.cssText = 'padding:6px 12px 4px;font-size:10px;font-weight:600;color:#52525b;text-transform:uppercase;letter-spacing:0.05em;';
                header.textContent = window.i18n ? window.i18n.t('notebook.platformModels') : 'Platform Models';
                container.appendChild(header);

                onlinePlatform.forEach(function(m) {
                    var c = colorMap[m.id] || '#6366f1';
                    var task = (m.task || 'analysis').replace(/_/g, ' ');
                    container.appendChild(self._createModelItem(
                        m.short_name || m.name, task, m.credit_cost || 10, c, true, false, null, m
                    ));
                });
            }

            if (onlineUserModels.length > 0) {
                var umHeader = document.createElement('div');
                umHeader.style.cssText = 'padding:10px 12px 4px;font-size:10px;font-weight:600;color:#52525b;text-transform:uppercase;letter-spacing:0.05em;border-top:1px solid rgba(255,255,255,0.06);margin-top:4px;';
                umHeader.textContent = window.i18n ? window.i18n.t('notebook.customModels') : 'Custom Models';
                container.appendChild(umHeader);

                onlineUserModels.forEach(function(um) {
                    container.appendChild(self._createModelItem(
                        um.model_name || 'Custom Model', (um.model_type || 'custom'), um.credit_cost || 10, '#f97316', true,
                        um.encryption_status === 'encrypted', null, um
                    ));
                });
            }

            if (onlinePlatform.length === 0 && onlineUserModels.length === 0) {
                var empty = document.createElement('div');
                empty.style.cssText = 'padding:24px 12px;text-align:center;color:#71717a;font-size:12px;';
                empty.textContent = window.i18n ? window.i18n.t('notebook.noOnlineModels') : 'No online models available';
                container.appendChild(empty);

                // Helpful hint if there are models but they're all offline
                if (platformModels.length > 0 || userModels.length > 0) {
                    var hint = document.createElement('div');
                    hint.style.cssText = 'padding:8px 12px;text-align:center;color:#52525b;font-size:10px;';
                    var offCount = (platformModels.length - onlinePlatform.length) + (userModels.length - onlineUserModels.length);
                    hint.textContent = (offCount + ' ' + (window.i18n ? window.i18n.t('notebook.offlineModelsHint') : 'model(s) offline or coming soon'));
                    container.appendChild(hint);
                }
            }

            var footer = document.createElement('div');
            footer.style.cssText = 'padding:12px;margin-top:8px;border-top:1px solid rgba(255,255,255,0.06);text-align:center;';
            var footerLink = document.createElement('a');
            footerLink.href = '/dashboard#submit-job';
            footerLink.style.cssText = 'font-size:12px;color:#10b981;text-decoration:none;';
            footerLink.textContent = window.i18n ? window.i18n.t('notebook.submitJob') : 'Submit Job \u2192';
            footer.appendChild(footerLink);
            container.appendChild(footer);
        } catch (e) {
            console.error('Failed to load models panel:', e);
            container.textContent = '';
            var errEl = document.createElement('div');
            errEl.style.cssText = 'padding:16px 12px;text-align:center;color:#ef4444;font-size:12px;';
            errEl.textContent = window.i18n ? window.i18n.t('notebook.failedToLoadModels') : 'Failed to load models';
            container.appendChild(errEl);
            this._invalidateSidebarPanelCache('models');
        }
    }
};

// ============================================================================
// TERMINAL CONNECTION
// ============================================================================

class TerminalConnection {
    constructor(module) {
        this.module = module;
        this.socket = null;
        this.term = null;
        this.fitAddon = null;
        this.container = null;
        this.connected = false;
        this.resizeObserver = null;
    }

    init(containerId) {
        this.container = document.getElementById(containerId);
        if (!this.container) return;

        // Clean up previous instance if any
        if (this.term) {
            this.term.dispose();
        }

        this.term = new Terminal({
            cursorBlink: true,
            theme: {
                background: '#09090b',
                foreground: '#f8f8f2',
                cursor: '#f8f8f2',
                selection: 'rgba(255, 255, 255, 0.3)'
            },
            fontFamily: 'Menlo, Monaco, "Courier New", monospace',
            fontSize: 13,
            lineHeight: 1.2
        });

        this.fitAddon = new FitAddon.FitAddon();
        this.term.loadAddon(this.fitAddon);
        this.term.open(this.container);
        this.fitAddon.fit();

        this.term.onData(data => {
            if (this.socket && this.connected) {
                this.socket.emit('terminal:input', { data: data });
            }
        });

        // Handle resize
        this.resizeObserver = new ResizeObserver(() => {
            if (this.fitAddon) {
                this.fitAddon.fit();
                if (this.socket && this.connected) {
                    this.socket.emit('terminal:resize', {
                        cols: this.term.cols,
                        rows: this.term.rows
                    });
                }
            }
        });
        this.resizeObserver.observe(this.container);
    }

    connect() {
        if (this.connected) return;
        
        // We reuse the kernel socket connection if available, or create a new one?
        // Actually, the backend 'socket_handlers_notebook.py' handles terminal events on the same namespace '/notebook'.
        // So we should use the existing socket from KernelManager if possible, or wait for it.
        
        if (!this.module.kernel.socket) {
            if (this.term) this.term.writeln('\r\n\x1b[33mWaiting for connection...\x1b[0m\r\n');
            return;
        }

        this.socket = this.module.kernel.socket;

        if (!this.module.kernel.sessionId) {
             if (this.term) this.term.writeln('\r\n\x1b[33mNo active kernel session. Start a kernel first.\x1b[0m\r\n');
             return;
        }

        if (this.term) this.term.writeln('\r\n\x1b[32mConnecting to terminal...\x1b[0m\r\n');
        
        this.socket.emit('terminal:start', { 
            session_id: this.module.kernel.sessionId,
            cols: this.term ? this.term.cols : 80,
            rows: this.term ? this.term.rows : 24
        });

        // Set up listeners (ensure we don't duplicate if called multiple times)
        this.socket.off('terminal:output');
        this.socket.off('terminal:ready');
        this.socket.off('terminal:error');

        this.socket.on('terminal:output', (data) => {
            if (this.term) this.term.write(data.data);
        });
        
        this.socket.on('terminal:ready', () => {
            this.connected = true;
            if (this.term) {
                this.term.writeln('\r\n\x1b[32mTerminal connected.\x1b[0m\r\n');
                this.term.focus();
            }
        });

        this.socket.on('terminal:error', (data) => {
            if (this.term) this.term.writeln(`\r\n\x1b[31mError: ${data.message}\x1b[0m\r\n`);
        });
    }

    disconnect() {
        if (this.socket) {
            this.socket.emit('terminal:stop');
            this.socket.off('terminal:output');
            this.socket.off('terminal:ready');
            this.socket.off('terminal:error');
        }
        this.connected = false;
        if (this.term) {
            this.term.writeln('\r\n\x1b[33mTerminal disconnected.\x1b[0m\r\n');
        }
    }
    
    fit() {
        if (this.fitAddon) this.fitAddon.fit();
    }
}

// ============================================================================ 
// KERNEL MANAGER
// ============================================================================ 

class KernelManager {
    constructor(module) {
        this.module = module;
        this.socket = null;
        this.sessionId = null;
        this.pendingExecutions = new Map();
        this._executionFallbackTimers = new Map();
        this._pendingExecutionPromises = new Map();
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 3;
        this.connectionStatus = 'disconnected';
        this._connectPromise = null;
        this._joinPromise = null;
        this._joinResolve = null;
        this._joinReject = null;
        this._joinTimeoutHandle = null;
        this._joinTimeoutMs = 90000;
        this._httpTimeoutMs = 60000;
        this._socketJoinGraceMs = 60000;
    }

    isConnected() {
        return this.socket && this.socket.connected && this.sessionId;
    }

    isConnecting() {
        return this.connectionStatus === 'connecting' || !!this._connectPromise;
    }

    waitForConnection() {
        if (this.isConnected()) return Promise.resolve(true);
        return this._connectPromise || Promise.resolve(false);
    }

    _beginJoinWait() {
        this._clearJoinWait();
        this._joinPromise = new Promise((resolve, reject) => {
            this._joinResolve = resolve;
            this._joinReject = reject;
        });
        this._joinTimeoutHandle = setTimeout(() => {
            this._rejectJoinWait(new Error('Kernel startup timed out after ' + Math.round(this._joinTimeoutMs / 1000) + 's — backend may be slow to allocate the runtime container.'));
        }, this._joinTimeoutMs);
    }

    _resolveJoinWait() {
        if (this._joinResolve) {
            this._joinResolve(true);
        }
        this._clearJoinWait();
    }

    _rejectJoinWait(error) {
        if (this._joinReject) {
            this._joinReject(error instanceof Error ? error : new Error(String(error || 'Kernel connection failed')));
        }
        this._clearJoinWait();
    }

    _clearJoinWait() {
        if (this._joinTimeoutHandle) {
            clearTimeout(this._joinTimeoutHandle);
        }
        this._joinPromise = null;
        this._joinResolve = null;
        this._joinReject = null;
        this._joinTimeoutHandle = null;
    }

    async connect(notebookId, options = {}) {
        if (!notebookId || notebookId === 'undefined' || notebookId === 'null') {
            this.module.showToast('Invalid notebook ID', 3000);
            return false;
        }
        if (this.isConnected()) {
            return true;
        }

        if (this._connectPromise) {
            return this._connectPromise;
        }

        this._connectPromise = this._connectInternal(notebookId, options);
        try {
            return await this._connectPromise;
        } finally {
            this._connectPromise = null;
        }
    }

    async _connectInternal(notebookId, options = {}) {
        if (!notebookId || notebookId === 'undefined' || notebookId === 'null') {
            throw new Error('Invalid notebookId: ' + notebookId);
        }
        const forceRestart = options.forceRestart || false;
        var runtimeLevel = options.runtimeLevel || null;

        // Get runtime from settings if not explicitly provided
        if (!runtimeLevel) {
            try {
                if (window.NotebookModule && window.NotebookModule._getEffectiveSettings) {
                    var settings = window.NotebookModule._getEffectiveSettings();
                    runtimeLevel = settings.defaultRuntime || 'python';
                }
            } catch (e) {
                // Fallback
            }
        }
        if (!runtimeLevel) runtimeLevel = 'python';
        this._activeRuntime = runtimeLevel;

        this.updateUI('connecting');
        this.connectionStatus = 'connecting';

        try {
            if (this.socket) {
                this.socket.disconnect();
                this.socket = null;
            }

            const endpoint = `/api/notebooks/${notebookId}/kernel/start`;

            const startKernelWithRetry = async (attempt = 1) => {
                const maxAttempts = 3;
                const controller = new AbortController();
                const httpTimer = setTimeout(() => controller.abort(), this._httpTimeoutMs);
                let res;
                try {
                    res = await fetch(endpoint, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            runtime_level: runtimeLevel,
                            force_restart: forceRestart
                        }),
                        signal: controller.signal,
                        credentials: 'same-origin'
                    });
                } catch (netErr) {
                    clearTimeout(httpTimer);
                    const isAbort = (netErr && netErr.name === 'AbortError');
                    const message = isAbort
                        ? `Kernel start timed out after ${Math.round(this._httpTimeoutMs / 1000)}s`
                        : ('Network error: ' + (netErr && netErr.message ? netErr.message : netErr));
                    if (attempt < maxAttempts) {
                        const waitMs = 2000 * attempt;
                        console.warn(`Kernel start attempt ${attempt} failed (${message}); retrying in ${waitMs}ms...`);
                        this.module.showToast((window.i18n ? window.i18n.t('notebook.kernelStartingRetry').replace('{n}', String(attempt)) : `Kernel start retry ${attempt}/3...`), 2000);
                        await new Promise(r => setTimeout(r, waitMs));
                        return startKernelWithRetry(attempt + 1);
                    }
                    throw new Error(message);
                }
                clearTimeout(httpTimer);

                if (!res.ok) {
                    const errorData = await res.json().catch(() => ({}));
                    const errMsg = errorData.error || ('Failed to start kernel (HTTP ' + res.status + ')');
                    // Retry on transient Docker / race-condition errors
                    const transient = /start|race|timeout|docker|connection/i.test(errMsg);
                    if (attempt < maxAttempts && transient) {
                        const waitMs = 2000 * attempt;
                        console.warn(`Kernel start attempt ${attempt} failed (${errMsg}); retrying in ${waitMs}ms...`);
                        this.module.showToast((window.i18n ? window.i18n.t('notebook.kernelStartingRetry').replace('{n}', String(attempt)) : `Kernel start retry ${attempt}/3...`), 2000);
                        await new Promise(r => setTimeout(r, waitMs));
                        return startKernelWithRetry(attempt + 1);
                    }
                    throw new Error(errMsg);
                }
                return res.json();
            };

            const data = await startKernelWithRetry();
            this.sessionId = data.session_id;
            if (data.runtime_level) this._activeRuntime = data.runtime_level;
            this.reconnectAttempts = 0;

            this._beginJoinWait();
            this.setupSocket();
            this.module.showToast(window.i18n ? window.i18n.t('notebook.kernelStarting') : 'Kernel starting...', 2000);
            if (this._joinPromise) {
                await this._joinPromise;
            }

        } catch (e) {
            console.error('Kernel connection error:', e);
            this.connectionStatus = 'error';
            this.updateUI('error');
            this._rejectJoinWait(e);
            this.module.showToast((window.i18n ? window.i18n.t('notebook.failedToStartKernel') : 'Failed to start kernel: ') + e.message, 4000);
            return false;
        }

        return true;
    }

    _showRuntimeSelector(runtimes) {
        return new Promise(function(resolve) {
            // Create popover overlay
            var overlay = document.createElement('div');
            overlay.className = 'runtime-selector-overlay';

            var popover = document.createElement('div');
            popover.className = 'runtime-selector-popover';

            var title = document.createElement('h3');
            title.className = 'runtime-selector-title';
            title.textContent = window.i18n ? window.i18n.t('notebook.selectRuntime') : 'Select Runtime';
            popover.appendChild(title);

            var desc = document.createElement('p');
            desc.className = 'runtime-selector-desc';
            desc.textContent = window.i18n ? window.i18n.t('notebook.chooseRuntime') : 'Choose a runtime level for this kernel session:';
            popover.appendChild(desc);

            runtimes.forEach(function(rt) {
                var btn = document.createElement('button');
                btn.className = 'runtime-option-btn';

                var nameSpan = document.createElement('span');
                nameSpan.className = 'runtime-option-name';
                nameSpan.textContent = rt.display_name;
                btn.appendChild(nameSpan);

                var info = [];
                if (rt.mem_limit) info.push(rt.mem_limit);
                if (rt.cpu_quota) info.push(rt.cpu_quota + (window.i18n ? ' ' + window.i18n.t('notebook.cpu') : ' CPU'));
                if (rt.gpu_enabled) info.push(window.i18n ? window.i18n.t('notebook.gpu') : 'GPU');
                if (info.length > 0) {
                    var detailSpan = document.createElement('span');
                    detailSpan.className = 'runtime-option-detail';
                    detailSpan.textContent = info.join(' / ');
                    btn.appendChild(detailSpan);
                }

                btn.addEventListener('click', function() {
                    overlay.remove();
                    resolve(rt.name);
                });
                popover.appendChild(btn);
            });

            var cancelBtn = document.createElement('button');
            cancelBtn.className = 'runtime-cancel-btn';
            cancelBtn.textContent = window.i18n ? window.i18n.t('notebook.cancel') : 'Cancel';
            cancelBtn.addEventListener('click', function() {
                overlay.remove();
                resolve(null);
            });
            popover.appendChild(cancelBtn);

            overlay.appendChild(popover);
            overlay.addEventListener('click', function(e) {
                if (e.target === overlay) {
                    overlay.remove();
                    resolve(null);
                }
            });
            document.body.appendChild(overlay);
        });
    }

    setupSocket() {
        this.socket = io('/notebook', {
            transports: ['websocket', 'polling'],
            reconnection: true,
            reconnectionAttempts: this.maxReconnectAttempts,
            reconnectionDelay: 1000
        });
        
        this.socket.on('connect', () => {
            console.log('Socket connected, joining session:', this.sessionId);
            this.socket.emit('notebook:join', { session_id: this.sessionId });
            this.connectionStatus = 'connecting';
            // Bump the join timeout once we have a live socket — backend may
            // still be waiting for the Docker container to report readiness.
            if (this._joinTimeoutHandle && this._joinTimeoutMs < this._socketJoinGraceMs) {
                clearTimeout(this._joinTimeoutHandle);
                this._joinTimeoutHandle = setTimeout(() => {
                    this._rejectJoinWait(new Error('Kernel startup timed out after socket connect — runtime container did not become ready in time.'));
                }, this._socketJoinGraceMs);
            }
        });

        this.socket.on('notebook:joined', (data) => {
            console.log('Joined kernel session:', data);
            this.connectionStatus = 'connected';
            this._resolveJoinWait();
            this.updateUI('idle');
            this.module.showToast(window.i18n ? window.i18n.t('notebook.kernelReady') : 'Kernel ready', 2000);
        });

        this.socket.on('notebook:error', (data) => {
            const message = (data && data.message) ? data.message : 'Notebook kernel error';
            console.error('Notebook socket error:', data);
            if (this.connectionStatus === 'connecting') {
                this._rejectJoinWait(new Error(message));
                this.connectionStatus = 'error';
                this.updateUI('error');
            }
            if (!data || !data.cell_id) {
                this.module.showToast(message, 4000);
            }
        });
        
        this.socket.on('notebook:kernel_status', (data) => {
            console.log('Kernel status:', data.status);
            if (data && data.runtime_level) {
                this._activeRuntime = data.runtime_level;
            }
            if (data && data.backend_type) {
                this._kernelBackendType = data.backend_type;
            }
            this.updateUI(data.status);
        });
        
        this.socket.on('connect_error', (err) => {
            console.error('Socket connection error:', err);
            this.reconnectAttempts++;
            if (this.reconnectAttempts >= this.maxReconnectAttempts) {
                this.connectionStatus = 'error';
                this._rejectJoinWait(new Error('Connection lost while starting kernel'));
                this.updateUI('error');
                this.module.showToast(window.i18n ? window.i18n.t('notebook.connectionLost') : 'Connection lost. Please restart the kernel.', 5000);
            }
        });
        
        this.socket.on('disconnect', (reason) => {
            console.log('Socket disconnected:', reason);
            if (this.connectionStatus === 'connecting') {
                this._rejectJoinWait(new Error('Disconnected while starting kernel'));
            }
            this.connectionStatus = 'disconnected';
            // Stop all live timers when disconnected
            if (this.module && this.module._liveTimers) {
                for (var cellId in this.module._liveTimers) {
                    this.module.stopLiveTimer(cellId);
                }
            }
            if (reason === 'io server disconnect') {
                this.socket.connect();
            }
        });
        
        this.socket.on('reconnect', () => {
            console.log('Socket reconnected');
            if (this.sessionId) {
                this.socket.emit('notebook:join', { session_id: this.sessionId });
            }
        });
        
        this.socket.on('notebook:execute_started', (data) => {
            this.pendingExecutions.set(data.msg_id, data.cell_id);
            const cell = this.module.notebook.getCell(data.cell_id);
            if (cell) {
                cell.execution_count = data.execution_count;
                this.module.updateCellOutputUI(cell);

                const cellEl = document.getElementById(data.cell_id);
                if (cellEl) {
                    cellEl.classList.remove('completed', 'error');
                    cellEl.classList.add('running');
                }
            }

            const cellId = data.cell_id;
            const msgId = data.msg_id;

            // Re-key the pending promise from cell_id to msg_id so overlapping
            // runs of the same cell resolve independently.
            const resolver = this._pendingExecutionPromises.get(cellId);
            if (resolver) {
                this._pendingExecutionPromises.delete(cellId);
                this._pendingExecutionPromises.set(msgId, resolver);
            }

            const timeoutMs = 400000;
            const fallbackTimer = setTimeout(() => {
                if (this.pendingExecutions.has(msgId)) {
                    this.pendingExecutions.delete(msgId);
                    const cellEl = document.getElementById(cellId);
                    if (cellEl && cellEl.classList.contains('running')) {
                        cellEl.classList.remove('running');
                        cellEl.classList.add('completed');
                        console.warn(`Cell ${cellId} marked as completed (fallback timeout after ${timeoutMs}ms)`);
                    }
                    const pending = this._pendingExecutionPromises.get(msgId);
                    if (pending) {
                        pending({ status: 'ok', fallback: true });
                        this._pendingExecutionPromises.delete(msgId);
                    }
                    this.updateUI('idle');
                }
            }, timeoutMs);
            this._executionFallbackTimers.set(msgId, fallbackTimer);
        });

        this.socket.on('notebook:output_replay_start', () => {
            // A fresh replay batch is starting. The server buffer now holds
            // only the latest run of each cell, so clear a cell's rendered
            // outputs the first time this batch touches it (see below), then
            // append. This makes reconnect replay idempotent instead of piling
            // duplicate/stale outputs — including old errors — onto cells.
            this._replayClearedCells = new Set();
        });

        this.socket.on('notebook:output', (data) => {
            // Prefer pendingExecutions (active execution), fall back to
            // data.cell_id for replayed buffered outputs after reconnect.
            const cellId = this.pendingExecutions.get(data.msg_id) || data.cell_id;
            if (cellId) {
                const cell = this.module.notebook.getCell(cellId);
                if (cell) {
                    // On the first replayed output for a cell in this batch,
                    // discard whatever it currently shows so the replay is a
                    // fresh, authoritative render rather than an append.
                    if (data.replay && this._replayClearedCells && !this._replayClearedCells.has(cellId)) {
                        cell.clearOutputs();
                        this._replayClearedCells.add(cellId);
                    }
                    cell.addOutput(data);
                    this.module.updateCellOutputUI(cell);
                    if (this.module.notebook) this.module.notebook.dirty = true;
                }
            }
        });

        this.socket.on('notebook:execute_reply', (data) => {
            const msgId = data.msg_id;
            const cellId = this.pendingExecutions.get(msgId) || data.cell_id;
            this.pendingExecutions.delete(msgId);

            const fallbackTimer = this._executionFallbackTimers.get(msgId);
            if (fallbackTimer) {
                clearTimeout(fallbackTimer);
                this._executionFallbackTimers.delete(msgId);
            }

            // Cells often write files into /workspace — surface them in the
            // explorer without a manual refresh.
            if (window.NotebookModule && NotebookModule._scheduleWorkspaceRefresh) {
                NotebookModule._scheduleWorkspaceRefresh();
            }

            if (cellId) {
                const cellEl = document.getElementById(cellId);
                if (cellEl) {
                    cellEl.classList.remove('running');
                    if (data.status === 'error') {
                        cellEl.classList.add('error');
                    } else {
                        cellEl.classList.add('completed');
                    }
                }

                // Resolve the pending execution promise so runAllCells
                // waits for each cell before dispatching the next. Prefer the
                // msg_id key (set after execute_started); fall back to cell_id
                // for older or replayed messages.
                let pending = this._pendingExecutionPromises.get(msgId);
                if (!pending && cellId) {
                    pending = this._pendingExecutionPromises.get(cellId);
                }
                if (pending) {
                    pending(data);
                    this._pendingExecutionPromises.delete(msgId);
                    this._pendingExecutionPromises.delete(cellId);
                }

                // If the cell produced no output, make sure it is not left
                // looking empty/incomplete by adding a minimal "no output" marker.
                const cell = this.module.notebook.getCell(cellId);
                if (cell && data.status === 'ok' && (!cell.outputs || cell.outputs.length === 0)) {
                    cell.addOutput({
                        output_type: 'stream',
                        name: 'stdout',
                        text: ''
                    });
                    this.module.updateCellOutputUI(cell);
                }
            }

            if (data.status === 'ok' || data.status === 'error') {
                this.updateUI('idle');
                if (this.module.notebook) {
                    this.module.notebook.dirty = true;
                    this.module.scheduleOutputSave();
                }
            }
        });

        this.socket.on('notebook:output_replay_done', (data) => {
            console.log(`Output replay complete: ${data.count} messages replayed`);
            this._replayClearedCells = null;
        });

        this.socket.on('notebook:input_request', (data) => {
            this.module.showInputDialog(data);
        });

        this.socket.on('notebook:timeout_warning', (data) => {
            const cellId = data.cell_id;
            const remaining = data.remaining_seconds;
            this.module.showToast((window.i18n ? window.i18n.t('notebook.cellWillTimeout').replace('{n}', remaining) : `Cell will timeout in ${remaining}s`), 2000);
        });

        this.socket.on('notebook:cell_timeout', (data) => {
            const cellId = data.cell_id;
            console.log('Cell timed out:', cellId);
            this.module.showToast(window.i18n ? window.i18n.t('notebook.cellTimedOut') : 'Cell execution timed out', 3000);

            let resolvedByMsgId = false;
            for (const [msgId, storedCellId] of this.pendingExecutions.entries()) {
                if (storedCellId === cellId) {
                    this.pendingExecutions.delete(msgId);
                    const fallbackTimer = this._executionFallbackTimers.get(msgId);
                    if (fallbackTimer) {
                        clearTimeout(fallbackTimer);
                        this._executionFallbackTimers.delete(msgId);
                    }
                    const pending = this._pendingExecutionPromises.get(msgId);
                    if (pending) {
                        pending({ status: 'error', timeout: true });
                        this._pendingExecutionPromises.delete(msgId);
                        resolvedByMsgId = true;
                    }
                }
            }

            const cellEl = document.getElementById(cellId);
            if (cellEl) {
                cellEl.classList.remove('running');
                cellEl.classList.add('error');
            }

            if (!resolvedByMsgId) {
                const pending = this._pendingExecutionPromises.get(cellId);
                if (pending) {
                    pending({ status: 'error', timeout: true });
                    this._pendingExecutionPromises.delete(cellId);
                }
            }
            this.updateUI('idle');
        });

        const onKernelInterrupted = (data) => {
            console.log('Kernel interrupted');
            this._clearPendingExecutions({ interrupted: true });
            this.updateUI('idle');
            this.module.showToast(window.i18n ? window.i18n.t('notebook.kernelInterrupted') : 'Kernel interrupted', 2000);
        };
        this.socket.on('notebook:kernel_interrupt', onKernelInterrupted);
        // Backend also emits 'notebook:interrupted' (legacy name) — handle both.
        this.socket.on('notebook:interrupted', onKernelInterrupted);

        this.socket.on('notebook:restarted', (data) => {
            console.log('Kernel restarted:', data);
            this._clearPendingExecutions({ restarted: true });
            if (data && data.runtime_level) this._activeRuntime = data.runtime_level;
            if (data && data.new_session_id && data.new_session_id !== this.sessionId) {
                this.sessionId = data.new_session_id;
                if (this.socket && this.socket.connected) {
                    this.socket.emit('notebook:join', { session_id: this.sessionId });
                }
            }
            this.connectionStatus = 'connected';
            this.updateUI('idle');
            this.module.showToast(window.i18n ? window.i18n.t('notebook.kernelRestarted') : 'Kernel restarted', 2000);
        });

        this.socket.on('notebook:kernel_shutdown', (data) => {
            console.log('Kernel shutdown:', data);
            this.connectionStatus = 'disconnected';
            this.sessionId = null;
            this.updateUI('disconnected');
            this.module.showToast(NbT('kernelDisconnected', 'Kernel disconnected'), 3000);
        });
    }

    async disconnect() {
        if (!this.socket && !this.sessionId) {
            return true;
        }
        
        try {
            this._rejectJoinWait(new Error('Kernel connection cancelled'));
            if (this.sessionId) {
                await fetch(`/api/notebooks/kernel/${this.sessionId}/stop`, { 
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                }).catch(() => {});
            }
            
            if (this.socket) {
                this.socket.disconnect();
                this.socket = null;
            }
            
            this.sessionId = null;
            this._clearPendingExecutions({ disconnected: true });
            this.connectionStatus = 'disconnected';
            this.updateUI('disconnected');

            this.module.showToast(NbT('kernelDisconnected', 'Kernel disconnected'), 2000);
            return true;
        } catch (e) {
            console.error('Error disconnecting kernel:', e);
            return false;
        }
    }

    // Resolve all in-flight execution promises and clear running-cell UI
    // state. Used by restart/interrupt/shutdown paths.
    _clearPendingExecutions(reason) {
        this.pendingExecutions.forEach((cellId) => {
            this.module.stopLiveTimer(cellId);
            const cellEl = document.getElementById(cellId);
            if (cellEl) cellEl.classList.remove('running');
        });
        this.pendingExecutions.clear();
        this._executionFallbackTimers.forEach(t => clearTimeout(t));
        this._executionFallbackTimers.clear();
        this._pendingExecutionPromises.forEach((resolve) => {
            resolve(Object.assign({ status: 'error' }, reason));
        });
        this._pendingExecutionPromises.clear();
    }

    async restart(notebookId) {
        if (!notebookId || notebookId === "undefined" || notebookId === "null") {
            this.module.showToast("Invalid notebook ID", 3000);
            return false;
        }
        this.module.showToast(window.i18n ? window.i18n.t('notebook.restartingKernel') : 'Restarting kernel...', 2000);

        // Fast path: server-side restart endpoint. In-place restarts keep the
        // same session token, ZMQ ports, and socket room — no reconnect churn.
        if (this.sessionId && this.socket && this.socket.connected) {
            this._clearPendingExecutions({ restarted: true });
            this.updateUI('restarting');
            try {
                const res = await fetch(`/api/notebooks/kernel/${this.sessionId}/restart`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin'
                });
                if (res.ok) {
                    const data = await res.json();
                    if (data.runtime_level) this._activeRuntime = data.runtime_level;
                    if (data.session_id && data.session_id !== this.sessionId) {
                        // Full restart produced a new session: rejoin its room
                        // on the existing socket.
                        this.sessionId = data.session_id;
                        this.socket.emit('notebook:join', { session_id: this.sessionId });
                    }
                    this.connectionStatus = 'connected';
                    this.updateUI('idle');
                    this.module.showToast(window.i18n ? window.i18n.t('notebook.kernelRestarted') : 'Kernel restarted', 2000);
                    return true;
                }
                console.warn('Kernel restart endpoint failed (HTTP ' + res.status + '); falling back to reconnect');
            } catch (e) {
                console.warn('Kernel restart endpoint error; falling back to reconnect', e);
            }
        }

        // Fallback: tear down and reconnect with force_restart (old behavior).
        this._rejectJoinWait(new Error('Kernel restarting'));

        if (this.socket) {
            this.socket.disconnect();
            this.socket = null;
        }

        this._clearPendingExecutions({ restarted: true });
        this.sessionId = null;

        return await this.connect(notebookId, { forceRestart: true });
    }

    execute(cellId, code, cellType = 'code') {
        if (!this.isConnected()) {
            this.module.showToast(window.i18n ? window.i18n.t('notebook.kernelNotConnected') : 'Kernel not connected', 3000);
            return Promise.resolve(false);
        }

        this.updateUI('busy');

        // Return a promise that resolves when execute_reply is received.
        // We key by cell_id until the server tells us the msg_id, then re-key
        // so a second run of the same cell does not overwrite the first resolver.
        return new Promise((resolve) => {
            this._pendingExecutionPromises.set(cellId, resolve);
            this.socket.emit('notebook:execute', {
                session_id: this.sessionId,
                cell_id: cellId,
                code: code,
                cell_type: cellType
            });
        });
    }

    sendInput(value, msgId = null) {
        if (!this.isConnected()) return;
        this.socket.emit('notebook:input_reply', {
            session_id: this.sessionId,
            value: value,
            msg_id: msgId
        });
    }

    interrupt() {
        if (!this.isConnected() || !this.sessionId) return;
        this.socket.emit('notebook:interrupt', {
            session_id: this.sessionId
        });
    }

    updateUI(status) {
        const text = document.getElementById('kernel-text');
        const dot = document.getElementById('kernel-indicator');
        const statusWidget = document.getElementById('kernel-status-widget');
        const metaKernel = document.getElementById('meta-kernel');

        // Determine the effective runtime. Prefer the explicit `_activeRuntime`,
        // fall back to the runtime level cached on the kernel widget, then to
        // the kernel status broadcast, then to the user default setting.
        var _activeRuntime = this._activeRuntime
            || (this._kernelBackendType)
            || (statusWidget && statusWidget.dataset && statusWidget.dataset.runtimeLevel)
            || (window.NotebookModule && NotebookModule._getEffectiveSettings
                ? (NotebookModule._getEffectiveSettings().defaultRuntime || 'python')
                : 'python');

        // Resolve a language label based on the runtime level name. The hybrid
        // runtime mixes both languages; any other name containing 'wolfram'
        // is Wolfram Language; 'quantum' is the quantum Python kernel.
        // Everything else is shown as Python 3.11.
        function resolveLanguageLabel(rt) {
            if (!rt) return 'Python 3.11';
            var lower = String(rt).toLowerCase();
            if (lower === 'hybrid' || lower.indexOf('wolfram_quantum') !== -1) return 'Python + Wolfram';
            if (lower.indexOf('wolfram') !== -1) return 'Wolfram Language';
            if (lower.indexOf('quantum') !== -1) return 'Python 3.11 Quantum';
            return 'Python 3.11';
        }

        var languageLabel = resolveLanguageLabel(_activeRuntime);

        var configs = {
            'idle': { text: languageLabel, class: 'idle', label: window.i18n ? window.i18n.t('notebook.kernelReady') : 'Ready', metaText: window.i18n ? window.i18n.t('notebook.kernelReadyStatus') : 'Kernel Ready' },
            'busy': { text: window.i18n ? window.i18n.t('notebook.kernelBusy') : 'Busy', class: 'busy', label: window.i18n ? window.i18n.t('notebook.kernelRunning') : 'Running', metaText: window.i18n ? window.i18n.t('notebook.kernelBusyStatus') : 'Kernel Busy' },
            'connecting': { text: window.i18n ? window.i18n.t('notebook.kernelConnecting') : 'Connecting...', class: 'disconnected', label: window.i18n ? window.i18n.t('notebook.kernelConnecting') : 'Connecting', metaText: window.i18n ? window.i18n.t('notebook.kernelConnecting') : 'Connecting...' },
            'error': { text: window.i18n ? window.i18n.t('notebook.kernelError') : 'Error', class: 'disconnected', label: window.i18n ? window.i18n.t('notebook.kernelError') : 'Error', metaText: window.i18n ? window.i18n.t('notebook.kernelErrorStatus') : 'Kernel Error' },
            'disconnected': { text: NbT('kernelDisconnected', 'Disconnected'), class: 'disconnected', label: NbT('kernelDisconnected', 'Disconnected'), metaText: NbT('kernelDisconnected', 'Disconnected') },
            'restarting': { text: window.i18n ? window.i18n.t('notebook.kernelRestarting') : 'Restarting...', class: 'busy', label: window.i18n ? window.i18n.t('notebook.kernelRestarting') : 'Restarting', metaText: window.i18n ? window.i18n.t('notebook.kernelRestarting') : 'Restarting...' }
        };

        var config = configs[status] || { text: status, class: 'disconnected', label: status, metaText: status };

        // Update status bar kernel indicator
        this.module.updateStatusBarKernel(status);

        // Update status bar language indicator
        var statusLang = document.getElementById('status-language');
        if (statusLang) {
            statusLang.textContent = languageLabel;
        }

        if (text) {
            text.textContent = config.text;
            text.removeAttribute('data-i18n');
        }
        if (dot) dot.className = 'nb-status-dot ' + config.class;
        // Standalone mode: update meta-kernel span
        if (metaKernel) {
            metaKernel.textContent = config.metaText;
            metaKernel.removeAttribute('data-i18n');
            metaKernel.className = 'kernel-status ' + config.class;
        }

        // Persist the resolved runtime on the kernel status widget so subsequent
        // updateUI() calls (e.g. after session continue) can recover it.
        if (statusWidget && _activeRuntime) {
            statusWidget.dataset.runtimeLevel = String(_activeRuntime);
        }

        // Show runtime level badge
        var badgeEl = document.getElementById('kernel-runtime-badge');
        var effectiveRuntime = _activeRuntime;
        if (effectiveRuntime && status !== 'disconnected') {
            if (!badgeEl) {
                badgeEl = document.createElement('span');
                badgeEl.id = 'kernel-runtime-badge';
                badgeEl.className = 'kernel-runtime-badge';
                var widget = document.getElementById('kernel-status-widget');
                if (widget) widget.insertBefore(badgeEl, widget.querySelector('.nb-kernel-action-btn'));
            }
            // Match by substring so any custom runtime level whose name contains
            // "wolfram" still resolves to a clean "Wolfram" badge label.
            var displayNames = { 'python': 'Python', 'wolfram': 'Wolfram', 'basic': 'Python' };
            var rtLower = String(effectiveRuntime).toLowerCase();
            var badgeText = displayNames[effectiveRuntime];
            if (!badgeText) {
                badgeText = rtLower.indexOf('wolfram') !== -1 ? 'Wolfram' : effectiveRuntime;
            }
            badgeEl.textContent = badgeText;
            badgeEl.setAttribute('data-runtime', String(effectiveRuntime));
            badgeEl.setAttribute('data-language', rtLower.indexOf('wolfram') !== -1 ? 'wolfram' : 'python');
            badgeEl.style.display = 'inline-block';
        } else if (badgeEl) {
            badgeEl.style.display = 'none';
        }

        if (statusWidget) {
            const actionBtn = document.getElementById('kernel-action-btn');
            var actionTitle;
            if (status === 'disconnected' || status === 'error') {
                actionTitle = window.i18n ? window.i18n.t('notebook.clickToConnect') : 'Click to connect';
                statusWidget.title = actionTitle;
                if (actionBtn) {
                    actionBtn.style.display = 'flex';
                    actionBtn.title = actionTitle;
                    actionBtn.setAttribute('aria-label', actionTitle);
                }
            } else if (status === 'connecting' || status === 'restarting') {
                actionTitle = window.i18n ? window.i18n.t('notebook.connectingToKernel') : 'Connecting to kernel...';
                statusWidget.title = actionTitle;
                if (actionBtn) {
                    actionBtn.style.display = 'none';
                    actionBtn.title = actionTitle;
                    actionBtn.setAttribute('aria-label', actionTitle);
                }
            } else {
                actionTitle = window.i18n ? window.i18n.t('notebook.clickToDisconnect') : 'Click to disconnect';
                statusWidget.title = actionTitle;
                if (actionBtn) {
                    actionBtn.style.display = 'flex';
                    actionBtn.title = actionTitle;
                    actionBtn.setAttribute('aria-label', actionTitle);
                }
            }
        }
    }
}

// ============================================================================
// NOTEBOOK DOCS — Full-screen, searchable documentation reader for the whole
// Notebook IDE. Opens as an overlay over the work area (#docs-overlay), with a
// table-of-contents rail, scroll-spy, client-side search, and a live model
// list pulled from /api/notebooks/models.
// ============================================================================
window.NotebookDocs = {
    _rendered: false,
    _observer: null,

    init: function () {
        var self = this;
        var trigger = document.getElementById('docs-trigger');
        if (trigger) {
            trigger.addEventListener('click', function () { self.toggle(); });
        }
        var closeBtn = document.getElementById('docs-close-btn');
        if (closeBtn) {
            closeBtn.addEventListener('click', function () { self.close(); });
        }
        var search = document.getElementById('docs-search-input');
        if (search) {
            search.addEventListener('input', function (e) { self._filter(e.target.value || ''); });
        }
        // Escape closes the reader when it is open.
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && self.isOpen()) {
                e.stopPropagation();
                self.close();
            }
        });
    },

    isOpen: function () {
        var overlay = document.getElementById('docs-overlay');
        return !!overlay && !overlay.hidden;
    },

    toggle: function () {
        if (this.isOpen()) { this.close(); } else { this.open(); }
    },

    open: function () {
        var overlay = document.getElementById('docs-overlay');
        if (!overlay) return;
        if (!this._rendered) { this._render(); }
        overlay.hidden = false;
        var trigger = document.getElementById('docs-trigger');
        if (trigger) trigger.classList.add('active');
        this._loadModels();
        var search = document.getElementById('docs-search-input');
        if (search) { setTimeout(function () { search.focus(); }, 40); }
    },

    close: function () {
        var overlay = document.getElementById('docs-overlay');
        if (!overlay) return;
        overlay.hidden = true;
        var trigger = document.getElementById('docs-trigger');
        if (trigger) trigger.classList.remove('active');
    },

    _t: function (key, fallback) {
        return (window.i18n && typeof window.i18n.t === 'function') ? window.i18n.t('notebook.' + key) : fallback;
    },

    _esc: function (value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    },

    // Small inline-SVG icon set used by the TOC and section headings.
    _icon: function (name) {
        var p = {
            rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91 0z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22 22 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/>',
            layout: '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/>',
            cells: '<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/>',
            cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
            folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
            box: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
            sigma: '<path d="M18 7V4H6l6 8-6 8h12v-3"/>',
            atom: '<circle cx="12" cy="12" r="1.6"/><ellipse cx="12" cy="12" rx="10" ry="4"/><ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(60 12 12)"/><ellipse cx="12" cy="12" rx="10" ry="4" transform="rotate(120 12 12)"/>',
            layers: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
            keyboard: '<rect x="2" y="4" width="20" height="16" rx="2"/><line x1="6" y1="8" x2="6" y2="8"/><line x1="10" y1="8" x2="10" y2="8"/><line x1="14" y1="8" x2="14" y2="8"/><line x1="18" y1="8" x2="18" y2="8"/><line x1="6" y1="12" x2="6" y2="12"/><line x1="10" y1="12" x2="10" y2="12"/><line x1="14" y1="12" x2="14" y2="12"/><line x1="18" y1="12" x2="18" y2="12"/><line x1="8" y1="16" x2="16" y2="16"/>',
            help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>'
        };
        var body = p[name] || p.help;
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' + body + '</svg>';
    },

    _render: function () {
        var toc = document.getElementById('docs-toc');
        var reading = document.getElementById('docs-reading');
        if (!toc || !reading) return;

        var sections = this._sections();
        var tocHtml = '';
        var articleHtml = '';
        var lastGroup = null;

        sections.forEach(function (s) {
            if (s.group !== lastGroup) {
                tocHtml += '<div class="nb-docs-toc-heading">' + NotebookDocs._esc(s.group) + '</div>';
                lastGroup = s.group;
            }
            tocHtml += '<a data-target="docs-sec-' + s.id + '" data-title="' + NotebookDocs._esc(s.title) + '">' +
                NotebookDocs._icon(s.icon) + '<span>' + NotebookDocs._esc(s.title) + '</span></a>';

            articleHtml += '<section class="nb-docs-section" id="docs-sec-' + s.id + '">' +
                '<h2>' + NotebookDocs._icon(s.icon) + NotebookDocs._esc(s.title) + '</h2>' +
                (s.lede ? '<p class="nb-docs-lede">' + s.lede + '</p>' : '') +
                s.body + '</section>';
        });

        toc.innerHTML = tocHtml;
        reading.innerHTML = articleHtml;

        this._bindNav();
        this._rendered = true;
    },

    _bindNav: function () {
        var self = this;
        var article = document.getElementById('docs-article');
        var links = Array.prototype.slice.call(document.querySelectorAll('#docs-toc a[data-target]'));

        links.forEach(function (link) {
            link.addEventListener('click', function (e) {
                e.preventDefault();
                var el = document.getElementById(link.dataset.target);
                if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            });
        });

        // Scroll-spy: highlight the TOC entry for the section nearest the top.
        if (this._observer) { this._observer.disconnect(); }
        this._observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    var id = entry.target.id;
                    links.forEach(function (l) {
                        l.classList.toggle('active', l.dataset.target === id);
                    });
                }
            });
        }, { root: article, rootMargin: '0px 0px -70% 0px', threshold: 0 });

        document.querySelectorAll('#docs-reading .nb-docs-section').forEach(function (sec) {
            self._observer.observe(sec);
        });
    },

    _filter: function (query) {
        var q = query.trim().toLowerCase();
        var sections = Array.prototype.slice.call(document.querySelectorAll('#docs-reading .nb-docs-section'));
        var links = Array.prototype.slice.call(document.querySelectorAll('#docs-toc a[data-target]'));
        var headings = Array.prototype.slice.call(document.querySelectorAll('#docs-toc .nb-docs-toc-heading'));
        var anyVisible = false;

        sections.forEach(function (sec) {
            var match = !q || (sec.textContent || '').toLowerCase().indexOf(q) !== -1;
            sec.classList.toggle('filtered-out', !match);
            var link = links.filter(function (l) { return l.dataset.target === sec.id; })[0];
            if (link) link.classList.toggle('filtered-out', !match);
            if (match) anyVisible = true;
        });

        // Hide a TOC group heading when every link under it is filtered out.
        headings.forEach(function (h) {
            var visible = false;
            var node = h.nextElementSibling;
            while (node && !node.classList.contains('nb-docs-toc-heading')) {
                if (node.matches('a') && !node.classList.contains('filtered-out')) { visible = true; break; }
                node = node.nextElementSibling;
            }
            h.style.display = visible ? '' : 'none';
        });

        var noResults = document.getElementById('docs-noresults');
        if (noResults) noResults.classList.toggle('show', !anyVisible);
    },

    _loadModels: function () {
        var container = document.getElementById('docs-model-grid');
        if (!container || container.dataset.loaded === '1') return;

        var query = (window.NotebookModule && NotebookModule.projectId)
            ? ('?project_id=' + encodeURIComponent(NotebookModule.projectId)) : '';

        fetch('/api/notebooks/models' + query)
            .then(function (res) { return res.json(); })
            .then(function (data) {
                var all = Array.isArray(data.platform_models) ? data.platform_models : [];
                var models = all.filter(function (m) {
                    if (!m) return false;
                    if (m.available === false || m.is_active === false || m.greyed_out) return false;
                    if (m.runtime_status && /^(offline|down|unavailable|disabled)$/i.test(String(m.runtime_status))) return false;
                    return true;
                });
                if (!models.length) {
                    container.innerHTML = '<div class="nb-docs-muted">' + NotebookDocs._t('noOnlineModels', 'No online models available') + '</div>';
                    return;
                }
                var online = NotebookDocs._t('online', 'Online');
                var costLabel = NotebookDocs._t('creditCost', 'Credit cost:');
                container.innerHTML = models.map(function (m) {
                    return '<div class="nb-docs-model">' +
                        '<div class="nb-docs-model-name"><span>' + NotebookDocs._esc(m.name || m.short_name) + '</span>' +
                        '<span class="nb-docs-model-badge">● ' + online + '</span></div>' +
                        '<div class="nb-docs-model-desc">' + NotebookDocs._esc(m.description || '') + '</div>' +
                        '<div class="nb-docs-model-cost">' + costLabel + ' ' + (m.credit_cost || 0) + '</div>' +
                        '</div>';
                }).join('');
                container.dataset.loaded = '1';
            })
            .catch(function () {
                container.innerHTML = '<div class="nb-docs-muted">' + NotebookDocs._t('failedToLoadModels', 'Failed to load models') + '</div>';
            });
    },

    _sections: function () {
        var t = this._t.bind(this);
        var basics = t('docsGroupBasics', 'Basics');
        var reference = t('docsGroupReference', 'SDK Reference');
        var help = t('docsGroupHelp', 'Help');

        return [
            {
                id: 'overview', group: basics, icon: 'rocket',
                title: t('docsSecOverview', 'Getting Started'),
                lede: 'The Notebook IDE is a project-centric workspace for running Python, Wolfram Language, and quantum circuits side by side — with direct access to your project files, job outputs, and the Phonos model catalog.',
                body:
                    '<h3>Create a notebook</h3>' +
                    '<p>Pick a project from the selector at the top of the sidebar, then use the <strong>Notebooks</strong> panel or the welcome screen\'s quick actions to create one. Each starter (Code, Text, Data Analysis, Protein Analysis, Wolfram, Quantum, and the experimental Wolfram + Quantum) opens with cells and a runtime already suited to that workflow.</p>' +
                    '<h3>Save &amp; autosave</h3>' +
                    '<p>Changes autosave periodically (configurable in <strong>Settings &rarr; Execution</strong>), and you can save at any time with <code>Ctrl</code>+<code>S</code>. The <strong>History</strong> panel keeps prior snapshots you can restore.</p>' +
                    '<div class="nb-docs-callout tip">' + this._icon('help') + '<p>New here? Open the <strong>Quantum Hello World</strong> starter to see a Bell-state circuit run end to end on the simulator.</p></div>'
            },
            {
                id: 'interface', group: basics, icon: 'layout',
                title: t('docsSecInterface', 'The Interface'),
                lede: 'The IDE follows a familiar editor layout. The vertical activity bar on the far left switches what the sidebar shows.',
                body:
                    '<table class="nb-docs-table"><thead><tr><th>Area</th><th>What it does</th></tr></thead><tbody>' +
                    '<tr><td>Explorer</td><td>Project files, notebooks, and job outputs. Upload files and open them alongside your notebook.</td></tr>' +
                    '<tr><td>Notebooks</td><td>All notebooks in the current project, with search and active-session indicators.</td></tr>' +
                    '<tr><td>Runtimes</td><td>Active kernel sessions and controls to connect, restart, or stop them.</td></tr>' +
                    '<tr><td>Terminal</td><td>An interactive shell in the notebook workspace.</td></tr>' +
                    '<tr><td>Outline</td><td>Jump between markdown headings and cells.</td></tr>' +
                    '<tr><td>History</td><td>Saved snapshots you can review and restore.</td></tr>' +
                    '<tr><td>Documentation</td><td>This reader.</td></tr>' +
                    '<tr><td>Account / Settings</td><td>Editor, execution, runtime, quantum, and Wolfram preferences.</td></tr>' +
                    '</tbody></table>' +
                    '<p>The <strong>toolbar</strong> above the editor adds cells and runs or interrupts the kernel; the <strong>status bar</strong> at the bottom shows kernel state, cell count, and cursor position. Press <code>Ctrl</code>+<code>Shift</code>+<code>P</code> for the command palette.</p>'
            },
            {
                id: 'cells', group: basics, icon: 'cells',
                title: t('docsSecCells', 'Working with Cells'),
                lede: 'A notebook is an ordered list of cells. Add them from the toolbar, run them in place, and reorder or delete them as you go.',
                body:
                    '<table class="nb-docs-table"><thead><tr><th>Cell type</th><th>Purpose</th></tr></thead><tbody>' +
                    '<tr><td>Code</td><td>Python, executed in the connected kernel.</td></tr>' +
                    '<tr><td>Markdown</td><td>Formatted text, headings, and notes.</td></tr>' +
                    '<tr><td>Wolfram</td><td>Wolfram Language, evaluated in a persistent Wolfram session.</td></tr>' +
                    '<tr><td>Quantum</td><td>Visual circuit builder that generates Qiskit code and runs on a simulator or IBM hardware.</td></tr>' +
                    '<tr><td>Raw</td><td>Unrendered text, passed through as-is on export.</td></tr>' +
                    '<tr><td>C / Rust</td><td>Compiled and executed as standalone snippets.</td></tr>' +
                    '</tbody></table>' +
                    '<h3>Running cells</h3>' +
                    '<p>Run the selected cell with <code>Ctrl</code>+<code>Enter</code>, or run it and move to the next with <code>Shift</code>+<code>Enter</code>. Outputs — text, tables, images, and inline graphics — appear directly beneath each cell. Execution is serialized per session, so overlapping runs queue rather than clash.</p>'
            },
            {
                id: 'runtimes', group: basics, icon: 'cpu',
                title: t('docsSecRuntimes', 'Runtimes & Kernels'),
                lede: 'A runtime is the environment your cells execute in. Connect one from the status bar or the Runtimes panel; sessions persist across reloads.',
                body:
                    '<table class="nb-docs-table"><thead><tr><th>Runtime</th><th>Use it for</th></tr></thead><tbody>' +
                    '<tr><td>Basic Python</td><td>A standard Python 3 kernel.</td></tr>' +
                    '<tr><td>Wolfram</td><td>A Wolfram Engine session for Wolfram cells.</td></tr>' +
                    '<tr><td>Quantum Python 3.11</td><td>Python plus Qiskit, pre-configured from your Quantum Settings.</td></tr>' +
                    '<tr><td>Quantum + Wolfram (Hybrid)</td><td>One session shared by Python, Quantum, and Wolfram cells.</td></tr>' +
                    '</tbody></table>' +
                    '<p>Set the default in <strong>Settings &rarr; Runtime</strong>, or let a notebook pick the right kernel from its type on first connect. Use the Runtimes panel to <strong>restart</strong> (clear state) or <strong>interrupt</strong> (stop a running cell). State is kept server-side, so outputs produced while the tab was in the background are replayed on reconnect.</p>' +
                    '<div class="nb-docs-callout warn">' + this._icon('help') + '<p>Real IBM quantum hardware runs are queued and can take noticeably longer than the simulator. Busy sessions are not idle-reaped while a hardware job is pending.</p></div>'
            },
            {
                id: 'wolfram-start', group: basics, icon: 'sigma',
                title: t('docsSecWolframStart', 'Wolfram Getting Started'),
                lede: 'Everything you need before your first Wolfram cell: connect your account, evaluate, and know the edge cases. Previously the sign-in "Getting Started" guide — now it lives here.',
                body:
                    '<h3>1. Connect your Wolfram Cloud account</h3>' +
                    '<p>Wolfram cells evaluate through the Wolfram Cloud, so the IDE needs your Wolfram ID. Open <strong>Settings &rarr; Wolfram Account</strong> (or the Account panel in the right sidebar), enter your Wolfram ID and password, and hit <strong>Test Connection</strong>. Admins fall back to the platform account automatically; everyone else must connect their own — cells fail fast with a <em>MissingCredentials</em> notice otherwise. Free accounts work; create one at wolfram.com if needed.</p>' +
                    '<h3>2. Your first Wolfram cell</h3>' +
                    '<p>Create a notebook from the <strong>Wolfram</strong> starter (or add a Wolfram cell to any notebook via the toolbar\'s <em>Wolfram</em> button), then run:</p>' +
                    '<div class="nb-docs-code"><code>(* variables persist between cells *)\nx = 2^100;\nN[Sqrt[x]]</code></div>' +
                    '<p>State persists across cells in the same session — functions defined in one cell are callable from the next. Graphics (<code>Plot</code>, <code>BarChart</code>, ...) render inline as images.</p>' +
                    '<h3>3. Kernel management &amp; switching runtimes</h3>' +
                    '<p>The kernel widget in the toolbar shows the connection state; click it to connect or disconnect. Restart from the toolbar (or <strong>Restart &amp; Run All</strong>) to clear all state; use <strong>Change Runtime</strong> to switch between Python, Wolfram, Quantum, and Hybrid — switching restarts the kernel. The <strong>Runtimes</strong> panel lists every active session with interrupt / restart / stop controls.</p>' +
                    '<h3>4. Edge cases that throw errors</h3>' +
                    '<table class="nb-docs-table"><thead><tr><th>Symptom</th><th>Cause &amp; fix</th></tr></thead><tbody>' +
                    '<tr><td><em>MissingCredentials</em></td><td>No Wolfram account connected — add it in Settings &rarr; Wolfram Account.</td></tr>' +
                    '<tr><td><em>Wolfram Cloud rejected your credentials</em></td><td>Password changed or typo — re-enter and Test Connection.</td></tr>' +
                    '<tr><td>Cell errors but earlier assignments survive</td><td>Expected: statements that succeeded before the failing one are kept, so one bad line doesn\'t poison the session.</td></tr>' +
                    '<tr><td>A cell runs forever</td><td>Interrupt it from the toolbar — evaluation is killed server-side and session state is preserved.</td></tr>' +
                    '<tr><td>Undefined symbols after a restart</td><td>Restart intentionally clears the session; re-run defining cells (or use Restart &amp; Run All).</td></tr>' +
                    '</tbody></table>' +
                    '<h3>5. Quick checklist</h3>' +
                    '<p>Project selected &rarr; Wolfram account connected &amp; tested &rarr; notebook created from the Wolfram starter &rarr; kernel shows <em>Ready</em> &rarr; run a cell &rarr; save (<code>Ctrl</code>+<code>S</code>).</p>' +
                    '<div class="nb-docs-callout tip">' + this._icon('help') + '<p>Prefer a document? <a href="/static/docs/Getting_Started_Wolfram_Notebooks.pdf" download>Download the full Getting Started PDF</a> — the same guide in printable form.</p></div>'
            },
            {
                id: 'files', group: basics, icon: 'folder',
                title: t('docsFilesSection', 'Project Files'),
                lede: 'Project uploads and job outputs are mounted into the runtime under /project, so you can read them straight from a cell.',
                body:
                    '<div class="nb-docs-code"><code>import pandas as pd\n\n' +
                    'df = pd.read_csv(\'/project/uploads/my-data.csv\')\n' +
                    'df.head()</code></div>' +
                    '<p>Job outputs live under <code>/project/jobs/&lt;job_id&gt;/</code>:</p>' +
                    '<div class="nb-docs-code"><code>with open(\'/project/jobs/1234/structure.pdb\') as f:\n' +
                    '    structure = f.read()\n' +
                    'print(structure[:300])</code></div>' +
                    '<p>The Explorer lists every file with its container path. Upload new files with the Explorer\'s upload button, or save results back to the project from a cell.</p>'
            },
            {
                id: 'sdk', group: reference, icon: 'box',
                title: t('docsSdk', 'Phonos SDK'),
                lede: 'The phonos library runs AI models, submits and tracks jobs, and saves outputs — all from inside a notebook.',
                body:
                    '<div class="nb-docs-code"><code>import phonos\n\n' +
                    'job = phonos.esmfold.run(\'MVLSPADKTN...\')\n' +
                    'job.wait()\n' +
                    'phonos.download_output(job.job_id, \'structure.pdb\')</code></div>' +
                    '<h3>Job management</h3>' +
                    '<table class="nb-docs-table"><tbody>' +
                    '<tr><td><code>phonos.list_models()</code></td><td>List available AI models.</td></tr>' +
                    '<tr><td><code>phonos.submit_job(model, **kwargs)</code></td><td>Submit a job.</td></tr>' +
                    '<tr><td><code>phonos.get_job(job_id)</code></td><td>Fetch job status.</td></tr>' +
                    '<tr><td><code>phonos.list_jobs(limit=50)</code></td><td>List your jobs.</td></tr>' +
                    '<tr><td><code>phonos.download_output(job_id, filename)</code></td><td>Save a file from a job output to project storage.</td></tr>' +
                    '</tbody></table>' +
                    '<h3>Model functions</h3>' +
                    '<p>Each model exposes a <code>.run(...)</code> that returns a Job: <code>phonos.esmfold.run(sequence)</code>, <code>phonos.diffdock.run(protein, ligand)</code>, <code>phonos.rfdiffusion.run(config)</code>, <code>phonos.boltz2.run(sequences)</code>, <code>phonos.evo2.run(sequence)</code>.</p>' +
                    '<h3>Job object</h3>' +
                    '<p><code>job.result(timeout=300)</code>, <code>job.wait()</code>, <code>job.refresh()</code>, <code>job.cancel()</code>, <code>job.logs()</code>, and the properties <code>job.status</code> / <code>job.job_id</code>.</p>' +
                    '<h3>' + t('docsAvailableModels', 'Available Models') + '</h3>' +
                    '<div class="nb-docs-model-grid" id="docs-model-grid"><div class="nb-docs-muted">' + t('loadingModels', 'Loading models...') + '</div></div>'
            },
            {
                id: 'wolfram', group: reference, icon: 'sigma',
                title: t('docsWolframTitle', 'Wolfram SDK'),
                lede: 'Bridge Python and Wolfram Language. Evaluations share the notebook\'s Wolfram session, so state flows both ways.',
                body:
                    '<table class="nb-docs-table"><tbody>' +
                    '<tr><td><code>phonos.wolfram.eval(code)</code></td><td>Evaluate Wolfram code; returns a WolframResult (graphics render inline).</td></tr>' +
                    '<tr><td><code>phonos.wolfram.get(expr)</code></td><td>Evaluate and return a native Python object (via JSON).</td></tr>' +
                    '<tr><td><code>phonos.wolfram.set(name, value)</code></td><td>Assign a JSON-serializable Python value to a Wolfram symbol.</td></tr>' +
                    '<tr><td><code>phonos.wolfram.help()</code></td><td>Print this reference from a cell.</td></tr>' +
                    '</tbody></table>' +
                    '<div class="nb-docs-code"><code>phonos.wolfram.set(\'counts\', {\'00\': 512, \'11\': 488})\n' +
                    'phonos.wolfram.eval(\'BarChart[Values[counts]]\')</code></div>' +
                    '<div class="nb-docs-callout tip">' + this._icon('help') + '<p>Requires Wolfram Cloud credentials (<strong>Settings &rarr; Wolfram Account</strong>) unless you are an admin using the platform account.</p></div>'
            },
            {
                id: 'quantum', group: reference, icon: 'atom',
                title: t('docsQuantumTitle', 'Quantum SDK'),
                lede: 'Run Qiskit circuits where your Quantum Settings say they should run — a local simulator or real IBM hardware.',
                body:
                    '<table class="nb-docs-table"><tbody>' +
                    '<tr><td><code>phonos.quantum.run(circuit, shots=1024)</code></td><td>Run a circuit; returns a measurement counts dict.</td></tr>' +
                    '<tr><td><code>phonos.quantum.mode()</code></td><td>Current mode: <code>simulator</code> or <code>hardware</code>.</td></tr>' +
                    '<tr><td><code>phonos.quantum.backend()</code></td><td>Resolved backend object.</td></tr>' +
                    '<tr><td><code>phonos.quantum.service()</code></td><td>Authenticated QiskitRuntimeService (hardware).</td></tr>' +
                    '<tr><td><code>phonos.quantum.status()</code></td><td>Summary of the effective quantum config.</td></tr>' +
                    '</tbody></table>' +
                    '<div class="nb-docs-code"><code>from qiskit import QuantumCircuit\n\n' +
                    'qc = QuantumCircuit(2, 2)\n' +
                    'qc.h(0); qc.cx(0, 1); qc.measure([0, 1], [0, 1])\n\n' +
                    'counts = phonos.quantum.run(qc, shots=1024)</code></div>' +
                    '<p>Configure execution mode, backend, and credentials in <strong>Settings &rarr; Quantum</strong>. Prefer the visual <strong>Quantum</strong> cell to build circuits without writing Qiskit by hand.</p>'
            },
            {
                id: 'hybrid', group: reference, icon: 'layers',
                title: t('docsSecHybrid', 'Hybrid Workflows'),
                lede: 'The experimental hybrid runtime runs Python, Quantum, and Wolfram cells as one continuous computation in a single shared session.',
                body:
                    '<p>Push data between engines with the Wolfram bridge — for example, hand quantum measurement counts to Wolfram for analysis and plotting:</p>' +
                    '<div class="nb-docs-code"><code>counts = phonos.quantum.run(qc, shots=1024)   # Python + Qiskit\n' +
                    'phonos.wolfram.set(\'counts\', counts)          # push into Wolfram\n' +
                    'phonos.wolfram.get(\'Total[Values[counts]]\')   # pull a result back</code></div>' +
                    '<p>Start a hybrid notebook from the <strong>Wolfram + Quantum</strong> starter, or set the default runtime to <em>Quantum + Wolfram</em> in Settings.</p>'
            },
            {
                id: 'shortcuts', group: help, icon: 'keyboard',
                title: t('keyboardShortcuts', 'Keyboard Shortcuts'),
                lede: 'The essentials. See the full list any time from the keyboard icon at the bottom of the activity bar.',
                body:
                    '<table class="nb-docs-table"><tbody>' +
                    '<tr><td><kbd>Ctrl</kbd> + <kbd>Enter</kbd></td><td>Run the selected cell.</td></tr>' +
                    '<tr><td><kbd>Shift</kbd> + <kbd>Enter</kbd></td><td>Run the cell and select the next.</td></tr>' +
                    '<tr><td><kbd>Ctrl</kbd> + <kbd>B</kbd></td><td>Add a code cell.</td></tr>' +
                    '<tr><td><kbd>Ctrl</kbd> + <kbd>S</kbd></td><td>Save the notebook.</td></tr>' +
                    '<tr><td><kbd>Ctrl</kbd> + <kbd>F</kbd></td><td>Find in notebook.</td></tr>' +
                    '<tr><td><kbd>Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>P</kbd></td><td>Command palette.</td></tr>' +
                    '</tbody></table>'
            },
            {
                id: 'troubleshooting', group: help, icon: 'help',
                title: t('docsTroubleshooting', 'Troubleshooting'),
                lede: 'Quick fixes for the most common snags.',
                body:
                    '<ul>' +
                    '<li><strong>Files or models won\'t load</strong> — refresh the panel, and confirm a project is selected.</li>' +
                    '<li><strong>A runtime is stuck busy</strong> — interrupt the kernel, or disconnect and reconnect from the Runtimes panel.</li>' +
                    '<li><strong>A Wolfram or quantum cell errors on credentials</strong> — check <strong>Settings &rarr; Wolfram / Quantum Account</strong>.</li>' +
                    '<li><strong>A hardware quantum job seems stuck</strong> — it is likely queued on IBM; watch the printed job id for progress.</li>' +
                    '</ul>' +
                    '<h3>' + t('docsKnownLimitations', 'Known Limitations') + '</h3>' +
                    '<ul>' +
                    '<li>Wolfram widgets (<code>Manipulate</code>, <code>Dynamic</code>) render as static images, not live controls.</li>' +
                    '<li>Cell execution is serialized per session — runs queue rather than execute in parallel.</li>' +
                    '</ul>'
            }
        ];
    }
};

// ============================================================================
// COLLABORATION MANAGER - Real-time co-editing
// ============================================================================

const CollaborationManager = {
    enabled: false,
    notebookId: null,
    socket: null,
    userColors: ['#3b82f6', '#ef4444', '#22c55e', '#f59e0b', '#a855f7', '#ec4899', '#06b6d4', '#f97316'],
    userColorIndex: 0,
    activeUsers: {},
    typingUsers: {},
    _typingTimeout: null,

    init: function(notebookId) {
        this.notebookId = notebookId;
        if (typeof io === 'undefined') {
            console.log('[Collab] Socket.IO not available');
            return;
        }

        try {
            // forceNew: KernelManager also opens an io('/notebook') socket on
            // kernel connect (auto-triggered when a notebook has an active
            // session). Without forceNew, socket.io-client returns the SAME
            // cached socket here, so this 'connect' handler attaches AFTER
            // the kernel's connect already fired — and notebook:join_collab
            // never emits, leaving this user invisible to collaborators
            // until a page refresh changes the timing. forceNew gives the
            // collaboration manager its own dedicated connection.
            this.socket = io('/notebook', {
                transports: ['websocket', 'polling'],
                reconnection: true,
                reconnectionDelay: 2000,
                forceNew: true,
            });

            this.socket.on('notebook:welcome', (data) => {
                if (data && data.user_id !== undefined) this.myUserId = data.user_id;
            });

            this.socket.on('connect', () => {
                console.log('[Collab] Connected, joining notebook', notebookId);
                this.socket.emit('notebook:join_collab', { notebook_id: notebookId });
                this.enabled = true;
                this._startHeartbeat();
            });

            // Defensive: if the socket were already connected before the
            // handler above attached (should not happen with forceNew, but
            // guards against any future socket reuse), join immediately.
            if (this.socket.connected) {
                this.socket.emit('notebook:join_collab', { notebook_id: notebookId });
                this.enabled = true;
                this._startHeartbeat();
            }

            this.socket.on('disconnect', () => {
                console.log('[Collab] Disconnected');
                this.enabled = false;
                this.activeUsers = {};
                this.typingUsers = {};
                this._remoteCellUsers = {};
                document.querySelectorAll('.cell.remote-active').forEach(el => {
                    el.classList.remove('remote-active');
                    const badge = el.querySelector('.collab-remote-badge');
                    if (badge) badge.remove();
                });
                this._clearRemoteCursors();
                this._renderPresenceBar();
            });

            this.socket.io.on('reconnect', () => {
                console.log('[Collab] Reconnected, re-joining notebook');
                if (this.notebookId && this.socket?.connected) {
                    this.socket.emit('notebook:join_collab', { notebook_id: this.notebookId });
                    this.enabled = true;
                    this._startHeartbeat();
                    // Re-fetch the authoritative saved notebook state so
                    // edits that happened while disconnected are not lost
                    // / silently dropped. Missed cell_synced events during
                    // the outage are not replayed (last-write-wins), so a
                    // fresh pull is the safest recovery.
                    if (window.NotebookModule && NotebookModule.notebookId && typeof NotebookModule.loadNotebook === 'function') {
                        NotebookModule.loadNotebook(NotebookModule.notebookId).catch(function(e) {
                            console.warn('[Collab] Re-fetch after reconnect failed', e);
                        });
                    }
                }
            });

            this.socket.on('notebook:presence', (data) => {
                this._updatePresence(data.users || []);
            });

            // The notebook has reached the collaborator cap (default 5
            // distinct users). The server refused our join, so we disable
            // live collaboration for this session. The user can still edit
            // locally; they just won't send/receive live changes until a
            // slot frees up. Reconnecting later will retry the join.
            this.socket.on('notebook:collab_full', (data) => {
                this.enabled = false;
                if (this._heartbeatInterval) { clearInterval(this._heartbeatInterval); this._heartbeatInterval = null; }
                this._remoteCellUsers = {};
                document.querySelectorAll('.cell.remote-active').forEach(el => {
                    el.classList.remove('remote-active');
                    const badge = el.querySelector('.collab-remote-badge');
                    if (badge) badge.remove();
                });
                this._clearRemoteCursors();
                this.activeUsers = {};
                this._renderPresenceBar();
                var msg = (data && data.message) || 'Notebook is at the collaboration limit.';
                if (window.Toast) { Toast.warning(msg); } else { console.warn('[Collab]', msg); }
                var bar = document.getElementById('collab-bar');
                if (bar) {
                    bar.style.display = '';
                    bar.innerHTML = '';
                    var span = document.createElement('span');
                    span.className = 'collab-count';
                    span.style.color = 'var(--accent-warning,#D9822B)';
                    span.textContent = msg;
                    bar.appendChild(span);
                }
            });

            this.socket.on('notebook:cell_synced', (data) => {
                this._applyRemoteCellEdit(data);
            });

            this.socket.on('notebook:cursor_updated', (data) => {
                this._updateRemoteCursor(data);
            });

            this.socket.on('notebook:cell_selected', (data) => {
                this._highlightRemoteCell(data);
            });

            this.socket.on('notebook:typing_update', (data) => {
                this._handleTypingUpdate(data);
            });

            // Someone (possibly in another tab) restored this notebook to an
            // older version. Without this, every other open editor keeps its
            // stale in-memory cells and its next 30s autosave would silently
            // overwrite the restore. Reload the restored cells locally and
            // clear the dirty flag so autosave has nothing stale to save.
            this.socket.on('notebook:history_restored', (data) => {
                if (!data || String(data.notebook_id) !== String(this.notebookId)) return;
                if (window.NotebookModule && typeof NotebookModule.loadCells === 'function' && data.cells) {
                    NotebookModule.loadCells(data.cells);
                }
                var who = data.restored_by ? data.restored_by : 'A collaborator';
                var msg = who + ' restored this notebook to an earlier version.';
                if (window.Toast) { Toast.warning(msg); } else { console.warn('[Collab]', msg); }
            });
        } catch(e) {
            console.log('[Collab] Init error:', e);
        }
    },

    _startHeartbeat: function() {
        if (this._heartbeatInterval) clearInterval(this._heartbeatInterval);
        this._heartbeatInterval = setInterval(() => {
            if (!this.enabled || !this.socket?.connected) return;
            this.socket.emit('notebook:heartbeat', {
                notebook_id: this.notebookId,
                cell_id: NotebookModule?.currentCellId,
            });
            // Clean stale cursors
            this._cleanStaleCursors();
        }, 10000);
    },

    _handleTypingUpdate: function(data) {
        if (!data.is_typing) {
            delete this.typingUsers[data.user_id];
        } else {
            this.typingUsers[data.user_id] = {
                username: data.username,
                cell_id: data.cell_id,
                timestamp: Date.now(),
            };
        }
        this._renderTypingIndicators();
    },

    _renderTypingIndicators: function() {
        var typingList = Object.values(this.typingUsers);
        if (typingList.length === 0) return;

        // Group typing users by cell
        var byCell = {};
        typingList.forEach(function(u) {
            if (!byCell[u.cell_id]) byCell[u.cell_id] = [];
            byCell[u.cell_id].push(u.username);
        });

        Object.keys(byCell).forEach(function(cellId) {
            var cellEl = document.querySelector('.cell[data-cell-id="' + cellId + '"]');
            if (!cellEl) return;
            var existing = cellEl.querySelector('.collab-typing-indicator');
            if (existing) existing.remove();
            var names = byCell[cellId];
            var el = document.createElement('div');
            el.className = 'collab-typing-indicator';
            el.textContent = names.join(', ') + ' typing\u2026';
            cellEl.querySelector('.nb-cell-footer')?.appendChild(el);
        });
    },

    _updatePresence: function(users) {
        const oldIds = Object.keys(this.activeUsers);
        const newUserIds = new Set(users.map(u => u.user_id));
        const collabBar = document.getElementById('collab-bar');

        this.activeUsers = {};
        users.forEach(u => {
            if (!this.activeUsers[u.user_id]) {
                this.activeUsers[u.user_id] = { ...u, color: this._getUserColor(u.user_id) };
            } else {
                Object.assign(this.activeUsers[u.user_id], u);
            }
        });

        // Remove cursors and typing indicators for users who left
        oldIds.forEach(id => {
            if (!newUserIds.has(parseInt(id))) {
                this._removeCursor(parseInt(id));
                delete this.typingUsers[parseInt(id)];
            }
        });
        this._renderTypingIndicators();

        this._renderPresenceBar();

        // Hydrate cursors for everyone already in the room from this presence
        // snapshot. Previously a cursor only appeared once its owner's NEXT
        // cursorActivity fired after you joined — so if they were idle (or you
        // joined after their last move), you'd see no cursor until either of
        // you happened to move again, which in practice often meant "reload
        // the page" was the only thing that seemed to fix it. Presence already
        // carries each user's last known cell_id/cursor_pos, so render it
        // immediately instead of waiting for a live cursor_updated event.
        users.forEach(u => {
            if (u.user_id === this.myUserId) return;
            if (!u.cell_id || !u.cursor_pos) return;
            this._updateRemoteCursor({
                user_id: u.user_id,
                username: u.username,
                cell_id: u.cell_id,
                cursor_pos: u.cursor_pos,
                selection: null,
            });
        });

        // Build per-cell remote user map for green-highlight badges
        this._remoteCellUsers = {};
        users.forEach(u => {
            if (!u.cell_id) return;
            if (!this._remoteCellUsers[u.cell_id]) this._remoteCellUsers[u.cell_id] = [];
            this._remoteCellUsers[u.cell_id].push({
                user_id: u.user_id,
                username: u.username,
                color: this._getUserColor(u.user_id),
                is_editing: u.is_editing || false,
            });
        });
        this._renderRemoteCellHighlights();
    },

    _renderPresenceBar: function() {
        var bar = document.getElementById('collab-bar');
        if (!bar) return;

        var userList = Object.values(this.activeUsers);
        if (userList.length === 0) {
            bar.style.display = 'none';
            return;
        }

        // Build typing status message
        var typingNames = Object.values(this.typingUsers).map(function(u) { return u.username; });
        var typingMsg = typingNames.length > 0
            ? (typingNames.slice(0, 3).join(', ') + (typingNames.length > 3 ? ' +' + (typingNames.length - 3) : '') + ' typing\u2026')
            : '';

        bar.style.display = 'flex';
        bar.innerHTML = userList.map(function(u) {
            return '<div class="collab-user-avatar" style="background:' + u.color + ';" title="' + u.username + '">'
                + '<span>' + u.username.charAt(0).toUpperCase() + '</span></div>';
        }).join('')
            + '<span class="collab-count">' + userList.length + '</span>'
            + (typingMsg ? '<span class="collab-typing-status">' + typingMsg + '</span>' : '');
    },

    _applyRemoteCellEdit: function(data) {
        const cm = NotebookModule?.getCodeMirrorInstance?.(data.cell_id);
        if (!cm) return;
        // Only apply if the local user is not actively editing this cell right now
        const isActive = NotebookModule?.currentCellId === data.cell_id;
        if (isActive) return;
        NotebookModule?.setCellSource?.(data.cell_id, data.source);
    },

    _updateRemoteCursor: function(data) {
        const cellEl = document.querySelector(`.cell[data-cell-id="${data.cell_id}"]`);
        if (!cellEl) return;

        // Remove old cursor element for this user
        this._removeCursor(data.user_id);

        const cm = NotebookModule?.getCodeMirrorInstance?.(data.cell_id);
        const color = this._getUserColor(data.user_id);

        // Track text markers per user so we can clear previous selections.
        this._remoteMarks = this._remoteMarks || {};
        if (this._remoteMarks[data.user_id]) {
            try { this._remoteMarks[data.user_id].clear(); } catch (e) {}
        }
        delete this._remoteMarks[data.user_id];

        // Render selection range (highlight) via CodeMirror markText when the
        // remote user has an active selection.
        if (cm && data.selection && data.selection.anchor && data.selection.head) {
            try {
                var from = data.selection.anchor;
                var to = data.selection.head;
                // markText requires from <= to
                if ((from.line > to.line) || (from.line === to.line && from.ch > to.ch)) {
                    var tmp = from; from = to; to = tmp;
                }
                this._remoteMarks[data.user_id] = cm.markText(from, to, {
                    css: 'background-color: ' + this._withAlpha(color, 0.25) + ';'
                });
            } catch (e) {}
        }

        // Caret indicator
        const cursor = document.createElement('div');
        cursor.className = 'collab-cursor';
        cursor.id = `collab-cursor-${data.user_id}`;
        cursor.style.cssText = `
            position: absolute; width: 2px; height: 18px;
            background: ${color};
            z-index: 100; pointer-events: none;
            transition: top 0.08s ease, left 0.08s ease;
        `;

        const label = document.createElement('span');
        label.className = 'collab-cursor-label';
        label.style.cssText = `
            position: absolute; top: -18px; left: 0;
            background: ${color};
            color: #fff; font-size: 10px; padding: 1px 5px;
            border-radius: var(--r-ui, 0px); white-space: nowrap;
            font-family: var(--font-mono, monospace);
        `;
        label.textContent = data.username;
        cursor.appendChild(label);
        cellEl.appendChild(cursor);

        // Position caret using window coordinates so it aligns regardless of
        // CodeMirror scroll state. Previously used local coords minus bounding
        // rect (double-counting) which left cursors stuck at top-left.
        if (data.cursor_pos && data.cursor_pos.line !== undefined && cm) {
            try {
                var pos = { line: data.cursor_pos.line, ch: data.cursor_pos.ch || 0 };
                var coords = cm.cursorCoords(pos, 'window');
                var cellRect = cellEl.getBoundingClientRect();
                cursor.style.top = (coords.top - cellRect.top) + 'px';
                cursor.style.left = (coords.left - cellRect.left) + 'px';
            } catch (e) {}
        }
    },

    _withAlpha: function(hexColor, alpha) {
        // Convert #rrggbb -> rgba(r,g,b,alpha) for selection highlights.
        var m = /^#([0-9a-f]{6})$/i.exec(hexColor || '');
        if (!m) return hexColor;
        var n = parseInt(m[1], 16);
        var r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
        return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
    },

    _removeCursor: function(userId) {
        const el = document.getElementById(`collab-cursor-${userId}`);
        if (el) el.remove();
        // Also clear any selection highlight this user had.
        if (this._remoteMarks && this._remoteMarks[userId]) {
            try { this._remoteMarks[userId].clear(); } catch (e) {}
            delete this._remoteMarks[userId];
        }
    },

    _clearRemoteCursors: function() {
        document.querySelectorAll('.collab-cursor').forEach(el => el.remove());
        if (this._remoteMarks) {
            Object.keys(this._remoteMarks).forEach(function(uid) {
                try { this._remoteMarks[uid].clear(); } catch (e) {}
            }.bind(this));
            this._remoteMarks = {};
        }
    },

    _cleanStaleCursors: function() {
        const activeIds = new Set(Object.keys(this.activeUsers));
        document.querySelectorAll('.collab-cursor').forEach(el => {
            const uid = parseInt(el.id.replace('collab-cursor-', ''));
            if (!activeIds.has(String(uid))) el.remove();
        });
    },

    _renderRemoteCellHighlights: function() {
        document.querySelectorAll('.cell.remote-active').forEach(el => {
            el.classList.remove('remote-active');
            const badge = el.querySelector('.collab-remote-badge');
            if (badge) badge.remove();
        });
        Object.keys(this._remoteCellUsers || {}).forEach(cellId => {
            const users = this._remoteCellUsers[cellId];
            const el = document.querySelector(`.cell[data-cell-id="${cellId}"]`);
            if (!el) return;
            el.classList.add('remote-active');
            const existing = el.querySelector('.collab-remote-badge');
            if (existing) existing.remove();
            const badge = document.createElement('div');
            badge.className = 'collab-remote-badge';
            badge.innerHTML = users.map(u =>
                `<span class="collab-remote-avatar" style="background:${u.color}" title="${u.username} — ${u.is_editing ? 'editing' : 'viewing'}">${u.username.charAt(0).toUpperCase()}</span>`
            ).join('');
            el.appendChild(badge);
        });
    },

    _highlightRemoteCell: function(data) {
        if (!this._remoteCellUsers) this._remoteCellUsers = {};
        // Remove this user from any previously tracked cell
        Object.keys(this._remoteCellUsers).forEach(cellId => {
            const arr = this._remoteCellUsers[cellId];
            const idx = arr.findIndex(u => u.user_id === data.user_id);
            if (idx !== -1) arr.splice(idx, 1);
            if (arr.length === 0) delete this._remoteCellUsers[cellId];
        });
        // Add to the newly focused cell
        if (!this._remoteCellUsers[data.cell_id]) {
            this._remoteCellUsers[data.cell_id] = [];
        }
        this._remoteCellUsers[data.cell_id].push({
            user_id: data.user_id,
            username: data.username,
            color: this._getUserColor(data.user_id),
            is_editing: false,
        });
        this._renderRemoteCellHighlights();
    },

    _getUserColor: function(userId) {
        const colors = this.userColors;
        return colors[userId % colors.length];
    },

    _lastCellEdit: 0,
    _editThrottleMs: 300,

    sendCellEdit: function(cellId, source) {
        if (!this.enabled || !this.socket?.connected) return;
        var now = Date.now();
        if (now - this._lastCellEdit < this._editThrottleMs) return;
        this._lastCellEdit = now;
        this.socket.emit('notebook:cell_edit', {
            notebook_id: this.notebookId,
            cell_id: cellId,
            source: source,
            timestamp: now,
        });
    },

    sendTyping: function(cellId, isTyping) {
        if (!this.enabled || !this.socket?.connected) return;
        this.socket.emit('notebook:typing', {
            notebook_id: this.notebookId,
            cell_id: cellId,
            is_typing: isTyping,
        });
    },

    sendCursorSync: function(cellId, cursorPos, selection) {
        if (!this.enabled || !this.socket?.connected) return;
        this.socket.emit('notebook:cursor_sync', {
            notebook_id: this.notebookId,
            cell_id: cellId,
            cursor_pos: cursorPos,
            selection: selection,
        });
    },

    sendCellSelect: function(cellId) {
        if (!this.enabled || !this.socket?.connected) return;
        this.socket.emit('notebook:cell_select', {
            notebook_id: this.notebookId,
            cell_id: cellId,
        });
    },

    destroy: function() {
        if (this._heartbeatInterval) clearInterval(this._heartbeatInterval);
        if (this._typingStopTimer) clearTimeout(this._typingStopTimer);
        if (this.enabled && this.socket?.connected) {
            this.socket.emit('notebook:leave_collab', { notebook_id: this.notebookId });
        }
        if (this.socket) {
            this.socket.off('notebook:presence');
            this.socket.off('notebook:collab_full');
            this.socket.off('notebook:cell_synced');
            this.socket.off('notebook:cursor_updated');
            this.socket.off('notebook:cell_selected');
            this.socket.off('notebook:typing_update');
        }
        this.enabled = false;
        this.activeUsers = {};
        this.typingUsers = {};
        this._remoteCellUsers = {};
        document.querySelectorAll('.cell.remote-active').forEach(el => {
            el.classList.remove('remote-active');
            const badge = el.querySelector('.collab-remote-badge');
            if (badge) badge.remove();
        });
        this._clearRemoteCursors();
    }
};

// ============================================================================
// HISTORY MANAGER - Git-like version control
// ============================================================================

const HistoryManager = {
    notebookId: null,
    history: [],
    totalCommits: 0,
    limit: 50,
    offset: 0,
    currentFilter: null,
    searchQuery: '',
    compareMode: false,
    compareBase: null,
    compareTarget: null,
    pinnedHashes: new Set(),

    init: function(notebookId) {
        this.notebookId = notebookId;
        this.history = [];
        this.offset = 0;
        this.currentFilter = null;
        this.searchQuery = '';
        this.compareMode = false;
        this.compareBase = null;
        this.compareTarget = null;
        this._loadPinnedHashes();
    },

    _loadPinnedHashes: function() {
        try {
            var key = 'phonos-pinned-commits-' + (this.notebookId || 'global');
            var raw = localStorage.getItem(key);
            this.pinnedHashes = new Set(raw ? JSON.parse(raw) : []);
        } catch (e) {
            this.pinnedHashes = new Set();
        }
    },

    _savePinnedHashes: function() {
        try {
            var key = 'phonos-pinned-commits-' + (this.notebookId || 'global');
            localStorage.setItem(key, JSON.stringify(Array.from(this.pinnedHashes)));
        } catch (e) {}
    },

    togglePin: function(commitHash) {
        if (this.pinnedHashes.has(commitHash)) {
            this.pinnedHashes.delete(commitHash);
        } else {
            this.pinnedHashes.add(commitHash);
        }
        this._savePinnedHashes();
        this.renderHistoryPanel(document.getElementById('history-commits'));
    },

    loadHistory: function(append) {
        if (!this.notebookId) return Promise.resolve([]);
        var url = '/api/notebooks/' + this.notebookId + '/history?limit=' + this.limit + '&offset=' + this.offset;
        if (this.currentFilter) url += '&action=' + encodeURIComponent(this.currentFilter);
        if (this.searchQuery) url += '&search=' + encodeURIComponent(this.searchQuery);
        var container = document.getElementById('history-commits');
        if (container) container.classList.add('nb-history-loading');
        return fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (container) container.classList.remove('nb-history-loading');
                if (append) {
                    this.history = this.history.concat(data.history || []);
                } else {
                    this.history = data.history || [];
                }
                this.totalCommits = data.total || 0;
                return this.history;
            }.bind(this))
            .catch(function(err) {
                if (container) container.classList.remove('nb-history-loading');
                console.error('[History] Failed to load:', err);
                return [];
            });
    },

    loadMore: function() {
        this.offset += this.limit;
        return this.loadHistory(true);
    },

    createCommit: function(message) {
        if (!this.notebookId) return Promise.reject('No notebook ID');
        return fetch(`/api/notebooks/${this.notebookId}/history`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: message, timestamp: new Date().toISOString() }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    this.history.unshift(data.commit);
                    this.totalCommits++;
                }
                return data;
            });
    },

    getSnapshot: function(commitHash) {
        return fetch(`/api/notebooks/${this.notebookId}/history/${commitHash}`)
            .then(r => r.json());
    },

    getDiff: function(commitHash) {
        return fetch(`/api/notebooks/${this.notebookId}/history/${commitHash}/diff`)
            .then(r => r.json());
    },

    restoreCommit: function(commitHash) {
        return fetch(`/api/notebooks/${this.notebookId}/restore/${commitHash}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    // Reload notebook cells from the restored version
                    return this.getSnapshot(data.commit.commit_hash);
                }
                throw new Error(data.error || 'Restore failed');
            });
    },

    filterByAction: function(action, container) {
        if (container) container.querySelectorAll('.nb-history-filter-btn').forEach(function(b) {
            b.classList.toggle('active', (b.dataset.action || '') === (action || ''));
        });
        this.currentFilter = action || null;
        this.offset = 0;
        return this._fetchHistory(0, container);
    },

    searchHistory: function(query, container) {
        this.searchQuery = (query || '').trim();
        this.offset = 0;
        return this._fetchHistory(0, container);
    },

    _fetchHistory: function(offset, container) {
        if (!this.notebookId) return Promise.resolve([]);
        var url = '/api/notebooks/' + this.notebookId + '/history?limit=' + this.limit + '&offset=' + offset;
        if (this.currentFilter) url += '&action=' + encodeURIComponent(this.currentFilter);
        if (this.searchQuery) url += '&search=' + encodeURIComponent(this.searchQuery);
        if (container) container.classList.add('nb-history-loading');
        return fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (container) container.classList.remove('nb-history-loading');
                this.history = data.history || [];
                this.totalCommits = data.total || 0;
                this.offset = offset;
                if (container) this.renderHistoryPanel(container);
                return this.history;
            }.bind(this))
            .catch(function(err) {
                if (container) container.classList.remove('nb-history-loading');
                console.error('[History] Fetch error:', err);
            });
    },

    setCompareMode: function(enabled, container) {
        this.compareMode = !!enabled;
        if (!this.compareMode) {
            this.compareBase = null;
            this.compareTarget = null;
        }
        this.renderHistoryPanel(container);
    },

    selectCompareCommit: function(commitHash) {
        if (this.compareBase === commitHash) {
            this.compareBase = null;
        } else if (this.compareTarget === commitHash) {
            this.compareTarget = null;
        } else if (!this.compareBase) {
            this.compareBase = commitHash;
        } else if (!this.compareTarget) {
            this.compareTarget = commitHash;
        } else {
            // Both full: shift target -> base, set new target
            this.compareBase = this.compareTarget;
            this.compareTarget = commitHash;
        }
        this.renderHistoryPanel(document.getElementById('history-commits'));
    },

    compareSelected: function() {
        if (!this.compareBase || !this.compareTarget) return;
        window.open(`/api/notebooks/${this.notebookId}/history/${this.compareBase}/compare/${this.compareTarget}?view=1`, '_blank');
    },

    exportCommit: function(commitHash) {
        window.open(`/api/notebooks/${this.notebookId}/history/${commitHash}/ipynb`, '_blank');
    },

    renderHistoryPanel: function(container) {
        if (!container) return;

        container.innerHTML = `
            <div class="nb-history-panel">
                <div class="nb-history-header">
                    <span class="nb-history-title">${window.i18n ? window.i18n.t('notebook.history') : 'History'} (${this.totalCommits})</span>
                    <button class="nb-history-commit-btn" onclick="HistoryManager._showCommitDialog()" title="${NbT('commitChanges', 'Commit changes')}">
                        <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8zm9 3V9h2V7H9V5H7v2H5v2h2v2z"/></svg>
                    </button>
                </div>
                <div class="nb-history-search">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
                    <input type="text" id="nb-history-search-input" placeholder="${NbT('searchHistory', 'Search history...')}" value="${this._escape(this.searchQuery)}">
                </div>
                <div class="nb-history-filter">
                    <button class="nb-history-filter-btn${!this.currentFilter ? ' active' : ''}" data-action="">All</button>
                    <button class="nb-history-filter-btn${this.currentFilter === 'manual_commit' ? ' active' : ''}" data-action="manual_commit">Commits</button>
                    <button class="nb-history-filter-btn${this.currentFilter === 'auto_save' ? ' active' : ''}" data-action="auto_save">Auto-saves</button>
                    <button class="nb-history-filter-btn${this.currentFilter === 'restore' ? ' active' : ''}" data-action="restore">Restores</button>
                </div>
                <div class="nb-history-toolbar">
                    <button class="nb-history-tool-btn${this.compareMode ? ' active' : ''}" id="nb-history-compare-toggle">${this.compareMode ? (NbT('exitCompare', 'Exit Compare')) : (NbT('compareVersions', 'Compare Versions'))}</button>
                    ${this.compareMode && this.compareBase && this.compareTarget ? '<button class="nb-history-tool-btn primary" id="nb-history-run-compare">' + (NbT('viewDiff', 'View Diff')) + '</button>' : ''}
                    ${this.compareMode ? '<span class="nb-history-compare-hint">' + (NbT('selectTwoCommits', 'Select two commits')) + '</span>' : ''}
                </div>
                <div class="nb-history-list"></div>
                ${this.history.length < this.totalCommits ? '<button class="nb-history-load-more">' + (NbT('loadMore', 'Load more')) + '</button>' : ''}
            </div>
        `;

        var listEl = container.querySelector('.nb-history-list');
        var loadMoreBtn = container.querySelector('.nb-history-load-more');
        var self = this;

        // Bind search input
        var searchInput = container.querySelector('#nb-history-search-input');
        if (searchInput) {
            var searchTimeout = null;
            searchInput.addEventListener('input', function() {
                clearTimeout(searchTimeout);
                var value = this.value;
                searchTimeout = setTimeout(function() {
                    self.searchHistory(value, container);
                }, 300);
            });
            searchInput.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') {
                    this.value = '';
                    self.searchHistory('', container);
                }
            });
        }

        // Bind filter buttons
        container.querySelectorAll('.nb-history-filter-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                self.filterByAction(this.dataset.action || null, container);
            });
        });

        // Bind compare toggle
        var compareToggle = container.querySelector('#nb-history-compare-toggle');
        if (compareToggle) {
            compareToggle.addEventListener('click', function() {
                self.setCompareMode(!self.compareMode, container);
            });
        }

        var runCompare = container.querySelector('#nb-history-run-compare');
        if (runCompare) {
            runCompare.addEventListener('click', function() {
                self._showCompareDialog();
            });
        }

        if (loadMoreBtn) {
            loadMoreBtn.addEventListener('click', function() {
                self.loadMore().then(function() { self.renderHistoryPanel(container); });
            });
        }

        if (this.history.length === 0) {
            listEl.innerHTML = '<div class="nb-history-empty">' + (NbT('noHistory', 'No history yet. Save the notebook to create snapshots.')) + '</div>';
            return;
        }

        var self = this;
        this.history.forEach(function(entry) {
            var item = document.createElement('div');
            item.className = 'nb-history-item';
            item.dataset.commit = entry.commit_hash;

            var actionLabels = {
                auto_save: NbT('autoSave', 'Auto-save'),
                manual_commit: NbT('manualCommit', 'Commit'),
                cell_change: NbT('cellChange', 'Cell change'),
                rename: window.i18n ? window.i18n.t('notebook.rename') : 'Renamed',
                restore: NbT('restore', 'Restored'),
                import: NbT('import', 'Imported'),
            };

            var actionLabel = actionLabels[entry.action] || entry.action;
            var isManual = entry.action === 'manual_commit' || entry.action === 'restore';
            var commitMsg = entry.message || (isManual ? '' : actionLabel);

            var isPinned = self.pinnedHashes.has(entry.commit_hash);
            var isCompareBase = self.compareBase === entry.commit_hash;
            var isCompareTarget = self.compareTarget === entry.commit_hash;
            var compareStateClass = '';
            if (this.compareMode) {
                if (isCompareBase) compareStateClass = ' compare-base';
                else if (isCompareTarget) compareStateClass = ' compare-target';
                else compareStateClass = ' compare-selectable';
            }

            item.className = 'nb-history-item' + compareStateClass + (isPinned ? ' pinned' : '');
            item.innerHTML = ''
                + '<div class="nb-history-dot ' + (isManual ? 'manual' : 'auto') + '"></div>'
                + '<div class="nb-history-content">'
                +   '<div class="nb-history-msg">' + self._escape(commitMsg || actionLabel) + '</div>'
                +   '<div class="nb-history-meta">'
                +     '<span class="nb-history-user">' + self._escape(entry.username || '') + '</span>'
                +     '<span class="nb-history-time">' + self._formatTime(entry.created_at) + '</span>'
                +     '<span class="nb-history-action ' + entry.action + '">' + self._escape(actionLabel) + '</span>'
                +     '<span class="nb-history-hash">' + (entry.commit_hash ? entry.commit_hash.substring(0, 8) : '') + '</span>'
                +     '<span class="nb-history-cells">' + (entry.cell_count || 0) + ' cells</span>'
                +   '</div>'
                + '</div>'
                + '<div class="nb-history-actions">'
                +   '<span class="nb-history-pin-btn' + (isPinned ? ' active' : '') + '" data-hash="' + entry.commit_hash + '" title="' + (NbT('pinCommit', 'Pin commit')) + '">'
                +     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><path d="M12 2l-5 9h10l-5-9z"/><path d="M12 11v9"/><path d="M8 20h8"/></svg>'
                +   '</span>'
                +   '<span class="nb-history-export-btn" data-hash="' + entry.commit_hash + '" title="' + (NbT('exportAsIpynb', 'Export as .ipynb')) + '">'
                +     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="12" height="12"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
                +   '</span>'
                +   '<span class="nb-history-restore-btn" data-hash="' + entry.commit_hash + '" title="' + (NbT('restoreThisVersion', 'Restore this version')) + '">'
                +     '<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor"><path d="M1 2v4h4M15 14v-4h-4"/><path d="M3.51 9a6.5 6.5 0 0 1 10.8-3.04L15 6.5M1 9.5l.69.54A6.5 6.5 0 0 0 12.49 7"/></svg>'
                +   '</span>'
                + '</div>';

            item.querySelector('.nb-history-pin-btn').addEventListener('click', function(e) {
                e.stopPropagation();
                self.togglePin(entry.commit_hash);
            });

            item.querySelector('.nb-history-export-btn').addEventListener('click', function(e) {
                e.stopPropagation();
                self.exportCommit(entry.commit_hash);
            });

            item.querySelector('.nb-history-restore-btn').addEventListener('click', function(e) {
                e.stopPropagation();
                if (!confirm(NbT('confirmRestore', 'Restore this version? Unsaved changes will be saved as a new commit.'))) return;
                self.restoreCommit(entry.commit_hash).then(function(snapshot) {
                    if (snapshot && snapshot.cells && window.NotebookModule && NotebookModule.loadCells) {
                        NotebookModule.loadCells(snapshot.cells);
                        if (window.Toast) Toast.success(NbT('restoreSuccess', 'Version restored'));
                        self.loadHistory().then(function() { self.renderHistoryPanel(document.getElementById('history-commits')); });
                    }
                }).catch(function(err) {
                    if (window.Toast) Toast.error(err.message || 'Restore failed');
                });
            });

            item.addEventListener('click', function() {
                if (self.compareMode) {
                    self.selectCompareCommit(entry.commit_hash);
                } else {
                    self._showCommitDetail(entry);
                }
            });
            listEl.appendChild(item);
        });
    },

    _showCommitDialog: function() {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal nb-commit-dialog">
                <div class="modal-header">
                    <h3>${NbT('commitChanges', 'Commit Changes')}</h3>
                    <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">&times;</button>
                </div>
                <div class="modal-body">
                    <textarea id="commit-message-input" placeholder="${NbT('commitMessagePlaceholder', 'Describe your changes...')}" rows="3" style="width:100%;background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--border-color);border-radius: var(--r-ui, 0px);padding:8px;resize:vertical;"></textarea>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">${window.i18n ? window.i18n.t('cancel') : 'Cancel'}</button>
                    <button class="btn btn-primary" id="commit-confirm-btn">${NbT('commit', 'Commit')}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        overlay.querySelector('#commit-confirm-btn').addEventListener('click', () => {
            const msg = overlay.querySelector('#commit-message-input').value.trim();
            if (!msg) return;
            this.createCommit(msg).then(data => {
                overlay.remove();
                if (data.success) {
                    this.renderHistoryPanel(document.getElementById('history-commits'));
                    if (window.Toast) Toast.success(NbT('commitCreated', 'Commit created'));
                }
            });
        });
    },

    _showCommitDetail: function(entry) {
        this.getDiff(entry.commit_hash).then(diffData => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay nb-history-modal-overlay';
            overlay.style.zIndex = '1000';

            const diff = diffData?.diff || {};
            const parentInfo = diffData?.parent;
            const isInitial = !entry.parent_hash;
            const details = diff.changed_details || [];

            // Git-style unified diff renderer. Uses backend-provided
            // diff_lines (LCS) when available; otherwise falls back to a
            // simple before/after preview so older commits still render.
            const renderDiffLines = (lines) => {
                if (!lines || !lines.length) return '';
                return '<div class="nb-history-unified-diff">' + lines.map(l => {
                    const cls = l.type === 'add' ? 'add' : (l.type === 'del' ? 'del' : 'ctx');
                    const sign = l.type === 'add' ? '+' : (l.type === 'del' ? '-' : ' ');
                    const oldNo = l.old_no != null ? String(l.old_no).padStart(4, ' ') : '    ';
                    const newNo = l.new_no != null ? String(l.new_no).padStart(4, ' ') : '    ';
                    return '<div class="nb-diff-line ' + cls + '"><span class="nb-diff-gutter">' + this._escape(oldNo) + ' ' + this._escape(newNo) + '</span><span class="nb-diff-sign">' + sign + '</span><span class="nb-diff-text">' + this._escape(l.text) + '</span></div>';
                }).join('') + '</div>';
            };

            const renderSource = (source) => {
                if (!source) return '<em style="color:var(--text-tertiary)">(empty)</em>';
                const lines = String(source).split('\n').slice(0, 8);
                return lines.map(l => this._escape(l)).join('<br>');
            };

            const renderCellList = (items, color, label) => {
                if (!items || !items.length) return '';
                return `
                    <div class="nb-history-diff-section">
                        <div class="nb-history-diff-section-title" style="color:${color}">${label} (${items.length})</div>
                        ${items.slice(0, 20).map(item => {
                            const cid = typeof item === 'string' ? item : item.cell_id;
                            const preview = typeof item === 'object' ? item : null;
                            const diffHtml = preview && preview.diff_lines ? renderDiffLines(preview.diff_lines) : '';
                            const fallbackPreview = preview && !preview.diff_lines ? `
                                <div class="nb-history-diff-cell-preview">
                                    <div class="nb-history-diff-before">${renderSource(preview.before_preview)}</div>
                                    <div class="nb-history-diff-after">${renderSource(preview.after_preview)}</div>
                                </div>` : '';
                            return `
                                <div class="nb-history-diff-cell">
                                    <div class="nb-history-diff-cell-id">${this._escape(cid)}</div>
                                    ${diffHtml || fallbackPreview}
                                </div>
                            `;
                        }).join('')}
                        ${items.length > 20 ? `<div class="nb-history-diff-more">...and ${items.length - 20} more</div>` : ''}
                    </div>
                `;
            };

            overlay.innerHTML = `
                <div class="modal nb-commit-detail nb-history-detail-modal">
                    <div class="modal-header">
                        <h3>${this._escape(entry.message || 'Commit ' + entry.commit_hash?.substring(0, 8))}</h3>
                        <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">&times;</button>
                    </div>
                    <div class="modal-body">
                        <div class="nb-history-detail-meta">
                            <span>${this._escape(entry.username || '')}</span>
                            <span class="nb-history-detail-sep">|</span>
                            <span>${this._formatTime(entry.created_at)}</span>
                            <span class="nb-history-detail-sep">|</span>
                            <span>${entry.commit_hash?.substring(0, 12) || ''}</span>
                            ${entry.parent_hash ? `<span class="nb-history-detail-sep">|</span><span>parent: ${entry.parent_hash.substring(0, 8)}</span>` : ''}
                            <span class="nb-history-detail-sep">|</span>
                            <span>${entry.cell_count || 0} cells</span>
                        </div>
                        <div class="nb-history-detail-summary">
                            <strong>${diff.summary || (isInitial ? 'Initial commit' : 'No changes')}</strong>
                            ${diff.added?.length ? ` <span class="nb-history-stat added">+${diff.added.length}</span>` : ''}
                            ${diff.removed?.length ? ` <span class="nb-history-stat removed">-${diff.removed.length}</span>` : ''}
                            ${diff.changed?.length ? ` <span class="nb-history-stat changed">~${diff.changed.length}</span>` : ''}
                        </div>
                        <div class="nb-history-detail-actions">
                            <button class="nb-btn small secondary" id="nb-history-detail-export">${NbT('exportAsIpynb', 'Export .ipynb')}</button>
                            <button class="nb-btn small secondary" id="nb-history-detail-compare" data-hash="${entry.commit_hash}">${NbT('compareWith', 'Compare with...')}</button>
                        </div>
                        <div class="commit-cells-preview">
                            ${isInitial ? '<div class="nb-history-initial-msg">' + (NbT('initialCommit', 'Initial commit — all cells are new')) + '</div>' : ''}
                            ${renderCellList(diff.changed_details || diff.changed, 'var(--accent-warning,#D9822B)', NbT('modifiedCells', 'Modified cells'))}
                            ${renderCellList(diff.added_details || diff.added, 'var(--accent-success,#0F9960)', NbT('addedCells', 'Added cells'))}
                            ${renderCellList(diff.removed_details || diff.removed, 'var(--accent-danger,#DB3737)', NbT('removedCells', 'Removed cells'))}
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">${window.i18n ? window.i18n.t('close') : 'Close'}</button>
                        <button class="btn btn-primary restore-commit-btn" data-hash="${entry.commit_hash}" style="background:#d97706;">${NbT('restoreThisVersion', 'Restore this version')}</button>
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);

            overlay.querySelector('#nb-history-detail-export').addEventListener('click', () => {
                this.exportCommit(entry.commit_hash);
            });

            overlay.querySelector('#nb-history-detail-compare').addEventListener('click', () => {
                overlay.remove();
                this.compareBase = entry.commit_hash;
                this.compareTarget = null;
                this.compareMode = true;
                this.renderHistoryPanel(document.getElementById('history-commits'));
            });

            overlay.querySelector('.restore-commit-btn').addEventListener('click', () => {
                if (!confirm(NbT('confirmRestore', 'Are you sure you want to restore this version? Current changes will be saved as a new commit before restoring.'))) return;
                this.restoreCommit(entry.commit_hash).then(snapshot => {
                    if (snapshot && snapshot.cells) {
                        overlay.remove();
                        if (window.NotebookModule && NotebookModule.loadCells) {
                            NotebookModule.loadCells(snapshot.cells);
                        }
                        if (window.Toast) Toast.success(NbT('restoreSuccess', 'Version restored'));
                        this.loadHistory().then(() => {
                            this.renderHistoryPanel(document.getElementById('history-commits'));
                        });
                    }
                }).catch(err => {
                    if (window.Toast) Toast.error(err.message || 'Restore failed');
                });
            });
        }).catch(err => {
            console.error('[History] Failed to get diff:', err);
        });
    },

    _showCompareDialog: function() {
        if (!this.compareBase || !this.compareTarget) return;
        var base = this.history.find(h => h.commit_hash === this.compareBase);
        var target = this.history.find(h => h.commit_hash === this.compareTarget);
        if (!base || !target) {
            this.showToast && this.showToast('Selected commits not loaded');
            return;
        }

        fetch(`/api/notebooks/${this.notebookId}/history/${this.compareBase}/compare/${this.compareTarget}`)
            .then(r => r.json())
            .then(data => {
                if (data.error) throw new Error(data.error);
                const diff = data.diff || {};
                const overlay = document.createElement('div');
                overlay.className = 'modal-overlay nb-history-modal-overlay';
                overlay.style.zIndex = '1000';

                const renderSource = (source) => {
                    if (!source) return '<em style="color:var(--text-tertiary)">(empty)</em>';
                    const lines = String(source).split('\n').slice(0, 10);
                    return lines.map(l => this._escape(l)).join('<br>');
                };

                const renderDiffLines = (lines) => {
                    if (!lines || !lines.length) return '';
                    return '<div class="nb-history-unified-diff">' + lines.map(l => {
                        const cls = l.type === 'add' ? 'add' : (l.type === 'del' ? 'del' : 'ctx');
                        const sign = l.type === 'add' ? '+' : (l.type === 'del' ? '-' : ' ');
                        const oldNo = l.old_no != null ? String(l.old_no).padStart(4, ' ') : '    ';
                        const newNo = l.new_no != null ? String(l.new_no).padStart(4, ' ') : '    ';
                        return '<div class="nb-diff-line ' + cls + '"><span class="nb-diff-gutter">' + this._escape(oldNo) + ' ' + this._escape(newNo) + '</span><span class="nb-diff-sign">' + sign + '</span><span class="nb-diff-text">' + this._escape(l.text) + '</span></div>';
                    }).join('') + '</div>';
                };

                const renderCellList = (items, color, label, isChange) => {
                    if (!items || !items.length) return '';
                    return `
                        <div class="nb-history-diff-section">
                            <div class="nb-history-diff-section-title" style="color:${color}">${label} (${items.length})</div>
                            ${items.slice(0, 50).map(item => {
                                const cid = isChange ? item.cell_id : (typeof item === 'string' ? item : item.cell_id);
                                const diffHtml = (typeof item === 'object' && item.diff_lines) ? renderDiffLines(item.diff_lines) : '';
                                const fallbackPreview = isChange ? `
                                    <div class="nb-history-diff-cell-preview">
                                        <div class="nb-history-diff-before">${renderSource(item.before_preview)}</div>
                                        <div class="nb-history-diff-after">${renderSource(item.after_preview)}</div>
                                    </div>` : '';
                                return `
                                    <div class="nb-history-diff-cell">
                                        <div class="nb-history-diff-cell-id">${this._escape(cid)}</div>
                                        ${diffHtml || fallbackPreview}
                                    </div>
                                `;
                            }).join('')}
                            ${items.length > 50 ? `<div class="nb-history-diff-more">...and ${items.length - 50} more</div>` : ''}
                        </div>
                    `;
                };

                overlay.innerHTML = `
                    <div class="modal nb-commit-detail nb-history-detail-modal">
                        <div class="modal-header">
                            <h3>${NbT('compareVersions', 'Compare Versions')}</h3>
                            <button class="modal-close" onclick="this.closest('.modal-overlay').remove()">&times;</button>
                        </div>
                        <div class="modal-body">
                            <div class="nb-history-compare-header">
                                <div class="nb-history-compare-side">
                                    <div class="nb-history-compare-label">${NbT('base', 'Base')}</div>
                                    <div class="nb-history-compare-hash">${this.compareBase.substring(0, 8)}</div>
                                    <div class="nb-history-compare-msg">${this._escape(base.message || '')}</div>
                                </div>
                                <div class="nb-history-compare-arrow">→</div>
                                <div class="nb-history-compare-side">
                                    <div class="nb-history-compare-label">${NbT('target', 'Target')}</div>
                                    <div class="nb-history-compare-hash">${this.compareTarget.substring(0, 8)}</div>
                                    <div class="nb-history-compare-msg">${this._escape(target.message || '')}</div>
                                </div>
                            </div>
                            <div class="nb-history-detail-summary">
                                <strong>${diff.summary || 'No changes'}</strong>
                                ${diff.added?.length ? ` <span class="nb-history-stat added">+${diff.added.length}</span>` : ''}
                                ${diff.removed?.length ? ` <span class="nb-history-stat removed">-${diff.removed.length}</span>` : ''}
                                ${diff.changed?.length ? ` <span class="nb-history-stat changed">~${diff.changed.length}</span>` : ''}
                            </div>
                            <div class="commit-cells-preview">
                                ${renderCellList(diff.changed_details, 'var(--accent-warning,#D9822B)', NbT('modifiedCells', 'Modified cells'), true)}
                                ${renderCellList(diff.added_details || diff.added, 'var(--accent-success,#0F9960)', NbT('addedCells', 'Added cells'), false)}
                                ${renderCellList(diff.removed_details || diff.removed, 'var(--accent-danger,#DB3737)', NbT('removedCells', 'Removed cells'), false)}
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">${window.i18n ? window.i18n.t('close') : 'Close'}</button>
                            <button class="btn btn-primary" id="nb-history-compare-restore" data-hash="${this.compareTarget}" style="background:#d97706;">${NbT('restoreTargetVersion', 'Restore target version')}</button>
                        </div>
                    </div>
                `;

                document.body.appendChild(overlay);

                overlay.querySelector('#nb-history-compare-restore').addEventListener('click', () => {
                    if (!confirm(NbT('confirmRestore', 'Are you sure you want to restore this version?'))) return;
                    this.restoreCommit(this.compareTarget).then(snapshot => {
                        if (snapshot && snapshot.cells) {
                            overlay.remove();
                            if (window.NotebookModule && NotebookModule.loadCells) {
                                NotebookModule.loadCells(snapshot.cells);
                            }
                            if (window.Toast) Toast.success(NbT('restoreSuccess', 'Version restored'));
                            this.loadHistory().then(() => {
                                this.renderHistoryPanel(document.getElementById('history-commits'));
                            });
                        }
                    }).catch(err => {
                        if (window.Toast) Toast.error(err.message || 'Restore failed');
                    });
                });
            }).catch(err => {
                console.error('[History] Compare failed:', err);
                if (window.Toast) Toast.error(err.message || 'Compare failed');
            });
    },

    _formatTime: function(ts) {
        if (!ts) return '';
        const d = new Date(ts);
        const now = new Date();
        const diffMs = now - d;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return diffMins + 'm ago';
        if (diffHours < 24) return diffHours + 'h ago';
        if (diffDays < 7) return diffDays + 'd ago';
        return d.toLocaleDateString();
    },

    _escape: function(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
};

// ============================================================================
// EXPOSE to NotebookModule
// ============================================================================

// Assign to the enclosing scope so the Notebook class can use them
window.CollaborationManager = CollaborationManager;
window.HistoryManager = HistoryManager;

})();
