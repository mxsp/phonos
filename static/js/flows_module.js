/**
 * Flows Module v3.0 - Immersive Visual Workflow Editor
 * 
 * Architecture:
 * - FlowsManager: App integration, routing between Dashboard/Editor
 * - FlowsDashboard: Grid view of workflows
 * - FlowsEditor: The core canvas editor, handles interaction & state
 * - NodeRenderer: Canvas drawing logic (Cards, Bézier curves)
 * - FlowsAPI: Backend communication
 */

// =============================================================================
// CONFIGURATION & SCHEMAS
// =============================================================================

const NodeConfigSchemas = {
    input: {
        title: 'Input Node',
        color: '#10b981',
        icon: '📥',
        description: 'Start point for workflow data',
        outputs: ['data'],
        params: []
    },
    databank: {
        title: 'PDB Databank',
        color: '#10b981',
        icon: '🗄️',
        description: 'Fetch structures from PDB',
        outputs: ['pdb', 'structure'],
        params: [
            { id: 'pdb_id', type: 'text', label: 'PDB ID', placeholder: 'e.g., 1ABC', required: true },
            { id: 'chain', type: 'text', label: 'Chain', placeholder: 'A' }
        ]
    },
    upload: {
        title: 'Upload FASTA',
        color: '#10b981',
        icon: '📄',
        description: 'Upload sequence file',
        outputs: ['fasta', 'sequence'],
        params: [
            { id: 'file_id', type: 'file_select', label: 'Select File' },
            { id: 'sequence_index', type: 'number', label: 'Index', default: 0 }
        ]
    },
    esmfold: {
        title: 'ESMFold',
        color: '#3b82f6',
        icon: '🧬',
        description: 'Fast structure prediction',
        inputs: ['sequence'],
        outputs: ['pdb'],
        params: [
            { id: 'model_version', type: 'select', label: 'Version', options: ['v1', 'v2'], default: 'v2' }
        ]
    },
    alphafold: {
        title: 'AlphaFold2',
        color: '#8b5cf6',
        icon: '🔬',
        description: 'High-accuracy prediction',
        inputs: ['sequence', 'msa'],
        outputs: ['pdb', 'confidence'],
        params: [
            { id: 'num_models', type: 'number', label: 'Models', default: 1 }
        ]
    },
    diffdock: {
        title: 'DiffDock',
        color: '#3b82f6',
        icon: '⚛️',
        description: 'Molecular docking',
        inputs: ['protein', 'ligand'],
        outputs: ['complex'],
        params: [
            { id: 'num_poses', type: 'number', label: 'Poses', default: 10 }
        ]
    },
    analysis: {
        title: 'Analysis',
        color: '#f59e0b',
        icon: '📊',
        description: 'Structure metrics',
        inputs: ['structure'],
        outputs: ['report'],
        params: [
            { id: 'calculate_rmsd', type: 'checkbox', label: 'RMSD', default: true }
        ]
    },
    viewer: {
        title: '3D Viewer',
        color: '#f97316',
        icon: '🎯',
        description: 'Visualize output',
        inputs: ['structure'],
        outputs: [],
        params: [
            { id: 'style', type: 'select', label: 'Style', options: ['cartoon', 'stick', 'surface'], default: 'cartoon' }
        ]
    },
    email: {
        title: 'Email Notify',
        color: '#a855f7',
        icon: '📧',
        description: 'Send results via email',
        inputs: ['any'],
        outputs: [],
        params: [
            { id: 'recipients', type: 'text', label: 'To' }
        ]
    }
};

const BuiltInTemplates = [
    {
        id: 'protein-folding',
        name: 'Basic Protein Folding',
        description: 'Upload FASTA -> ESMFold -> Viewer',
        nodes: [
            { type: 'upload', x: 100, y: 300 },
            { type: 'esmfold', x: 500, y: 300 },
            { type: 'viewer', x: 900, y: 300 }
        ],
        connections: [
            { from: 0, to: 1 },
            { from: 1, to: 2 }
        ]
    }
];

// =============================================================================
// FLOWS MANAGER (Entry Point)
// =============================================================================

class FlowsManager {
    constructor(app) {
        this.app = app; // Reference to main App object (Views)
        this.currentProject = null;
        this.currentFlow = null;
        
        // Sub-modules
        this.dashboard = new FlowsDashboard(this);
        this.editor = new FlowsEditor(this);
    }

    async render() {
        this.injectCSS();

        if (typeof Socket !== 'undefined' && Socket.socket) {
            Socket.socket.off('flow_update');
            Socket.socket.on('flow_update', (data) => this.handleFlowUpdate(data));
        }

        const projectId = sessionStorage.getItem('selectedProject');
        if (!projectId) {
            this.app.contentArea.innerHTML = this.renderProjectSelector();
            return;
        }

        try {
            const [projectsResponse, modelsResponse, userModelsResponse, filesResponse] = await Promise.all([
                API.get('/api/projects'),
                API.get('/api/available_models'),
                API.get('/api/user-models').catch(() => ({ models: [] })), // Handle no user models
                API.get(`/api/projects/${projectId}/files`).catch(() => ([])) // Handle no files
            ]);
            
            this.currentProject = projectsResponse.find(p => p.id == projectId);
            if (!this.currentProject) throw new Error('Project not found');

            this.availableModels = Array.isArray(modelsResponse) ? modelsResponse : (modelsResponse.models || []);
            this.userModels = Array.isArray(userModelsResponse) ? userModelsResponse.models : (userModelsResponse.models || []);
            this.projectFiles = filesResponse;

            // Keep hidden/offline models out of flow node choices.
            const hiddenModelIds = new Set(['type-5', 'boltz2-enhanced']);
            this.availableModels = this.availableModels.filter(m => m.available !== false && !hiddenModelIds.has(m.id)).map(m => {
                if (m.id === 'type-9') {
                    m.beta = true; // Add Beta flag to Boltz2 (type-9)
                }
                return m;
            });

            // Re-initialize editor and dashboard with fetched data
            this.dashboard = new FlowsDashboard(this, this.availableModels, this.userModels);
            this.editor = new FlowsEditor(this, this.availableModels, this.userModels, this.projectFiles);

        } catch (e) {
            console.error(e);
            this.app.contentArea.innerHTML = '<div class="alert alert-error">Failed to load project context or models.</div>';
            return;
        }

        this.showDashboard();
    }

    injectCSS() {
        if (!document.getElementById('flows-css')) {
            const link = document.createElement('link');
            link.id = 'flows-css';
            link.rel = 'stylesheet';
            link.href = '/static/css/flows.css?v=3.0';
            document.head.appendChild(link);
        }
    }

    handleFlowUpdate(data) {
        if (this.currentFlow && this.currentFlow.flow_id === data.execution_id) {
            // Update editor if viewing this flow
            this.editor.handleUpdate(data);
        }
    }

    renderProjectSelector() {
        return `
            <div class="empty-state">
                <h3>Select a Project</h3>
                <p>Workflows belong to specific projects. Please select one to continue.</p>
                <button class="btn btn-primary" onclick="window.FlowsModule.showCreateModal()">
                    Go to Projects
                </button>
            </div>
        `;
    }

    async showDashboard() {
        this.currentFlow = null;
        await this.dashboard.render(this.app.contentArea, this.currentProject.id);
    }

    async openFlow(flowId) {
        try {
            // Load flow data
            const response = await API.get(`
/api/flows/${flowId}
`);
            this.currentFlow = response.flow;
            
            // Switch to Editor
            this.editor.render(this.app.contentArea, this.currentFlow);
        } catch (e) {
            Toast.error('Failed to open workflow');
            console.error(e);
        }
    }

    async createFlow(name, description) {
        try {
            const response = await API.post(`
/api/projects/${this.currentProject.id}/flows`,
 {
                name, description,
                flow_data: { nodes: [], connections: [] }
            });
            Toast.success('Workflow created');
            this.openFlow(response.flow.flow_id);
        } catch (e) {
            Toast.error('Failed to create workflow');
        }
    }
}

// =============================================================================
// FLOWS DASHBOARD (Grid View)
// =============================================================================

class FlowsDashboard {
    constructor(manager) {
        this.manager = manager;
    }

    async render(container, projectId) {
        container.innerHTML = '<div class="loading-spinner">Loading workflows...</div>';
        
        try {
            const response = await API.get(`
/api/projects/${projectId}/flows`
);
            const flows = response.flows || [];

            container.innerHTML = `
                <div class="flows-dashboard">
                    <div class="flows-dashboard-header">
                        <div class="flows-dashboard-title">
                            <h1>
                                <span style="color: var(--color-primary)">⚡</span> 
                                Workflows
                            </h1>
                            <p class="text-muted">Manage computational pipelines for ${escapeHtml(this.manager.currentProject.name)}</p>
                        </div>
                        <div class="flows-actions">
                            <button class="btn btn-primary" onclick="window.FlowsModule.showCreateModal()">
                                + New Workflow
                            </button>
                        </div>
                    </div>

                    <div class="flows-grid">
                        ${flows.length === 0 ? this.renderEmpty() : flows.map(f => this.renderCard(f)).join('')}
                    </div>
                </div>
            `;
        } catch (e) {
            container.innerHTML = '<div class="alert alert-error">Failed to load flows</div>';
        }
    }

    renderCard(flow) {
        const nodeCount = flow.flow_data?.nodes?.length || 0;
        return `
            <div class="flow-card" onclick="window.FlowsModule.openFlow('${flow.flow_id}')">
                <div class="flow-card-header">
                    <div class="flow-card-title">${escapeHtml(flow.name)}</div>
                    <div class="badge badge-sm">${flow.status || 'Draft'}</div>
                </div>
                <div class="flow-card-desc text-muted text-sm">
                    ${escapeHtml(flow.description || 'No description')}
                </div>
                <div class="flow-card-meta">
                    <span>${nodeCount} Nodes</span>
                    <span>•</span>
                    <span>Updated ${new Date(flow.updated_at || flow.created_at).toLocaleDateString()}</span>
                </div>
            </div>
        `;
    }

    renderEmpty() {
        return `
            <div class="empty-state" style="grid-column: 1/-1;">
                <p>No workflows found. Create one to get started.</p>
            </div>
        `;
    }
}

// =============================================================================
// FLOWS EDITOR (Canvas & UI)
// =============================================================================

class FlowsEditor {
    constructor(manager, availableModels, userModels, projectFiles) {
        this.manager = manager;
        this.canvas = null;
        this.ctx = null;
        this.availableModels = availableModels;
        this.userModels = userModels;
        this.projectFiles = projectFiles;
        
        // Dynamically add fetched models to NodeConfigSchemas
        const dynamicNodeConfigSchemas = {};
        this.availableModels.forEach(m => {
            dynamicNodeConfigSchemas[m.id] = {
                id: m.id,
                title: m.short_name || m.name,
                color: '#6366f1', // Default color, can be made dynamic later
                icon: '🚀', // Default icon
                description: m.description,
                inputs: (m.input_modes && m.input_modes.includes('sequence')) ? ['sequence'] : ['file'], // Simplified input mapping
                outputs: ['result'], // Generic output
                params: m.custom_fields || []
            };
        });
        this.userModels.forEach(m => {
            const colorPalette = ['#f97316', '#06b6d4', '#8b5cf6', '#14b8a6', '#f43f5e', '#6366f1', '#22c55e', '#eab308', '#ec4899', '#0ea5e9'];
            const modelColor = colorPalette[m.id % colorPalette.length];
            dynamicNodeConfigSchemas[`user-${m.id}`] = {
                id: `user-${m.id}`,
                title: m.model_name,
                color: modelColor,
                icon: '👤', // User model icon
                description: m.model_description,
                inputs: m.input_schema.filter(f => f.type === 'sequence' || f.type === 'file').map(f => f.name), // Filter for relevant inputs
                outputs: ['result'], // Generic output
                params: m.input_schema || []
            };
        });

        // Combine static and dynamic schemas
        this.allNodeConfigSchemas = { ...NodeConfigSchemas, ...dynamicNodeConfigSchemas };
        this.renderer = new NodeRenderer(this.allNodeConfigSchemas); // Pass combined schemas to renderer
        
        // State
        this.nodes = [];
        this.connections = [];
        this.offset = { x: 0, y: 0 };
        this.zoom = 1;
        
        // Interaction State
        this.isDraggingCanvas = false;
        this.dragStart = { x: 0, y: 0 };
        this.draggingNode = null;
        this.connectingFrom = null; // Port we are dragging a wire from
        this.mousePos = { x: 0, y: 0 }; // Current mouse pos (screen)
        this.worldMouse = { x: 0, y: 0 }; // Current mouse pos (world)
    }

    render(container, flow) {
        this.flow = flow;
        this.nodes = flow.flow_data.nodes || [];
        this.connections = flow.flow_data.connections || [];

                container.innerHTML = `

                    <div class="flows-v3-container">

                        <!-- Toolbar -->

                        <div class="flows-toolbar">

                            <button class="control-btn" onclick="window.FlowsModule.goBack()" title="Back to Dashboard">

                                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>

                            </button>

                            <div class="vertical-divider"></div>

                            <input type="text" class="flows-title-edit" value="${escapeHtml(flow.name)}" onchange="window.FlowsModule.updateName(this.value)">

                            <div class="vertical-divider"></div>

                            <button class="control-btn text-primary" onclick="window.FlowsModule.saveFlow()" title="Save">

                                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/></svg>

                            </button>

                            <button class="control-btn text-accent" onclick="window.FlowsModule.executeFlow()" title="Run">

                                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>

                            </button>

                        </div>

        

                        <!-- Editor Area -->

                        <div class="flows-editor" id="flows-editor-area">

                            <canvas class="flows-canvas" id="flows-canvas"></canvas>

                            

                            <!-- Left: Node Library -->

                            <div class="flows-library">

                                <div class="library-header">

                                    <span>Nodes</span>

                                    <button class="btn btn-ghost btn-sm">Library</button>

                                </div>

                                <div class="library-search">
                                    <input type="text" id="flows-node-search" placeholder="Search nodes..." onkeyup="window.FlowsModule.filterNodes(this.value)">
                                </div>

                                <div class="library-content">

                                    ${this.renderLibraryItems()}

                                </div>

                            </div>

        

                            <!-- Right: Inspector (Hidden by default) -->

                            <div class="flows-inspector" id="flows-inspector">

                                <div class="inspector-header">

                                    <span id="inspector-title">Properties</span>

                                    <button class="btn-close" onclick="window.FlowsModule.closeInspector()" title="Close">×</button>

                                </div>

                                <div class="inspector-content" id="inspector-content"></div>

                            </div>

        

                            <!-- Bottom Left: Controls -->

                            <div class="flows-controls">

                                <button class="control-btn" onclick="window.FlowsModule.zoomIn()">+</button>

                                <button class="control-btn" onclick="window.FlowsModule.zoomOut()">-</button>

                                <button class="control-btn" onclick="window.FlowsModule.fitView()">Fit</button>

                            </div>

                        </div>

                    </div>

                `;

        this.initCanvas();
        this.centerView();
    }

    renderLibraryItems() {
        const categories = {
            'Sources': ['input', 'databank', 'upload'],
            'Official Models': [],
            'User Models': [],
            'Output & Analysis': ['analysis', 'viewer', 'email']
        };

        // Populate 'Official Models' and 'User Models' categories
        for (const key in this.allNodeConfigSchemas) {
            if (Object.prototype.hasOwnProperty.call(this.allNodeConfigSchemas, key)) {
                // Skip static nodes already categorized
                if (['input', 'databank', 'upload', 'analysis', 'viewer', 'email'].includes(key)) continue;

                const schema = this.allNodeConfigSchemas[key];
                if (key.startsWith('user-')) {
                    categories['User Models'].push(key);
                } else {
                    categories['Official Models'].push(key);
                }
            }
        }

        let html = '';
        for (const catName in categories) {
            if (Object.prototype.hasOwnProperty.call(categories, catName)) {
                const types = categories[catName];
                if (types.length === 0 && (catName === 'Official Models' || catName === 'User Models')) continue; // Skip empty dynamic categories

                html += `
                    <div class="node-category">
                        <div class="category-title">
                            ${catName}
                        </div>
                        <div class="category-nodes">
                            ${types.map(type => {
                                const schema = this.allNodeConfigSchemas[type];
                                return `
                                    <div class="library-node" draggable="true" ondragstart="window.FlowsModule.handleDragStart(event, '${type}')">
                                        <div class="library-node-icon" style="color: ${schema.color}">${schema.icon}</div>
                                        <span>${schema.title}</span>
                                    </div>
                                `;
                            }).join('')}
                        </div>
                    </div>
                `;
            }
        }
        return html;
    }

    initCanvas() {
        this.canvas = document.getElementById('flows-canvas');
        this.ctx = this.canvas.getContext('2d');
        const parent = this.canvas.parentElement;

        // Resize handler
        const resize = () => {
            this.canvas.width = parent.offsetWidth;
            this.canvas.height = parent.offsetHeight;
            this.draw();
        };
        window.addEventListener('resize', resize);
        resize();

        // Event Listeners
        this.canvas.addEventListener('mousedown', e => this.onMouseDown(e));
        this.canvas.addEventListener('mousemove', e => this.onMouseMove(e));
        this.canvas.addEventListener('mouseup', e => this.onMouseUp(e));
        this.canvas.addEventListener('wheel', e => this.onWheel(e));
        this.canvas.addEventListener('contextmenu', e => e.preventDefault());

    // Drag & Drop
    parent.addEventListener('dragover', e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
    });
    parent.addEventListener('drop', e => this.onDrop(e));

        // Start Loop
        requestAnimationFrame(() => this.loop());
    }

    // --- Interaction Handlers ---

    getMousePos(e) {
        const rect = this.canvas.getBoundingClientRect();
        return {
            x: e.clientX - rect.left,
            y: e.clientY - rect.top
        };
    }

    screenToWorld(pos) {
        return {
            x: (pos.x - this.offset.x) / this.zoom,
            y: (pos.y - this.offset.y) / this.zoom
        };
    }

    onMouseDown(e) {
        this.mousePos = this.getMousePos(e);
        this.worldMouse = this.screenToWorld(this.mousePos);

        // 1. Check for Nodes
        const clickedNode = this.renderer.hitTestNode(this.nodes, this.worldMouse);
        
        if (clickedNode) {
            // Check for Ports
            const port = this.renderer.hitTestPort(clickedNode, this.worldMouse);
            if (port) {
                // Start connection
                this.connectingFrom = { node: clickedNode, port: port };
            } else {
                // Drag node
                this.draggingNode = clickedNode;
                this.showInspector(clickedNode);
            }
            return;
        }

        // 2. Pan Canvas
        this.isDraggingCanvas = true;
        this.dragStart = this.mousePos;
    }

    onMouseMove(e) {
        const pos = this.getMousePos(e);
        const dx = pos.x - this.mousePos.x;
        const dy = pos.y - this.mousePos.y;
        this.mousePos = pos;
        this.worldMouse = this.screenToWorld(pos);

        if (this.draggingNode) {
            this.draggingNode.x += dx / this.zoom;
            this.draggingNode.y += dy / this.zoom;
        } else if (this.isDraggingCanvas) {
            this.offset.x += dx;
            this.offset.y += dy;
        }
    }

    onMouseUp(e) {
        if (this.connectingFrom) {
            // Check drop target
            const targetNode = this.renderer.hitTestNode(this.nodes, this.worldMouse);
            if (targetNode && targetNode !== this.connectingFrom.node) {
                const targetPort = this.renderer.hitTestPort(targetNode, this.worldMouse);
                if (targetPort && targetPort.type !== this.connectingFrom.port.type) {
                    // Create Connection
                    this.connections.push({
                        from: this.connectingFrom.port.type === 'output' ? this.connectingFrom.node.id : targetNode.id,
                        to: this.connectingFrom.port.type === 'input' ? this.connectingFrom.node.id : targetNode.id
                    });
                }
            }
        }

        this.isDraggingCanvas = false;
        this.draggingNode = null;
        this.connectingFrom = null;
    }

    onWheel(e) {
        e.preventDefault();
        const zoomSpeed = 0.1;
        const delta = e.deltaY > 0 ? -zoomSpeed : zoomSpeed;
        const newZoom = Math.max(0.1, Math.min(5, this.zoom + delta));
        
        // Zoom towards mouse
        const mouseWorldBefore = this.screenToWorld(this.mousePos);
        this.zoom = newZoom;
        const mouseWorldAfter = this.screenToWorld(this.mousePos);
        
        this.offset.x += (mouseWorldAfter.x - mouseWorldBefore.x) * this.zoom;
        this.offset.y += (mouseWorldAfter.y - mouseWorldBefore.y) * this.zoom;
    }

    onDrop(e) {
        e.preventDefault();
        const type = e.dataTransfer.getData('nodeType');
        if (type) {
            const pos = this.getMousePos(e);
            const worldPos = this.screenToWorld(pos);
            this.addNode(type, worldPos.x, worldPos.y);
        }
    }

    // --- Logic ---

    addNode(type, x, y) {
        const schema = this.allNodeConfigSchemas[type];
        if (!schema) {
            console.error(`Unknown node type: ${type}`);
            return;
        }
        this.nodes.push({
            id: 'n_' + Date.now(),
            type, x, y,
            w: 200, h: 120, // Default size, renderer might adjust
            config: {} // Default params
        });
    }

    centerView() {
        if (this.nodes.length === 0) {
            this.offset = { x: this.canvas.width / 2, y: this.canvas.height / 2 };
            return;
        }
        // Calculate bounding box and center...
    }

    showInspector(node) {
        const inspector = document.getElementById('flows-inspector');
        const content = document.getElementById('inspector-content');
        const title = document.getElementById('inspector-title');
        
        inspector.style.display = 'flex';
        const schema = this.allNodeConfigSchemas[node.type];
        if (!schema) {
            console.error(`No schema found for node type: ${node.type}`);
            return;
        }
        title.textContent = schema.title;
        title.style.color = schema.color;

        content.innerHTML = this.renderInspectorParams(node, schema);
    }

    renderInspectorParams(node, schema) {
        if (!schema.params || schema.params.length === 0) {
            return '<p class="text-muted" style="padding: 16px;">No configuration needed for this node.</p>';
        }

        return schema.params.map(p => {
            const fieldId = `node-param-${node.id}-${p.id}`;
            let inputHtml = '';
            const currentValue = node.config[p.id] !== undefined ? node.config[p.id] : p.default;
            const onChangeHandler = `window.FlowsModule.updateNodeParam('${node.id}', '${p.id}', this.value)`;

            switch (p.type) {
                case 'text':
                    inputHtml = `
                        <input type="text" class="form-input" id="${fieldId}"
                               value="${escapeHtml(currentValue || '')}"
                               onchange="${onChangeHandler}"
                               placeholder="${escapeHtml(p.placeholder || '')}">
                    `;
                    break;
                case 'number':
                    inputHtml = `
                        <input type="number" class="form-input" id="${fieldId}"
                               value="${currentValue}"
                               onchange="${onChangeHandler}"
                               ${p.min !== undefined ? `min="${p.min}"` : ''}
                               ${p.max !== undefined ? `max="${p.max}"` : ''}
                               ${p.step !== undefined ? `step="${p.step}"` : ''}
                               placeholder="${escapeHtml(p.placeholder || '')}">
                    `;
                    break;
                case 'select':
                    inputHtml = `
                        <select class="form-select" id="${fieldId}" onchange="${onChangeHandler}">
                            ${(p.options || []).map(opt => {
                                const optValue = typeof opt === 'object' ? opt.value : opt;
                                const optLabel = typeof opt === 'object' ? opt.label : opt;
                                const selected = optValue == currentValue ? 'selected' : ''; // Use == for loose comparison
                                return `<option value="${escapeHtml(optValue)}" ${selected}>${escapeHtml(optLabel)}</option>`;
                            }).join('')}
                        </select>
                    `;
                    break;
                case 'checkbox':
                    inputHtml = `
                        <label class="form-checkbox">
                            <input type="checkbox" id="${fieldId}" ${currentValue ? 'checked' : ''}
                                   onchange="window.FlowsModule.updateNodeParam('${node.id}', '${p.id}', this.checked)">
                            <span>${escapeHtml(p.checkbox_label || p.label)}</span>
                        </label>
                    `;
                    break;
                case 'file_select': // Custom type for selecting project files
                    const projectFiles = this.manager.projectFiles || [];
                    inputHtml = `
                        <select class="form-select" id="${fieldId}" onchange="${onChangeHandler}">
                            <option value="">-- Select a file --</option>
                            ${projectFiles.map(file => {
                                const selected = file.id == currentValue ? 'selected' : '';
                                return `<option value="${file.id}" ${selected}>${escapeHtml(file.filename)}</option>`;
                            }).join('')}
                        </select>
                    `;
                    break;
                case 'textarea':
                    inputHtml = `
                        <textarea class="form-input" id="${fieldId}" rows="3"
                                  onchange="${onChangeHandler}"
                                  placeholder="${escapeHtml(p.placeholder || '')}"
                                  style="resize: vertical; min-height: 60px;">${escapeHtml(currentValue || '')}</textarea>
                    `;
                    break;
                case 'color':
                    inputHtml = `
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <input type="color" id="${fieldId}-color" value="${currentValue || '#ffffff'}"
                                   onchange="document.getElementById('${fieldId}').value = this.value; ${onChangeHandler}"
                                   style="width: 40px; height: 36px; border: none; border-radius: var(--r-ui, 0px); cursor: pointer; padding: 2px;">
                            <input type="text" class="form-input" id="${fieldId}"
                                   value="${escapeHtml(currentValue || '')}"
                                   onchange="${onChangeHandler}"
                                   placeholder="#000000" style="flex: 1;">
                        </div>
                    `;
                    break;
                case 'range':
                    inputHtml = `
                        <div style="display: flex; align-items: center; gap: 12px;">
                            <input type="range" id="${fieldId}" value="${currentValue || p.default || 0}"
                                   onchange="${onChangeHandler}"
                                   ${p.min !== undefined ? `min="${p.min}"` : ''}
                                   ${p.max !== undefined ? `max="${p.max}"` : ''}
                                   ${p.step !== undefined ? `step="${p.step}"` : ''}
                                   style="flex: 1; accent-color: var(--color-primary, #8b5cf6);">
                            <span class="range-value" id="${fieldId}-value" style="min-width: 40px; text-align: right; color: #a1a1aa; font-size: 12px;">${currentValue || p.default || 0}</span>
                        </div>
                    `;
                    break;
                // Add more input types (textarea, range, etc.) as needed
                default:
                    inputHtml = `
                        <input type="text" class="form-input" id="${fieldId}"
                               value="${escapeHtml(currentValue || '')}"
                               onchange="${onChangeHandler}"
                               placeholder="${escapeHtml(p.placeholder || '')}">
                    `;
            }

            return `
                <div class="form-group" style="padding-bottom: 10px;">
                    <label class="form-label">${escapeHtml(p.label || p.id)}</label>
                    ${inputHtml}
                    ${p.help ? `<div class="form-hint">${escapeHtml(p.help)}</div>` : ''}
                </div>
            `;
        }).join('');
    }

    handleUpdate(data) {

        if (data.event === 'node_started') {

            const node = this.nodes.find(n => n.id === data.node_id);

            if (node) {

                node.status = 'running';

                this.draw();

            }

        } else if (data.event === 'node_completed') {

            const node = this.nodes.find(n => n.id === data.node_id);

            if (node) {

                node.status = 'completed';

                this.draw();

            }

        } else if (data.event === 'node_failed') {

            const node = this.nodes.find(n => n.id === data.node_id);

            if (node) {

                node.status = 'failed';

                this.draw();

            }

        }

    }

    // --- Loop ---

    loop() {
        this.draw();
        requestAnimationFrame(() => this.loop());
    }

    draw() {
        if (!this.ctx) return;
        const ctx = this.ctx;
        
        // Clear
        ctx.fillStyle = '#09090b';
        ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

        // Grid (handled by CSS background, but we could draw custom grid here)

        ctx.save();
        ctx.translate(this.offset.x, this.offset.y);
        ctx.scale(this.zoom, this.zoom);

        // Connections
        this.connections.forEach(conn => {
            const n1 = this.nodes.find(n => n.id === conn.from);
            const n2 = this.nodes.find(n => n.id === conn.to);
            if (n1 && n2) this.renderer.drawConnection(ctx, n1, n2);
        });

        // Dragging Wire
        if (this.connectingFrom) {
            this.renderer.drawDragWire(ctx, this.connectingFrom.node, this.worldMouse);
        }

        // Nodes
        this.nodes.forEach(node => {
            this.renderer.drawNode(ctx, node, node === this.draggingNode);
        });

        ctx.restore();
    }
}

// =============================================================================
// NODE RENDERER (Visuals)
// =============================================================================

class NodeRenderer {
    constructor(allNodeConfigSchemas) {
        this.cardRadius = 8;
        this.headerHeight = 32;
        this.allNodeConfigSchemas = allNodeConfigSchemas;
    }

    drawNode(ctx, node, isSelected) {
        const schema = this.allNodeConfigSchemas[node.type];
        const w = node.w;
        const h = node.h;
        const x = node.x;
        const y = node.y;

        // Shadow
        ctx.shadowColor = isSelected ? schema.color : 'rgba(0,0,0,0.5)';
        ctx.shadowBlur = isSelected ? 20 : 10;
        ctx.shadowOffsetX = 0;
        ctx.shadowOffsetY = 4;

        // Body Background
        ctx.fillStyle = '#18181b';
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, this.cardRadius);
        ctx.fill();
        ctx.shadowBlur = 0; // Reset shadow

        // Header Background
        ctx.fillStyle = schema.color + '22'; // 20% opacity
        ctx.beginPath();
        ctx.roundRect(x, y, w, this.headerHeight, [this.cardRadius, this.cardRadius, 0, 0]);
        ctx.fill();

        // Header Border / Outline
        ctx.strokeStyle = isSelected ? schema.color : '#27272a';
        ctx.lineWidth = isSelected ? 2 : 1;
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, this.cardRadius);
        ctx.stroke();

        // Icon & Title
        ctx.fillStyle = schema.color;
        ctx.font = '16px Arial';
        ctx.fillText(schema.icon, x + 10, y + 22);
        
        ctx.fillStyle = '#ffffff';
        ctx.font = '600 12px Inter, sans-serif';
        ctx.fillText(schema.title, x + 35, y + 21);

        // Status Indicator (example)
        if (node.status) {
            ctx.fillStyle = node.status === 'running' ? '#3b82f6' : '#10b981';
            ctx.beginPath();
            ctx.arc(x + w - 15, y + 16, 4, 0, Math.PI * 2);
            ctx.fill();
        }

        // Ports
        this.drawPorts(ctx, node, schema);
    }

    drawPorts(ctx, node, schema) {
        // Input Ports (Left)
        if (schema.inputs) {
            schema.inputs.forEach((label, i) => {
                const py = node.y + this.headerHeight + 20 + (i * 24);
                
                // Dot
                ctx.fillStyle = '#71717a';
                ctx.beginPath();
                ctx.arc(node.x, py, 5, 0, Math.PI * 2);
                ctx.fill();

                // Label
                ctx.fillStyle = '#a1a1aa';
                ctx.font = '10px Inter';
                ctx.textAlign = 'left';
                ctx.fillText(label, node.x + 10, py + 3);
            });
        }

        // Output Ports (Right)
        if (schema.outputs) {
            schema.outputs.forEach((label, i) => {
                const py = node.y + this.headerHeight + 20 + (i * 24);
                
                // Dot
                ctx.fillStyle = '#71717a';
                ctx.beginPath();
                ctx.arc(node.x + node.w, py, 5, 0, Math.PI * 2);
                ctx.fill();

                // Label
                ctx.fillStyle = '#a1a1aa';
                ctx.font = '10px Inter';
                ctx.textAlign = 'right';
                ctx.fillText(label, node.x + node.w - 10, py + 3);
            });
        }
    }

    drawConnection(ctx, n1, n2) {
        const startX = n1.x + n1.w;
        const startY = n1.y + this.headerHeight + 20; // Simplified port calculation
        const endX = n2.x;
        const endY = n2.y + this.headerHeight + 20;

        ctx.strokeStyle = '#52525b';
        ctx.lineWidth = 2;
        ctx.beginPath();
        
        // Cubic Bézier
        const cp1x = startX + (endX - startX) * 0.5;
        const cp2x = endX - (endX - startX) * 0.5;
        ctx.moveTo(startX, startY);
        ctx.bezierCurveTo(cp1x, startY, cp2x, endY, endX, endY);
        ctx.stroke();
    }

    drawDragWire(ctx, node, mousePos) {
        const startX = node.x + node.w;
        const startY = node.y + this.headerHeight + 20;
        
        ctx.strokeStyle = '#3b82f6';
        ctx.lineWidth = 2;
        ctx.setLineDash([5, 5]);
        ctx.beginPath();
        ctx.moveTo(startX, startY);
        ctx.lineTo(mousePos.x, mousePos.y);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    hitTestNode(nodes, pos) {
        // Reverse order to hit top nodes first
        for (let i = nodes.length - 1; i >= 0; i--) {
            const n = nodes[i];
            if (pos.x >= n.x && pos.x <= n.x + n.w && pos.y >= n.y && pos.y <= n.y + n.h) {
                return n;
            }
        }
        return null;
    }

    hitTestPort(node, pos) {
        // Simplified port hit testing (checks proximity to left/right edges)
        if (pos.x < node.x + 10) return { type: 'input' };
        if (pos.x > node.x + node.w - 10) return { type: 'output' };
        return null;
    }
}

// =============================================================================
// GLOBAL BINDINGS
// =============================================================================

window.FlowsModule = {
    manager: null,

    // Public API called by App.js
    render: async function() {
        if (!this.manager) {
            // App instance is passed via window.Views or similar if needed, 
            // but we can assume window.App or window.Views exists.
            this.manager = new FlowsManager(window.Views); 
        }
        await this.manager.render();
    },

    // Bridge methods for HTML callbacks
    showCreateModal: () => {
        const name = prompt("Workflow Name:");
        if (name) window.FlowsModule.manager.createFlow(name);
    },
    
    openFlow: (id) => window.FlowsModule.manager.openFlow(id),
    
    goBack: () => window.FlowsModule.manager.showDashboard(),
    
    saveFlow: async () => {
        const manager = window.FlowsModule.manager;
        if (!manager || !manager.currentFlow) {
            Toast.error('No workflow to save');
            return;
        }

        try {
            // Sync editor state to flow data
            const flowData = {
                nodes: manager.editor.nodes,
                connections: manager.editor.connections
            };

            const response = await API.put(`/api/flows/${manager.currentFlow.flow_id}`, {
                name: manager.currentFlow.name,
                description: manager.currentFlow.description,
                flow_data: flowData
            });

            Toast.success('Workflow saved successfully');
            console.log('Flow saved:', response);
        } catch (error) {
            console.error('Error saving flow:', error);
            Toast.error('Failed to save workflow');
        }
    },

    executeFlow: async () => {
        const manager = window.FlowsModule.manager;
        if (!manager || !manager.currentFlow) {
            Toast.error('No workflow to execute');
            return;
        }

        try {
            Toast.info('Starting workflow execution...');
            
            // Prepare execution payload
            const flowData = {
                nodes: manager.editor.nodes,
                connections: manager.editor.connections
            };

            const response = await API.post(`/api/flows/${manager.currentFlow.flow_id}/execute`, {
                flow_data: flowData
            });

            if (response.execution_id) {
                Toast.success('Workflow execution started');
                manager.currentFlow.execution_id = response.execution_id;
                
                // Update editor to show execution status
                manager.editor.nodes.forEach(node => {
                    node.status = 'pending';
                });
                manager.editor.draw();
            } else {
                throw new Error('No execution ID returned');
            }
        } catch (error) {
            console.error('Error executing flow:', error);
            Toast.error('Failed to start workflow execution');
        }
    },

    handleDragStart: (e, type) => {
        e.dataTransfer.setData('nodeType', type);
    },

    updateName: (val) => {
        // Update local state
    },

    updateNodeParam: (nodeId, paramId, value) => {
        const node = window.FlowsModule.manager.editor.nodes.find(n => n.id === nodeId);
        if (node) {
            node.config[paramId] = value;
        }
    },

    closeInspector: () => {
        document.getElementById('flows-inspector').style.display = 'none';
    },

    zoomIn: () => {
        const editor = window.FlowsModule.manager.editor;
        editor.zoom = Math.min(3, editor.zoom + 0.1);
    },

    zoomOut: () => {
        const editor = window.FlowsModule.manager.editor;
        editor.zoom = Math.max(0.2, editor.zoom - 0.1);
    },

    fitView: () => {
        const editor = window.FlowsModule.manager.editor;
        editor.zoom = 1;
        editor.offset = { x: 0, y: 0 };
    },

    filterNodes: (searchTerm) => {
        const editor = window.FlowsModule.manager.editor;
        if (!editor) return;
        
        const libraryNodes = document.querySelectorAll('.library-node');
        const term = searchTerm.toLowerCase().trim();
        
        libraryNodes.forEach(node => {
            const text = node.textContent.toLowerCase();
            if (text.includes(term)) {
                node.style.display = 'flex';
            } else {
                node.style.display = 'none';
            }
        });
    }
};

// Export class for manual instantiation if needed
window.FlowsManager = FlowsManager;
