/* Pipelines: New Pipeline builder + Pipelines list.
 * Self-contained module (mirrors DatabasesModule/BunchesModule pattern) so the
 * monolithic app.js only needs two switch cases that delegate here.
 * Chains interchangeable ONLINE models (Fold/Dock/MolGen/DNAGen/Design); a
 * pipeline always costs a flat 100 credits regardless of matrix size.
 */
(function () {
  "use strict";

  const esc = (s) => (window.escapeHtml ? window.escapeHtml(String(s ?? "")) : String(s ?? ""));

  // Mirror of production/config.py PIPELINE_TASK_CATEGORY.
  const TASK_CATEGORY = {
    protein_folding: "folding",
    structure_prediction: "folding",
    protein_complex_folding: "folding",
    enhanced_structure_prediction: "folding",
    molecular_docking: "docking",
    molecule_generation: "molecule_gen",
    dna_generation: "dna_gen",
    dna_sequence_generation: "dna_gen",
    protein_design: "protein_design",
    fold_and_dock: "integrated",
  };

  const CATEGORY_LABELS = {
    dna_gen: "DNA Generation",
    molecule_gen: "Molecule Generation",
    folding: "Fold (Structure)",
    protein_design: "Protein Design",
    docking: "Dock",
    integrated: "Fold + Dock",
  };

  // Per-category visual identity (color + short label + icon paths).
  const CATEGORY_META = {
    dna_gen:        { short: "DNA Gen", color: "#22c55e", paths: 'M5 3c0 5 14 6 14 8s-14 3-14 8M19 3c0 5-14 6-14 8s14 3 14 8M7 7h10M6.5 12h11M7 17h10' },
    molecule_gen:   { short: "MolGen",  color: "#06b6d4", paths: 'M12 2l8 4.7v9.3L12 22l-8-4.7V6.7zM12 8v0M12 12v0' },
    folding:        { short: "Fold",    color: "#a855f7", paths: 'M3 7c4-4 14 4 18 0M3 12c4-4 14 4 18 0M3 17c4-4 14 4 18 0' },
    protein_design: { short: "Design",  color: "#f59e0b", paths: 'M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z' },
    docking:        { short: "Dock",    color: "#ec4899", paths: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6M12 2v3M12 19v3M2 12h3M19 12h3' },
    integrated:     { short: "Fold+Dock", color: "#8b5cf6", paths: 'M3 12c4-4 14 4 18 0M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6' },
  };

  const catColor = (c) => (CATEGORY_META[c] || {}).color || "#a855f7";
  const catIcon = (c, size) => {
    const m = CATEGORY_META[c] || { paths: "" };
    return `<svg viewBox="0 0 24 24" width="${size || 18}" height="${size || 18}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${
      m.paths.split("M").filter(Boolean).map((d) => `<path d="M${d}"/>`).join("")
    }</svg>`;
  };
  const flowConnector = `<div class="pipe-conn"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M19 12l-7 7-7-7"/></svg></div>`;

  // Mirror of api_pipelines._STAGE_REQUIRES / _STAGE_PRODUCES.
  const REQUIRES = {
    folding: ["sequence"], docking: ["structure"], protein_design: ["structure"],
    molecule_gen: [], dna_gen: [], integrated: [],
  };
  const PRODUCES = {
    folding: ["structure"], docking: ["poses"], protein_design: ["structure", "sequence"],
    molecule_gen: ["ligand"], dna_gen: ["sequence"], integrated: ["structure"],
  };

  const PRESETS = [
    { name: "Fold → Dock", cats: ["folding", "docking"] },
    { name: "MolGen → Dock", cats: ["molecule_gen", "docking"] },
    { name: "Fold + MolGen → Dock", cats: ["folding", "molecule_gen", "docking"] },
    { name: "DNAGen → Fold → Dock", cats: ["dna_gen", "folding", "docking"] },
  ];

  const PipelineModule = {
    state: {
      projectId: null,
      projects: [],
      modelsByCat: {},   // {category: [model, ...]} online only
      stages: [],        // [{category, model, params:{}}]
      seed: { type: "sequence", sequence: "", file_id: null },
      files: [],
      estimate: null,
    },

    // ---------------------------------------------------------------
    // NEW PIPELINE
    // ---------------------------------------------------------------
    async renderSubmit(contentArea) {
      this.area = contentArea;
      this.state.stages = [];
      try {
        const [projects, models] = await Promise.all([
          API.get("/api/projects"),
          API.get("/api/available_models"),
        ]);
        this.state.projects = Array.isArray(projects) ? projects : (projects.projects || []);
        this._indexModels(models);
      } catch (e) {
        contentArea.innerHTML = `<div class="pipe-wrap"><p class="pipe-error">Failed to load: ${esc(e.message)}</p></div>`;
        return;
      }
      this._renderShell();
    },

    _indexModels(models) {
      const list = Array.isArray(models) ? models : [];
      const byCat = {};
      list.forEach((m) => {
        // ONLINE only: runtime_available + visible + available.
        const online = m.runtime_available && (m.is_visible !== false) && (m.available !== false);
        if (!online) return;
        const cat = TASK_CATEGORY[m.task];
        if (!cat || cat === "integrated") return; // integrated handled as single-shot, optional
        (byCat[cat] = byCat[cat] || []).push(m);
      });
      this.state.modelsByCat = byCat;
    },

    _availableCategories() {
      // Categories that have at least one online model.
      return Object.keys(CATEGORY_LABELS).filter(
        (c) => (this.state.modelsByCat[c] || []).length > 0
      );
    },

    // Which categories can be appended given current chain + seed.
    _nextCategories() {
      const avail = new Set(this._seedKinds());
      this.state.stages.forEach((s) => (PRODUCES[s.category] || []).forEach((k) => avail.add(k)));
      const out = [];
      this._availableCategories().forEach((cat) => {
        const reqs = REQUIRES[cat] || [];
        const ok = this.state.stages.length === 0
          ? true
          : reqs.every((r) => avail.has(r));
        if (ok) out.push(cat);
      });
      return out;
    },

    _seedKinds() {
      const t = this.state.seed.type;
      if (t === "smiles") return ["ligand"];
      if (t === "file") return ["structure", "sequence"];
      return ["sequence"];
    },

    _hero(title, description, buttons) {
      if (window.Views && Views.createHeroBanner) {
        return Views.createHeroBanner({
          gradient: "linear-gradient(135deg, #a855f7 0%, #7c3aed 100%)",
          icon: `<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2">
                   <circle cx="5" cy="6" r="2"/><circle cx="12" cy="6" r="2"/><circle cx="19" cy="6" r="2"/>
                   <path d="M7 6h3M14 6h3"/><path d="M12 8v4M5 8v8a2 2 0 0 0 2 2h10"/></svg>`,
          title, description, buttons: buttons || [],
        });
      }
      return `<div class="pipe-hero"><h1>${esc(title)}</h1><p>${esc(description)}</p></div>`;
    },

    _renderShell() {
      const projOpts = this.state.projects
        .map((p) => `<option value="${p.id}" ${p.id == this.state.projectId ? "selected" : ""}>${esc(p.name)}</option>`)
        .join("");
      const hidden = this.state.projectId ? "" : "pipe-hidden";
      this.area.innerHTML = `
        ${this._hero("New Pipeline", "Chain interchangeable online models. Generated outputs matrix-expand into the next stage. Flat 100 credits per pipeline.")}

        <div class="card">
          <div class="card-header"><h3 class="card-title">Configure Pipeline</h3></div>
          <div class="card-body" style="padding: 0;">
            <div class="form-group" style="padding: var(--space-4); margin-bottom: 0; border-bottom: 1px solid var(--color-border);">
              <label class="form-label">Select Project</label>
              <select class="form-select" id="pipe-project" style="max-width: 400px;">
                <option value="">Select a project...</option>${projOpts}
              </select>
            </div>

            <div id="pipe-builder" class="${hidden}" style="padding: var(--space-4);">
              <div class="form-group">
                <label class="form-label">Pipeline name</label>
                <input id="pipe-name" class="form-input" placeholder="My Fold-Dock run" style="max-width: 400px;" />
              </div>

              <div class="form-group">
                <label class="form-label">Seed input</label>
                <select id="pipe-seed-type" class="form-select" style="max-width: 240px; margin-bottom: 10px;">
                  <option value="sequence">Paste sequence</option>
                  <option value="file">Project file</option>
                  <option value="smiles">SMILES (ligand)</option>
                </select>
                <div id="pipe-seed-input"></div>
              </div>

              <div class="form-group">
                <label class="form-label">Quick start</label>
                <div class="pipe-presets">
                  ${PRESETS.map((p, i) => `
                    <button type="button" class="pipe-preset" data-preset="${i}">
                      <div class="pipe-preset-flow">${p.cats.map((c, j) =>
                        `${j ? '<span class="pipe-preset-arrow">→</span>' : ""}<span class="pipe-preset-node" style="color:${catColor(c)}">${catIcon(c, 16)}</span>`).join("")}</div>
                      <span class="pipe-preset-name">${esc(p.name)}</span>
                    </button>`).join("")}
                </div>
              </div>

              <div class="form-group" style="margin-bottom: 0;">
                <label class="form-label">Stages</label>
                <div id="pipe-stages"></div>
                <div id="pipe-add"></div>
              </div>
            </div>
          </div>
        </div>

        <div id="pipe-launch-bar" class="pipe-footer ${hidden}">
          <div class="pipe-cost"><span class="pipe-cost-num">100</span> credits<span id="pipe-est" class="pipe-est"></span></div>
          <button id="pipe-launch" class="btn btn-primary" disabled>Launch pipeline</button>
        </div>`;
      this._bindShell();
      this._renderSeedInput();
      this._renderStages();
    },

    _bindShell() {
      const proj = document.getElementById("pipe-project");
      proj.addEventListener("change", async () => {
        this.state.projectId = proj.value || null;
        const hide = !this.state.projectId;
        document.getElementById("pipe-builder").classList.toggle("pipe-hidden", hide);
        const bar = document.getElementById("pipe-launch-bar");
        if (bar) bar.classList.toggle("pipe-hidden", hide);
        if (this.state.projectId) await this._loadFiles();
      });
      document.getElementById("pipe-seed-type").addEventListener("change", (e) => {
        this.state.seed.type = e.target.value;
        this._renderSeedInput();
        this._renderStages();
        this._refreshEstimate();
      });
      this.area.querySelectorAll("[data-preset]").forEach((btn) => {
        btn.addEventListener("click", () => this._applyPreset(parseInt(btn.dataset.preset, 10)));
      });
      document.getElementById("pipe-launch").addEventListener("click", () => this._launch());
    },

    async _loadFiles() {
      try {
        const files = await API.get(`/api/projects/${this.state.projectId}/files`);
        this.state.files = Array.isArray(files) ? files : (files.files || []);
      } catch (_) { this.state.files = []; }
      this._renderSeedInput();
    },

    _renderSeedInput() {
      const host = document.getElementById("pipe-seed-input");
      if (!host) return;
      const t = this.state.seed.type;
      if (t === "file") {
        const opts = this.state.files
          .map((f) => `<option value="${f.id}">${esc(f.name || f.filename)}</option>`).join("");
        host.innerHTML = `<select id="pipe-seed-file" class="form-select">
            <option value="">Select a file…</option>${opts}</select>`;
        const sel = document.getElementById("pipe-seed-file");
        sel.addEventListener("change", () => { this.state.seed.file_id = sel.value || null; });
      } else {
        const ph = t === "smiles" ? "CCO (SMILES seed)" : "Paste protein/DNA sequence (FASTA or raw)";
        host.innerHTML = `<textarea id="pipe-seed-seq" class="form-input pipe-textarea" placeholder="${ph}"></textarea>`;
        document.getElementById("pipe-seed-seq").addEventListener("input", (e) => {
          this.state.seed.sequence = e.target.value;
        });
      }
    },

    // --- model + param helpers -------------------------------------
    _modelById(id) {
      for (const cat in this.state.modelsByCat) {
        const m = (this.state.modelsByCat[cat] || []).find((x) => x.id === id);
        if (m) return m;
      }
      return null;
    },

    _defaultParams(model) {
      const p = {};
      ((model && model.custom_fields) || []).forEach((f) => {
        if (f.name && f.default !== undefined && f.default !== null) p[f.name] = f.default;
      });
      return p;
    },

    // Input-source fields the orchestrator wires automatically (so we hide them).
    _hiddenFields(category, idx) {
      const base = {
        docking: ["protein_input_type", "protein_file", "protein_pdb_id",
                  "protein_uniprot_id", "protein"],
        protein_design: ["input_pdb", "input_pdb_content", "pdb_path_chains"],
        folding: [], molecule_gen: [], dna_gen: [], integrated: [],
      }[category] || [];
      const set = new Set(base);
      if (category === "docking") {
        const upstreamMol = this.state.stages.slice(0, idx).some((s) => s.category === "molecule_gen");
        if (upstreamMol) ["ligand", "ligand_input_type", "ligand_file",
          "ligand_file_type", "ligand_pubchem_cid"].forEach((f) => set.add(f));
      }
      return set;
    },

    _stageNote(stage, idx) {
      const find = (cats) => {
        for (let i = idx - 1; i >= 0; i--) if (cats.includes(this.state.stages[i].category)) return i + 1;
        return null;
      };
      const bits = [];
      if (stage.category === "folding") {
        const s = find(["dna_gen"]);
        bits.push(s ? `Sequence from Stage ${s} (DNA Generation)` : "Sequence from seed input");
      } else if (stage.category === "docking") {
        const p = find(["folding", "integrated", "protein_design"]);
        bits.push(p ? `Protein from Stage ${p}` : "Protein from seed input");
        const m = find(["molecule_gen"]);
        if (m) bits.push(`Ligands from Stage ${m} (every molecule x every protein)`);
      } else if (stage.category === "protein_design") {
        const p = find(["folding", "integrated"]);
        bits.push(p ? `Structure from Stage ${p}` : "Structure from seed input");
      } else if (stage.category === "molecule_gen" || stage.category === "dna_gen") {
        bits.push("Seeded from the parameters below");
      }
      return bits.join(" &middot; ");
    },

    _renderField(idx, field, value) {
      const fid = `pf-${idx}-${field.name}`;
      const label = esc(field.label || field.name);
      const data = `data-stage="${idx}" data-field="${esc(field.name)}"`;
      const v = value;
      if (field.type === "checkbox") {
        return `<label class="pipe-check"><input type="checkbox" id="${fid}" ${data} ${v ? "checked" : ""}> ${label}</label>`;
      }
      let input;
      if (field.type === "select") {
        const opts = (field.options || []).map((o) =>
          `<option value="${esc(o.value)}" ${String(v) === String(o.value) ? "selected" : ""}>${esc(o.label)}</option>`).join("");
        input = `<select id="${fid}" ${data} class="form-select">${opts}</select>`;
      } else if (field.type === "range") {
        input = `<div class="pipe-range"><input type="range" id="${fid}" ${data} min="${field.min}" max="${field.max}" step="${field.step || 1}" value="${v}"><output id="${fid}-out">${esc(v)}</output></div>`;
      } else if (field.type === "number") {
        input = `<input type="number" id="${fid}" ${data} class="form-input" value="${esc(v ?? "")}"${field.min != null ? ` min="${field.min}"` : ""}${field.max != null ? ` max="${field.max}"` : ""}${field.step != null ? ` step="${field.step}"` : ""}>`;
      } else if (field.type === "textarea") {
        input = `<textarea id="${fid}" ${data} class="form-input" rows="2" placeholder="${esc(field.placeholder || "")}">${esc(v ?? "")}</textarea>`;
      } else if (field.type === "file") {
        const opts = this.state.files.map((f) =>
          `<option value="${f.id}" ${String(v) === String(f.id) ? "selected" : ""}>${esc(f.name || f.filename)}</option>`).join("");
        input = `<select id="${fid}" ${data} class="form-select"><option value="">Select file…</option>${opts}</select>`;
      } else {
        input = `<input type="text" id="${fid}" ${data} class="form-input" value="${esc(v ?? "")}" placeholder="${esc(field.placeholder || "")}">`;
      }
      return `<div class="pipe-field"><label class="pipe-field-label">${label}</label>${input}</div>`;
    },

    _newStage(cat) {
      const models = this.state.modelsByCat[cat] || [];
      const model = models[0] || null;
      return { category: cat, model: model ? model.id : null, params: this._defaultParams(model) };
    },

    _applyPreset(i) {
      const preset = PRESETS[i];
      if (!preset) return;
      this.state.stages = preset.cats.map((cat) => this._newStage(cat)).filter((s) => s.model);
      this._renderStages();
      this._refreshEstimate();
    },

    _renderStages() {
      const host = document.getElementById("pipe-stages");
      if (!host) return;
      const inner = this.state.stages
        .map((s, idx) => (idx ? flowConnector : "") + this._stageRow(s, idx)).join("");
      host.innerHTML = this.state.stages.length
        ? `<div class="pipe-flow">${inner}</div>`
        : `<div class="pipe-empty">
             <div class="pipe-empty-icon">${catIcon("folding", 26)}</div>
             <p>No stages yet.<br>Pick a quick-start preset above, or add a stage below.</p>
           </div>`;

      // Model switch -> reset that stage's params to the new model's defaults.
      this.state.stages.forEach((s, idx) => {
        const modelSel = document.getElementById(`pipe-model-${idx}`);
        if (modelSel) modelSel.addEventListener("change", (e) => {
          const st = this.state.stages[idx];
          st.model = e.target.value;
          st.params = this._defaultParams(this._modelById(st.model));
          this._renderStages(); this._refreshEstimate();
        });
        const rm = document.getElementById(`pipe-rm-${idx}`);
        if (rm) rm.addEventListener("click", () => {
          this.state.stages.splice(idx, 1); this._renderStages(); this._refreshEstimate();
        });
      });

      // Per-field value capture (no re-render, so focus/scroll is preserved).
      host.querySelectorAll("[data-field]").forEach((el) => {
        el.addEventListener("input", () => {
          const idx = parseInt(el.dataset.stage, 10);
          const name = el.dataset.field;
          if (!this.state.stages[idx]) return;
          let val;
          if (el.type === "checkbox") val = el.checked;
          else if (el.type === "number" || el.type === "range") val = el.value === "" ? "" : Number(el.value);
          else val = el.value;
          this.state.stages[idx].params[name] = val;
          const out = document.getElementById(el.id + "-out");
          if (out) out.textContent = el.value;
        });
      });

      this._renderAdd();
      this._updateLaunch();
    },

    _stageRow(stage, idx) {
      const models = this.state.modelsByCat[stage.category] || [];
      const opts = models.map((m) =>
        `<option value="${m.id}" ${m.id === stage.model ? "selected" : ""}>${esc(m.short_name || m.name || m.id)}</option>`
      ).join("");
      const model = this._modelById(stage.model);
      const hidden = this._hiddenFields(stage.category, idx);
      const fields = ((model && model.custom_fields) || []).filter((f) => f.name && !hidden.has(f.name));
      const note = this._stageNote(stage, idx);
      const fieldsHtml = fields.map((f) => this._renderField(idx, f, stage.params[f.name])).join("");
      const paramsBlock = fields.length
        ? `<details class="pipe-params" open>
             <summary>${fields.length} parameter${fields.length > 1 ? "s" : ""}</summary>
             <div class="pipe-fields-grid">${fieldsHtml}</div>
           </details>`
        : `<p class="pipe-muted" style="margin:6px 0 0;">No configurable parameters.</p>`;
      const color = catColor(stage.category);
      return `
        <div class="pipe-stage" id="pipe-stage-${idx}" style="--stage-color:${color};">
          <div class="pipe-stage-head">
            <div class="pipe-stage-chip" style="background:${color}22;color:${color};">${catIcon(stage.category, 18)}</div>
            <div class="pipe-stage-titles">
              <div class="pipe-stage-cat">${esc(CATEGORY_LABELS[stage.category] || stage.category)}</div>
              <div class="pipe-stage-step">Stage ${idx + 1}</div>
            </div>
            <select id="pipe-model-${idx}" class="form-select pipe-mini" title="Model (interchangeable)">${opts}</select>
            <button id="pipe-rm-${idx}" class="pipe-rm" title="Remove stage" aria-label="Remove stage">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
            </button>
          </div>
          ${note ? `<div class="pipe-stage-note">${note}</div>` : ""}
          ${paramsBlock}
        </div>`;
    },

    _renderAdd() {
      const host = document.getElementById("pipe-add");
      if (!host) return;
      if (this.state.stages.length >= 5) { host.innerHTML = `<p class="pipe-muted" style="text-align:center;">Maximum of 5 stages reached.</p>`; return; }
      const cats = this._nextCategories();
      const hasStages = this.state.stages.length > 0;
      host.innerHTML = `
        ${hasStages ? flowConnector : ""}
        <div class="pipe-add-tile">
          <div class="pipe-add-label">${hasStages ? "Add next stage" : "Add a stage"}</div>
          <div class="pipe-add-row">${cats.map((c) =>
            `<button class="pipe-add-btn" data-cat="${c}" style="--stage-color:${catColor(c)};">
               <span class="pipe-add-ic" style="color:${catColor(c)};">${catIcon(c, 16)}</span>${esc(CATEGORY_LABELS[c] || c)}
             </button>`).join("") ||
            `<span class="pipe-muted">No compatible model online to add next.</span>`}</div>
        </div>`;
      host.querySelectorAll("[data-cat]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const st = this._newStage(btn.dataset.cat);
          if (!st.model) return;
          this.state.stages.push(st);
          this._renderStages(); this._refreshEstimate();
        });
      });
    },

    _updateLaunch() {
      const btn = document.getElementById("pipe-launch");
      if (btn) btn.disabled = !(this.state.projectId && this.state.stages.length && this.state.stages.every((s) => s.model));
    },

    _payload() {
      const seed = {};
      if (this.state.seed.type === "file") { seed.type = "file"; seed.file_id = this.state.seed.file_id; }
      else if (this.state.seed.type === "smiles") { seed.type = "smiles"; seed.sequence = this.state.seed.sequence; }
      else { seed.type = "sequence"; seed.sequence = this.state.seed.sequence; }
      return {
        name: (document.getElementById("pipe-name") || {}).value || "",
        seed_input: seed,
        stages: this.state.stages.map((s) => ({ model: s.model, custom_params: s.params || {} })),
      };
    },

    _refreshEstimate() {
      this._updateLaunch();
      const el = document.getElementById("pipe-est");
      if (!el || !this.state.projectId || !this.state.stages.length) { if (el) el.textContent = ""; return; }
      clearTimeout(this._estTimer);
      this._estTimer = setTimeout(async () => {
        try {
          const r = await API.post(`/api/projects/${this.state.projectId}/pipelines/estimate`, this._payload());
          el.textContent = ` · ≈ up to ${r.estimated_max_jobs} jobs${r.capped ? " (capped)" : ""}`;
        } catch (e) { el.textContent = ` · ${esc(e.message)}`; }
      }, 350);
    },

    async _launch() {
      const btn = document.getElementById("pipe-launch");
      btn.disabled = true; btn.textContent = "Launching…";
      try {
        const res = await API.post(`/api/projects/${this.state.projectId}/pipelines`, this._payload());
        if (window.Toast) Toast.success("Pipeline launched!");
        const pid = res.pipeline_id;
        if (pid) window.location.href = `/view_pipeline/${pid}`;
        else if (window.Views) Views.load("pipelines");
      } catch (e) {
        if (window.Toast) Toast.error(e.message || "Failed to launch pipeline");
        btn.disabled = false; btn.textContent = "Launch pipeline";
      }
    },

    // ---------------------------------------------------------------
    // PIPELINES LIST
    // ---------------------------------------------------------------
    async renderList(contentArea) {
      this.area = contentArea;
      let pipelines = [];
      try {
        pipelines = await API.get("/api/pipelines/all", { skipCache: true });
      } catch (e) {
        contentArea.innerHTML = `<div class="pipe-wrap"><p class="pipe-error">Failed to load pipelines: ${esc(e.message)}</p></div>`;
        return;
      }
      const list = Array.isArray(pipelines) ? pipelines : [];
      const count = (s) => list.filter((p) => p.status === s).length;
      const stats = [
        { label: "Total", value: list.length, color: "#a855f7" },
        { label: "Running", value: count("running") + count("pending"), color: "#60a5fa" },
        { label: "Completed", value: count("completed"), color: "#4ade80" },
        { label: "Partial / Failed", value: count("partial") + count("failed"), color: "#f87171" },
      ];
      const rows = list.map((p) => this._listRow(p)).join("");
      contentArea.innerHTML = `
        ${this._hero("Pipelines", "Multi-stage model runs. Each pipeline is a flat 100 credits.",
          [`<button class="btn btn-primary" style="background:white;color:#7c3aed;border:none;font-weight:600;" onclick="Views.load('submit-pipeline')">+ New Pipeline</button>`])}
        <div class="pipe-stats">
          ${stats.map((s) => `<div class="pipe-stat"><div class="pipe-stat-val" style="color:${s.color};">${s.value}</div><div class="pipe-stat-lbl">${s.label}</div></div>`).join("")}
        </div>
        <div class="pipe-list">
          ${rows || `<div class="pipe-empty"><div class="pipe-empty-icon">${catIcon("docking", 26)}</div><p>No pipelines yet.<br>Create one to chain Fold &rarr; Dock and more.</p><button class="btn btn-primary" onclick="Views.load('submit-pipeline')" style="margin-top:14px;">+ New Pipeline</button></div>`}
        </div>`;
      this._bindLive();
    },

    _miniFlow(stages) {
      if (!stages || !stages.length) return "";
      return `<div class="pipe-mini-flow">${stages.map((s, j) =>
        `${j ? '<span class="pipe-mini-arrow">→</span>' : ""}<span class="pipe-mini-node" style="color:${catColor(s.category)}" title="${esc(CATEGORY_LABELS[s.category] || s.category)}">${catIcon(s.category, 15)}</span>`).join("")}</div>`;
    },

    _listRow(p) {
      const total = p.total_jobs || 0;
      const done = p.completed_jobs || 0;
      const failed = p.failed_jobs || 0;
      const pct = total ? Math.round((done / total) * 100) : 0;
      const failPct = total ? Math.round((failed / total) * 100) : 0;
      const stages = (p.definition && p.definition.stages) || [];
      const status = p.status || "pending";
      const stageTxt = `Stage ${Math.min((p.current_stage ?? 0) + 1, p.total_stages || 1)}/${p.total_stages || "?"}`;
      return `
        <div class="pipe-card-row pipe-accent-${esc(status)}" data-pid="${esc(p.pipeline_id)}" onclick="window.location.href='/view_pipeline/${esc(p.pipeline_id)}'">
          <div class="pipe-card-top">
            <div class="pipe-card-left">
              <div class="pipe-card-name">${esc(p.name)}</div>
              ${this._miniFlow(stages)}
            </div>
            <span class="pipe-pill pipe-status-${esc(status)}">${esc(status)}</span>
          </div>
          <div class="pipe-card-bar"><span style="width:${pct}%;"></span><span class="pipe-card-bar-fail" style="width:${failPct}%;"></span></div>
          <div class="pipe-card-meta">
            <span>${esc(p.project_name || "")}</span>
            <span>${stageTxt}</span>
            <span>${done}/${total} jobs${failed ? ` &middot; ${failed} failed` : ""}</span>
            <span>${p.cost || 100} credits</span>
          </div>
        </div>`;
    },

    _bindLive() {
      if (this._liveBound || !window.socket) return;
      this._liveBound = true;
      window.socket.on("pipeline_update", () => {
        if (window.Views && Views.current === "pipelines") this.renderList(this.area);
      });
    },
  };

  window.PipelineModule = PipelineModule;
})();
