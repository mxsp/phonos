/**
 * Custom Models App - Enhanced Model Management with E2E Encryption
 * @version 3.2.0
 */

// i18n.t() returns the bare key when it is missing from the locale, which showed up
// as "customModels.noModelsYet" on screen. Use the English text in that case.
function cmT(key, fallback) {
    try {
        if (window.i18n && typeof window.i18n.t === 'function') {
            var v = window.i18n.t(key);
            if (v && v !== key) return v;
        }
    } catch (e) { /* fall through */ }
    return fallback == null ? '' : String(fallback);
}

const CustomModelsApp = {
    // State
    models: [],
    selectedFiles: [],
    currentModel: null,
    encryptionKey: null,
    
    // Initialize the application
    async init() {
        console.log('[CustomModelsApp] Initializing...');
        
        // Setup mobile menu
        this.setupMobileMenu();
        
        // Load encryption key from secure storage
        await this.loadEncryptionKey();
        
        // Load models
        await this.loadModels();
        
        // Setup event listeners
        this.setupEventListeners();
        
        console.log('[CustomModelsApp] Initialized successfully');
    },
    
    // Setup mobile menu
    setupMobileMenu() {
        const mobileMenuBtn = document.getElementById('mobile-menu-btn');
        const sidebar = document.getElementById('sidebar');
        const sidebarOverlay = document.getElementById('sidebar-overlay');
        
        if (mobileMenuBtn && sidebar && sidebarOverlay) {
            mobileMenuBtn.addEventListener('click', () => {
                sidebar.classList.toggle('open');
                sidebarOverlay.classList.toggle('active');
            });
            
            sidebarOverlay.addEventListener('click', () => {
                sidebar.classList.remove('open');
                sidebarOverlay.classList.remove('active');
            });
        }
    },
    
    // Load encryption key from secure storage
    async loadEncryptionKey() {
        try {
            // Try to get key from IndexedDB or generate new one
            const storedKey = await this.getStoredKey();
            if (storedKey) {
                this.encryptionKey = storedKey;
                console.log('[CustomModelsApp] Encryption key loaded');
            } else {
                // Generate new encryption key
                this.encryptionKey = await this.generateEncryptionKey();
                await this.storeKey(this.encryptionKey);
                console.log('[CustomModelsApp] New encryption key generated');
            }
        } catch (error) {
            console.error('[CustomModelsApp] Failed to load encryption key:', error);
            this.showToast(cmT('customModels.warningEncryptionKey', 'Warning: Encryption key not available'), 'error');
        }
    },
    
    // Generate a new encryption key using Web Crypto API
    async generateEncryptionKey() {
        return await window.crypto.subtle.generateKey(
            {
                name: 'AES-GCM',
                length: 256
            },
            true, // extractable
            ['encrypt', 'decrypt']
        );
    },
    
    // Store encryption key in IndexedDB
    async storeKey(key) {
        try {
            const db = await this.openKeyDB();
            const transaction = db.transaction(['keys'], 'readwrite');
            const store = transaction.objectStore('keys');
            await store.put({ id: 'modelEncryptionKey', key: key, created: Date.now() });
            db.close();
        } catch (error) {
            console.error('[CustomModelsApp] Failed to store key:', error);
        }
    },
    
    // Get stored encryption key from IndexedDB
    async getStoredKey() {
        try {
            const db = await this.openKeyDB();
            const transaction = db.transaction(['keys'], 'readonly');
            const store = transaction.objectStore('keys');
            const result = await store.get('modelEncryptionKey');
            db.close();
            return result ? result.key : null;
        } catch (error) {
            console.error('[CustomModelsApp] Failed to get stored key:', error);
            return null;
        }
    },
    
    // Open IndexedDB for key storage
    openKeyDB() {
        return new Promise((resolve, reject) => {
            const request = indexedDB.open('PhonosCustomModelsDB', 1);
            
            request.onerror = () => reject(request.error);
            request.onsuccess = () => resolve(request.result);
            
            request.onupgradeneeded = (event) => {
                const db = event.target.result;
                if (!db.objectStoreNames.contains('keys')) {
                    db.createObjectStore('keys', { keyPath: 'id' });
                }
            };
        });
    },
    
    // Encrypt data using AES-GCM
    async encryptData(data) {
        if (!this.encryptionKey) {
            throw new Error('Encryption key not available');
        }
        
        // Generate random IV
        const iv = window.crypto.getRandomValues(new Uint8Array(12));
        
        // Convert data to ArrayBuffer
        const encoder = new TextEncoder();
        const dataBuffer = encoder.encode(JSON.stringify(data));
        
        // Encrypt
        const encryptedBuffer = await window.crypto.subtle.encrypt(
            {
                name: 'AES-GCM',
                iv: iv
            },
            this.encryptionKey,
            dataBuffer
        );
        
        // Combine IV and encrypted data
        const result = new Uint8Array(iv.length + encryptedBuffer.byteLength);
        result.set(iv);
        result.set(new Uint8Array(encryptedBuffer), iv.length);
        
        // Convert to base64 for storage
        return btoa(String.fromCharCode.apply(null, result));
    },
    
    // Decrypt data using AES-GCM
    async decryptData(encryptedBase64) {
        if (!this.encryptionKey) {
            throw new Error('Encryption key not available');
        }
        
        // Convert from base64
        const encryptedData = Uint8Array.from(atob(encryptedBase64), c => c.charCodeAt(0));
        
        // Extract IV and ciphertext
        const iv = encryptedData.slice(0, 12);
        const ciphertext = encryptedData.slice(12);
        
        // Decrypt
        const decryptedBuffer = await window.crypto.subtle.decrypt(
            {
                name: 'AES-GCM',
                iv: iv
            },
            this.encryptionKey,
            ciphertext
        );
        
        // Convert to string and parse JSON
        const decoder = new TextDecoder();
        return JSON.parse(decoder.decode(decryptedBuffer));
    },
    
    // Load models from server
    async loadModels() {
        try {
            const response = await fetch('/api/user-models', { credentials: 'same-origin' });
            if (!response.ok) throw new Error('Failed to load models');
            
            const payload = await response.json();
            this.models = Array.isArray(payload) ? payload : (payload.models || []);
            this.renderModels();
            this.updateStats();
            
        } catch (error) {
            console.error('[CustomModelsApp] Failed to load models:', error);
            this.renderEmptyState();
        }
    },
    
    // Render models list
    renderModels() {
        const container = document.getElementById('models-list');
        
        if (!this.models || this.models.length === 0) {
            this.renderEmptyState();
            return;
        }

        const cards = this.models.map(model => {
            const modelId = this.escapeJsString(model.id);
            const modelName = this.escapeHtml(this.getModelName(model));
            const modelType = this.escapeHtml(this.getModelType(model));
            const modelDescription = this.escapeHtml(this.getModelDescription(model));
            const createdAt = this.formatDate(model.created_at || model.createdAt || new Date().toISOString());
            const runs = this.getModelRuns(model);
            const sizeLabel = this.formatSize(this.getModelSizeBytes(model));
            const isActive = this.isModelActive(model);
            const isEncrypted = this.isEncryptedModel(model);
            const creditCost = this.getModelCreditCost(model);
            const modelVisual = this.getModelVisual(model);
            const modelTitle = this.escapeHtml(this.getModelName(model));

            return `
                <div class="model-selection-card custom-model-card ${isActive ? 'is-active' : 'is-inactive'}"
                     data-model-id="${this.escapeHtml(String(model.id))}"
                     onclick="CustomModelsApp.showModelDetails('${modelId}')"
                     tabindex="0"
                     role="button"
                     aria-label="${window.i18n ? cmT('customModels.openDetailsFor', 'Open details for {name}').replace('{name}', modelTitle) : `Open details for ${modelTitle}`}"
                     onkeydown="if(event.key === 'Enter' || event.key === ' '){ event.preventDefault(); CustomModelsApp.showModelDetails('${modelId}'); }">
                    <div class="model-card-glow" style="background:radial-gradient(circle, ${modelVisual.background}22, transparent 60%);"></div>
                    <div class="model-card-icon custom-model-icon"
                         style="background: ${modelVisual.background};">
                        ${modelVisual.icon}
                    </div>

                    <div class="model-card-content custom-model-content">
                        <h4 class="model-card-title">${modelName}</h4>
                        <p class="model-card-description">${modelDescription}</p>
                        <div class="model-card-meta custom-model-meta">
                            <span class="model-card-cost">${creditCost} ${cmT('common.creditsPerJob', 'credits per job')}</span>
                            <span class="custom-model-pill ${isActive ? 'pill-active' : 'pill-inactive'}">
                                ${isActive ? (cmT('common.active', 'Active')) : (cmT('common.inactive', 'Inactive'))}
                            </span>
                            ${isEncrypted ? `<span class="custom-model-pill pill-encrypted">${cmT('common.encrypted', 'Encrypted')}</span>` : ''}
                        </div>
                        <div class="custom-model-submeta">
                            <span class="custom-model-submeta-item">${modelType}</span>
                            <span class="custom-model-submeta-item">${createdAt}</span>
                            <span class="custom-model-submeta-item">${runs} ${cmT('common.runs', 'runs')}</span>
                            <span class="custom-model-submeta-item">${sizeLabel}</span>
                        </div>
                    </div>

                    <div class="custom-model-actions" onclick="event.stopPropagation()">
                        <button class="custom-model-action-btn"
                                onclick="CustomModelsApp.downloadModel('${modelId}')"
                                title="${cmT('common.downloadModel', 'Download model')}">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                                <polyline points="7 10 12 15 17 10"/>
                                <line x1="12" y1="15" x2="12" y2="3"/>
                            </svg>
                        </button>
                        <button class="custom-model-action-btn danger"
                                onclick="CustomModelsApp.deleteModel('${modelId}')"
                                title="${cmT('common.deleteModel', 'Delete model')}">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"/>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                            </svg>
                        </button>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = `<div class="model-selection-grid custom-models-grid">${cards}</div>`;
    },
    
    // Render empty state
    renderEmptyState() {
        const container = document.getElementById('models-list');
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="width: 32px; height: 32px; color: #9ca3af;">
                        <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                        <path d="M2 17l10 5 10-5"/>
                        <path d="M2 12l10 5 10-5"/>
                    </svg>
                </div>
                <h3>${cmT('customModels.noModelsYet', 'No custom models yet')}</h3>
                <p>${cmT('customModels.uploadFirstModel', 'Upload your first AI model to get started with secure, encrypted model management.')}</p>
                <button class="btn btn-primary" onclick="CustomModelsApp.showUploadModal()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                        <polyline points="17 8 12 3 7 8"/>
                        <line x1="12" y1="3" x2="12" y2="15"/>
                    </svg>
                    ${cmT('common.uploadModel', 'Upload Model')}
                </button>
            </div>
        `;
    },
    
    // Update statistics
    updateStats() {
        const totalModels = this.models.length;
        const activeModels = this.models.filter(m => this.isModelActive(m)).length;
        const totalRuns = this.models.reduce((sum, m) => sum + this.getModelRuns(m), 0);
        const totalSize = this.models.reduce((sum, m) => sum + this.getModelSizeBytes(m), 0);
        
        document.getElementById('total-models').textContent = totalModels;
        document.getElementById('active-models').textContent = activeModels;
        document.getElementById('total-runs').textContent = totalRuns.toLocaleString();
        document.getElementById('storage-used').textContent = this.formatSize(totalSize);
        document.getElementById('model-count').textContent = totalModels;
    },
    
    // Filter models
    filterModels() {
        const searchTerm = document.getElementById('model-search').value.toLowerCase();
        const filterValue = document.getElementById('model-filter').value;
        
        const filtered = this.models.filter(model => {
            const modelName = this.getModelName(model).toLowerCase();
            const matchesSearch = modelName.includes(searchTerm);
            const modelIsActive = this.isModelActive(model);
            const matchesFilter = filterValue === 'all' || 
                                 (filterValue === 'active' && modelIsActive) ||
                                 (filterValue === 'inactive' && !modelIsActive) ||
                                 (filterValue === 'encrypted' && this.isEncryptedModel(model));
            return matchesSearch && matchesFilter;
        });
        
        // Re-render with filtered models
        this.renderFilteredModels(filtered);
    },
    
    // Render filtered models
    renderFilteredModels(models) {
        // Temporarily replace models array
        const originalModels = this.models;
        this.models = models;
        this.renderModels();
        this.models = originalModels;
    },
    
    // Show upload modal
    showUploadModal() {
        document.getElementById('upload-modal').style.display = 'flex';
        this.selectedFiles = [];
        this.updateFileList();
    },
    
    // Close upload modal
    closeUploadModal() {
        document.getElementById('upload-modal').style.display = 'none';
        this.selectedFiles = [];
        this.resetUploadForm();
    },
    
    // Reset upload form
    resetUploadForm() {
        document.getElementById('model-name').value = '';
        document.getElementById('model-description').value = '';
        document.getElementById('model-type').value = '';
        document.getElementById('framework-version').value = '';
        document.getElementById('model-schema').value = '';
        document.getElementById('enable-encryption').checked = true;
        this.selectedFiles = [];
        this.updateFileList();
    },
    
    // Handle drag over
    handleDragOver(event) {
        event.preventDefault();
        event.currentTarget.classList.add('drag-over');
    },
    
    // Handle drag leave
    handleDragLeave(event) {
        event.currentTarget.classList.remove('drag-over');
    },
    
    // Handle drop
    handleDrop(event) {
        event.preventDefault();
        event.currentTarget.classList.remove('drag-over');
        
        const files = Array.from(event.dataTransfer.files);
        this.addFiles(files);
    },
    
    // Handle file select
    handleFileSelect(event) {
        const files = Array.from(event.target.files);
        this.addFiles(files);
    },
    
    // Add files to selection
    addFiles(files) {
        const allowedTypes = ['.pt', '.pth', '.h5', '.hdf5', '.onnx', '.pkl', '.joblib', '.zip', '.tar', '.gz', '.bin', '.safetensors', '.gguf', '.ggml', '.keras', '.pb', '.ckpt', '.model', '.json', '.yaml', '.yml', '.txt'];
        
        files.forEach(file => {
            const ext = '.' + file.name.split('.').pop().toLowerCase();
            if (allowedTypes.includes(ext)) {
                this.selectedFiles.push(file);
            } else {
                this.showToast(window.i18n ? cmT('customModels.fileTypeNotSupported', 'File type {ext} not supported').replace('{ext}', ext) : `File type ${ext} not supported`, 'error');
            }
        });
        
        this.updateFileList();
    },
    
    // Update file list display
    updateFileList() {
        const container = document.getElementById('file-list');
        
        if (this.selectedFiles.length === 0) {
            container.innerHTML = '';
            return;
        }
        
        container.innerHTML = this.selectedFiles.map((file, index) => `
            <div class="file-item-cm">
                <div class="file-info">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width: 16px; height: 16px; color: #3b82f6; flex-shrink: 0;">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                    </svg>
                    <span class="file-name">${this.escapeHtml(file.name)}</span>
                    <span class="file-size">${this.formatSize(file.size)}</span>
                </div>
                <button class="file-remove" onclick="CustomModelsApp.removeFile(${index})" title="Remove file">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;">
                        <line x1="18" y1="6" x2="6" y2="18"/>
                        <line x1="6" y1="6" x2="18" y2="18"/>
                    </svg>
                </button>
            </div>
        `).join('');
    },
    
    // Remove file from selection
    removeFile(index) {
        this.selectedFiles.splice(index, 1);
        this.updateFileList();
    },
    
    // Upload model
    async uploadModel() {
        const name = document.getElementById('model-name').value.trim();
        const type = document.getElementById('model-type').value;
        
        if (!name) {
            this.showToast(cmT('customModels.enterModelName', 'Please enter a model name'), 'error');
            return;
        }
        
        if (!type) {
            this.showToast(cmT('customModels.selectModelType', 'Please select a model type'), 'error');
            return;
        }
        
        if (this.selectedFiles.length === 0) {
            this.showToast(cmT('customModels.selectAtLeastOneFile', 'Please select at least one file'), 'error');
            return;
        }
        
        const btn = document.getElementById('upload-submit-btn');
        const btnText = btn.querySelector('.btn-text');
        const btnSpinner = btn.querySelector('.btn-spinner');
        const originalText = btnText.textContent;
        
        btn.disabled = true;
        btnText.textContent = cmT('common.preparing', 'Preparing...');
        btnSpinner.style.display = 'block';
        
        // Animated upload progress states
        setTimeout(() => {
            if (btn.disabled) {
                btnText.textContent = cmT('common.encrypting', 'Encrypting...');
            }
        }, 300);
        setTimeout(() => {
            if (btn.disabled) {
                btnText.textContent = cmT('common.uploading', 'Uploading...');
            }
        }, 1200);
        
        try {
            const enableEncryption = document.getElementById('enable-encryption').checked;
            
            // Prepare metadata
            const metadata = {
                name: name,
                description: document.getElementById('model-description').value,
                type: type,
                framework_version: document.getElementById('framework-version').value,
                schema: document.getElementById('model-schema').value,
                encrypted: enableEncryption
            };
            
            // The backend (/api/user-models) takes JSON: a worker-backed model definition.
            // Weight files are not uploaded through this route.
            let inputSchema = [];
            try {
                const parsed = JSON.parse(metadata.schema || '[]');
                if (Array.isArray(parsed)) inputSchema = parsed;
            } catch (e) { /* free-text schema: keep empty list */ }

            const response = await fetch('/api/user-models', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': window.PHONOS_CONFIG?.csrfToken || ''
                },
                body: JSON.stringify({
                    model_name: metadata.name,
                    model_description: metadata.description || metadata.name,
                    model_type: metadata.type || 'custom',
                    input_schema: inputSchema
                })
            });
            
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.message || (cmT('common.uploadFailed', 'Upload failed')));
            }
            
            const result = await response.json();
            
            this.showToast(cmT('customModels.modelUploaded', 'Model uploaded successfully!'), 'success');
            this.closeUploadModal();
            await this.loadModels();
            
        } catch (error) {
            console.error('[CustomModelsApp] Upload failed:', error);
            this.showToast(error.message || (cmT('common.uploadFailed', 'Upload failed')), 'error');
        } finally {
            btn.disabled = false;
            btnText.textContent = originalText || (cmT('customModels.uploadAndEncrypt', 'Upload & Encrypt'));
            btnSpinner.style.display = 'none';
        }
    },
    
    // Show model details
    showModelDetails(modelId) {
        const model = this.findModelById(modelId);
        if (!model) return;
        
        this.currentModel = model;
        
        const modelIsActive = this.isModelActive(model);
        document.getElementById('detail-model-name').textContent = this.getModelName(model);
        document.getElementById('detail-status').textContent = modelIsActive ? (cmT('common.active', 'Active')) : (cmT('common.inactive', 'Inactive'));
        document.getElementById('detail-status').className = 'detail-value ' + (modelIsActive ? 'text-success' : 'text-muted');
        document.getElementById('detail-type').textContent = this.getModelType(model);
        document.getElementById('detail-created').textContent = this.formatDateTime(model.created_at || model.createdAt);
        const lastUsedAt = model.last_used || model.lastUsedAt;
        document.getElementById('detail-last-used').textContent = lastUsedAt ? this.formatDateTime(lastUsedAt) : (cmT('common.never', 'Never'));
        
        const encryptionStatus = document.getElementById('detail-encryption');
        if (this.isEncryptedModel(model)) {
            encryptionStatus.style.display = 'flex';
        } else {
            encryptionStatus.style.display = 'none';
        }
        
        document.getElementById('model-details-modal').style.display = 'flex';
    },
    
    // Close model details modal
    closeModelDetailsModal() {
        document.getElementById('model-details-modal').style.display = 'none';
        this.currentModel = null;
    },
    
    // Download model
    async downloadModel(modelId) {
        const model = modelId ? this.findModelById(modelId) : this.currentModel;
        if (!model) return;
        
        try {
            this.showToast(cmT('customModels.preparingDownload', 'Preparing download...'), 'info');
            
            // No download route exists for /api/user-models (worker-backed models have no archive).
            const response = await fetch(`/api/user-models/${model.id}/download`);
            if (!response.ok) throw new Error('Download failed');
            
            const blob = await response.blob();
            
            // If encrypted, decrypt it
            if (this.isEncryptedModel(model) && this.encryptionKey) {
                // Implementation would decrypt the blob here
                // For now, we'll just download as-is
            }
            
            // Trigger download
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${this.getModelName(model)}.zip`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
            
            this.showToast(cmT('customModels.downloadStarted', 'Download started'), 'success');
            
        } catch (error) {
            console.error('[CustomModelsApp] Download failed:', error);
            this.showToast(cmT('common.downloadFailed', 'Download failed'), 'error');
        }
    },
    
    // Delete model
    async deleteModel(modelId) {
        const model = modelId ? this.findModelById(modelId) : this.currentModel;
        if (!model) return;
        
        if (!confirm(window.i18n ? cmT('customModels.confirmDelete', 'Are you sure you want to delete "{name}"? This action cannot be undone.').replace('{name}', this.getModelName(model)) : `Are you sure you want to delete "${this.getModelName(model)}"? This action cannot be undone.`)) {
            return;
        }
        
        try {
            const response = await fetch(`/api/user-models/${model.id}`, {
                method: 'DELETE',
                credentials: 'same-origin',
                headers: { 'X-CSRF-Token': window.PHONOS_CONFIG?.csrfToken || '' }
            });
            
            if (!response.ok) throw new Error('Delete failed');
            
            this.showToast(cmT('customModels.modelDeleted', 'Model deleted successfully'), 'success');
            this.closeModelDetailsModal();
            await this.loadModels();
            
        } catch (error) {
            console.error('[CustomModelsApp] Delete failed:', error);
            this.showToast(cmT('common.deleteFailed', 'Delete failed'), 'error');
        }
    },
    
    // Test model
    async testModel() {
        if (!this.currentModel) return;
        
        this.showToast(cmT('customModels.openingTestInterface', 'Opening model test interface...'), 'info');
        // Would open a test interface
        window.open(`/test-model/${this.currentModel.id}`, '_blank');
    },
    
    // Show encryption keys
    showEncryptionKeys() {
        this.showToast(cmT('customModels.encryptionKeysComingSoon', 'Encryption keys management coming soon'), 'info');
    },
    
    // Show access logs
    showAccessLogs() {
        this.showToast(cmT('customModels.accessLogsComingSoon', 'Access logs coming soon'), 'info');
    },
    
    // Show model store
    showModelStore() {
        this.showToast(cmT('customModels.modelStoreComingSoon', 'Model store coming soon'), 'info');
    },
    
    // View all activity
    viewAllActivity() {
        this.showToast(cmT('customModels.fullActivityLogComingSoon', 'Full activity log coming soon'), 'info');
    },
    
    // Show toast notification
    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width: 20px; height: 20px; flex-shrink: 0;">
                ${type === 'success' ? '<path d="M20 6L9 17l-5-5"/>' : 
                  type === 'error' ? '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>' : 
                  '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>'}
            </svg>
            <span>${this.escapeHtml(message)}</span>
        `;
        
        container.appendChild(toast);
        
        setTimeout(() => {
            toast.style.animation = 'slideIn 0.3s ease reverse';
            setTimeout(() => toast.remove(), 300);
        }, 5000);
    },
    
    // Setup event listeners
    setupEventListeners() {
        // Close modals on escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.closeUploadModal();
                this.closeModelDetailsModal();
            }
        });
        
        // Close modals on overlay click
        document.querySelectorAll('.modal-overlay').forEach(overlay => {
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    overlay.style.display = 'none';
                }
            });
        });
    },

    getModelName(model) {
        return String(model?.name || model?.model_name || model?.title || (window.i18n ? cmT('customModels.defaultModelName', 'Model {id}').replace('{id}', model?.id || '') : `Model ${model?.id || ''}`)).trim();
    },

    getModelDescription(model) {
        const description = model?.description || model?.model_description;
        return String(description || (cmT('customModels.defaultDescription', 'Custom model ready for worker execution.'))).trim();
    },

    getModelType(model) {
        return String(model?.type || model?.model_type || 'custom').trim();
    },

    getModelRuns(model) {
        const raw = Number(model?.runs ?? model?.total_jobs_run ?? 0);
        return Number.isFinite(raw) && raw > 0 ? raw : 0;
    },

    getModelSizeBytes(model) {
        const raw = Number(model?.size ?? model?.storage_bytes ?? model?.size_bytes ?? 0);
        return Number.isFinite(raw) && raw > 0 ? raw : 0;
    },

    getModelCreditCost(model) {
        const raw = Number(model?.credit_cost);
        return Number.isFinite(raw) && raw > 0 ? raw : 5;
    },

    getModelVisual(model) {
        const type = this.getModelType(model).toLowerCase();
        const visuals = {
            pytorch: {
                background: 'linear-gradient(135deg, rgba(249, 115, 22, 0.2), rgba(249, 115, 22, 0.08))',
                icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>'
            },
            tensorflow: {
                background: 'linear-gradient(135deg, rgba(59, 130, 246, 0.2), rgba(59, 130, 246, 0.08))',
                icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 7h10v10H7z"/></svg>'
            },
            onnx: {
                background: 'linear-gradient(135deg, rgba(16, 185, 129, 0.2), rgba(16, 185, 129, 0.08))',
                icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M8 12h8"/><path d="M12 8v8"/></svg>'
            },
            sklearn: {
                background: 'linear-gradient(135deg, rgba(168, 85, 247, 0.2), rgba(168, 85, 247, 0.08))',
                icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3h18v18H3z"/><path d="M7 15l3-3 2 2 5-5"/></svg>'
            }
        };

        return visuals[type] || {
            background: 'linear-gradient(135deg, rgba(236, 72, 153, 0.2), rgba(219, 39, 119, 0.08))',
            icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>'
        };
    },

    findModelById(modelId) {
        const lookup = String(modelId ?? '');
        return this.models.find(model => String(model?.id ?? '') === lookup);
    },

    isModelActive(model) {
        if (typeof model?.is_active === 'boolean') return model.is_active;
        if (typeof model?.status === 'string') return model.status.toLowerCase() === 'active';
        return true;
    },

    isEncryptedModel(model) {
        return Boolean(model?.encrypted || model?.encryption_enabled);
    },

    escapeJsString(value) {
        return String(value ?? '')
            .replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'");
    },
    
    // Utility: Escape HTML
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
    
    // Utility: Format date
    formatDate(dateString) {
        const date = new Date(dateString);
        return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    },
    
    // Utility: Format datetime
    formatDateTime(dateString) {
        const date = new Date(dateString);
        return date.toLocaleString('en-US', { 
            month: 'short', 
            day: 'numeric', 
            year: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        });
    },
    
    // Utility: Format size
    formatSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }
};

// Expose to global scope for onclick handlers
window.CustomModelsApp = CustomModelsApp;
window.App = CustomModelsApp;
