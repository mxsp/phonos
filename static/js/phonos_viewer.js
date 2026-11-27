/**
 * PhonosViewer — Unified Molecular Structure Viewer for Phonos Platform
 * Built on pdbe-molstar (Mol* / Molstar) — the engine behind RCSB PDB & AlphaFold DB
 *
 * @version 2.6.0
 *
 * Features:
 *  - Region focus & isolation (zoom + dim surroundings)
 *  - pLDDT confidence coloring (AlphaFold/OpenFold/ESMFold/Boltz2)
 *  - Confidence-aware quick focus (high/low confidence regions)
 *  - Binding site & ligand focus
 *  - Sequence viewer panel (synced with 3D)
 *  - Visibility controls (polymer, ligands, water, ions)
 *  - Chain/spectrum/element coloring
 *  - Screenshots, spin, fullscreen, keyboard shortcuts
 *  - Focus & Analysis side panel (optional)
 *  - Raw data + URL + PDB ID loading
 */
(function (global) {
    'use strict';

    /* ------------------------------------------------------------------ */
    /*  Constants                                                          */
    /* ------------------------------------------------------------------ */

    var VERSION = '2.8.0';

    var PLDDT = {
        VERY_HIGH:  { min: 90, max: 100, color: { r: 0,   g: 83,  b: 214 }, label: (window.i18n ? window.i18n.t('phonosViewer.plddtVeryHigh') : 'Very high (>90)'),  css: '#0053d6' },
        CONFIDENT:  { min: 70, max: 90,  color: { r: 101, g: 203, b: 243 }, label: (window.i18n ? window.i18n.t('phonosViewer.plddtConfident') : 'Confident (70-90)'), css: '#65cbf3' },
        LOW:        { min: 50, max: 70,  color: { r: 255, g: 219, b: 19  }, label: (window.i18n ? window.i18n.t('phonosViewer.plddtLow') : 'Low (50-70)'),        css: '#ffdb13' },
        VERY_LOW:   { min: 0,  max: 50,  color: { r: 255, g: 125, b: 69  }, label: (window.i18n ? window.i18n.t('phonosViewer.plddtVeryLow') : 'Very low (<50)'),     css: '#ff7d45' }
    };

    var CHAIN_COLORS = [
        '#10b981', '#60a5fa', '#fb923c', '#a78bfa',
        '#fb7185', '#facc15', '#22d3ee', '#f472b6'
    ];

    var BG_DARK  = { r: 9,  g: 9,  b: 11 };
    var BG_SLATE = { r: 17, g: 24, b: 39 };
    var BG_NAVY  = { r: 11, g: 31, b: 52 };
    var BG_LIGHT = { r: 248, g: 250, b: 252 };

    var BACKGROUNDS = [
        { name: 'dark',  rgb: BG_DARK  },
        { name: 'slate', rgb: BG_SLATE },
        { name: 'navy',  rgb: BG_NAVY  },
        { name: 'light', rgb: BG_LIGHT }
    ];

    var VISUAL_STYLE_MAP = {
        cartoon: 'cartoon',
        stick: 'ball-and-stick',
        sphere: 'spacefill',
        surface: 'molecular-surface',
        line: 'line',
        cross: 'point'
    };

    function normalizeVisualStyle(style) {
        var key = String(style || 'cartoon').toLowerCase();
        return VISUAL_STYLE_MAP[key] || VISUAL_STYLE_MAP.cartoon;
    }

    /* ------------------------------------------------------------------ */
    /*  Render quality presets                                            */
    /* ------------------------------------------------------------------ */

    /* Canvas-level post-processing presets (ambient occlusion + outlines +
       shadows). These are global to the canvas — independent of which
       representations/overlay layers are loaded — so they survive rebuilds and
       lift the default flat PDBeMolstar look toward the AlphaFold-DB / RCSB
       look. Only long-stable Mol* postprocessing keys are used, and the whole
       thing is feature-detected + try/caught so older engines just stay flat. */
    var RENDER_QUALITY = {
        flat:         { label: 'Flat' },
        standard:     { label: 'Standard' },
        illustrative: { label: 'Illustrative' },
        cinematic:    { label: 'Cinematic' }
    };

    var DEFAULT_RENDER_QUALITY = 'standard';

    function off(name) { return { name: 'off', params: {} }; }

    function aoParams(strength) {
        var p = {
            multiScale: { name: 'off', params: {} },
            samples: 32,
            radius: strength === 'strong' ? 6 : 5,
            bias: strength === 'strong' ? 1.0 : 0.8,
            blurKernelSize: 15
        };
        return p;
    }

    function outlineParams(bold) {
        return {
            scale: bold ? 1.2 : 1,
            threshold: bold ? 0.4 : 0.25,
            color: 0x0a0a0a
        };
    }

    function shadowParams() {
        return { steps: 1, bias: 0.6, maxDistance: 3, tolerance: 1.0 };
    }

    /* Build the partial Canvas3D props for a quality preset. */
    function buildCanvas3DProps(preset) {
        switch (preset) {
            case 'flat':
                return { postprocessing: { occlusion: off(), outline: off(), shadow: off() } };
            case 'illustrative':
                return { postprocessing: {
                    occlusion: { name: 'on', params: aoParams('strong') },
                    outline:   { name: 'on', params: outlineParams(true) },
                    shadow:    off()
                } };
            case 'cinematic':
                return { postprocessing: {
                    occlusion: { name: 'on', params: aoParams('strong') },
                    outline:   { name: 'on', params: outlineParams(false) },
                    shadow:    { name: 'on', params: shadowParams() }
                } };
            case 'standard':
            default:
                return { postprocessing: {
                    occlusion: { name: 'on', params: aoParams('soft') },
                    outline:   { name: 'on', params: outlineParams(false) },
                    shadow:    off()
                } };
        }
    }

    /* dataURI → Blob (for clipboard copy of hi-res off-screen renders). */
    function dataUriToBlob(uri) {
        try {
            var parts = uri.split(',');
            var mime = (parts[0].match(/:(.*?);/) || [])[1] || 'image/png';
            var bin = atob(parts[1]);
            var arr = new Uint8Array(bin.length);
            for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            return new Blob([arr], { type: mime });
        } catch (e) { return null; }
    }

    /* Ligand ball-and-stick: plain CPK (grey carbons) instead of Mol*'s default
       chain-colored carbons, which on a 'LIG' chain come out orange and read as the
       'very low' pLDDT band next to a confidence-colored protein. */
    var LIGAND_ELEMENT_COLOR_PARAMS = { carbonColor: { name: 'element-symbol', params: {} } };

    /* Our display-layer keys → Mol* representation type names. */
    function molstarRepType(t) {
        switch (String(t || '').toLowerCase()) {
            case 'cartoon': return 'cartoon';
            case 'surface':
            case 'molecular-surface': return 'molecular-surface';
            case 'stick':
            case 'ballstick':
            case 'ball-and-stick': return 'ball-and-stick';
            case 'sphere':
            case 'spacefill': return 'spacefill';
            case 'line': return 'line';
            case 'cross':
            case 'point': return 'point';
            default: return 'cartoon';
        }
    }

    /* Color-scheme keys → Mol* built-in color theme names (used for 'Auto' layers). */
    var COLOR_THEME_MAP = {
        chainid: 'chain-id',
        chain: 'chain-id',
        rainbow: 'sequence-id',
        hydrophobicity: 'hydrophobicity',
        // pLDDT has no usable Mol* color theme on custom blob-URL structures, so it is
        // painted via visual.select() overpaint instead (see OVERPAINT_SCHEMES). The
        // base reps render with a neutral theme; the overpaint covers the polymer.
        plddt: 'chain-id',
        element: 'element-symbol',
        'default': 'element-symbol'
    };

    /* Schemes applied via per-residue visual.select() overpaint rather than a Mol*
       color theme. Overlay layers use COLOR_THEME_MAP as a neutral base, then the
       overpaint is re-applied on top after every rebuild/theme change. */
    var OVERPAINT_SCHEMES = { plddt: 1 };

    /* '#rrggbb' → Mol* Color (an 0xRRGGBB integer). Returns null when invalid. */
    function hexToMolColor(hex) {
        if (typeof hex !== 'string') return null;
        var h = hex.replace('#', '');
        if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
        var n = parseInt(h, 16);
        return isNaN(n) ? null : n;
    }

    /* ------------------------------------------------------------------ */
    /*  DOM Helpers                                                        */
    /* ------------------------------------------------------------------ */

    function el(id) {
        return typeof id === 'string' ? document.getElementById(id) : id;
    }

    function qs(parent, sel) {
        return parent.querySelector(sel);
    }

    function qsa(parent, sel) {
        return Array.prototype.slice.call(parent.querySelectorAll(sel));
    }

    function mkEl(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                if (key === 'class') node.className = attrs[key];
                else if (key === 'text') node.textContent = attrs[key];
                else if (key === 'style' && typeof attrs[key] === 'string') node.style.cssText = attrs[key];
                else node.setAttribute(key, attrs[key]);
            });
        }
        if (children) {
            (Array.isArray(children) ? children : [children]).forEach(function (child) {
                if (!child) return;
                if (typeof child === 'string') node.appendChild(document.createTextNode(child));
                else node.appendChild(child);
            });
        }
        return node;
    }

    function svgIcon(paths, w, h) {
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('width', String(w || 14));
        svg.setAttribute('height', String(h || 14));
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        paths.forEach(function (d) {
            var pathEl;
            if (d.indexOf('cx=') === 0 || d.tag === 'circle') {
                pathEl = document.createElementNS(ns, 'circle');
                pathEl.setAttribute('cx', d.cx || '11');
                pathEl.setAttribute('cy', d.cy || '11');
                pathEl.setAttribute('r', d.r || '8');
            } else if (d.tag === 'rect') {
                pathEl = document.createElementNS(ns, 'rect');
                Object.keys(d).forEach(function (k) { if (k !== 'tag') pathEl.setAttribute(k, d[k]); });
            } else {
                pathEl = document.createElementNS(ns, 'path');
                pathEl.setAttribute('d', d);
            }
            svg.appendChild(pathEl);
        });
        return svg;
    }

    function dataToBlob(data) {
        return URL.createObjectURL(new Blob([data], { type: 'text/plain' }));
    }

    function nowStamp() {
        var d = new Date();
        var p = function (n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
    }

    function downloadDataUrl(dataUrl, name) {
        var a = document.createElement('a');
        a.href = dataUrl;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    function copyImageToClipboard(canvas) {
        if (!canvas || !navigator.clipboard || typeof ClipboardItem === 'undefined') {
            return Promise.reject(new Error('Clipboard API unavailable'));
        }
        return new Promise(function (resolve, reject) {
            canvas.toBlob(function (blob) {
                if (!blob) return reject(new Error('Canvas toBlob failed'));
                navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]).then(resolve, reject);
            }, 'image/png');
        });
    }

    /* ------------------------------------------------------------------ */
    /*  Confidence Data Parser                                             */
    /* ------------------------------------------------------------------ */

    /* Residue-anchor atoms used to read a per-residue confidence value: CA for
       amino acids, C4' (then P) for nucleotides — the same anchors AlphaFold DB,
       Boltz and the Phonos workers use, so the 3D colors match the pLDDT chart. */
    var CONF_ANCHOR_ATOMS = { 'CA': 1, "C4'": 1, 'P': 1 };

    /* Split one mmCIF data row into tokens, honouring '...' / "..." quoting
       (atom names such as "C4'" are written quoted). */
    function tokenizeCifRow(line) {
        var out = [];
        var i = 0, n = line.length;
        while (i < n) {
            var ch = line[i];
            if (ch === ' ' || ch === '\t') { i++; continue; }
            if (ch === "'" || ch === '"') {
                var j = i + 1;
                // A closing quote only counts when followed by whitespace / EOL.
                while (j < n && !(line[j] === ch && (j + 1 === n || line[j + 1] === ' ' || line[j + 1] === '\t'))) j++;
                out.push(line.substring(i + 1, j));
                i = j + 1;
                continue;
            }
            var k = i;
            while (k < n && line[k] !== ' ' && line[k] !== '\t') k++;
            out.push(line.substring(i, k));
            i = k;
        }
        return out;
    }

    function pushConfidenceEntry(acc, entry) {
        var key = entry.authChain + ':' + entry.authResi + ':' + (entry.insCode || '');
        if (acc.seen[key]) return;
        acc.seen[key] = true;
        acc.scores.push(entry);
        if (!acc.byChain[entry.chain]) acc.byChain[entry.chain] = [];
        acc.byChain[entry.chain].push(entry);
    }

    function parseConfidenceFromPDB(data) {
        var acc = { scores: [], byChain: {}, seen: {} };
        var lines = data.split('\n');
        var sawModel = false;

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var rec = line.substring(0, 6);
            // Multi-model files: only the first model is shown by default.
            if (rec === 'MODEL ') { if (sawModel) break; sawModel = true; continue; }
            if (rec === 'ENDMDL' && acc.scores.length) break;
            if (line.substring(0, 4) !== 'ATOM') continue;
            var atomName = line.substring(12, 16).trim();
            if (!CONF_ANCHOR_ATOMS[atomName]) continue;

            var chain = line.substring(21, 22).trim() || 'A';
            var resi  = parseInt(line.substring(22, 26).trim(), 10);
            if (isNaN(resi)) continue;
            var ins   = line.substring(26, 27).trim();
            var resn  = line.substring(17, 20).trim().toUpperCase();
            var bfact = parseFloat(line.substring(60, 66).trim());

            pushConfidenceEntry(acc, {
                chain: chain, resi: resi, resn: resn, score: bfact,
                authChain: chain, authResi: resi, insCode: ins
            });
        }
        return { scores: acc.scores, byChain: acc.byChain };
    }

    function parseConfidenceFromCIF(data) {
        var acc = { scores: [], byChain: {}, seen: {} };
        var lines = data.split('\n');
        var fieldNames = [];
        var inAtomSite = false;
        var col = {};
        var firstModel = null;

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i].trim();
            if (line === 'loop_') {
                if (inAtomSite && acc.scores.length) break;
                inAtomSite = false; fieldNames = []; col = {};
                continue;
            }
            if (line.indexOf('_atom_site.') === 0) {
                inAtomSite = true;
                col[line.substring(11).split(/\s+/)[0]] = fieldNames.length;
                fieldNames.push(line);
                continue;
            }
            if (!inAtomSite || col.B_iso_or_equiv === undefined ||
                (col.label_atom_id === undefined && col.auth_atom_id === undefined)) continue;
            if (line.length === 0 || line[0] === '#' || line[0] === '_') {
                if (acc.scores.length > 0) break;
                continue;
            }
            var parts = tokenizeCifRow(line);
            if (parts.length < fieldNames.length) continue;

            var atomName = parts[col.label_atom_id !== undefined ? col.label_atom_id : col.auth_atom_id];
            if (!CONF_ANCHOR_ATOMS[atomName]) continue;

            if (col.pdbx_PDB_model_num !== undefined) {
                var modelNum = parts[col.pdbx_PDB_model_num];
                if (firstModel === null) firstModel = modelNum;
                else if (modelNum !== firstModel) break;
            }

            // Only polymer residues carry a numeric label_seq_id ('.' for ligands/ions).
            var labelSeq = col.label_seq_id !== undefined ? parseInt(parts[col.label_seq_id], 10) : NaN;
            var authSeq = col.auth_seq_id !== undefined ? parseInt(parts[col.auth_seq_id], 10) : NaN;
            if (isNaN(labelSeq) && isNaN(authSeq)) continue;
            if (col.label_seq_id !== undefined && isNaN(labelSeq)) continue;

            var labelChain = col.label_asym_id !== undefined ? parts[col.label_asym_id] : '';
            var authChain = col.auth_asym_id !== undefined ? parts[col.auth_asym_id] : '';
            var ch = authChain || labelChain || 'A';
            var bfact = parseFloat(parts[col.B_iso_or_equiv]);
            var ins = col.pdbx_PDB_ins_code !== undefined ? parts[col.pdbx_PDB_ins_code] : '';
            if (ins === '?' || ins === '.') ins = '';
            var compCol = col.label_comp_id !== undefined ? col.label_comp_id : col.auth_comp_id;
            var resn = (compCol !== undefined ? String(parts[compCol] || '') : '').toUpperCase();

            pushConfidenceEntry(acc, {
                // chain/resi keep their historical meaning (auth chain, label seq) for
                // existing callers; the explicit fields below drive 3D coloring.
                chain: ch,
                resi: isNaN(labelSeq) ? authSeq : labelSeq,
                resn: resn,
                score: bfact,
                authChain: ch,
                authResi: isNaN(authSeq) ? labelSeq : authSeq,
                labelChain: labelChain || ch,
                labelSeq: isNaN(labelSeq) ? null : labelSeq,
                insCode: ins
            });
        }
        return { scores: acc.scores, byChain: acc.byChain };
    }

    /* Experimental structures store crystallographic B-factors (Å², higher = less
       certain), not pLDDT — never paint those with the confidence bands. */
    function looksExperimental(data, format) {
        var head = String(data || '').slice(0, 200000);
        if (format === 'cif' || format === 'mmcif') {
            return /_exptl\.method\s+['"]?(X-RAY|ELECTRON|SOLUTION NMR|SOLID-STATE NMR|NEUTRON|FIBER)/i.test(head) ||
                /_refine\.ls_d_res_high\s+[0-9]/.test(head);
        }
        return /^EXPDTA\s+(X-RAY|ELECTRON|SOLUTION NMR|SOLID-STATE NMR|NEUTRON|FIBER)/mi.test(head) ||
            /^REMARK\s+2\s+RESOLUTION\.\s+[0-9]/m.test(head);
    }

    /* Bring B-factor confidence values onto the 0-100 pLDDT scale and decide whether
       they are real confidence scores at all.
         - 0..1 values (Boltz / ESMFold-style fractions) are scaled ×100 as a whole
           array — never per value, which would mis-scale a genuine 0-100 score ≤1.
         - Values above 100, all-identical values (e.g. B = 0 from RFdiffusion /
           docking tools) and experimental files are flagged hasScores = false: the
           residue list is still used for chain / rainbow / hydrophobicity coloring. */
    function finalizeConfidence(result, data, format) {
        var scores = result.scores;
        var min = Infinity, max = -Infinity, finite = 0;
        for (var i = 0; i < scores.length; i++) {
            var v = scores[i].score;
            if (typeof v !== 'number' || !isFinite(v)) continue;
            finite++;
            if (v < min) min = v;
            if (v > max) max = v;
        }
        var scale = 'percent';
        var hasScores = finite > 0;
        if (hasScores && max > 100.5) hasScores = false;
        if (hasScores && (max - min) < 1e-6) hasScores = false;
        if (hasScores && looksExperimental(data, format)) hasScores = false;
        if (hasScores && max <= 1.0 + 1e-6) scale = 'fraction';

        for (var j = 0; j < scores.length; j++) {
            var s = scores[j].score;
            if (typeof s !== 'number' || !isFinite(s)) { scores[j].score = null; continue; }
            if (scale === 'fraction') s = s * 100;
            if (hasScores) s = Math.max(0, Math.min(100, s));
            scores[j].score = Math.round(s * 100) / 100;
        }
        result.hasScores = hasScores;
        result.scale = hasScores ? scale : 'none';
        return result;
    }

    function parseConfidence(data, format) {
        if (!data) return { scores: [], byChain: {}, hasScores: false, scale: 'none' };
        format = (format || 'pdb').toLowerCase();
        var parsed = (format === 'cif' || format === 'mmcif')
            ? parseConfidenceFromCIF(data)
            : parseConfidenceFromPDB(data);
        return finalizeConfidence(parsed, data, format);
    }

    /* True when the confidence data carries real per-residue pLDDT values. Objects
       built by callers (no hasScores flag) count when they have numeric scores. */
    function confidenceHasScores(cd) {
        if (!cd || !cd.scores || !cd.scores.length) return false;
        if (cd.hasScores === false) return false;
        return true;
    }

    /* pdbe-molstar visual.select() entry for a run of residues on one chain. Uses
       AUTHOR chain + residue numbers — the identifiers written in the file and the
       ones Mol* hover / picking report — so coloring never depends on how Mol*
       derives label_asym_id / label_seq_id (which differ from the file numbering for
       mmCIF with renamed chains, offsets or gaps). */
    function residueRunEntry(chain, start, end, color) {
        var e = { auth_asym_id: chain, color: color };
        if (start === end) e.auth_residue_number = start;
        else { e.start_auth_residue_number = start; e.end_auth_residue_number = end; }
        return e;
    }

    function entryAuthChain(s) { return s.authChain != null ? s.authChain : s.chain; }
    function entryAuthResi(s) { return s.authResi != null ? s.authResi : s.resi; }

    /* Turn [{entry, color}] into merged visual.select() runs: residues that are
       adjacent in the file (same chain, consecutive author numbers) and share a
       color collapse into one range so the payload stays small. Residues with a
       null color are skipped (they keep Mol*'s native/CPK color). */
    function buildRunsSelectData(colored) {
        var byChain = {};
        var order = [];
        for (var i = 0; i < colored.length; i++) {
            var c = colored[i];
            if (!c || !c.color) continue;
            var ch = entryAuthChain(c.entry);
            var r = entryAuthResi(c.entry);
            if (r == null || isNaN(r)) continue;
            if (!byChain[ch]) { byChain[ch] = []; order.push(ch); }
            byChain[ch].push({ resi: r, color: c.color });
        }
        var data = [];
        order.forEach(function (chain) {
            var cs = byChain[chain].sort(function (a, b) { return a.resi - b.resi; });
            var runStart = cs[0].resi, runEnd = cs[0].resi, runColor = cs[0].color;
            for (var j = 1; j < cs.length; j++) {
                if (cs[j].resi === runEnd) continue; // insertion-code duplicate
                if (cs[j].color === runColor && cs[j].resi === runEnd + 1) { runEnd = cs[j].resi; continue; }
                data.push(residueRunEntry(chain, runStart, runEnd, runColor));
                runStart = runEnd = cs[j].resi; runColor = cs[j].color;
            }
            data.push(residueRunEntry(chain, runStart, runEnd, runColor));
        });
        return data;
    }

    /* Build visual.select() data from parsed pLDDT scores (contiguous runs per band).
       color must be a hex string — pdbe-molstar visual.select() ignores RGB objects. */
    function buildPLDDTSelectData(scores) {
        return buildRunsSelectData((scores || []).map(function (s) {
            return { entry: s, color: (typeof s.score === 'number' && isFinite(s.score)) ? plddtColor(s.score) : null };
        }));
    }

    /* ---- Spectrum (rainbow) + hydrophobicity overpaints ---------------- */

    /* Kyte-Doolittle hydropathy index. Positive = hydrophobic, negative =
       hydrophilic. Residues outside the table (ligands, modified residues, NA)
       are left uncolored so they keep their element/CPK colors. */
    var KYTE_DOOLITTLE = {
        ILE: 4.5, VAL: 4.2, LEU: 3.8, PHE: 2.8, CYS: 2.5, MET: 1.9, ALA: 1.8,
        GLY: -0.4, THR: -0.7, SER: -0.8, TRP: -0.9, TYR: -1.3, PRO: -1.6,
        HIS: -3.2, GLU: -3.5, GLN: -3.5, ASP: -3.5, ASN: -3.5, LYS: -3.9, ARG: -4.5
    };

    /* Hydrophobicity ramp: teal (hydrophilic) → grey → orange (hydrophobic).
       t is normalised 0..1 over the Kyte-Doolittle range [-4.5, 4.5]. */
    var HYDRO_STOPS = [
        { t: 0.0, rgb: [ 45, 212, 191] },
        { t: 0.5, rgb: [148, 163, 184] },
        { t: 1.0, rgb: [249, 115,  22] }
    ];

    function toHex(r, g, b) {
        var f = function (v) { return ('0' + Math.max(0, Math.min(255, Math.round(v))).toString(16)).slice(-2); };
        return '#' + f(r) + f(g) + f(b);
    }

    function rampColor(stops, t) {
        t = Math.max(0, Math.min(1, t));
        for (var i = 1; i < stops.length; i++) {
            if (t <= stops[i].t) {
                var a = stops[i - 1], b = stops[i];
                var span = (b.t - a.t) || 1;
                var k = (t - a.t) / span;
                return toHex(
                    a.rgb[0] + (b.rgb[0] - a.rgb[0]) * k,
                    a.rgb[1] + (b.rgb[1] - a.rgb[1]) * k,
                    a.rgb[2] + (b.rgb[2] - a.rgb[2]) * k
                );
            }
        }
        var last = stops[stops.length - 1].rgb;
        return toHex(last[0], last[1], last[2]);
    }

    /* Rainbow N→C: hue sweeps 240° (blue, N-term) → 0° (red, C-term). */
    function spectrumColor(t) {
        var hue = (1 - Math.max(0, Math.min(1, t))) * 240;
        var c = 0.75, x = c * (1 - Math.abs(((hue / 60) % 2) - 1)), m = 0.15;
        var r = 0, g = 0, b = 0;
        if (hue < 60)       { r = c; g = x; }
        else if (hue < 120) { r = x; g = c; }
        else if (hue < 180) { g = c; b = x; }
        else if (hue < 240) { g = x; b = c; }
        else                { r = x; b = c; }
        return toHex((r + m) * 255, (g + m) * 255, (b + m) * 255);
    }

    /* Build visual.select() data by mapping every residue through colorFn(entry, ctx).
       Residues that map to null are omitted (keeping their native colors). Adjacent
       residues sharing a color are merged into a single range so the payload stays
       small even for large multi-chain assemblies. */
    function buildRampSelectData(byChain, colorFn) {
        var colored = [];
        Object.keys(byChain).forEach(function (chain) {
            var cs = (byChain[chain] || []).slice().sort(function (a, b) { return a.resi - b.resi; });
            if (!cs.length) return;
            var ctx = { chain: chain, first: cs[0].resi, last: cs[cs.length - 1].resi, count: cs.length };
            for (var i = 0; i < cs.length; i++) colored.push({ entry: cs[i], color: colorFn(cs[i], ctx, i) });
        });
        return buildRunsSelectData(colored);
    }

    /* Returns hex CSS color string for a pLDDT score value */
    function plddtColor(score) {
        return PLDDT[plddtBand(score)].css;
    }

    /* AlphaFold DB bands, shared by the 3D coloring, the legends and the charts:
       >=90 very high, 70-<90 confident, 50-<70 low, <50 very low. */
    function plddtBand(score) {
        if (score >= 90) return 'VERY_HIGH';
        if (score >= 70) return 'CONFIDENT';
        if (score >= 50) return 'LOW';
        return 'VERY_LOW';
    }

    function getResiduesInConfidenceRange(confidenceData, minScore, maxScore) {
        var result = {};
        var scores = confidenceData.scores || [];
        for (var i = 0; i < scores.length; i++) {
            var s = scores[i];
            if (typeof s.score === 'number' && s.score >= minScore && s.score < maxScore) {
                if (!result[s.chain]) result[s.chain] = [];
                result[s.chain].push(s.resi);
            }
        }
        return result;
    }

    function buildContiguousRanges(residueNumbers) {
        if (!residueNumbers || residueNumbers.length === 0) return [];
        var sorted = residueNumbers.slice().sort(function (a, b) { return a - b; });
        var ranges = [];
        var start = sorted[0], end = sorted[0];
        for (var i = 1; i < sorted.length; i++) {
            if (sorted[i] <= end + 3) { end = sorted[i]; }
            else { ranges.push({ start: start, end: end }); start = sorted[i]; end = sorted[i]; }
        }
        ranges.push({ start: start, end: end });
        return ranges;
    }

    function computeConfidenceStats(confidenceData) {
        var scores = confidenceHasScores(confidenceData) ? confidenceData.scores : [];
        var vals = scores.map(function (s) { return s.score; })
            .filter(function (v) { return typeof v === 'number' && isFinite(v); });
        if (vals.length === 0) return { mean: 0, median: 0, min: 0, max: 0, count: 0 };
        vals.sort(function (a, b) { return a - b; });
        var sum = 0;
        for (var i = 0; i < vals.length; i++) sum += vals[i];
        return {
            mean: Math.round(sum / vals.length * 10) / 10,
            median: vals[Math.floor(vals.length / 2)],
            min: vals[0],
            max: vals[vals.length - 1],
            count: vals.length
        };
    }

    /* ------------------------------------------------------------------ */
    /*  Focus Panel Builder (DOM-safe, no innerHTML)                       */
    /* ------------------------------------------------------------------ */

    function buildSectionTitle(text) {
        var div = mkEl('div', { 'class': 'pfp-section-title', text: text });
        return div;
    }

    function buildQuickBtn(action, color, label, title) {
        var btn = mkEl('button', { type: 'button', 'class': 'pfp-qbtn', 'data-action': action, title: title });
        var dot = mkEl('i', { style: 'background:' + color });
        btn.appendChild(dot);
        btn.appendChild(document.createTextNode(label));
        return btn;
    }

    function buildToolBtn(action, pathData, label, title) {
        var btn = mkEl('button', { type: 'button', 'class': 'pfp-tbtn', 'data-action': action, title: title });
        btn.appendChild(svgIcon(pathData));
        var span = mkEl('span', { text: label });
        btn.appendChild(span);
        return btn;
    }

    function buildCheckbox(dataAttr, dataVal, label, checked) {
        var lbl = mkEl('label', { 'class': 'pfp-check' });
        var cb = mkEl('input', { type: 'checkbox' });
        cb.setAttribute(dataAttr, dataVal);
        if (checked) cb.checked = true;
        lbl.appendChild(cb);
        lbl.appendChild(document.createTextNode(' ' + label));
        return lbl;
    }

    function buildColorBtn(colorVal, label, active) {
        var btn = mkEl('button', { type: 'button', 'class': 'pfp-cbtn' + (active ? ' active' : ''), 'data-color': colorVal, text: label });
        return btn;
    }

    function buildQualityBtn(qualityVal, label, active) {
        return mkEl('button', { type: 'button', 'class': 'pfp-cbtn' + (active ? ' active' : ''), 'data-quality': qualityVal, text: label });
    }

    function buildStyleBtn(styleVal, label, active) {
        return mkEl('button', { type: 'button', 'class': 'pfp-cbtn' + (active ? ' active' : ''), 'data-style': styleVal, text: label });
    }

    function t(key, fallback) {
        return window.i18n ? window.i18n.t('phonosViewer.' + key) : fallback;
    }

    /* Keyboard cheat sheet — rendered lazily into the viewer container. */
    var SHORTCUTS = [
        ['R', 'shortcutReset', 'Reset view'],
        ['S', 'shortcutSpin', 'Toggle spin'],
        ['P', 'shortcutPng', 'Save PNG'],
        ['⇧ P', 'shortcutPngTransparent', 'Save transparent PNG'],
        ['C', 'shortcutCopy', 'Copy image to clipboard'],
        ['B', 'shortcutBg', 'Cycle background'],
        ['Q', 'shortcutQuality', 'Cycle render quality'],
        ['F', 'shortcutFullscreen', 'Fullscreen'],
        ['Esc', 'shortcutClear', 'Clear focus'],
        ['?', 'shortcutHelp', 'Show this help']
    ];

    function buildShortcutSheet() {
        var sheet = mkEl('div', { 'class': 'pfp-shortcuts' });
        var head = mkEl('div', { 'class': 'pfp-shortcuts-head' });
        head.appendChild(mkEl('span', { text: t('keyboardShortcutsTitle', 'Keyboard Shortcuts') }));
        var close = mkEl('button', { type: 'button', 'class': 'pfp-shortcuts-close', 'data-action': 'close-shortcuts', text: '×' });
        head.appendChild(close);
        sheet.appendChild(head);
        var list = mkEl('div', { 'class': 'pfp-shortcuts-list' });
        SHORTCUTS.forEach(function (row) {
            var item = mkEl('div', { 'class': 'pfp-shortcut' });
            item.appendChild(mkEl('kbd', { text: row[0] }));
            item.appendChild(mkEl('span', { text: t(row[1], row[2]) }));
            list.appendChild(item);
        });
        sheet.appendChild(list);
        var hint = mkEl('div', { 'class': 'pfp-shortcuts-hint', text: t('mouseHints', 'Drag to rotate · Scroll to zoom · Shift+drag to pan') });
        sheet.appendChild(hint);
        return sheet;
    }

    function buildFocusPanel(options) {
        var panel = mkEl('div', { 'class': 'phonos-focus-panel' });

        // Header
        var header = mkEl('div', { 'class': 'pfp-header', 'data-action': 'toggle-panel' });
        header.appendChild(svgIcon([{ tag: 'circle', cx: '11', cy: '11', r: '8' }, 'M21 21l-4.35-4.35']));
        var focusAnalysisLabel = window.i18n ? window.i18n.t('phonosViewer.focusAnalysis') : 'Focus & Analysis';
        header.appendChild(mkEl('span', { text: focusAnalysisLabel }));
        var collapseBtn = mkEl('button', { 'class': 'pfp-collapse-btn', 'data-action': 'toggle-panel', type: 'button' });
        collapseBtn.appendChild(svgIcon(['M15 18L9 12L15 6'], 12, 12));
        header.appendChild(collapseBtn);
        panel.appendChild(header);

        var body = mkEl('div', { 'class': 'pfp-body' });

        // --- Confidence Overview ---
        var confSection = mkEl('div', { 'class': 'pfp-section', id: 'pfp-confidence-section', style: 'display:none' });
        var confidenceOverviewLabel = window.i18n ? window.i18n.t('phonosViewer.confidenceOverview') : 'Confidence Overview';
        confSection.appendChild(buildSectionTitle(confidenceOverviewLabel));
        confSection.appendChild(mkEl('div', { 'class': 'pfp-confidence-bar', id: 'pfp-confidence-bar' }));
        confSection.appendChild(mkEl('div', { 'class': 'pfp-confidence-stats', id: 'pfp-confidence-stats' }));

        var legend = mkEl('div', { 'class': 'pfp-confidence-legend' });
        [['#0053d6', '>90'], ['#65cbf3', '70-90'], ['#ffdb13', '50-70'], ['#ff7d45', '<50']].forEach(function (pair) {
            var span = mkEl('span');
            span.appendChild(mkEl('i', { style: 'background:' + pair[0] }));
            span.appendChild(document.createTextNode(pair[1]));
            legend.appendChild(span);
        });
        confSection.appendChild(legend);
        body.appendChild(confSection);

        // --- Chains (filled in after a structure loads; hidden while empty) ---
        var chainSection = mkEl('div', { 'class': 'pfp-section', id: 'pfp-chain-section', style: 'display:none' });
        chainSection.appendChild(buildSectionTitle(t('chains', 'Chains')));
        chainSection.appendChild(mkEl('div', { 'class': 'pfp-chain-chips', id: 'pfp-chain-chips' }));
        chainSection.appendChild(mkEl('div', {
            'class': 'pfp-hint',
            text: t('chainChipHint', 'Click to focus · Shift+click to isolate')
        }));
        body.appendChild(chainSection);

        // --- Region Focus ---
        var regionSection = mkEl('div', { 'class': 'pfp-section' });
        var regionFocusLabel = window.i18n ? window.i18n.t('phonosViewer.regionFocus') : 'Region Focus';
        regionSection.appendChild(buildSectionTitle(regionFocusLabel));

        var fieldRow = mkEl('div', { 'class': 'pfp-field-row' });
        var f1 = mkEl('div', { 'class': 'pfp-field' });
        var chainLabel = window.i18n ? window.i18n.t('phonosViewer.chain') : 'Chain';
        f1.appendChild(mkEl('label', { 'class': 'pfp-label', text: chainLabel }));
        f1.appendChild(mkEl('input', { type: 'text', 'class': 'pfp-input', id: 'pfp-chain', value: 'A', maxlength: '4', spellcheck: 'false' }));
        fieldRow.appendChild(f1);

        var f2 = mkEl('div', { 'class': 'pfp-field' });
        var fromLabel = window.i18n ? window.i18n.t('phonosViewer.from') : 'From';
        f2.appendChild(mkEl('label', { 'class': 'pfp-label', text: fromLabel }));
        f2.appendChild(mkEl('input', { type: 'number', 'class': 'pfp-input', id: 'pfp-from', min: '1', placeholder: '1' }));
        fieldRow.appendChild(f2);

        var f3 = mkEl('div', { 'class': 'pfp-field' });
        var toLabel = window.i18n ? window.i18n.t('phonosViewer.to') : 'To';
        f3.appendChild(mkEl('label', { 'class': 'pfp-label', text: toLabel }));
        f3.appendChild(mkEl('input', { type: 'number', 'class': 'pfp-input', id: 'pfp-to', placeholder: 'end' }));
        fieldRow.appendChild(f3);
        regionSection.appendChild(fieldRow);

        var btnRow = mkEl('div', { 'class': 'pfp-btn-row' });
        var focusBtnLabel = window.i18n ? window.i18n.t('phonosViewer.focus') : 'Focus';
        btnRow.appendChild(mkEl('button', { type: 'button', 'class': 'pfp-btn pfp-btn-accent', 'data-action': 'focus-region', text: focusBtnLabel }));
        var isolateBtnLabel = window.i18n ? window.i18n.t('phonosViewer.isolate') : 'Isolate';
        btnRow.appendChild(mkEl('button', { type: 'button', 'class': 'pfp-btn', 'data-action': 'isolate-region', text: isolateBtnLabel }));
        var resetBtnLabel = window.i18n ? window.i18n.t('phonosViewer.reset') : 'Reset';
        btnRow.appendChild(mkEl('button', { type: 'button', 'class': 'pfp-btn', 'data-action': 'clear-focus', text: resetBtnLabel }));
        regionSection.appendChild(btnRow);
        body.appendChild(regionSection);

        // --- Quick Focus ---
        var quickSection = mkEl('div', { 'class': 'pfp-section' });
        var quickFocusLabel = window.i18n ? window.i18n.t('phonosViewer.quickFocus') : 'Quick Focus';
        quickSection.appendChild(buildSectionTitle(quickFocusLabel));
        var qGrid = mkEl('div', { 'class': 'pfp-quick-grid' });
        qGrid.appendChild(buildQuickBtn('focus-high-conf', '#0053d6',
          window.i18n ? window.i18n.t('phonosViewer.highConf') : 'High Conf',
          window.i18n ? window.i18n.t('phonosViewer.highConfTooltip') : 'Zoom to pLDDT > 90'));
        qGrid.appendChild(buildQuickBtn('focus-low-conf', '#ff7d45',
          window.i18n ? window.i18n.t('phonosViewer.lowConf') : 'Low Conf',
          window.i18n ? window.i18n.t('phonosViewer.lowConfTooltip') : 'Zoom to pLDDT < 50'));
        qGrid.appendChild(buildQuickBtn('focus-medium-conf', '#ffdb13',
          window.i18n ? window.i18n.t('phonosViewer.medium') : 'Medium',
          window.i18n ? window.i18n.t('phonosViewer.mediumTooltip') : 'Zoom to pLDDT 50-70'));
        qGrid.appendChild(buildQuickBtn('focus-ligands', '#10b981',
          window.i18n ? window.i18n.t('phonosViewer.ligands') : 'Ligands',
          window.i18n ? window.i18n.t('phonosViewer.ligandsTooltip') : 'Show ligands'));
        qGrid.appendChild(buildQuickBtn('focus-termini', '#f59e0b',
          window.i18n ? window.i18n.t('phonosViewer.termini') : 'Termini',
          window.i18n ? window.i18n.t('phonosViewer.terminiTooltip') : 'Focus N/C termini'));
        qGrid.appendChild(buildQuickBtn('show-all', '#a1a1aa',
          window.i18n ? window.i18n.t('phonosViewer.showAll') : 'Show All',
          window.i18n ? window.i18n.t('phonosViewer.showAllTooltip') : 'Show full structure'));
        quickSection.appendChild(qGrid);
        body.appendChild(quickSection);

        // --- Visibility ---
        var visSection = mkEl('div', { 'class': 'pfp-section' });
        var visibilityLabel = window.i18n ? window.i18n.t('phonosViewer.visibility') : 'Visibility';
        visSection.appendChild(buildSectionTitle(visibilityLabel));
        var visGrid = mkEl('div', { 'class': 'pfp-check-grid' });
        visGrid.appendChild(buildCheckbox('data-vis', 'polymer', window.i18n ? window.i18n.t('phonosViewer.proteinNa') : 'Protein/NA', true));
        visGrid.appendChild(buildCheckbox('data-vis', 'het', window.i18n ? window.i18n.t('phonosViewer.ligands') : 'Ligands', true));
        visGrid.appendChild(buildCheckbox('data-vis', 'water', window.i18n ? window.i18n.t('phonosViewer.water') : 'Water', false));
        visGrid.appendChild(buildCheckbox('data-vis', 'carbs', window.i18n ? window.i18n.t('phonosViewer.carbs') : 'Carbs', false));
        visSection.appendChild(visGrid);
        body.appendChild(visSection);

        // --- Representation ---
        var styleSection = mkEl('div', { 'class': 'pfp-section' });
        styleSection.appendChild(buildSectionTitle(t('representation', 'Representation')));
        var styleRow = mkEl('div', { 'class': 'pfp-color-row pfp-style-row' });
        var activeStyle = normalizeVisualStyle(options.visualStyle || 'cartoon');
        [
            ['cartoon', t('styleCartoon', 'Cartoon')],
            ['stick', t('styleStick', 'Stick')],
            ['sphere', t('styleSphere', 'Sphere')],
            ['surface', t('styleSurface', 'Surface')],
            ['line', t('styleLine', 'Line')]
        ].forEach(function (pair) {
            styleRow.appendChild(buildStyleBtn(pair[0], pair[1], normalizeVisualStyle(pair[0]) === activeStyle));
        });
        styleSection.appendChild(styleRow);
        body.appendChild(styleSection);

        // --- Coloring ---
        var colorSection = mkEl('div', { 'class': 'pfp-section' });
        var coloringLabel = window.i18n ? window.i18n.t('phonosViewer.coloring') : 'Coloring';
        colorSection.appendChild(buildSectionTitle(coloringLabel));
        var colorRow = mkEl('div', { 'class': 'pfp-color-row' });
        colorRow.appendChild(buildColorBtn('default', window.i18n ? window.i18n.t('phonosViewer.defaultColor') : 'Default', true));
        colorRow.appendChild(buildColorBtn('confidence', window.i18n ? window.i18n.t('phonosViewer.plddt') : 'pLDDT', false));
        colorRow.appendChild(buildColorBtn('chain', window.i18n ? window.i18n.t('phonosViewer.chainColor') : 'Chain', false));
        colorRow.appendChild(buildColorBtn('spectrum', t('rainbow', 'Rainbow'), false));
        colorRow.appendChild(buildColorBtn('hydrophobicity', t('hydrophobicity', 'Hydrophob.'), false));
        colorSection.appendChild(colorRow);
        // Legend for whichever ramp scheme is active — hidden until one is picked.
        colorSection.appendChild(mkEl('div', { 'class': 'pfp-ramp-legend', id: 'pfp-ramp-legend', style: 'display:none' }));
        body.appendChild(colorSection);

        // --- Rendering quality ---
        var renderSection = mkEl('div', { 'class': 'pfp-section' });
        var renderingLabel = window.i18n ? window.i18n.t('phonosViewer.rendering') : 'Rendering';
        renderSection.appendChild(buildSectionTitle(renderingLabel));
        var renderRow = mkEl('div', { 'class': 'pfp-color-row pfp-quality-row' });
        var activeQuality = options.renderQuality || DEFAULT_RENDER_QUALITY;
        renderRow.appendChild(buildQualityBtn('standard', window.i18n ? window.i18n.t('phonosViewer.standard') : 'Standard', activeQuality === 'standard'));
        renderRow.appendChild(buildQualityBtn('illustrative', window.i18n ? window.i18n.t('phonosViewer.illustrative') : 'Illustr.', activeQuality === 'illustrative'));
        renderRow.appendChild(buildQualityBtn('cinematic', window.i18n ? window.i18n.t('phonosViewer.cinematic') : 'Cinematic', activeQuality === 'cinematic'));
        renderRow.appendChild(buildQualityBtn('flat', window.i18n ? window.i18n.t('phonosViewer.flat') : 'Flat', activeQuality === 'flat'));
        renderSection.appendChild(renderRow);
        body.appendChild(renderSection);

        // --- Panels ---
        var panelSection = mkEl('div', { 'class': 'pfp-section' });
        var panelsLabel = window.i18n ? window.i18n.t('phonosViewer.panels') : 'Panels';
        panelSection.appendChild(buildSectionTitle(panelsLabel));
        var panelGrid = mkEl('div', { 'class': 'pfp-check-grid' });
        panelGrid.appendChild(buildCheckbox('data-panel', 'sequence', window.i18n ? window.i18n.t('phonosViewer.sequenceViewer') : 'Sequence Viewer', !!options.sequencePanel));
        panelGrid.appendChild(buildCheckbox('data-panel', 'controls', window.i18n ? window.i18n.t('phonosViewer.molStarControls') : 'Mol* Controls', false));
        panelSection.appendChild(panelGrid);
        body.appendChild(panelSection);

        // --- Tools ---
        var toolSection = mkEl('div', { 'class': 'pfp-section' });
        var toolsLabel = window.i18n ? window.i18n.t('phonosViewer.tools') : 'Tools';
        toolSection.appendChild(buildSectionTitle(toolsLabel));
        var toolRow = mkEl('div', { 'class': 'pfp-tool-row' });
        toolRow.appendChild(buildToolBtn('spin', ['M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.3'],
          window.i18n ? window.i18n.t('phonosViewer.spin') : 'Spin',
          window.i18n ? window.i18n.t('phonosViewer.spinTooltip') : 'Toggle spin (S)'));
        toolRow.appendChild(buildToolBtn('measure-distance', ['M4 20L20 4', { tag: 'circle', cx: '4', cy: '20', r: '2' }, { tag: 'circle', cx: '20', cy: '4', r: '2' }],
          window.i18n ? window.i18n.t('phonosViewer.measureDistance') : 'Distance',
          window.i18n ? window.i18n.t('phonosViewer.measureDistanceTooltip') : 'Measure distance: click two atoms'));
        toolRow.appendChild(buildToolBtn('measure-angle', ['M4 20L12 6L20 20', 'M4 20L20 20'],
          window.i18n ? window.i18n.t('phonosViewer.measureAngle') : 'Angle',
          window.i18n ? window.i18n.t('phonosViewer.measureAngleTooltip') : 'Measure angle: click three atoms'));
        toolRow.appendChild(buildToolBtn('clear-measurements', ['M18 6L6 18', 'M6 6l12 12'],
          window.i18n ? window.i18n.t('phonosViewer.clearMeasurements') : 'Clear',
          window.i18n ? window.i18n.t('phonosViewer.clearMeasurementsTooltip') : 'Clear all measurements'));
        toolRow.appendChild(buildToolBtn('screenshot', [{ tag: 'rect', x: '3', y: '3', width: '18', height: '18', rx: '2' }, 'M21 15l-5-5L5 21'],
          window.i18n ? window.i18n.t('phonosViewer.png') : 'PNG',
          window.i18n ? window.i18n.t('phonosViewer.pngTooltip') : 'Screenshot (P)'));
        toolRow.appendChild(buildToolBtn('copy-screenshot', [{ tag: 'rect', x: '9', y: '9', width: '13', height: '13', rx: '2', ry: '2' }, 'M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'],
          window.i18n ? window.i18n.t('phonosViewer.copy') : 'Copy',
          window.i18n ? window.i18n.t('phonosViewer.copyTooltip') : 'Copy screenshot (C)'));
        toolRow.appendChild(buildToolBtn('screenshot-transparent', [{ tag: 'rect', x: '3', y: '3', width: '18', height: '18', rx: '2' }, 'M3 3l18 18', 'M21 15l-5-5L5 21'],
          window.i18n ? window.i18n.t('phonosViewer.pngTransparent') : 'PNG+',
          window.i18n ? window.i18n.t('phonosViewer.pngTransparentTooltip') : 'Transparent hi-res PNG'));
        toolRow.appendChild(buildToolBtn('background', ['M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42', { tag: 'circle', cx: '12', cy: '12', r: '5' }],
          window.i18n ? window.i18n.t('phonosViewer.bg') : 'BG',
          window.i18n ? window.i18n.t('phonosViewer.bgTooltip') : 'Cycle background (B)'));
        toolRow.appendChild(buildToolBtn('fullscreen', ['M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3'],
          window.i18n ? window.i18n.t('phonosViewer.full') : 'Full',
          window.i18n ? window.i18n.t('phonosViewer.fullTooltip') : 'Fullscreen (F)'));
        toolRow.appendChild(buildToolBtn('reset-view', ['M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5'],
          window.i18n ? window.i18n.t('phonosViewer.resetView') : 'Reset',
          window.i18n ? window.i18n.t('phonosViewer.resetViewTooltip') : 'Reset view (R)'));
        toolRow.appendChild(buildToolBtn('export-structure', ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'M7 10l5 5 5-5', 'M12 15V3'],
          t('exportStructure', 'Save'), t('exportStructureTooltip', 'Download the loaded structure file')));
        toolRow.appendChild(buildToolBtn('shortcuts', [{ tag: 'circle', cx: '12', cy: '12', r: '10' }, 'M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3', 'M12 17h.01'],
          t('help', 'Keys'), t('helpTooltip', 'Keyboard shortcuts (?)')));
        toolSection.appendChild(toolRow);
        body.appendChild(toolSection);

        panel.appendChild(body);
        return panel;
    }

    /* ------------------------------------------------------------------ */
    /*  Confidence Bar Renderer (DOM-safe)                                 */
    /* ------------------------------------------------------------------ */

    function renderConfidenceBar(container, confidenceData) {
        if (!container) return;
        var scores = confidenceHasScores(confidenceData) ? confidenceData.scores : [];
        var section = container.parentElement;

        if (scores.length === 0) {
            if (section) section.style.display = 'none';
            return;
        }
        if (section) section.style.display = '';

        // Clear existing
        while (container.firstChild) container.removeChild(container.firstChild);

        var track = mkEl('div', { 'class': 'pfp-cbar-track' });
        var total = scores.length;
        var widthPct = 100 / total;

        for (var i = 0; i < total; i++) {
            var s = scores[i].score;
            var color = (typeof s === 'number') ? plddtColor(s) : '#3f3f46';

            var seg = mkEl('div', {
                'class': 'pfp-cbar-seg',
                style: 'background:' + color + ';width:' + widthPct + '%',
                title: scores[i].chain + ':' + scores[i].resi + ' pLDDT=' + Math.round(s)
            });
            track.appendChild(seg);
        }
        container.appendChild(track);

        // Stats
        var stats = computeConfidenceStats(confidenceData);
        var statsEl = section ? section.querySelector('#pfp-confidence-stats') : null;
        if (statsEl) {
            var statsText = window.i18n ? window.i18n.t('phonosViewer.confidenceStats') : 'Mean: {mean}  Median: {median}  Range: {min}-{max}  Residues: {count}';
            statsEl.textContent = statsText.replace('{mean}', stats.mean).replace('{median}', stats.median).replace('{min}', Math.round(stats.min)).replace('{max}', Math.round(stats.max)).replace('{count}', stats.count);
        }
    }

    /* ------------------------------------------------------------------ */
    /*  Toast (DOM-safe)                                                   */
    /* ------------------------------------------------------------------ */

    function showToast(viewer, msg) {
        var toast = viewer._toast;
        if (!toast) {
            toast = mkEl('div', { 'class': 'pfp-toast' });
            viewer.container.appendChild(toast);
            viewer._toast = toast;
        }
        toast.textContent = msg;
        toast.classList.add('visible');
        clearTimeout(viewer._toastTimer);
        viewer._toastTimer = setTimeout(function () {
            toast.classList.remove('visible');
        }, 2000);
    }

    /* ------------------------------------------------------------------ */
    /*  SDF → HETATM PDB Converter                                        */
    /* ------------------------------------------------------------------ */

    /**
     * Infer connectivity for SDF/MOL files missing a bond block.
     * Uses standard covalent radii + tolerance to detect inter-atomic bonds.
     */
    function inferBondsForSDF(sdfData) {
        if (!sdfData || typeof sdfData !== 'string') return sdfData;
        var lines = sdfData.replace(/\r\n/g, '\n').split('\n');
        var countsIdx = -1;
        // Find the counts line (typically line 4 in V2000)
        for (var i = 3; i < Math.min(lines.length, 10); i++) {
            if (/^\s*\d+\s+\d+/.test(lines[i])) { countsIdx = i; break; }
        }
        if (countsIdx < 0) return sdfData;

        var atomCount = parseInt(lines[countsIdx].substring(0, 3).trim(), 10);
        var bondCount = parseInt(lines[countsIdx].substring(3, 6).trim(), 10);
        if (isNaN(atomCount) || atomCount <= 1 || bondCount > 0) return sdfData;

        // Extract atom coordinates and elements
        var atoms = [];
        var COVALENT_RADII = {
            'H': 0.31, 'HE': 0.28,
            'LI': 1.28, 'BE': 0.96, 'B': 0.84, 'C': 0.76, 'N': 0.71, 'O': 0.66, 'F': 0.57, 'NE': 0.58,
            'NA': 1.66, 'MG': 1.41, 'AL': 1.21, 'SI': 1.11, 'P': 1.07, 'S': 1.05, 'CL': 1.02, 'AR': 1.06,
            'K': 2.03, 'CA': 1.76, 'ZN': 1.22, 'BR': 1.20, 'I': 1.39
        };
        for (var a = 0; a < atomCount; a++) {
            var lineIdx = countsIdx + 1 + a;
            if (lineIdx >= lines.length) break;
            var line = lines[lineIdx];
            if (line.length < 34) continue;
            var x = parseFloat(line.substring(0, 10));
            var y = parseFloat(line.substring(10, 20));
            var z = parseFloat(line.substring(20, 30));
            var sym = line.substring(31, 34).trim().toUpperCase();
            atoms.push({ x: x, y: y, z: z, r: COVALENT_RADII[sym] || 0.77 });
        }
        if (atoms.length < 2) return sdfData;

        // Simple N^2 bond inference (typically < 100 atoms for ligands)
        var inferredBonds = [];
        var TOLERANCE = 0.45; // Angstroms
        for (var i = 0; i < atoms.length; i++) {
            for (var j = i + 1; j < atoms.length; j++) {
                var a1 = atoms[i], a2 = atoms[j];
                var dx = a1.x - a2.x, dy = a1.y - a2.y, dz = a1.z - a2.z;
                var distSq = dx * dx + dy * dy + dz * dz;
                var maxDist = a1.r + a2.r + TOLERANCE;
                if (distSq <= maxDist * maxDist && distSq >= 0.16) { // min 0.4A distance
                    inferredBonds.push({ a1: i + 1, a2: j + 1 });
                }
            }
        }
        if (inferredBonds.length === 0) return sdfData;

        // Reconstruct SDF with new bond count and bond block
        var newLines = lines.slice(0, countsIdx);
        var countsLine = lines[countsIdx];
        var newCounts = String(atomCount).padStart(3) + String(inferredBonds.length).padStart(3) + countsLine.substring(6);
        newLines.push(newCounts);

        // Atoms
        for (var a = 0; a < atomCount; a++) {
            newLines.push(lines[countsIdx + 1 + a]);
        }

        // Bonds
        for (var b = 0; b < inferredBonds.length; b++) {
            var bond = inferredBonds[b];
            newLines.push(String(bond.a1).padStart(3) + String(bond.a2).padStart(3) + '  1  0  0  0  0');
        }

        // Remainder
        var skipIdx = countsIdx + 1 + atomCount + bondCount;
        for (var k = skipIdx; k < lines.length; k++) {
            newLines.push(lines[k]);
        }

        return newLines.join('\n');
    }

    /* Converts an SDF/MOL V2000 molecule to PDB HETATM + CONECT records.
       Returns the block as a string (without END), or '' on failure. */
    function sdfToHetAtmPdb(sdfData, chainId, resName) {
        chainId = (chainId || 'Z').charAt(0);
        resName = (resName || 'LIG').padEnd(3, ' ').substring(0, 3);

        // Infer bonds if missing (e.g. Boltz2 worker extracts only atoms)
        sdfData = inferBondsForSDF(sdfData);

        var lines = sdfData.replace(/\r\n/g, '\n').split('\n');
        // Find the counts line: first line after the 3-line header that matches the pattern
        var countsIdx = -1;
        for (var i = 3; i < Math.min(lines.length, 8); i++) {
            if (/^\s*\d+\s+\d+/.test(lines[i])) { countsIdx = i; break; }
        }
        if (countsIdx < 0) return '';

        var atomCount = parseInt(lines[countsIdx].substring(0, 3).trim(), 10);
        var bondCount = parseInt(lines[countsIdx].substring(3, 6).trim(), 10);
        if (isNaN(atomCount) || atomCount <= 0 || atomCount > 9999) return '';
        bondCount = isNaN(bondCount) ? 0 : bondCount;

        var hetatmLines = '';
        var serialOffset = 1; // PDB serial numbers start at 1
        for (var a = 0; a < atomCount; a++) {
            var lineIdx = countsIdx + 1 + a;
            if (lineIdx >= lines.length) break;
            var line = lines[lineIdx];
            if (line.length < 34) continue;

            var x = parseFloat(line.substring(0, 10));
            var y = parseFloat(line.substring(10, 20));
            var z = parseFloat(line.substring(20, 30));
            var sym = line.substring(31, 34).trim();
            if (isNaN(x) || isNaN(y) || isNaN(z) || !sym) continue;

            var serial = String(a + serialOffset).padStart(5);
            var aname = (' ' + sym.charAt(0).toUpperCase() + (sym.length > 1 ? sym.charAt(1).toLowerCase() : ' ') + String(a + 1)).padEnd(4, ' ').substring(0, 4);
            var xf = x.toFixed(3).padStart(8);
            var yf = y.toFixed(3).padStart(8);
            var zf = z.toFixed(3).padStart(8);
            var elem = sym.length > 1
                ? (sym.charAt(0).toUpperCase() + sym.charAt(1).toLowerCase()).padStart(2)
                : (' ' + sym.toUpperCase()).padStart(2);

            // HETATM sssssdaaaa lrrr cnnnn   xxxxxxxx yyyyyyyy zzzzzzzz oo.oo bb.bb      ee
            hetatmLines += 'HETATM' + serial + ' ' + aname + ' ' + resName + ' ' + chainId + '   1    ' +
                           xf + yf + zf + '  1.00  0.00          ' + elem + '\n';
        }
        if (!hetatmLines) return '';

        // Parse bond block and emit CONECT records (gives Mol* explicit connectivity)
        var conectLines = '';
        for (var b = 0; b < bondCount; b++) {
            var bIdx = countsIdx + 1 + atomCount + b;
            if (bIdx >= lines.length) break;
            var bl = lines[bIdx];
            if (bl.length < 6) continue;
            var a1 = parseInt(bl.substring(0, 3).trim(), 10);
            var a2 = parseInt(bl.substring(3, 6).trim(), 10);
            if (isNaN(a1) || isNaN(a2) || a1 < 1 || a2 < 1 || a1 > atomCount || a2 > atomCount) continue;
            conectLines += 'CONECT' + String(a1).padStart(5) + String(a2).padStart(5) + '\n';
        }

        return hetatmLines + 'TER\n' + conectLines;
    }

    /* ------------------------------------------------------------------ */
    /*  PhonosViewer Class                                                 */
    /* ------------------------------------------------------------------ */

    function PhonosViewer(container, options) {
        this.container = el(container);
        this.options = options || {};
        this.instance = null;
        this.confidenceData = null;
        this._blobUrls = [];
        this._initialized = false;
        this._spinning = false;
        this._bgIndex = 0;
        this._panel = null;
        this._shortcutSheet = null;
        this._toast = null;
        this._toastTimer = null;
        this._rawData = null;
        this._rawFormat = null;
        this._destroyed = false;
        this._loadedCallbacks = [];
        this._visualStyle = normalizeVisualStyle(this.options.visualStyle || 'cartoon');
        this._pendingVisualUpdate = Promise.resolve();
        this._hasLoadedStructure = false;
        this._currentSource = null;
        // Ligands/second structures added via the raw Mol* builder API (CIF / mol2
        // base, where they can't be inlined into _rawData). _currentSource does NOT
        // include them, so they must be re-appended after any reload of the base
        // structure (restyle / recolor) or they silently vanish.
        this._appendedRaw = [];
        // True when the current base structure embeds a HETATM ligand (or is a CIF
        // complex) and therefore needs an explicit ball-and-stick representation
        // forced on top of the cartoon preset. Set by loadData(); consumed by
        // _reloadCurrentStructure() so the ligand survives every restyle / recolor
        // (a plain visual.update rebuild only restores _appendedRaw, not this).
        this._forceBallStick = false;
        this._currentAlphaFoldView = !!this.options.alphafoldView;
        this._visualStyleDefault = !this.options.visualStyle;
        // ---- Active color scheme (persisted across restyle reloads & protein switches) ----
        // A plain visual.update(fullLoad) rebuild wipes the visual.select() overpaint that
        // confidence / chain / custom coloring relies on, so every restyle, recolor, ligand
        // append and model switch would silently revert the structure to Mol*'s native
        // colors. We remember the active scheme and re-assert it after each reload.
        //   _colorScheme:     'confidence' | 'chain' | 'default' | 'custom' | null (=auto)
        //   _colorUserSet:    true once the user explicitly picks a scheme — until then a
        //                     freshly loaded structure defaults to pLDDT when it has scores.
        //   _customSelectData: the raw select() payload for a 'custom' (rainbow/hydrophobicity)
        //                     scheme, so it can be replayed after a same-structure restyle.
        this._colorScheme = null;
        //   _rampScheme:      'spectrum' | 'hydrophobicity' | null — which built-in ramp
        //                     produced the current 'custom' overpaint, so it can be
        //                     rebuilt (not replayed) when the structure changes.
        this._rampScheme = null;
        this._colorUserSet = false;
        this._customSelectData = null;
        // When false, a structure with confidence scores is NOT auto-colored by pLDDT on
        // load (the caller drives coloring itself). Defaults on so every embedding —
        // including the file manager, which never re-applies color — gets the default.
        this._autoConfidenceColor = this.options.autoConfidenceColor !== false;
        this._renderQuality = RENDER_QUALITY[this.options.renderQuality]
            ? this.options.renderQuality : DEFAULT_RENDER_QUALITY;
        this._overlay = null;
        this._overlayMsg = null;
        this._loadingTimer = null;

        try {
            var saved = localStorage.getItem('phonos_viewer_bg');
            if (saved !== null) this._bgIndex = parseInt(saved, 10) || 0;
            var savedQ = localStorage.getItem('phonos_viewer_quality');
            if (savedQ && RENDER_QUALITY[savedQ] && this.options.renderQuality === undefined) {
                this._renderQuality = savedQ;
            }
        } catch (e) {}
    }

    /* ---- Init -------------------------------------------------------- */

    PhonosViewer.prototype.init = function () {
        var self = this;

        if (!this.container) throw new Error('[PhonosViewer] Container not found');
        if (!global.PDBeMolstarPlugin) throw new Error('[PhonosViewer] PDBeMolstarPlugin not loaded');

        try {
            this.instance = new global.PDBeMolstarPlugin();
        } catch (e) {
            console.error('[PhonosViewer] PDBeMolstarPlugin constructor failed:', e);
            throw e;
        }

        var bg = BACKGROUNDS[this._bgIndex] ? BACKGROUNDS[this._bgIndex].rgb : BG_DARK;

        var opts = {
            bgColor: this.options.bgColor || bg,
            hideControls: this.options.hideControls !== false,
            // Hide ALL Mol* canvas buttons — PhonosViewer provides its own controls
            hideCanvasControls: this.options.hideCanvasControls ||
                ['animation', 'controlToggle', 'controlInfo', 'expand', 'screenshot', 'selection'],
            // Auto-enable sequence panel for full-featured viewers (allows click-to-highlight)
            sequencePanel: this.options.sequencePanel !== undefined
                ? !!this.options.sequencePanel
                : (this.options.focusPanel === true),
            leftPanel: this.options.leftPanel === undefined ? false : !!this.options.leftPanel,
            rightPanel: this.options.rightPanel === undefined ? false : !!this.options.rightPanel,
            pdbeLink: this.options.pdbeLink === undefined ? false : !!this.options.pdbeLink,
            visualStyle: this._visualStyle || undefined,
            landscape: true,
            reactive: true,
            subscribeEvents: true,
            granularity: this.options.granularity || 'residue',
            encoding: 'cif'
        };

        if (this.options.alphafoldView) opts.alphafoldView = true;
        if (this.options.moleculeId) opts.moleculeId = this.options.moleculeId;
        if (this.options.customData) opts.customData = this.options.customData;
        if (this.options.ligandView) opts.ligandView = this.options.ligandView;
        if (this.options.superposition) opts.superposition = true;

        try {
            this.instance.render(this.container, opts);
        } catch (e) {
            console.error('[PhonosViewer] render() failed:', e);
            throw e;
        }
        this._initialized = true;

        // Build focus panel
        if (this.options.focusPanel === true) {
            this._buildPanel();
        }

        this._setupKeyboard();
        this._setupEvents();

        // Listen for load complete
        if (this.instance.events && this.instance.events.loadComplete) {
            this.instance.events.loadComplete.subscribe(function () {
                self._hideLoading();
                try { self._applyRenderQuality(); } catch (e) {
                    console.warn('[PhonosViewer] post-processing apply failed, skipping:', e);
                }
                // Ensure camera is centered on loaded structure
                try {
                    var plg = self.instance && self.instance.plugin;
                    if (plg && plg.canvas3d && plg.canvas3d.requestCameraReset) {
                        plg.canvas3d.requestCameraReset();
                    }
                } catch (e) {}
                for (var i = 0; i < self._loadedCallbacks.length; i++) {
                    try { self._loadedCallbacks[i](); } catch (e) {}
                }
            });
        }

        return this;
    };

    /* ---- Render quality (post-processing) ---------------------------- */

    /* Set the canvas post-processing preset: 'flat' | 'standard' |
       'illustrative' | 'cinematic'. Persists the choice and applies it. */
    PhonosViewer.prototype.setRenderQuality = function (preset) {
        this._renderQuality = RENDER_QUALITY[preset] ? preset : DEFAULT_RENDER_QUALITY;
        try { localStorage.setItem('phonos_viewer_quality', this._renderQuality); } catch (e) {}
        this._applyRenderQuality();
        showToast(this, (window.i18n ? window.i18n.t('phonosViewer.renderingLabel') : 'Rendering: ') + RENDER_QUALITY[this._renderQuality].label);
        return this._renderQuality;
    };

    PhonosViewer.prototype.getRenderQuality = function () { return this._renderQuality; };

    PhonosViewer.prototype._applyRenderQuality = function () {
        var plugin = this.instance && this.instance.plugin;
        var c3d = plugin && plugin.canvas3d;
        if (!c3d || !c3d.setProps) return false;
        try {
            var desired = buildCanvas3DProps(this._renderQuality).postprocessing || {};
            // Different pdbe-molstar / Mol* builds expose different post-processing
            // params (e.g. `shadow` only exists in newer Mol*). setProps-ing a key the
            // build doesn't know throws "can't access property 'name'" deep in Mol*.
            // So only send keys that already exist in the live props; when we can't
            // introspect, include the long-stable ones (occlusion/outline) but never
            // the newer `shadow`.
            var currentPP = c3d.props && c3d.props.postprocessing;
            var pp = {};
            Object.keys(desired).forEach(function (k) {
                var exists = currentPP ? (currentPP[k] !== undefined) : (k !== 'shadow');
                if (exists) pp[k] = desired[k];
            });
            if (!Object.keys(pp).length) return false;
            c3d.setProps({ postprocessing: pp });
            if (c3d.requestDraw) c3d.requestDraw();
            return true;
        } catch (e) {
            console.warn('[PhonosViewer] setRenderQuality failed, keeping current look', e);
            return false;
        }
    };

    PhonosViewer.prototype._queueVisualUpdate = function (work, markLoaded) {
        var self = this;

        var run = function () {
            if (self._destroyed || !self._initialized || !self.instance) return Promise.resolve(false);

            // Structure loads (markLoaded) get the loading overlay; it is debounced
            // so fast restyle/recolor reloads don't flash a spinner.
            if (markLoaded) self._showLoading();

            var result;
            try {
                result = work();
            } catch (error) {
                if (markLoaded) {
                    self._showError(error && error.message ? error.message : String(error));
                }
                return Promise.reject(error);
            }

            return Promise.resolve(result).then(function (value) {
                if (markLoaded) { self._hasLoadedStructure = true; self._hideLoading(); }
                return value;
            }, function (error) {
                if (markLoaded) {
                    self._showError(error && error.message ? error.message : String(error));
                    console.error('[PhonosViewer] visual update failed:', error);
                }
                throw error;
            });
        };

        var next = Promise.resolve(this._pendingVisualUpdate).catch(function () {}).then(run);
        this._pendingVisualUpdate = next;
        return next;
    };

    /* ---- Loading / error overlay ------------------------------------- */

    PhonosViewer.prototype._ensureOverlay = function () {
        if (this._overlay || !this.container) return this._overlay;
        var ov = mkEl('div', { 'class': 'pfp-loading-overlay' });
        ov.appendChild(mkEl('div', { 'class': 'pfp-spinner' }));
        var msg = mkEl('div', { 'class': 'pfp-loading-msg' });
        ov.appendChild(msg);
        this.container.style.position = this.container.style.position || 'relative';
        this.container.appendChild(ov);
        this._overlay = ov;
        this._overlayMsg = msg;
        return ov;
    };

    PhonosViewer.prototype._showLoading = function (message) {
        var self = this;
        clearTimeout(this._loadingTimer);
        this._loadingTimer = setTimeout(function () {
            var ov = self._ensureOverlay();
            if (!ov) return;
            ov.classList.remove('error');
            self._overlayMsg.textContent = message || (window.i18n ? window.i18n.t('phonosViewer.loadingStructure') : 'Loading structure…');
            ov.classList.add('visible');
        }, 180);
    };

    PhonosViewer.prototype._hideLoading = function () {
        clearTimeout(this._loadingTimer);
        if (this._overlay) this._overlay.classList.remove('visible', 'error');
    };

    PhonosViewer.prototype._showError = function (message) {
        clearTimeout(this._loadingTimer);
        var ov = this._ensureOverlay();
        if (!ov) return;
        ov.classList.add('error', 'visible');
        this._overlayMsg.textContent = message || (window.i18n ? window.i18n.t('phonosViewer.failedToLoadStructure') : 'Failed to load structure');
        console.error('[PhonosViewer] ERROR:', message || 'Failed to load structure');
    };

    PhonosViewer.prototype.whenReady = function () {
        return Promise.resolve(this._pendingVisualUpdate).catch(function () {});
    };

    PhonosViewer.prototype._fireLoaded = function () {
        this._hasLoadedStructure = true;
        for (var i = 0; i < this._loadedCallbacks.length; i++) {
            try { this._loadedCallbacks[i](); } catch (e) {}
        }
    };

    PhonosViewer.prototype._buildCurrentUpdateOptions = function (overrides) {
        if (!this._currentSource) return null;

        var base;
        if (this._currentSource.kind === 'data' || this._currentSource.kind === 'url') {
            base = {
                customData: {
                    url: this._currentSource.url,
                    format: this._currentSource.format,
                    binary: !!this._currentSource.binary
                }
            };
        } else if (this._currentSource.kind === 'pdb') {
            base = {
                moleculeId: this._currentSource.pdbId
            };
        } else if (this._currentSource.kind === 'alphafold') {
            base = {
                customData: {
                    url: this._currentSource.url,
                    format: 'cif',
                    binary: false
                }
            };
        } else {
            return null;
        }

        if (this._visualStyle) base.visualStyle = this._visualStyle;
        if (this._currentAlphaFoldView) base.alphafoldView = true;

        if (overrides) {
            Object.keys(overrides).forEach(function (key) {
                base[key] = overrides[key];
            });
        }

        return base;
    };

    PhonosViewer.prototype._reloadCurrentStructure = function () {
        var self = this;
        var updateOpts = this._buildCurrentUpdateOptions();
        if (!updateOpts) return Promise.resolve(false);

        // Snapshot raw-builder appends (ligands on a CIF/mol2 base). visual.update with
        // fullLoad=true rebuilds the scene from _currentSource only, so without this the
        // ligand silently disappears on every restyle / recolor.
        var appended = (this._appendedRaw || []).slice();

        // Standalone small molecules (SDF/MOL/MOL2) cannot be rendered by
        // PDBeMolstar's polymer-centric visual presets ('cartoon', etc.).
        // Override to 'ball-and-stick' so the reload leaves the scene
        // populated (then _forceBallAndStickRaw layers the raw builder rep).
        if (this._forceBallStick) {
            var _fmt = this._currentSource && this._currentSource.format;
            if (_fmt && _isSmallMoleculeFormat(_fmt)) {
                updateOpts.visualStyle = 'ball-and-stick';
            }
        }

        var forceBallStick = !!this._forceBallStick;

        return this._queueVisualUpdate(function () {
            return Promise.resolve(self.instance.visual.update(updateOpts, true)).then(function (r) {
                // fullLoad=true rebuilt the scene from the cartoon preset, dropping the
                // ball-and-stick rep that loadData() forced onto the embedded ligand.
                // Re-force it (no-op when the structure has no non-polymer component)
                // so protein+ligand complexes keep their ligand through restyle/recolor.
                var step = forceBallStick
                    ? self._forceBallAndStickRaw().catch(function (e) {
                        console.warn('[PhonosViewer] re-force ball-and-stick after reload failed', e);
                    })
                    : Promise.resolve();
                return step.then(function () {
                    if (!appended.length) return r;
                    return appended.reduce(function (p, entry) {
                        return p.then(function () {
                            return self._appendViaMolstarBuildersWork(entry.data, entry.format);
                        }).catch(function (e) {
                            console.warn('[PhonosViewer] re-append after reload failed', e);
                        });
                    }, Promise.resolve()).then(function () { return r; });
                });
            });
        }, true).then(function (r) {
            // The fullLoad rebuild above repainted the structure with Mol*'s native
            // colors, discarding the confidence / chain / custom overpaint. Re-assert
            // the active scheme as a separate queued op so a restyle / recolor / ligand
            // append never silently reverts a colored structure to the default.
            return Promise.resolve(self._reapplyColorScheme()).then(function () { return r; });
        });
    };

    /* ---- Loading ------------------------------------------------------ */

    PhonosViewer.prototype.loadUrl = function (url, format, options) {
        if (!this._initialized) this.init();
        var self = this;
        format = format || 'pdb';
        options = options || {};

        var updateOpts = {
            customData: { url: url, format: format, binary: options.binary || false },
            visualStyle: this._visualStyle || undefined
        };
        this._currentAlphaFoldView = !!(options.alphafoldView || this.options.alphafoldView);
        if (this._currentAlphaFoldView) updateOpts.alphafoldView = true;
        this._currentSource = { kind: 'url', url: url, format: format, binary: options.binary || false };

        var updatePromise = this._queueVisualUpdate(function () {
            return self.instance.visual.update(updateOpts, true);
        }, true);

        if (options.parseConfidence !== false) {
            fetch(url).then(function (r) { return r.text(); }).then(function (data) {
                self._rawData = data;
                self._rawFormat = format;
                self.confidenceData = parseConfidence(data, format);
                self._customSelectData = null;
                self._updateConfidenceUI();
                // Confidence arrives after the structure has rendered; apply the default
                // (pLDDT when available) or persisted scheme now that scores are known.
                return updatePromise.then(function () { return self._applyInitialColor(); });
            }).catch(function () {});
        }

        return updatePromise;
    };

    function _isSmallMoleculeFormat(format) {
        return format === 'sdf' || format === 'mol' || format === 'mol2';
    }

    /* A standalone small molecule (no polymer backbone): an SDF/MOL/MOL2 file, or a
       PDB/CIF that is purely HETATM (a bare ligand exported as PDB). For these,
       PDBeMolstar's `visualStyle: 'ball-and-stick'` preset is unreliable — isolated
       small molecules frequently come back as spacefill (balls only, no bonds), so
       we re-render them with an explicit ball-and-stick representation. */
    function _isStandaloneSmallMolecule(format, data) {
        if (_isSmallMoleculeFormat(format)) return true;
        var t = String(data || '');
        if (format === 'pdb' || format === 'ent' || format === 'pdbqt') {
            return /^HETATM\s/m.test(t) && !/^ATOM\s/m.test(t);
        }
        if (format === 'cif' || format === 'mmcif') {
            return /^HETATM\s/m.test(t) && !/^ATOM\s/m.test(t);
        }
        return false;
    }

    /* True when the data (PDB or CIF text) contains HETATM records — ligands
       embedded alongside polymer ATOM records. These should always be forced to
       ball-and-stick so they are visible, even when the visual style is 'cartoon'. */
    function _dataHasHetatm(format, data) {
        var t = String(data || '');
        if (format === 'pdb' || format === 'ent' || format === 'pdbqt') {
            return /^HETATM\s/m.test(t);
        }
        if (format === 'cif' || format === 'mmcif') {
            // Robust CIF check: look for HETATM records at start of line
            return /^HETATM\s/m.test(t);
        }
        return false;
    }

    /* Reduce a CIF to a single data_ block + the _atom_site loop_ only.
       Predicted-complex CIFs (Boltz2 / Chai / OpenFold3 / DiffDock) sometimes
       carry extra category loops that crash Mol*'s mmCIF parser ("items is
       undefined" / Symbol.iterator). Stripping everything but the coordinate
       loop lets the structure still render instead of showing a blank/error
       state. Returns null when no usable atom_site loop is found. */
    function sanitizeCifAtomSiteOnly(structureText) {
        var lines = String(structureText || '').split(/\r?\n/);
        var dataName = 'data_phonos';
        for (var i = 0; i < lines.length; i++) {
            if (lines[i].trim().indexOf('data_') === 0) { dataName = lines[i].trim(); break; }
        }
        var atomHeaders = [];
        var atomRows = [];
        var pendingLoop = false;
        var inAtomLoop = false;

        for (var j = 0; j < lines.length; j++) {
            var rawLine = lines[j];
            var line = rawLine.trim();
            if (!inAtomLoop) {
                if (line === 'loop_') { pendingLoop = true; atomHeaders.length = 0; continue; }
                if (pendingLoop && line.indexOf('_atom_site.') === 0) {
                    inAtomLoop = true;
                    atomHeaders.push(line);
                    continue;
                }
                if (pendingLoop && line.charAt(0) === '_' && line.indexOf('_atom_site.') !== 0) {
                    pendingLoop = false;
                }
                continue;
            }

            if (line.indexOf('_atom_site.') === 0) { atomHeaders.push(line); continue; }
            if (!line) continue;
            if (line === '#' || line === 'loop_') break;
            atomRows.push(rawLine);
        }

        if (!atomHeaders.length || !atomRows.length) return null;

        return [dataName, '#', 'loop_'].concat(atomHeaders, atomRows, ['#', '']).join('\n');
    }

    /* Force an explicit ball-and-stick representation on every loaded structure via
       the raw Mol* builder API. Guarantees both atoms (balls) AND bonds (sticks)
       regardless of what PDBeMolstar's preset chose — the small-molecule "balls,
       no sticks" fix. Runs INSIDE an already-queued visual update (does not queue
       itself). Returns a promise resolving true when a representation was added.
       Uses CPK (element-symbol) coloring, the standard look for small molecules;
       the overpaint-based color schemes (chain/pLDDT/clearSelection) layer on top
       without rebuilding, so this survives the rest of the load pipeline. */
    PhonosViewer.prototype._forceBallAndStickRaw = function () {
        var molPlugin = this.instance && this.instance.plugin;
        if (!molPlugin || !molPlugin.builders || !molPlugin.builders.structure ||
            !molPlugin.managers || !molPlugin.managers.structure) return Promise.resolve(false);
        var b = molPlugin.builders.structure;
        if (!b.representation || !b.representation.addRepresentation || !b.tryCreateComponentStatic) {
            return Promise.resolve(false);
        }
        var hier = molPlugin.managers.structure.hierarchy && molPlugin.managers.structure.hierarchy.current;
        var structs = hier && hier.structures;
        if (!structs || !structs.length) return Promise.resolve(false);
        var self = this;
        var build = function () {
            var chain = Promise.resolve();
            var added = 0;
            structs.forEach(function (s) {
                var ref = s && s.cell;
                if (!ref) return;
                chain = chain
                    .then(function () {
                        // Check if we have polymer (protein/nucleic). 
                        // If so, we only want to force ball-and-stick on LIGANDS.
                        return b.tryCreateComponentStatic(ref, 'polymer').then(function (poly) {
                            var selector = poly ? 'ligand' : 'all';
                            return b.tryCreateComponentStatic(ref, selector).then(function (comp) {
                                if (!comp) {
                                    if (poly) return;
                                    return;
                                }
                                return b.representation.addRepresentation(comp, {
                                    type: 'ball-and-stick',
                                    typeParams: { alpha: 1, sizeFactor: 0.35 },
                                    color: 'element-symbol',
                                    colorParams: LIGAND_ELEMENT_COLOR_PARAMS
                                }).then(function () { added++; });
                            });
                        });
                    });
            });
            return chain.then(function () { return added; });
        };
        var run = molPlugin.dataTransaction
            ? molPlugin.dataTransaction(build, 'phonos-ball-stick')
            : build();
        return Promise.resolve(run).then(function (added) {
            if (added) { self._layerReps = {}; self._fireLoaded(); }
            return !!added;
        }).catch(function (e) {
            console.warn('[PhonosViewer] ball-and-stick override failed, keeping preset:', e);
            return false;
        });
    };

    /* Remove EVERY structure currently in the scene (the pdbe-managed one plus any
       added via the raw Mol* builder API by appendData()). loadData() is a replace
       operation, so it must start from an empty scene; relying solely on
       pdbe-molstar's internal clear-on-fullLoad left ligands/structures appended
       through the raw builder path behind, which piled up when switching structures. */
    PhonosViewer.prototype._removeAllStructures = function () {
        var molPlugin = this.instance && this.instance.plugin;
        if (!molPlugin || !molPlugin.managers || !molPlugin.managers.structure) return Promise.resolve(false);
        var mgr = molPlugin.managers.structure.hierarchy;
        var current = mgr && mgr.current;
        var structs = current && current.structures;
        if (!structs || !structs.length) return Promise.resolve(false);
        try {
            this._layerReps = {};
            return Promise.resolve(mgr.remove(structs.slice(), false))
                .then(function () { return true; })
                .catch(function (e) { console.warn('[PhonosViewer] _removeAllStructures failed', e); return false; });
        } catch (e) {
            console.warn('[PhonosViewer] _removeAllStructures threw', e);
            return Promise.resolve(false);
        }
    };

    PhonosViewer.prototype.loadData = function (data, format, options) {
        if (!this._initialized) this.init();
        var self = this;
        format = format || 'pdb';
        options = options || {};

        // SDF/MOL files frequently arrive with no bond block (e.g. Boltz2 /
        // RDKit ligand exports). Mol* then renders them as balls with no
        // sticks. Reconstruct connectivity from inter-atomic distances up
        // front so the ball-and-stick representation has bonds to draw.
        // inferBondsForSDF is a no-op when bonds already exist or the format
        // is V3000, so this is safe to run unconditionally for sdf/mol.
        if (format === 'sdf' || format === 'mol') {
            data = inferBondsForSDF(data);
        }

        this._rawData = data;
        this._rawFormat = format;
        // Fresh base structure replaces the whole scene — any previously
        // raw-builder-appended ligands are gone, so drop their restore records.
        this._appendedRaw = [];
        // A custom (rainbow / hydrophobicity) overpaint was built from the previous
        // structure's sequence — discard it so a restyle of THIS structure (before the
        // caller rebuilds the scheme) can't replay stale per-residue colors.
        this._customSelectData = null;
        // Measurements reference loci in the previous structure's atom indices,
        // which are meaningless (or wrong) against freshly-loaded data — clear
        // rather than try to re-anchor them.
        this.clearMeasurements();

        // Small molecules (SDF / MOL / MOL2) have no polymer backbone — using
        // 'cartoon' would render as spacefill or nothing.  Default to ball-and-
        // stick when no explicit style was requested.
        if (_isSmallMoleculeFormat(format) && this._visualStyleDefault) {
            this._visualStyle = 'ball-and-stick';
        }
        // PDB files can also be standalone ligands (HETATM-only, no ATOM
        // records).  Detect this and switch to ball-and-stick.
        if (format === 'pdb' && this._visualStyleDefault && !/^ATOM\s/m.test(String(data || ''))) {
            this._visualStyle = 'ball-and-stick';
        }

        this.confidenceData = parseConfidence(data, format);
        this._updateConfidenceUI();

        this._currentSource = { kind: 'data', url: null, format: format, binary: false };

        var blobUrl = dataToBlob(data);
        this._blobUrls.push(blobUrl);
        this._currentSource.url = blobUrl;

        var updateOpts = {
            customData: { url: blobUrl, format: format, binary: false },
            visualStyle: this._visualStyle || undefined
        };
        this._currentAlphaFoldView = !!(options.alphafoldView || this.options.alphafoldView);
        if (this._currentAlphaFoldView) updateOpts.alphafoldView = true;

        // Standalone small molecules render as spacefill (balls only) under
        // PDBeMolstar's preset; after the load settles, force an explicit
        // ball-and-stick rep so bonds (sticks) actually show.  Also force
        // ball-and-stick for PDB/CIF files that contain HETATM ligands
        // embedded alongside polymer ATOM records — the 'cartoon' preset
        // renders these as spacefill or invisible.
        //
        // mmCIF: predicted-structure pipelines (Boltz2 / Chai / DiffDock) write
        // the bound ligand with group_PDB = ATOM, not HETATM, so the HETATM
        // regex above misses it and the ligand vanishes into the cartoon. For
        // any CIF/mmCIF we therefore always run the override — it is a safe
        // no-op when Mol*'s composition-based 'ligand' component query finds
        // nothing (e.g. a plain AlphaFold protein), and the only structures it
        // touches are those that genuinely contain a non-polymer component.
        var forceBallStick = (this._visualStyle === 'ball-and-stick'
            && _isStandaloneSmallMolecule(format, data))
            || (_dataHasHetatm(format, data))
            || (format === 'cif' || format === 'mmcif');
        // Remember so a later restyle/recolor (_reloadCurrentStructure) can
        // re-force the ligand ball-and-stick. Without this the embedded ligand
        // (e.g. an OpenFold3 protein+GTP complex) renders on first load but
        // vanishes the moment view_output applies its default style/colour.
        this._forceBallStick = forceBallStick;

        // Mol* mounts its canvas asynchronously; the very first load against a
        // freshly created viewer can race the plugin render and reject. Retry the
        // visual.update a few times with a short delay so the viewer settles —
        // this is what makes structures (especially the initial one) load
        // reliably on the view_output page, mirroring the dedicated visualiser tab.
        // Only retry while nothing has rendered yet: the race is a freshly-mounted
        // canvas, so once _hasLoadedStructure is true a rejection is a genuine parse
        // error (e.g. a bad ligand candidate being trialled) and must fail fast.
        var attemptUpdate = function (opts, attemptsLeft) {
            return Promise.resolve(self.instance.visual.update(opts, true)).catch(function (err) {
                if (attemptsLeft > 0 && !self._destroyed && !self._hasLoadedStructure) {
                    return new Promise(function (res) { setTimeout(res, 400); })
                        .then(function () { return attemptUpdate(opts, attemptsLeft - 1); });
                }
                throw err;
            });
        };

        return this._queueVisualUpdate(function () {
            // Guarantee a clean scene first — removes any structures/ligands added
            // via the raw builder API (appendData) that pdbe's fullLoad clear misses,
            // so switching structures replaces rather than piles up.
            return self._removeAllStructures().then(function () {
                return attemptUpdate(updateOpts, 3).catch(function (err) {
                    // Malformed / extra-category CIFs crash Mol*'s parser. Retry with
                    // an atom_site-only sanitized CIF so the structure still shows
                    // instead of a blank/error state (matches the visualiser tab).
                    if (format === 'cif' || format === 'mmcif') {
                        var sane = sanitizeCifAtomSiteOnly(data);
                        if (sane && sane !== data) {
                            console.warn('[PhonosViewer] CIF parse failed; retrying with atom_site-only fallback', err);
                            var saneBlob = dataToBlob(sane);
                            self._blobUrls.push(saneBlob);
                            self._rawData = sane;
                            self._currentSource = { kind: 'data', url: saneBlob, format: 'cif', binary: false };
                            var saneOpts = {
                                customData: { url: saneBlob, format: 'cif', binary: false },
                                visualStyle: updateOpts.visualStyle
                            };
                            if (updateOpts.alphafoldView) saneOpts.alphafoldView = true;
                            return self._removeAllStructures().then(function () {
                                return attemptUpdate(saneOpts, 1);
                            });
                        }
                    }
                    throw err;
                }).then(function (r) {
                    if (!forceBallStick) return r;
                    return self._forceBallAndStickRaw().then(function () { return r; });
                });
            });
        }, true).then(function (r) {
            // Apply the default (pLDDT when available) or the user's persisted scheme to
            // the freshly loaded structure, as a separate queued op once it has rendered.
            return Promise.resolve(self._applyInitialColor()).then(function () { return r; });
        });
    };

    /* Append a small molecule to an already-loaded scene using the raw Mol* builder API.
       This is used when the base structure is CIF (can't inline-combine CIF+PDB) or when
       the format is mol2.  The new structure is added to the existing scene rather than
       replacing it, so both protein and ligand are visible at once. */
    PhonosViewer.prototype._appendViaMolstarBuilders = function (data, format) {
        var self = this;
        var molPlugin = this.instance && this.instance.plugin;
        if (!molPlugin || !molPlugin.builders ||
            !molPlugin.builders.data || !molPlugin.builders.structure) {
            console.warn('[PhonosViewer] Raw Mol* builder API not available, loading standalone');
            return this.loadData(data, format);
        }

        // Remember this append so it can be restored after a base-structure reload
        // (setVisualStyle / applyConfidenceColoring etc. rebuild only _currentSource,
        // which doesn't contain raw-builder structures, so the ligand would vanish).
        if (!this._appendedRaw) this._appendedRaw = [];
        this._appendedRaw.push({ data: data, format: format });

        return this._queueVisualUpdate(function () {
            return self._appendViaMolstarBuildersWork(data, format);
        }, false);
    };

    /* The actual raw Mol* builder work for appendData — kept separate from the queue
       wrapper above so _reloadCurrentStructure can re-run it INSIDE its own queued
       task (calling the queueing _appendViaMolstarBuilders there would deadlock the
       visual-update queue). Assumes the builder API is available (checked by callers). */
    PhonosViewer.prototype._appendViaMolstarBuildersWork = function (data, format) {
        var self = this;
        var molPlugin = this.instance && this.instance.plugin;
        if (!molPlugin || !molPlugin.builders ||
            !molPlugin.builders.data || !molPlugin.builders.structure) {
            return Promise.resolve(false);
        }

        // SDF/MOL ligand poses (e.g. DiffDock/Boltz2 rank files) often ship with no
        // bond block, so Mol* renders them as disconnected dots / nothing. loadData
        // infers connectivity up front; the append path must do the same or the
        // appended ligand fails to display. No-op when bonds already exist / V3000.
        if (format === 'sdf' || format === 'mol') {
            data = inferBondsForSDF(data);
        }

        // Mol* format name mapping
        var molFormat = format === 'mol2' ? 'mol2'
                      : (format === 'cif' || format === 'mmcif') ? 'mmcif'
                      : format === 'pdb' ? 'pdb'
                      : 'sdf'; // SDF / MOL → Mol* 'sdf' (handles both V2000 and V3000)

        var builders = molPlugin.builders;
        // Build a data node, then parse as trajectory, then apply default preset
        var dataPromise = (builders.data.rawData)
            ? Promise.resolve(builders.data.rawData({ data: data, label: 'Ligand' }))
            : (function () {
                // Older Mol* API: download from a blob URL
                var blob = new Blob([data], { type: 'text/plain' });
                var url = URL.createObjectURL(blob);
                self._blobUrls.push(url);
                return Promise.resolve(builders.data.download({ url: url, isBinary: false, label: 'Ligand' }));
            }());

        return dataPromise
            .then(function (dataRef) {
                return builders.structure.parseTrajectory(dataRef, molFormat);
            })
            .then(function (trajectory) {
                var h = builders.structure.hierarchy;
                if (!h || !h.applyPreset) return Promise.resolve();
                // Apply the 'default' preset to register the structure in the
                // scene, then overlay a ball-and-stick representation on top.
                // We do NOT clear the default representations (the old
                // clear-approach could remove the ball-and-stick too); instead
                // the ball-and-stick is layered above and visually dominates.
                return h.applyPreset(trajectory, 'default').then(function () {
                    if (!molPlugin.managers || !molPlugin.managers.structure) return;
                    var structs = molPlugin.managers.structure.hierarchy &&
                        molPlugin.managers.structure.hierarchy.current &&
                        molPlugin.managers.structure.hierarchy.current.structures;
                    if (!structs || !structs.length) return;
                    var ligStruct = structs[structs.length - 1];
                    var ref = ligStruct && ligStruct.cell;
                    if (!ref) return;
                    // Only force ball-and-stick on the LIGAND when the appended structure
                    // also contains polymer (a second protein / complex CIF) — otherwise
                    // the polymer renders as cartoon (from the 'default' preset) AND
                    // ball-and-stick (this overlay) at once. For a pure small molecule
                    // (no polymer, e.g. an SDF ligand) force it on 'all'. Mirrors
                    // _forceBallAndStickRaw so appended structures match loaded ones.
                    return builders.structure.tryCreateComponentStatic(ref, 'polymer').then(function (poly) {
                        var selector = poly ? 'ligand' : 'all';
                        return builders.structure.tryCreateComponentStatic(ref, selector).then(function (comp) {
                            if (!comp) return;
                            return builders.structure.representation.addRepresentation(comp, {
                                type: 'ball-and-stick',
                                typeParams: { alpha: 1, sizeFactor: 0.4 },
                                color: 'element-symbol',
                                colorParams: LIGAND_ELEMENT_COLOR_PARAMS
                            });
                        });
                    }).catch(function (e) {
                        console.warn('[PhonosViewer] Ligand rep override failed, keeping preset:', e);
                    });
                });
            })
            .catch(function (e) {
                console.warn('[PhonosViewer] Raw Mol* append failed, skipping ligand:', e);
            });
    };

    /* Combine a ligand (SDF/MOL) or second protein with the already-loaded structure.
       For SDF/MOL + PDB base: converts to HETATM PDB records and appends inline.
       For SDF/MOL + CIF base: uses raw Mol* builder API to add second structure to scene.
       For mol2: uses raw Mol* builder API (mol2 can't be inlined as HETATM).
       Falls back to a plain loadData() if no base data exists yet. */
    PhonosViewer.prototype.appendData = function (data, format, options) {
        if (!this._initialized) this.init();
        var self = this;
        format = format || 'pdb';
        options = options || {};

        // No existing data — just do a normal load
        if (!this._rawData) {
            return this.loadData(data, format, options);
        }

        var incoming = data;

        if (format === 'mol2') {
            // mol2 can't be inlined as HETATM — add as second Mol* structure
            return this._appendViaMolstarBuilders(data, format);
        }

        if (format === 'sdf' || format === 'mol') {
            // Inline HETATM combination only works when the base is plain PDB text.
            // For any other base (CIF / SDF / MOL / MOL2) add the ligand as a second
            // Mol* structure instead of corrupting the base file.
            if (this._rawFormat !== 'pdb') {
                return this._appendViaMolstarBuilders(data, format);
            }
            // Convert SDF/MOL to HETATM PDB records for inline combination
            var molName = data.split('\n')[0].trim().replace(/[^A-Z0-9]/gi, '').toUpperCase().substring(0, 3) || 'LIG';
            incoming = sdfToHetAtmPdb(data, options.chainId || 'Z', molName);
            if (!incoming) {
                console.warn('[PhonosViewer] sdfToHetAtmPdb conversion yielded nothing, trying Mol* builder append');
                return this._appendViaMolstarBuilders(data, format);
            }
            format = 'pdb';
        }

        // Combine: strip trailing END from base PDB, append HETATM, re-add END
        var basePdb = this._rawData;
        if (this._rawFormat !== 'pdb') {
            // Non-PDB base can't be inlined as HETATM — use the Mol* builder API
            return this._appendViaMolstarBuilders(data, format);
        }
        basePdb = basePdb.replace(/^END\s*(\r?\n)?/m, '').trimEnd();
        var combined = basePdb + '\n' + incoming.trimEnd() + '\nEND\n';

        this._rawData = combined;
        this._rawFormat = 'pdb';
        this.confidenceData = parseConfidence(combined, 'pdb');
        this._updateConfidenceUI();

        var blobUrl = dataToBlob(combined);
        this._blobUrls.push(blobUrl);
        this._currentSource = { kind: 'data', url: blobUrl, format: 'pdb', binary: false };

        var updateOpts = {
            customData: { url: blobUrl, format: 'pdb', binary: false },
            visualStyle: this._visualStyle || undefined
        };
        if (this._currentAlphaFoldView) updateOpts.alphafoldView = true;

        var hasLigand = _dataHasHetatm('pdb', combined);

        // Persist ball-and-stick for the appended ligand so it survives later
        // restyles/recolors. _reloadCurrentStructure (triggered by setVisualStyle /
        // applyConfidenceColoring) rebuilds from the cartoon preset and only
        // re-forces ball-and-stick when _forceBallStick is set. Without this the
        // appended ligand renders once here, then vanishes on the very next restyle —
        // e.g. the applyStyle() call loadArtifact() runs right after appending it
        // (DiffDock protein + separate ligand pose).
        if (hasLigand) this._forceBallStick = true;

        return this._queueVisualUpdate(function () {
            return Promise.resolve(self.instance.visual.update(updateOpts, true)).then(function (r) {
                if (hasLigand) {
                    return self._forceBallAndStickRaw().then(function () { return r; });
                }
                return r;
            });
        }, true).then(function (r) {
            // Inlining the ligand re-loaded the combined PDB (fullLoad), wiping the
            // protein's overpaint. Re-assert the active scheme so appending a ligand to
            // a confidence-colored protein doesn't drop it back to default coloring.
            return Promise.resolve(self._reapplyColorScheme()).then(function () { return r; });
        });
    };

    PhonosViewer.prototype.loadPdb = function (pdbId) {
        if (!this._initialized) this.init();
        var self = this;
        this._currentSource = { kind: 'pdb', pdbId: pdbId.toLowerCase() };
        this._currentAlphaFoldView = false;
        return this._queueVisualUpdate(function () {
            return self.instance.visual.update({ moleculeId: pdbId.toLowerCase() }, true);
        }, true);
    };

    PhonosViewer.prototype.loadAlphaFoldDb = function (uniprotId) {
        if (!this._initialized) this.init();
        var self = this;
        var afUrl = 'https://alphafold.ebi.ac.uk/files/AF-' + uniprotId.toUpperCase() + '-F1-model_v4.cif';
        this._currentSource = { kind: 'alphafold', url: afUrl };
        this._currentAlphaFoldView = true;
        return this._queueVisualUpdate(function () {
            return self.instance.visual.update({
                customData: {
                    url: afUrl,
                    format: 'cif',
                    binary: false
                },
                alphafoldView: true
            }, true);
        }, true);
    };

    /* ---- Confidence -------------------------------------------------- */

    PhonosViewer.prototype.setConfidenceData = function (data, format) {
        this.confidenceData = parseConfidence(data, format || 'pdb');
        this._updateConfidenceUI();
    };

    PhonosViewer.prototype.getConfidenceStats = function () {
        return this.confidenceData ? computeConfidenceStats(this.confidenceData) : null;
    };

    /* True when the displayed structure carries real per-residue pLDDT values. */
    PhonosViewer.prototype.hasConfidence = function () {
        return confidenceHasScores(this.confidenceData);
    };

    /* Residue counts per AlphaFold band for the displayed structure, or null. */
    PhonosViewer.prototype.getConfidenceBands = function () {
        if (!confidenceHasScores(this.confidenceData)) return null;
        var out = { VERY_HIGH: 0, CONFIDENT: 0, LOW: 0, VERY_LOW: 0, total: 0 };
        this.confidenceData.scores.forEach(function (s) {
            if (typeof s.score !== 'number') return;
            out[plddtBand(s.score)]++;
            out.total++;
        });
        return out.total ? out : null;
    };

    /* Per-residue list in file order: [{chain, resi, resn, score|null}] with author
       chain/numbering (what hover/picking and highlightResidues() use). */
    PhonosViewer.prototype.getConfidenceResidues = function () {
        var has = confidenceHasScores(this.confidenceData);
        return ((this.confidenceData && this.confidenceData.scores) || []).map(function (s) {
            return { chain: entryAuthChain(s), resi: entryAuthResi(s), resn: s.resn || '',
                     score: has && typeof s.score === 'number' ? s.score : null };
        });
    };

    /* Look up one residue by author chain + number (as reported by hover/picking).
       Returns {score, band, color, resn} or null when the residue has no score. */
    PhonosViewer.prototype.getResidueConfidence = function (chain, resi) {
        if (!confidenceHasScores(this.confidenceData)) return null;
        var n = Number(resi);
        if (!isFinite(n)) return null;
        var idx = this._confIndex;
        if (!idx || idx.data !== this.confidenceData) {
            idx = { data: this.confidenceData, map: {} };
            this.confidenceData.scores.forEach(function (s) {
                var k = entryAuthChain(s) + ':' + entryAuthResi(s);
                if (!(k in idx.map)) idx.map[k] = s;
            });
            this._confIndex = idx;
        }
        var hit = idx.map[String(chain || '') + ':' + n];
        if (!hit && !chain) {
            // No chain given: accept a unique match on the residue number.
            var keys = Object.keys(idx.map).filter(function (k) { return k.split(':')[1] === String(n); });
            if (keys.length === 1) hit = idx.map[keys[0]];
        }
        if (!hit || typeof hit.score !== 'number') return null;
        var band = plddtBand(hit.score);
        return { score: hit.score, band: band, color: PLDDT[band].css, resn: hit.resn || '' };
    };

    PhonosViewer.prototype.getChains = function () {
        if (!this.confidenceData) return [];
        return Object.keys(this.confidenceData.byChain);
    };

    /* Per-chain summary for UI chips: residue span + mean pLDDT (null when the
       structure carries no confidence scores). */
    PhonosViewer.prototype.getChainSummaries = function () {
        var byChain = (this.confidenceData && this.confidenceData.byChain) || {};
        var scoredOk = confidenceHasScores(this.confidenceData);
        return Object.keys(byChain).map(function (chain) {
            var entries = byChain[chain] || [];
            var sum = 0, scored = 0, min = Infinity, max = -Infinity;
            for (var i = 0; i < entries.length; i++) {
                var e = entries[i];
                if (e.resi < min) min = e.resi;
                if (e.resi > max) max = e.resi;
                if (scoredOk && typeof e.score === 'number' && !isNaN(e.score)) { sum += e.score; scored++; }
            }
            return {
                chain: chain,
                count: entries.length,
                first: min === Infinity ? null : min,
                last: max === -Infinity ? null : max,
                meanScore: scored ? Math.round(sum / scored * 10) / 10 : null
            };
        }).sort(function (a, b) { return a.chain < b.chain ? -1 : (a.chain > b.chain ? 1 : 0); });
    };

    /* The structure text currently rendered as the base model, if it was loaded
       from raw data (loadData / appendData). URL- and PDB-ID-sourced structures
       return null — the caller already has a download link for those. */
    PhonosViewer.prototype.getStructureText = function () {
        return this._rawData || null;
    };

    /* Download the loaded structure as a file. Falls back to opening the source
       URL when the structure was streamed rather than supplied as text. */
    PhonosViewer.prototype.exportStructure = function () {
        var text = this.getStructureText();
        var src = this._currentSource || {};
        if (!text) {
            if (src.url) { window.open(src.url, '_blank', 'noopener'); return true; }
            showToast(this, window.i18n ? window.i18n.t('phonosViewer.nothingToExport') : 'Nothing to export');
            return false;
        }
        var ext = (src.format === 'cif' || src.format === 'mmcif') ? 'cif' : (src.format || 'pdb');
        var url = URL.createObjectURL(new Blob([text], { type: 'chemical/x-pdb' }));
        downloadDataUrl(url, 'phonos-structure-' + nowStamp() + '.' + ext);
        setTimeout(function () { try { URL.revokeObjectURL(url); } catch (e) {} }, 5000);
        showToast(this, window.i18n ? window.i18n.t('phonosViewer.structureExported') : 'Structure downloaded');
        return true;
    };

    PhonosViewer.prototype._updateConfidenceUI = function () {
        // Let host pages keep their own controls (Confidence button, legend) in
        // step with the structure that is actually displayed.
        try {
            if (this.container) {
                this.container.dispatchEvent(new CustomEvent('phonos:confidence', {
                    detail: {
                        hasScores: confidenceHasScores(this.confidenceData),
                        scale: this.confidenceData ? this.confidenceData.scale : 'none',
                        bands: this.getConfidenceBands()
                    }
                }));
            }
        } catch (e) {}
        if (!this._panel) return;
        var bar = qs(this._panel, '#pfp-confidence-bar');
        if (bar && this.confidenceData) renderConfidenceBar(bar, this.confidenceData);
        this._renderChainChips();
    };

    /* One chip per chain: click focuses it, Shift+click isolates it. Hidden entirely
       for single-chain structures, where the chips would just be noise. */
    PhonosViewer.prototype._renderChainChips = function () {
        if (!this._panel) return;
        var section = qs(this._panel, '#pfp-chain-section');
        var host = qs(this._panel, '#pfp-chain-chips');
        if (!section || !host) return;

        var summaries = this.getChainSummaries();
        if (summaries.length < 2) { section.style.display = 'none'; return; }
        section.style.display = '';
        while (host.firstChild) host.removeChild(host.firstChild);

        var self = this;
        summaries.forEach(function (info, i) {
            var chip = mkEl('button', { type: 'button', 'class': 'pfp-chain-chip', 'data-chain': info.chain });
            chip.appendChild(mkEl('i', { style: 'background:' + CHAIN_COLORS[i % CHAIN_COLORS.length] }));
            chip.appendChild(mkEl('span', { text: info.chain }));
            chip.appendChild(mkEl('em', { text: String(info.count) }));
            var tip = info.chain + ' · ' + info.count + ' ' + t('residues', 'residues');
            if (info.first !== null) tip += ' (' + info.first + '–' + info.last + ')';
            if (info.meanScore !== null) tip += ' · ' + t('meanPlddt', 'mean pLDDT') + ' ' + info.meanScore;
            chip.title = tip;
            chip.addEventListener('click', function (e) {
                var isolate = e.shiftKey;
                self.focusChain(info.chain, { isolate: isolate });
                qsa(host, '.pfp-chain-chip').forEach(function (c) { c.classList.remove('active'); });
                chip.classList.add('active');
                showToast(self, (isolate ? t('isolated', 'Isolated') : t('focused', 'Focused')) + ' ' + info.chain);
            });
            host.appendChild(chip);
        });
    };

    /* Colour ramp legend under the coloring row — only meaningful for the two
       continuous schemes; any other scheme hides it. */
    PhonosViewer.prototype._renderRampLegend = function (scheme) {
        if (!this._panel) return;
        var host = qs(this._panel, '#pfp-ramp-legend');
        if (!host) return;
        while (host.firstChild) host.removeChild(host.firstChild);

        var stops, left, right;
        if (scheme === 'spectrum') {
            stops = [];
            for (var i = 0; i <= 20; i++) stops.push(spectrumColor(i / 20));
            left = t('nTerminus', 'N-term');
            right = t('cTerminus', 'C-term');
        } else if (scheme === 'hydrophobicity') {
            stops = [];
            for (var j = 0; j <= 20; j++) stops.push(rampColor(HYDRO_STOPS, j / 20));
            left = t('hydrophilic', 'Hydrophilic');
            right = t('hydrophobic', 'Hydrophobic');
        } else {
            host.style.display = 'none';
            return;
        }

        host.style.display = '';
        host.appendChild(mkEl('div', {
            'class': 'pfp-ramp-bar',
            style: 'background:linear-gradient(90deg,' + stops.join(',') + ')'
        }));
        var labels = mkEl('div', { 'class': 'pfp-ramp-labels' });
        labels.appendChild(mkEl('span', { text: left }));
        labels.appendChild(mkEl('span', { text: right }));
        host.appendChild(labels);
    };

    /* ---- Focus & Selection ------------------------------------------- */

    PhonosViewer.prototype.focusRegion = function (selections, options) {
        if (!this._initialized || !this.instance) return;
        var self = this;
        options = options || {};

        var focusData = [];
        for (var i = 0; i < selections.length; i++) {
            var s = selections[i];
            var entry = {};
            if (s.chain) entry.struct_asym_id = s.chain;
            if (s.authChain) entry.auth_asym_id = s.authChain;
            if (s.start !== undefined) entry.start_residue_number = s.start;
            if (s.end !== undefined) entry.end_residue_number = s.end;
            focusData.push(entry);
        }

        return this._queueVisualUpdate(function () {
            self.instance.visual.focus(focusData);

            if (options.isolate) {
                var selectData = [];
                for (var j = 0; j < selections.length; j++) {
                    var sel = selections[j];
                    var selEntry = {};
                    if (sel.chain) selEntry.struct_asym_id = sel.chain;
                    if (sel.authChain) selEntry.auth_asym_id = sel.authChain;
                    if (sel.start !== undefined) selEntry.start_residue_number = sel.start;
                    if (sel.end !== undefined) selEntry.end_residue_number = sel.end;
                    selEntry.color = sel.color || '#10b981';
                    selectData.push(selEntry);
                }
                self.instance.visual.select({
                    data: selectData,
                    nonSelectedColor: options.dimColor || '#373738'
                });
            }
        }, false);
    };

    PhonosViewer.prototype.focusChain = function (chainId, options) {
        this.focusRegion([{ chain: chainId, start: 1, end: 99999 }], options);
    };

    PhonosViewer.prototype.focusLigand = function (compId) {
        if (!this._initialized) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            return self.instance.visual.update({ ligandView: { label_comp_id: compId } }, false);
        }, false);
    };

    PhonosViewer.prototype.focusConfidenceRange = function (minScore, maxScore, options) {
        if (!this.confidenceData) {
            showToast(this, window.i18n ? window.i18n.t('phonosViewer.noConfidenceData') : 'No confidence data available');
            return;
        }
        var byChain = getResiduesInConfidenceRange(this.confidenceData, minScore, maxScore);
        var chains = Object.keys(byChain);
        if (chains.length === 0) {
            showToast(this, (window.i18n ? window.i18n.t('phonosViewer.noResiduesInRange') : 'No residues in range ') + minScore + '-' + maxScore);
            return;
        }
        var selections = [];
        for (var c = 0; c < chains.length; c++) {
            var ranges = buildContiguousRanges(byChain[chains[c]]);
            for (var r = 0; r < ranges.length; r++) {
                selections.push({ chain: chains[c], start: ranges[r].start, end: ranges[r].end });
            }
        }
        options = options || {};
        options.isolate = options.isolate !== false;
        this.focusRegion(selections, options);
        showToast(this, (window.i18n ? window.i18n.t('phonosViewer.focusedRegion') : 'Focused: ') + selections.length + ' region(s), pLDDT ' + minScore + '-' + maxScore);
    };

    PhonosViewer.prototype.focusTermini = function (residues) {
        if (!this.confidenceData) { showToast(this, window.i18n ? window.i18n.t('phonosViewer.noConfidenceDataShort') : 'No confidence data'); return; }
        residues = residues || 15;
        var chains = Object.keys(this.confidenceData.byChain);
        var selections = [];
        for (var i = 0; i < chains.length; i++) {
            var cData = this.confidenceData.byChain[chains[i]];
            if (!cData || cData.length === 0) continue;
            var sorted = cData.slice().sort(function (a, b) { return a.resi - b.resi; });
            var first = sorted[0].resi;
            var last  = sorted[sorted.length - 1].resi;
            selections.push({ chain: chains[i], start: first, end: Math.min(first + residues - 1, last) });
            if (last > first + residues) {
                selections.push({ chain: chains[i], start: Math.max(last - residues + 1, first), end: last });
            }
        }
        this.focusRegion(selections, { isolate: true });
        showToast(this, (window.i18n ? window.i18n.t('phonosViewer.nCTermini') : 'N/C termini (') + residues + ' residues)');
    };

    PhonosViewer.prototype.clearFocus = function () {
        if (!this._initialized || !this.instance) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
            try { self.instance.visual.reset({ camera: true, theme: true }); } catch (e) {}
        }, false).then(function (r) {
            // reset({theme:true}) drops the color overpaint too — re-assert the active
            // scheme so "Reset" only resets the camera/focus, not the user's coloring.
            return Promise.resolve(self._reapplyColorScheme()).then(function () { return r; });
        });
    };

    /* Empty the scene entirely — removes the pdbe-managed structure AND any
       raw-builder appends (clearFocus only resets the camera/selection, so the
       last toggled-off model used to linger on screen). Use this when no models
       remain so "opt-out" of the final structure actually hides it. */
    PhonosViewer.prototype.clear = function () {
        if (!this._initialized || !this.instance) return Promise.resolve(false);
        var self = this;
        this._appendedRaw = [];
        this._forceBallStick = false;
        this._currentSource = null;
        this._rawData = null;
        this._hasLoadedStructure = false;
        // Keep the user's chosen scheme (so loading another model in the same session
        // honors it) but drop the structure-specific custom overpaint payload.
        this._customSelectData = null;
        return this._queueVisualUpdate(function () {
            return self._removeAllStructures().then(function (r) {
                try { self.instance.visual.clearSelection(); } catch (e) {}
                return r;
            });
        }, false);
    };

    /* ---- Coloring ---------------------------------------------------- */

    /* Map a UI color-scheme key (as used by app.js / view_output) to the engine's
       internal _colorScheme tag, or null when it isn't an engine-replayable scheme
       (e.g. rainbow / hydrophobicity, which the caller rebuilds per structure). */
    function colorSchemeTag(scheme) {
        if (scheme === 'plddt' || scheme === 'confidence') return 'confidence';
        if (scheme === 'chainid' || scheme === 'chain') return 'chain';
        if (scheme === 'default' || scheme === 'element') return 'default';
        return null;
    }

    /* The three public color methods record that the user has taken control of the
       scheme (so a subsequent protein load keeps it instead of reverting to the
       pLDDT default), then delegate to the worker that actually paints. The workers
       set _colorScheme but NOT _colorUserSet, so the auto-default path and the
       after-reload re-apply can reuse them without claiming the user picked. */
    PhonosViewer.prototype.applyConfidenceColoring = function () {
        this._colorUserSet = true;
        return this._doConfidenceColoring(false);
    };
    PhonosViewer.prototype.applyDefaultColoring = function () {
        this._colorUserSet = true;
        return this._doDefaultColoring(false);
    };
    PhonosViewer.prototype.applyChainColoring = function () {
        this._colorUserSet = true;
        return this._doChainColoring(false);
    };
    PhonosViewer.prototype.applySpectrumColoring = function () {
        this._colorUserSet = true;
        return this._doSpectrumColoring(false);
    };
    PhonosViewer.prototype.applyHydrophobicityColoring = function () {
        this._colorUserSet = true;
        return this._doHydrophobicityColoring(false);
    };

    /* Shared tail for the two ramp schemes: paint, remember the payload as a
       'custom' scheme so _reapplyColorScheme() can replay it after a style rebuild,
       and toast. Returns false (without touching the current scheme) when the
       structure carries no per-residue backbone data to paint. */
    PhonosViewer.prototype._applyRampColoring = function (selectData, silent, message, emptyMessage) {
        if (!selectData || !selectData.length) {
            if (!silent) showToast(this, emptyMessage);
            return Promise.resolve(false);
        }
        var self = this;
        this._currentAlphaFoldView = false;
        this._colorScheme = 'custom';
        this._customSelectData = { data: selectData };
        return this._queueVisualUpdate(function () {
            try { return self.instance.visual.select({ data: selectData }); } catch (e) {}
        }, false).then(function () {
            if (!silent) showToast(self, message);
            return true;
        });
    };

    /* Rainbow N→C per chain (blue N-terminus → red C-terminus). */
    PhonosViewer.prototype._doSpectrumColoring = function (silent) {
        if (!this._initialized) return Promise.resolve(false);
        var byChain = (this.confidenceData && this.confidenceData.byChain) || {};
        var data = buildRampSelectData(byChain, function (entry, ctx) {
            var span = (ctx.last - ctx.first) || 1;
            return spectrumColor((entry.resi - ctx.first) / span);
        });
        this._rampScheme = 'spectrum';
        return this._applyRampColoring(
            data, silent,
            window.i18n ? window.i18n.t('phonosViewer.rainbowColoring') : 'Rainbow (N → C)',
            window.i18n ? window.i18n.t('phonosViewer.noResidueData') : 'No per-residue data to color'
        );
    };

    /* Kyte-Doolittle hydrophobicity per residue. */
    PhonosViewer.prototype._doHydrophobicityColoring = function (silent) {
        if (!this._initialized) return Promise.resolve(false);
        var byChain = (this.confidenceData && this.confidenceData.byChain) || {};
        var data = buildRampSelectData(byChain, function (entry) {
            var kd = KYTE_DOOLITTLE[entry.resn];
            if (kd === undefined) return null;
            return rampColor(HYDRO_STOPS, (kd + 4.5) / 9);
        });
        this._rampScheme = 'hydrophobicity';
        return this._applyRampColoring(
            data, silent,
            window.i18n ? window.i18n.t('phonosViewer.hydrophobicityColoring') : 'Hydrophobicity (Kyte-Doolittle)',
            window.i18n ? window.i18n.t('phonosViewer.noAminoAcidData') : 'No amino-acid residues to color'
        );
    };

    PhonosViewer.prototype._doConfidenceColoring = function (silent) {
        if (!this._initialized) return Promise.resolve(false);
        var self = this;
        this._currentAlphaFoldView = true;
        this._colorScheme = 'confidence';
        this._customSelectData = null;
        this._rampScheme = null;

        // visual.select() overpaint is the ONLY reliable way to apply per-residue pLDDT
        // colors on custom blob-URL structures. visual.update({alphafoldView:true}) is
        // silently ignored by pdbe-molstar for non-EBI-AlphaFold URLs. The ligand has no
        // CA atoms so it is absent from the select data and keeps its element (CPK) colors.
        if (confidenceHasScores(this.confidenceData)) {
            var selectData = buildPLDDTSelectData(this.confidenceData.scores);
            if (!selectData.length) return Promise.resolve(false);
            return this._queueVisualUpdate(function () {
                // Drop any previous overpaint (chain / rainbow / transient highlight)
                // first so no stale layer survives underneath the pLDDT bands.
                try { self.instance.visual.clearSelection(); } catch (e) {}
                return self.instance.visual.select({ data: selectData });
            }, false).then(function () {
                if (!silent) showToast(self, window.i18n ? window.i18n.t('phonosViewer.plddtColoring') : 'pLDDT confidence coloring');
                return true;
            });
        }

        // No pLDDT in this structure (experimental input, B = 0 design output, ...):
        // show the native scheme instead of leaving a stale overpaint while claiming
        // "confidence". The intent is kept, so a later prediction load re-applies it.
        if (!silent) showToast(this, window.i18n ? window.i18n.t('phonosViewer.noConfidenceDataLoadFirst') : 'No confidence data — load a predicted structure first');
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
        }, false).then(function () { return false; });
    };

    PhonosViewer.prototype._doDefaultColoring = function (silent) {
        if (!this._initialized) return Promise.resolve(false);
        var self = this;
        this._currentAlphaFoldView = false;
        this._colorScheme = 'default';
        this._customSelectData = null;
        this._rampScheme = null;

        // clearSelection() removes any visual.select() overpaint — Mol* default is chain coloring
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
        }, false).then(function () {
            if (!silent) showToast(self, window.i18n ? window.i18n.t('phonosViewer.defaultColoring') : 'Default coloring');
            return true;
        });
    };

    PhonosViewer.prototype._doChainColoring = function (silent) {
        if (!this._initialized) return Promise.resolve(false);
        var self = this;
        this._currentAlphaFoldView = false;
        this._colorScheme = 'chain';
        this._customSelectData = null;
        this._rampScheme = null;

        // Build per-chain color data
        if (this.confidenceData && Object.keys(this.confidenceData.byChain).length > 0) {
            var chains = Object.keys(this.confidenceData.byChain);
            var data = [];
            for (var i = 0; i < chains.length; i++) {
                var cd = this.confidenceData.byChain[chains[i]];
                if (!cd || cd.length === 0) continue;
                var authNums = cd.map(entryAuthResi).filter(function (n) { return n != null && !isNaN(n); });
                if (!authNums.length) continue;
                data.push(residueRunEntry(entryAuthChain(cd[0]),
                    Math.min.apply(null, authNums), Math.max.apply(null, authNums),
                    CHAIN_COLORS[i % CHAIN_COLORS.length]));
            }
            if (data.length > 0) {
                var chainData = data;
                return this._queueVisualUpdate(function () {
                    try { self.instance.visual.clearSelection(); } catch (e) {}
                    return self.instance.visual.select({ data: chainData });
                }, false).then(function () {
                    if (!silent) showToast(self, window.i18n ? window.i18n.t('phonosViewer.chainColoring') : 'Chain coloring');
                    return true;
                });
            }
        }

        // No chain data — just clear to get Mol* default (which is chain-based)
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
        }, false).then(function () {
            if (!silent) showToast(self, window.i18n ? window.i18n.t('phonosViewer.chainColoring') : 'Chain coloring');
            return true;
        });
    };

    /* "What color should a freshly chosen-by-the-engine default be?" — confidence when
       the structure carries pLDDT scores, otherwise Mol*'s native preset. Marks the
       scheme as engine-chosen (NOT user-set) so an explicit pick still wins later. */
    PhonosViewer.prototype.applyDefaultColorScheme = function () {
        var hasConf = confidenceHasScores(this.confidenceData);
        if (this._autoConfidenceColor && hasConf) return this._doConfidenceColoring(true);
        // Leave Mol*'s native coloring (chain for polymers, element/CPK for ligands).
        this._colorScheme = null;
        this._customSelectData = null;
        this._rampScheme = null;
        return Promise.resolve(false);
    };

    /* Re-assert the active color scheme after a fullLoad reload wiped the overpaint.
       Re-derives confidence / chain from the (possibly new) confidenceData, or replays
       the stored custom select payload. A no-op for the 'auto' (null) scheme. Runs as a
       fresh queued op — never call this from INSIDE a queued task (it would deadlock). */
    PhonosViewer.prototype._reapplyColorScheme = function () {
        var self = this;
        switch (this._colorScheme) {
            case 'confidence': return this._doConfidenceColoring(true);
            case 'chain':      return this._doChainColoring(true);
            case 'default':    return this._doDefaultColoring(true);
            case 'custom':
                if (this._customSelectData) {
                    var d = this._customSelectData;
                    return this._queueVisualUpdate(function () {
                        try { return self.instance.visual.select({ data: d.data, nonSelectedColor: d.nonSelectedColor }); } catch (e) {}
                    }, false);
                }
                return Promise.resolve(false);
            default: return Promise.resolve(false);
        }
    };

    /* Color a newly loaded base structure: keep the user's explicit scheme (re-derived
       for this structure) if they set one, otherwise apply the engine default. 'custom'
       schemes are structure-specific (built from the previous sequence) so they are NOT
       replayed across a protein switch — the caller rebuilds them. */
    PhonosViewer.prototype._applyInitialColor = function () {
        if (this._colorUserSet) {
            if (this._colorScheme === 'custom') {
                // Ramp schemes (rainbow / hydrophobicity) are derived from the residue
                // list, so a stale payload built for the previous structure must not be
                // replayed — rebuild it from the new confidenceData instead. Any other
                // custom overpaint stays the caller's responsibility.
                if (this._rampScheme === 'spectrum') return this._doSpectrumColoring(true);
                if (this._rampScheme === 'hydrophobicity') return this._doHydrophobicityColoring(true);
                return Promise.resolve(false);
            }
            return this._reapplyColorScheme();
        }
        return this.applyDefaultColorScheme();
    };

    /* Keep the panel's Representation row in step with the active style, whether the
       change came from the panel itself or from a host page calling setVisualStyle(). */
    PhonosViewer.prototype._syncStyleButtons = function () {
        if (!this._panel) return;
        var active = this._visualStyle;
        qsa(this._panel, '[data-style]').forEach(function (b) {
            b.classList.toggle('active', normalizeVisualStyle(b.dataset.style) === active);
        });
    };

    PhonosViewer.prototype.setVisualStyle = function (style) {
        if (!this._initialized) return Promise.resolve(false);
        var normalized = normalizeVisualStyle(style);
        // Overlay reps (added by setDisplayLayers) live as extra children of the
        // structure component. Switching to a single native style must tear them
        // out via the reload below, so the unchanged-style fast-skip MUST NOT fire
        // while they exist — otherwise the old overlay layers linger as ghost reps
        // (e.g. Cartoon stays visible after toggling back to a single Surface
        // display, the "overlay <-> single switching not working" bug).
        var hasLayerReps = !!(this._layerReps && Object.keys(this._layerReps).length);
        // Already rendering this exact named style on a loaded structure — skip the
        // costly full _reloadCurrentStructure() rebuild. A pure recolor / visibility
        // change in the UI routes through here too (applyStyle always calls
        // setVisualStyle), so without this guard every colour click needlessly tore
        // down and rebuilt the whole structure, which is what made switching displays
        // back-and-forth feel sluggish/broken. Skipping when unchanged is safe: when
        // the style genuinely differs we still fall through to the rebuild below.
        if (this._hasLoadedStructure && !this._visualStyleDefault &&
            normalized === this._visualStyle && !hasLayerReps) {
            return Promise.resolve(false);
        }
        this._visualStyle = normalized;
        this._visualStyleDefault = false;
        this._syncStyleButtons();
        // The reload (fullLoad) rebuilds the structure subtree, removing any overlay
        // reps; drop their now-dead registry refs so a later skipStyleReload colour
        // change can't take the in-place re-theme branch on stale selections.
        this._layerReps = {};
        return this._reloadCurrentStructure();
    };

    /* Set the visual style WITHOUT reloading (so the next loadData renders once with
       it). Pass a falsy style to use PDBeMolstar's native smart preset (fast:
       cartoon polymer + ball-and-stick ligands), which avoids a rebuild on load. */
    PhonosViewer.prototype.setStyleSilently = function (style) {
        this._visualStyle = style ? normalizeVisualStyle(style) : null;
        this._visualStyleDefault = !style;
    };

    /* Switch to PDBeMolstar's native smart preset (cartoon + ligand ball-and-stick),
       reloading the current structure unless reload === false. */
    PhonosViewer.prototype.useNativePreset = function (reload) {
        this._visualStyle = null;
        this._visualStyleDefault = true;
        this._layerReps = {};
        if (reload === false) return Promise.resolve(true);
        return this._reloadCurrentStructure();
    };

    /* Tear down the raw overlay reps that setDisplayLayers added (keeping the loaded
       structure) so the canvas genuinely empties. Used when the user toggles every
       display layer off while in overlay mode — setStyleSilently(null) alone records
       the style but leaves the old overlay reps on screen. No-op without overlay reps. */
    PhonosViewer.prototype.clearOverlayLayers = function () {
        var plugin = this.instance && this.instance.plugin;
        this._layerReps = {};
        if (!plugin || !plugin.managers || !plugin.managers.structure ||
            !plugin.managers.structure.component) return Promise.resolve(false);
        var hier = plugin.managers.structure.hierarchy;
        var current = hier && hier.current;
        var structures = current && current.structures;
        if (!structures || !structures.length) return Promise.resolve(false);
        var list = structures.slice();
        return this._queueVisualUpdate(function () {
            return Promise.resolve(plugin.managers.structure.component.clear(list))
                .then(function () { return true; })
                .catch(function (e) { console.warn('[PhonosViewer] clearOverlayLayers failed', e); return false; });
        }, false);
    };

    /* ---- Layered overlay displays ------------------------------------ */

    /* True when the raw Mol* representation API needed for overlaying multiple
       displays (cartoon + surface + ball-and-stick ...) is available. */
    PhonosViewer.prototype.supportsDisplayLayers = function () {
        var p = this.instance && this.instance.plugin;
        return !!(p && p.builders && p.builders.structure &&
            p.builders.structure.representation &&
            p.builders.structure.representation.addRepresentation &&
            p.builders.structure.tryCreateComponentStatic &&
            p.managers && p.managers.structure && p.managers.structure.component &&
            p.managers.structure.hierarchy && p.managers.structure.hierarchy.current);
    };

    /* Render an arbitrary set of overlaid display layers on the current structure.
         layers:     [{ type, on, opacity(0..1), color('#rrggbb'|null) }]   (polymer/all target)
         ligandLayer:{ on, style, opacity(0..1), color('#rrggbb'|null) }    (ligand target)
         colorScheme: key driving 'Auto' (null color) layers — see COLOR_THEME_MAP
       Falls back to a single PDBeMolstar visualStyle when the raw API is unavailable
       or any step throws, so the viewer never ends up blank. */
    PhonosViewer.prototype.setDisplayLayers = function (layers, ligandLayer, colorScheme) {
        var self = this;
        return this._queueVisualUpdate(function () {
            return self._applyDisplayLayers(layers, ligandLayer, colorScheme);
        }, false);
    };

    PhonosViewer.prototype._fallbackSingleStyle = function (layers) {
        var first = (layers || []).filter(function (l) { return l && l.on; })[0];
        if (!first) return Promise.resolve(false);
        this._visualStyle = normalizeVisualStyle(first.type);
        return this._reloadCurrentStructure();
    };

    PhonosViewer.prototype._applyDisplayLayers = function (layers, ligandLayer, colorScheme) {
        var self = this;
        if (!this.supportsDisplayLayers()) return this._fallbackSingleStyle(layers);

        var plugin = this.instance.plugin;
        var current = plugin.managers.structure.hierarchy.current;
        var structures = current && current.structures;
        if (!structures || !structures.length) return Promise.resolve(false);

        var themeName = COLOR_THEME_MAP[colorScheme] || 'chain-id';
        var addRep = plugin.builders.structure.representation.addRepresentation.bind(
            plugin.builders.structure.representation);
        var mkComp = plugin.builders.structure.tryCreateComponentStatic.bind(
            plugin.builders.structure);

        var repFor = function (type, opacity, color, autoTheme) {
            var molColor = color ? hexToMolColor(color) : null;
            var params = {
                type: molstarRepType(type),
                typeParams: { alpha: (typeof opacity === 'number' ? opacity : 1) }
            };
            if (molColor != null) { params.color = 'uniform'; params.colorParams = { value: molColor }; }
            else { params.color = autoTheme; }
            return params;
        };

        self._layerColorScheme = colorScheme;
        // Record the engine-level scheme tag (without claiming a user pick) so an
        // overlay→single transition's reload re-applies the right overpaint.
        self._colorScheme = colorSchemeTag(colorScheme);

        // `added` lives in the OUTER scope rather than being read off the build's
        // return value: plugin.dataTransaction() awaits its callback but resolves to
        // `undefined` (it discards whatever the callback returns), so reading the
        // count off that promise always saw `undefined`. That made EVERY overlay
        // build wrongly fall through to _fallbackSingleStyle — only the first layer
        // ever rendered (the "multiple overlay not working" bug), the overlay reps
        // were torn down by the fallback reload, and _layerReps was left stale.
        // Capturing the count via closure makes it independent of dataTransaction.
        var added = 0;
        var build = async function () {
            // Reset the per-layer representation registry INSIDE the async build
            // so in-place opacity/colour updates queued between now and the first
            // await don't see an empty registry and trigger a redundant full rebuild.
            self._layerReps = {};
            added = 0;
            for (var si = 0; si < structures.length; si++) {
                var structure = structures[si];
                try { await plugin.managers.structure.component.clear([structure]); } catch (e) {}

                var ref = structure.cell;
                var baseComp = null;
                var hasPolymer = false;
                try { baseComp = await mkComp(ref, 'polymer'); } catch (e) {}
                if (baseComp) { hasPolymer = true; }
                else { try { baseComp = await mkComp(ref, 'all'); } catch (e) {} }

                if (baseComp) {
                    for (var li = 0; li < (layers || []).length; li++) {
                        var layer = layers[li];
                        if (!layer || !layer.on) continue;
                        try {
                            var sel = await addRep(baseComp, repFor(layer.type, layer.opacity, layer.color, themeName));
                            if (si === 0) self._layerReps[layer.type] = sel;
                            added++;
                        } catch (e) { console.warn('[PhonosViewer] addRepresentation failed:', layer.type, e); }
                    }
                }

                // The separate ligand layer only makes sense when there's a distinct
                // polymer to contrast it against. For a bare small molecule the base
                // component is already 'all', so a 'ligand' layer would double-render
                // (z-fighting) the same atoms — skip it.
                if (ligandLayer && ligandLayer.on !== false && hasPolymer) {
                    var ligComp = null;
                    try { ligComp = await mkComp(ref, 'ligand'); } catch (e) {}
                    if (ligComp) {
                        try {
                            var ligSel = await addRep(ligComp, repFor(
                                ligandLayer.style || 'ball-and-stick',
                                ligandLayer.opacity, ligandLayer.color, 'element-symbol'));
                            if (si === 0) self._layerReps.__ligand = ligSel;
                            added++;
                        } catch (e) { console.warn('[PhonosViewer] ligand addRepresentation failed:', e); }
                    }
                }
            }
            return added;
        };

        // Batch the whole rebuild into one transaction so the canvas repaints once.
        var run = plugin.dataTransaction
            ? plugin.dataTransaction(build, 'phonos-display-layers')
            : build();

        return Promise.resolve(run).then(function () {
            if (!added) return self._fallbackSingleStyle(layers);
            self._fireLoaded();
            // Rebuilding the reps clears any prior overpaint. For overpaint-based
            // schemes (pLDDT) the neutral base theme is now on every layer, so paint
            // the per-residue confidence colors on top; otherwise drop stale overpaint.
            // Use the worker (silent, no user-pick claim) so an overlay build during an
            // initial load doesn't lock the scheme in as user-chosen.
            if (OVERPAINT_SCHEMES[colorScheme]) { try { self._doConfidenceColoring(true); } catch (e) {} }
            else { try { self.instance.visual.clearSelection(); } catch (e) {} }
            return true;
        }).catch(function (e) {
            console.warn('[PhonosViewer] setDisplayLayers failed, falling back to single style', e);
            return self._fallbackSingleStyle(layers);
        });
    };

    /* In-place update of a single representation's transform params (no rebuild).
       `mutate(params)` returns the new params object. */
    PhonosViewer.prototype._updateRepTransform = function (sel, mutate) {
        var plugin = this.instance && this.instance.plugin;
        if (!plugin || !sel) return Promise.resolve(false);
        var ref = (sel.ref != null) ? sel.ref : sel;
        try {
            var cell = plugin.state.data.cells.get(ref);
            if (!cell || !cell.transform || !cell.transform.transformer) return Promise.resolve(false);
            var update = plugin.build();
            update.to(ref).update(cell.transform.transformer, function (old) { return mutate(old) || old; });
            return update.commit();
        } catch (e) {
            console.warn('[PhonosViewer] _updateRepTransform failed', e);
            return Promise.resolve(false);
        }
    };

    /* Smoothly change one layer's opacity (0..1) without rebuilding the scene.
       layerKey: a display type ('cartoon'...) or '__ligand'. */
    PhonosViewer.prototype.updateDisplayLayerOpacity = function (layerKey, opacity) {
        var sel = this._layerReps && this._layerReps[layerKey];
        if (!sel) return Promise.resolve(false);
        var a = Math.max(0, Math.min(1, (typeof opacity === 'number' ? opacity : 1)));
        return this._updateRepTransform(sel, function (old) {
            return Object.assign({}, old, {
                type: Object.assign({}, old.type, {
                    params: Object.assign({}, old.type && old.type.params, { alpha: a })
                })
            });
        });
    };

    /* True when an overlay representation currently exists for this layer key,
       i.e. an in-place opacity/colour update can be applied without a rebuild. */
    PhonosViewer.prototype.hasDisplayLayer = function (layerKey) {
        return !!(this._layerReps && this._layerReps[layerKey]);
    };

    /* Smoothly set one layer's custom (uniform) colour without rebuilding. Pass a
       hex string for a solid colour. For 'Auto' (theme) colour, rebuild instead. */
    PhonosViewer.prototype.updateDisplayLayerColor = function (layerKey, hex) {
        var sel = this._layerReps && this._layerReps[layerKey];
        if (!sel) return Promise.resolve(false);
        var molColor = hexToMolColor(hex);
        if (molColor == null) return Promise.resolve(false);
        return this._updateRepTransform(sel, function (old) {
            return Object.assign({}, old, { colorTheme: { name: 'uniform', params: { value: molColor } } });
        });
    };

    /* Update the color theme on all overlay layers that use 'Auto' (theme) coloring
       without triggering a full rebuild. Layers with an explicit uniform color are
       left untouched. Called on color-scheme dropdown changes in overlay mode. */
    PhonosViewer.prototype.updateDisplayLayerColorTheme = function (scheme, layerStates, ligandState) {
        var self = this;
        var isOverpaint = !!OVERPAINT_SCHEMES[scheme];
        var themeName = COLOR_THEME_MAP[scheme] || 'chain-id';
        var keys = self._layerReps ? Object.keys(self._layerReps) : [];
        var promises = [];
        for (var i = 0; i < keys.length; i++) {
            var key = keys[i];
            var state;
            if (key === '__ligand') {
                state = ligandState;
            } else if (layerStates && layerStates[key]) {
                state = layerStates[key];
            }
            // Only update layers whose color is null (use theme); uniform-color layers stay.
            if (state && state.color == null) {
                // IIFE so each deferred mutate closure captures its OWN sel/theme —
                // a shared `var t` would leave every layer with the last loop value
                // (the "multiple overlays mess up colours" bug).
                (function (sel, t) {
                    promises.push(self._updateRepTransform(sel, function (old) {
                        return Object.assign({}, old, { colorTheme: { name: t, params: {} } });
                    }));
                })(self._layerReps[key], key === '__ligand' ? 'element-symbol' : themeName);
            }
        }
        self._layerColorScheme = scheme;
        // This is a user-driven dropdown change in overlay mode, so record both the
        // scheme tag and that the user has taken control (keeps it across reloads/loads).
        self._colorScheme = colorSchemeTag(scheme);
        self._colorUserSet = true;
        self._customSelectData = null;
        // The per-rep theme above only sets the neutral base. Apply (or clear) the
        // pLDDT overpaint on top so switching schemes in overlay mode stays correct.
        return Promise.all(promises).then(function () {
            if (isOverpaint) { return self._doConfidenceColoring(true); }
            return self._queueVisualUpdate(function () {
                try { self.instance.visual.clearSelection(); } catch (e) {}
            }, false);
        });
    };

    /* ---- Selection --------------------------------------------------- */

    /* Build a StructureElement.Loci for a single residue using raw Mol* structure data.
       Returns null if the Mol* internals are unavailable or the residue is not found. */
    PhonosViewer.prototype._tryBuildResidueLoci = function (plugin, chainId, resSeqNum) {
        try {
            var structures = plugin.managers.structure.hierarchy.current.structures;
            if (!structures || !structures.length) return null;
            var structCell = structures[0].cell;
            if (!structCell || !structCell.obj) return null;
            var structure = structCell.obj.data;
            if (!structure || !structure.units) return null;

            var resultElements = [];
            for (var ui = 0; ui < structure.units.length; ui++) {
                var unit = structure.units[ui];
                if (!unit || !unit.model) continue;
                var ah = unit.model.atomicHierarchy;
                if (!ah || !ah.residueAtomSegments || !ah.chainAtomSegments ||
                    !ah.residues || !ah.chains) continue;
                var elements = unit.elements;
                if (!elements) continue;

                var unitIndices = [];
                for (var ei = 0; ei < elements.length; ei++) {
                    var atomIdx = elements[ei];
                    var residueIdx = ah.residueAtomSegments.index[atomIdx];
                    // chainAtomSegments.index is indexed by ATOM, not residue.
                    var chainIdx = ah.chainAtomSegments.index[atomIdx];
                    var authSeqId = ah.residues.auth_seq_id && ah.residues.auth_seq_id.value(residueIdx);
                    var authAsymId = ah.chains.auth_asym_id && ah.chains.auth_asym_id.value(chainIdx);
                    if (authSeqId === resSeqNum && authAsymId === chainId) {
                        unitIndices.push(ei);
                    }
                }
                if (unitIndices.length > 0) {
                    resultElements.push({ unit: unit, indices: new Int32Array(unitIndices) });
                }
            }
            if (!resultElements.length) return null;
            return { kind: 'element-loci', structure: structure, elements: resultElements };
        } catch (e) {
            return null;
        }
    };

    /* Generalized loci builder: collect atoms whose (authAsymId, authSeqId, compId)
       satisfy matchFn. Returns a Mol* StructureElement.Loci or null. */
    PhonosViewer.prototype._buildLociByPredicate = function (plugin, matchFn) {
        try {
            var structures = plugin.managers.structure.hierarchy.current.structures;
            if (!structures || !structures.length) return null;
            var structCell = structures[0].cell;
            if (!structCell || !structCell.obj) return null;
            var structure = structCell.obj.data;
            if (!structure || !structure.units) return null;

            var resultElements = [];
            for (var ui = 0; ui < structure.units.length; ui++) {
                var unit = structure.units[ui];
                if (!unit || !unit.model) continue;
                var ah = unit.model.atomicHierarchy;
                if (!ah || !ah.residueAtomSegments || !ah.chainAtomSegments ||
                    !ah.residues || !ah.chains) continue;
                var elements = unit.elements;
                if (!elements) continue;

                var unitIndices = [];
                for (var ei = 0; ei < elements.length; ei++) {
                    var atomIdx = elements[ei];
                    var residueIdx = ah.residueAtomSegments.index[atomIdx];
                    // chainAtomSegments.index is indexed by ATOM, not residue.
                    var chainIdx = ah.chainAtomSegments.index[atomIdx];
                    var authSeqId = ah.residues.auth_seq_id && ah.residues.auth_seq_id.value(residueIdx);
                    var authAsymId = ah.chains.auth_asym_id && ah.chains.auth_asym_id.value(chainIdx);
                    var compId = ah.atoms && ah.atoms.auth_comp_id ? ah.atoms.auth_comp_id.value(atomIdx)
                        : (ah.residues.label_comp_id ? ah.residues.label_comp_id.value(residueIdx) : '');
                    if (matchFn(authAsymId, authSeqId, compId)) unitIndices.push(ei);
                }
                if (unitIndices.length > 0) {
                    resultElements.push({ unit: unit, indices: new Int32Array(unitIndices) });
                }
            }
            if (!resultElements.length) return null;
            return { kind: 'element-loci', structure: structure, elements: resultElements };
        } catch (e) {
            return null;
        }
    };

    /* Loci for a set of residue ranges: [{ chain, start, end }]. */
    PhonosViewer.prototype._buildRangesLoci = function (plugin, selections) {
        var sels = (selections || []).map(function (s) {
            return { chain: s.chain, start: (s.start != null ? s.start : s.resi),
                     end: (s.end != null ? s.end : (s.start != null ? s.start : s.resi)) };
        });
        return this._buildLociByPredicate(plugin, function (chainId, seqId) {
            for (var i = 0; i < sels.length; i++) {
                var s = sels[i];
                if (s.chain && chainId !== s.chain) continue;
                if (seqId >= s.start && seqId <= s.end) return true;
            }
            return false;
        });
    };

    /* Loci for one ligand instance, matched by chain+residue number (preferred) or
       comp id (fallback when chain/number are unknown). */
    PhonosViewer.prototype._buildLigandLoci = function (plugin, chain, resNum, compId) {
        var num = (resNum != null && resNum !== '') ? parseInt(resNum, 10) : null;
        var comp = compId ? String(compId).toUpperCase() : null;
        return this._buildLociByPredicate(plugin, function (chainId, seqId, cId) {
            if (num != null) {
                if (seqId !== num) return false;
                if (chain && chainId !== chain) return false;
                return true;
            }
            return comp && String(cId || '').toUpperCase() === comp;
        });
    };

    /* Display the given ligand residues as ball-and-stick, INDEPENDENT of Mol*'s
       automatic ligand classification. The classifier (tryCreateComponentStatic
       'ligand') misses ligands recorded as group_PDB = ATOM inside a predicted
       complex (Boltz2 / Chai / DiffDock), so the default cartoon preset shows
       nothing for them. We instead build a composition-matched element loci by
       (chain, residue number, comp id) — which works no matter how the atom
       records are tagged — and render it via Mol*'s focus representation
       (ball-and-stick + label). ligs: [{ chain, num, name }]. opts.zoom moves the
       camera to the ligand; omit it to just make the ligand visible on load.
       Purely additive + defensive: a failure to build the loci leaves the scene
       untouched, so this can never regress an already-working render. */
    PhonosViewer.prototype.showLigandResidues = function (ligs, opts) {
        if (!this._initialized || !this.instance) return Promise.resolve(false);
        if (!Array.isArray(ligs) || !ligs.length) return Promise.resolve(false);
        var self = this;
        opts = opts || {};
        var wanted = ligs.map(function (l) {
            return {
                chain: l.chain || null,
                num: (l.num != null && l.num !== '') ? parseInt(l.num, 10) : null,
                name: (l.name || '').toUpperCase()
            };
        });
        return this._queueVisualUpdate(function () {
            var plugin = self.instance.plugin;
            var loci = self._buildLociByPredicate(plugin, function (chainId, seqId, cId) {
                for (var i = 0; i < wanted.length; i++) {
                    var w = wanted[i];
                    if (w.num != null) {
                        if (seqId === w.num && (!w.chain || chainId === w.chain)) return true;
                    } else if (w.name && String(cId || '').toUpperCase() === w.name) {
                        return true;
                    }
                }
                return false;
            });
            if (!loci) return false;
            var shown = self._focusLociRep(loci);
            self._lastLigandLoci = loci;
            if (opts.zoom) self._zoomToLoci(loci, 5);
            return !!shown;
        }, false);
    };

    /* Zoom the camera to the ligand residues last shown via showLigandResidues(),
       or to the supplied ligs if given. Convenience wrapper for an explicit
       "zoom to ligand" control. */
    PhonosViewer.prototype.zoomToLigands = function (ligs) {
        if (ligs && ligs.length) return this.showLigandResidues(ligs, { zoom: true });
        if (this._lastLigandLoci) { this._zoomToLoci(this._lastLigandLoci, 5); return Promise.resolve(true); }
        return Promise.resolve(false);
    };

    /* ---- Raw Mol* camera / highlight / focus (work with overlay reps) --- */

    PhonosViewer.prototype._zoomToLoci = function (loci, extraRadius) {
        try {
            var cam = this.instance && this.instance.plugin && this.instance.plugin.managers.camera;
            if (cam && cam.focusLoci) { cam.focusLoci(loci, { extraRadius: extraRadius == null ? 4 : extraRadius, durationMs: 260 }); return true; }
        } catch (e) {}
        return false;
    };

    PhonosViewer.prototype._highlightLoci = function (loci) {
        try {
            var inter = this.instance && this.instance.plugin && this.instance.plugin.managers.interactivity;
            if (inter && inter.lociHighlights && inter.lociHighlights.highlightOnly) {
                inter.lociHighlights.highlightOnly({ loci: loci });
                return true;
            }
        } catch (e) {}
        return false;
    };

    PhonosViewer.prototype._clearHighlightLoci = function () {
        try {
            var inter = this.instance && this.instance.plugin && this.instance.plugin.managers.interactivity;
            if (inter && inter.lociHighlights && inter.lociHighlights.clearHighlights) {
                inter.lociHighlights.clearHighlights();
                return true;
            }
        } catch (e) {}
        return false;
    };

    /* ---- Measurement tools (distance / angle) ----------------------------
       Built on Mol*'s own click-interaction behavior (plugin.behaviors
       .interaction.click) to pick atoms, and its structure measurement
       manager (plugin.managers.structure.measurement) to render the
       distance/angle representations - the same manager backing Mol*
       Viewer's own built-in "Measurements" panel. No new 3D engine or
       vendor library is introduced (NGL's dead measurement code elsewhere
       in this codebase is being retired separately, not reused).

       Design choice: measurements are cleared on every loadData()/restyle
       reload (added below in the reload path) rather than re-anchored,
       since the underlying structure/atom indices can change across a
       reload (e.g. a different file loaded) - simpler and safer than
       trying to re-resolve stale loci against new structure data. */

    PhonosViewer.prototype._setMeasurementMode = function (mode) {
        this._measurementMode = mode;
        this._measurementPicks = [];
        this._ensureMeasurementClickSub();
        return !!mode;
    };

    PhonosViewer.prototype._ensureMeasurementClickSub = function () {
        if (this._measurementClickSub) return true;
        try {
            var plugin = this.instance && this.instance.plugin;
            var click = plugin && plugin.behaviors && plugin.behaviors.interaction && plugin.behaviors.interaction.click;
            if (!click || !click.subscribe) {
                console.warn('[PhonosViewer] measurement tools: plugin.behaviors.interaction.click not available in this Mol* build - measurement picking disabled.');
                return false;
            }
            var self = this;
            this._measurementClickSub = click.subscribe(function (event) {
                self._onMeasurementClick(event);
            });
            return true;
        } catch (e) {
            console.warn('[PhonosViewer] measurement tools: failed to subscribe to click events.', e);
            return false;
        }
    };

    PhonosViewer.prototype._onMeasurementClick = function (event) {
        if (!this._measurementMode) return;
        try {
            var loci = event && event.current && event.current.loci;
            if (!loci || loci.kind !== 'element-loci' || !loci.elements || !loci.elements.length) return;

            this._measurementPicks = this._measurementPicks || [];
            this._measurementPicks.push(loci);

            var needed = this._measurementMode === 'angle' ? 3 : 2;
            if (this._measurementPicks.length < needed) {
                showToast(this, (window.i18n ? window.i18n.t('phonosViewer.measurePickMore') : 'Picked ') +
                    this._measurementPicks.length + '/' + needed);
                return;
            }

            var picks = this._measurementPicks;
            this._measurementPicks = [];
            if (this._measurementMode === 'distance') this._addDistanceMeasurement(picks[0], picks[1]);
            else if (this._measurementMode === 'angle') this._addAngleMeasurement(picks[0], picks[1], picks[2]);
        } catch (e) {
            console.warn('[PhonosViewer] measurement click handling failed.', e);
        }
    };

    PhonosViewer.prototype._measurementManager = function () {
        try {
            var plugin = this.instance && this.instance.plugin;
            return plugin && plugin.managers && plugin.managers.structure && plugin.managers.structure.measurement;
        } catch (e) { return null; }
    };

    PhonosViewer.prototype._addDistanceMeasurement = function (lociA, lociB) {
        var mgr = this._measurementManager();
        if (!mgr || !mgr.addDistance) {
            console.warn('[PhonosViewer] measurement tools: structure.measurement.addDistance not available in this Mol* build.');
            showToast(this, window.i18n ? window.i18n.t('phonosViewer.measureUnavailable') : 'Measurement tools unavailable in this viewer build');
            return false;
        }
        try {
            mgr.addDistance(lociA, lociB);
            return true;
        } catch (e) {
            console.warn('[PhonosViewer] addDistance failed.', e);
            return false;
        }
    };

    PhonosViewer.prototype._addAngleMeasurement = function (lociA, lociB, lociC) {
        var mgr = this._measurementManager();
        if (!mgr || !mgr.addAngle) {
            console.warn('[PhonosViewer] measurement tools: structure.measurement.addAngle not available in this Mol* build.');
            showToast(this, window.i18n ? window.i18n.t('phonosViewer.measureUnavailable') : 'Measurement tools unavailable in this viewer build');
            return false;
        }
        try {
            mgr.addAngle(lociA, lociB, lociC);
            return true;
        } catch (e) {
            console.warn('[PhonosViewer] addAngle failed.', e);
            return false;
        }
    };

    /* Remove all rendered distance/angle measurements and exit measurement
       picking mode. Safe to call even if none were added. */
    PhonosViewer.prototype.clearMeasurements = function () {
        this._measurementMode = null;
        this._measurementPicks = [];
        var mgr = this._measurementManager();
        if (mgr && mgr.clear) {
            try { mgr.clear(); return true; } catch (e) { console.warn('[PhonosViewer] clearMeasurements failed.', e); }
        }
        return false;
    };

    PhonosViewer.prototype.enableDistanceMeasurement = function () { return this._setMeasurementMode('distance'); };
    PhonosViewer.prototype.enableAngleMeasurement = function () { return this._setMeasurementMode('angle'); };
    PhonosViewer.prototype.disableMeasurementMode = function () { return this._setMeasurementMode(null); };

    PhonosViewer.prototype._focusLociRep = function (loci) {
        try {
            var sf = this.instance && this.instance.plugin && this.instance.plugin.managers.structure &&
                this.instance.plugin.managers.structure.focus;
            if (sf && sf.setFromLoci) { sf.setFromLoci(loci); return true; }
        } catch (e) {}
        return false;
    };

    /* Zoom to and pocket-focus a ligand (ball-and-stick + surrounding residues +
       camera). Mol*'s focus representation auto-includes the interacting shell. */
    PhonosViewer.prototype.focusLigandResidue = function (chain, resNum, compId) {
        if (!this._initialized || !this.instance) return Promise.resolve(false);
        var self = this;
        return this._queueVisualUpdate(function () {
            var plugin = self.instance.plugin;
            var loci = self._buildLigandLoci(plugin, chain, resNum, compId);
            if (loci) {
                self._focusLociRep(loci);
                self._zoomToLoci(loci, 6);
                return true;
            }
            // Fallback to PDBeMolstar's comp-id based ligand view.
            if (compId) { try { self.instance.visual.update({ ligandView: { label_comp_id: compId } }, false); } catch (e) {} }
            return false;
        }, false);
    };

    /* Zoom to a residue and show it as ball-and-stick with colorful highlight.
       Applies bright color overpaint + dims the rest, then triggers Mol*'s focus
       representation (ball-and-stick + glow) via the raw plugin focus manager. */
    PhonosViewer.prototype.focusBallAndStick = function (chain, resNum, color, opts) {
        if (!this._initialized || !this.instance) return Promise.resolve(false);
        var self = this;
        chain = chain || 'A';
        color = color || '#10b981';
        var doZoom = !opts || opts.zoom !== false;
        var focusEntry = [{ struct_asym_id: chain, start_residue_number: resNum, end_residue_number: resNum }];
        var selectEntry = [{ struct_asym_id: chain, start_residue_number: resNum, end_residue_number: resNum, color: color }];

        return this._queueVisualUpdate(function () {
            var plugin = self.instance.plugin;
            // Preferred path: raw Mol* loci → focus representation (ball-and-stick +
            // surrounding shell) + camera zoom. Works with the overlay reps, unlike
            // PDBeMolstar's visual.focus/select which need its own components.
            var loci = self._tryBuildResidueLoci(plugin, chain, resNum);
            if (loci) {
                self._focusLociRep(loci);
                if (doZoom) self._zoomToLoci(loci, 4);
                self._highlightLoci(loci);
            } else {
                // Fallback to PDBeMolstar's component-based focus/overpaint.
                if (doZoom) { try { self.instance.visual.focus(focusEntry); } catch (e) {} }
                try { self.instance.visual.select({ data: selectEntry, nonSelectedColor: '#1f2937' }); } catch (e) {}
            }
        }, false);
    };

    /* Clear the ball-and-stick focus state set by focusBallAndStick(). */
    PhonosViewer.prototype.clearFocusBallAndStick = function () {
        if (!this._initialized || !this.instance) return;
        var self = this;
        this._clearHighlightLoci();
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
            try {
                var plugin = self.instance.plugin;
                if (plugin && plugin.managers && plugin.managers.structure &&
                    plugin.managers.structure.focus) {
                    plugin.managers.structure.focus.clear();
                }
            } catch (e) {}
        }, false).then(function () {
            // clearSelection() also removed the active color overpaint (pLDDT /
            // chain / rainbow) — put it back so deselecting a residue does not
            // silently revert the structure to default colors.
            return self._reapplyColorScheme();
        });
    };

    /* Drop transient selection overpaint (range highlight, isolate) and restore the
       active color scheme. */
    PhonosViewer.prototype.restoreColoring = function () {
        if (!this._initialized || !this.instance) return Promise.resolve(false);
        var self = this;
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
        }, false).then(function () { return self._reapplyColorScheme(); });
    };

    /* opts.persist === true marks this overpaint as the active 'custom' color scheme
       (rainbow / hydrophobicity) so it is replayed after a restyle reload. Transient
       overpaint (interface highlight, isolate) must NOT pass it, or "Reset" / a display
       switch would resurrect a stale highlight as if it were the chosen coloring. */
    PhonosViewer.prototype.selectResidues = function (selections, nonSelectedColor, opts) {
        if (!this._initialized) return;
        var self = this;
        var data = [];
        for (var i = 0; i < selections.length; i++) {
            var s = selections[i];
            data.push({
                struct_asym_id: s.chain,
                start_residue_number: s.start,
                end_residue_number: s.end,
                color: s.color || '#10b981'
            });
        }
        var nonSel = nonSelectedColor || '#505055';
        if (opts && opts.persist) {
            this._colorUserSet = true;
            this._colorScheme = 'custom';
            this._customSelectData = { data: data, nonSelectedColor: nonSel };
        }
        return this._queueVisualUpdate(function () {
            return self.instance.visual.select({ data: data, nonSelectedColor: nonSel });
        }, false);
    };

    PhonosViewer.prototype.highlightResidues = function (selections) {
        if (!this._initialized) return;
        var self = this;
        // Raw Mol* interactivity highlight works regardless of which components are
        // present (overlay reps included). Run it directly so hover stays responsive.
        var plugin = this.instance && this.instance.plugin;
        var loci = plugin ? this._buildRangesLoci(plugin, selections) : null;
        if (loci && this._highlightLoci(loci)) return;

        var data = [];
        for (var i = 0; i < selections.length; i++) {
            data.push({
                struct_asym_id: selections[i].chain,
                start_residue_number: selections[i].start,
                end_residue_number: selections[i].end
            });
        }
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.highlight({ data: data, color: (selections[0] && selections[0].color) || '#10b981' }); } catch (e) {}
        }, false);
    };

    PhonosViewer.prototype.clearSelection = function () {
        if (!this.instance) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
        }, false);
    };

    PhonosViewer.prototype.clearHighlight = function () {
        if (!this.instance) return;
        if (this._clearHighlightLoci()) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearHighlight(); } catch (e) {}
        }, false);
    };

    /* ---- Visibility -------------------------------------------------- */

    PhonosViewer.prototype.setVisibility = function (vis) {
        if (!this._initialized) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            self.instance.visual.visibility({
                polymer: vis.polymer !== false,
                het: vis.het !== false,
                water: !!vis.water,
                carbs: !!vis.carbs,
                nonStandard: vis.nonStandard !== false,
                maps: !!vis.maps
            });
        }, false);
    };

    /* ---- Tools ------------------------------------------------------- */

    PhonosViewer.prototype.toggleSpin = function (state) {
        if (!this._initialized) return false;
        var self = this;
        this._spinning = (state !== undefined) ? !!state : !this._spinning;
        this._queueVisualUpdate(function () {
            try {
                if (self.instance && self.instance.visual) {
                    self.instance.visual.toggleSpin(self._spinning);
                }
            } catch (e) {
                console.warn('[PhonosViewer] toggleSpin() error:', e);
            }
        }, false);
        return this._spinning;
    };

    PhonosViewer.prototype.getCanvas = function () {
        return this.container ? this.container.querySelector('canvas') : null;
    };

    PhonosViewer.prototype._canvasDataUri = function () {
        var canvas = this.getCanvas();
        try { return canvas ? canvas.toDataURL('image/png') : null; } catch (e) { return null; }
    };

    /* Render a PNG data-URI, preferring Mol*'s off-screen image renderer so we can
       produce a higher-resolution shot than the visible canvas and (optionally) a
       transparent background. Falls back to the live canvas when the helper is
       unavailable or throws — so a screenshot is always produced.
         opts: { scale (default 2), transparent (default false) } */
    PhonosViewer.prototype._renderImageDataUri = function (opts) {
        opts = opts || {};
        var self = this;
        var plugin = this.instance && this.instance.plugin;
        var helper = plugin && plugin.helpers && plugin.helpers.viewportScreenshot;
        if (helper && helper.getImageDataUri) {
            try {
                var scale = opts.scale || 2;
                var canvas = this.getCanvas();
                var w = Math.round((canvas ? canvas.width : 1920) * scale);
                var h = Math.round((canvas ? canvas.height : 1080) * scale);
                var cur = helper.values ||
                    (helper.behaviors && helper.behaviors.values && helper.behaviors.values.value) || {};
                var next = Object.assign({}, cur, {
                    resolution: { name: 'custom', params: { width: w, height: h } },
                    transparent: !!opts.transparent
                });
                try { helper.values = next; }
                catch (e2) {
                    if (helper.behaviors && helper.behaviors.values && helper.behaviors.values.next) {
                        helper.behaviors.values.next(next);
                    }
                }
                return Promise.resolve(helper.getImageDataUri()).then(function (uri) {
                    return uri || self._canvasDataUri();
                }, function () { return self._canvasDataUri(); });
            } catch (e) { /* fall through to canvas */ }
        }
        return Promise.resolve(this._canvasDataUri());
    };

    PhonosViewer.prototype.screenshot = function (opts) {
        var self = this;
        opts = opts || {};
        return this._renderImageDataUri(opts).then(function (uri) {
            if (!uri) { showToast(self, window.i18n ? window.i18n.t('phonosViewer.screenshotUnavailable') : 'Screenshot unavailable'); return; }
            downloadDataUrl(uri, 'phonos-structure-' + nowStamp() + '.png');
            showToast(self, opts.transparent ? (window.i18n ? window.i18n.t('phonosViewer.savedTransparent') : 'Saved (transparent PNG)') : (window.i18n ? window.i18n.t('phonosViewer.screenshotSaved') : 'Screenshot saved'));
        });
    };

    PhonosViewer.prototype.copyScreenshot = function (opts) {
        var self = this;
        return this._renderImageDataUri(opts || {}).then(function (uri) {
            var blob = uri ? dataUriToBlob(uri) : null;
            if (blob && navigator.clipboard && typeof ClipboardItem !== 'undefined') {
                return navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]).then(function () {
                    showToast(self, window.i18n ? window.i18n.t('phonosViewer.copiedToClipboard') : 'Copied to clipboard');
                });
            }
            // Older clipboard path: copy the live canvas, else download.
            var canvas = self.getCanvas();
            if (canvas) {
                return copyImageToClipboard(canvas).then(function () {
                    showToast(self, window.i18n ? window.i18n.t('phonosViewer.copiedToClipboard') : 'Copied to clipboard');
                }, function () { return self.screenshot(opts); });
            }
            return self.screenshot(opts);
        });
    };

    PhonosViewer.prototype.cycleBackground = function () {
        this._bgIndex = (this._bgIndex + 1) % BACKGROUNDS.length;
        var bg = BACKGROUNDS[this._bgIndex];
        if (this.instance) this.instance.canvas.setBgColor(bg.rgb);
        try { localStorage.setItem('phonos_viewer_bg', String(this._bgIndex)); } catch (e) {}
        showToast(this, (window.i18n ? window.i18n.t('phonosViewer.backgroundPrefix') : 'Background: ') + bg.name);
    };

    PhonosViewer.prototype.toggleFullscreen = function () {
        if (!this.container) return;
        if (document.fullscreenElement === this.container) { document.exitFullscreen(); }
        else if (this.container.requestFullscreen) { this.container.requestFullscreen().catch(function () {}); }
    };

    PhonosViewer.prototype.toggleSequencePanel = function (visible) {
        if (!this._initialized) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            return self.instance.visual.update({ sequencePanel: !!visible }, false);
        }, false);
    };

    PhonosViewer.prototype.toggleControls = function (visible) {
        if (!this._initialized) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            self.instance.canvas.toggleControls(!!visible);
        }, false);
    };

    PhonosViewer.prototype.resetView = function () {
        if (!this._initialized) return;
        var self = this;
        return this._queueVisualUpdate(function () {
            try { self.instance.visual.clearSelection(); } catch (e) {}
            try { self.instance.visual.reset({ camera: true, theme: true }); } catch (e) {}
            if (self._spinning) {
                self._spinning = false;
                try { self.instance.visual.toggleSpin(false); } catch (e) {}
            }
        }, false).then(function (value) {
            // reset({theme:true}) drops the color overpaint too — re-assert the active
            // scheme so "Reset" only resets the camera/spin, not the user's coloring.
            return Promise.resolve(self._reapplyColorScheme()).then(function () {
                showToast(self, window.i18n ? window.i18n.t('phonosViewer.viewReset') : 'View reset');
                return value;
            });
        });
    };

    PhonosViewer.prototype.setBgColor = function (r, g, b) {
        if (this.instance) this.instance.canvas.setBgColor({ r: r, g: g, b: b });
    };

    /* ---- Events ------------------------------------------------------ */

    PhonosViewer.prototype._setupEvents = function () {
        var self = this;
        document.addEventListener('PDB.molstar.click', function (e) {
            if (self._destroyed) return;
            self.container.dispatchEvent(new CustomEvent('phonos:click', { detail: e.eventData || e.detail }));
        });
        document.addEventListener('PDB.molstar.mouseover', function (e) {
            if (self._destroyed) return;
            self.container.dispatchEvent(new CustomEvent('phonos:hover', { detail: e.eventData || e.detail }));
        });
    };

    /* ── Atom / residue picking ──────────────────────────────────────────────
       Resolve a Mol* element loci back to the residue it belongs to, so callers
       can react to clicks and hovers in the 3D scene (sync a 2D diagram, show
       an atom readout, ...). This is the inverse of _buildLociByPredicate and
       reads the same auth_* fields, so identifiers round-trip consistently. */
    /* First index + size of a Mol* OrderedSet. Live hover/click loci carry it as an
       Interval packed into a single float64 (two int32: start, end-exclusive), not
       as an array — reading `.length` on that returned undefined, so every real
       hover/click resolved to "nothing" and picking callbacks never fired. */
    var _packF64 = new Float64Array(1);
    var _packI32 = new Int32Array(_packF64.buffer);
    function orderedSetSpan(set) {
        if (set === null || set === undefined) return null;
        if (typeof set === 'number') {
            _packF64[0] = set;
            var start = _packI32[0], end = _packI32[1];
            return end > start ? { first: start, count: end - start } : null;
        }
        if (set.length) return { first: set[0], count: set.length };
        return null;
    }

    PhonosViewer.prototype._describeLoci = function (loci) {
        try {
            if (!loci || loci.kind !== 'element-loci' || !loci.elements || !loci.elements.length) return null;
            var first = loci.elements[0];
            var unit = first.unit;
            if (!unit || !unit.model) return null;
            var span = orderedSetSpan(first.indices);
            if (!span) return null;

            var ah = unit.model.atomicHierarchy;
            if (!ah || !ah.residueAtomSegments || !ah.chainAtomSegments) return null;

            var atomIdx = unit.elements[span.first];
            if (atomIdx === undefined) return null;
            var residueIdx = ah.residueAtomSegments.index[atomIdx];
            var chainIdx = ah.chainAtomSegments.index[atomIdx];

            var resnum = ah.residues.auth_seq_id ? ah.residues.auth_seq_id.value(residueIdx) : null;
            var chain = ah.chains.auth_asym_id ? ah.chains.auth_asym_id.value(chainIdx) : '';
            var resname = ah.atoms && ah.atoms.auth_comp_id
                ? ah.atoms.auth_comp_id.value(atomIdx)
                : (ah.residues.label_comp_id ? ah.residues.label_comp_id.value(residueIdx) : '');
            var atomName = ah.atoms && ah.atoms.auth_atom_id
                ? ah.atoms.auth_atom_id.value(atomIdx)
                : (ah.atoms && ah.atoms.label_atom_id ? ah.atoms.label_atom_id.value(atomIdx) : '');
            var element = ah.atoms && ah.atoms.type_symbol ? ah.atoms.type_symbol.value(atomIdx) : '';
            var conf = unit.model.atomicConformation;
            var bFactor = conf && conf.B_iso_or_equiv ? conf.B_iso_or_equiv.value(atomIdx) : null;
            var ax = conf && conf.x ? Number(conf.x.value(atomIdx)) : null;
            var ay = conf && conf.y ? Number(conf.y.value(atomIdx)) : null;
            var az = conf && conf.z ? Number(conf.z.value(atomIdx)) : null;

            return {
                chain: chain,
                resnum: (resnum === null || resnum === undefined) ? null : Number(resnum),
                resname: resname || '',
                atomName: atomName || '',
                element: element || '',
                atomCount: span.count,
                labelChain: ah.chains.label_asym_id ? ah.chains.label_asym_id.value(chainIdx) : '',
                labelSeq: ah.residues.label_seq_id ? ah.residues.label_seq_id.value(residueIdx) : null,
                bFactor: (bFactor === null || bFactor === undefined) ? null : Number(bFactor),
                x: ax,
                y: ay,
                z: az
            };
        } catch (e) {
            return null;
        }
    };

    /* Subscribe to scene clicks/hovers. `handlers` may define onPick(info) and
       onHover(info|null). Returns true when the Mol* build exposes the
       interaction behaviors this needs. Safe to call repeatedly - a second call
       replaces the previous handlers. */
    PhonosViewer.prototype.enablePicking = function (handlers) {
        this._pickHandlers = handlers || {};
        if (this._pickSubs) return true;

        var plugin = this.instance && this.instance.plugin;
        var interaction = plugin && plugin.behaviors && plugin.behaviors.interaction;
        if (!interaction) {
            console.warn('[PhonosViewer] picking: plugin.behaviors.interaction unavailable in this Mol* build.');
            return false;
        }

        var self = this;
        this._pickSubs = [];

        if (interaction.click && interaction.click.subscribe) {
            this._pickSubs.push(interaction.click.subscribe(function (event) {
                // The measurement tools own the click stream while a measurement
                // mode is armed; don't also fire a pick for those clicks.
                if (self._measurementMode) return;
                var info = self._describeLoci(event && event.current && event.current.loci);
                if (info && self._pickHandlers.onPick) self._pickHandlers.onPick(info);
            }));
        }

        if (interaction.hover && interaction.hover.subscribe) {
            this._pickSubs.push(interaction.hover.subscribe(function (event) {
                if (!self._pickHandlers.onHover) return;
                var info = self._describeLoci(event && event.current && event.current.loci);
                // A hover that resolves to nothing means the cursor left the
                // structure - report it so callers can clear their highlight.
                self._pickHandlers.onHover(info);
            }));
        }

        return this._pickSubs.length > 0;
    };

    PhonosViewer.prototype.disablePicking = function () {
        (this._pickSubs || []).forEach(function (sub) {
            try { sub.unsubscribe(); } catch (e) {}
        });
        this._pickSubs = null;
        this._pickHandlers = null;
    };

    PhonosViewer.prototype.on = function (event, cb) { this.container.addEventListener('phonos:' + event, cb); };
    PhonosViewer.prototype.off = function (event, cb) { this.container.removeEventListener('phonos:' + event, cb); };
    PhonosViewer.prototype.onLoaded = function (cb) { this._loadedCallbacks.push(cb); };

    /* ---- Panel ------------------------------------------------------- */

    PhonosViewer.prototype._buildPanel = function () {
        var panel = buildFocusPanel(this.options);
        this.container.style.position = this.container.style.position || 'relative';
        this.container.appendChild(panel);
        this._panel = panel;

        // If sequence panel is active, shift focus panel below it so clicks on
        // the sequence panel aren't intercepted by our z-index:40 overlay.
        var seqActive = this.options.sequencePanel !== undefined
            ? !!this.options.sequencePanel
            : true; // default: enabled when focusPanel is true
        if (seqActive) {
            panel.style.top = '58px';
        }

        this._wirePanelActions(panel);
        this._wirePanelInputs(panel);

        // Sync the Rendering buttons to the resolved preset (may come from
        // localStorage, not just the options default the panel was built with).
        var self = this;
        qsa(panel, '[data-quality]').forEach(function (b) {
            b.classList.toggle('active', b.dataset.quality === self._renderQuality);
        });
    };

    PhonosViewer.prototype._wirePanelActions = function (panel) {
        var self = this;
        panel.addEventListener('click', function (e) {
            var btn = e.target.closest('[data-action]');
            if (!btn) return;
            e.preventDefault();
            e.stopPropagation();
            var action = btn.dataset.action;
            switch (action) {
                case 'toggle-panel': panel.classList.toggle('collapsed'); break;
                case 'focus-region': self._panelFocusRegion(false); break;
                case 'isolate-region': self._panelFocusRegion(true); break;
                case 'clear-focus': self.clearFocus(); showToast(self, window.i18n ? window.i18n.t('phonosViewer.focusCleared') : 'Focus cleared'); break;
                case 'focus-high-conf': self.focusConfidenceRange(90, 101); break;
                case 'focus-low-conf': self.focusConfidenceRange(0, 50); break;
                case 'focus-medium-conf': self.focusConfidenceRange(50, 70); break;
                case 'focus-ligands': self.setVisibility({ polymer: true, het: true, water: false }); showToast(self, window.i18n ? window.i18n.t('phonosViewer.showingLigands') : 'Showing ligands'); break;
                case 'focus-termini': self.focusTermini(15); break;
                case 'show-all': self.clearFocus(); self.setVisibility({ polymer: true, het: true, water: false }); showToast(self, window.i18n ? window.i18n.t('phonosViewer.fullStructure') : 'Full structure'); break;
                case 'spin':
                    var sp = self.toggleSpin();
                    btn.classList.toggle('active', sp);
                    showToast(self, sp ? (window.i18n ? window.i18n.t('phonosViewer.spinOn') : 'Spin on') : (window.i18n ? window.i18n.t('phonosViewer.spinOff') : 'Spin off'));
                    break;
                case 'screenshot': self.screenshot(); break;
                case 'screenshot-transparent': self.screenshot({ transparent: true, scale: 2 }); break;
                case 'copy-screenshot': self.copyScreenshot(); break;
                case 'background': self.cycleBackground(); break;
                case 'fullscreen': self.toggleFullscreen(); break;
                case 'reset-view': self.resetView(); break;
                case 'measure-distance':
                    var distOn = self._measurementMode === 'distance' ? self._setMeasurementMode(null) : self._setMeasurementMode('distance');
                    btn.classList.toggle('active', distOn);
                    qsa(panel, '[data-action="measure-angle"]').forEach(function (b) { b.classList.remove('active'); });
                    showToast(self, distOn
                        ? (window.i18n ? window.i18n.t('phonosViewer.measureDistanceOn') : 'Click two atoms to measure distance')
                        : (window.i18n ? window.i18n.t('phonosViewer.measureOff') : 'Measurement mode off'));
                    break;
                case 'measure-angle':
                    var angOn = self._measurementMode === 'angle' ? self._setMeasurementMode(null) : self._setMeasurementMode('angle');
                    btn.classList.toggle('active', angOn);
                    qsa(panel, '[data-action="measure-distance"]').forEach(function (b) { b.classList.remove('active'); });
                    showToast(self, angOn
                        ? (window.i18n ? window.i18n.t('phonosViewer.measureAngleOn') : 'Click three atoms to measure angle')
                        : (window.i18n ? window.i18n.t('phonosViewer.measureOff') : 'Measurement mode off'));
                    break;
                case 'clear-measurements':
                    self.clearMeasurements();
                    qsa(panel, '[data-action="measure-distance"], [data-action="measure-angle"]').forEach(function (b) { b.classList.remove('active'); });
                    showToast(self, window.i18n ? window.i18n.t('phonosViewer.measurementsCleared') : 'Measurements cleared');
                    break;
                case 'export-structure': self.exportStructure(); break;
                case 'ask-copilot': self.askCopilotAboutStructure(); break;
                case 'shortcuts': self.toggleShortcuts(); break;
                case 'close-shortcuts': self.toggleShortcuts(false); break;
            }
        });
    };

    /* Show/hide the keyboard cheat sheet. Built on first use. */
    PhonosViewer.prototype.toggleShortcuts = function (visible) {
        if (!this._shortcutSheet) {
            this._shortcutSheet = buildShortcutSheet();
            this.container.style.position = this.container.style.position || 'relative';
            this.container.appendChild(this._shortcutSheet);
            var self = this;
            this._shortcutSheet.addEventListener('click', function (e) {
                if (e.target.closest('[data-action="close-shortcuts"]')) self.toggleShortcuts(false);
            });
        }
        var show = (visible === undefined) ? !this._shortcutSheet.classList.contains('visible') : !!visible;
        this._shortcutSheet.classList.toggle('visible', show);
        return show;
    };

    PhonosViewer.prototype._wirePanelInputs = function (panel) {
        var self = this;
        qsa(panel, '[data-vis]').forEach(function (cb) {
            cb.addEventListener('change', function () {
                var vis = {};
                qsa(panel, '[data-vis]').forEach(function (c) { vis[c.dataset.vis] = c.checked; });
                self.setVisibility(vis);
            });
        });
        qsa(panel, '[data-color]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                qsa(panel, '[data-color]').forEach(function (b) { b.classList.remove('active'); });
                btn.classList.add('active');
                var scheme = btn.dataset.color;
                switch (scheme) {
                    case 'confidence': self.applyConfidenceColoring(); break;
                    case 'chain': self.applyChainColoring(); break;
                    case 'default': self.applyDefaultColoring(); break;
                    case 'spectrum': self.applySpectrumColoring(); break;
                    case 'hydrophobicity': self.applyHydrophobicityColoring(); break;
                }
                self._renderRampLegend(scheme);
            });
        });
        qsa(panel, '[data-style]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                qsa(panel, '[data-style]').forEach(function (b) { b.classList.remove('active'); });
                btn.classList.add('active');
                self.setVisualStyle(btn.dataset.style);
                showToast(self, t('representation', 'Representation') + ': ' + btn.textContent);
            });
        });
        qsa(panel, '[data-panel]').forEach(function (cb) {
            cb.addEventListener('change', function () {
                if (cb.dataset.panel === 'sequence') self.toggleSequencePanel(cb.checked);
                else if (cb.dataset.panel === 'controls') self.toggleControls(cb.checked);
            });
        });
        qsa(panel, '[data-quality]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                qsa(panel, '[data-quality]').forEach(function (b) { b.classList.remove('active'); });
                btn.classList.add('active');
                self.setRenderQuality(btn.dataset.quality);
            });
        });
    };

    PhonosViewer.prototype._panelFocusRegion = function (isolate) {
        if (!this._panel) return;
        var chain = qs(this._panel, '#pfp-chain').value.trim() || 'A';
        var from = parseInt(qs(this._panel, '#pfp-from').value, 10);
        var to = parseInt(qs(this._panel, '#pfp-to').value, 10);
        if (isNaN(from) && isNaN(to)) {
            this.focusChain(chain, { isolate: isolate });
            showToast(this, (window.i18n ? window.i18n.t('phonosViewer.focusedOnChain') : 'Focused on chain ') + chain);
            return;
        }
        if (isNaN(from)) from = 1;
        if (isNaN(to)) to = 99999;
        this.focusRegion([{ chain: chain, start: from, end: to }], { isolate: isolate });
        showToast(this, (isolate ? (window.i18n ? window.i18n.t('phonosViewer.isolated') : 'Isolated') : (window.i18n ? window.i18n.t('phonosViewer.focused') : 'Focused')) + ' ' + chain + ':' + from + '-' + to);
    };

    /* ---- Keyboard ---------------------------------------------------- */

    PhonosViewer.prototype._setupKeyboard = function () {
        var self = this;
        this.container.setAttribute('tabindex', this.container.getAttribute('tabindex') || '0');

        // The shortcuts are bound to the container, so they silently did nothing
        // until the user happened to click the canvas first — the toolbar
        // tooltips promised keys that appeared broken. Claim focus on hover
        // instead, which is how every other 3D viewer behaves. Typing in a form
        // field always wins: never yank focus out from under an input.
        this.container.addEventListener('pointerenter', function () {
            if (self._destroyed) return;
            var active = document.activeElement;
            if (active && active !== document.body) {
                var tag = (active.tagName || '').toLowerCase();
                if (tag === 'input' || tag === 'textarea' || tag === 'select' || active.isContentEditable) return;
            }
            if (self.container.contains(document.activeElement)) return;
            try { self.container.focus({ preventScroll: true }); } catch (e) { self.container.focus(); }
        });

        this.container.addEventListener('keydown', function (e) {
            var tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
            var key = (e.key || '').toLowerCase();
            switch (key) {
                case 'r': e.preventDefault(); self.resetView(); break;
                case 's':
                    e.preventDefault();
                    var sp = self.toggleSpin();
                    if (self._panel) { var sb = qs(self._panel, '[data-action="spin"]'); if (sb) sb.classList.toggle('active', sp); }
                    showToast(self, sp ? (window.i18n ? window.i18n.t('phonosViewer.spinOn') : 'Spin on') : (window.i18n ? window.i18n.t('phonosViewer.spinOff') : 'Spin off'));
                    break;
                case 'p':
                    e.preventDefault();
                    self.screenshot({ transparent: e.shiftKey, scale: 2 });
                    break;
                case 'c': e.preventDefault(); self.copyScreenshot(); break;
                case 'b': e.preventDefault(); self.cycleBackground(); break;
                case 'q':
                    e.preventDefault();
                    var order = ['standard', 'illustrative', 'cinematic', 'flat'];
                    var idx = (order.indexOf(self._renderQuality) + 1) % order.length;
                    self.setRenderQuality(order[idx]);
                    if (self._panel) {
                        qsa(self._panel, '[data-quality]').forEach(function (b) {
                            b.classList.toggle('active', b.dataset.quality === self._renderQuality);
                        });
                    }
                    break;
                case 'f': e.preventDefault(); self.toggleFullscreen(); break;
                case 'm': e.preventDefault(); self.toggleDistanceRuler(); break;
                case 'd': e.preventDefault(); self.exportCoordinates(); break;
                case 'escape':
                    e.preventDefault();
                    if (self._shortcutSheet && self._shortcutSheet.classList.contains('visible')) {
                        self.toggleShortcuts(false);
                        break;
                    }
                    self.clearFocus();
                    showToast(self, window.i18n ? window.i18n.t('phonosViewer.focusCleared') : 'Focus cleared');
                    break;
                case '?': e.preventDefault(); self.toggleShortcuts(); break;
            }
        });
    };

    /* ---- Scientific HUD, Distance Ruler, Export & Copilot Bridge ---- */

    PhonosViewer.prototype._ensureScientificHud = function () {
        if (this._sciHud && this._sciHud.parentNode) return this._sciHud;
        if (!this.container) return null;
        this.container.style.position = this.container.style.position || 'relative';
        var hud = document.createElement('div');
        hud.className = 'phonos-sci-hud';
        hud.style.cssText = 'position:absolute;left:12px;bottom:12px;z-index:28;display:none;padding:6px 11px;border-radius:0;background:rgba(41,55,66,0.95);border:1px solid rgba(245,248,250,0.68);border-left:3px solid #34d399;color:#F5F8FA;font:500 11px/1.45 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:0.05em;pointer-events:none;box-shadow:4px 4px 0 rgba(0,0,0,0.55);';
        this.container.appendChild(hud);
        this._sciHud = hud;
        return hud;
    };

    PhonosViewer.prototype.toggleDistanceRuler = function (forceState) {
        var self = this;
        this._rulerActive = (forceState !== undefined) ? !!forceState : !this._rulerActive;
        this._rulerFirstAtom = null;
        var hud = this._ensureScientificHud();
        if (!this._rulerActive) {
            if (hud) hud.style.display = 'none';
            showToast(this, 'Distance ruler off');
            return false;
        }
        var plugin = this.instance && this.instance.plugin;
        var interaction = plugin && plugin.behaviors && plugin.behaviors.interaction;
        if (interaction && interaction.click && !this._rulerSub) {
            this._rulerSub = interaction.click.subscribe(function (ev) {
                if (!self._rulerActive) return;
                var info = self._describeLoci(ev && ev.current && ev.current.loci);
                if (!info || info.x === null) return;
                if (!self._rulerFirstAtom) {
                    self._rulerFirstAtom = info;
                    if (hud) {
                        hud.style.display = 'block';
                        hud.textContent = '📏 Atom 1: ' + (info.chain || 'A') + ':' + info.resname + info.resnum + '.' + info.atomName + ' — click second atom…';
                    }
                } else {
                    var a = self._rulerFirstAtom;
                    var dx = info.x - a.x, dy = info.y - a.y, dz = info.z - a.z;
                    var dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
                    var note = dist < 3.5 ? ' (H-bond / salt-bridge range)' : (dist < 5.0 ? ' (hydrophobic contact)' : '');
                    var label = '📏 ' + (a.chain || 'A') + ':' + a.resname + a.resnum + '.' + a.atomName +
                        ' ↔ ' + (info.chain || 'A') + ':' + info.resname + info.resnum + '.' + info.atomName +
                        ' = ' + dist.toFixed(2) + ' Å' + note;
                    self._lastMeasurement = label;
                    self._rulerFirstAtom = null;
                    if (hud) {
                        hud.style.display = 'block';
                        hud.textContent = label;
                    }
                    showToast(self, dist.toFixed(2) + ' Å');
                }
            });
        }
        if (interaction && interaction.hover && !this._hoverHudSub) {
            this._hoverHudSub = interaction.hover.subscribe(function (ev) {
                if (self._rulerFirstAtom) return;
                var info = self._describeLoci(ev && ev.current && ev.current.loci);
                if (!info || !hud) return;
                var bf = info.bFactor !== null ? (' · pLDDT/B ' + (info.bFactor <= 1.0 && info.bFactor > 0 ? (info.bFactor * 100).toFixed(1) : info.bFactor.toFixed(1))) : '';
                hud.style.display = 'block';
                hud.textContent = (info.chain || 'A') + ':' + info.resname + ' ' + info.resnum + ' · ' + info.atomName + ' (' + info.element + ')' + bf;
            });
        }
        if (hud) {
            hud.style.display = 'block';
            hud.textContent = '📏 Distance Ruler ON — click any two atoms to measure Ångström distance';
        }
        showToast(this, 'Distance ruler ON (M)');
        return true;
    };

    PhonosViewer.prototype.exportCoordinates = function () {
        var src = this._currentSource;
        if (!src) {
            showToast(this, 'No structure loaded to export');
            return;
        }
        if (src.rawData) {
            var fmt = (src.format || 'pdb').toLowerCase() === 'mmcif' ? 'cif' : (src.format || 'pdb');
            var blob = new Blob([src.rawData], { type: 'chemical/x-' + fmt });
            var url = URL.createObjectURL(blob);
            downloadDataUrl(url, 'phonos-structure-' + nowStamp() + '.' + fmt);
            setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
            showToast(this, 'Exported .' + fmt);
        } else if (src.url) {
            downloadDataUrl(src.url, 'phonos-structure-' + nowStamp() + '.' + (src.format || 'pdb'));
            showToast(this, 'Downloading coordinates…');
        }
    };

    PhonosViewer.prototype.askCopilotAboutStructure = function () {
        var prompt = 'Analyse the 3D molecular structure currently open in the viewer: run structure_confidence_plddt, summarise chains/ligands, highlight any low-confidence (<70 pLDDT) loops, and suggest next-step design or docking actions.';
        if (this._lastMeasurement) {
            prompt += ' Active atom measurement: ' + this._lastMeasurement + '.';
        }
        if (typeof window.PhonosCopilotAsk === 'function') {
            window.PhonosCopilotAsk(prompt);
        } else {
            window.open('/agent', '_blank');
        }
    };

    /* ---- Cleanup ----------------------------------------------------- */

    PhonosViewer.prototype.destroy = function () {
        this._destroyed = true;
        this.disablePicking();
        clearTimeout(this._loadingTimer);
        clearTimeout(this._toastTimer);
        for (var i = 0; i < this._blobUrls.length; i++) {
            try { URL.revokeObjectURL(this._blobUrls[i]); } catch (e) {}
        }
        this._blobUrls = [];
        if (this._panel && this._panel.parentNode) this._panel.remove();
        if (this._shortcutSheet && this._shortcutSheet.parentNode) this._shortcutSheet.remove();
        this._shortcutSheet = null;
        if (this._toast && this._toast.parentNode) this._toast.remove();
        if (this._overlay && this._overlay.parentNode) this._overlay.remove();
        if (this.instance) { try { this.instance.clear(); } catch (e) {} }
        this._initialized = false;
        this.confidenceData = null;
        this._loadedCallbacks = [];
        this._currentSource = null;
        this._appendedRaw = [];
    };

    /* ---- Static ------------------------------------------------------ */

    PhonosViewer.create = function (container, options) {
        var viewer = new PhonosViewer(container, options);
        viewer.init();
        return viewer;
    };

    PhonosViewer.VERSION = VERSION;
    PhonosViewer.PLDDT = PLDDT;
    PhonosViewer.CHAIN_COLORS = CHAIN_COLORS;
    PhonosViewer.parseConfidence = parseConfidence;
    PhonosViewer.plddtColor = plddtColor;
    PhonosViewer.plddtBand = plddtBand;

    global.PhonosViewer = PhonosViewer;

})(window);
