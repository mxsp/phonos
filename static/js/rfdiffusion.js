/**
 * RFDiffusion - Protein Design Interface
 * Handles 3D structure visualization with multi-structure toggle support
 */
(function() {
'use strict';

// State
var viewer = null;
var inputPdbContent = null;
var structures = [];
var visibleStructures = new Set(['input']);
var isSpinning = false;

// Escape HTML for safe display
function escapeHtml(text) {
    if (!text) return '';
    var div = document.createElement('div');
    div.textContent = text;
    return div.textContent;
}

// Initialize PhonosViewer (Mol*)
function initViewer() {
    var element = document.getElementById('viewer3d');
    if (!element) return;

    viewer = PhonosViewer.create('viewer3d', {
        alphafoldView: false,
        hideControls: true,
        sequencePanel: false,
        focusPanel: false
    });
    window._phonosViewer = viewer;
}

// Update viewer with current visibility states
function updateViewer() {
    if (!viewer || !viewer._plugin) return;

    // Combine visible PDB data into multi-model PDB
    var combinedPdb = '';
    var modelNum = 1;

    if (inputPdbContent && visibleStructures.has('input')) {
        combinedPdb += 'MODEL     ' + modelNum + '\n';
        combinedPdb += inputPdbContent;
        if (!inputPdbContent.endsWith('\n')) combinedPdb += '\n';
        combinedPdb += 'ENDMDL\n';
        modelNum++;
    }

    structures.forEach(function(struct, i) {
        var key = 'design_' + i;
        if (visibleStructures.has(key)) {
            combinedPdb += 'MODEL     ' + modelNum + '\n';
            combinedPdb += struct.pdb;
            if (!struct.pdb.endsWith('\n')) combinedPdb += '\n';
            combinedPdb += 'ENDMDL\n';
            modelNum++;
        }
    });

    if (combinedPdb) {
        combinedPdb += 'END\n';
        viewer.loadData(combinedPdb, 'pdb');
        viewer.applyChainColoring();
    }
}

// Toggle structure visibility
function toggleStructure(key) {
    if (visibleStructures.has(key)) {
        visibleStructures.delete(key);
    } else {
        visibleStructures.add(key);
    }
    updateViewer();
    updateToggleButtons();
}

// Update toggle button states
function updateToggleButtons() {
    document.querySelectorAll('.rf-structure-toggle').forEach(function(btn) {
        var key = btn.dataset.structure;
        if (visibleStructures.has(key)) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
}

// Build structure toggles UI
function buildToggles() {
    var container = document.getElementById('structure-toggles');
    if (!container) return;

    container.textContent = '';

    // Input toggle
    var inputToggle = document.createElement('div');
    inputToggle.className = 'rf-structure-toggle' + (visibleStructures.has('input') ? ' active' : '');
    inputToggle.dataset.structure = 'input';

    var inputDot = document.createElement('span');
    inputDot.className = 'rf-color-dot';
    inputDot.style.background = '#808080';
    inputToggle.appendChild(inputDot);

    var inputLabel = document.createElement('span');
    inputLabel.className = 'rf-toggle-label';
    inputLabel.textContent = 'Input';
    inputToggle.appendChild(inputLabel);

    inputToggle.onclick = function() { toggleStructure('input'); };
    container.appendChild(inputToggle);

    // Design toggles
    structures.forEach(function(struct, i) {
        var key = 'design_' + i;
        var toggle = document.createElement('div');
        toggle.className = 'rf-structure-toggle' + (visibleStructures.has(key) ? ' active' : '');
        toggle.dataset.structure = key;

        var dot = document.createElement('span');
        dot.className = 'rf-color-dot';
        dot.style.background = struct.color;
        toggle.appendChild(dot);

        var label = document.createElement('span');
        label.className = 'rf-toggle-label';
        label.textContent = struct.name;
        toggle.appendChild(label);

        toggle.onclick = function() { toggleStructure(key); };
        container.appendChild(toggle);
    });

    // Show All button
    if (structures.length > 0) {
        var showAll = document.createElement('div');
        showAll.className = 'rf-structure-toggle';
        var showAllLabel = document.createElement('span');
        showAllLabel.className = 'rf-toggle-label';
        showAllLabel.textContent = 'Show All';
        showAll.appendChild(showAllLabel);
        showAll.onclick = function() {
            visibleStructures.add('input');
            structures.forEach(function(_, i) { visibleStructures.add('design_' + i); });
            updateViewer();
            updateToggleButtons();
        };
        container.appendChild(showAll);

        // Hide All button
        var hideAll = document.createElement('div');
        hideAll.className = 'rf-structure-toggle';
        var hideAllLabel = document.createElement('span');
        hideAllLabel.className = 'rf-toggle-label';
        hideAllLabel.textContent = 'Hide All';
        hideAll.appendChild(hideAllLabel);
        hideAll.onclick = function() {
            visibleStructures.clear();
            updateViewer();
            updateToggleButtons();
        };
        container.appendChild(hideAll);
    }
}

// Show status message
function showStatus(message, type) {
    var container = document.getElementById('status-container');
    if (!container) return;

    var statusDiv = document.createElement('div');
    statusDiv.className = 'rf-status ' + type;

    if (type === 'info') {
        var spinner = document.createElement('div');
        spinner.className = 'rf-spinner';
        statusDiv.appendChild(spinner);
    }

    var msgSpan = document.createElement('span');
    msgSpan.textContent = message;
    statusDiv.appendChild(msgSpan);

    container.textContent = '';
    container.appendChild(statusDiv);
}

function clearStatus() {
    var container = document.getElementById('status-container');
    if (container) container.textContent = '';
}

// Handle file upload
function setupFileUpload() {
    var upload = document.getElementById('pdb-upload');
    var fileInput = document.getElementById('pdb-file');
    var filenameEl = document.getElementById('pdb-filename');

    if (!upload || !fileInput) return;

    upload.onclick = function() { fileInput.click(); };

    upload.ondragover = function(e) {
        e.preventDefault();
        upload.style.borderColor = 'var(--rf-primary)';
    };

    upload.ondragleave = function() {
        upload.style.borderColor = '';
    };

    upload.ondrop = function(e) {
        e.preventDefault();
        upload.style.borderColor = '';
        if (e.dataTransfer.files.length > 0) {
            handleFile(e.dataTransfer.files[0]);
        }
    };

    fileInput.onchange = function(e) {
        if (e.target.files.length > 0) {
            handleFile(e.target.files[0]);
        }
    };

    function handleFile(file) {
        var reader = new FileReader();
        reader.onload = function(e) {
            inputPdbContent = e.target.result;
            if (filenameEl) filenameEl.textContent = file.name;
            upload.classList.add('has-file');
            buildToggles();
            updateViewer();
        };
        reader.readAsText(file);
    }
}

// Fetch PDB from RCSB
async function fetchPdb() {
    var pdbIdInput = document.getElementById('pdb-id');
    var pdbId = pdbIdInput ? pdbIdInput.value.trim().toUpperCase() : '';

    if (!pdbId || pdbId.length !== 4) {
        alert('Please enter a valid 4-character PDB ID');
        return;
    }

    showStatus('Fetching ' + pdbId + ' from RCSB...', 'info');

    try {
        var resp = await fetch('/api/rfdiffusion/fetch-pdb/' + pdbId);
        var data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.error || 'Failed to fetch PDB');
        }

        inputPdbContent = data.pdb_content;

        var filenameEl = document.getElementById('pdb-filename');
        if (filenameEl) filenameEl.textContent = pdbId + '.pdb (' + data.atom_count + ' atoms)';

        var upload = document.getElementById('pdb-upload');
        if (upload) upload.classList.add('has-file');

        clearStatus();
        buildToggles();
        updateViewer();

    } catch (err) {
        showStatus(err.message, 'error');
    }
}

// Generate designs
async function generateDesigns() {
    if (!inputPdbContent) {
        alert('Please upload or fetch a PDB structure first');
        return;
    }

    var contigsInput = document.getElementById('contigs');
    var contigs = contigsInput ? contigsInput.value.trim() : '';
    if (!contigs) {
        alert('Please enter a contig specification');
        return;
    }

    var hotspotInput = document.getElementById('hotspot-res');
    var hotspotRes = [];
    if (hotspotInput && hotspotInput.value.trim()) {
        hotspotRes = hotspotInput.value.trim().split(',').map(function(s) { return s.trim(); }).filter(function(s) { return s; });
    }

    var diffusionStepsInput = document.getElementById('diffusion-steps');
    var diffusionSteps = diffusionStepsInput ? parseInt(diffusionStepsInput.value) : 50;

    var numDesignsInput = document.getElementById('num-designs');
    var numDesigns = numDesignsInput ? parseInt(numDesignsInput.value) : 1;

    var noiseScaleInput = document.getElementById('noise-scale');
    var noiseScale = noiseScaleInput ? parseFloat(noiseScaleInput.value) : 1.0;

    showStatus('Generating designs... This may take a few minutes.', 'info');

    var generateBtn = document.getElementById('generate-btn');
    if (generateBtn) generateBtn.disabled = true;

    try {
        var csrfToken = '';
        var csrfMeta = document.querySelector('meta[name="csrf-token"]');
        if (csrfMeta) csrfToken = csrfMeta.content;

        var resp = await fetch('/api/rfdiffusion/predict', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken
            },
            body: JSON.stringify({
                input_pdb: inputPdbContent,
                contigs: contigs,
                hotspot_res: hotspotRes,
                diffusion_steps: diffusionSteps,
                num_designs: numDesigns,
                noise_scale: noiseScale
            })
        });

        var data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.error || data.message || 'Generation failed');
        }

        // Handle async job
        if (data.status === 'queued') {
            showStatus('Job queued: ' + data.job_id + '. Check dashboard for results.', 'info');
            return;
        }

        // Handle sync results
        if (data.structures && data.structures.length > 0) {
            var colors = ['#00ff00', '#ff6600', '#0099ff', '#ff00ff', '#ffff00', '#00ffff'];
            structures = data.structures.map(function(s, i) {
                return {
                    pdb: atob(s.pdb_b64),
                    name: s.name || ('Design ' + (i + 1)),
                    color: colors[i % colors.length]
                };
            });

            // Enable all designs by default
            structures.forEach(function(_, i) { visibleStructures.add('design_' + i); });

            buildToggles();
            updateViewer();
            showStatus('Generated ' + structures.length + ' designs in ' + data.execution_time.toFixed(1) + 's', 'success');
        }

    } catch (err) {
        showStatus(err.message, 'error');
    } finally {
        if (generateBtn) generateBtn.disabled = false;
    }
}

// Load examples
async function loadExamples() {
    try {
        var resp = await fetch('/api/rfdiffusion/examples');
        var data = await resp.json();

        var container = document.getElementById('examples-container');
        if (!container) return;

        data.examples.forEach(function(ex) {
            var card = document.createElement('div');
            card.className = 'rf-example-card';

            var nameDiv = document.createElement('div');
            nameDiv.className = 'rf-example-name';
            nameDiv.textContent = ex.name;
            card.appendChild(nameDiv);

            var descDiv = document.createElement('div');
            descDiv.className = 'rf-example-desc';
            descDiv.textContent = ex.description;
            card.appendChild(descDiv);

            card.onclick = function() {
                var contigsInput = document.getElementById('contigs');
                if (contigsInput) contigsInput.value = ex.contigs;

                var hotspotInput = document.getElementById('hotspot-res');
                if (hotspotInput) hotspotInput.value = (ex.hotspot_res || []).join(',');

                var stepsInput = document.getElementById('diffusion-steps');
                var stepsVal = document.getElementById('diffusion-steps-val');
                if (stepsInput) {
                    stepsInput.value = ex.diffusion_steps;
                    if (stepsVal) stepsVal.textContent = ex.diffusion_steps;
                }

                if (ex.pdb_url) {
                    var match = ex.pdb_url.match(/\/(\w{4})\.pdb$/);
                    if (match) {
                        var pdbIdInput = document.getElementById('pdb-id');
                        if (pdbIdInput) pdbIdInput.value = match[1];
                        fetchPdb();
                    }
                }
            };
            container.appendChild(card);
        });
    } catch (err) {
        console.error('Failed to load examples', err);
    }
}

// Setup range sliders
function setupRangeSliders() {
    var sliders = [
        { id: 'diffusion-steps', valId: 'diffusion-steps-val' },
        { id: 'num-designs', valId: 'num-designs-val' },
        { id: 'noise-scale', valId: 'noise-scale-val' }
    ];

    sliders.forEach(function(config) {
        var slider = document.getElementById(config.id);
        var valEl = document.getElementById(config.valId);
        if (slider && valEl) {
            slider.oninput = function() { valEl.textContent = slider.value; };
        }
    });
}

// Setup viewer controls
function setupViewerControls() {
    var resetBtn = document.getElementById('reset-view-btn');
    if (resetBtn) {
        resetBtn.onclick = function() {
            if (viewer && viewer._plugin) {
                viewer.resetView();
            }
        };
    }

    var spinBtn = document.getElementById('spin-btn');
    if (spinBtn) {
        spinBtn.onclick = function() {
            isSpinning = !isSpinning;
            if (viewer && viewer._plugin) viewer.toggleSpin(isSpinning);
            if (isSpinning) {
                spinBtn.classList.add('active');
            } else {
                spinBtn.classList.remove('active');
            }
        };
    }

    var downloadBtn = document.getElementById('download-btn');
    if (downloadBtn) {
        downloadBtn.onclick = function() {
            if (structures.length === 0) {
                alert('No designs to download');
                return;
            }

            // Download first design
            var blob = new Blob([structures[0].pdb], { type: 'chemical/x-pdb' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = 'rfdiffusion_design_1.pdb';
            a.click();
            URL.revokeObjectURL(url);
        };
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', function() {
    initViewer();
    setupFileUpload();
    setupRangeSliders();
    setupViewerControls();
    loadExamples();
    buildToggles();

    var fetchBtn = document.getElementById('fetch-pdb-btn');
    if (fetchBtn) fetchBtn.onclick = fetchPdb;

    var generateBtn = document.getElementById('generate-btn');
    if (generateBtn) generateBtn.onclick = generateDesigns;
});

})();
