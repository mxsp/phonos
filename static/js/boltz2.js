/**
 * Boltz2 Structure Prediction UI
 * Handles user interactions, API calls, and 3D visualization
 */

// State
let currentMoleculeType = 'protein';
let currentLigandType = 'none';
let selectedLigandCCD = '';
let currentLigandCategory = 'all';
let viewer3D = null;
let currentStructure = null;
let currentDbSource = 'pubchem';
let selectedDbCompound = null;
let isMultimer = false;
let isBatch = false;
let multimerChains = []; // [{id, moleculeType, sequence, structure}]
let multimerBatch = null; // MultimerBatch controller (shared Structure-above-chains model)

const CHAIN_COLORS = ['#0d9488', '#8b5cf6', '#f59e0b', '#ef4444', '#3b82f6', '#ec4899',
                       '#06b6d4', '#f97316', '#84cc16', '#a855f7', '#14b8a6', '#e11d48'];

// Extended common ligands database with categories
const commonLigands = [
    // Nucleotides
    { ccd: 'ATP', name: 'Adenosine Triphosphate', category: 'nucleotides' },
    { ccd: 'ADP', name: 'Adenosine Diphosphate', category: 'nucleotides' },
    { ccd: 'AMP', name: 'Adenosine Monophosphate', category: 'nucleotides' },
    { ccd: 'GTP', name: 'Guanosine Triphosphate', category: 'nucleotides' },
    { ccd: 'GDP', name: 'Guanosine Diphosphate', category: 'nucleotides' },
    { ccd: 'CTP', name: 'Cytidine Triphosphate', category: 'nucleotides' },
    { ccd: 'UTP', name: 'Uridine Triphosphate', category: 'nucleotides' },
    { ccd: 'TTP', name: 'Thymidine Triphosphate', category: 'nucleotides' },

    // Cofactors
    { ccd: 'NAD', name: 'NAD+', category: 'cofactors' },
    { ccd: 'NAP', name: 'NADP+', category: 'cofactors' },
    { ccd: 'FAD', name: 'FAD', category: 'cofactors' },
    { ccd: 'FMN', name: 'FMN', category: 'cofactors' },
    { ccd: 'HEM', name: 'Heme', category: 'cofactors' },
    { ccd: 'COA', name: 'Coenzyme A', category: 'cofactors' },
    { ccd: 'SAM', name: 'S-Adenosylmethionine', category: 'cofactors' },
    { ccd: 'PLP', name: 'Pyridoxal Phosphate', category: 'cofactors' },
    { ccd: 'TPP', name: 'Thiamine Pyrophosphate', category: 'cofactors' },
    { ccd: 'BTN', name: 'Biotin', category: 'cofactors' },
    { ccd: 'B12', name: 'Cobalamin (B12)', category: 'cofactors' },

    // Metal Ions
    { ccd: 'ZN', name: 'Zinc Ion', category: 'metal-ions' },
    { ccd: 'MG', name: 'Magnesium Ion', category: 'metal-ions' },
    { ccd: 'CA', name: 'Calcium Ion', category: 'metal-ions' },
    { ccd: 'FE', name: 'Iron Ion (Fe2+)', category: 'metal-ions' },
    { ccd: 'FE2', name: 'Iron Ion (Fe3+)', category: 'metal-ions' },
    { ccd: 'MN', name: 'Manganese Ion', category: 'metal-ions' },
    { ccd: 'CU', name: 'Copper Ion', category: 'metal-ions' },
    { ccd: 'NI', name: 'Nickel Ion', category: 'metal-ions' },
    { ccd: 'CO', name: 'Cobalt Ion', category: 'metal-ions' },
    { ccd: 'K', name: 'Potassium Ion', category: 'metal-ions' },
    { ccd: 'NA', name: 'Sodium Ion', category: 'metal-ions' },

    // Common Drugs / Small Molecules
    { ccd: 'ACE', name: 'Acetate', category: 'drugs' },
    { ccd: 'PO4', name: 'Phosphate', category: 'drugs' },
    { ccd: 'SO4', name: 'Sulfate', category: 'drugs' },
    { ccd: 'GLU', name: 'Glucose', category: 'drugs' },
    { ccd: 'NAG', name: 'N-Acetyl Glucosamine', category: 'drugs' },
    { ccd: 'GOL', name: 'Glycerol', category: 'drugs' },
    { ccd: 'EDO', name: 'Ethylene Glycol', category: 'drugs' },
    { ccd: 'STI', name: 'Imatinib (Gleevec)', category: 'drugs' },
    { ccd: 'AZM', name: 'Acetazolamide', category: 'drugs' },
];

// Example sequences
const examples = {
    insulin: {
        type: 'protein',
        sequence: 'GIVEQCCTSICSLYQLENYCN',
        ligand: 'none'
    },
    'insulin-atp': {
        type: 'protein',
        sequence: 'MKFLKFSLLTAVLLSVVFAFSSCGDDDDTGYLPPSQAIQDLLKRMKV',
        ligand: 'ccd',
        ligand_ccd: 'ATP'
    },
    dna: {
        type: 'dna',
        sequence: 'ATCGATCGATCG',
        ligand: 'none'
    },
    'insulin-complex': {
        multimer: true,
        chains: [
            { moleculeType: 'protein', sequence: 'GIVEQCCTSICSLYQLENYCN' },
            { moleculeType: 'protein', sequence: 'FVNQHLCGSHLVEALYLVCGERGFFYTPKT' }
        ],
        ligand: 'none'
    }
};

// Utility Functions
function parseFASTA(text) {
    if (!text || !text.trim()) return '';

    const lines = text.trim().split('\n');
    let sequence = '';

    for (const line of lines) {
        const trimmedLine = line.trim();

        // Skip empty lines, headers (>), comments (;), and any line that looks like metadata
        if (!trimmedLine ||
            trimmedLine.startsWith('>') ||
            trimmedLine.startsWith(';') ||
            trimmedLine.startsWith('#')) {
            continue;
        }

        // Extract only alphabetic characters (valid sequence characters)
        const cleanedLine = trimmedLine.replace(/[^A-Za-z]/g, '');
        if (cleanedLine) {
            sequence += cleanedLine.toUpperCase();
        }
    }

    return sequence;
}

function handleFileUpload(file) {
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e) => {
        const content = e.target.result;
        const sequence = parseFASTA(content);

        // Set the parsed sequence in the textarea
        const sequenceTextarea = document.getElementById('sequence');
        if (sequenceTextarea) {
            sequenceTextarea.value = sequence;
        }

        // Display file name
        const fileNameDisplay = document.getElementById('file-name-display');
        if (fileNameDisplay) {
            fileNameDisplay.textContent = 'Loaded: ' + file.name;
        }

        // Show success message
        showSuccess('Loaded ' + sequence.length + ' ' + (currentMoleculeType === 'protein' ? 'residues' : 'bases') + ' from ' + file.name);
    };

    reader.onerror = () => {
        showError('Failed to read file');
    };

    reader.readAsText(file);
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initializeEventListeners();
    populateLigandGrid();
    updateSequenceHints();
    initializeFileUploadPreview();
    initializeMultimerToggle();
});

function initializeFileUploadPreview() {
    const dropzoneContainer = document.getElementById('boltz2-sequence-dropzone-container');
    const sequenceTextarea = document.getElementById('sequence');

    if (!dropzoneContainer || !sequenceTextarea || typeof FileUploadPreview === 'undefined') {
        return;
    }

    FileUploadPreview.create(dropzoneContainer, {
        accept: '.fasta,.fa,.faa,.fna,.txt,.seq',
        maxSize: 10 * 1024 * 1024,
        previewType: 'sequence',
        validateSequenceType: null, // Will validate based on selected molecule type
        onFileSelected: (file, parsedData) => {
            // Set the parsed sequence
            sequenceTextarea.value = parsedData.sequence;

            // Show success message
            showNotification('Loaded ' + parsedData.length + ' residues from ' + file.name, 'success');

            // Validate against current molecule type
            validateSequence(parsedData.sequence);
        },
        onError: (error) => {
            showNotification(error.message, 'error');
        }
    });
}

function validateSequence(sequence) {
    if (!sequence) return;

    const type = currentMoleculeType;
    const patterns = {
        'protein': /^[ACDEFGHIKLMNPQRSTVWY]+$/i,
        'dna': /^[ATCG]+$/i,
        'rna': /^[AUCG]+$/i
    };

    if (patterns[type] && !patterns[type].test(sequence)) {
        const allowedChars = {
            'protein': 'ACDEFGHIKLMNPQRSTVWY',
            'dna': 'ATCG',
            'rna': 'AUCG'
        };
        showNotification('Invalid ' + type.toUpperCase() + ' sequence. Allowed characters: ' + allowedChars[type], 'warning');
    }
}

function showNotification(message, type) {
    // Simple notification display
    const notification = document.createElement('div');
    notification.style.cssText = '\n        position: fixed;\n        top: 20px;\n        right: 20px;\n        padding: 16px 24px;\n        background: ' + (type === 'success' ? '#10b981' : type === 'error' ? '#ef4444' : '#f59e0b') + ';\n        color: white;\n        border-radius: var(--r-ui, 0px);\n        box-shadow: 0 4px 12px rgba(0,0,0,0.15);\n        z-index: 10000;\n        animation: slideIn 0.3s ease-out;\n        font-size: 14px;\n        font-weight: 500;\n        max-width: 400px;\n    ';
    notification.textContent = message;
    document.body.appendChild(notification);

    setTimeout(() => {
        notification.style.animation = 'slideOut 0.3s ease-in';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}

function initializeEventListeners() {
    // Molecule type tabs
    document.querySelectorAll('[data-type]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const type = btn.dataset.type;
            selectMoleculeType(type);
        });
    });

    // Ligand type tabs
    document.querySelectorAll('[data-ligand]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const ligandType = btn.dataset.ligand;
            selectLigandType(ligandType);
        });
    });

    // Ligand category tabs
    document.querySelectorAll('[data-category]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const category = btn.dataset.category;
            selectLigandCategory(category);
        });
    });

    // Ligand search
    const ligandSearch = document.getElementById('ligand-search');
    if (ligandSearch) {
        ligandSearch.addEventListener('input', (e) => {
            filterLigands(e.target.value);
        });
    }

    // SMILES example buttons
    document.querySelectorAll('.smiles-example-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const smiles = btn.dataset.smiles;
            const smilesInput = document.getElementById('ligand-smiles-input');
            if (smilesInput && smiles) {
                smilesInput.value = smiles;
            }
        });
    });

    // Project SDF/MOL -> SMILES ligand picker
    setupBoltz2ProjectLigandPicker();

    // Database source tabs
    document.querySelectorAll('.db-source-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            selectDbSource(btn.dataset.source);
        });
    });

    // Database search
    const dbSearchBtn = document.getElementById('ligand-db-search-btn');
    const dbSearchInput = document.getElementById('ligand-db-search');
    if (dbSearchBtn) {
        dbSearchBtn.addEventListener('click', () => {
            const query = dbSearchInput ? dbSearchInput.value.trim() : '';
            if (query) searchDatabase(query);
        });
    }
    if (dbSearchInput) {
        dbSearchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const query = dbSearchInput.value.trim();
                if (query) searchDatabase(query);
            }
        });
    }

    // Clear database selection
    const clearDbBtn = document.getElementById('clear-db-selection');
    if (clearDbBtn) {
        clearDbBtn.addEventListener('click', () => {
            selectedDbCompound = null;
            const dbSelected = document.getElementById('ligand-db-selected');
            if (dbSelected) dbSelected.classList.add('hidden');
        });
    }

    // Sliders
    document.getElementById('recycling-steps').addEventListener('input', (e) => {
        document.getElementById('recycling-value').textContent = e.target.value;
    });

    document.getElementById('sampling-steps').addEventListener('input', (e) => {
        document.getElementById('sampling-value').textContent = e.target.value;
    });

    document.getElementById('diffusion-samples').addEventListener('input', (e) => {
        document.getElementById('diffusion-value').textContent = e.target.value;
    });

    // Step scale slider
    const stepScaleSlider = document.getElementById('step-scale');
    if (stepScaleSlider) {
        stepScaleSlider.addEventListener('input', (e) => {
            document.getElementById('step-scale-value').textContent = parseFloat(e.target.value).toFixed(2);
        });
    }

    // Affinity sliders
    const affinitySamplingSlider = document.getElementById('affinity-sampling-steps');
    if (affinitySamplingSlider) {
        affinitySamplingSlider.addEventListener('input', (e) => {
            document.getElementById('affinity-sampling-value').textContent = e.target.value;
        });
    }

    const affinitySamplesSlider = document.getElementById('affinity-samples');
    if (affinitySamplesSlider) {
        affinitySamplesSlider.addEventListener('input', (e) => {
            document.getElementById('affinity-samples-value').textContent = e.target.value;
        });
    }

    // Predict affinity checkbox
    const predictAffinityCheckbox = document.getElementById('predict-affinity');
    if (predictAffinityCheckbox) {
        predictAffinityCheckbox.addEventListener('change', (e) => {
            const subParams = document.getElementById('affinity-sub-params');
            if (subParams) {
                subParams.classList.toggle('hidden', !e.target.checked);
            }
        });
    }

    // Examples
    document.querySelectorAll('[data-example]').forEach(card => {
        card.addEventListener('click', () => {
            const exampleKey = card.dataset.example;
            loadExample(exampleKey);
        });
    });

    // File upload (supports multiple files)
    const fileInput = document.getElementById('sequence-file-input');
    if (fileInput) {
        fileInput.addEventListener('change', (e) => {
            const files = Array.from(e.target.files);
            for (const file of files) {
                handleFileUpload(file);
            }
        });
    }

    // FASTA parsing on paste
    const sequenceTextarea = document.getElementById('sequence');
    if (sequenceTextarea) {
        sequenceTextarea.addEventListener('paste', (e) => {
            setTimeout(() => {
                const value = sequenceTextarea.value;
                if (value.includes('>')) {
                    // Contains FASTA format, parse it
                    const parsed = parseFASTA(value);
                    sequenceTextarea.value = parsed;
                }
            }, 10);
        });
    }

    // Form submission
    document.getElementById('prediction-form').addEventListener('submit', (e) => {
        e.preventDefault();
        submitPrediction();
    });
}

function selectMoleculeType(type) {
    currentMoleculeType = type;

    // Update tabs
    document.querySelectorAll('[data-type]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.type === type);
    });

    // Update hidden input
    document.getElementById('molecule-type').value = type;

    // Show/hide cyclic option (only for proteins)
    const cyclicGroup = document.getElementById('cyclic-group');
    if (cyclicGroup) {
        cyclicGroup.style.display = type === 'protein' ? 'block' : 'none';
    }

    // Update UI text
    updateSequenceHints();
}

function selectLigandType(type) {
    currentLigandType = type;
    console.log('Ligand type selected:', type);

    // Update tabs
    document.querySelectorAll('[data-ligand]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.ligand === type);
    });

    // Get panel elements
    const ccdPanel = document.getElementById('ligand-ccd-panel');
    const smilesPanel = document.getElementById('ligand-smiles-panel');
    const databasePanel = document.getElementById('ligand-database-panel');
    const affinityParams = document.getElementById('affinity-params');
    const ligandIdGroup = document.getElementById('ligand-id-group');

    // Hide all panels first
    if (ccdPanel) ccdPanel.classList.add('hidden');
    if (smilesPanel) smilesPanel.classList.add('hidden');
    if (databasePanel) databasePanel.classList.add('hidden');
    if (affinityParams) affinityParams.classList.add('hidden');
    if (ligandIdGroup) ligandIdGroup.classList.add('hidden');

    // Show relevant panels based on selection
    const pocketSection = document.getElementById('pocket-constraints-section');
    if (type === 'ccd') {
        if (ccdPanel) ccdPanel.classList.remove('hidden');
        if (affinityParams) affinityParams.classList.remove('hidden');
        if (ligandIdGroup) ligandIdGroup.classList.remove('hidden');
        if (pocketSection) pocketSection.classList.remove('hidden');
    } else if (type === 'smiles') {
        if (smilesPanel) smilesPanel.classList.remove('hidden');
        if (affinityParams) affinityParams.classList.remove('hidden');
        if (ligandIdGroup) ligandIdGroup.classList.remove('hidden');
        if (pocketSection) pocketSection.classList.remove('hidden');
    } else if (type === 'database') {
        if (databasePanel) databasePanel.classList.remove('hidden');
        if (affinityParams) affinityParams.classList.remove('hidden');
        if (ligandIdGroup) ligandIdGroup.classList.remove('hidden');
        if (pocketSection) pocketSection.classList.remove('hidden');
    } else {
        if (pocketSection) pocketSection.classList.add('hidden');
    }

    // Clear selections when switching away from CCD
    if (type !== 'ccd') {
        selectedLigandCCD = '';
        const ligandCcdInput = document.getElementById('ligand-ccd');
        if (ligandCcdInput) ligandCcdInput.value = '';
        document.querySelectorAll('.ligand-option').forEach(opt => {
            opt.classList.remove('selected');
        });
    }

    // Clear SMILES input when switching away
    if (type !== 'smiles') {
        const smilesInput = document.getElementById('ligand-smiles-input');
        if (smilesInput) {
            smilesInput.value = '';
        }
    }

    // Clear database selection when switching away
    if (type !== 'database') {
        selectedDbCompound = null;
        const dbSelected = document.getElementById('ligand-db-selected');
        if (dbSelected) dbSelected.classList.add('hidden');
    }
}

function selectLigandCategory(category) {
    currentLigandCategory = category;

    // Update category tabs
    document.querySelectorAll('[data-category]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.category === category);
    });

    // Re-populate grid with filter
    populateLigandGrid(category);
}

function filterLigands(searchTerm) {
    const term = searchTerm.toLowerCase().trim();
    const grid = document.getElementById('ligand-grid');

    if (!grid) return;

    // Filter ligands
    const filteredLigands = commonLigands.filter(ligand => {
        // Check category
        if (currentLigandCategory !== 'all' && ligand.category !== currentLigandCategory) {
            return false;
        }

        // Check search term
        if (term) {
            return ligand.ccd.toLowerCase().includes(term) ||
                   ligand.name.toLowerCase().includes(term);
        }

        return true;
    });

    // Re-render grid
    renderLigandGrid(filteredLigands);
}

function updateSequenceHints() {
    const hints = {
        protein: {
            label: 'Protein Sequence',
            placeholder: 'Enter amino acid sequence (e.g., MKFLKFSLLT...) or load from file\n\nSupports FASTA format (headers will be stripped)',
            hint: 'Standard amino acids only (ACDEFGHIKLMNPQRSTVWY). Max 4096 residues. Supports FASTA format.'
        },
        dna: {
            label: 'DNA Sequence',
            placeholder: 'Enter DNA sequence (e.g., ATCGATCG...) or load from file\n\nSupports FASTA format (headers will be stripped)',
            hint: 'DNA nucleotides only (ACGT). Max 4096 bases. Supports FASTA format.'
        },
        rna: {
            label: 'RNA Sequence',
            placeholder: 'Enter RNA sequence (e.g., AUCGAUCG...) or load from file\n\nSupports FASTA format (headers will be stripped)',
            hint: 'RNA nucleotides only (ACGU). Max 4096 bases. Supports FASTA format.'
        }
    };

    const config = hints[currentMoleculeType];
    document.getElementById('sequence-label').textContent = config.label;
    document.getElementById('sequence').placeholder = config.placeholder;
    document.getElementById('sequence-hint').textContent = config.hint;
}

function populateLigandGrid(category) {
    category = category || currentLigandCategory;

    // Filter by category
    const filteredLigands = category === 'all'
        ? commonLigands
        : commonLigands.filter(l => l.category === category);

    renderLigandGrid(filteredLigands);
}

function renderLigandGrid(ligands) {
    const grid = document.getElementById('ligand-grid');
    if (!grid) return;

    // Clear safely
    while (grid.firstChild) {
        grid.removeChild(grid.firstChild);
    }

    ligands.forEach(ligand => {
        const option = document.createElement('div');
        option.className = 'ligand-option';
        if (selectedLigandCCD === ligand.ccd) {
            option.classList.add('selected');
        }

        const code = document.createElement('div');
        code.className = 'ligand-code';
        code.textContent = ligand.ccd;

        const name = document.createElement('div');
        name.className = 'ligand-name';
        name.textContent = ligand.name;

        option.appendChild(code);
        option.appendChild(name);
        option.addEventListener('click', () => selectLigand(ligand.ccd, option));
        grid.appendChild(option);
    });

    // Show empty state if no ligands match
    if (ligands.length === 0) {
        const emptyMsg = document.createElement('div');
        emptyMsg.style.cssText = 'grid-column: 1 / -1; text-align: center; padding: 20px; color: #71717a; font-size: 13px;';
        emptyMsg.textContent = 'No ligands match your search';
        grid.appendChild(emptyMsg);
    }
}

function selectLigand(ccd, element) {
    selectedLigandCCD = ccd;
    document.getElementById('ligand-ccd').value = ccd;

    // Update UI
    document.querySelectorAll('.ligand-option').forEach(opt => {
        opt.classList.remove('selected');
    });
    element.classList.add('selected');

    // Show feedback
    const ligandName = element.querySelector('.ligand-name').textContent;
    console.log('Selected ligand: ' + ccd + ' (' + ligandName + ')');
}

function loadExample(key) {
    const example = examples[key];
    if (!example) return;

    if (example.multimer) {
        // Switch to multimer mode
        var modeSwitch = document.getElementById('mode-switch');
        var allModeBtns = modeSwitch.querySelectorAll('.mode-switch-btn');
        for (var j = 0; j < allModeBtns.length; j++) allModeBtns[j].classList.remove('active');
        modeSwitch.querySelector('[data-mode="multimer"]').classList.add('active');
        isMultimer = true;
        document.getElementById('monomer-input').classList.add('hidden');
        document.getElementById('multimer-input').classList.remove('hidden');

        // Clear and rebuild chains
        multimerChains = [];
        var container = document.getElementById('chains-container');
        while (container.firstChild) container.removeChild(container.firstChild);

        for (var c = 0; c < example.chains.length; c++) {
            addChainEntry();
            multimerChains[c].moleculeType = example.chains[c].moleculeType;
            multimerChains[c].sequence = example.chains[c].sequence;
            var entry = container.children[c];
            var textarea = entry.querySelector('.chain-sequence-input');
            if (textarea) textarea.value = example.chains[c].sequence;
            var select = entry.querySelector('.chain-type-select');
            if (select) select.value = example.chains[c].moleculeType;
        }
    } else {
        // Switch to monomer mode
        var modeSwitch = document.getElementById('mode-switch');
        var allModeBtns = modeSwitch.querySelectorAll('.mode-switch-btn');
        for (var j = 0; j < allModeBtns.length; j++) allModeBtns[j].classList.remove('active');
        modeSwitch.querySelector('[data-mode="monomer"]').classList.add('active');
        isMultimer = false;
        document.getElementById('monomer-input').classList.remove('hidden');
        document.getElementById('multimer-input').classList.add('hidden');

        // Set molecule type
        selectMoleculeType(example.type);

        // Set sequence
        document.getElementById('sequence').value = example.sequence;
    }

    // Set ligand
    selectLigandType(example.ligand);
    if (example.ligand === 'ccd' && example.ligand_ccd) {
        const ligandOption = Array.from(document.querySelectorAll('.ligand-option'))
            .find(opt => opt.querySelector('.ligand-code').textContent === example.ligand_ccd);
        if (ligandOption) {
            selectLigand(example.ligand_ccd, ligandOption);
        }
    }

    // Scroll to top
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ============================================================================
// MULTIMER CHAIN MANAGEMENT
// ============================================================================

function initializeMultimerToggle() {
    var modeSwitch = document.getElementById('mode-switch');
    if (!modeSwitch) return;

    var btns = modeSwitch.querySelectorAll('.mode-switch-btn');
    for (var i = 0; i < btns.length; i++) {
        btns[i].addEventListener('click', function() {
            var allBtns = modeSwitch.querySelectorAll('.mode-switch-btn');
            for (var j = 0; j < allBtns.length; j++) allBtns[j].classList.remove('active');
            this.classList.add('active');

            var mode = this.dataset.mode;
            isMultimer = (mode === 'multimer');
            isBatch = (mode === 'batch');

            document.getElementById('monomer-input').classList.toggle('hidden', isMultimer || isBatch);
            document.getElementById('multimer-input').classList.toggle('hidden', !isMultimer);
            document.getElementById('batch-input').classList.toggle('hidden', !isBatch);

            if (isMultimer && multimerChains.length === 0) {
                // Initialize with 2 chains for multimer
                addChainEntry();
                addChainEntry();
            }

            if (isBatch) {
                ensureMultimerBatch();
                if (multimerBatch.count() === 0) {
                    // Initialize with 1 structure for batch
                    multimerBatch.addStructure();
                }
            }
        });
    }

    // Add chain button
    var addBtn = document.getElementById('add-chain-btn');
    if (addBtn) {
        addBtn.addEventListener('click', function() {
            if (multimerChains.length >= 12) {
                showError('Maximum 12 chains allowed');
                return;
            }
            addChainEntry();
        });
    }

    // Add batch item button
    var addBatchBtn = document.getElementById('add-batch-item-btn');
    if (addBatchBtn) {
        addBatchBtn.addEventListener('click', function() {
            ensureMultimerBatch();
            multimerBatch.addStructure();
        });
    }

    // Upload batch button
    var uploadBatchBtn = document.getElementById('upload-batch-btn');
    var batchFileInput = document.getElementById('batch-file-input');
    if (uploadBatchBtn && batchFileInput) {
        uploadBatchBtn.addEventListener('click', () => batchFileInput.click());
        batchFileInput.addEventListener('change', (e) => {
            ensureMultimerBatch();
            multimerBatch.loadFile(e.target.files[0]);
            e.target.value = '';
        });
    }
}

// Lazily create the shared MultimerBatch controller (Structure-above-chains model).
function ensureMultimerBatch() {
    if (multimerBatch) return multimerBatch;
    multimerBatch = new MultimerBatch({
        model: 'type-9',
        batchName: 'Boltz2 Batch',
        containerId: 'batch-items-container',
        maxStructures: 50,
        maxChains: 12,
        moleculeTypes: ['protein', 'dna', 'rna'],
        parseSequence: parseFASTA,
        onError: showError,
        onSuccess: showSuccess
    });
    return multimerBatch;
}

function addChainEntry() {
    var chainIndex = multimerChains.length;
    var chainId = String.fromCharCode(65 + chainIndex); // A, B, C, ...
    var color = CHAIN_COLORS[chainIndex % CHAIN_COLORS.length];

    var chainData = { id: chainId, moleculeType: 'protein', sequence: '', structure: null, inputType: 'sequence' };
    multimerChains.push(chainData);

    var container = document.getElementById('chains-container');
    var entry = document.createElement('div');
    entry.className = 'chain-entry';
    entry.dataset.chainIndex = chainIndex;

    var header = document.createElement('div');
    header.className = 'chain-entry-header';

    var labelSpan = document.createElement('span');
    labelSpan.className = 'chain-label';

    var badge = document.createElement('span');
    badge.className = 'chain-id-badge';
    badge.style.background = color;
    badge.textContent = chainId;

    var labelText = document.createTextNode(' Chain ' + chainId);
    labelSpan.appendChild(badge);
    labelSpan.appendChild(labelText);

    var rightControls = document.createElement('div');
    rightControls.style.cssText = 'display: flex; align-items: center; gap: 8px;';

    // Input Type Toggle (Seq/Struct)
    var inputTypeSelect = document.createElement('select');
    inputTypeSelect.className = 'chain-type-select';
    inputTypeSelect.style.width = '100px';
    inputTypeSelect.innerHTML = '<option value="sequence">Sequence</option><option value="structure">Structure</option>';
    inputTypeSelect.addEventListener('change', function() {
        var idx = parseInt(this.closest('.chain-entry').dataset.chainIndex);
        var type = this.value;
        multimerChains[idx].inputType = type;
        entry.querySelector('.chain-sequence-input').classList.toggle('hidden', type !== 'sequence');
        entry.querySelector('.chain-structure-input').classList.toggle('hidden', type !== 'structure');
    });
    rightControls.appendChild(inputTypeSelect);

    var typeSelect = document.createElement('select');
    typeSelect.className = 'chain-type-select';
    typeSelect.dataset.chainIndex = chainIndex;
    var types = ['protein', 'dna', 'rna'];
    for (var t = 0; t < types.length; t++) {
        var opt = document.createElement('option');
        opt.value = types[t];
        opt.textContent = types[t].charAt(0).toUpperCase() + types[t].slice(1);
        typeSelect.appendChild(opt);
    }
    typeSelect.addEventListener('change', function() {
        var idx = parseInt(this.closest('.chain-entry').dataset.chainIndex);
        if (multimerChains[idx]) {
            multimerChains[idx].moleculeType = this.value;
        }
    });
    rightControls.appendChild(typeSelect);

    // Remove button
    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'chain-remove-btn';
    removeBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
    removeBtn.dataset.chainIndex = chainIndex;
    removeBtn.addEventListener('click', function() {
        removeChainEntry(parseInt(this.closest('.chain-entry').dataset.chainIndex));
    });
    if (multimerChains.length <= 2) {
        removeBtn.style.display = 'none';
    }
    rightControls.appendChild(removeBtn);

    header.appendChild(labelSpan);
    header.appendChild(rightControls);

    // Sequence Textarea
    var textarea = document.createElement('textarea');
    textarea.className = 'chain-sequence-input';
    textarea.placeholder = 'Enter amino acid sequence...';
    textarea.addEventListener('input', function() {
        var idx = parseInt(this.closest('.chain-entry').dataset.chainIndex);
        if (multimerChains[idx]) {
            multimerChains[idx].sequence = this.value;
        }
    });

    // Structure File Input
    var structDiv = document.createElement('div');
    structDiv.className = 'chain-structure-input hidden';
    structDiv.style.cssText = 'padding: 12px; border: 1px dashed #3f3f46; border-radius: var(--r-ui, 0px); background: #09090b; text-align: center;';
    
    var structFileBtn = document.createElement('button');
    structFileBtn.type = 'button';
    structFileBtn.className = 'add-chain-btn';
    structFileBtn.style.border = 'none';
    structFileBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Upload PDB/CIF';
    
    var structFileInput = document.createElement('input');
    structFileInput.type = 'file';
    structFileInput.accept = '.pdb,.cif';
    structFileInput.className = 'hidden';
    
    var structStatus = document.createElement('div');
    structStatus.style.cssText = 'font-size: 11px; color: #71717a; margin-top: 6px;';
    structStatus.textContent = 'No file selected';
    
    structFileBtn.addEventListener('click', () => structFileInput.click());
    structFileInput.addEventListener('change', function(e) {
        var file = e.target.files[0];
        if (file) {
            var idx = parseInt(this.closest('.chain-entry').dataset.chainIndex);
            var reader = new FileReader();
            reader.onload = (re) => {
                multimerChains[idx].structure = re.target.result;
                structStatus.textContent = 'Selected: ' + file.name;
                structStatus.style.color = 'var(--of3-teal)';
            };
            reader.readAsText(file);
        }
    });

    structDiv.appendChild(structFileBtn);
    structDiv.appendChild(structFileInput);
    structDiv.appendChild(structStatus);

    entry.appendChild(header);
    entry.appendChild(textarea);
    entry.appendChild(structDiv);
    container.appendChild(entry);

    if (multimerChains.length > 2) {
        var allRemoveBtns = container.querySelectorAll('.chain-remove-btn');
        for (var r = 0; r < allRemoveBtns.length; r++) allRemoveBtns[r].style.display = '';
    }
}

function removeChainEntry(index) {
    if (multimerChains.length <= 2) {
        showError('Multimer requires at least 2 chains');
        return;
    }

    multimerChains.splice(index, 1);
    rebuildChainEntries();
}


function rebuildChainEntries() {
    var container = document.getElementById('chains-container');
    while (container.firstChild) container.removeChild(container.firstChild);

    var savedChains = multimerChains.slice();
    multimerChains = [];

    for (var i = 0; i < savedChains.length; i++) {
        addChainEntry();
        // Restore data
        var newId = String.fromCharCode(65 + i);
        multimerChains[i].moleculeType = savedChains[i].moleculeType;
        multimerChains[i].sequence = savedChains[i].sequence;
        multimerChains[i].id = newId;

        // Update DOM
        var entry = container.children[i];
        var textarea = entry.querySelector('.chain-sequence-input');
        if (textarea) textarea.value = savedChains[i].sequence;
        var select = entry.querySelector('.chain-type-select');
        if (select) select.value = savedChains[i].moleculeType;
    }
}

// ============================================================================
// SUBMISSION
// ============================================================================

async function submitPrediction() {
    // Validate ligand input first (shared between modes)
    if (currentLigandType === 'ccd' && !selectedLigandCCD) {
        showError(window.i18n.t('boltz2.selectLigandError'));
        return;
    } else if (currentLigandType === 'smiles') {
        const smiles = document.getElementById('ligand-smiles-input').value.trim();
        if (!smiles) {
            showError(window.i18n.t('boltz2.enterSmilesError'));
            return;
        }
    } else if (currentLigandType === 'database') {
        if (!selectedDbCompound || !selectedDbCompound.smiles) {
            showError(window.i18n.t('boltz2.searchCompoundError'));
            return;
        }
    }

    // Build common config
    const commonConfig = {
        recycling_steps: parseInt(document.getElementById('recycling-steps').value),
        sampling_steps: parseInt(document.getElementById('sampling-steps').value),
        diffusion_samples: parseInt(document.getElementById('diffusion-samples').value),
        step_scale: parseFloat(document.getElementById('step-scale').value),
        cyclic: document.getElementById('cyclic-peptide')?.checked || false,
        without_potentials: document.getElementById('without-potentials')?.checked || false,
        write_full_pae: document.getElementById('write-full-pae')?.checked || false,
        concatenate_msas: document.getElementById('concatenate-msas')?.checked || false
    };

    // Add ligand to config if selected
    if (currentLigandType === 'ccd' && selectedLigandCCD) {
        commonConfig.ligand_ccd = selectedLigandCCD;
        commonConfig.ligand_id = document.getElementById('ligand-id').value.toUpperCase() || 'L';
        commonConfig.ligand_count = parseInt(document.getElementById('ligand-count').value) || 1;
    } else if (currentLigandType === 'smiles') {
        const smiles = document.getElementById('ligand-smiles-input').value.trim();
        if (smiles) {
            commonConfig.ligand_smiles = smiles;
            commonConfig.ligand_id = document.getElementById('ligand-id').value.toUpperCase() || 'L';
            commonConfig.ligand_count = parseInt(document.getElementById('ligand-smiles-count').value) || 1;
        }
    } else if (currentLigandType === 'database' && selectedDbCompound && selectedDbCompound.smiles) {
        commonConfig.ligand_smiles = selectedDbCompound.smiles;
        commonConfig.ligand_id = document.getElementById('ligand-id').value.toUpperCase() || 'L';
        commonConfig.ligand_count = parseInt(document.getElementById('ligand-db-count').value) || 1;
    }

    // Affinity prediction
    if (document.getElementById('predict-affinity')?.checked && currentLigandType !== 'none') {
        commonConfig.predict_affinity = true;
        commonConfig.sampling_steps_affinity = parseInt(document.getElementById('affinity-sampling-steps').value);
        commonConfig.diffusion_samples_affinity = parseInt(document.getElementById('affinity-samples').value);
        commonConfig.affinity_mw_correction = document.getElementById('affinity-mw-correction')?.checked || false;
    }

    // Pocket Constraints
    const pocketResidues = document.getElementById('pocket-residues')?.value?.trim();
    if (pocketResidues && currentLigandType !== 'none') {
        const residues = pocketResidues.split(',').map(r => {
            const parts = r.trim().split(':');
            if (parts.length === 2) {
                return { id: parts[0].trim().toUpperCase(), residue_index: parseInt(parts[1].trim()) };
            }
            return null;
        }).filter(r => r && !isNaN(r.residue_index));

        if (residues.length > 0) {
            commonConfig.constraints = [
                {
                    constraint_type: 'pocket',
                    binder: commonConfig.ligand_id || 'L',
                    contacts: residues
                }
            ];
        }
    }

    if (isBatch) {
        // --- Batch Mode (one multimer job per Structure) ---
        ensureMultimerBatch();
        const projectId = parseInt(sessionStorage.getItem('selectedProject'), 10) || 1;
        const batchPayload = multimerBatch.toPayload(commonConfig, projectId);
        if (!batchPayload) return; // validation failed; onError already fired

        try {
            showLoading('Submitting batch...');
            const response = await fetch('/api/batch/jobs', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
                body: JSON.stringify(batchPayload)
            });
            const result = await response.json();
            hideLoading();
            if (result.status === 'success' || (result.jobs_count && result.jobs_count > 0)) {
                const created = result.jobs_count != null ? result.jobs_count : batchPayload.items.length;
                showSuccess('Batch submitted successfully! Created ' + created + ' job(s).' +
                    (result.errors_count ? ' ' + result.errors_count + ' failed.' : ''));
                setTimeout(() => window.location.href = '/dashboard', 2000);
            } else {
                showError(result.message || 'Batch submission failed');
            }
        } catch (err) {
            hideLoading();
            showError('Failed to submit batch: ' + err.message);
        }
        return;
    }

    // --- Monomer / Multimer Mode (Individual Job) ---
    const payload = { ...commonConfig };

    if (isMultimer) {
        const chains = [];
        for (var i = 0; i < multimerChains.length; i++) {
            var chain = multimerChains[i];
            if (chain.inputType === 'sequence') {
                var seq = parseFASTA(chain.sequence);
                if (!seq) {
                    showError('Chain ' + chain.id + ': Please enter a sequence');
                    return;
                }
                chains.push({ sequence: seq, molecule_type: chain.moleculeType, chain_id: chain.id });
            } else {
                if (!chain.structure) {
                    showError('Chain ' + chain.id + ': Please upload a structure file');
                    return;
                }
                chains.push({ structure: chain.structure, molecule_type: chain.moleculeType, chain_id: chain.id });
            }
        }
        if (chains.length < 2) {
            showError('Multimer requires at least 2 chains');
            return;
        }
        payload.chains = chains;
        payload.molecule_type = chains[0].molecule_type;
    } else {
        const rawSequence = document.getElementById('sequence').value.trim();
        if (!rawSequence) {
            showError('Please enter a sequence');
            return;
        }
        const cleanedSequence = parseFASTA(rawSequence);
        if (!cleanedSequence) {
            showError('No valid sequence found after cleaning');
            return;
        }
        payload.sequence = cleanedSequence;
        payload.molecule_type = currentMoleculeType;
        payload.chain_id = document.getElementById('chain-id').value.toUpperCase() || 'A';
    }

    // Show loading state
    showLoading();

    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 120000); // 120s for complex requests

        const response = await fetch('/api/boltz2/predict', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': getCsrfToken()
            },
            body: JSON.stringify(payload),
            signal: controller.signal
        });
        clearTimeout(timeoutId);

        const result = await response.json();

        if (result.error || result.status === 'error') {
            hideLoading();
            showError(result.message || 'Prediction failed');
            return;
        }

        // Async task - start polling
        if (result.status === 'pending' && result.task_id) {
            await pollPredictionStatus(result.task_id, Date.now());
            return;
        }

        // Immediate result
        _handlePredictionResult(result);

    } catch (error) {
        if (error.name === 'AbortError') {
            showError('Request timed out. The server may be busy — please try again.');
        } else {
            console.error('Prediction error:', error);
            showError('Network error. Please try again.');
        }
        hideLoading();
    }
}

async function pollPredictionStatus(taskId, startTime) {
    const maxWaitMs = 360000; // 6 minutes max polling
    const pollInterval = 3000; // poll every 3 seconds

    const updateLoadingText = (elapsed) => {
        const secs = Math.floor(elapsed / 1000);
        const loadingText = document.querySelector('.loading-text');
        if (loadingText) {
            loadingText.textContent = `Predicting structure... (${secs}s)`;
        }
        const loadingNote = document.querySelector('#loading-panel p');
        if (loadingNote) {
            if (secs < 30) {
                loadingNote.textContent = 'Submitting to Boltz2 — this typically takes 1-5 minutes.';
            } else if (secs < 120) {
                loadingNote.textContent = 'Structure prediction in progress. Please wait...';
            } else {
                loadingNote.textContent = `Still running (${secs}s). Complex structures may take longer.`;
            }
        }
    };

    while (Date.now() - startTime < maxWaitMs) {
        updateLoadingText(Date.now() - startTime);
        await new Promise(resolve => setTimeout(resolve, pollInterval));

        try {
            const statusRes = await fetch(`/api/boltz2/status/${encodeURIComponent(taskId)}`, {
                headers: { 'X-CSRF-Token': getCsrfToken() }
            });
            const statusData = await statusRes.json();

            if (statusData.error) {
                showError(statusData.message || 'Prediction failed');
                return;
            }

            if (statusData.status === 'pending') {
                continue; // Keep polling
            }

            // Completed result
            _handlePredictionResult(statusData);
            return;

        } catch (err) {
            console.warn('Boltz2 status poll error:', err);
            // Continue polling on transient errors
        }
    }

    showError('Prediction timed out after 6 minutes. Please try again or use a shorter sequence.');
}

function _handlePredictionResult(result) {
    // Handle successful prediction
    displayResults(result);

    // Update credits
    if (result.credits_remaining !== undefined) {
        document.getElementById('user-credits').textContent = result.credits_remaining;
    } else {
        // No balance in the response: take the real one from the server (price comes from config)
        if (window.ModelCost) window.ModelCost.refresh();
    }
    if (window.ModelCost && result.credits_remaining !== undefined) window.ModelCost.refresh();
}

function displayResults(result) {
    // Hide loading, show results
    document.getElementById('loading-panel').classList.add('hidden');
    document.getElementById('placeholder-panel').classList.add('hidden');
    document.getElementById('result-panel').classList.remove('hidden');

    // Store structure
    currentStructure = result;

    // Display metrics
    displayMetrics(result);

    // Visualize structure(s) — load primary, then append additional models
    if (result.structures && result.structures.length > 0) {
        visualizeStructure(result.structures[0].structure);

        // Append additional structures (multiple diffusion samples)
        if (result.structures.length > 1 && viewer3D && viewer3D.appendData) {
            for (var si = 1; si < result.structures.length; si++) {
                var extra = result.structures[si];
                if (extra && extra.structure) {
                    try {
                        var fmt = (extra.format === 'mmcif' || extra.format === 'cif') ? 'cif' : 'pdb';
                        viewer3D.appendData(extra.structure, fmt);
                    } catch (appendErr) {
                        console.warn('Could not append structure model ' + si + ':', appendErr);
                    }
                }
            }
        }

        // Show confidence legend after a short delay to ensure the viewer is ready
        setTimeout(function() {
            toggleConfidenceLegend(true);
        }, 500);
    }
}

function displayMetrics(result) {
    const metricsGrid = document.getElementById('metrics-grid');
    // Clear safely
    while (metricsGrid.firstChild) {
        metricsGrid.removeChild(metricsGrid.firstChild);
    }

    const metrics = [];

    // Confidence score
    if (result.confidence_scores && result.confidence_scores.length > 0) {
        const confidence = result.confidence_scores[0];
        // Boltz2 returns scores in 0-1 range, convert to percentage
        const displayValue = confidence > 1.0 ? confidence.toFixed(1) : (confidence * 100).toFixed(1);
        const exactValue = confidence > 1.0 ? confidence.toFixed(6) : (confidence * 100).toFixed(6);
        const displayLabel = confidence > 1.0 ? '' : '%';
        metrics.push({
            label: 'Confidence',
            value: displayValue + displayLabel,
            exact: 'Exact: ' + exactValue + displayLabel + ' (raw: ' + confidence.toFixed(6) + ')',
            class: confidence > 0.7 ? 'good' : confidence > 0.5 ? 'medium' : ''
        });
    }

    // PTM score
    if (result.ptm_scores && result.ptm_scores.length > 0) {
        const ptm = result.ptm_scores[0];
        const displayValue = ptm > 1.0 ? ptm.toFixed(1) : (ptm * 100).toFixed(1);
        const exactValue = ptm > 1.0 ? ptm.toFixed(6) : (ptm * 100).toFixed(6);
        const displayLabel = ptm > 1.0 ? '' : '%';
        metrics.push({
            label: 'PTM Score',
            value: displayValue + displayLabel,
            exact: 'Exact: ' + exactValue + displayLabel + ' (raw: ' + ptm.toFixed(6) + ')',
            class: ptm > 0.8 ? 'good' : ptm > 0.6 ? 'medium' : ''
        });
    }

    // iPTM score (for multi-chain)
    if (result.iptm_scores && result.iptm_scores.length > 0 && result.iptm_scores[0] > 0) {
        const iptm = result.iptm_scores[0];
        const displayValue = iptm > 1.0 ? iptm.toFixed(1) : (iptm * 100).toFixed(1);
        const exactValue = iptm > 1.0 ? iptm.toFixed(6) : (iptm * 100).toFixed(6);
        const displayLabel = iptm > 1.0 ? '' : '%';
        metrics.push({
            label: 'iPTM',
            value: displayValue + displayLabel,
            exact: 'Exact: ' + exactValue + displayLabel + ' (raw: ' + iptm.toFixed(6) + ')',
            class: iptm > 0.8 ? 'good' : iptm > 0.6 ? 'medium' : ''
        });
    }

    // Binding Affinity
    if (result.affinities && Object.keys(result.affinities).length > 0) {
        for (const [ligandId, affinity] of Object.entries(result.affinities)) {
            if (!affinity || typeof affinity !== 'object') continue;
            const subMetrics = [];
            function resolveAffinity(v) {
                return Array.isArray(v) ? v[0] : v;
            }
            function affLabel(v, label) {
                return Array.isArray(v) && v.length > 1 ? label + ' (' + v.length + ' samples)' : label;
            }
            const predVal = resolveAffinity(affinity.affinity_pred_value);
            if (predVal !== undefined) {
                subMetrics.push({ label: affLabel(affinity.affinity_pred_value, 'Pred Value'), value: Number(predVal).toFixed(2) + ' kcal/mol' });
            }
            const pic50 = resolveAffinity(affinity.affinity_pic50);
            if (pic50 !== undefined) {
                subMetrics.push({ label: affLabel(affinity.affinity_pic50, 'pIC50'), value: Number(pic50).toFixed(2) });
            }
            const prob = resolveAffinity(affinity.affinity_probability_binary);
            if (prob !== undefined) {
                subMetrics.push({ label: affLabel(affinity.affinity_probability_binary, 'Prob (binary)'), value: (Number(prob) * 100).toFixed(1) + '%' });
            }
            for (const sm of subMetrics) {
                metrics.push({
                    label: ligandId + ' ' + sm.label,
                    value: sm.value,
                    exact: '',
                    class: ''
                });
            }
        }
    }

    // Render metrics safely
    metrics.forEach(metric => {
        const card = document.createElement('div');
        card.className = 'metric-card';
        if (metric.exact) {
            card.title = metric.exact;
            card.style.cursor = 'help';
        }

        const label = document.createElement('div');
        label.className = 'metric-label';
        label.textContent = metric.label;

        const value = document.createElement('div');
        value.className = 'metric-value ' + metric.class;
        value.textContent = metric.value;

        card.appendChild(label);
        card.appendChild(value);
        metricsGrid.appendChild(card);
    });
}

var plddtChartInstance = null;

function renderConfidenceChart(scores) {
    var panel = document.getElementById('confidence-chart-panel');
    if (!panel || !scores || scores.length === 0) return;

    var normalizedScores = scores.map(function(score) {
        var numeric = Number(score);
        if (!isFinite(numeric) || numeric < 0) return 0;
        if (numeric <= 1.05) return numeric * 100;
        return Math.min(100, numeric);
    });

    panel.classList.remove('hidden');

    // Average label
    var avg = normalizedScores.reduce(function(a, b) { return a + b; }, 0) / normalizedScores.length;
    var avgLabel = document.getElementById('confidence-avg-label');
    if (avgLabel) avgLabel.textContent = 'Avg: ' + avg.toFixed(1);

    // pLDDT color for a score
    function pLDDTColor(s) {
        if (s >= 90) return '#0053D6';
        if (s >= 70) return '#65CBF3';
        if (s >= 50) return '#FFDB13';
        return '#FF7D45';
    }

    // 1. Line Chart
    var ctx = document.getElementById('plddt-chart');
    if (ctx && typeof Chart !== 'undefined') {
        if (plddtChartInstance) {
            plddtChartInstance.destroy();
        }
        plddtChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: normalizedScores.map(function(_, i) { return i + 1; }),
                datasets: [{
                    label: 'pLDDT',
                    data: normalizedScores,
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16, 185, 129, 0.08)',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    pointHoverBackgroundColor: '#10b981',
                    fill: true,
                    tension: 0.1,
                    segment: {
                        borderColor: function(ctx2) {
                            return pLDDTColor(ctx2.p0.parsed.y);
                        }
                    }
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 400 },
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(24, 24, 27, 0.95)',
                        titleColor: '#fafafa',
                        bodyColor: '#fafafa',
                        borderColor: '#3f3f46',
                        borderWidth: 1,
                        callbacks: {
                            title: function(items) { return 'Residue ' + items[0].label; },
                            label: function(item) { return 'pLDDT: ' + item.raw.toFixed(1); }
                        }
                    }
                },
                scales: {
                    y: {
                        min: 0,
                        max: 100,
                        grid: { color: 'rgba(255,255,255,0.05)' },
                        ticks: { color: '#71717a', font: { size: 10 } },
                        title: { display: true, text: 'pLDDT Score', color: '#71717a', font: { size: 11 } }
                    },
                    x: {
                        display: true,
                        grid: { color: 'rgba(255,255,255,0.03)' },
                        ticks: {
                            color: '#71717a',
                            font: { size: 10 },
                            maxTicksLimit: 20,
                            callback: function(val) { return val; }
                        },
                        title: { display: true, text: 'Residue Position', color: '#71717a', font: { size: 11 } }
                    }
                }
            }
        });
    }

    // 2. Heatmap strip
    var heatmap = document.getElementById('plddt-heatmap');
    if (heatmap) {
        heatmap.width = normalizedScores.length;
        var hctx = heatmap.getContext('2d');
        for (var i = 0; i < normalizedScores.length; i++) {
            hctx.fillStyle = pLDDTColor(normalizedScores[i]);
            hctx.fillRect(i, 0, 1, 16);
        }
    }
}

function visualizeStructure(structureData) {
    // Initialize PhonosViewer (Mol*) for structure visualization
    var viewerElement = document.getElementById('structure-viewer');
    if (!viewerElement) return;

    // Destroy previous viewer instance
    if (window._phonosViewer) {
        window._phonosViewer.destroy();
        window._phonosViewer = null;
    }
    // Clear container
    while (viewerElement.firstChild) {
        viewerElement.removeChild(viewerElement.firstChild);
    }

    // Create PhonosViewer with AlphaFold-style confidence coloring
    viewer3D = PhonosViewer.create('structure-viewer', {
        alphafoldView: true,
        hideControls: true,
        sequencePanel: false,
        focusPanel: false
    });
    window._phonosViewer = viewer3D;

    // Load structure data — Mol* handles pLDDT coloring automatically
    viewer3D.loadData(structureData, 'cif', { alphafoldView: true });

    // Ensure ligand/het atoms are visible (combined CIF contains both ATOM and HETATM)
    if (viewer3D.setVisibility) {
        setTimeout(function() {
            try { viewer3D.setVisibility({ polymer: true, het: true, water: false }); } catch (_) {}
        }, 300);
    }

    // Extract confidence scores for the Chart.js chart (separate from viewer coloring)
    var confidenceScores = extractConfidenceScoresFromBoltz2(structureData);
    if (confidenceScores.length > 0) {
        renderConfidenceChart(confidenceScores);
    } else {
        // Fallback: use PhonosViewer's parsed confidence data
        var confData = viewer3D.confidenceData;
        if (confData && confData.scores && confData.scores.length > 0) {
            var fallback = confData.scores.map(function(s) { return s.score; });
            // Normalize if 0-1
            var mx = Math.max.apply(null, fallback);
            if (mx <= 1.05) fallback = fallback.map(function(v) { return v * 100; });
            renderConfidenceChart(fallback);
        }
    }
}

function resetView() {
    if (window._phonosViewer) {
        window._phonosViewer.resetView();
    }
}

function downloadStructure() {
    if (!currentStructure || !currentStructure.structures) {
        return;
    }

    const structure = currentStructure.structures[0].structure;
    const blob = new Blob([structure], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'boltz2_structure_' + Date.now() + '.cif';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function showLoading() {
    document.getElementById('placeholder-panel').classList.add('hidden');
    document.getElementById('result-panel').classList.add('hidden');
    document.getElementById('loading-panel').classList.remove('hidden');
    document.getElementById('submit-btn').disabled = true;
    // Reset chart panel
    var chartPanel = document.getElementById('confidence-chart-panel');
    if (chartPanel) chartPanel.classList.add('hidden');
    if (plddtChartInstance) { plddtChartInstance.destroy(); plddtChartInstance = null; }
}

function hideLoading() {
    document.getElementById('loading-panel').classList.add('hidden');
    document.getElementById('placeholder-panel').classList.remove('hidden');
    document.getElementById('submit-btn').disabled = false;
}

function showSuccess(message) {
    // Display a temporary success message (without changing main panels)
    console.log('Success:', message);
    showNotification(message, 'success');
}

function showError(message) {
    document.getElementById('loading-panel').classList.add('hidden');
    document.getElementById('result-panel').classList.add('hidden');
    document.getElementById('placeholder-panel').classList.remove('hidden');
    document.getElementById('submit-btn').disabled = false;

    // Create error message safely using DOM methods
    const placeholder = document.getElementById('placeholder-panel');
    while (placeholder.firstChild) {
        placeholder.removeChild(placeholder.firstChild);
    }

    const card = document.createElement('div');
    card.className = 'card';

    const cardBody = document.createElement('div');
    cardBody.className = 'card-body';

    const statusMsg = document.createElement('div');
    statusMsg.className = 'status-message error';

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '20');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');

    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', '12');
    circle.setAttribute('cy', '12');
    circle.setAttribute('r', '10');
    svg.appendChild(circle);

    const line1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line1.setAttribute('x1', '12');
    line1.setAttribute('y1', '8');
    line1.setAttribute('x2', '12');
    line1.setAttribute('y2', '12');
    svg.appendChild(line1);

    const line2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line2.setAttribute('x1', '12');
    line2.setAttribute('y1', '16');
    line2.setAttribute('x2', '12.01');
    line2.setAttribute('y2', '16');
    svg.appendChild(line2);

    const span = document.createElement('span');
    span.textContent = message;

    statusMsg.appendChild(svg);
    statusMsg.appendChild(span);
    cardBody.appendChild(statusMsg);
    card.appendChild(cardBody);
    placeholder.appendChild(card);

    // Reset after 5 seconds
    setTimeout(resetPlaceholder, 5000);
}

function resetPlaceholder() {
    const placeholder = document.getElementById('placeholder-panel');
    while (placeholder.firstChild) {
        placeholder.removeChild(placeholder.firstChild);
    }

    const card = document.createElement('div');
    card.className = 'card';

    const cardBody = document.createElement('div');
    cardBody.className = 'card-body';

    const container = document.createElement('div');
    container.className = 'loading-container';

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '64');
    svg.setAttribute('height', '64');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.5');
    svg.style.color = '#3f3f46';

    const path1 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path1.setAttribute('d', 'M12 2L2 7l10 5 10-5-10-5z');
    svg.appendChild(path1);

    const path2 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path2.setAttribute('d', 'M2 17l10 5 10-5M2 12l10 5 10-5');
    svg.appendChild(path2);

    const textDiv = document.createElement('div');
    textDiv.style.textAlign = 'center';

    const h3 = document.createElement('h3');
    h3.style.fontSize = '16px';
    h3.style.color = '#fafafa';
    h3.style.marginBottom = '8px';
    h3.textContent = 'Ready to Predict';

    const p = document.createElement('p');
    p.style.fontSize = '13px';
    p.style.color = '#71717a';
    p.textContent = window.i18n.t('boltz2.predictionStartHint');

    textDiv.appendChild(h3);
    textDiv.appendChild(p);
    container.appendChild(svg);
    container.appendChild(textDiv);
    cardBody.appendChild(container);
    card.appendChild(cardBody);
    placeholder.appendChild(card);
}

function getCsrfToken() {
    // Try to get CSRF token from meta tag or cookie
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) {
        return meta.getAttribute('content');
    }

    // Try cookie
    const cookies = document.cookie.split(';');
    for (let cookie of cookies) {
        const [name, value] = cookie.trim().split('=');
        if (name === 'csrf_token') {
            return value;
        }
    }

    return '';
}

// Pick a project-stored SDF/MOL/MOL2 ligand, convert it to SMILES server-side
// (RDKit), and load it into the SMILES ligand input.
function setupBoltz2ProjectLigandPicker() {
    const projSel = document.getElementById('boltz2-ligand-project');
    const fileSel = document.getElementById('boltz2-ligand-file');
    const statusEl = document.getElementById('boltz2-ligand-convert-status');
    if (!projSel || !fileSel) return;
    const LIG_EXT = /\.(sdf|mol|mol2)$/i;

    const setStatus = (msg, isError) => {
        if (statusEl) {
            statusEl.textContent = msg;
            statusEl.style.color = isError ? '#fca5a5' : '#52525b';
        }
    };

    fetch('/api/projects', { headers: { 'Accept': 'application/json' } })
        .then(r => r.ok ? r.json() : [])
        .then(projects => {
            if (!Array.isArray(projects)) return;
            projects.forEach(p => {
                const o = document.createElement('option');
                o.value = p.id;
                o.textContent = p.name || ('Project ' + p.id);
                projSel.appendChild(o);
            });
        })
        .catch(() => {});

    projSel.addEventListener('change', () => {
        fileSel.innerHTML = '<option value="">Select file…</option>';
        fileSel.disabled = true;
        if (!projSel.value) return;
        setStatus('Loading files…');
        fetch('/api/projects/' + encodeURIComponent(projSel.value) + '/files', { headers: { 'Accept': 'application/json' } })
            .then(r => r.ok ? r.json() : null)
            .then(data => {
                let files = (data && (data.files || data.items || data)) || [];
                if (!Array.isArray(files)) files = [];
                const ligands = files.filter(f => {
                    const nm = f.filename || f.name || '';
                    return LIG_EXT.test(nm) && (f.type === 'upload' || f.type === undefined);
                });
                if (!ligands.length) { setStatus('No SDF/MOL files in this project.'); return; }
                ligands.forEach(f => {
                    const o = document.createElement('option');
                    o.value = f.id;
                    o.textContent = f.filename || f.name;
                    fileSel.appendChild(o);
                });
                fileSel.disabled = false;
                setStatus(ligands.length + ' ligand file(s) found.');
            })
            .catch(() => setStatus('Could not load project files.', true));
    });

    fileSel.addEventListener('change', () => {
        if (!fileSel.value) return;
        setStatus('Converting to SMILES…');
        fetch('/api/ligand/sdf-to-smiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
            body: JSON.stringify({ file_id: parseInt(fileSel.value, 10) })
        })
            .then(r => r.json().then(j => ({ ok: r.ok, body: j })))
            .then(res => {
                if (!res.ok || !res.body.success || !res.body.smiles) {
                    setStatus((res.body && res.body.error) || 'Conversion failed.', true);
                    return;
                }
                const smInput = document.getElementById('ligand-smiles-input');
                if (smInput) smInput.value = res.body.smiles;
                // Ensure SMILES is the active ligand type (selectLigandType clears the
                // input on switch, so set the input AFTER switching).
                if (typeof selectLigandType === 'function' && currentLigandType !== 'smiles') {
                    selectLigandType('smiles');
                    if (smInput) smInput.value = res.body.smiles;
                }
                setStatus('Loaded SMILES from ' + fileSel.options[fileSel.selectedIndex].textContent + '.');
            })
            .catch(() => setStatus('Conversion request failed.', true));
    });
}

// ============================================================================
// DATABASE SEARCH FUNCTIONS
// ============================================================================

function selectDbSource(source) {
    currentDbSource = source;

    // Update tabs
    document.querySelectorAll('.db-source-btn').forEach(btn => {
        if (btn.dataset.source === source) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });

    // Clear results and selection
    selectedDbCompound = null;
    const dbResults = document.getElementById('ligand-db-results');
    if (dbResults) {
        renderDbEmptyState(dbResults, 'Search for compounds to find their SMILES structure');
    }
    const dbSelected = document.getElementById('ligand-db-selected');
    if (dbSelected) dbSelected.classList.add('hidden');
}

function renderDbEmptyState(container, message) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'padding: 40px 20px; text-align: center; color: #71717a; font-size: 13px;';

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '32');
    svg.setAttribute('height', '32');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.5');
    svg.style.cssText = 'margin: 0 auto 12px; display: block; opacity: 0.5;';

    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', '11');
    circle.setAttribute('cy', '11');
    circle.setAttribute('r', '8');
    svg.appendChild(circle);

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M21 21l-4.35-4.35');
    svg.appendChild(path);

    const text = document.createElement('div');
    text.textContent = message;

    wrapper.appendChild(svg);
    wrapper.appendChild(text);
    container.appendChild(wrapper);
}

function renderDbLoadingState(container) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'padding: 40px 20px; text-align: center;';

    const spinner = document.createElement('div');
    spinner.style.cssText = 'width: 32px; height: 32px; border: 3px solid #27272a; border-top-color: var(--accent-primary); border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 12px;';

    const text = document.createElement('div');
    text.style.cssText = 'color: #71717a; font-size: 13px;';
    text.textContent = 'Searching ' + (currentDbSource === 'pubchem' ? 'PubChem' : 'ChEMBL') + '...';

    wrapper.appendChild(spinner);
    wrapper.appendChild(text);
    container.appendChild(wrapper);
}

async function searchDatabase(query) {
    if (!query || query.length < 2) {
        showNotification('Please enter at least 2 characters', 'warning');
        return;
    }

    const dbResults = document.getElementById('ligand-db-results');
    if (!dbResults) return;

    // Show loading
    renderDbLoadingState(dbResults);

    try {
        let results = [];

        if (currentDbSource === 'pubchem') {
            results = await searchPubChem(query);
        } else {
            results = await searchChEMBL(query);
        }

        renderDbResults(results);
    } catch (error) {
        console.error('Database search error:', error);
        renderDbEmptyState(dbResults, 'Search failed. Please try again.');
    }
}

async function searchPubChem(query) {
    // Use PubChem PUG REST API
    const searchUrl = 'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/' + encodeURIComponent(query) + '/property/IUPACName,CanonicalSMILES,MolecularFormula,MolecularWeight/JSON';

    try {
        const response = await fetch(searchUrl);
        if (!response.ok) {
            // Try autocomplete search for partial matches
            const autocompleteUrl = 'https://pubchem.ncbi.nlm.nih.gov/rest/autocomplete/compound/' + encodeURIComponent(query) + '/json?limit=10';
            const autoResponse = await fetch(autocompleteUrl);
            if (autoResponse.ok) {
                const autoData = await autoResponse.json();
                if (autoData.dictionary_terms && autoData.dictionary_terms.compound) {
                    // Get details for top matches
                    const results = [];
                    const terms = autoData.dictionary_terms.compound.slice(0, 5);
                    for (const term of terms) {
                        try {
                            const detailUrl = 'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/' + encodeURIComponent(term) + '/property/IUPACName,CanonicalSMILES,MolecularFormula,MolecularWeight/JSON';
                            const detailRes = await fetch(detailUrl);
                            if (detailRes.ok) {
                                const detailData = await detailRes.json();
                                if (detailData.PropertyTable && detailData.PropertyTable.Properties) {
                                    const prop = detailData.PropertyTable.Properties[0];
                                    results.push({
                                        id: prop.CID,
                                        name: term,
                                        smiles: prop.CanonicalSMILES,
                                        formula: prop.MolecularFormula,
                                        weight: prop.MolecularWeight,
                                        source: 'pubchem'
                                    });
                                }
                            }
                        } catch (e) {
                            console.warn('Failed to get details for:', term);
                        }
                    }
                    return results;
                }
            }
            return [];
        }

        const data = await response.json();
        if (data.PropertyTable && data.PropertyTable.Properties) {
            return data.PropertyTable.Properties.map(prop => ({
                id: prop.CID,
                name: prop.IUPACName || query,
                smiles: prop.CanonicalSMILES,
                formula: prop.MolecularFormula,
                weight: prop.MolecularWeight,
                source: 'pubchem'
            }));
        }
        return [];
    } catch (error) {
        console.error('PubChem search error:', error);
        throw error;
    }
}

async function searchChEMBL(query) {
    // Use ChEMBL API
    const searchUrl = 'https://www.ebi.ac.uk/chembl/api/data/molecule/search?q=' + encodeURIComponent(query) + '&format=json&limit=10';

    try {
        const response = await fetch(searchUrl);
        if (!response.ok) return [];

        const data = await response.json();
        if (data.molecules) {
            return data.molecules.filter(mol => mol.molecule_structures && mol.molecule_structures.canonical_smiles).map(mol => ({
                id: mol.molecule_chembl_id,
                name: mol.pref_name || mol.molecule_chembl_id,
                smiles: mol.molecule_structures.canonical_smiles,
                formula: mol.molecule_properties ? mol.molecule_properties.full_molformula : '',
                weight: mol.molecule_properties ? mol.molecule_properties.full_mwt : '',
                source: 'chembl'
            }));
        }
        return [];
    } catch (error) {
        console.error('ChEMBL search error:', error);
        throw error;
    }
}

function renderDbResults(results) {
    const dbResults = document.getElementById('ligand-db-results');
    if (!dbResults) return;

    // Clear container
    while (dbResults.firstChild) {
        dbResults.removeChild(dbResults.firstChild);
    }

    if (!results || results.length === 0) {
        renderDbEmptyState(dbResults, 'No compounds found. Try a different search term.');
        return;
    }

    results.forEach((compound, index) => {
        const item = document.createElement('div');
        item.className = 'db-result-item';
        item.dataset.index = index;
        item.style.cssText = 'padding: 12px 16px; border-bottom: 1px solid #27272a; cursor: pointer; transition: background 0.15s;' + (index % 2 === 1 ? ' background: rgba(255,255,255,0.02);' : '');

        item.addEventListener('mouseenter', () => { item.style.background = 'rgba(99, 102, 241, 0.1)'; });
        item.addEventListener('mouseleave', () => { item.style.background = index % 2 === 1 ? 'rgba(255,255,255,0.02)' : 'transparent'; });

        const row = document.createElement('div');
        row.style.cssText = 'display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;';

        const info = document.createElement('div');
        info.style.cssText = 'flex: 1; min-width: 0;';

        const nameEl = document.createElement('div');
        nameEl.style.cssText = 'font-size: 13px; font-weight: 600; color: #fafafa; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;';
        nameEl.textContent = compound.name;

        const smilesEl = document.createElement('div');
        smilesEl.style.cssText = 'font-size: 11px; color: #71717a; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;';
        smilesEl.textContent = compound.smiles || '';

        info.appendChild(nameEl);
        info.appendChild(smilesEl);

        if (compound.formula || compound.weight) {
            const metaEl = document.createElement('div');
            metaEl.style.cssText = 'font-size: 11px; color: #52525b; margin-top: 4px;';
            let metaText = '';
            if (compound.formula) metaText += compound.formula;
            if (compound.formula && compound.weight) metaText += ' · ';
            if (compound.weight) metaText += compound.weight + ' g/mol';
            metaEl.textContent = metaText;
            info.appendChild(metaEl);
        }

        const selectBtn = document.createElement('button');
        selectBtn.type = 'button';
        selectBtn.className = 'select-compound-btn';
        selectBtn.dataset.index = index;
        selectBtn.style.cssText = 'padding: 6px 12px; background: var(--accent-primary); border: none; border-radius: var(--r-ui, 0px); color: white; font-size: 11px; font-weight: 500; cursor: pointer; white-space: nowrap;';
        selectBtn.textContent = 'Select';

        selectBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            selectDbCompound(compound);
        });

        row.appendChild(info);
        row.appendChild(selectBtn);
        item.appendChild(row);

        item.addEventListener('click', () => {
            selectDbCompound(compound);
        });

        dbResults.appendChild(item);
    });
}

function selectDbCompound(compound) {
    selectedDbCompound = compound;

    const dbSelected = document.getElementById('ligand-db-selected');
    const nameEl = document.getElementById('db-selected-name');
    const smilesEl = document.getElementById('db-selected-smiles');

    if (dbSelected && nameEl && smilesEl) {
        nameEl.textContent = compound.name;
        smilesEl.textContent = compound.smiles;
        dbSelected.classList.remove('hidden');
    }

    showNotification('Selected: ' + compound.name, 'success');
}

// ============================================================================
// PER-RESIDUE CONFIDENCE FUNCTIONS
// ============================================================================

function extractConfidenceScoresFromBoltz2(structureData) {
    const scores = [];
    const lines = structureData.split('\n');

    // Step 1: Parse mmCIF _atom_site column headers to get field positions
    const colNames = [];
    let dataStartIdx = -1;

    for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (trimmed === 'loop_') {
            // Check if next line starts an _atom_site block
            const next = (lines[i + 1] || '').trim();
            if (next.startsWith('_atom_site.')) {
                // Collect all column names
                let j = i + 1;
                while (j < lines.length && lines[j].trim().startsWith('_atom_site.')) {
                    colNames.push(lines[j].trim());
                    j++;
                }
                dataStartIdx = j;
                break;
            }
        }
    }

    if (colNames.length === 0) {
        console.log('[Boltz2] No _atom_site loop header found in mmCIF');
        return scores;
    }

    // Build column index map
    const col = {};
    colNames.forEach((name, idx) => { col[name] = idx; });

    const atomIdCol = col['_atom_site.label_atom_id'];
    const compIdCol = col['_atom_site.label_comp_id'];
    const chainCol = col['_atom_site.label_asym_id'] !== undefined
        ? col['_atom_site.label_asym_id']
        : col['_atom_site.auth_asym_id'];
    const seqIdCol = col['_atom_site.label_seq_id'] !== undefined
        ? col['_atom_site.label_seq_id']
        : col['_atom_site.auth_seq_id'];
    const bFactorCol = col['_atom_site.B_iso_or_equiv'];
    const groupCol = col['_atom_site.group_PDB'];

    console.log('[Boltz2] mmCIF columns:', colNames.length,
        '| atomId:', atomIdCol, '| compId:', compIdCol,
        '| chain:', chainCol, '| seqId:', seqIdCol, '| bFactor:', bFactorCol);

    if (bFactorCol === undefined) {
        console.log('[Boltz2] B_iso_or_equiv column not found. Available:', colNames.join(', '));
        return scores;
    }

    // Step 2: Parse ATOM data lines using column positions
    const knownProtein = new Set(['ALA','ARG','ASN','ASP','CYS','GLN','GLU','GLY','HIS','ILE',
                                  'LEU','LYS','MET','PHE','PRO','SER','THR','TRP','TYR','VAL']);
    const knownNucleic = new Set(['A','U','G','C','DA','DU','DG','DC','ADE','THY','GUA','CYT','DT','DGUA']);

    let currentResId = '';
    let currentChain = '';

    for (let i = dataStartIdx; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        // Stop at end of atom data (new loop_, data_, or # block)
        if (trimmed === '' || trimmed.startsWith('loop_') || trimmed.startsWith('data_') || trimmed.startsWith('#')) {
            if (scores.length > 0) break;
            continue;
        }

        const parts = trimmed.split(/\s+/);
        if (parts.length < colNames.length) continue;

        // Filter to ATOM records only (skip HETATM for ligands)
        if (groupCol !== undefined && parts[groupCol] !== 'ATOM') continue;

        const atomName = atomIdCol !== undefined ? parts[atomIdCol] : '';
        const compId = compIdCol !== undefined ? parts[compIdCol] : '';
        const chain = chainCol !== undefined ? parts[chainCol] : '';
        const seqId = seqIdCol !== undefined ? parts[seqIdCol] : '';
        const bFactor = bFactorCol !== undefined ? parseFloat(parts[bFactorCol]) : NaN;

        const isProtein = knownProtein.has(compId);
        const isNucleic = knownNucleic.has(compId);
        if (!isProtein && !isNucleic) continue;

        // Use CA for protein, P for nucleic acid as representative atom
        const targetAtom = isProtein ? 'CA' : 'P';
        if (atomName !== targetAtom) continue;

        // One score per unique residue+chain
        const resKey = chain + '_' + seqId;
        if (resKey === currentResId + '_' + currentChain) continue;
        if (isNaN(bFactor)) continue;

        scores.push(bFactor);
        currentResId = seqId;
        currentChain = chain;
    }

    if (scores.length > 0) {
        console.log('[Boltz2] Extracted ' + scores.length + ' per-residue confidence scores');
        console.log('[Boltz2] Score range: ' + Math.min(...scores).toFixed(1) + ' - ' + Math.max(...scores).toFixed(1));
    } else {
        console.log('[Boltz2] No confidence scores extracted');
        // Debug: show first few data lines
        const sampleLines = lines.slice(dataStartIdx, dataStartIdx + 3);
        console.log('[Boltz2] Sample data lines:', sampleLines);
        console.log('[Boltz2] Column count:', colNames.length, '| Columns:', colNames.slice(0, 5).join(', '), '...');
    }

    // Normalize to 0-100 range (pLDDT scale)
    if (scores.length > 0) {
        const maxScore = Math.max(...scores);
        if (maxScore <= 1.05) {
            return scores.map(s => s * 100);
        }
    }

    return scores;
}

function applyBoltz2ConfidenceColoring(confidenceScores) {
    // PhonosViewer (Mol*) handles confidence coloring natively via alphafoldView
    if (window._phonosViewer) {
        window._phonosViewer.applyConfidenceColoring();
        console.log('[Boltz2] Applied pLDDT confidence coloring via Mol* (' + confidenceScores.length + ' residues)');
    }
}

// Add function to show/hide confidence legend
function toggleConfidenceLegend(show) {
    const legend = document.getElementById('confidence-legend');
    
    if (!legend) {
        createConfidenceLegend();
        return;
    }
    
    // If show is undefined, toggle current state
    if (show === undefined) {
        const currentState = legend.style.display !== 'none';
        legend.style.display = currentState ? 'none' : 'block';
    } else {
        legend.style.display = show ? 'block' : 'none';
    }
}

function createConfidenceLegend() {
    const viewerContainer = document.getElementById('structure-viewer');
    if (!viewerContainer) return;

    const legend = document.createElement('div');
    legend.id = 'confidence-legend';
    legend.style.cssText = `
        position: absolute;
        bottom: 16px;
        left: 16px;
        background: rgba(24, 24, 27, 0.95);
        border: 1px solid #3f3f46;
        border-radius: var(--r-ui, 0px);
        padding: 12px;
        font-size: 12px;
        color: #fafafa;
        backdrop-filter: blur(10px);
        z-index: 10;
    `;

    legend.innerHTML = `
        <div style="font-weight: 600; margin-bottom: 8px; color: #a1a1aa;">Per-Residue Confidence (pLDDT)</div>
        <div style="display: flex; align-items: center; margin-bottom: 4px;">
            <div style="width: 20px; height: 12px; background: #0053D6; border-radius: var(--r-ui, 0px); margin-right: 8px;"></div>
            <span>Very High (≥90)</span>
        </div>
        <div style="display: flex; align-items: center; margin-bottom: 4px;">
            <div style="width: 20px; height: 12px; background: #65CBF3; border-radius: var(--r-ui, 0px); margin-right: 8px;"></div>
            <span>High (70-89)</span>
        </div>
        <div style="display: flex; align-items: center; margin-bottom: 4px;">
            <div style="width: 20px; height: 12px; background: #FFDB13; border-radius: var(--r-ui, 0px); margin-right: 8px;"></div>
            <span>Low (50-69)</span>
        </div>
        <div style="display: flex; align-items: center;">
            <div style="width: 20px; height: 12px; background: #FF7D45; border-radius: var(--r-ui, 0px); margin-right: 8px;"></div>
            <span>Very Low (<50)</span>
        </div>
    `;

    viewerContainer.appendChild(legend);
}
