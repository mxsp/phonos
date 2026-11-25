/**
 * PhonosLigandDiagram - 2D protein-ligand interaction diagram.
 *
 * Renders the payload from POST /api/analysis/ligand-diagram as a Maestro-style
 * ligand interaction diagram: the ligand's real 2D chemical structure at the
 * centre, binding-site residues placed around it near the part of the ligand
 * they actually contact, and one styled connector per interaction type.
 *
 * Layout: each residue is anchored to the centroid of the ligand atoms it
 * touches, pushed radially outward past the ligand, then relaxed against its
 * neighbours so discs do not overlap. This is what makes the diagram readable -
 * a residue that hydrogen-bonds to the carboxylate sits by the carboxylate,
 * rather than at an arbitrary slot on a circle.
 *
 * The SVG is built with createElementNS rather than innerHTML so no payload
 * string (residue names, ligand names) is ever parsed as markup.
 */
(function (global) {
    'use strict';

    var SVG_NS = 'http://www.w3.org/2000/svg';

    // Interaction styling. `dash` is an SVG stroke-dasharray; `directional`
    // draws an arrowhead (donor -> acceptor); `ring` connects to a ring centroid
    // rather than to a single atom.
    var INTERACTION_STYLES = {
        hydrogen_bond: { color: '#34d399', dash: '6 4', width: 1.8, label: 'H-bond', directional: true },
        halogen_bond: { color: '#22d3ee', dash: '6 4', width: 1.8, label: 'Halogen bond', directional: true },
        hydrophobic: { color: '#fbbf24', dash: '1 5', width: 2.2, label: 'Hydrophobic' },
        salt_bridge: { color: '#f472b6', dash: '8 3 2 3', width: 2.0, label: 'Salt bridge' },
        pi_stacking: { color: '#818cf8', dash: '3 3', width: 2.0, label: 'π-stacking', ring: true },
        cation_pi: { color: '#f87171', dash: '3 3', width: 2.0, label: 'Cation-π', ring: true },
        metal_coordination: { color: '#c084fc', dash: '0', width: 2.2, label: 'Metal coordination' },
        water_bridge: { color: '#60a5fa', dash: '4 3', width: 1.6, label: 'Water bridge', viaWater: true },
        contact: { color: '#94a3b8', dash: '2 4', width: 1.4, label: 'Contact' }
    };

    // Residue disc colours by Maestro-style chemical category.
    var RESIDUE_CATEGORY_STYLES = {
        charged_positive: { fill: '#1e3a8a', stroke: '#60a5fa', label: 'Charged (positive)' },
        charged_negative: { fill: '#7f1d1d', stroke: '#f87171', label: 'Charged (negative)' },
        polar: { fill: '#155e63', stroke: '#22d3ee', label: 'Polar' },
        hydrophobic: { fill: '#14532d', stroke: '#4ade80', label: 'Hydrophobic' },
        glycine: { fill: '#3f3f46', stroke: '#a1a1aa', label: 'Glycine' },
        metal: { fill: '#4c1d95', stroke: '#c084fc', label: 'Metal' },
        water: { fill: '#1e3a5f', stroke: '#60a5fa', label: 'Water' },
        other: { fill: '#3f3f46', stroke: '#a1a1aa', label: 'Other' }
    };

    // CPK-ish element colours for atom labels in the 2D sketch.
    var ELEMENT_COLORS = {
        N: '#60a5fa', O: '#f87171', S: '#fbbf24', P: '#fb923c',
        F: '#4ade80', CL: '#4ade80', BR: '#c084fc', I: '#a78bfa',
        SE: '#fbbf24', B: '#fda4af'
    };
    var CARBON_COLOR = '#e5e7eb';

    var LIGAND_TARGET_RADIUS = 108;   // px, ligand drawing half-size
    var RESIDUE_RADIUS = 26;          // px, residue disc radius
    var RESIDUE_RING_GAP = 74;        // px beyond the ligand extent
    var RELAX_ITERATIONS = 220;

    function svgEl(tag, attrs) {
        var node = document.createElementNS(SVG_NS, tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                if (attrs[key] === null || attrs[key] === undefined) return;
                node.setAttribute(key, String(attrs[key]));
            });
        }
        return node;
    }

    function textNode(tag, attrs, content) {
        var node = svgEl(tag, attrs);
        node.textContent = content === null || content === undefined ? '' : String(content);
        return node;
    }

    function elementColor(element) {
        return ELEMENT_COLORS[String(element || '').toUpperCase()] || CARBON_COLOR;
    }

    function residueStyle(category) {
        return RESIDUE_CATEGORY_STYLES[category] || RESIDUE_CATEGORY_STYLES.other;
    }

    function interactionStyle(type) {
        return INTERACTION_STYLES[type] || INTERACTION_STYLES.contact;
    }

    function LigandDiagram(container, options) {
        this.container = typeof container === 'string'
            ? document.getElementById(container) : container;
        this.options = options || {};
        this.data = null;
        this.view = { scale: 1, x: 0, y: 0 };
        this.typeFilter = null;      // null = show all
        this.selectedResidue = null;
        this._layout = null;
        this._svg = null;
    }

    /** Replace the rendered payload. Pass null to clear. */
    LigandDiagram.prototype.setData = function (payload) {
        this.data = payload && payload.success ? payload : null;
        this.view = { scale: 1, x: 0, y: 0 };
        this.selectedResidue = null;
        this._externalHighlightKey = null;
        // Any layout the user arranged for this ligand is restored with it.
        this._loadOverrides();
        this.render();
        return this;
    };

    /** Restrict rendering to a set of interaction types (object of type->bool). */
    LigandDiagram.prototype.setTypeFilter = function (filter) {
        this.typeFilter = filter || null;
        this.render();
        return this;
    };

    /** Residue key in the form the payload uses, from chain + residue number. */
    LigandDiagram.prototype.findResidueKey = function (chain, resnum) {
        if (!this.data) return null;
        var match = (this.data.residues || []).filter(function (res) {
            return String(res.chain || '') === String(chain || '')
                && Number(res.resnum) === Number(resnum);
        })[0];
        return match ? match.key : null;
    };

    /**
     * Highlight a residue from outside the diagram (e.g. a pick in the 3D
     * scene). Purely visual and cheap - it toggles classes rather than
     * re-rendering, so it is safe to drive from a hover stream.
     */
    LigandDiagram.prototype.highlightResidueByPosition = function (chain, resnum) {
        var key = this.findResidueKey(chain, resnum);
        this._setExternalHighlight(key);
        return !!key;
    };

    LigandDiagram.prototype.clearExternalHighlight = function () {
        this._setExternalHighlight(null);
        return this;
    };

    LigandDiagram.prototype._setExternalHighlight = function (key) {
        if (!this._svg) return;
        if (this._externalHighlightKey === key) return;
        this._externalHighlightKey = key;

        var groups = this._svg.querySelectorAll('.lid-residue');
        Array.prototype.forEach.call(groups, function (group) {
            group.classList.toggle('is-external-highlight',
                !!key && group.getAttribute('data-residue-key') === key);
        });
        var lines = this._svg.querySelectorAll('.lid-interaction');
        Array.prototype.forEach.call(lines, function (line) {
            line.classList.toggle('is-highlighted',
                !!key && line.getAttribute('data-residue-key') === key);
        });
    };

    /** Select a residue as if its disc had been clicked. */
    LigandDiagram.prototype.selectResidueByPosition = function (chain, resnum) {
        var key = this.findResidueKey(chain, resnum);
        if (!key) return false;
        this.selectedResidue = key;
        this.render();
        return true;
    };

    LigandDiagram.prototype._typeEnabled = function (type) {
        if (!this.typeFilter) return true;
        return this.typeFilter[type] !== false;
    };

    LigandDiagram.prototype._showMessage = function (message) {
        if (!this.container) return;
        this.container.textContent = '';
        var note = document.createElement('div');
        note.className = 'lid-empty';
        note.textContent = message;
        this.container.appendChild(note);
    };

    /**
     * Project the ligand's RDKit 2D coordinates into diagram pixel space, then
     * place and relax the residue discs around it.
     */
    LigandDiagram.prototype._computeLayout = function () {
        var data = this.data;
        var ligand = data.ligand || {};
        var atoms = ligand.atoms || [];
        var heavy = atoms.filter(function (a) { return !a.is_hydrogen; });
        if (!heavy.length) return null;

        var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        heavy.forEach(function (a) {
            minX = Math.min(minX, a.x); maxX = Math.max(maxX, a.x);
            minY = Math.min(minY, a.y); maxY = Math.max(maxY, a.y);
        });
        var spanX = Math.max(maxX - minX, 0.001);
        var spanY = Math.max(maxY - minY, 0.001);
        // Uniform scale so the depiction keeps its aspect ratio (a squashed
        // benzene ring reads as a chemistry error, not a layout choice).
        var scale = (LIGAND_TARGET_RADIUS * 2) / Math.max(spanX, spanY);
        var midX = (minX + maxX) / 2;
        var midY = (minY + maxY) / 2;

        // SVG y grows downward, chemistry y grows upward - flip so the depiction
        // is not drawn mirrored.
        var positions = {};
        atoms.forEach(function (a) {
            positions[a.idx] = {
                x: (a.x - midX) * scale,
                y: -(a.y - midY) * scale
            };
        });

        var ligandRadius = 0;
        heavy.forEach(function (a) {
            var p = positions[a.idx];
            ligandRadius = Math.max(ligandRadius, Math.hypot(p.x, p.y));
        });
        ligandRadius = Math.max(ligandRadius, 40);

        // --- residue placement ---
        var self = this;
        var residues = (data.residues || []).filter(function (res) {
            return (res.types || []).some(function (type) { return self._typeEnabled(type); });
        });

        // Size the ring so every disc fits around it. Without this, a busy
        // binding site packs more discs than the circumference can hold and the
        // relaxation can only trade one overlap for another.
        var slotArc = RESIDUE_RADIUS * 2 + 12;
        var requiredRadius = residues.length
            ? (residues.length * slotArc) / (2 * Math.PI) : 0;
        var ringRadius = Math.max(ligandRadius + RESIDUE_RING_GAP, requiredRadius);

        var nodes = residues.map(function (res, index) {
            // Anchor on the ligand atoms this residue actually touches.
            var anchors = (res.ligand_atoms || [])
                .map(function (idx) { return positions[idx]; })
                .filter(Boolean);
            var ax = 0, ay = 0;
            if (anchors.length) {
                anchors.forEach(function (p) { ax += p.x; ay += p.y; });
                ax /= anchors.length;
                ay /= anchors.length;
            }
            var dist = Math.hypot(ax, ay);
            var angle;
            if (dist < 1e-3) {
                // Residue with no usable anchor: spread evenly so it still lands
                // somewhere sensible instead of stacking at the origin.
                angle = (index / Math.max(residues.length, 1)) * Math.PI * 2;
            } else {
                angle = Math.atan2(ay, ax);
            }
            return {
                residue: res,
                anchor: { x: ax, y: ay },
                angle: angle,
                x: Math.cos(angle) * ringRadius,
                y: Math.sin(angle) * ringRadius
            };
        });

        this._spreadAngles(nodes, ringRadius);
        nodes.forEach(function (node) {
            node.x = Math.cos(node.angle) * ringRadius;
            node.y = Math.sin(node.angle) * ringRadius;
        });
        this._relaxNodes(nodes, ligandRadius);

        // A user-dragged disc wins over the computed layout, and is applied
        // after relaxation so the solver never pushes it back.
        var overrides = this._positionOverrides || {};
        nodes.forEach(function (node) {
            var saved = overrides[node.residue.key];
            if (saved) {
                node.x = saved.x;
                node.y = saved.y;
                node.pinned = true;
            }
        });

        var bounds = { minX: -ligandRadius, maxX: ligandRadius, minY: -ligandRadius, maxY: ligandRadius };
        nodes.forEach(function (n) {
            bounds.minX = Math.min(bounds.minX, n.x - RESIDUE_RADIUS);
            bounds.maxX = Math.max(bounds.maxX, n.x + RESIDUE_RADIUS);
            bounds.minY = Math.min(bounds.minY, n.y - RESIDUE_RADIUS);
            bounds.maxY = Math.max(bounds.maxY, n.y + RESIDUE_RADIUS);
        });

        return {
            positions: positions,
            atoms: atoms,
            heavy: heavy,
            nodes: nodes,
            ligandRadius: ligandRadius,
            bounds: bounds
        };
    };

    /**
     * Enforce a minimum angular gap between discs before the free relaxation.
     *
     * Residues that contact the same part of the ligand get the same preferred
     * angle - a pi-stacking and a cation-pi residue both anchor to the ring
     * centroid, so both start at exactly the same point and pairwise repulsion
     * alone separates them only slowly and arbitrarily. Sorting by angle and
     * pushing neighbours apart in angular order resolves ties deterministically
     * and keeps every disc on the side of the ligand it belongs to.
     */
    LigandDiagram.prototype._spreadAngles = function (nodes, ringRadius) {
        if (nodes.length < 2) return;

        var minGap = Math.min((RESIDUE_RADIUS * 2 + 10) / ringRadius,
                              (2 * Math.PI) / nodes.length);
        var TWO_PI = Math.PI * 2;

        var norm = function (a) {
            var v = a % TWO_PI;
            return v < 0 ? v + TWO_PI : v;
        };
        nodes.forEach(function (node) { node.angle = norm(node.angle); });
        nodes.sort(function (a, b) { return a.angle - b.angle; });

        for (var pass = 0; pass < 60; pass++) {
            var moved = false;
            for (var i = 0; i < nodes.length; i++) {
                var current = nodes[i];
                var next = nodes[(i + 1) % nodes.length];
                var gap = next.angle - current.angle;
                if (i === nodes.length - 1) gap += TWO_PI;   // wrap-around pair
                if (gap >= minGap) continue;
                var push = (minGap - gap) / 2;
                current.angle = norm(current.angle - push);
                next.angle = norm(next.angle + push);
                moved = true;
            }
            if (!moved) break;
            nodes.sort(function (a, b) { return a.angle - b.angle; });
        }
    };

    /**
     * Push residue discs apart and away from the ligand.
     *
     * Discs start on a ring at their anchor angle, which clusters them wherever
     * the ligand is busiest; without relaxation those clusters overlap into an
     * unreadable pile. Each pass applies pairwise repulsion, a minimum clearance
     * from the ligand, and a weak pull back toward the anchor direction so a
     * disc never drifts to the wrong side of the molecule.
     */
    LigandDiagram.prototype._relaxNodes = function (nodes, ligandRadius) {
        if (nodes.length < 2) {
            if (nodes.length === 1) {
                var only = nodes[0];
                var minR = ligandRadius + RESIDUE_RADIUS + 24;
                var r = Math.hypot(only.x, only.y);
                if (r < minR) {
                    var s = minR / (r || 1);
                    only.x *= s; only.y *= s;
                }
            }
            return;
        }

        var minSeparation = RESIDUE_RADIUS * 2 + 8;
        var minRadius = ligandRadius + RESIDUE_RADIUS + 22;

        for (var iter = 0; iter < RELAX_ITERATIONS; iter++) {
            var cooling = 1 - (iter / RELAX_ITERATIONS);

            for (var i = 0; i < nodes.length; i++) {
                for (var j = i + 1; j < nodes.length; j++) {
                    var a = nodes[i], b = nodes[j];
                    var dx = b.x - a.x, dy = b.y - a.y;
                    var d = Math.hypot(dx, dy);
                    if (d >= minSeparation) continue;
                    if (d < 1e-6) {
                        // Exactly coincident: nudge along a deterministic axis so
                        // the pass is reproducible rather than randomised.
                        dx = Math.cos(i); dy = Math.sin(i); d = 1;
                    }
                    var push = ((minSeparation - d) / 2) * 0.55;
                    var ux = dx / d, uy = dy / d;
                    a.x -= ux * push; a.y -= uy * push;
                    b.x += ux * push; b.y += uy * push;
                }
            }

            for (var k = 0; k < nodes.length; k++) {
                var n = nodes[k];

                // Keep clear of the ligand drawing.
                var r = Math.hypot(n.x, n.y);
                if (r < minRadius) {
                    var scale = minRadius / (r || 1);
                    n.x *= scale; n.y *= scale;
                }

                // Weak restoring pull toward the anchor direction.
                var targetAngle = n.angle;
                var currentAngle = Math.atan2(n.y, n.x);
                var delta = Math.atan2(Math.sin(targetAngle - currentAngle),
                                       Math.cos(targetAngle - currentAngle));
                var corrected = currentAngle + delta * 0.06 * cooling;
                var radius = Math.hypot(n.x, n.y);
                n.x = Math.cos(corrected) * radius;
                n.y = Math.sin(corrected) * radius;
            }
        }
    };

    LigandDiagram.prototype.render = function () {
        if (!this.container) return this;
        if (!this.data) {
            this._showMessage(this.options.emptyMessage
                || 'Load a protein–ligand complex to map interactions.');
            return this;
        }

        var layout = this._layout = this._computeLayout();
        if (!layout) {
            this._showMessage('The ligand has no drawable heavy atoms.');
            return this;
        }

        var pad = 26;
        var width = (layout.bounds.maxX - layout.bounds.minX) + pad * 2;
        var height = (layout.bounds.maxY - layout.bounds.minY) + pad * 2;
        var originX = layout.bounds.minX - pad;
        var originY = layout.bounds.minY - pad;

        this.container.textContent = '';

        var svg = svgEl('svg', {
            class: 'lid-svg',
            viewBox: originX + ' ' + originY + ' ' + width + ' ' + height,
            preserveAspectRatio: 'xMidYMid meet',
            role: 'img',
            'aria-label': 'Protein–ligand interaction diagram'
        });
        this._svg = svg;
        this._viewBoxBase = { x: originX, y: originY, w: width, h: height };

        svg.appendChild(this._buildDefs());

        var scene = svgEl('g', { class: 'lid-scene' });
        this._scene = scene;
        svg.appendChild(scene);

        // Draw order matters: connectors sit under the ligand and residue discs
        // so a line never crosses over a label, but the distance labels go on
        // top of everything or a disc can bury them.
        var labelLayer = svgEl('g', { class: 'lid-label-layer' });
        scene.appendChild(this._buildInteractionLayer(layout, labelLayer));
        scene.appendChild(this._buildSolventLayer(layout));
        scene.appendChild(this._buildLigandLayer(layout));
        scene.appendChild(this._buildResidueLayer(layout));
        scene.appendChild(labelLayer);

        this.container.appendChild(svg);
        this._attachInteractionHandlers();
        this._applyView();
        return this;
    };

    LigandDiagram.prototype._buildDefs = function () {
        var defs = svgEl('defs');
        Object.keys(INTERACTION_STYLES).forEach(function (type) {
            var style = INTERACTION_STYLES[type];
            if (!style.directional) return;
            var marker = svgEl('marker', {
                id: 'lid-arrow-' + type,
                viewBox: '0 0 10 10',
                refX: 9, refY: 5,
                markerWidth: 5, markerHeight: 5,
                orient: 'auto-start-reverse'
            });
            marker.appendChild(svgEl('path', { d: 'M 0 1 L 9 5 L 0 9 z', fill: style.color }));
            defs.appendChild(marker);
        });

        // Radial fade used for the solvent-exposure halo.
        var gradient = svgEl('radialGradient', { id: 'lid-solvent-halo' });
        gradient.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#38bdf8', 'stop-opacity': '0.34' }));
        gradient.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#38bdf8', 'stop-opacity': '0' }));
        defs.appendChild(gradient);
        return defs;
    };

    /** Grey-blue halos over solvent-exposed ligand atoms. */
    LigandDiagram.prototype._buildSolventLayer = function (layout) {
        var group = svgEl('g', { class: 'lid-solvent-layer' });
        var exposure = (this.data.ligand || {}).solvent_exposure || [];
        if (!exposure.length || this.options.showSolventExposure === false) return group;

        layout.heavy.forEach(function (atom) {
            var info = exposure[atom.idx];
            if (!info) return;
            var exposed = 1 - (info.buried_fraction || 0);
            if (exposed < 0.25) return;   // essentially buried, no halo
            var p = layout.positions[atom.idx];
            group.appendChild(svgEl('circle', {
                cx: p.x.toFixed(2), cy: p.y.toFixed(2),
                r: (10 + exposed * 14).toFixed(2),
                fill: 'url(#lid-solvent-halo)',
                class: 'lid-solvent-halo'
            }));
        });
        return group;
    };

    /** The ligand's 2D chemical structure. */
    LigandDiagram.prototype._buildLigandLayer = function (layout) {
        var group = svgEl('g', { class: 'lid-ligand-layer' });
        var positions = layout.positions;
        var atoms = layout.atoms;
        var bonds = (this.data.ligand || {}).bonds || [];

        var byIdx = {};
        atoms.forEach(function (a) { byIdx[a.idx] = a; });

        var bondGroup = svgEl('g', { class: 'lid-bonds' });
        bonds.forEach(function (bond) {
            var a = byIdx[bond.begin], b = byIdx[bond.end];
            if (!a || !b || a.is_hydrogen || b.is_hydrogen) return;
            var p1 = positions[bond.begin], p2 = positions[bond.end];
            if (!p1 || !p2) return;

            var dx = p2.x - p1.x, dy = p2.y - p1.y;
            var len = Math.hypot(dx, dy) || 1;
            // Shorten each end so bonds stop at the atom label, not under it.
            var trimA = a.element === 'C' ? 0 : 9;
            var trimB = b.element === 'C' ? 0 : 9;
            var ux = dx / len, uy = dy / len;
            var x1 = p1.x + ux * trimA, y1 = p1.y + uy * trimA;
            var x2 = p2.x - ux * trimB, y2 = p2.y - uy * trimB;

            var order = bond.aromatic ? 2 : (bond.order || 1);
            if (order >= 2) {
                // Offset pair for double/triple bonds, perpendicular to the bond.
                var offset = 2.6;
                var px = -uy * offset, py = ux * offset;
                bondGroup.appendChild(svgEl('line', {
                    x1: (x1 + px).toFixed(2), y1: (y1 + py).toFixed(2),
                    x2: (x2 + px).toFixed(2), y2: (y2 + py).toFixed(2),
                    class: 'lid-bond'
                }));
                bondGroup.appendChild(svgEl('line', {
                    x1: (x1 - px).toFixed(2), y1: (y1 - py).toFixed(2),
                    x2: (x2 - px).toFixed(2), y2: (y2 - py).toFixed(2),
                    class: 'lid-bond' + (bond.aromatic ? ' lid-bond-aromatic' : '')
                }));
                if (order >= 3) {
                    bondGroup.appendChild(svgEl('line', {
                        x1: x1.toFixed(2), y1: y1.toFixed(2),
                        x2: x2.toFixed(2), y2: y2.toFixed(2),
                        class: 'lid-bond'
                    }));
                }
            } else {
                bondGroup.appendChild(svgEl('line', {
                    x1: x1.toFixed(2), y1: y1.toFixed(2),
                    x2: x2.toFixed(2), y2: y2.toFixed(2),
                    class: 'lid-bond'
                }));
            }
        });
        group.appendChild(bondGroup);

        // Atom labels: heteroatoms and charged atoms only, as in a normal sketch
        // (carbons are implied by the vertices).
        var labelGroup = svgEl('g', { class: 'lid-atoms' });
        layout.heavy.forEach(function (atom) {
            var p = positions[atom.idx];
            var isCarbon = String(atom.element).toUpperCase() === 'C';
            if (isCarbon && !atom.charge) return;

            var label = atom.element;
            if (atom.h_count === 1) label += 'H';
            else if (atom.h_count > 1) label += 'H' + atom.h_count;

            labelGroup.appendChild(svgEl('circle', {
                cx: p.x.toFixed(2), cy: p.y.toFixed(2), r: 9.5,
                class: 'lid-atom-bg'
            }));
            labelGroup.appendChild(textNode('text', {
                x: p.x.toFixed(2), y: (p.y + 3.6).toFixed(2),
                class: 'lid-atom-label',
                fill: elementColor(atom.element),
                'data-atom': atom.idx
            }, label));

            if (atom.charge) {
                labelGroup.appendChild(textNode('text', {
                    x: (p.x + 9).toFixed(2), y: (p.y - 6).toFixed(2),
                    class: 'lid-atom-charge'
                }, atom.charge > 0 ? (atom.charge > 1 ? atom.charge + '+' : '+')
                                   : (atom.charge < -1 ? Math.abs(atom.charge) + '−' : '−')));
            }
        });
        group.appendChild(labelGroup);
        return group;
    };

    /** One disc per binding-site residue. */
    LigandDiagram.prototype._buildResidueLayer = function (layout) {
        var group = svgEl('g', { class: 'lid-residue-layer' });
        var self = this;

        layout.nodes.forEach(function (node) {
            var res = node.residue;
            var style = residueStyle(res.category);
            var isSelected = self.selectedResidue === res.key;

            var g = svgEl('g', {
                class: 'lid-residue' + (isSelected ? ' is-selected' : '')
                    + (node.pinned ? ' is-pinned' : ''),
                'data-residue-key': res.key,
                'data-chain': res.chain,
                'data-resi': res.resnum,
                tabindex: '0',
                role: 'button',
                'aria-label': res.resname + ' ' + res.chain + res.resnum
                    + ', ' + (res.types || []).join(', ')
            });

            g.appendChild(svgEl('circle', {
                cx: node.x.toFixed(2), cy: node.y.toFixed(2), r: RESIDUE_RADIUS,
                fill: style.fill, stroke: style.stroke, 'stroke-width': isSelected ? 3 : 1.8,
                class: 'lid-residue-disc'
            }));
            g.appendChild(textNode('text', {
                x: node.x.toFixed(2), y: (node.y - 2).toFixed(2),
                class: 'lid-residue-name'
            }, res.resname));
            g.appendChild(textNode('text', {
                x: node.x.toFixed(2), y: (node.y + 10).toFixed(2),
                class: 'lid-residue-num'
            }, String(res.chain || '') + res.resnum));

            var title = svgEl('title');
            title.textContent = res.resname + ' ' + (res.chain || '') + res.resnum
                + ' · ' + (res.types || []).map(function (t) { return interactionStyle(t).label; }).join(', ')
                + (res.min_distance != null ? ' · closest ' + res.min_distance + ' Å' : '');
            g.appendChild(title);

            group.appendChild(g);
        });
        return group;
    };

    /**
     * Collapse the raw interaction list to one connector per residue+type.
     *
     * A single hydrophobic residue routinely makes six or more atom-level
     * contacts with a ring; drawing each one produces a fan of identical lines
     * and a pile of overlapping "3.8 Å" labels. Maestro draws one mark per
     * residue per interaction type, so contacts are merged here: the connector
     * reports the closest approach and points at the centroid of the ligand
     * atoms involved.
     */
    LigandDiagram.prototype._aggregateInteractions = function (positions) {
        var self = this;
        var merged = {};
        var order = [];

        (this.data.interactions || []).forEach(function (item) {
            if (!self._typeEnabled(item.type)) return;
            var protein = item.protein || {};
            var residueKey = (protein.chain || '') + ':'
                + (protein.resnum != null ? protein.resnum : '') + ':'
                + (protein.resname || '').toUpperCase();
            var key = residueKey + '|' + item.type;

            var atomIds = (item.ligand_atom_indices && item.ligand_atom_indices.length)
                ? item.ligand_atom_indices
                : (item.ligand_atom_index != null ? [item.ligand_atom_index] : []);

            var entry = merged[key];
            if (!entry) {
                entry = merged[key] = {
                    residueKey: residueKey, type: item.type, protein: protein,
                    distance: item.distance, angle: item.angle, geometry: item.geometry,
                    water: item.water, ligandWaterDistance: item.ligand_water_distance,
                    count: 0, atomIds: []
                };
                order.push(key);
            }
            entry.count++;
            if (item.distance != null && (entry.distance == null || item.distance < entry.distance)) {
                entry.distance = item.distance;
                entry.angle = item.angle;
                entry.geometry = item.geometry;
                entry.ligandWaterDistance = item.ligand_water_distance;
            }
            atomIds.forEach(function (idx) {
                if (positions[idx] && entry.atomIds.indexOf(idx) === -1) entry.atomIds.push(idx);
            });
        });

        return order.map(function (key) { return merged[key]; });
    };

    /** Connectors between residue discs and ligand atoms. */
    LigandDiagram.prototype._buildInteractionLayer = function (layout, labelGroup) {
        var group = svgEl('g', { class: 'lid-interaction-layer' });
        var self = this;
        var positions = layout.positions;

        var nodeByKey = {};
        layout.nodes.forEach(function (n) { nodeByKey[n.residue.key] = n; });

        var aggregated = this._aggregateInteractions(positions);

        // A residue making two kinds of interaction with the same part of the
        // ligand (pi-stacking and hydrophobic with one ring, say) would draw two
        // exactly collinear connectors, hiding one. Count them per residue so
        // each can be fanned to its own offset.
        var perResidue = {};
        aggregated.forEach(function (item) {
            perResidue[item.residueKey] = (perResidue[item.residueKey] || 0) + 1;
        });
        var drawnPerResidue = {};

        aggregated.forEach(function (item) {
            var key = item.residueKey;
            var protein = item.protein || {};
            var node = nodeByKey[key];
            if (!node) return;

            var style = interactionStyle(item.type);
            var total = perResidue[key];
            var ordinal = (drawnPerResidue[key] = (drawnPerResidue[key] || 0) + 1) - 1;
            // Symmetric fan: 1 connector -> 0, 2 -> -1/+1, 3 -> -2/0/+2 ...
            var fanOffset = total > 1 ? (ordinal - (total - 1) / 2) * 11 : 0;

            // Ring-level interactions attach to the ring centroid; everything
            // else to the centroid of the contacted ligand atoms.
            var target = null;
            if (item.atomIds.length) {
                var sx = 0, sy = 0, n = 0;
                item.atomIds.forEach(function (idx) {
                    var p = positions[idx];
                    if (p) { sx += p.x; sy += p.y; n++; }
                });
                if (n) target = { x: sx / n, y: sy / n };
            }
            if (!target) target = { x: 0, y: 0 };

            // Stop the connector at the disc edge rather than at its centre, and
            // shift it sideways by its fan offset so sibling connectors between
            // the same pair stay individually visible.
            var dx = target.x - node.x, dy = target.y - node.y;
            var len = Math.hypot(dx, dy) || 1;
            var ux = dx / len, uy = dy / len;
            var px = -uy * fanOffset, py = ux * fanOffset;
            var startX = node.x + ux * RESIDUE_RADIUS + px;
            var startY = node.y + uy * RESIDUE_RADIUS + py;
            var endX = target.x - ux * 11 + px;
            var endY = target.y - uy * 11 + py;

            // A water bridge is two hydrogen bonds through one water, so the
            // connector is drawn as a dog-leg that visibly passes through the
            // water node rather than as a straight line with a bead on it.
            var waterPoint = null;
            if (style.viaWater && item.water) {
                waterPoint = {
                    x: (startX + endX) / 2 - uy * 18,
                    y: (startY + endY) / 2 + ux * 18
                };
            }

            var line;
            if (waterPoint) {
                line = svgEl('polyline', {
                    points: startX.toFixed(2) + ',' + startY.toFixed(2) + ' '
                        + waterPoint.x.toFixed(2) + ',' + waterPoint.y.toFixed(2) + ' '
                        + endX.toFixed(2) + ',' + endY.toFixed(2),
                    fill: 'none'
                });
            } else {
                line = svgEl('line', {
                    x1: startX.toFixed(2), y1: startY.toFixed(2),
                    x2: endX.toFixed(2), y2: endY.toFixed(2)
                });
            }
            [['stroke', style.color], ['stroke-width', style.width],
             ['stroke-dasharray', style.dash], ['stroke-linecap', 'round'],
             ['class', 'lid-interaction lid-interaction-' + item.type],
             ['data-residue-key', key], ['data-type', item.type]
            ].forEach(function (pair) { line.setAttribute(pair[0], String(pair[1])); });
            if (style.directional) {
                // Protein-donor H-bonds point at the ligand; ligand-donor ones
                // point back at the residue.
                line.setAttribute('marker-end', 'url(#lid-arrow-' + item.type + ')');
            }

            var title = svgEl('title');
            title.textContent = style.label + ' · ' + (protein.resname || '')
                + (protein.chain || '') + (protein.resnum != null ? protein.resnum : '')
                + (item.distance != null ? ' · closest ' + item.distance + ' Å' : '')
                + (item.angle != null ? ' · ' + item.angle + '°' : '')
                + (item.geometry ? ' · ' + item.geometry : '')
                + (item.count > 1 ? ' · ' + item.count + ' contacts' : '');
            line.appendChild(title);
            group.appendChild(line);

            if (waterPoint) {
                group.appendChild(svgEl('circle', {
                    cx: waterPoint.x.toFixed(2), cy: waterPoint.y.toFixed(2), r: 8,
                    class: 'lid-water-node'
                }));
                group.appendChild(textNode('text', {
                    x: waterPoint.x.toFixed(2), y: (waterPoint.y + 3).toFixed(2),
                    class: 'lid-water-label'
                }, 'W'));
            }

            if (self.options.showDistances !== false && labelGroup) {
                // A water bridge's `distance` is the summed two-leg path, which
                // is not a distance anyone reads off a diagram; label the leg
                // that touches the ligand instead.
                var labelled = style.viaWater ? item.ligandWaterDistance : item.distance;
                if (labelled != null) {
                    // For a dog-legged water bridge, label the ligand-side leg.
                    var fromX = waterPoint ? waterPoint.x : startX;
                    var fromY = waterPoint ? waterPoint.y : startY;
                    var lx = fromX + (endX - fromX) * 0.58;
                    var ly = fromY + (endY - fromY) * 0.58;
                    labelGroup.appendChild(textNode('text', {
                        x: lx.toFixed(2), y: (ly - 3).toFixed(2),
                        class: 'lid-distance-label',
                        fill: style.color
                    }, Number(labelled).toFixed(1) + ' Å'));
                }
            }
        });
        return group;
    };

    LigandDiagram.prototype._attachInteractionHandlers = function () {
        var self = this;
        if (!this._svg) return;

        this._svg.addEventListener('click', function (event) {
            var target = event.target.closest ? event.target.closest('.lid-residue') : null;
            if (!target) return;
            var key = target.getAttribute('data-residue-key');
            self.selectedResidue = self.selectedResidue === key ? null : key;
            self.render();
            if (typeof self.options.onResidueClick === 'function') {
                self.options.onResidueClick({
                    key: key,
                    chain: target.getAttribute('data-chain'),
                    resi: parseInt(target.getAttribute('data-resi'), 10)
                });
            }
        });

        this._svg.addEventListener('keydown', function (event) {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            var target = event.target.closest ? event.target.closest('.lid-residue') : null;
            if (!target) return;
            event.preventDefault();
            target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });

        this._svg.addEventListener('mouseover', function (event) {
            var target = event.target.closest ? event.target.closest('.lid-residue') : null;
            if (!target) return;
            self._highlightResidue(target.getAttribute('data-residue-key'), true);
            if (typeof self.options.onResidueHover === 'function') {
                self.options.onResidueHover({
                    key: target.getAttribute('data-residue-key'),
                    chain: target.getAttribute('data-chain'),
                    resi: parseInt(target.getAttribute('data-resi'), 10)
                });
            }
        });

        this._svg.addEventListener('mouseout', function (event) {
            var target = event.target.closest ? event.target.closest('.lid-residue') : null;
            if (!target) return;
            self._highlightResidue(target.getAttribute('data-residue-key'), false);
            if (typeof self.options.onResidueHoverEnd === 'function') {
                self.options.onResidueHoverEnd();
            }
        });

        this._attachResidueDragHandlers();

        // Wheel zoom and drag pan over the diagram.
        this._svg.addEventListener('wheel', function (event) {
            event.preventDefault();
            var factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
            self.view.scale = Math.max(0.4, Math.min(6, self.view.scale * factor));
            self._applyView();
        }, { passive: false });

        var dragging = false, lastX = 0, lastY = 0;
        this._svg.addEventListener('pointerdown', function (event) {
            if (event.target.closest && event.target.closest('.lid-residue')) return;
            dragging = true;
            lastX = event.clientX; lastY = event.clientY;
            self._svg.classList.add('is-panning');
            self._svg.setPointerCapture(event.pointerId);
        });
        this._svg.addEventListener('pointermove', function (event) {
            if (!dragging) return;
            var rect = self._svg.getBoundingClientRect();
            // Convert pixel drag into viewBox units so panning tracks the cursor
            // at any zoom level.
            var unitsPerPx = self._viewBoxBase.w / (rect.width || 1);
            self.view.x += (event.clientX - lastX) * unitsPerPx / self.view.scale;
            self.view.y += (event.clientY - lastY) * unitsPerPx / self.view.scale;
            lastX = event.clientX; lastY = event.clientY;
            self._applyView();
        });
        var endDrag = function (event) {
            if (!dragging) return;
            dragging = false;
            self._svg.classList.remove('is-panning');
            try { self._svg.releasePointerCapture(event.pointerId); } catch (e) {}
        };
        this._svg.addEventListener('pointerup', endDrag);
        this._svg.addEventListener('pointercancel', endDrag);
    };

    /**
     * Let a user drag residue discs to rearrange a crowded diagram.
     *
     * Dragged positions are kept per residue key and re-applied on every
     * subsequent render (see _computeLayout), so switching interaction filters
     * or refreshing the analysis does not undo the user's arrangement. A drag
     * is distinguished from a click by distance, so dragging a disc does not
     * also fire the click-to-focus handler.
     */
    LigandDiagram.prototype._attachResidueDragHandlers = function () {
        var self = this;
        var active = null;

        var toDiagramUnits = function (event) {
            var rect = self._svg.getBoundingClientRect();
            var unitsPerPx = self._viewBoxBase.w / (rect.width || 1);
            return unitsPerPx / self.view.scale;
        };

        this._svg.addEventListener('pointerdown', function (event) {
            var group = event.target.closest ? event.target.closest('.lid-residue') : null;
            if (!group) return;
            var key = group.getAttribute('data-residue-key');
            var node = (self._layout && self._layout.nodes || []).filter(function (candidate) {
                return candidate.residue.key === key;
            })[0];
            if (!node) return;

            event.stopPropagation();   // don't also start a canvas pan
            active = {
                key: key, group: group, node: node,
                startX: event.clientX, startY: event.clientY,
                originX: node.x, originY: node.y,
                moved: false, captured: false
            };
            // Pointer capture is deliberately NOT taken here: capturing on
            // pointerdown retargets the following `click` to the <svg>, so
            // `closest('.lid-residue')` would miss and a simple click would
            // stop selecting a residue. It is taken on first movement instead.
        });

        this._svg.addEventListener('pointermove', function (event) {
            if (!active) return;
            var scale = toDiagramUnits(event);
            var dx = (event.clientX - active.startX) * scale;
            var dy = (event.clientY - active.startY) * scale;
            if (!active.moved && Math.hypot(dx, dy) < 3) return;   // still a click
            if (!active.moved) {
                active.moved = true;
                // Now that this is unambiguously a drag, capture the pointer so
                // it keeps tracking even if the cursor leaves the disc.
                try {
                    self._svg.setPointerCapture(event.pointerId);
                    active.captured = true;
                } catch (e) {}
            }

            active.node.x = active.originX + dx;
            active.node.y = active.originY + dy;
            self._redrawResidue(active.group, active.node);
        });

        var endDrag = function (event) {
            if (!active) return;
            if (active.moved) {
                self._positionOverrides = self._positionOverrides || {};
                self._positionOverrides[active.key] = { x: active.node.x, y: active.node.y };
                self._persistOverrides();
                // Connectors are anchored to the old position until redrawn.
                self.render();
            }
            if (active.captured) {
                try { self._svg.releasePointerCapture(event.pointerId); } catch (e) {}
            }
            active = null;
        };
        this._svg.addEventListener('pointerup', endDrag);
        this._svg.addEventListener('pointercancel', endDrag);

        // Suppress the click that follows a drag, so it does not toggle focus.
        this._svg.addEventListener('click', function (event) {
            if (self._suppressNextClick) {
                self._suppressNextClick = false;
                event.stopImmediatePropagation();
                event.preventDefault();
            }
        }, true);
        this._svg.addEventListener('pointerup', function () {
            self._suppressNextClick = !!(active && active.moved);
        }, true);
    };

    /** Move one already-rendered residue group without a full re-render. */
    LigandDiagram.prototype._redrawResidue = function (group, node) {
        var circle = group.querySelector('.lid-residue-disc');
        if (circle) {
            circle.setAttribute('cx', node.x.toFixed(2));
            circle.setAttribute('cy', node.y.toFixed(2));
        }
        var texts = group.querySelectorAll('text');
        if (texts[0]) {
            texts[0].setAttribute('x', node.x.toFixed(2));
            texts[0].setAttribute('y', (node.y - 2).toFixed(2));
        }
        if (texts[1]) {
            texts[1].setAttribute('x', node.x.toFixed(2));
            texts[1].setAttribute('y', (node.y + 10).toFixed(2));
        }
    };

    /** Reset every dragged disc back to the computed layout. */
    LigandDiagram.prototype.resetLayout = function () {
        this._positionOverrides = {};
        this._persistOverrides();
        this.render();
        return this;
    };

    LigandDiagram.prototype._storageKey = function () {
        if (!this.data) return null;
        var ligand = this.data.ligand || {};
        return 'phonosLidLayout:' + (ligand.name || 'LIG') + ':' + (ligand.atoms || []).length;
    };

    LigandDiagram.prototype._persistOverrides = function () {
        var key = this._storageKey();
        if (!key) return;
        try {
            var overrides = this._positionOverrides || {};
            if (Object.keys(overrides).length) {
                window.localStorage.setItem(key, JSON.stringify(overrides));
            } else {
                window.localStorage.removeItem(key);
            }
        } catch (e) {}
    };

    LigandDiagram.prototype._loadOverrides = function () {
        this._positionOverrides = {};
        var key = this._storageKey();
        if (!key) return;
        try {
            var stored = window.localStorage.getItem(key);
            if (stored) this._positionOverrides = JSON.parse(stored) || {};
        } catch (e) {
            this._positionOverrides = {};
        }
    };

    LigandDiagram.prototype._highlightResidue = function (key, on) {
        if (!this._svg) return;
        var lines = this._svg.querySelectorAll('.lid-interaction[data-residue-key="'
            + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
        Array.prototype.forEach.call(lines, function (line) {
            line.classList.toggle('is-highlighted', !!on);
        });
    };

    LigandDiagram.prototype._applyView = function () {
        if (!this._scene) return;
        this._scene.setAttribute('transform',
            'scale(' + this.view.scale + ') translate(' + this.view.x + ',' + this.view.y + ')');
    };

    LigandDiagram.prototype.resetView = function () {
        this.view = { scale: 1, x: 0, y: 0 };
        this._applyView();
        return this;
    };

    LigandDiagram.prototype.zoomBy = function (factor) {
        this.view.scale = Math.max(0.4, Math.min(6, this.view.scale * factor));
        this._applyView();
        return this;
    };

    /** Legend entries actually present in the current payload. */
    LigandDiagram.prototype.getLegend = function () {
        if (!this.data) return { interactions: [], residues: [] };
        var counts = this.data.counts || {};
        var self = this;
        // Every type present in the payload is listed, including ones currently
        // filtered out - otherwise a type toggled off would vanish from the
        // legend and there would be no way to click it back on.
        var interactions = Object.keys(counts)
            .filter(function (type) { return counts[type]; })
            .map(function (type) {
                var style = interactionStyle(type);
                return {
                    type: type, label: style.label, color: style.color,
                    count: counts[type], enabled: self._typeEnabled(type)
                };
            });

        var seen = {};
        (this.data.residues || []).forEach(function (res) { seen[res.category] = true; });
        var residues = Object.keys(seen).map(function (category) {
            var style = residueStyle(category);
            return { category: category, label: style.label, fill: style.fill, stroke: style.stroke };
        });
        return { interactions: interactions, residues: residues };
    };

    /** Serialize the current diagram as a standalone SVG string. */
    LigandDiagram.prototype.toSVGString = function () {
        if (!this._svg) return '';
        var clone = this._svg.cloneNode(true);
        clone.setAttribute('xmlns', SVG_NS);
        // Inline the stylesheet rules the diagram depends on, so the exported
        // file renders identically outside the app.
        var style = document.createElementNS(SVG_NS, 'style');
        style.textContent = LigandDiagram.EXPORT_STYLES;
        clone.insertBefore(style, clone.firstChild);
        var rect = this._viewBoxBase;
        clone.setAttribute('width', Math.round(rect.w));
        clone.setAttribute('height', Math.round(rect.h));
        return new XMLSerializer().serializeToString(clone);
    };

    LigandDiagram.prototype.downloadSVG = function (filename) {
        var svgText = this.toSVGString();
        if (!svgText) return;
        var blob = new Blob([svgText], { type: 'image/svg+xml;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = filename || 'ligand-interaction-diagram.svg';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    };

    /** Rasterize to PNG at `scale`x the natural size. */
    LigandDiagram.prototype.downloadPNG = function (filename, scale) {
        var svgText = this.toSVGString();
        if (!svgText) return;
        var rect = this._viewBoxBase;
        var factor = scale || 2;
        var blob = new Blob([svgText], { type: 'image/svg+xml;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var image = new Image();
        image.onload = function () {
            var canvas = document.createElement('canvas');
            canvas.width = Math.round(rect.w * factor);
            canvas.height = Math.round(rect.h * factor);
            var ctx = canvas.getContext('2d');
            ctx.fillStyle = '#101821';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
            URL.revokeObjectURL(url);
            canvas.toBlob(function (pngBlob) {
                if (!pngBlob) return;
                var pngUrl = URL.createObjectURL(pngBlob);
                var link = document.createElement('a');
                link.href = pngUrl;
                link.download = filename || 'ligand-interaction-diagram.png';
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                setTimeout(function () { URL.revokeObjectURL(pngUrl); }, 1000);
            }, 'image/png');
        };
        image.onerror = function () { URL.revokeObjectURL(url); };
        image.src = url;
    };

    // Styles duplicated into exported SVGs (the app stylesheet is not available
    // to a downloaded file).
    LigandDiagram.EXPORT_STYLES = [
        '.lid-svg{background:#101821;}',
        '.lid-bond{stroke:#cbd5e1;stroke-width:1.9;stroke-linecap:round;}',
        '.lid-bond-aromatic{stroke-dasharray:5 3;}',
        '.lid-atom-bg{fill:#101821;}',
        '.lid-atom-label{font:600 12px system-ui,sans-serif;text-anchor:middle;}',
        '.lid-atom-charge{font:600 10px system-ui,sans-serif;fill:#fbbf24;text-anchor:middle;}',
        '.lid-residue-name{font:700 11px system-ui,sans-serif;fill:#f8fafc;text-anchor:middle;}',
        '.lid-residue-num{font:500 9px system-ui,sans-serif;fill:#cbd5e1;text-anchor:middle;}',
        '.lid-distance-label{font:500 8.5px system-ui,sans-serif;text-anchor:middle;}',
        '.lid-water-node{fill:#1e3a5f;stroke:#60a5fa;stroke-width:1.4;}',
        '.lid-water-label{font:600 8px system-ui,sans-serif;fill:#bfdbfe;text-anchor:middle;}'
    ].join('');

    LigandDiagram.INTERACTION_STYLES = INTERACTION_STYLES;
    LigandDiagram.RESIDUE_CATEGORY_STYLES = RESIDUE_CATEGORY_STYLES;

    LigandDiagram.create = function (container, options) {
        return new LigandDiagram(container, options);
    };

    global.PhonosLigandDiagram = LigandDiagram;
})(window);
