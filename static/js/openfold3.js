/**
 * OpenFold3 Structure Prediction UI
 * Handles user interactions, API calls, and 3D visualization
 */

// State
let currentMoleculeType = 'protein';
let currentLigandType = 'none';
let selectedLigandCCD = '';
let currentOutputFormat = 'pdb';
let viewer3D = null;
let currentStructure = null;
let currentColorMode = 'confidence'; // 'confidence' or 'chain'
let isMultimer = false;
let isBatch = false;
let multimerChains = [];
let multimerBatch = null; // MultimerBatch controller (lazy-initialised when batch mode is first opened)

var CHAIN_COLORS = ['#0d9488', '#8b5cf6', '#f59e0b', '#ef4444', '#3b82f6', '#ec4899',
                     '#06b6d4', '#f97316', '#84cc16', '#a855f7', '#14b8a6', '#e11d48'];

// Common ligands
const commonLigands = [
    { ccd: 'ATP', name: 'ATP', category: 'nucleotides' },
    { ccd: 'ADP', name: 'ADP', category: 'nucleotides' },
    { ccd: 'GTP', name: 'GTP', category: 'nucleotides' },
    { ccd: 'GDP', name: 'GDP', category: 'nucleotides' },
    { ccd: 'NAD', name: 'NAD+', category: 'cofactors' },
    { ccd: 'FAD', name: 'FAD', category: 'cofactors' },
    { ccd: 'HEM', name: 'Heme', category: 'cofactors' },
    { ccd: 'COA', name: 'CoA', category: 'cofactors' },
    { ccd: 'SAM', name: 'SAM', category: 'cofactors' },
    { ccd: 'PLP', name: 'PLP', category: 'cofactors' },
    { ccd: 'ZN', name: 'Zinc', category: 'metal-ions' },
    { ccd: 'MG', name: 'Magnesium', category: 'metal-ions' },
    { ccd: 'CA', name: 'Calcium', category: 'metal-ions' },
    { ccd: 'FE', name: 'Iron', category: 'metal-ions' },
    { ccd: 'MN', name: 'Manganese', category: 'metal-ions' },
];

// Example sequences
const examples = {
    insulin: {
        type: 'protein',
        sequence: 'GIVEQCCTSICSLYQLENYCN',
        ligand: 'none'
    },
    ubiquitin: {
        type: 'protein',
        sequence: 'MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG',
        ligand: 'none'
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
    var lines = text.trim().split('\n');
    var sequence = '';
    for (var i = 0; i < lines.length; i++) {
        var trimmedLine = lines[i].trim();
        if (!trimmedLine || trimmedLine.startsWith('>') || trimmedLine.startsWith(';') || trimmedLine.startsWith('#')) {
            continue;
        }
        var cleanedLine = trimmedLine.replace(/[^A-Za-z]/g, '');
        if (cleanedLine) {
            sequence += cleanedLine.toUpperCase();
        }
    }
    return sequence;
}

function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute('content');
    var cookies = document.cookie.split(';');
    for (var i = 0; i < cookies.length; i++) {
        var parts = cookies[i].trim().split('=');
        if (parts[0] === 'csrf_token') return decodeURIComponent(parts[1]);
    }
    return '';
}

// Lets the user pick a project-stored SDF/MOL/MOL2 ligand file; converts it to a
// SMILES string server-side (RDKit) and feeds it into the SMILES ligand input.
function setupProjectLigandPicker() {
    var projSel = document.getElementById('of3-ligand-project');
    var fileSel = document.getElementById('of3-ligand-file');
    var statusEl = document.getElementById('of3-ligand-convert-status');
    if (!projSel || !fileSel) return;
    var LIG_EXT = /\.(sdf|mol|mol2)$/i;

    function setStatus(msg, isError) {
        if (statusEl) {
            statusEl.textContent = msg;
            statusEl.style.color = isError ? '#fca5a5' : '#52525b';
        }
    }

    // Populate projects once.
    fetch('/api/projects', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (projects) {
            if (!Array.isArray(projects)) return;
            projects.forEach(function (p) {
                var o = document.createElement('option');
                o.value = p.id;
                o.textContent = p.name || (window.i18n ? window.i18n.t('project_prefix', {id: p.id}) : 'Project ' + p.id);
                projSel.appendChild(o);
            });
        })
        .catch(function () {});

    projSel.addEventListener('change', function () {
        fileSel.innerHTML = '<option value="">' + (window.i18n ? window.i18n.t('select_file') : 'Select file…') + '</option>';
        fileSel.disabled = true;
        if (!projSel.value) return;
        setStatus(window.i18n ? window.i18n.t('loading_files') : 'Loading files…');
        fetch('/api/projects/' + encodeURIComponent(projSel.value) + '/files', { headers: { 'Accept': 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                var files = (data && (data.files || data.items || data)) || [];
                if (!Array.isArray(files)) files = [];
                var ligands = files.filter(function (f) {
                    var nm = f.filename || f.name || '';
                    return LIG_EXT.test(nm) && (f.type === 'upload' || f.type === undefined);
                });
                if (!ligands.length) {
                    setStatus(window.i18n ? window.i18n.t('no_sdf_mol_files') : 'No SDF/MOL files in this project.');
                    return;
                }
                ligands.forEach(function (f) {
                    var o = document.createElement('option');
                    o.value = f.id;
                    o.textContent = f.filename || f.name;
                    fileSel.appendChild(o);
                });
                fileSel.disabled = false;
                setStatus(window.i18n ? window.i18n.t('ligand_files_found', {count: ligands.length}) : ligands.length + ' ligand file(s) found.');
            })
            .catch(function () { setStatus(window.i18n ? window.i18n.t('could_not_load_files') : 'Could not load project files.', true); });
    });

    fileSel.addEventListener('change', function () {
        if (!fileSel.value) return;
        setStatus(window.i18n ? window.i18n.t('converting_to_smiles') : 'Converting to SMILES…');
        fetch('/api/ligand/sdf-to-smiles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
            body: JSON.stringify({ file_id: parseInt(fileSel.value, 10) })
        })
            .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
            .then(function (res) {
                if (!res.ok || !res.body.success || !res.body.smiles) {
                    setStatus((res.body && res.body.error) || (window.i18n ? window.i18n.t('conversion_failed') : 'Conversion failed.'), true);
                    return;
                }
                var smInput = document.getElementById('ligand-smiles-input');
                if (smInput) smInput.value = res.body.smiles;
                // Make sure the SMILES ligand type is the active one so submit keeps it.
                currentLigandType = 'smiles';
                var smBtn = document.querySelector('.ligand-btn[data-ligand="smiles"]');
                if (smBtn && !smBtn.classList.contains('active')) smBtn.click();
                setStatus(window.i18n ? window.i18n.t('loaded_smiles_from', {name: fileSel.options[fileSel.selectedIndex].textContent}) : 'Loaded SMILES from ' + fileSel.options[fileSel.selectedIndex].textContent + '.');
            })
            .catch(function () { setStatus(window.i18n ? window.i18n.t('conversion_request_failed') : 'Conversion request failed.', true); });
    });
}

function showError(message) {
    showToast(message, 'error');
}

function showSuccess(message) {
    showToast(message, 'success');
}

function showToast(message, type) {
    // Remove existing toasts
    var existing = document.querySelectorAll('.toast');
    for (var i = 0; i < existing.length; i++) {
        existing[i].remove();
    }

    var toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(function() {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(function() { toast.remove(); }, 300);
    }, 5000);
}

function showLoading() {
    document.getElementById('placeholder-panel').classList.add('hidden');
    document.getElementById('result-panel').classList.add('hidden');
    document.getElementById('loading-panel').classList.remove('hidden');

    var submitBtn = document.getElementById('submit-btn');
    submitBtn.disabled = true;
    submitBtn.textContent = window.i18n ? window.i18n.t('predicting') : 'Predicting...';
}

function hideLoading() {
    document.getElementById('loading-panel').classList.add('hidden');

    var submitBtn = document.getElementById('submit-btn');
    submitBtn.disabled = false;
    // Clear and rebuild button content safely
    while (submitBtn.firstChild) submitBtn.removeChild(submitBtn.firstChild);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '20');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    var path1 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path1.setAttribute('d', 'M12 2L2 7l10 5 10-5-10-5z');
    var path2 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path2.setAttribute('d', 'M2 17l10 5 10-5M2 12l10 5 10-5');
    svg.appendChild(path1);
    svg.appendChild(path2);
    submitBtn.appendChild(svg);
    submitBtn.appendChild(document.createTextNode(window.i18n ? window.i18n.t('predict_structure') : ' Predict Structure'));
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
            var batchInput = document.getElementById('batch-input');
            if (batchInput) batchInput.classList.toggle('hidden', !isBatch);

            if (isMultimer && multimerChains.length === 0) {
                addChainEntry();
                addChainEntry();
            }

            if (isBatch) {
                ensureMultimerBatch();
                if (multimerBatch.count() === 0) {
                    multimerBatch.addStructure();
                }
            }
        });
    }

    var addBtn = document.getElementById('add-chain-btn');
    if (addBtn) {
        addBtn.addEventListener('click', function() {
            if (multimerChains.length >= 12) {
                showError(window.i18n ? window.i18n.t('max_chains') : 'Maximum 12 chains allowed');
                return;
            }
            addChainEntry();
        });
    }

    // Batch controls
    var addBatchBtn = document.getElementById('add-batch-item-btn');
    if (addBatchBtn) {
        addBatchBtn.addEventListener('click', function() {
            ensureMultimerBatch();
            multimerBatch.addStructure();
        });
    }

    var uploadBatchBtn = document.getElementById('upload-batch-btn');
    var batchFileInput = document.getElementById('batch-file-input');
    if (uploadBatchBtn && batchFileInput) {
        uploadBatchBtn.addEventListener('click', function() { batchFileInput.click(); });
        batchFileInput.addEventListener('change', function(e) {
            ensureMultimerBatch();
            multimerBatch.loadFile(e.target.files[0]);
            e.target.value = '';
        });
    }
}

function ensureMultimerBatch() {
    if (multimerBatch) return multimerBatch;
    multimerBatch = new MultimerBatch({
        model: 'openfold3',
        batchName: 'OpenFold3 Batch',
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
    var chainId = String.fromCharCode(65 + chainIndex);
    var color = CHAIN_COLORS[chainIndex % CHAIN_COLORS.length];

    var chainData = { id: chainId, moleculeType: 'protein', sequence: '' };
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

    var labelText = document.createTextNode(window.i18n ? window.i18n.t('chain_label', {id: chainId}) : ' Chain ' + chainId);
    labelSpan.appendChild(badge);
    labelSpan.appendChild(labelText);

    var rightControls = document.createElement('div');
    rightControls.style.cssText = 'display: flex; align-items: center; gap: 8px;';

    var typeSelect = document.createElement('select');
    typeSelect.className = 'chain-type-select';
    typeSelect.dataset.chainIndex = chainIndex;
    var types = ['protein', 'dna', 'rna'];
    for (var t = 0; t < types.length; t++) {
        var opt = document.createElement('option');
        opt.value = types[t];
        opt.textContent = window.i18n ? window.i18n.t('chain_type_' + types[t]) : types[t].charAt(0).toUpperCase() + types[t].slice(1);
        typeSelect.appendChild(opt);
    }
    typeSelect.addEventListener('change', function() {
        var idx = parseInt(this.dataset.chainIndex);
        if (multimerChains[idx]) {
            multimerChains[idx].moleculeType = this.value;
        }
    });
    rightControls.appendChild(typeSelect);

    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'chain-remove-btn';
    removeBtn.textContent = window.i18n ? window.i18n.t('remove') : 'Remove';
    removeBtn.dataset.chainIndex = chainIndex;
    removeBtn.addEventListener('click', function() {
        removeChainEntry(parseInt(this.dataset.chainIndex));
    });
    if (multimerChains.length <= 2) {
        removeBtn.style.display = 'none';
    }
    rightControls.appendChild(removeBtn);

    header.appendChild(labelSpan);
    header.appendChild(rightControls);

    var textarea = document.createElement('textarea');
    textarea.className = 'chain-sequence-input';
    textarea.dataset.chainIndex = chainIndex;
    textarea.placeholder = window.i18n ? (chainIndex === 0 ? window.i18n.t('chain_placeholder_first') : window.i18n.t('chain_placeholder_additional')) : 'Enter ' + (chainIndex === 0 ? 'first' : 'additional') + ' chain sequence...';
    textarea.addEventListener('input', function() {
        var idx = parseInt(this.dataset.chainIndex);
        if (multimerChains[idx]) {
            multimerChains[idx].sequence = this.value;
        }
    });

    entry.appendChild(header);
    entry.appendChild(textarea);
    container.appendChild(entry);

    // Show all remove buttons when 3+ chains
    if (multimerChains.length > 2) {
        var allRemoveBtns = container.querySelectorAll('.chain-remove-btn');
        for (var r = 0; r < allRemoveBtns.length; r++) {
            allRemoveBtns[r].style.display = '';
        }
    }
}

function removeChainEntry(index) {
    if (multimerChains.length <= 2) {
        showError(window.i18n ? window.i18n.t('multimer_min_chains') : 'Multimer requires at least 2 chains');
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
        var newId = String.fromCharCode(65 + i);
        multimerChains[i].moleculeType = savedChains[i].moleculeType;
        multimerChains[i].sequence = savedChains[i].sequence;
        multimerChains[i].id = newId;

        var entry = container.children[i];
        var textarea = entry.querySelector('.chain-sequence-input');
        if (textarea) textarea.value = savedChains[i].sequence;
        var select = entry.querySelector('.chain-type-select');
        if (select) select.value = savedChains[i].moleculeType;
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', function() {
    initializeMultimerToggle();

    // Molecule type tabs
    var molTabs = document.querySelectorAll('.molecule-tab');
    for (var i = 0; i < molTabs.length; i++) {
        molTabs[i].addEventListener('click', function() {
            var allTabs = document.querySelectorAll('.molecule-tab');
            for (var j = 0; j < allTabs.length; j++) allTabs[j].classList.remove('active');
            this.classList.add('active');
            currentMoleculeType = this.dataset.type;

            var seq = document.getElementById('sequence');
            if (currentMoleculeType === 'protein') {
                seq.placeholder = window.i18n ? window.i18n.t('protein_placeholder') : 'Enter protein sequence (amino acids) or paste FASTA format...\n\nExample: GIVEQCCTSICSLYQLENYCN';
            } else if (currentMoleculeType === 'dna') {
                seq.placeholder = window.i18n ? window.i18n.t('dna_placeholder') : 'Enter DNA sequence (A, C, G, T)...\n\nExample: ATCGATCGATCG';
            } else {
                seq.placeholder = window.i18n ? window.i18n.t('rna_placeholder') : 'Enter RNA sequence (A, C, G, U)...\n\nExample: AUCGAUCGAUCG';
            }

            var msaSection = document.getElementById('msa-section');
            if (msaSection) {
                msaSection.style.display = currentMoleculeType === 'protein' ? 'block' : 'none';
            }
        });
    }

    // Ligand toggle
    var ligandBtns = document.querySelectorAll('.ligand-btn');
    for (var i = 0; i < ligandBtns.length; i++) {
        ligandBtns[i].addEventListener('click', function() {
            var allBtns = document.querySelectorAll('.ligand-btn');
            for (var j = 0; j < allBtns.length; j++) allBtns[j].classList.remove('active');
            this.classList.add('active');
            currentLigandType = this.dataset.ligand;

            document.getElementById('ligand-ccd-panel').classList.toggle('hidden', currentLigandType !== 'ccd');
            document.getElementById('ligand-smiles-panel').classList.toggle('hidden', currentLigandType !== 'smiles');
            document.getElementById('ligand-id-group').classList.toggle('hidden', currentLigandType === 'none');
        });
    }

    // Output format toggle
    var formatBtns = document.querySelectorAll('.format-btn');
    for (var i = 0; i < formatBtns.length; i++) {
        formatBtns[i].addEventListener('click', function() {
            var allBtns = document.querySelectorAll('.format-btn');
            for (var j = 0; j < allBtns.length; j++) allBtns[j].classList.remove('active');
            this.classList.add('active');
            currentOutputFormat = this.dataset.format;
        });
    }

    // Populate ligands grid
    var ligandsGrid = document.getElementById('ligands-grid');
    if (ligandsGrid) {
        for (var i = 0; i < commonLigands.length; i++) {
            (function(ligand) {
                var chip = document.createElement('div');
                chip.className = 'ligand-chip';
                chip.dataset.ccd = ligand.ccd;
                chip.textContent = ligand.ccd;
                chip.title = ligand.name;
                chip.addEventListener('click', function() {
                    var allChips = document.querySelectorAll('.ligand-chip');
                    for (var j = 0; j < allChips.length; j++) allChips[j].classList.remove('selected');
                    chip.classList.add('selected');
                    selectedLigandCCD = ligand.ccd;
                    var customCcd = document.getElementById('custom-ccd');
                    if (customCcd) customCcd.value = '';
                });
                ligandsGrid.appendChild(chip);
            })(commonLigands[i]);
        }
    }

    // Custom CCD input
    var customCcd = document.getElementById('custom-ccd');
    if (customCcd) {
        customCcd.addEventListener('input', function() {
            if (customCcd.value.trim()) {
                var allChips = document.querySelectorAll('.ligand-chip');
                for (var j = 0; j < allChips.length; j++) allChips[j].classList.remove('selected');
                selectedLigandCCD = customCcd.value.trim().toUpperCase();
            }
        });
    }

    // SMILES example buttons
    var smilesExBtns = document.querySelectorAll('.smiles-example-btn');
    for (var i = 0; i < smilesExBtns.length; i++) {
        smilesExBtns[i].addEventListener('click', function() {
            var smilesInput = document.getElementById('ligand-smiles-input');
            if (smilesInput) {
                smilesInput.value = this.dataset.smiles;
            }
        });
    }

    // Project SDF/MOL -> SMILES ligand picker
    setupProjectLigandPicker();

    // Sequence length counter
    var sequenceInput = document.getElementById('sequence');
    if (sequenceInput) {
        sequenceInput.addEventListener('input', function() {
            var cleaned = parseFASTA(sequenceInput.value);
            var label = document.getElementById('seq-length-label');
            if (label) {
                label.textContent = window.i18n ? window.i18n.t('sequence_length', {length: cleaned.length}) : cleaned.length + ' / 1000';
                label.style.color = cleaned.length > 1000 ? '#ef4444' : '#52525b';
            }
        });
    }

    // Example cards
    var exampleCards = document.querySelectorAll('.example-card');
    for (var i = 0; i < exampleCards.length; i++) {
        exampleCards[i].addEventListener('click', function() {
            var example = examples[this.dataset.example];
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

                var allTabs = document.querySelectorAll('.molecule-tab');
                for (var j = 0; j < allTabs.length; j++) allTabs[j].classList.remove('active');
                var targetTab = document.querySelector('.molecule-tab[data-type="' + example.type + '"]');
                if (targetTab) {
                    targetTab.classList.add('active');
                    currentMoleculeType = example.type;
                }

                document.getElementById('sequence').value = example.sequence;
                document.getElementById('sequence').dispatchEvent(new Event('input'));
            }

            var allLigBtns = document.querySelectorAll('.ligand-btn');
            for (var j = 0; j < allLigBtns.length; j++) allLigBtns[j].classList.remove('active');
            document.querySelector('.ligand-btn[data-ligand="none"]').classList.add('active');
            currentLigandType = 'none';
            document.getElementById('ligand-ccd-panel').classList.add('hidden');
            document.getElementById('ligand-smiles-panel').classList.add('hidden');

            showSuccess(window.i18n ? window.i18n.t('loaded_example', {name: this.querySelector('.example-title').textContent}) : 'Loaded: ' + this.querySelector('.example-title').textContent);
        });
    }
});

// Submit Prediction
async function submitPrediction() {
    // Validate ligand
    if (currentLigandType === 'ccd' && !selectedLigandCCD) {
        showError(window.i18n ? window.i18n.t('select_ligand_or_none') : 'Please select a ligand or choose "None"');
        return;
    }
    if (currentLigandType === 'smiles') {
        var smilesVal = document.getElementById('ligand-smiles-input').value.trim();
        if (!smilesVal) {
            showError(window.i18n ? window.i18n.t('enter_smiles_or_none') : 'Please enter a SMILES string or choose "None"');
            return;
        }
    }

    // --- Batch mode: submit many multimer structures as a batch of jobs ---
    if (isBatch) {
        await submitBatchPrediction();
        return;
    }

    // Build payload based on mode
    var payload = {
        output_format: currentOutputFormat
    };

    if (isMultimer) {
        // --- Multimer mode ---
        var chains = [];
        for (var i = 0; i < multimerChains.length; i++) {
            var chain = multimerChains[i];
            var seq = parseFASTA(chain.sequence);
            if (!seq) {
                showError(window.i18n ? window.i18n.t('chain_enter_sequence', {id: chain.id}) : 'Chain ' + chain.id + ': Please enter a sequence');
                return;
            }
            if (seq.length > 1000) {
                showError(window.i18n ? window.i18n.t('chain_sequence_too_long', {id: chain.id, length: seq.length}) : 'Chain ' + chain.id + ': Sequence too long (' + seq.length + '). Max 1000 residues.');
                return;
            }
            chains.push({
                sequence: seq,
                molecule_type: chain.moleculeType,
                chain_id: chain.id
            });
        }
        if (chains.length < 2) {
            showError(window.i18n ? window.i18n.t('multimer_min_chains') : 'Multimer requires at least 2 chains');
            return;
        }
        payload.chains = chains;
        payload.molecule_type = chains[0].molecule_type;
    } else {
        // --- Monomer mode ---
        var rawSequence = document.getElementById('sequence').value.trim();
        if (!rawSequence) {
            showError(window.i18n ? window.i18n.t('enter_sequence') : 'Please enter a sequence');
            return;
        }
        var cleanedSequence = parseFASTA(rawSequence);
        if (!cleanedSequence) {
            showError(window.i18n ? window.i18n.t('no_valid_sequence') : 'No valid sequence found after cleaning');
            return;
        }
        if (cleanedSequence.length > 1000) {
            showError(window.i18n ? window.i18n.t('sequence_too_long', {length: cleanedSequence.length}) : 'Sequence too long (' + cleanedSequence.length + '). OpenFold3 supports up to 1000 residues.');
            return;
        }
        payload.sequence = cleanedSequence;
        payload.molecule_type = currentMoleculeType;
        payload.chain_id = document.getElementById('chain-id').value.toUpperCase() || 'A';
    }

    // Ligand
    // Safety net: typing/pasting a SMILES or CCD does not by itself flip the
    // ligand-type toggle, so a pasted ligand was being silently dropped at submit
    // ("ligand gets lost"). Infer the type from a filled, visible ligand field.
    var smilesPanelEl = document.getElementById('ligand-smiles-panel');
    var ccdPanelEl = document.getElementById('ligand-ccd-panel');
    var smilesFieldEl = document.getElementById('ligand-smiles-input');
    var ccdFieldEl = document.getElementById('custom-ccd');
    var smilesFilled = smilesFieldEl && smilesFieldEl.value.trim() &&
        smilesPanelEl && !smilesPanelEl.classList.contains('hidden');
    var ccdFilled = ((ccdFieldEl && ccdFieldEl.value.trim()) || selectedLigandCCD) &&
        ccdPanelEl && !ccdPanelEl.classList.contains('hidden');
    if (currentLigandType !== 'smiles' && currentLigandType !== 'ccd') {
        if (smilesFilled) currentLigandType = 'smiles';
        else if (ccdFilled) currentLigandType = 'ccd';
    }

    if (currentLigandType === 'ccd') {
        var ccdVal = selectedLigandCCD || (ccdFieldEl ? ccdFieldEl.value.trim().toUpperCase() : '');
        if (ccdVal) {
            payload.ligand_ccd = ccdVal;
            payload.ligand_id = document.getElementById('ligand-id').value.toUpperCase() || 'L';
        }
    } else if (currentLigandType === 'smiles') {
        var smiles = smilesFieldEl ? smilesFieldEl.value.trim() : '';
        if (smiles) {
            payload.ligand_smiles = smiles;
            payload.ligand_id = document.getElementById('ligand-id').value.toUpperCase() || 'L';
        }
    }

    // MSA (monomer protein only)
    if (!isMultimer) {
        var msaInput = document.getElementById('msa-csv');
        if (msaInput && msaInput.value.trim() && currentMoleculeType === 'protein') {
            payload.msa_input = msaInput.value.trim();
            payload.msa_format = 'csv';
        }
    }

    showLoading();

    try {
        var controller = new AbortController();
        var timeoutId = setTimeout(function() { controller.abort(); }, 90000);

        var response = await fetch('/api/openfold3/predict', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': getCsrfToken()
            },
            body: JSON.stringify(payload),
            signal: controller.signal
        });
        clearTimeout(timeoutId);

        var result = await response.json();

        if (result.error) {
            hideLoading();
            showError(result.message || (window.i18n ? window.i18n.t('prediction_failed') : 'Prediction failed'));
            return;
        }

        // Async task - start polling
        if (result.status === 'pending' && result.task_id) {
            await pollPredictionStatus(result.task_id, Date.now());
            return;
        }

        // Immediate result
        handlePredictionResult(result);

    } catch (error) {
        hideLoading();
        if (error.name === 'AbortError') {
            showError(window.i18n ? window.i18n.t('request_timed_out') : 'Request timed out. Please try again.');
        } else {
            console.error('Prediction error:', error);
            showError(window.i18n ? window.i18n.t('network_error') : 'Network error. Please try again.');
        }
    }
}

// Submit a batch of multimer structures via the project job queue.
async function submitBatchPrediction() {
    ensureMultimerBatch();

    // Shared per-job config applied to every structure in the batch.
    var config = { output_format: currentOutputFormat };

    if (currentLigandType === 'ccd' && selectedLigandCCD) {
        config.ligand_ccd = selectedLigandCCD;
        config.ligand_id = document.getElementById('ligand-id').value.toUpperCase() || 'L';
    } else if (currentLigandType === 'smiles') {
        var smiles = document.getElementById('ligand-smiles-input').value.trim();
        if (smiles) {
            config.ligand_smiles = smiles;
            config.ligand_id = document.getElementById('ligand-id').value.toUpperCase() || 'L';
        }
    }

    var msaInput = document.getElementById('msa-csv');
    if (msaInput && msaInput.value.trim()) {
        config.msa_input = msaInput.value.trim();
        config.msa_format = 'csv';
    }

    var projectId = parseInt(sessionStorage.getItem('selectedProject'), 10) || 1;
    var payload = multimerBatch.toPayload(config, projectId);
    if (!payload) return; // validation failed; onError already fired

    var submitBtn = document.getElementById('submit-btn');
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = window.i18n ? window.i18n.t('submitting_batch') : 'Submitting batch...'; }

    try {
        var response = await fetch('/api/batch/jobs', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': getCsrfToken()
            },
            body: JSON.stringify(payload)
        });
        var result = await response.json();

        if (result.status === 'success' || (result.jobs_count && result.jobs_count > 0)) {
            var created = result.jobs_count != null ? result.jobs_count : payload.items.length;
            showSuccess((window.i18n ? window.i18n.t('batch_submitted', {count: created}) : 'Batch submitted: ' + created + ' job(s) queued.') +
                (result.errors_count ? (window.i18n ? window.i18n.t('batch_failed_count', {count: result.errors_count}) : ' ' + result.errors_count + ' failed.') : ''));
            setTimeout(function() { window.location.href = '/dashboard'; }, 1800);
        } else {
            showError(result.message || (window.i18n ? window.i18n.t('batch_submission_failed') : 'Batch submission failed'));
        }
    } catch (err) {
        console.error('Batch submission error:', err);
        showError(window.i18n ? window.i18n.t('failed_to_submit_batch', {message: err.message}) : 'Failed to submit batch: ' + err.message);
    } finally {
        if (submitBtn) { submitBtn.disabled = false; }
        hideLoading();
    }
}

async function pollPredictionStatus(taskId, startTime) {
    var maxWaitMs = 960000; // 16 minutes (ligand jobs can take 10-15 min)
    var pollInterval = 3000;

    while (Date.now() - startTime < maxWaitMs) {
        var elapsed = Math.floor((Date.now() - startTime) / 1000);
        var loadingText = document.querySelector('.loading-text');
        if (loadingText) {
            loadingText.textContent = window.i18n ? window.i18n.t('predicting_elapsed', {seconds: elapsed}) : 'Predicting structure... (' + elapsed + 's)';
        }

        await new Promise(function(resolve) { setTimeout(resolve, pollInterval); });

        try {
            var statusRes = await fetch('/api/openfold3/status/' + encodeURIComponent(taskId), {
                headers: { 'X-CSRF-Token': getCsrfToken() }
            });
            var statusData = await statusRes.json();

            if (statusData.error) {
                hideLoading();
                showError(statusData.message || (window.i18n ? window.i18n.t('prediction_failed') : 'Prediction failed'));
                return;
            }

            if (statusData.status === 'pending') {
                continue;
            }

            // Completed
            handlePredictionResult(statusData);
            return;

        } catch (err) {
            console.warn('OpenFold3 status poll error:', err);
        }
    }

    hideLoading();
    showError(window.i18n ? window.i18n.t('prediction_timed_out') : 'Prediction timed out after 16 minutes. Please try again or use a shorter sequence.');
}

function handlePredictionResult(result) {
    hideLoading();
    displayResults(result);

    // Update credits display
    // Server knows the balance and the price (config); no hard-coded deduction here
    if (window.ModelCost) window.ModelCost.refresh();
}

function displayResults(result) {
    document.getElementById('loading-panel').classList.add('hidden');
    document.getElementById('placeholder-panel').classList.add('hidden');
    document.getElementById('result-panel').classList.remove('hidden');

    currentStructure = result;

    // Display metrics if available
    displayMetrics(result);

    // Visualize structure
    var structureData = extractStructureData(result);
    if (structureData) {
        visualizeStructure(structureData);
    }
}

function extractStructureData(result) {
    // OpenFold3 may return structure in various formats
    if (result.structure) {
        return result.structure;
    }
    if (result.structures && result.structures.length > 0) {
        return result.structures[0].structure || result.structures[0];
    }
    if (result.outputs && result.outputs.length > 0) {
        var output = result.outputs[0];
        if (output.structure) return output.structure;
        if (output.poses && output.poses.length > 0) return output.poses[0];
    }
    if (typeof result === 'string') return result;
    if (result.raw_content) return result.raw_content;
    return null;
}

// Mean per-residue pLDDT (0-100) — the real, human-meaningful confidence. OpenFold3 writes
// pLDDT into the ModelCIF QA table (_ma_qa_metric_local.metric_value) and the B-factor column.
function extractMeanPlddt(structureData) {
    if (typeof structureData !== 'string' || !structureData) return null;
    var lines = structureData.split('\n');
    var scores = [];

    // Strategy 1: ModelCIF per-residue QA table.
    if (structureData.indexOf('_ma_qa_metric_local.') !== -1) {
        var colCount = 0, valueIdx = -1, inLoop = false;
        for (var i = 0; i < lines.length; i++) {
            var ln = lines[i].trim();
            if (!ln) continue;
            if (ln.indexOf('_ma_qa_metric_local.') === 0) {
                inLoop = true;
                if (ln.indexOf('.metric_value') !== -1) valueIdx = colCount;
                colCount++;
                continue;
            }
            if (!inLoop) continue;
            if (ln.charAt(0) === '_' || ln.indexOf('loop_') === 0 || ln.charAt(0) === '#') break;
            var p = ln.split(/\s+/);
            if (valueIdx >= 0 && valueIdx < p.length) {
                var v = parseFloat(p[valueIdx]);
                if (isFinite(v)) scores.push(v);
            }
        }
    }

    // Strategy 2: CA B-factors from a PDB file (fixed columns 61-66).
    if (!scores.length && structureData.indexOf('_atom_site.') === -1) {
        var seen = {};
        for (var j = 0; j < lines.length; j++) {
            var line = lines[j];
            if (line.indexOf('ATOM') !== 0 || line.length < 66) continue;
            if (line.substring(12, 16).trim() !== 'CA') continue;
            var key = line.substring(21, 22) + ':' + line.substring(22, 26).trim();
            if (seen[key]) continue;
            seen[key] = true;
            var b = parseFloat(line.substring(60, 66));
            if (isFinite(b)) scores.push(b);
        }
    }

    if (!scores.length) return null;
    var mean = scores.reduce(function(a, b) { return a + b; }, 0) / scores.length;
    return mean <= 1.05 ? mean * 100 : mean; // some outputs store pLDDT as 0-1
}

function displayMetrics(result) {
    var metricsGrid = document.getElementById('metrics-grid');
    while (metricsGrid.firstChild) {
        metricsGrid.removeChild(metricsGrid.firstChild);
    }

    var metrics = [];

    // Headline confidence: mean per-residue pLDDT (0-100). This is the value users expect to
    // read as "how confident is this structure" — NOT the ranking score below.
    var meanPlddt = extractMeanPlddt(extractStructureData(result));
    if (meanPlddt !== null) {
        metrics.push({
            label: window.i18n ? window.i18n.t('metric_confidence') : 'Confidence (avg pLDDT)',
            value: meanPlddt.toFixed(1) + '%',
            cssClass: meanPlddt >= 70 ? 'good' : meanPlddt >= 50 ? 'medium' : ''
        });
    }

    // Ranking score (AF3/OpenFold3 composite: 0.8*ipTM + 0.2*pTM + 0.5*disorder - clash).
    // NOT a confidence percentage: for single-chain monomers ipTM~0, so the score is
    // capped near 0.2*pTM (~0.2) even for excellent structures. Show it raw, and let
    // the pLDDT above and pTM/ipTM below communicate actual confidence.
    if (result.confidence_scores && result.confidence_scores.length > 0) {
        var ranking = result.confidence_scores[0];
        metrics.push({
            label: window.i18n ? window.i18n.t('metric_ranking_score') : 'Ranking Score',
            value: ranking.toFixed(3),
            cssClass: ''
        });
    }

    // PTM scores
    if (result.ptm_scores && result.ptm_scores.length > 0) {
        var ptm = result.ptm_scores[0];
        var ptmValue = ptm > 1.0 ? ptm.toFixed(1) : (ptm * 100).toFixed(1);
        var ptmLabel = ptm > 1.0 ? '' : '%';
        var normPtm = ptm > 1.0 ? ptm / 100 : ptm;
        metrics.push({
            label: window.i18n ? window.i18n.t('metric_ptm_score') : 'PTM Score',
            value: ptmValue + ptmLabel,
            cssClass: normPtm > 0.8 ? 'good' : normPtm > 0.6 ? 'medium' : ''
        });
    }

    // iPTM scores
    if (result.iptm_scores && result.iptm_scores.length > 0 && result.iptm_scores[0] > 0) {
        var iptm = result.iptm_scores[0];
        var iptmValue = iptm > 1.0 ? iptm.toFixed(1) : (iptm * 100).toFixed(1);
        var iptmLabel = iptm > 1.0 ? '' : '%';
        var normIptm = iptm > 1.0 ? iptm / 100 : iptm;
        metrics.push({
            label: window.i18n ? window.i18n.t('metric_iptm') : 'iPTM',
            value: iptmValue + iptmLabel,
            cssClass: normIptm > 0.8 ? 'good' : normIptm > 0.6 ? 'medium' : ''
        });
    }

    // Output format
    metrics.push({
            label: window.i18n ? window.i18n.t('metric_format') : 'Format',
        value: currentOutputFormat.toUpperCase(),
        cssClass: ''
    });

    // Sequence length
    var totalLength = 0;
    if (isMultimer) {
        for (var c = 0; c < multimerChains.length; c++) {
            totalLength += parseFASTA(multimerChains[c].sequence).length;
        }
    } else {
        var seq = document.getElementById('sequence');
        if (seq) totalLength = parseFASTA(seq.value).length;
    }
    if (totalLength > 0) {
        metrics.push({
            label: isMultimer ? (window.i18n ? window.i18n.t('metric_total_residues') : 'Total Residues') : (window.i18n ? window.i18n.t('metric_sequence_length') : 'Sequence Length'),
            value: String(totalLength),
            cssClass: ''
        });
    }

    for (var i = 0; i < metrics.length; i++) {
        var m = metrics[i];
        var card = document.createElement('div');
        card.className = 'metric-card';

        var valueDiv = document.createElement('div');
        valueDiv.className = 'metric-value' + (m.cssClass ? ' ' + m.cssClass : '');
        valueDiv.textContent = m.value;

        var labelDiv = document.createElement('div');
        labelDiv.className = 'metric-label';
        labelDiv.textContent = m.label;

        card.appendChild(valueDiv);
        card.appendChild(labelDiv);
        metricsGrid.appendChild(card);
    }
}

function visualizeStructure(structureData) {
    // Determine format
    var format = 'pdb';
    if (typeof structureData === 'string') {
        if (structureData.indexOf('_atom_site') !== -1 || structureData.indexOf('data_') !== -1) {
            format = 'cif';
        }
    }

    // Initialize PhonosViewer (Mol*) on first call
    if (!viewer3D || !viewer3D._plugin) {
        viewer3D = PhonosViewer.create('structure-viewer', {
            alphafoldView: true,
            hideControls: true,
            sequencePanel: false,
            focusPanel: false
        });
        window._phonosViewer = viewer3D;
    }

    viewer3D.loadData(structureData, format, { alphafoldView: true });

    // Ensure ligand/het atoms are visible (combined CIF/PDB may contain HETATM)
    if (viewer3D.setVisibility) {
        setTimeout(function() {
            try { viewer3D.setVisibility({ polymer: true, het: true, water: false }); } catch (_) {}
        }, 300);
    }

    // Show confidence legend
    document.getElementById('confidence-legend').classList.remove('hidden');
}

function applyConfidenceColoring() {
    if (!viewer3D || !viewer3D._plugin) return;
    viewer3D.applyConfidenceColoring();
    currentColorMode = 'confidence';
}

function applyChainColoring() {
    if (!viewer3D || !viewer3D._plugin) return;
    viewer3D.applyChainColoring();
    currentColorMode = 'chain';
}

function resetView() {
    if (viewer3D && viewer3D._plugin) {
        viewer3D.resetView();
    }
}

function toggleColorMode() {
    if (currentColorMode === 'confidence') {
        applyChainColoring();
    } else {
        applyConfidenceColoring();
    }
}

function downloadStructure() {
    if (!currentStructure) {
        showError(window.i18n ? window.i18n.t('no_structure_download') : 'No structure to download');
        return;
    }

    var structureData = extractStructureData(currentStructure);
    if (!structureData) {
        showError(window.i18n ? window.i18n.t('no_structure_data') : 'No structure data available');
        return;
    }

    var ext = currentOutputFormat === 'mmcif' ? '.cif' : '.pdb';
    var blob = new Blob([structureData], { type: 'text/plain' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'openfold3_prediction' + ext;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showSuccess(window.i18n ? window.i18n.t('structure_downloaded') : 'Structure downloaded');
}
