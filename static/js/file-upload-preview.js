/**
 * PHONOS FileUploadPreview Module
 *
 * Provides drag-and-drop file upload with inline preview, validation,
 * and auto-fill capabilities for job submission forms.
 *
 * Features:
 * - Drag-and-drop with visual feedback
 * - Multi-format parsers (FASTA, PDB, CIF, SMILES, TXT)
 * - Inline preview renderers (sequences, structures, text)
 * - Real-time validation with helpful error messages
 * - Auto-fill form fields from parsed data
 * - Moderate animations (250-400ms) aligned with Zinc design system
 */

const FileUploadPreview = {
    // Active instances tracker
    instances: new Map(),

    /**
     * Create a new FileUploadPreview instance
     * @param {HTMLElement} container - Container element for the dropzone
     * @param {Object} options - Configuration options
     * @returns {Object} Instance controller
     */
    create(container, options = {}) {
        const defaults = {
            accept: '*',
            maxSize: 50 * 1024 * 1024, // 50MB
            previewType: 'auto', // 'auto', 'sequence', 'structure', 'smiles', 'text'
            autoFill: true,
            onFileSelected: (file, parsedData) => {},
            onError: (error) => {},
            showPreview: true,
            allowedExtensions: null,
            validateSequenceType: null // 'protein', 'dna', 'rna'
        };

        const config = { ...defaults, ...options };
        const instanceId = `fup-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

        // Create dropzone UI
        const dropzone = this.createDropzone(container, config);
        const fileInput = dropzone.querySelector('input[type="file"]');
        const previewPanel = this.createPreviewPanel(container);

        // Attach event handlers
        this.attachDragHandlers(dropzone, config, previewPanel);
        this.attachFileInputHandler(fileInput, config, previewPanel, dropzone);

        // Store instance
        const instance = {
            id: instanceId,
            container,
            dropzone,
            fileInput,
            previewPanel,
            config,
            currentFile: null,
            currentData: null
        };

        this.instances.set(instanceId, instance);

        return {
            destroy: () => this.destroyInstance(instanceId),
            reset: () => this.resetInstance(instanceId),
            getData: () => instance.currentData,
            getFile: () => instance.currentFile
        };
    },

    /**
     * Create dropzone UI element using safe DOM methods
     */
    createDropzone(container, config) {
        const dropzone = document.createElement('div');
        dropzone.className = 'file-upload-dropzone';
        dropzone.setAttribute('role', 'button');
        dropzone.setAttribute('tabindex', '0');
        dropzone.setAttribute('aria-label', 'Upload file');

        // Icon container
        const iconDiv = document.createElement('div');
        iconDiv.className = 'dropzone-icon';
        const svg = this.createSVGElement('svg', { width: '48', height: '48', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
        svg.appendChild(this.createSVGElement('path', { d: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4' }));
        svg.appendChild(this.createSVGElement('polyline', { points: '17 8 12 3 7 8' }));
        svg.appendChild(this.createSVGElement('line', { x1: '12', y1: '3', x2: '12', y2: '15' }));
        iconDiv.appendChild(svg);

        // Text
        const textP = document.createElement('p');
        textP.className = 'dropzone-text';
        textP.textContent = 'Drop file here or click to browse';

        // Hint
        const hintP = document.createElement('p');
        hintP.className = 'dropzone-hint';
        hintP.textContent = `Supports ${this.formatAcceptedTypes(config.accept)} (max ${this.formatFileSize(config.maxSize)})`;

        // Hidden file input
        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.accept = config.accept;
        fileInput.multiple = true;
        fileInput.style.display = 'none';

        dropzone.appendChild(iconDiv);
        dropzone.appendChild(textP);
        dropzone.appendChild(hintP);
        dropzone.appendChild(fileInput);

        // Make it clickable
        dropzone.addEventListener('click', () => fileInput.click());

        // Keyboard accessibility
        dropzone.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                fileInput.click();
            }
        });

        container.appendChild(dropzone);
        return dropzone;
    },

    /**
     * Create preview panel element
     */
    createPreviewPanel(container) {
        const panel = document.createElement('div');
        panel.className = 'file-upload-preview-panel';
        panel.style.display = 'none';

        // Header
        const header = document.createElement('div');
        header.className = 'preview-header';

        const title = document.createElement('h4');
        title.className = 'preview-title';
        title.textContent = 'File Preview';

        const closeBtn = document.createElement('button');
        closeBtn.className = 'preview-close';
        closeBtn.title = 'Clear preview';
        closeBtn.setAttribute('aria-label', 'Clear preview');
        const closeSvg = this.createSVGElement('svg', { width: '16', height: '16', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' });
        closeSvg.appendChild(this.createSVGElement('line', { x1: '18', y1: '6', x2: '6', y2: '18' }));
        closeSvg.appendChild(this.createSVGElement('line', { x1: '6', y1: '6', x2: '18', y2: '18' }));
        closeBtn.appendChild(closeSvg);

        header.appendChild(title);
        header.appendChild(closeBtn);

        // Content
        const content = document.createElement('div');
        content.className = 'preview-content';

        panel.appendChild(header);
        panel.appendChild(content);

        // Close button handler
        closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.hidePreview(panel);
        });

        container.appendChild(panel);
        return panel;
    },

    /**
     * Helper to create SVG elements
     */
    createSVGElement(tag, attrs = {}) {
        const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
        Object.entries(attrs).forEach(([key, value]) => {
            el.setAttribute(key, value);
        });
        return el;
    },

    /**
     * Attach drag-and-drop event handlers
     */
    attachDragHandlers(dropzone, config, previewPanel) {
        let dragCounter = 0;

        dropzone.addEventListener('dragenter', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dragCounter++;
            dropzone.classList.add('dragging');
        });

        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.stopPropagation();
        });

        dropzone.addEventListener('dragleave', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dragCounter--;
            if (dragCounter === 0) {
                dropzone.classList.remove('dragging');
            }
        });

        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dragCounter = 0;
            dropzone.classList.remove('dragging');

            const files = Array.from(e.dataTransfer.files);
            for (const file of files) {
                this.handleFile(file, config, previewPanel, dropzone);
            }
        });
    },

    /**
     * Attach file input change handler
     */
    attachFileInputHandler(fileInput, config, previewPanel, dropzone) {
        fileInput.addEventListener('change', (e) => {
            const files = Array.from(e.target.files);
            for (const file of files) {
                this.handleFile(file, config, previewPanel, dropzone);
            }
        });
    },

    /**
     * Handle file selection
     */
    async handleFile(file, config, previewPanel, dropzone) {
        // Show loading state
        this.showLoading(dropzone);

        // Validate file
        const sizeValidation = this.validators.fileSize(file, config.maxSize);
        const typeValidation = this.validators.fileType(file, config.accept);

        if (!sizeValidation.valid) {
            this.hideLoading(dropzone);
            this.showError(previewPanel, sizeValidation.error);
            config.onError({ message: sizeValidation.error, file });
            return;
        }

        if (!typeValidation.valid) {
            this.hideLoading(dropzone);
            this.showError(previewPanel, typeValidation.error);
            config.onError({ message: typeValidation.error, file });
            return;
        }

        try {
            // Read file
            const content = await this.readFile(file);

            // Parse file based on type
            const fileType = this.detectFileType(file.name, config.previewType);
            let parsedData = null;

            switch (fileType) {
                case 'fasta':
                    parsedData = this.parsers.fasta(content);
                    break;
                case 'pdb':
                    parsedData = this.parsers.pdb(content);
                    break;
                case 'cif':
                    parsedData = this.parsers.cif(content);
                    break;
                case 'smiles':
                    parsedData = this.parsers.smiles(content);
                    break;
                case 'text':
                default:
                    parsedData = this.parsers.text(content);
                    break;
            }

            parsedData.fileName = file.name;
            parsedData.fileSize = file.size;
            parsedData.fileType = fileType;
            parsedData.rawContent = content;

            // Additional validation if sequence type specified
            if (config.validateSequenceType && parsedData.sequence) {
                const seqValidation = this.validators.sequence(parsedData.sequence, config.validateSequenceType);
                if (!seqValidation.valid) {
                    this.hideLoading(dropzone);
                    this.showError(previewPanel, seqValidation.errors[0]);
                    config.onError({ message: seqValidation.errors[0], file, parsedData });
                    return;
                }
            }

            // Hide loading, show success
            this.hideLoading(dropzone);
            this.showSuccess(dropzone);

            // Render preview
            if (config.showPreview) {
                this.renderPreview(parsedData, previewPanel, fileType);
            }

            // Callback
            config.onFileSelected(file, parsedData);

        } catch (error) {
            this.hideLoading(dropzone);
            this.showError(previewPanel, error.message);
            config.onError({ message: error.message, file, error });
        }
    },

    /**
     * Read file content
     */
    readFile(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = (e) => resolve(e.target.result);
            reader.onerror = (e) => reject(new Error('Failed to read file'));
            reader.readAsText(file);
        });
    },

    /**
     * Detect file type from filename and config
     */
    detectFileType(filename, previewTypeHint) {
        if (previewTypeHint !== 'auto') {
            return previewTypeHint;
        }

        const ext = filename.split('.').pop().toLowerCase();
        const typeMap = {
            'fasta': 'fasta', 'fa': 'fasta', 'faa': 'fasta', 'fna': 'fasta', 'seq': 'fasta',
            'pdb': 'pdb',
            'cif': 'cif',
            'smi': 'smiles', 'smiles': 'smiles',
            'txt': 'text'
        };

        return typeMap[ext] || 'text';
    },

    /**
     * Render preview based on file type
     */
    renderPreview(data, panel, fileType) {
        const contentDiv = panel.querySelector('.preview-content');
        contentDiv.textContent = ''; // Clear safely

        switch (fileType) {
            case 'fasta':
                this.renderers.sequence(data, contentDiv);
                break;
            case 'pdb':
            case 'cif':
                this.renderers.structure(data, contentDiv);
                break;
            case 'smiles':
                this.renderers.smiles(data, contentDiv);
                break;
            case 'text':
            default:
                this.renderers.text(data, contentDiv);
                break;
        }

        panel.style.display = 'block';
        panel.classList.add('slide-in');
        setTimeout(() => panel.classList.remove('slide-in'), 300);
    },

    // ==================== PARSERS ====================

    parsers: {
        /**
         * Parse FASTA file
         */
        fasta(content) {
            const lines = content.split('\n');
            const headers = [];
            let sequence = '';

            lines.forEach(line => {
                line = line.trim();
                if (line.startsWith('>')) {
                    headers.push(line.substring(1));
                } else if (line.length > 0) {
                    sequence += line;
                }
            });

            // Sequence stats
            const length = sequence.length;
            const composition = {};
            for (const char of sequence.toUpperCase()) {
                composition[char] = (composition[char] || 0) + 1;
            }

            // Calculate GC content for DNA/RNA
            const gcContent = ((composition['G'] || 0) + (composition['C'] || 0)) / length * 100;

            // Amino acid properties
            const hydrophobic = ['A', 'V', 'I', 'L', 'M', 'F', 'Y', 'W'];
            const charged = ['R', 'K', 'D', 'E', 'H'];
            const polar = ['S', 'T', 'N', 'Q', 'C'];

            let hydrophobicCount = 0, chargedCount = 0, polarCount = 0;
            for (const char of sequence.toUpperCase()) {
                if (hydrophobic.includes(char)) hydrophobicCount++;
                if (charged.includes(char)) chargedCount++;
                if (polar.includes(char)) polarCount++;
            }

            return {
                sequence,
                headers,
                length,
                composition,
                gcContent: gcContent.toFixed(1),
                properties: {
                    hydrophobic: (hydrophobicCount / length * 100).toFixed(1),
                    charged: (chargedCount / length * 100).toFixed(1),
                    polar: (polarCount / length * 100).toFixed(1)
                }
            };
        },

        /**
         * Parse PDB file
         */
        pdb(content) {
            const lines = content.split('\n');
            const chains = new Set();
            const residues = new Set();
            const ligands = new Set();
            let hasProtein = false;

            const proteinResidues = [
                'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY',
                'HIS', 'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER',
                'THR', 'TRP', 'TYR', 'VAL'
            ];

            lines.forEach(line => {
                if (line.startsWith('ATOM') || line.startsWith('HETATM')) {
                    const chain = line.substring(21, 22).trim();
                    const residue = line.substring(17, 20).trim();

                    if (chain) chains.add(chain);
                    if (residue) residues.add(residue);

                    if (proteinResidues.includes(residue)) {
                        hasProtein = true;
                    } else if (line.startsWith('HETATM') && residue !== 'HOH') {
                        ligands.add(residue);
                    }
                }
            });

            return {
                chains: Array.from(chains),
                residues: Array.from(residues),
                residueCount: residues.size,
                ligands: Array.from(ligands).filter(l => l !== 'HOH'),
                hasProtein,
                rawContent: content
            };
        },

        /**
         * Parse CIF file (simplified)
         */
        cif(content) {
            const lines = content.split('\n');
            const chains = new Set();

            lines.forEach(line => {
                if (line.startsWith('ATOM') || line.includes('_atom_site')) {
                    const parts = line.split(/\s+/);
                    if (parts.length > 5) {
                        chains.add(parts[5]);
                    }
                }
            });

            return {
                chains: Array.from(chains),
                hasProtein: true,
                rawContent: content
            };
        },

        /**
         * Parse/validate SMILES string
         */
        smiles(content) {
            const smiles = content.trim().split('\n')[0];
            const validChars = /^[A-Za-z0-9@+\-\[\]\(\)=#\\/\\%*\.]+$/;
            const isValid = validChars.test(smiles) && smiles.length > 0 && smiles.length < 1000;

            return {
                smiles,
                isValid,
                length: smiles.length
            };
        },

        /**
         * Parse text file (generic)
         */
        text(content) {
            const lines = content.split('\n');
            const preview = lines.slice(0, 10).join('\n');

            return {
                content,
                preview,
                lineCount: lines.length,
                charCount: content.length
            };
        }
    },

    // ==================== VALIDATORS ====================

    validators: {
        fileSize(file, maxSize) {
            if (file.size > maxSize) {
                return {
                    valid: false,
                    error: `File size exceeds limit (max ${FileUploadPreview.formatFileSize(maxSize)})`
                };
            }
            return { valid: true };
        },

        fileType(file, acceptString) {
            if (acceptString === '*') return { valid: true };

            const allowedExts = acceptString.split(',').map(ext => ext.trim().toLowerCase());
            const fileExt = '.' + file.name.split('.').pop().toLowerCase();

            if (!allowedExts.includes(fileExt)) {
                return {
                    valid: false,
                    error: `Invalid file type. Expected: ${allowedExts.join(', ')}`
                };
            }
            return { valid: true };
        },

        sequence(sequence, type) {
            const patterns = {
                'protein': /^[ACDEFGHIKLMNPQRSTVWY*]+$/i,
                'dna': /^[ATCGN]+$/i,
                'rna': /^[AUCGN]+$/i
            };

            const allowedChars = {
                'protein': 'ACDEFGHIKLMNPQRSTVWY*',
                'dna': 'ATCGN',
                'rna': 'AUCGN'
            };

            if (!type || !patterns[type]) {
                return { valid: true };
            }

            const pattern = patterns[type];
            const cleanSeq = sequence.replace(/\s/g, '').toUpperCase();

            if (!pattern.test(cleanSeq)) {
                return {
                    valid: false,
                    errors: [`Invalid ${type.toUpperCase()} sequence. Allowed characters: ${allowedChars[type]}`]
                };
            }

            if (cleanSeq.length < 1) {
                return {
                    valid: false,
                    errors: ['Sequence is empty']
                };
            }

            return { valid: true };
        }
    },

    // ==================== RENDERERS ====================

    renderers: {
        /**
         * Render sequence preview with stats using safe DOM methods
         */
        sequence(data, container) {
            const statsDiv = document.createElement('div');
            statsDiv.className = 'file-upload-sequence-stats';

            // Validation status
            const statusDiv = document.createElement('div');
            statusDiv.className = 'validation-status success';
            const statusSvg = FileUploadPreview.createSVGElement('svg', { width: '20', height: '20', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2.5' });
            statusSvg.appendChild(FileUploadPreview.createSVGElement('polyline', { points: '20 6 9 17 4 12' }));
            const statusText = document.createElement('span');
            statusText.textContent = 'Valid Sequence';
            statusDiv.appendChild(statusSvg);
            statusDiv.appendChild(statusText);

            // Stats
            const statGroup = document.createElement('div');
            statGroup.className = 'stat-group';

            const lengthStat = this.createStatItem('Length:', `${data.length} residues`);
            statGroup.appendChild(lengthStat);

            if (data.gcContent !== 'NaN') {
                const gcStat = this.createStatItem('GC Content:', `${data.gcContent}%`);
                statGroup.appendChild(gcStat);
            }

            // Composition
            const compSection = document.createElement('div');
            compSection.className = 'composition-section';
            const compTitle = document.createElement('h5');
            compTitle.textContent = 'Composition:';
            compSection.appendChild(compTitle);

            const compGrid = document.createElement('div');
            compGrid.className = 'composition-grid';

            Object.entries(data.composition)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 8)
                .forEach(([aa, count]) => {
                    const pct = (count / data.length * 100).toFixed(1);
                    const item = this.createCompositionItem(aa, count, pct);
                    compGrid.appendChild(item);
                });

            compSection.appendChild(compGrid);

            // Properties
            if (data.properties && data.properties.hydrophobic !== 'NaN') {
                const propSection = document.createElement('div');
                propSection.className = 'properties-section';
                const propTitle = document.createElement('h5');
                propTitle.textContent = 'Properties:';
                propSection.appendChild(propTitle);

                propSection.appendChild(this.createPropertyItem('Hydrophobic:', data.properties.hydrophobic, 'hydrophobic'));
                propSection.appendChild(this.createPropertyItem('Charged:', data.properties.charged, 'charged'));
                propSection.appendChild(this.createPropertyItem('Polar:', data.properties.polar, 'polar'));

                statsDiv.appendChild(statusDiv);
                statsDiv.appendChild(statGroup);
                statsDiv.appendChild(compSection);
                statsDiv.appendChild(propSection);
            } else {
                statsDiv.appendChild(statusDiv);
                statsDiv.appendChild(statGroup);
                statsDiv.appendChild(compSection);
            }

            container.appendChild(statsDiv);
        },

        createStatItem(label, value) {
            const item = document.createElement('div');
            item.className = 'stat-item';
            const labelSpan = document.createElement('span');
            labelSpan.className = 'stat-label';
            labelSpan.textContent = label;
            const valueSpan = document.createElement('span');
            valueSpan.className = 'stat-value';
            valueSpan.textContent = value;
            item.appendChild(labelSpan);
            item.appendChild(valueSpan);
            return item;
        },

        createCompositionItem(aa, count, pct) {
            const item = document.createElement('div');
            item.className = 'composition-item';

            const label = document.createElement('span');
            label.className = 'aa-label';
            label.textContent = `${aa}:`;

            const countSpan = document.createElement('span');
            countSpan.className = 'aa-count';
            countSpan.textContent = `${count} (${pct}%)`;

            const bar = document.createElement('div');
            bar.className = 'aa-bar';
            const barFill = document.createElement('div');
            barFill.className = 'aa-bar-fill';
            barFill.style.width = `${pct}%`;
            bar.appendChild(barFill);

            item.appendChild(label);
            item.appendChild(countSpan);
            item.appendChild(bar);
            return item;
        },

        createPropertyItem(label, value, className) {
            const item = document.createElement('div');
            item.className = 'property-item';

            const labelSpan = document.createElement('span');
            labelSpan.className = 'property-label';
            labelSpan.textContent = label;

            const bar = document.createElement('div');
            bar.className = 'property-bar';
            const barFill = document.createElement('div');
            barFill.className = `property-bar-fill ${className}`;
            barFill.style.width = `${value}%`;
            bar.appendChild(barFill);

            const valueSpan = document.createElement('span');
            valueSpan.className = 'property-value';
            valueSpan.textContent = `${value}%`;

            item.appendChild(labelSpan);
            item.appendChild(bar);
            item.appendChild(valueSpan);
            return item;
        },

        /**
         * Render structure preview with 3D viewer
         */
        structure(data, container) {
            const viewerDiv = document.createElement('div');
            viewerDiv.className = 'file-upload-structure-viewer';

            // Status
            const statusDiv = document.createElement('div');
            statusDiv.className = 'validation-status success';
            const statusSvg = FileUploadPreview.createSVGElement('svg', { width: '20', height: '20', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2.5' });
            statusSvg.appendChild(FileUploadPreview.createSVGElement('polyline', { points: '20 6 9 17 4 12' }));
            const statusText = document.createElement('span');
            statusText.textContent = `Valid ${data.hasProtein ? 'PDB' : 'Structure'} File`;
            statusDiv.appendChild(statusSvg);
            statusDiv.appendChild(statusText);

            // 3D viewer container
            const viewerContainer = document.createElement('div');
            viewerContainer.className = 'structure-viewer-container';
            viewerContainer.id = `structure-viewer-${Date.now()}`;

            // Structure info
            const infoDiv = document.createElement('div');
            infoDiv.className = 'structure-info';

            infoDiv.appendChild(this.createInfoItem('Chains:', data.chains.join(', ') || 'None'));
            if (data.residueCount) {
                infoDiv.appendChild(this.createInfoItem('Residues:', data.residueCount.toString()));
            }
            if (data.ligands && data.ligands.length > 0) {
                infoDiv.appendChild(this.createInfoItem('Ligands:', data.ligands.join(', ')));
            }
            infoDiv.appendChild(this.createInfoItem('Has protein:', data.hasProtein ? '✓ Yes' : '✗ No'));

            viewerDiv.appendChild(statusDiv);
            viewerDiv.appendChild(viewerContainer);
            viewerDiv.appendChild(infoDiv);
            container.appendChild(viewerDiv);

            // Initialize 3D viewer (PhonosViewer / Mol*)
            if (typeof PhonosViewer !== 'undefined' && data.rawContent) {
                setTimeout(() => {
                    FileUploadPreview.init3DViewer(viewerContainer.id, data.rawContent);
                }, 100);
            } else {
                const placeholder = document.createElement('div');
                placeholder.className = 'viewer-placeholder';
                const placeholderSvg = FileUploadPreview.createSVGElement('svg', { width: '64', height: '64', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5' });
                placeholderSvg.appendChild(FileUploadPreview.createSVGElement('path', { d: 'M12 2L2 7l10 5 10-5-10-5z' }));
                placeholderSvg.appendChild(FileUploadPreview.createSVGElement('path', { d: 'M2 17l10 5 10-5' }));
                placeholderSvg.appendChild(FileUploadPreview.createSVGElement('path', { d: 'M2 12l10 5 10-5' }));
                const placeholderText = document.createElement('p');
                placeholderText.textContent = '3D viewer not available';
                placeholder.appendChild(placeholderSvg);
                placeholder.appendChild(placeholderText);
                viewerContainer.appendChild(placeholder);
            }
        },

        createInfoItem(label, value) {
            const item = document.createElement('div');
            item.className = 'info-item';
            const labelSpan = document.createElement('span');
            labelSpan.className = 'info-label';
            labelSpan.textContent = label;
            const valueSpan = document.createElement('span');
            valueSpan.className = 'info-value';
            valueSpan.textContent = value;
            item.appendChild(labelSpan);
            item.appendChild(valueSpan);
            return item;
        },

        /**
         * Render SMILES preview
         */
        smiles(data, container) {
            const smilesDiv = document.createElement('div');
            smilesDiv.className = 'file-upload-smiles-preview';

            const statusDiv = document.createElement('div');
            statusDiv.className = data.isValid ? 'validation-status success' : 'validation-status error';

            const statusSvg = FileUploadPreview.createSVGElement('svg', { width: '20', height: '20', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2.5' });
            if (data.isValid) {
                statusSvg.appendChild(FileUploadPreview.createSVGElement('polyline', { points: '20 6 9 17 4 12' }));
            } else {
                statusSvg.appendChild(FileUploadPreview.createSVGElement('line', { x1: '18', y1: '6', x2: '6', y2: '18' }));
                statusSvg.appendChild(FileUploadPreview.createSVGElement('line', { x1: '6', y1: '6', x2: '18', y2: '18' }));
            }

            const statusText = document.createElement('span');
            statusText.textContent = data.isValid ? 'Valid SMILES' : 'Invalid SMILES';
            statusDiv.appendChild(statusSvg);
            statusDiv.appendChild(statusText);

            const contentDiv = document.createElement('div');
            contentDiv.className = 'smiles-content';

            const stringDiv = document.createElement('div');
            stringDiv.className = 'smiles-string';
            const code = document.createElement('code');
            code.textContent = data.smiles;
            stringDiv.appendChild(code);

            const infoDiv = document.createElement('div');
            infoDiv.className = 'smiles-info';
            const label = document.createElement('span');
            label.className = 'info-label';
            label.textContent = 'Length:';
            const value = document.createElement('span');
            value.className = 'info-value';
            value.textContent = `${data.length} characters`;
            infoDiv.appendChild(label);
            infoDiv.appendChild(value);

            contentDiv.appendChild(stringDiv);
            contentDiv.appendChild(infoDiv);

            const noteDiv = document.createElement('div');
            noteDiv.className = 'smiles-note';
            const noteP1 = document.createElement('p');
            noteP1.textContent = '2D structure rendering requires additional library.';
            const noteP2 = document.createElement('p');
            noteP2.textContent = 'SMILES will be validated on the server.';
            noteDiv.appendChild(noteP1);
            noteDiv.appendChild(noteP2);

            smilesDiv.appendChild(statusDiv);
            smilesDiv.appendChild(contentDiv);
            smilesDiv.appendChild(noteDiv);
            container.appendChild(smilesDiv);
        },

        /**
         * Render text preview
         */
        text(data, container) {
            const textDiv = document.createElement('div');
            textDiv.className = 'file-upload-text-preview';

            const infoDiv = document.createElement('div');
            infoDiv.className = 'text-info';

            const label1 = document.createElement('span');
            label1.className = 'info-label';
            label1.textContent = 'Lines:';
            const value1 = document.createElement('span');
            value1.className = 'info-value';
            value1.textContent = data.lineCount.toString();

            const label2 = document.createElement('span');
            label2.className = 'info-label';
            label2.textContent = 'Characters:';
            const value2 = document.createElement('span');
            value2.className = 'info-value';
            value2.textContent = data.charCount.toString();

            infoDiv.appendChild(label1);
            infoDiv.appendChild(value1);
            infoDiv.appendChild(label2);
            infoDiv.appendChild(value2);

            const contentDiv = document.createElement('div');
            contentDiv.className = 'text-content';
            const pre = document.createElement('pre');
            const code = document.createElement('code');
            code.textContent = data.preview + (data.lineCount > 10 ? '\n...' : '');
            pre.appendChild(code);
            contentDiv.appendChild(pre);

            textDiv.appendChild(infoDiv);
            textDiv.appendChild(contentDiv);
            container.appendChild(textDiv);
        }
    },

    // ==================== 3D VIEWER ====================

    init3DViewer(containerId, pdbContent) {
        const container = document.getElementById(containerId);
        if (!container || typeof PhonosViewer === 'undefined') return;

        try {
            container.setAttribute('data-phonos-viewer', '');
            const pv = PhonosViewer.create(container, {
                hideControls: true,
                sequencePanel: false,
                focusPanel: false
            });
            pv.loadData(pdbContent, 'pdb');
        } catch (error) {
            console.error('Failed to initialize 3D viewer:', error);
            container.textContent = 'Failed to render 3D structure';
        }
    },

    // ==================== UI HELPERS ====================

    showLoading(dropzone) {
        dropzone.classList.add('loading');
        const icon = dropzone.querySelector('.dropzone-icon');
        if (icon) {
            icon.textContent = '';
            const svg = this.createSVGElement('svg', { width: '48', height: '48', viewBox: '0 0 24 24' });
            svg.classList.add('spinner');
            const circle = this.createSVGElement('circle', { cx: '12', cy: '12', r: '10', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-dasharray': '31.4 31.4', 'stroke-linecap': 'round' });
            svg.appendChild(circle);
            icon.appendChild(svg);
        }
        const text = dropzone.querySelector('.dropzone-text');
        if (text) text.textContent = 'Processing file...';
    },

    hideLoading(dropzone) {
        dropzone.classList.remove('loading');
        const icon = dropzone.querySelector('.dropzone-icon');
        if (icon) {
            icon.textContent = '';
            const svg = this.createSVGElement('svg', { width: '48', height: '48', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
            svg.appendChild(this.createSVGElement('path', { d: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4' }));
            svg.appendChild(this.createSVGElement('polyline', { points: '17 8 12 3 7 8' }));
            svg.appendChild(this.createSVGElement('line', { x1: '12', y1: '3', x2: '12', y2: '15' }));
            icon.appendChild(svg);
        }
        const text = dropzone.querySelector('.dropzone-text');
        if (text) text.textContent = 'Drop file here or click to browse';
    },

    showSuccess(dropzone) {
        dropzone.classList.add('success');
        const icon = dropzone.querySelector('.dropzone-icon');
        if (icon) {
            icon.textContent = '';
            const svg = this.createSVGElement('svg', { width: '48', height: '48', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2.5' });
            svg.classList.add('checkmark');
            svg.appendChild(this.createSVGElement('polyline', { points: '20 6 9 17 4 12' }));
            icon.appendChild(svg);
        }
        setTimeout(() => {
            dropzone.classList.remove('success');
            this.hideLoading(dropzone);
        }, 2000);
    },

    showError(panel, message) {
        const contentDiv = panel.querySelector('.preview-content');
        contentDiv.textContent = '';

        const errorDiv = document.createElement('div');
        errorDiv.className = 'file-upload-validation-message error';

        const svg = this.createSVGElement('svg', { width: '24', height: '24', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' });
        svg.appendChild(this.createSVGElement('circle', { cx: '12', cy: '12', r: '10' }));
        svg.appendChild(this.createSVGElement('line', { x1: '12', y1: '8', x2: '12', y2: '12' }));
        svg.appendChild(this.createSVGElement('line', { x1: '12', y1: '16', x2: '12.01', y2: '16' }));

        const messageContent = document.createElement('div');
        messageContent.className = 'message-content';
        const h4 = document.createElement('h4');
        h4.textContent = 'Error Loading File';
        const p = document.createElement('p');
        p.textContent = message;
        messageContent.appendChild(h4);
        messageContent.appendChild(p);

        errorDiv.appendChild(svg);
        errorDiv.appendChild(messageContent);
        contentDiv.appendChild(errorDiv);

        panel.style.display = 'block';
        panel.classList.add('slide-in');
        setTimeout(() => panel.classList.remove('slide-in'), 300);
    },

    hidePreview(panel) {
        panel.style.display = 'none';
        panel.querySelector('.preview-content').textContent = '';
    },

    destroyInstance(instanceId) {
        const instance = this.instances.get(instanceId);
        if (instance) {
            instance.dropzone.remove();
            instance.previewPanel.remove();
            this.instances.delete(instanceId);
        }
    },

    resetInstance(instanceId) {
        const instance = this.instances.get(instanceId);
        if (instance) {
            this.hidePreview(instance.previewPanel);
            this.hideLoading(instance.dropzone);
            instance.fileInput.value = '';
            instance.currentFile = null;
            instance.currentData = null;
        }
    },

    // ==================== UTILITIES ====================

    formatAcceptedTypes(acceptString) {
        if (acceptString === '*') return 'all file types';
        return acceptString.split(',').map(t => t.trim()).join(', ');
    },

    formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }
};

// Export for module systems if needed
if (typeof module !== 'undefined' && module.exports) {
    module.exports = FileUploadPreview;
}
