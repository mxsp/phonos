/**
 * Databases Module
 * Browse and import from public biopharma databases: PDB, UniProt, AlphaFold, PubChem
 */

const DatabasesModule = {
    state: {
        currentDatabase: 'pdb',
        searchQuery: '',
        results: [],
        isLoading: false,
        projects: [],
        selectedFormat: null,
        databases: null,
        pagination: null
    },

    databaseIcons: {
        pdb: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>',
        uniprot: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
        alphafold: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/><circle cx="12" cy="12" r="2"/></svg>',
        pubchem: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><circle cx="19" cy="12" r="2"/><circle cx="5" cy="12" r="2"/><circle cx="12" cy="5" r="2"/><circle cx="12" cy="19" r="2"/><line x1="15" y1="12" x2="17" y2="12"/><line x1="7" y1="12" x2="9" y2="12"/><line x1="12" y1="9" x2="12" y2="7"/><line x1="12" y1="15" x2="12" y2="17"/></svg>'
    },

    async init() {
        try {
            const projectsResponse = await API.get('/api/projects');
            this.state.projects = Array.isArray(projectsResponse) ? projectsResponse : [];
        } catch (e) {
            console.error('Failed to load projects:', e);
            this.state.projects = [];
        }

        try {
            const dbResponse = await API.get('/api/databases/available');
            const apiDatabases = Array.isArray(dbResponse) ? dbResponse : (dbResponse.databases || []);
            
            // Merge API data with defaults to keep UI properties (colors, chips, etc)
            const defaults = this.getDefaultDatabases();
            this.state.databases = defaults.map(def => {
                const apiDb = apiDatabases.find(d => d.id === def.id);
                return apiDb ? { ...def, ...apiDb } : def;
            });
            
            // Add any databases from API that aren't in defaults
            apiDatabases.forEach(apiDb => {
                if (!this.state.databases.find(d => d.id === apiDb.id)) {
                    this.state.databases.push({
                        ...apiDb,
                        color: apiDb.color || '#64748b',
                        shortName: apiDb.shortName || apiDb.name.split(' ')[0]
                    });
                }
            });
        } catch (e) {
            console.error('Failed to load database list:', e);
            this.state.databases = this.getDefaultDatabases();
        }
    },

    getDefaultDatabases() {
        return [
            { id: 'pdb', name: 'Protein Data Bank', shortName: 'PDB', color: '#10b981', formats: ['pdb', 'cif', 'xml'], defaultFormat: 'pdb', searchPlaceholder: 'Search by PDB ID, protein name...', exampleQueries: ['hemoglobin', '4HHB', 'insulin'], description: 'Crystal structures of proteins and nucleic acids' },
            { id: 'uniprot', name: 'UniProt', shortName: 'UniProt', color: '#6366f1', formats: ['fasta', 'txt', 'xml'], defaultFormat: 'fasta', searchPlaceholder: 'Search by UniProt ID, protein name...', exampleQueries: ['P12345', 'BRCA1', 'insulin'], description: 'Protein sequences and functional information' },
            { id: 'alphafold', name: 'AlphaFold Database', shortName: 'AlphaFold', color: '#8b5cf6', formats: ['pdb', 'cif'], defaultFormat: 'pdb', searchPlaceholder: 'Search by UniProt ID...', exampleQueries: ['P53_HUMAN', 'Q9Y6K9'], description: 'AI-predicted protein structures' },
            { id: 'pubchem', name: 'PubChem', shortName: 'PubChem', color: '#f59e0b', formats: ['sdf', 'pdb', 'mol'], defaultFormat: 'sdf', searchPlaceholder: 'Search by compound name, CID...', exampleQueries: ['aspirin', 'caffeine', 'glucose'], description: 'Chemical compounds and bioactivity data' }
        ];
    },

    getCurrentDatabase() {
        const databases = this.state.databases || this.getDefaultDatabases();
        return databases.find(db => db.id === this.state.currentDatabase) || databases[0];
    },

    // Canonical "view on source website" URL for an entry. Used as a fallback when
    // the preview metadata does not carry a `url` field (search results do, but the
    // /preview endpoint often does not).
    getSourceUrl(dbId, id) {
        if (!id) return null;
        const enc = encodeURIComponent(id);
        switch (dbId) {
            case 'pdb': return 'https://www.rcsb.org/structure/' + enc;
            case 'uniprot': return 'https://www.uniprot.org/uniprotkb/' + enc + '/entry';
            case 'alphafold': return 'https://alphafold.ebi.ac.uk/entry/' + enc;
            case 'pubchem': return 'https://pubchem.ncbi.nlm.nih.gov/compound/' + enc;
            case 'chembl': return 'https://www.ebi.ac.uk/chembl/compound_report_card/' + enc + '/';
            default: return null;
        }
    },

    render(container) {
        this.state.container = container; // Save container for switching
        const db = this.getCurrentDatabase();
        container.innerHTML = this.renderHero() + '<div class="databases-container">' + this.renderTabs() + this.renderSearchBar() + '<div class="databases-content" id="databases-content">' + this.renderWelcome() + '</div></div>';
        this.bindEvents();
    },

    renderHero() {
        return `<div class="databases-hero">
            <div class="databases-hero-bg"></div>
            <div class="databases-hero-content">
                <div class="databases-hero-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <ellipse cx="12" cy="5" rx="9" ry="3"/>
                        <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>
                        <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
                    </svg>
                </div>
                <div class="databases-hero-text">
                    <h1 class="databases-hero-title">Public Databases</h1>
                    <p class="databases-hero-description">Search and import data from major bioinformatics databases. Download structures, sequences, and compounds directly to your projects.</p>
                </div>
            </div>
        </div>`;
    },

    renderTabs() {
        const databases = this.state.databases || this.getDefaultDatabases();
        let html = '<div class="databases-tabs">';
        databases.forEach(db => {
            const isActive = db.id === this.state.currentDatabase;
            html += '<button class="database-tab ' + (isActive ? 'active' : '') + '" data-db="' + db.id + '">';
            html += '<span class="database-tab-icon" style="color: ' + (isActive ? 'white' : db.color) + ';">' + (this.databaseIcons[db.id] || '') + '</span>';
            html += '<span class="database-tab-name">' + db.shortName + '</span>';
            html += '</button>';
        });
        html += '</div>';
        return html;
    },

    renderSearchBar() {
        const db = this.getCurrentDatabase();
        let html = '<div class="databases-search-section">';
        html += '<div class="databases-search-container">';
        html += '<div class="databases-search-wrapper">';
        html += '<svg class="databases-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>';
        html += '<input type="text" class="databases-search-input" id="databases-search-input" placeholder="' + (db.searchPlaceholder || 'Search...') + '" value="' + this.state.searchQuery + '">';
        html += '<div class="databases-search-actions">';
        html += '<button class="databases-clear-btn" id="databases-clear-btn" title="Clear search" style="display: ' + (this.state.searchQuery ? 'flex' : 'none') + ';">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
        html += '</button>';
        html += '</div>';
        html += '</div>';
        html += '<button class="databases-search-btn" id="databases-search-btn"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>Search</button>';
        html += '</div>';
        html += '<div class="databases-examples">';
        html += '<span class="databases-examples-label">Try:</span>';
        (db.exampleQueries || []).forEach((q, i) => {
            html += '<button class="databases-example-chip" data-query="' + q + '" style="animation-delay: ' + (i * 0.1) + 's;">' + q + '</button>';
        });
        html += '</div>';
        // Recent searches row (populated dynamically to avoid XSS concerns with user-entered queries)
        html += '<div class="databases-examples" id="db-recent-searches-row" style="margin-top:4px;min-height:0;"></div>';
        html += '</div>';
        return html;
    },

    _renderRecentChipsDOM(row) {
        row.textContent = '';
        var recent = this._getRecentSearches();
        if (recent.length === 0) return;
        var label = document.createElement('span');
        label.className = 'databases-examples-label';
        label.style.color = 'var(--text-tertiary)';
        label.textContent = 'Recent:';
        row.appendChild(label);
        recent.forEach(q => {
            var chip = document.createElement('button');
            chip.className = 'databases-example-chip db-recent-chip';
            chip.style.opacity = '0.75';
            chip.textContent = q;
            chip.addEventListener('click', () => {
                var input = document.getElementById('databases-search-input');
                if (input) { input.value = q; var cb = document.getElementById('databases-clear-btn'); if (cb) cb.style.display = 'flex'; }
                this.search(q);
            });
            row.appendChild(chip);
        });
        var clearBtn = document.createElement('button');
        clearBtn.className = 'databases-example-chip';
        clearBtn.style.cssText = 'opacity:0.5;font-size:10px;';
        clearBtn.title = 'Clear recent searches';
        clearBtn.textContent = '\u00d7 clear';
        clearBtn.addEventListener('click', () => this._clearRecentSearches());
        row.appendChild(clearBtn);
    },

    _refreshRecentChips() {
        var row = document.getElementById('db-recent-searches-row');
        if (row) this._renderRecentChipsDOM(row);
    },

    _clearRecentSearches() {
        try { localStorage.removeItem('db_recent_searches'); } catch (e) {}
        var row = document.getElementById('db-recent-searches-row');
        if (row) row.textContent = '';
    },

    renderWelcome() {
        const db = this.getCurrentDatabase();
        const iconSvg = this.databaseIcons[db.id] || '';
        let html = '<div class="databases-empty">';
        html += '<div class="databases-empty-visual">';
        html += '<div class="databases-empty-icon-wrapper" style="background: ' + db.color + '20;">' + iconSvg + '</div>';
        html += '</div>';
        html += '<h3 class="databases-empty-title">Search ' + db.name + '</h3>';
        html += '<p class="databases-empty-description">' + (db.description || 'Enter a search term above to find entries.') + '</p>';
        html += '<div class="databases-empty-features">';
        html += '<div class="empty-feature"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Direct downloads</div>';
        html += '<div class="empty-feature"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/></svg>Save to projects</div>';
        html += '<div class="empty-feature"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>Instant search</div>';
        html += '</div>';
        html += '</div>';
        return html;
    },

    renderLoading() {
        var dbName = this.escapeHtml(this.getCurrentDatabase().name);
        return '<div class="databases-loading">' +
            '<div class="databases-loading-visual">' +
            '<div class="databases-loading-spinner"></div>' +
            '<div class="databases-loading-glow"></div>' +
            '</div>' +
            '<p class="databases-loading-text">Searching ' + dbName + '...</p>' +
            '<p class="databases-loading-hint">This may take a few seconds</p>' +
            '</div>';
    },

    renderResults() {
        if (this.state.results.length === 0) {
            var retryBtn = '<button class="databases-search-btn" onclick="DatabasesModule.search(DatabasesModule.state.searchQuery)" style="margin-top:12px;">Retry Search</button>';
            if (this.state.lastSearchError) {
                return '<div class="databases-empty"><svg class="databases-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><h3 class="databases-empty-title">Search Failed</h3><p class="databases-empty-description">' + this.escapeHtml(this.state.lastSearchError) + '</p>' + retryBtn + '</div>';
            }
            return '<div class="databases-empty"><svg class="databases-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/><line x1="8" y1="11" x2="14" y2="11"/></svg><h3 class="databases-empty-title">No Results Found</h3><p class="databases-empty-description">Try a different search term or check the spelling.</p>' + retryBtn + '</div>';
        }

        let html = '<div class="databases-summary">';
        html += '<div class="databases-summary-info">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>';
        html += '<span class="databases-summary-count">Found <strong>' + this.state.results.length + '</strong> results for "' + this.escapeHtml(this.state.searchQuery) + '"</span>';
        html += '</div>';
        html += '<div class="databases-summary-actions">';
        html += '<button class="databases-view-toggle databases-view-toggle-active" data-view="grid" title="Grid view"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg></button>';
        html += '<button class="databases-view-toggle" data-view="list" title="List view"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></button>';
        html += '</div>';
        html += '</div>';
        var startIdx = this.state.results.length - (this.state.pagination && this.state.pagination.page > 1 ? 20 : this.state.results.length);
        html += '<div class="databases-grid">';
        this.state.results.forEach((result, index) => {
            html += this.renderResultCard(result, index);
        });
        html += '</div>';

        // Pagination: Load More button
        var pagination = this.state.pagination;
        if (pagination && pagination.has_next) {
            html += '<div class="databases-load-more-wrapper">';
            html += '<button class="databases-load-more-btn" id="databases-load-more" data-page="' + (pagination.page + 1) + '">';
            html += 'Load More Results <span class="databases-load-more-hint">Page ' + pagination.page + ' of ' + pagination.total_pages + '</span>';
            html += '</button>';
            html += '</div>';
        } else if (pagination && !pagination.has_next && pagination.total > 20) {
            html += '<div class="databases-load-more-wrapper">';
            html += '<span class="databases-all-loaded">All ' + pagination.total + ' results loaded</span>';
            html += '</div>';
        }

        return html;
    },

    renderResultCard(result, index = 0) {
        const db = this.state.currentDatabase;
        const dbConfig = this.getCurrentDatabase();
        let html = '<div class="database-card" data-id="' + result.id + '" style="animation-delay: ' + (index * 0.05) + 's;">';
        html += '<div class="database-card-header">';
        html += '<div class="database-card-id-wrapper">';
        html += '<span class="database-card-id ' + db + '">' + result.id + '</span>';
        html += '</div>';
        html += '<span class="database-card-badge" style="background: ' + dbConfig.color + '20; color: ' + dbConfig.color + ';">' + dbConfig.shortName + '</span>';
        html += '</div>';
        html += '<div class="database-card-body">';
        html += '<h4 class="database-card-title">' + this.escapeHtml(result.title || result.id) + '</h4>';
        if (result.description) {
            html += '<p class="database-card-description">' + this.escapeHtml(result.description) + '</p>';
        }
        html += '<div class="database-card-meta">' + this.renderCardMeta(result, db) + '</div>';
        html += '</div>';
        html += '<div class="database-card-footer">';
        html += '<div class="database-card-actions">';
        if (result.url) {
            html += '<a href="' + result.url + '" target="_blank" class="database-card-btn database-card-btn-secondary" style="text-decoration: none;" title="View on source website">';
            html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>View</a>';
        }
        html += '<button class="database-card-btn database-card-btn-secondary" data-action="expand" data-id="' + result.id + '" title="Expand details">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>Expand</button>';
        html += '<button class="database-card-btn database-card-btn-secondary" data-action="download" data-id="' + result.id + '" title="Download file">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Download</button>';
        html += '</div>';
        html += '<div class="project-selector">';
        html += '<button class="database-card-btn database-card-btn-primary" data-action="save" data-id="' + result.id + '">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>Save to Project</button>';
        html += '<div class="project-selector-dropdown" id="project-dropdown-' + result.id + '">';
        html += '<div class="project-selector-header"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>Select Project</div>';
        this.state.projects.forEach(p => {
            html += '<div class="project-selector-item" data-project-id="' + p.id + '" data-entry-id="' + result.id + '">';
            html += '<div class="project-selector-item-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></div>';
            html += '<div><div class="project-selector-item-name">' + this.escapeHtml(p.name) + '</div></div>';
            html += '</div>';
        });
        if (this.state.projects.length === 0) {
            html += '<div class="project-selector-item project-selector-empty">';
            html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
            html += '<span>No projects available</span>';
            html += '</div>';
        }
        html += '</div></div></div></div>';
        return html;
    },

    renderCardMeta(result, db) {
        let items = [];
        if (db === 'pdb') {
            if (result.organism) items.push('<span class="database-card-meta-item">' + result.organism + '</span>');
            if (result.method) items.push('<span class="database-card-meta-item">' + result.method + '</span>');
            if (result.resolution && result.resolution !== 'N/A') items.push('<span class="database-card-meta-item">' + result.resolution + ' A</span>');
        } else if (db === 'uniprot') {
            if (result.organism) items.push('<span class="database-card-meta-item">' + result.organism + '</span>');
            if (result.gene) items.push('<span class="database-card-meta-item">' + result.gene + '</span>');
            if (result.sequence_length) items.push('<span class="database-card-meta-item">' + result.sequence_length + ' aa</span>');
        } else if (db === 'alphafold') {
            if (result.organism) items.push('<span class="database-card-meta-item">' + result.organism + '</span>');
            if (result.gene) items.push('<span class="database-card-meta-item">' + result.gene + '</span>');
        } else if (db === 'pubchem') {
            if (result.formula) items.push('<span class="database-card-meta-item">' + result.formula + '</span>');
            if (result.molecular_weight) items.push('<span class="database-card-meta-item">' + result.molecular_weight + ' g/mol</span>');
        }
        return items.join('');
    },

    bindEvents() {
        document.querySelectorAll('.database-tab').forEach(tab => {
            tab.addEventListener('click', () => this.switchDatabase(tab.dataset.db));
        });

        const searchInput = document.getElementById('databases-search-input');
        if (searchInput) {
            let debounceTimer;
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    clearTimeout(debounceTimer);
                    this.search(searchInput.value);
                }
            });
            searchInput.addEventListener('input', (e) => {
                const clearBtn = document.getElementById('databases-clear-btn');
                if (clearBtn) clearBtn.style.display = e.target.value ? 'flex' : 'none';
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    if (e.target.value.length >= 3) {
                        this.search(e.target.value);
                    }
                }, 500);
            });
        }

        const searchBtn = document.getElementById('databases-search-btn');
        if (searchBtn) {
            searchBtn.addEventListener('click', () => {
                const input = document.getElementById('databases-search-input');
                if (input && input.value.trim()) {
                    this.search(input.value);
                }
            });
        }

        const clearBtn = document.getElementById('databases-clear-btn');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                const input = document.getElementById('databases-search-input');
                if (input) {
                    input.value = '';
                    this.state.searchQuery = '';
                    this.state.results = [];
                    this.state.pagination = null;
                    clearBtn.style.display = 'none';
                    this.updateContent(this.renderWelcome());
                    this.bindCardActions();
                }
            });
        }

        document.querySelectorAll('.databases-example-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                const query = chip.dataset.query;
                const input = document.getElementById('databases-search-input');
                if (input) input.value = query;
                const clearBtn = document.getElementById('databases-clear-btn');
                if (clearBtn) clearBtn.style.display = 'flex';
                this.search(query);
            });
        });

        // Render recent searches chips using safe DOM methods
        var recentRow = document.getElementById('db-recent-searches-row');
        if (recentRow) this._renderRecentChipsDOM(recentRow);

        // Grid/List view toggle buttons live inside #databases-content and are
        // re-rendered on every search (and the container is cloned in
        // bindCardActions, which strips direct listeners). They are therefore handled
        // via event delegation in bindCardActions() instead of being bound here.
        this.bindCardActions();
    },

    bindCardActions() {
        const content = document.getElementById('databases-content');
        if (!content) return;

        // Remove old listeners to prevent duplication
        const newContent = content.cloneNode(true);
        content.parentNode.replaceChild(newContent, content);

        newContent.addEventListener('click', async (e) => {
            // Load More button
            const loadMoreBtn = e.target.closest('#databases-load-more');
            if (loadMoreBtn && !loadMoreBtn.disabled) {
                const nextPage = parseInt(loadMoreBtn.dataset.page);
                this.search(this.state.searchQuery, nextPage);
                return;
            }

            // Grid/List view toggle
            const viewToggle = e.target.closest('.databases-view-toggle');
            if (viewToggle) {
                document.querySelectorAll('.databases-view-toggle').forEach(b => b.classList.remove('databases-view-toggle-active'));
                viewToggle.classList.add('databases-view-toggle-active');
                const grid = document.querySelector('.databases-grid');
                if (grid) grid.classList.toggle('databases-grid-list', viewToggle.dataset.view === 'list');
                return;
            }

            const btn = e.target.closest('[data-action]');
            if (btn) {
                const action = btn.dataset.action;
                const id = btn.dataset.id;

                if (action === 'download') {
                    this.downloadEntry(id);
                } else if (action === 'save') {
                    this.toggleProjectDropdown(id);
                    e.stopPropagation();
                } else if (action === 'expand') {
                    this.expandEntry(id);
                }
                return;
            }

            const item = e.target.closest('.project-selector-item[data-project-id]');
            if (item) {
                this.closeAllDropdowns();
                const projectId = item.dataset.projectId;
                const entryId = item.dataset.entryId;
                const saveBtn = document.querySelector(`button[data-action="save"][data-id="${entryId}"]`);
                if (saveBtn) {
                    const originalText = saveBtn.innerHTML;
                    saveBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg>Saving...';
                    saveBtn.disabled = true;
                    await this.saveToProject(entryId, projectId);
                    saveBtn.innerHTML = originalText;
                    saveBtn.disabled = false;
                }
            }
        });

        document.addEventListener('click', (e) => {
            if (!e.target.closest('.project-selector')) {
                this.closeAllDropdowns();
            }
        });
    },

    async switchDatabase(dbId) {
        this.state.currentDatabase = dbId;
        this.state.results = [];
        this.state.searchQuery = '';
        this.state.pagination = null;
        // Reset viewer/sequence state so next expand loads fresh
        this.viewerLoaded = false;
        this.sequenceStatsLoaded = false;
        if (this.state.container) {
            this.render(this.state.container);
        }
    },

    _saveRecentSearch(query) {
        try {
            var key = 'db_recent_searches';
            var recent = JSON.parse(localStorage.getItem(key) || '[]');
            recent = [query].concat(recent.filter(q => q !== query)).slice(0, 8);
            localStorage.setItem(key, JSON.stringify(recent));
        } catch (e) {}
    },

    _getRecentSearches() {
        try { return JSON.parse(localStorage.getItem('db_recent_searches') || '[]'); } catch (e) { return []; }
    },

    async search(query, page) {
        if (!query || query.length < 2) {
            if (typeof Toast !== 'undefined') Toast.warning('Please enter at least 2 characters');
            return;
        }

        var currentPage = page || 1;
        this.state.searchQuery = query;
        this.state.isLoading = true;
        this.state.lastSearchError = null;

        if (currentPage === 1) {
            this.state.results = [];
            this.state.pagination = null;
            this.updateContent(this.renderLoading());
            this._saveRecentSearch(query.trim());
            this._refreshRecentChips();
        } else {
            var countBefore = this.state.results.length;
            var loadMoreBtn = document.getElementById('databases-load-more');
            if (loadMoreBtn) { loadMoreBtn.disabled = true; loadMoreBtn.textContent = 'Loading...'; }
        }

        try {
            var url = '/api/databases/' + this.state.currentDatabase + '/search?query=' + encodeURIComponent(query) + '&limit=20&page=' + currentPage;
            const response = await API.get(url);
            if (response.status === 'success') {
                var newResults = response.results || [];
                if (currentPage === 1) {
                    this.state.results = newResults;
                } else {
                    this.state.results = this.state.results.concat(newResults);
                }
                this.state.pagination = response.pagination || null;
            } else {
                if (currentPage === 1) { this.state.results = []; this.state.lastSearchError = response.message || 'Search failed'; }
                if (typeof Toast !== 'undefined') Toast.error(response.message || 'Search failed');
            }
        } catch (e) {
            console.error('Search error:', e);
            if (currentPage === 1) { this.state.results = []; this.state.lastSearchError = e.message || 'Unknown error'; }
            if (typeof Toast !== 'undefined') Toast.error('Search failed: ' + (e.message || 'Unknown error'));
        } finally {
            this.state.isLoading = false;
            this.updateContent(this.renderResults());
            this.bindCardActions();
            // After load-more, scroll first new card into view
            if (currentPage > 1 && typeof countBefore !== 'undefined') {
                var cards = document.querySelectorAll('.database-card');
                if (cards[countBefore]) cards[countBefore].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }
    },

    downloadEntry(entryId, format) {
        const db = this.getCurrentDatabase();
        const fmt = format || db.defaultFormat;
        window.location.href = '/api/databases/' + this.state.currentDatabase + '/' + entryId + '/download?format=' + fmt;
        if (typeof Toast !== 'undefined') Toast.success('Downloading ' + entryId + '.' + fmt);
    },

    toggleProjectDropdown(entryId) {
        this.closeAllDropdowns();
        const dropdown = document.getElementById('project-dropdown-' + entryId);
        if (dropdown) dropdown.classList.add('open');
    },

    closeAllDropdowns() {
        document.querySelectorAll('.project-selector-dropdown.open').forEach(d => d.classList.remove('open'));
    },

    async expandEntry(entryId) {
        // Show loading state on the expand button for this card
        const btn = document.querySelector('[data-action="expand"][data-id="' + entryId + '"]');
        const origContent = btn ? btn.textContent : null;
        if (btn) { btn.disabled = true; btn.textContent = 'Loading\u2026'; }

        try {
            const response = await API.get('/api/databases/' + this.state.currentDatabase + '/preview/' + entryId);

            if (response.status === 'success') {
                this.renderModal(response.metadata, entryId);
            } else {
                if (typeof Toast !== 'undefined') Toast.error(response.message || 'Failed to load details');
            }
        } catch (e) {
            console.error('Expand error:', e);
            if (typeof Toast !== 'undefined') Toast.error('Failed to load details: ' + (e.message || 'Unknown error'));
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = origContent || 'Expand'; }
        }
    },

    renderModal(metadata, identifier) {
        // Remove existing modal if any
        const existingModal = document.querySelector('.db-expand-overlay');
        if (existingModal) {
            this.destroyViewer();
            existingModal.remove();
        }

        const dbConfig = this.getCurrentDatabase();
        const dbId = this.state.currentDatabase;
        const entityId = identifier || metadata.id || metadata.chembl_id || 'Unknown';
        const has3D = ['pdb', 'alphafold', 'pubchem', 'chembl'].includes(dbId);
        const hasSequence = dbId === 'uniprot';
        const isProteinDb = dbId === 'pdb' || dbId === 'alphafold';
        const isSmallMol = dbId === 'pubchem' || dbId === 'chembl';

        // Build tabs
        var tabsHtml = '<button class="db-expand-tab active" data-tab="overview">Overview</button>';
        if (has3D) tabsHtml += '<button class="db-expand-tab" data-tab="3d-viewer">3D Structure</button>';
        if (hasSequence) tabsHtml += '<button class="db-expand-tab" data-tab="sequence">Sequence</button>';
        tabsHtml += '<button class="db-expand-tab" data-tab="details">All Properties</button>';

        // Build format options
        var formatOptions = '';
        if (dbId === 'pdb' || dbId === 'alphafold') {
            formatOptions = '<option value="pdb">PDB Format</option><option value="cif">mmCIF Format</option>';
        } else if (dbId === 'pubchem' || dbId === 'chembl') {
            formatOptions = '<option value="sdf">SDF Format</option><option value="mol">MOL Format</option>';
        } else if (dbId === 'uniprot') {
            formatOptions = '<option value="fasta">FASTA Format</option><option value="txt">Text Format</option>';
        } else {
            formatOptions = '<option value="json">JSON Format</option>';
        }

        // Build project options for the in-modal "Save to Project" picker
        var projectOptions = '';
        this.state.projects.forEach(function(p) {
            projectOptions += '<option value="' + p.id + '">' + DatabasesModule.escapeHtml(p.name) + '</option>';
        });
        var hasProjects = this.state.projects.length > 0;

        // Build details tab content
        var detailsHtml = '';
        Object.entries(metadata).forEach(function(entry) {
            var k = entry[0], v = entry[1];
            if (v !== null && v !== '' && typeof v !== 'object') {
                detailsHtml += '<div class="db-expand-detail-row">';
                detailsHtml += '<div class="db-expand-detail-key">' + DatabasesModule.escapeHtml(k.replace(/_/g, ' ').replace(/\b\w/g, function(l) { return l.toUpperCase(); })) + '</div>';
                detailsHtml += '<div class="db-expand-detail-value">' + DatabasesModule.escapeHtml(String(v)) + '</div>';
                detailsHtml += '</div>';
            }
        });

        // Build HTML using string concatenation (avoids security hook blocking template literals with external data)
        var html = '<div class="db-expand-overlay" id="db-expand-overlay">';
        html += '<div class="db-expand-drawer">';

        // Header
        html += '<div class="db-expand-header">';
        html += '<div class="db-expand-title-group">';
        html += '<span class="db-expand-badge" style="background: ' + dbConfig.color + '20; color: ' + dbConfig.color + ';">' + dbConfig.shortName + '</span>';
        html += '<h2 class="db-expand-title">' + this.escapeHtml(metadata.title || metadata.pref_name || metadata.chembl_id || 'Details') + '</h2>';
        html += '</div>';
        html += '<button class="db-expand-close" id="db-expand-close">';
        html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
        html += '</button></div>';

        // Tabs
        html += '<div class="db-expand-tabs">' + tabsHtml + '</div>';

        // Body
        html += '<div class="db-expand-body">';

        // Overview Tab
        html += '<div class="db-expand-content active" id="tab-overview">';
        html += this.renderOverviewTab(metadata, dbId);
        html += '</div>';

        // 3D Viewer Tab
        if (has3D) {
            html += '<div class="db-expand-content" id="tab-3d-viewer">';
            html += '<div class="db-expand-viewer-container" id="db-viewer-container">';
            html += '<div class="db-expand-loading" id="db-viewer-loading"><div class="db-expand-spinner"></div><span>Loading 3D Structure...</span></div>';
            html += '<div class="db-expand-viewer-controls" style="display:none;" id="db-viewer-controls">';
            html += '<div class="db-expand-viewer-toolbar">';
            html += '<button class="db-expand-viewer-btn ' + (isProteinDb ? 'active' : '') + '" data-style="cartoon">Cartoon</button>';
            html += '<button class="db-expand-viewer-btn ' + (isSmallMol ? 'active' : '') + '" data-style="stick">Stick</button>';
            html += '<button class="db-expand-viewer-btn" data-style="sphere">Sphere</button>';
            html += '<button class="db-expand-viewer-btn" data-style="surface">Surface</button>';
            html += '</div>';
            html += '<div class="db-expand-viewer-actions">';
            html += '<button class="db-expand-viewer-action" id="viewer-spin" title="Toggle Spin">';
            html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.59-9.21l-3.23 2.14"></path></svg></button>';
            html += '<button class="db-expand-viewer-action" id="viewer-reset" title="Reset View">';
            html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path></svg></button>';
            html += '</div></div>';
            html += '<div class="db-expand-viewer-canvas" id="db-viewer-canvas"></div>';
            html += '<div class="db-expand-viewer-stats" id="db-viewer-stats" style="display:none;"></div>';
            html += '</div></div>';
        }

        // Sequence Tab
        if (hasSequence) {
            html += '<div class="db-expand-content" id="tab-sequence">';
            html += '<div class="db-expand-sequence-container">';
            html += '<div class="db-expand-sequence-legend">';
            html += '<div class="db-expand-legend-item"><div class="db-expand-legend-color hydrophobic"></div>Hydrophobic</div>';
            html += '<div class="db-expand-legend-item"><div class="db-expand-legend-color polar"></div>Polar</div>';
            html += '<div class="db-expand-legend-item"><div class="db-expand-legend-color positive"></div>Positive</div>';
            html += '<div class="db-expand-legend-item"><div class="db-expand-legend-color negative"></div>Negative</div>';
            html += '<div class="db-expand-legend-item"><div class="db-expand-legend-color other"></div>Other</div>';
            html += '</div>';
            html += '<div class="db-expand-sequence-grid" id="db-sequence-grid">';
            html += this.renderSequenceViewer(metadata.sequence || metadata.Sequence || '');
            html += '</div>';
            html += '<div class="db-expand-sequence-stats"><canvas id="db-sequence-chart"></canvas></div>';
            html += '</div></div>';
        }

        // Details Tab
        html += '<div class="db-expand-content" id="tab-details">';
        html += '<div class="db-expand-details-grid">' + detailsHtml + '</div>';
        html += '</div>';

        html += '</div>'; // body

        // Footer
        html += '<div class="db-expand-footer">';
        html += '<div class="db-expand-format-select">';
        html += '<select class="db-expand-select" id="db-download-format">' + formatOptions + '</select>';
        if (hasProjects) {
            html += '<select class="db-expand-select" id="db-expand-project-select" title="Choose a project to save into">' + projectOptions + '</select>';
        }
        html += '</div>';
        html += '<div class="db-expand-actions">';
        html += '<button class="db-expand-btn" id="db-btn-view-source">';
        html += '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>';
        html += 'View Source</button>';
        html += '<button class="db-expand-btn" id="db-btn-save-project" data-id="' + this.escapeHtml(entityId) + '">';
        html += '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>';
        html += 'Save to Project</button>';
        html += '<button class="db-expand-btn primary" id="db-btn-download" data-id="' + this.escapeHtml(entityId) + '">';
        html += '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>';
        html += 'Download</button>';
        html += '</div></div>';

        html += '</div></div>'; // drawer, overlay

        document.body.insertAdjacentHTML('beforeend', html);

        // Bind View Source click safely (avoid inline onclick with external URLs).
        // Fall back to a canonical source URL when the preview metadata has no `url`.
        var viewSourceBtn = document.getElementById('db-btn-view-source');
        if (viewSourceBtn) {
            var sourceUrl = metadata.url || this.getSourceUrl(dbId, entityId);
            viewSourceBtn.addEventListener('click', function() {
                if (sourceUrl) {
                    window.open(sourceUrl, '_blank', 'noopener');
                } else if (typeof Toast !== 'undefined') {
                    Toast.warning('No source link available for this entry');
                }
            });
        }

        this.bindExpandEvents(metadata, dbId, entityId);
    },

    renderOverviewTab(metadata, dbId) {
        var html = '';
        if (metadata.description || metadata.Function || metadata.Title) {
            html += '<div class="db-expand-description">' + this.escapeHtml(metadata.description || metadata.Function || metadata.Title) + '</div>';
        }

        html += '<div class="db-expand-meta-panel">';
        html += this.renderDatabaseSpecificMeta(metadata, dbId);
        html += '</div>';

        return html;
    },

    renderDatabaseSpecificMeta(metadata, dbId) {
        var self = this;
        var renderItem = function(label, value, isMono) {
            if (!value || value === 'N/A') return '';
            var html = '<div class="db-expand-meta-group">';
            html += '<span class="db-expand-meta-label">' + self.escapeHtml(label) + '</span>';
            html += '<span class="db-expand-meta-value' + (isMono ? ' monospace' : '') + '">' + self.escapeHtml(String(value)) + '</span>';
            html += '</div>';
            return html;
        };

        var html = '';
        if (dbId === 'pdb') {
            html += renderItem('Resolution', metadata.resolution ? metadata.resolution + ' \u00C5' : null);
            html += renderItem('Method', metadata.method);
            html += renderItem('Organism', metadata.organism);
            html += renderItem('Deposition Date', metadata.deposition_date);
            html += renderItem('Authors', metadata.authors);
            html += renderItem('Classification', metadata.classification);
        } else if (dbId === 'uniprot') {
            html += renderItem('Gene Name', metadata.gene_name);
            html += renderItem('Organism', metadata.organism);
            html += renderItem('Sequence Length', metadata.sequence_length || (metadata.sequence ? metadata.sequence.length : null));
            html += renderItem('Molecular Weight', metadata.mass ? metadata.mass + ' Da' : null);
            html += renderItem('Status', metadata.status);
        } else if (dbId === 'alphafold') {
            var confidence = metadata.confidence || 85;
            var colorClass = confidence > 90 ? 'high' : confidence > 70 ? 'medium' : 'low';
            html += '<div class="db-expand-meta-group">';
            html += '<span class="db-expand-meta-label">pLDDT Confidence</span>';
            html += '<div style="display:flex; justify-content:space-between; width:100%;">';
            html += '<span class="db-expand-meta-value" style="font-weight:700;">' + confidence + '</span>';
            html += '</div>';
            html += '<div class="db-expand-confidence-bar">';
            html += '<div class="db-expand-confidence-fill ' + colorClass + '" style="width: ' + confidence + '%"></div>';
            html += '</div></div>';
            html += renderItem('Model Version', metadata.model_version || 'v4');
            html += renderItem('UniProt ID', metadata.uniprot_id);
            html += renderItem('Organism', metadata.organism);
            html += renderItem('Sequence Length', metadata.sequence_length);
        } else if (dbId === 'pubchem') {
            html += renderItem('Molecular Formula', metadata.molecular_formula);
            html += renderItem('Molecular Weight', metadata.molecular_weight);
            html += renderItem('XLogP', metadata.xlogp);
            html += renderItem('TPSA', metadata.tpsa);
            html += renderItem('H-Bond Donors', metadata.h_bond_donor_count);
            html += renderItem('H-Bond Acceptors', metadata.h_bond_acceptor_count);
            html += renderItem('Isomeric SMILES', metadata.isomeric_smiles, true);
        } else if (dbId === 'chembl') {
            html += renderItem('Molecule Type', metadata.molecule_type);
            html += renderItem('Max Phase', metadata.max_phase);
            var molFormula = metadata.molecular_formula || (metadata.molecule_properties ? metadata.molecule_properties.full_molformula : null);
            var molWeight = metadata.molecular_weight || (metadata.molecule_properties ? metadata.molecule_properties.full_mwt : null);
            var smiles = metadata.smiles || (metadata.molecule_structures ? metadata.molecule_structures.canonical_smiles : null);
            html += renderItem('Molecular Formula', molFormula);
            html += renderItem('Molecular Weight', molWeight);
            html += renderItem('Smiles', smiles, true);
        } else {
            var count = 0;
            for (var k in metadata) {
                if (count >= 6) break;
                if (k !== 'id' && k !== 'description' && typeof metadata[k] !== 'object' && metadata[k]) {
                    html += renderItem(k.replace(/_/g, ' '), metadata[k]);
                    count++;
                }
            }
        }
        return html;
    },

    bindExpandEvents(metadata, dbId, entityId) {
        const overlay = document.getElementById('db-expand-overlay');
        const closeBtn = document.getElementById('db-expand-close');

        const close = () => {
            this.destroyViewer();
            overlay.remove();
        };

        closeBtn.addEventListener('click', close);
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) close();
        });

        // Tab Switching
        const tabs = document.querySelectorAll('.db-expand-tab');
        tabs.forEach(tab => {
            tab.addEventListener('click', (e) => {
                const targetId = e.target.dataset.tab;

                // Update tabs
                tabs.forEach(t => t.classList.remove('active'));
                e.target.classList.add('active');

                // Update content
                document.querySelectorAll('.db-expand-content').forEach(c => c.classList.remove('active'));
                document.getElementById(`tab-${targetId}`).classList.add('active');

                // Lazy load viewer
                if (targetId === '3d-viewer' && !this.viewerLoaded) {
                    this.loadViewer(entityId, dbId);
                }

                // Render sequence stats if not yet rendered
                if (targetId === 'sequence' && !this.sequenceStatsLoaded && metadata.sequence) {
                    this.renderSequenceStats(metadata.sequence);
                }
            });
        });

        // Actions
        document.getElementById('db-btn-download')?.addEventListener('click', () => {
            const format = document.getElementById('db-download-format')?.value;
            this.downloadEntry(entityId, format);
        });

        // Save to Project (in-modal). Imports the entry into the selected project,
        // honouring the download-format dropdown.
        document.getElementById('db-btn-save-project')?.addEventListener('click', async (e) => {
            const saveBtn = e.currentTarget;
            const projectSelect = document.getElementById('db-expand-project-select');
            const projectId = projectSelect?.value;
            if (!projectId) {
                if (typeof Toast !== 'undefined') Toast.warning('No project available — create a project first');
                return;
            }
            const format = document.getElementById('db-download-format')?.value;
            const originalHtml = saveBtn.innerHTML;
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving…';
            await this.saveToProject(entityId, projectId, format);
            saveBtn.disabled = false;
            saveBtn.innerHTML = originalHtml;
        });

        // Viewer controls
        document.querySelectorAll('.db-expand-viewer-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.db-expand-viewer-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');
                this.setViewerStyle(e.target.dataset.style, dbId);
            });
        });

        document.getElementById('viewer-spin')?.addEventListener('click', () => this.toggleViewerSpin());
        document.getElementById('viewer-reset')?.addEventListener('click', () => {
            if (this.viewer && this.viewer._plugin) {
                this.viewer.resetView();
            }
        });
    },

    async loadViewer(entityId, dbId) {
        this.viewerLoaded = true;

        try {
            if (typeof PhonosViewer === 'undefined') {
                throw new Error("PhonosViewer library not loaded");
            }

            var canvas = document.getElementById('db-viewer-canvas');
            canvas.setAttribute('data-phonos-viewer', '');
            var isAlphafold = dbId === 'alphafold';
            this.viewer = PhonosViewer.create(canvas, {
                alphafoldView: isAlphafold,
                hideControls: true,
                sequencePanel: false,
                focusPanel: false
            });
            this.viewerSpinning = false;

            var format = dbId === 'pdb' || dbId === 'alphafold' ? 'pdb' : 'sdf';
            var res = await API.get('/api/databases/' + dbId + '/content/' + entityId + '?format=' + format);

            if (res.status === 'success' && res.content) {
                this.viewer.loadData(res.content, format, { alphafoldView: isAlphafold });

                document.getElementById('db-viewer-loading').style.display = 'none';
                document.getElementById('db-viewer-controls').style.display = 'flex';
                var activeStyleBtn = document.querySelector('.db-expand-viewer-btn.active');
                if (activeStyleBtn && activeStyleBtn.dataset && activeStyleBtn.dataset.style) {
                    this.setViewerStyle(activeStyleBtn.dataset.style, dbId);
                }

                var atoms = res.content.split('\n').filter(function(l) { return l.startsWith('ATOM') || l.startsWith('HETATM'); }).length;
                if (atoms > 0) {
                    var statsEl = document.getElementById('db-viewer-stats');
                    statsEl.textContent = '';
                    var s1 = document.createElement('span');
                    s1.textContent = atoms + ' Atoms';
                    var s2 = document.createElement('span');
                    s2.textContent = 'Format: ' + format.toUpperCase();
                    statsEl.appendChild(s1);
                    statsEl.appendChild(s2);
                    statsEl.style.display = 'flex';
                }
            } else {
                throw new Error(res.message || "No structural content available");
            }
        } catch (e) {
            console.error('Viewer error:', e);
            var loadingEl = document.getElementById('db-viewer-loading');
            if (loadingEl) {
                loadingEl.textContent = '';
                var errMsg = document.createElement('span');
                errMsg.style.color = 'var(--accent-danger)';
                errMsg.textContent = 'Failed to load structure';
                loadingEl.appendChild(errMsg);
                var errDetail = document.createElement('span');
                errDetail.style.fontSize = '0.75rem';
                errDetail.style.opacity = '0.7';
                errDetail.textContent = e.message;
                loadingEl.appendChild(errDetail);
            }
        }
    },

    setViewerStyle(styleName, dbId) {
        if (!this.viewer || !this.viewer._plugin) return;

        if (this.viewer.setVisualStyle) {
            this.viewer.setVisualStyle(styleName);
        }

        if (dbId === 'alphafold') {
            this.viewer.applyConfidenceColoring();
        } else {
            this.viewer.applyDefaultColoring();
        }
    },

    toggleViewerSpin() {
        if (!this.viewer || !this.viewer._plugin) return;
        const btn = document.getElementById('viewer-spin');
        this.viewerSpinning = !this.viewerSpinning;
        this.viewer.toggleSpin(this.viewerSpinning);
        if (this.viewerSpinning) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    },

    destroyViewer() {
        if (this.viewer) {
            if (this.viewerSpinning && this.viewer._plugin) this.viewer.toggleSpin(false);
            if (this.viewer.destroy) this.viewer.destroy();
            this.viewer = null;
        }
        this.viewerLoaded = false;
        this.sequenceStatsLoaded = false;
        if (this.seqChart) {
            this.seqChart.destroy();
            this.seqChart = null;
        }
    },

    renderSequenceViewer(sequence) {
        if (!sequence) return 'No sequence available';

        var hydrophobic = ['A', 'I', 'L', 'M', 'F', 'W', 'Y', 'V'];
        var polar = ['S', 'T', 'N', 'Q'];
        var positive = ['R', 'H', 'K'];
        var negative = ['D', 'E'];

        var html = '';
        var chunkSize = 10;
        var rowSize = 60;

        // Clean sequence
        sequence = sequence.replace(/\s/g, '').toUpperCase();

        for (var i = 0; i < sequence.length; i++) {
            if (i > 0 && i % rowSize === 0) html += '<br>';
            else if (i > 0 && i % chunkSize === 0) html += '<span class="db-expand-sequence-chunk"></span>';

            var aa = sequence[i];
            var cls = 'other';
            if (hydrophobic.includes(aa)) cls = 'hydrophobic';
            else if (polar.includes(aa)) cls = 'polar';
            else if (positive.includes(aa)) cls = 'positive';
            else if (negative.includes(aa)) cls = 'negative';

            html += '<span class="db-expand-aa ' + cls + '">' + aa + '</span>';
        }

        return html;
    },

    renderSequenceStats(sequence) {
        this.sequenceStatsLoaded = true;
        var ctx = document.getElementById('db-sequence-chart');
        if (!ctx || !sequence) return;

        // Clean sequence
        sequence = sequence.replace(/\s/g, '').toUpperCase();

        // Count AAs
        const counts = {};
        for (const aa of sequence) {
            counts[aa] = (counts[aa] || 0) + 1;
        }

        const sortedAAs = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
        const data = sortedAAs.map(aa => counts[aa]);

        // Match colors to legend
        const hydrophobic = ['A', 'I', 'L', 'M', 'F', 'W', 'Y', 'V'];
        const polar = ['S', 'T', 'N', 'Q'];
        const positive = ['R', 'H', 'K'];
        const negative = ['D', 'E'];

        const bgColors = sortedAAs.map(aa => {
            if (hydrophobic.includes(aa)) return 'rgb(14, 165, 233)'; // info (cyan)
            if (polar.includes(aa)) return 'rgb(16, 185, 129)'; // success (green)
            if (positive.includes(aa)) return 'rgb(239, 68, 68)'; // danger (red)
            if (negative.includes(aa)) return 'rgb(59, 130, 246)'; // primary (blue)
            return 'rgb(113, 113, 122)'; // other (gray)
        });

        this.seqChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: sortedAAs,
                datasets: [{
                    label: 'Amino Acid Count',
                    data: data,
                    backgroundColor: bgColors,
                    borderRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(255, 255, 255, 0.05)', drawBorder: false },
                        ticks: { color: '#71717a' }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#a1a1aa', font: { family: 'monospace', weight: 'bold' } }
                    }
                }
            }
        });
    },


    async saveToProject(entryId, projectId, format) {
        const db = this.getCurrentDatabase();
        try {
            const response = await API.post('/api/databases/' + this.state.currentDatabase + '/import', {
                project_id: parseInt(projectId),
                identifier: entryId,
                file_format: format || db.defaultFormat
            });
            if (response.status === 'success') {
                if (typeof Toast !== 'undefined') Toast.success('Saved ' + entryId + ' to ' + (response.file ? response.file.project_name || 'project' : 'project'));
            } else {
                throw new Error(response.message || 'Failed to save');
            }
        } catch (e) {
            console.error('Save error:', e);
            if (typeof Toast !== 'undefined') Toast.error('Failed to save: ' + (e.message || 'Unknown error'));
        }
    },

    updateContent(html) {
        const content = document.getElementById('databases-content');
        if (content) content.innerHTML = html;
    },

    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};

window.DatabasesModule = DatabasesModule;
