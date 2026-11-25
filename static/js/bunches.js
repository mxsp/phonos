/**
 * Bunches Module - Batch Job Management
 * Handles UI for creating, managing, and monitoring job bunches
 * Uses safe DOM methods only - no innerHTML
 */

window.BunchesModule = {
    allBunches: [],
    projects: [],

    async loadData() {
        try {
            const projectsResponse = await API.get('/api/projects');
            this.projects = Array.isArray(projectsResponse) ? projectsResponse : [];

            const bunchPromises = this.projects.map(async (p) => {
                try {
                    const response = await API.get('/api/projects/' + p.id + '/bunches');
                    return (response.bunches || []).map(b => ({ ...b, projectName: p.name }));
                } catch (e) {
                    return [];
                }
            });

            const bunchResults = await Promise.all(bunchPromises);
            this.allBunches = bunchResults.flat().sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
        } catch (error) {
            console.error('Error loading bunches:', error);
            if (typeof Toast !== 'undefined') Toast.error('Failed to load batch jobs');
        }
    },

    getStatusInfo(status) {
        const statusMap = {
            'pending': { bg: 'rgba(234, 179, 8, 0.1)', color: '#eab308', label: 'Pending' },
            'running': { bg: 'rgba(59, 130, 246, 0.1)', color: '#3b82f6', label: 'Running' },
            'completed': { bg: 'rgba(16, 185, 129, 0.1)', color: '#10b981', label: 'Completed' },
            'partially_failed': { bg: 'rgba(245, 158, 11, 0.1)', color: '#f59e0b', label: 'Partial' },
            'cancelled': { bg: 'rgba(239, 68, 68, 0.1)', color: '#ef4444', label: 'Cancelled' }
        };
        return statusMap[status] || statusMap['pending'];
    },

    createSvgIcon(paths, size, color) {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('width', String(size || 20));
        svg.setAttribute('height', String(size || 20));
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', color || 'currentColor');
        svg.setAttribute('stroke-width', '2');

        paths.forEach(pathData => {
            if (pathData.type === 'rect') {
                const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                rect.setAttribute('x', pathData.x);
                rect.setAttribute('y', pathData.y);
                rect.setAttribute('width', pathData.width);
                rect.setAttribute('height', pathData.height);
                if (pathData.rx) rect.setAttribute('rx', pathData.rx);
                svg.appendChild(rect);
            } else if (pathData.type === 'line') {
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', pathData.x1);
                line.setAttribute('y1', pathData.y1);
                line.setAttribute('x2', pathData.x2);
                line.setAttribute('y2', pathData.y2);
                svg.appendChild(line);
            } else if (pathData.type === 'circle') {
                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', pathData.cx);
                circle.setAttribute('cy', pathData.cy);
                circle.setAttribute('r', pathData.r);
                svg.appendChild(circle);
            } else if (pathData.type === 'path') {
                const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                path.setAttribute('d', pathData.d);
                svg.appendChild(path);
            } else if (pathData.type === 'polyline') {
                const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
                polyline.setAttribute('points', pathData.points);
                svg.appendChild(polyline);
            }
        });

        return svg;
    },

    getBatchIcon(size, color) {
        return this.createSvgIcon([
            { type: 'rect', x: '3', y: '3', width: '7', height: '7', rx: '1' },
            { type: 'rect', x: '14', y: '3', width: '7', height: '7', rx: '1' },
            { type: 'rect', x: '3', y: '14', width: '7', height: '7', rx: '1' },
            { type: 'rect', x: '14', y: '14', width: '7', height: '7', rx: '1' }
        ], size, color);
    },

    getPlusIcon(size, color) {
        return this.createSvgIcon([
            { type: 'line', x1: '12', y1: '5', x2: '12', y2: '19' },
            { type: 'line', x1: '5', y1: '12', x2: '19', y2: '12' }
        ], size, color);
    },

    getPlayIcon(size, color) {
        return this.createSvgIcon([
            { type: 'path', d: 'M5 3 L19 12 L5 21 Z' }
        ], size, color);
    },

    getCloseIcon(size, color) {
        return this.createSvgIcon([
            { type: 'line', x1: '18', y1: '6', x2: '6', y2: '18' },
            { type: 'line', x1: '6', y1: '6', x2: '18', y2: '18' }
        ], size, color);
    },

    renderBunchCard(bunch) {
        const card = document.createElement('div');
        card.className = 'bunch-card';
        card.dataset.bunchId = bunch.bunch_id;
        card.dataset.status = bunch.status;
        card.style.cssText = 'background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--r-ui, 0px); padding: 16px; cursor: pointer; transition: all 0.2s;';

        const status = this.getStatusInfo(bunch.status);
        const progress = bunch.progress_percent || 0;

        // Header
        const header = document.createElement('div');
        header.style.cssText = 'display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;';

        const titleDiv = document.createElement('div');
        const title = document.createElement('div');
        title.style.cssText = 'font-weight: 600; color: var(--color-text-primary); margin-bottom: 4px;';
        title.textContent = bunch.name;
        titleDiv.appendChild(title);

        const subtitle = document.createElement('div');
        subtitle.style.cssText = 'font-size: 12px; color: var(--color-text-secondary);';
        subtitle.textContent = (bunch.total_jobs || 0) + ' items - ' + (bunch.projectName || 'Unknown');
        titleDiv.appendChild(subtitle);
        header.appendChild(titleDiv);

        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.style.cssText = 'background: ' + status.bg + '; color: ' + status.color + '; text-transform: capitalize;';
        badge.textContent = bunch.status;
        header.appendChild(badge);

        card.appendChild(header);

        // Progress bar
        const progressContainer = document.createElement('div');
        progressContainer.style.cssText = 'margin-bottom: 12px;';

        const progressBar = document.createElement('div');
        progressBar.style.cssText = 'height: 6px; background: var(--color-border); border-radius: var(--r-ui, 0px); overflow: hidden;';

        const progressFill = document.createElement('div');
        progressFill.style.cssText = 'width: ' + progress + '%; height: 100%; background: ' + status.color + '; border-radius: var(--r-ui, 0px); transition: width 0.3s;';
        progressBar.appendChild(progressFill);
        progressContainer.appendChild(progressBar);

        const progressText = document.createElement('div');
        progressText.style.cssText = 'display: flex; justify-content: space-between; margin-top: 4px; font-size: 11px; color: var(--color-text-secondary);';

        const completedSpan = document.createElement('span');
        completedSpan.textContent = (bunch.completed_jobs || 0) + ' completed';
        progressText.appendChild(completedSpan);

        const percentSpan = document.createElement('span');
        percentSpan.textContent = progress.toFixed(0) + '%';
        progressText.appendChild(percentSpan);

        progressContainer.appendChild(progressText);
        card.appendChild(progressContainer);

        // Actions
        const actions = document.createElement('div');
        actions.style.cssText = 'display: flex; gap: 8px;';

        if (bunch.status === 'pending') {
            const startBtn = document.createElement('button');
            startBtn.className = 'btn btn-sm btn-primary';
            startBtn.style.flex = '1';
            startBtn.textContent = 'Start';
            startBtn.onclick = (e) => {
                e.stopPropagation();
                window.BunchesModule.startBunch(bunch.bunch_id);
            };
            actions.appendChild(startBtn);
        }

        const viewBtn = document.createElement('button');
        viewBtn.className = 'btn btn-sm btn-ghost';
        viewBtn.textContent = 'View Output';
        viewBtn.onclick = (e) => {
            e.stopPropagation();
            window.BunchesModule.openBunchJobs(bunch.bunch_id);
        };
        actions.appendChild(viewBtn);

        card.appendChild(actions);

        card.onclick = () => this.openBunchJobs(bunch.bunch_id);

        return card;
    },

    renderEmptyState(container) {
        const empty = document.createElement('div');
        empty.className = 'card';
        empty.style.cssText = 'text-align: center; padding: 60px 40px;';

        const iconWrapper = document.createElement('div');
        iconWrapper.style.cssText = 'width: 80px; height: 80px; margin: 0 auto 20px; background: rgba(139, 92, 246, 0.1); border-radius: 50%; display: flex; align-items: center; justify-content: center;';
        iconWrapper.appendChild(this.getBatchIcon(40, '#8b5cf6'));
        empty.appendChild(iconWrapper);

        const title = document.createElement('h3');
        title.style.cssText = 'margin: 0 0 8px; color: var(--color-text-primary);';
        title.textContent = 'No Batch Jobs Yet';
        empty.appendChild(title);

        const desc = document.createElement('p');
        desc.style.cssText = 'margin: 0 0 24px; color: var(--color-text-secondary); max-width: 400px; margin-left: auto; margin-right: auto;';
        desc.textContent = 'Create a batch to submit multiple protein sequences for analysis at once. Perfect for large-scale screening.';
        empty.appendChild(desc);

        const btn = document.createElement('button');
        btn.className = 'btn btn-primary';
        btn.textContent = 'Create Your First Batch';
        btn.onclick = () => this.showCreateModal();
        empty.appendChild(btn);

        container.appendChild(empty);
    },

    render(container) {
        container.textContent = '';

        // Page header
        const header = document.createElement('div');
        header.className = 'page-header';
        header.style.cssText = 'display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px;';

        const headerLeft = document.createElement('div');
        const pageTitle = document.createElement('h2');
        pageTitle.className = 'page-title';
        pageTitle.style.cssText = 'margin: 0; display: flex; align-items: center; gap: 12px;';
        pageTitle.appendChild(this.getBatchIcon(28, '#8b5cf6'));
        const titleText = document.createElement('span');
        titleText.textContent = 'Batch Jobs';
        pageTitle.appendChild(titleText);
        headerLeft.appendChild(pageTitle);

        const pageSubtitle = document.createElement('p');
        pageSubtitle.className = 'page-subtitle';
        pageSubtitle.style.cssText = 'margin: 4px 0 0 0; color: var(--color-text-secondary);';
        pageSubtitle.textContent = 'Submit and manage multiple jobs at once';
        headerLeft.appendChild(pageSubtitle);
        header.appendChild(headerLeft);

        const createBtn = document.createElement('button');
        createBtn.className = 'btn btn-primary';
        createBtn.style.cssText = 'display: flex; align-items: center; gap: 8px;';
        createBtn.appendChild(this.getPlusIcon(18, 'currentColor'));
        const btnText = document.createElement('span');
        btnText.textContent = 'New Batch';
        createBtn.appendChild(btnText);
        createBtn.onclick = () => this.showCreateModal();
        header.appendChild(createBtn);

        container.appendChild(header);

        // Stats
        const pending = this.allBunches.filter(b => b.status === 'pending').length;
        const running = this.allBunches.filter(b => b.status === 'running').length;
        const completed = this.allBunches.filter(b => b.status === 'completed' || b.status === 'partially_failed').length;

        const stats = document.createElement('div');
        stats.style.cssText = 'display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 24px;';

        const statItems = [
            { label: 'Total Batches', value: this.allBunches.length, color: '#8b5cf6' },
            { label: 'Pending', value: pending, color: '#eab308' },
            { label: 'Running', value: running, color: '#3b82f6' },
            { label: 'Completed', value: completed, color: '#10b981' }
        ];

        statItems.forEach(item => {
            const statCard = document.createElement('div');
            statCard.style.cssText = 'background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--r-ui, 0px); padding: 16px;';

            const statInner = document.createElement('div');
            statInner.style.cssText = 'display: flex; align-items: center; gap: 12px;';

            const iconDiv = document.createElement('div');
            iconDiv.style.cssText = 'width: 40px; height: 40px; border-radius: var(--r-ui, 0px); background: ' + item.color + '20; display: flex; align-items: center; justify-content: center;';
            iconDiv.appendChild(this.getBatchIcon(20, item.color));
            statInner.appendChild(iconDiv);

            const textDiv = document.createElement('div');
            const valueEl = document.createElement('div');
            valueEl.style.cssText = 'font-size: 24px; font-weight: 700; color: var(--color-text-primary);';
            valueEl.textContent = item.value;
            textDiv.appendChild(valueEl);

            const labelEl = document.createElement('div');
            labelEl.style.cssText = 'font-size: 13px; color: var(--color-text-secondary);';
            labelEl.textContent = item.label;
            textDiv.appendChild(labelEl);

            statInner.appendChild(textDiv);
            statCard.appendChild(statInner);
            stats.appendChild(statCard);
        });

        container.appendChild(stats);

        // Bunches list
        if (this.allBunches.length === 0) {
            this.renderEmptyState(container);
        } else {
            const grid = document.createElement('div');
            grid.style.cssText = 'display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px;';

            this.allBunches.forEach(bunch => {
                grid.appendChild(this.renderBunchCard(bunch));
            });

            container.appendChild(grid);
        }
    },

    async showCreateModal() {
        if (this.projects.length === 0) {
            if (typeof Toast !== 'undefined') Toast.warning('Please create a project first');
            if (typeof Views !== 'undefined') Views.load('projects');
            return;
        }

        const models = (window.PHONOS_CONFIG && window.PHONOS_CONFIG.supportedModels) || [];

        const modalBody = document.createElement('div');

        // Form
        const form = document.createElement('form');
        form.id = 'create-bunch-form';

        // Name field
        const nameGroup = document.createElement('div');
        nameGroup.className = 'form-group';
        const nameLabel = document.createElement('label');
        nameLabel.className = 'form-label';
        nameLabel.textContent = 'Batch Name';
        nameGroup.appendChild(nameLabel);
        const nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.className = 'form-input';
        nameInput.name = 'name';
        nameInput.required = true;
        nameInput.placeholder = 'e.g., Protein Screening Batch 1';
        nameGroup.appendChild(nameInput);
        form.appendChild(nameGroup);

        // Grid for project and model
        const grid = document.createElement('div');
        grid.className = 'grid grid-2';
        grid.style.gap = '16px';

        // Project select
        const projectGroup = document.createElement('div');
        projectGroup.className = 'form-group';
        const projectLabel = document.createElement('label');
        projectLabel.className = 'form-label';
        projectLabel.textContent = 'Project';
        projectGroup.appendChild(projectLabel);
        const projectSelect = document.createElement('select');
        projectSelect.className = 'form-select';
        projectSelect.name = 'project_id';
        projectSelect.required = true;
        this.projects.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.name;
            projectSelect.appendChild(opt);
        });
        projectGroup.appendChild(projectSelect);
        grid.appendChild(projectGroup);

        // Model select
        const modelGroup = document.createElement('div');
        modelGroup.className = 'form-group';
        const modelLabel = document.createElement('label');
        modelLabel.className = 'form-label';
        modelLabel.textContent = 'Model';
        modelGroup.appendChild(modelLabel);
        const modelSelect = document.createElement('select');
        modelSelect.className = 'form-select';
        modelSelect.name = 'model';
        modelSelect.required = true;
        models.filter(m => !m.hidden).forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.name;
            modelSelect.appendChild(opt);
        });
        modelGroup.appendChild(modelSelect);
        grid.appendChild(modelGroup);

        form.appendChild(grid);

        // Description
        const descGroup = document.createElement('div');
        descGroup.className = 'form-group';
        const descLabel = document.createElement('label');
        descLabel.className = 'form-label';
        descLabel.textContent = 'Description (optional)';
        descGroup.appendChild(descLabel);
        const descInput = document.createElement('textarea');
        descInput.className = 'form-textarea';
        descInput.name = 'description';
        descInput.rows = 2;
        descInput.placeholder = 'Describe this batch...';
        descGroup.appendChild(descInput);
        form.appendChild(descGroup);

        // Sequences section
        const seqGroup = document.createElement('div');
        seqGroup.className = 'form-group';

        const seqHeader = document.createElement('div');
        seqHeader.style.cssText = 'display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;';
        const seqLabel = document.createElement('label');
        seqLabel.className = 'form-label';
        seqLabel.style.margin = '0';
        seqLabel.textContent = 'Sequences';
        seqHeader.appendChild(seqLabel);

        const addSeqBtn = document.createElement('button');
        addSeqBtn.type = 'button';
        addSeqBtn.className = 'btn btn-xs btn-secondary';
        addSeqBtn.textContent = '+ Add Sequence';
        seqHeader.appendChild(addSeqBtn);
        seqGroup.appendChild(seqHeader);

        const seqHint = document.createElement('p');
        seqHint.className = 'text-xs text-muted';
        seqHint.style.marginBottom = '12px';
        seqHint.textContent = 'Add protein sequences to process. You can also paste FASTA format.';
        seqGroup.appendChild(seqHint);

        const seqList = document.createElement('div');
        seqList.id = 'bunch-sequences-list';
        seqList.style.cssText = 'display: flex; flex-direction: column; gap: 8px;';
        seqGroup.appendChild(seqList);

        addSeqBtn.onclick = () => this.addSequenceRow(seqList);

        // FASTA button
        const fastaBtn = document.createElement('button');
        fastaBtn.type = 'button';
        fastaBtn.className = 'btn btn-sm btn-ghost';
        fastaBtn.style.marginTop = '12px';
        fastaBtn.textContent = 'Paste FASTA';
        fastaBtn.onclick = () => this.showFastaModal(seqList);
        seqGroup.appendChild(fastaBtn);

        form.appendChild(seqGroup);
        modalBody.appendChild(form);

        // Footer
        const footer = document.createElement('div');
        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn btn-secondary';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.onclick = () => { if (typeof Modal !== 'undefined') Modal.hide(); };
        footer.appendChild(cancelBtn);

        const createBtn = document.createElement('button');
        createBtn.className = 'btn btn-primary';
        createBtn.textContent = 'Create Batch';
        createBtn.onclick = () => this.createBunch(form, seqList);
        footer.appendChild(createBtn);

        if (typeof Modal !== 'undefined') {
            Modal.open({
                title: 'Create New Batch',
                size: 'large',
                content: '', // Content set via bodyElement/footerElement not supported by standard Modal.open?
                // Standard Modal.open takes 'content' as HTML string.
                // We need to adapt or use a custom method if Modal.open is strictly string-based.
                // Looking at app.js, Modal.open takes 'content' string and sets bodyEl.innerHTML.
                // It doesn't seem to support DOM elements directly.
                // We might need to serialize or change approach.
                // BUT, wait, app.js Modal.open sets this.bodyEl.innerHTML = content.
                // If we pass an HTML string it works.
                // If we want to append elements, we should call open then append.
            });
            // HACK: Reset body content and append elements
            // Assuming Modal.bodyEl is accessible or we can find it.
            const modalBodyEl = document.getElementById('modal-body');
            const modalFooterEl = document.getElementById('modal-footer');
            if (modalBodyEl) {
                modalBodyEl.innerHTML = '';
                modalBodyEl.appendChild(modalBody);
            }
            if (modalFooterEl) {
                // Clear existing footer buttons except standard ones?
                // Modal.open shows standard buttons. We want custom ones.
                // Let's hide standard footer and use our own in body?
                // OR replace footer content.
                modalFooterEl.innerHTML = '';
                modalFooterEl.appendChild(footer);
                modalFooterEl.style.display = 'flex';
            }
        }

        // Add initial row
        setTimeout(() => this.addSequenceRow(seqList), 100);
    },

    addSequenceRow(container) {
        const idx = container.children.length;
        const row = document.createElement('div');
        row.className = 'bunch-sequence-row';
        row.style.cssText = 'display: flex; gap: 8px; align-items: flex-start;';

        const nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.className = 'form-input';
        nameInput.name = 'item_name_' + idx;
        nameInput.placeholder = 'Name (optional)';
        nameInput.style.width = '150px';
        row.appendChild(nameInput);

        const seqInput = document.createElement('textarea');
        seqInput.className = 'form-textarea';
        seqInput.name = 'item_sequence_' + idx;
        seqInput.placeholder = 'Sequence (e.g., MKTAYIAK...)';
        seqInput.rows = 1;
        seqInput.style.cssText = 'flex: 1; min-height: 38px; resize: vertical;';
        row.appendChild(seqInput);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn btn-sm btn-ghost btn-danger';
        removeBtn.title = 'Remove';
        removeBtn.appendChild(this.getCloseIcon(14, 'currentColor'));
        removeBtn.onclick = () => row.remove();
        row.appendChild(removeBtn);

        container.appendChild(row);
    },

    showFastaModal(seqList) {
        const modalBody = document.createElement('div');
        const group = document.createElement('div');
        group.className = 'form-group';

        const label = document.createElement('label');
        label.className = 'form-label';
        label.textContent = 'FASTA Content';
        group.appendChild(label);

        const textarea = document.createElement('textarea');
        textarea.id = 'fasta-input';
        textarea.className = 'form-textarea';
        textarea.rows = 10;
        textarea.placeholder = '>Protein1\nMKTAYIAKQRQISFVK...\n\n>Protein2\nMVLSPADKTNVKAAW...';
        group.appendChild(textarea);

        modalBody.appendChild(group);

        const footer = document.createElement('div');
        const backBtn = document.createElement('button');
        backBtn.className = 'btn btn-secondary';
        backBtn.textContent = 'Cancel';
        backBtn.onclick = () => { if (typeof Modal !== 'undefined') Modal.close(); };
        footer.appendChild(backBtn);

        const applyBtn = document.createElement('button');
        applyBtn.className = 'btn btn-primary';
        applyBtn.textContent = 'Apply';
        applyBtn.onclick = () => this.applyFasta(textarea.value, seqList);
        footer.appendChild(applyBtn);

        if (typeof Modal !== 'undefined') {
            Modal.open({
                title: 'Paste FASTA Sequences',
                content: ''
            });
            const modalBodyEl = document.getElementById('modal-body');
            const modalFooterEl = document.getElementById('modal-footer');
            if (modalBodyEl) {
                modalBodyEl.innerHTML = '';
                modalBodyEl.appendChild(modalBody);
            }
            if (modalFooterEl) {
                modalFooterEl.innerHTML = '';
                modalFooterEl.appendChild(footer);
                modalFooterEl.style.display = 'flex';
            }
        }
    },

    applyFasta(fasta, seqList) {
        const sequences = [];
        const lines = fasta.split('\n');
        let currentName = '';
        let currentSeq = '';

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var trimmed = line.trim();
            if (trimmed.charAt(0) === '>') {
                if (currentSeq) {
                    sequences.push({ name: currentName, sequence: currentSeq });
                }
                currentName = trimmed.substring(1).split(/\s+/)[0];
                currentSeq = '';
            } else if (trimmed && trimmed.charAt(0) !== ';') {
                currentSeq += trimmed.toUpperCase().replace(/[^A-Z]/g, '');
            }
        }
        if (currentSeq) {
            sequences.push({ name: currentName, sequence: currentSeq });
        }

        if (sequences.length === 0) {
            if (typeof Toast !== 'undefined') Toast.error('No valid sequences found');
            return;
        }

        if (typeof Modal !== 'undefined') Modal.close();

        // Reopen create modal and populate
        var self = this;
        setTimeout(function() {
            self.showCreateModal();
            setTimeout(function() {
                var container = document.getElementById('bunch-sequences-list');
                if (container) {
                    container.textContent = '';
                    sequences.forEach(function(seq, idx) {
                        self.addSequenceRow(container);
                        var nameInput = container.querySelector('[name="item_name_' + idx + '"]');
                        var seqInput = container.querySelector('[name="item_sequence_' + idx + '"]');
                        if (nameInput) nameInput.value = seq.name;
                        if (seqInput) seqInput.value = seq.sequence;
                    });
                    if (typeof Toast !== 'undefined') Toast.success('Parsed ' + sequences.length + ' sequences');
                }
            }, 200);
        }, 100);
    },

    async createBunch(form, seqList) {
        var nameEl = form.querySelector('[name="name"]');
        var projectEl = form.querySelector('[name="project_id"]');
        var modelEl = form.querySelector('[name="model"]');
        var descEl = form.querySelector('[name="description"]');

        var name = nameEl ? nameEl.value.trim() : '';
        var projectId = projectEl ? projectEl.value : '';
        var model = modelEl ? modelEl.value : '';
        var description = descEl ? descEl.value.trim() : '';

        if (!name) {
            if (typeof Toast !== 'undefined') Toast.error('Please enter a batch name');
            return;
        }

        var items = [];
        var rows = seqList.querySelectorAll('.bunch-sequence-row');
        rows.forEach(function(row, idx) {
            var nameInput = row.querySelector('[name="item_name_' + idx + '"]');
            var seqInput = row.querySelector('[name="item_sequence_' + idx + '"]');
            var itemName = nameInput ? nameInput.value.trim() : '';
            var sequence = seqInput ? seqInput.value.trim().toUpperCase().replace(/[^A-Z]/g, '') : '';
            if (!itemName) itemName = 'Item ' + (idx + 1);
            if (sequence) {
                items.push({ name: itemName, sequence: sequence });
            }
        });

        if (items.length === 0) {
            if (typeof Toast !== 'undefined') Toast.error('Please add at least one sequence');
            return;
        }

        try {
            var response = await API.post('/api/projects/' + projectId + '/bunches', {
                name: name,
                model: model,
                description: description,
                priority: 2,
                items: items
            });

            if (response.status === 'success') {
                if (typeof Toast !== 'undefined') Toast.success('Batch created with ' + items.length + ' items');
                if (typeof Modal !== 'undefined') Modal.close();
                if (typeof Views !== 'undefined') Views.load('bunches');
            } else {
                if (typeof Toast !== 'undefined') Toast.error(response.message || 'Failed to create batch');
            }
        } catch (error) {
            console.error('Error creating bunch:', error);
            if (typeof Toast !== 'undefined') Toast.error('Failed to create batch');
        }
    },

    async startBunch(bunchId) {
        if (!confirm('Start this batch? Credits will be deducted for all items.')) return;

        try {
            var response = await API.post('/api/bunches/' + bunchId + '/start');
            if (response.status === 'success') {
                if (typeof Toast !== 'undefined') Toast.success('Batch started');
                if (typeof Views !== 'undefined') Views.load('bunches');
            } else {
                if (typeof Toast !== 'undefined') Toast.error(response.message || 'Failed to start batch');
            }
        } catch (error) {
            console.error('Error starting bunch:', error);
            if (typeof Toast !== 'undefined') Toast.error('Failed to start batch');
        }
    },

    // Route a batch to the STANDARD job view-output page instead of a
    // redundant detail popup. A batch with a single job opens that job's
    // standard /view_output page directly (via App.viewJob, the same
    // navigation the normal Jobs list uses); batches with multiple jobs
    // fall back to the standard Jobs list where each job has its own view.
    async openBunchJobs(bunchId) {
        try {
            var response = await API.get('/api/bunches/' + bunchId + '/items');
            var items = response.items || [];

            // Only items that produced an actual job can be viewed.
            var jobUuids = items
                .map(function(item) { return item.job_uuid; })
                .filter(function(uuid) { return !!uuid; });

            if (jobUuids.length === 0) {
                if (typeof Toast !== 'undefined') Toast.info('No jobs to view yet for this batch');
                return;
            }

            if (jobUuids.length === 1 && typeof App !== 'undefined' && typeof App.viewJob === 'function') {
                // Single job: go straight to its standard view-output page.
                App.viewJob(jobUuids[0]);
                return;
            }

            // Multiple jobs: open the standard Jobs list, where each batch job
            // appears with its own standard "View Output" action.
            if (typeof Views !== 'undefined') {
                Views.load('jobs');
            }
        } catch (error) {
            console.error('Error opening batch jobs:', error);
            if (typeof Toast !== 'undefined') Toast.error('Failed to open batch jobs');
        }
    }
};

// Extend Views object to include bunches render
if (typeof Views !== 'undefined') {
    console.log('BunchesModule: Extending Views object with renderBunches');
    Views.renderBunches = async function(options) {
        console.log('BunchesModule: renderBunches called');
        await window.BunchesModule.loadData();
        window.BunchesModule.render(this.contentArea);
    };
} else {
    console.warn('BunchesModule: Views object not found! renderBunches extension failed.');
}
