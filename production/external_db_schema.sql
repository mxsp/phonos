-- ================================================================
-- EXTERNAL DATABASE INTEGRATION SCHEMA
-- Tables for caching and linking external bioinformatics databases
-- ================================================================

-- ================================================================
-- CACHED EXTERNAL DATABASE ENTRIES
-- ================================================================

-- Cache UniProt protein entries
CREATE TABLE IF NOT EXISTS external_uniprot_cache (
    id SERIAL PRIMARY KEY,
    uniprot_id VARCHAR(20) UNIQUE NOT NULL,
    protein_name TEXT,
    gene_name VARCHAR(100),
    organism VARCHAR(255),
    organism_id INTEGER,
    sequence TEXT,
    sequence_length INTEGER,
    molecular_weight INTEGER,
    function_description TEXT,
    keywords JSONB,  -- Array of keywords
    go_terms JSONB,  -- Gene Ontology terms
    cross_references JSONB,  -- Cross-references to other databases
    metadata JSONB,  -- Full API response
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cache AlphaFold predictions
CREATE TABLE IF NOT EXISTS external_alphafold_cache (
    id SERIAL PRIMARY KEY,
    uniprot_id VARCHAR(20) UNIQUE NOT NULL,
    entry_id VARCHAR(50),
    gene VARCHAR(100),
    organism TEXT,
    model_version INTEGER,
    model_date DATE,
    confidence_score FLOAT,
    has_pae BOOLEAN DEFAULT FALSE,
    pdb_url TEXT,
    cif_url TEXT,
    pae_url TEXT,
    plddt_data JSONB,  -- Per-residue confidence scores
    metadata JSONB,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cache PDB experimental structures
CREATE TABLE IF NOT EXISTS external_pdb_cache (
    id SERIAL PRIMARY KEY,
    pdb_id VARCHAR(4) UNIQUE NOT NULL,
    title TEXT,
    experimental_method VARCHAR(100),
    resolution FLOAT,
    release_date DATE,
    deposition_date DATE,
    authors TEXT[],
    organism VARCHAR(255),
    chains TEXT[],
    pdb_url TEXT,
    cif_url TEXT,
    metadata JSONB,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cache NCBI entries
CREATE TABLE IF NOT EXISTS external_ncbi_cache (
    id SERIAL PRIMARY KEY,
    ncbi_id VARCHAR(50) UNIQUE NOT NULL,
    accession VARCHAR(50),
    database VARCHAR(50),  -- 'protein', 'nucleotide', etc.
    title TEXT,
    organism VARCHAR(255),
    sequence TEXT,
    sequence_length INTEGER,
    metadata JSONB,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- PROJECT LINKS TO EXTERNAL DATABASES
-- ================================================================

-- Link projects to external database entries
CREATE TABLE IF NOT EXISTS project_external_links (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,  -- 'uniprot', 'alphafold', 'pdb', 'ncbi'
    external_id VARCHAR(100) NOT NULL,
    link_type VARCHAR(50),  -- 'reference', 'imported', 'compared'
    notes TEXT,
    user_id INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, source, external_id)
);

-- Link jobs to external database entries
CREATE TABLE IF NOT EXISTS job_external_links (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(100) NOT NULL,
    source VARCHAR(50) NOT NULL,
    external_id VARCHAR(100) NOT NULL,
    link_type VARCHAR(50),  -- 'input', 'comparison', 'reference'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, source, external_id)
);

-- Link files to external database sources
CREATE TABLE IF NOT EXISTS file_external_sources (
    id SERIAL PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,  -- 'uniprot', 'alphafold', 'pdb'
    external_id VARCHAR(100) NOT NULL,
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    import_metadata JSONB,  -- Store version, confidence, etc.
    UNIQUE(file_id, source, external_id)
);

-- ================================================================
-- EXTERNAL DATABASE SEARCH HISTORY
-- ================================================================

-- Track user searches for analytics and quick access
CREATE TABLE IF NOT EXISTS external_db_searches (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    query TEXT NOT NULL,
    databases TEXT[],  -- Which databases were searched
    results_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- PROTEIN COLLECTIONS
-- ================================================================

-- User-curated collections of proteins from external databases
CREATE TABLE IF NOT EXISTS protein_collections (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    is_public BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Proteins in collections
CREATE TABLE IF NOT EXISTS collection_proteins (
    id SERIAL PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES protein_collections(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    external_id VARCHAR(100) NOT NULL,
    notes TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(collection_id, source, external_id)
);

-- ================================================================
-- STRUCTURE COMPARISON RESULTS
-- ================================================================

-- Store structure alignment/comparison results
CREATE TABLE IF NOT EXISTS structure_comparisons (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    structure1_source VARCHAR(50),
    structure1_id VARCHAR(100),
    structure2_source VARCHAR(50),
    structure2_id VARCHAR(100),
    comparison_method VARCHAR(50),  -- 'tm-align', 'ce-align', 'fatcat'
    tm_score FLOAT,
    rmsd FLOAT,
    aligned_residues INTEGER,
    results_data JSONB,  -- Detailed alignment results
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- EXTERNAL DATABASE METADATA
-- ================================================================

-- Track database versions and update status
CREATE TABLE IF NOT EXISTS external_db_metadata (
    id SERIAL PRIMARY KEY,
    database_name VARCHAR(50) UNIQUE NOT NULL,
    version VARCHAR(50),
    last_sync TIMESTAMP,
    total_entries BIGINT,
    status VARCHAR(50),  -- 'active', 'syncing', 'error'
    api_status VARCHAR(50),  -- 'available', 'rate_limited', 'down'
    rate_limit_remaining INTEGER,
    rate_limit_reset TIMESTAMP,
    metadata JSONB
);

-- ================================================================
-- INDEXES FOR PERFORMANCE
-- ================================================================

-- UniProt cache indexes
CREATE INDEX IF NOT EXISTS idx_uniprot_cache_gene ON external_uniprot_cache(gene_name);
CREATE INDEX IF NOT EXISTS idx_uniprot_cache_organism ON external_uniprot_cache(organism);
CREATE INDEX IF NOT EXISTS idx_uniprot_cache_updated ON external_uniprot_cache(last_updated);

-- AlphaFold cache indexes
CREATE INDEX IF NOT EXISTS idx_alphafold_cache_gene ON external_alphafold_cache(gene);
CREATE INDEX IF NOT EXISTS idx_alphafold_cache_version ON external_alphafold_cache(model_version);
CREATE INDEX IF NOT EXISTS idx_alphafold_cache_updated ON external_alphafold_cache(last_updated);

-- PDB cache indexes
CREATE INDEX IF NOT EXISTS idx_pdb_cache_method ON external_pdb_cache(experimental_method);
CREATE INDEX IF NOT EXISTS idx_pdb_cache_resolution ON external_pdb_cache(resolution);
CREATE INDEX IF NOT EXISTS idx_pdb_cache_release ON external_pdb_cache(release_date DESC);

-- Project links indexes
CREATE INDEX IF NOT EXISTS idx_project_links_project ON project_external_links(project_id);
CREATE INDEX IF NOT EXISTS idx_project_links_source_id ON project_external_links(source, external_id);

-- Job links indexes
CREATE INDEX IF NOT EXISTS idx_job_links_job ON job_external_links(job_id);
CREATE INDEX IF NOT EXISTS idx_job_links_source_id ON job_external_links(source, external_id);

-- File sources indexes
CREATE INDEX IF NOT EXISTS idx_file_sources_file ON file_external_sources(file_id);
CREATE INDEX IF NOT EXISTS idx_file_sources_source ON file_external_sources(source, external_id);

-- Search history indexes
CREATE INDEX IF NOT EXISTS idx_searches_user ON external_db_searches(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_searches_query ON external_db_searches USING gin(to_tsvector('english', query));

-- Collections indexes
CREATE INDEX IF NOT EXISTS idx_collections_user ON protein_collections(user_id);
CREATE INDEX IF NOT EXISTS idx_collections_public ON protein_collections(is_public) WHERE is_public = TRUE;
CREATE INDEX IF NOT EXISTS idx_collection_proteins_collection ON collection_proteins(collection_id);

-- Comparisons indexes
CREATE INDEX IF NOT EXISTS idx_comparisons_user ON structure_comparisons(user_id);
CREATE INDEX IF NOT EXISTS idx_comparisons_project ON structure_comparisons(project_id);
CREATE INDEX IF NOT EXISTS idx_comparisons_tm_score ON structure_comparisons(tm_score DESC);

-- ================================================================
-- FULL TEXT SEARCH INDEXES
-- ================================================================

-- Enable pg_trgm for fuzzy search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Full-text search on protein names
CREATE INDEX IF NOT EXISTS idx_uniprot_protein_name_fts
ON external_uniprot_cache USING gin(to_tsvector('english', protein_name));

CREATE INDEX IF NOT EXISTS idx_uniprot_function_fts
ON external_uniprot_cache USING gin(to_tsvector('english', function_description));

CREATE INDEX IF NOT EXISTS idx_pdb_title_fts
ON external_pdb_cache USING gin(to_tsvector('english', title));

-- Trigram indexes for fuzzy matching
CREATE INDEX IF NOT EXISTS idx_uniprot_protein_name_trgm
ON external_uniprot_cache USING gin(protein_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_uniprot_gene_name_trgm
ON external_uniprot_cache USING gin(gene_name gin_trgm_ops);

-- ================================================================
-- VIEWS FOR COMMON QUERIES
-- ================================================================

-- View for proteins with all available structures
CREATE OR REPLACE VIEW proteins_with_structures AS
SELECT
    u.uniprot_id,
    u.protein_name,
    u.gene_name,
    u.organism,
    u.sequence_length,
    CASE WHEN af.id IS NOT NULL THEN TRUE ELSE FALSE END as has_alphafold,
    af.model_version as alphafold_version,
    af.confidence_score as alphafold_confidence,
    COUNT(DISTINCT p.pdb_id) as experimental_structures_count,
    ARRAY_AGG(DISTINCT p.pdb_id) FILTER (WHERE p.pdb_id IS NOT NULL) as pdb_ids,
    MIN(p.resolution) as best_resolution,
    u.last_updated as cache_updated
FROM external_uniprot_cache u
LEFT JOIN external_alphafold_cache af ON u.uniprot_id = af.uniprot_id
LEFT JOIN LATERAL (
    SELECT pdb_id, resolution
    FROM external_pdb_cache
    WHERE metadata->'cross_references'->>'UniProt' LIKE '%' || u.uniprot_id || '%'
) p ON TRUE
GROUP BY u.uniprot_id, u.protein_name, u.gene_name, u.organism,
         u.sequence_length, af.id, af.model_version, af.confidence_score, u.last_updated;

-- View for project-linked external entries
CREATE OR REPLACE VIEW project_external_entries AS
SELECT
    p.id as project_id,
    p.name as project_name,
    pel.source,
    pel.external_id,
    pel.link_type,
    COALESCE(
        uc.protein_name,
        pc.title,
        afc.gene
    ) as entry_name,
    pel.created_at as linked_at
FROM projects p
JOIN project_external_links pel ON p.id = pel.project_id
LEFT JOIN external_uniprot_cache uc ON pel.source = 'uniprot' AND pel.external_id = uc.uniprot_id
LEFT JOIN external_pdb_cache pc ON pel.source = 'pdb' AND pel.external_id = pc.pdb_id
LEFT JOIN external_alphafold_cache afc ON pel.source = 'alphafold' AND pel.external_id = afc.uniprot_id;

-- ================================================================
-- TRIGGERS FOR AUTOMATIC CACHE INVALIDATION
-- ================================================================

-- Update last_updated on cache modification
CREATE OR REPLACE FUNCTION update_cache_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_uniprot_cache_timestamp
BEFORE UPDATE ON external_uniprot_cache
FOR EACH ROW
EXECUTE FUNCTION update_cache_timestamp();

CREATE TRIGGER update_alphafold_cache_timestamp
BEFORE UPDATE ON external_alphafold_cache
FOR EACH ROW
EXECUTE FUNCTION update_cache_timestamp();

CREATE TRIGGER update_pdb_cache_timestamp
BEFORE UPDATE ON external_pdb_cache
FOR EACH ROW
EXECUTE FUNCTION update_cache_timestamp();

-- ================================================================
-- UTILITY FUNCTIONS
-- ================================================================

-- Function to check if cache entry is stale (older than X days)
CREATE OR REPLACE FUNCTION is_cache_stale(
    last_update TIMESTAMP,
    max_age_days INTEGER DEFAULT 30
) RETURNS BOOLEAN AS $$
BEGIN
    RETURN last_update < (CURRENT_TIMESTAMP - (max_age_days || ' days')::INTERVAL);
END;
$$ LANGUAGE plpgsql;

-- Function to get all structures for a UniProt ID
CREATE OR REPLACE FUNCTION get_all_structures(uniprot_acc VARCHAR)
RETURNS TABLE(
    source VARCHAR,
    identifier VARCHAR,
    title TEXT,
    method VARCHAR,
    resolution FLOAT,
    confidence FLOAT
) AS $$
BEGIN
    -- AlphaFold prediction
    RETURN QUERY
    SELECT
        'alphafold'::VARCHAR as source,
        uniprot_id::VARCHAR as identifier,
        ('AlphaFold prediction for ' || gene)::TEXT as title,
        'prediction'::VARCHAR as method,
        NULL::FLOAT as resolution,
        confidence_score as confidence
    FROM external_alphafold_cache
    WHERE uniprot_id = uniprot_acc;

    -- Experimental structures from PDB
    -- (This is simplified - you'd need proper UniProt-PDB mapping)
    RETURN QUERY
    SELECT
        'pdb'::VARCHAR as source,
        pdb_id::VARCHAR as identifier,
        title::TEXT,
        experimental_method::VARCHAR as method,
        resolution,
        NULL::FLOAT as confidence
    FROM external_pdb_cache
    WHERE metadata @> jsonb_build_object('uniprot', uniprot_acc);
END;
$$ LANGUAGE plpgsql;

-- ================================================================
-- DATA MIGRATION / INITIAL SETUP
-- ================================================================

-- Initialize external database metadata
INSERT INTO external_db_metadata (database_name, status, api_status)
VALUES
    ('uniprot', 'active', 'available'),
    ('alphafold', 'active', 'available'),
    ('pdb', 'active', 'available'),
    ('ncbi', 'active', 'available')
ON CONFLICT (database_name) DO NOTHING;

-- ================================================================
-- MAINTENANCE QUERIES
-- ================================================================

-- Clean up old cache entries (run periodically)
-- DELETE FROM external_uniprot_cache WHERE last_updated < CURRENT_TIMESTAMP - INTERVAL '90 days';
-- DELETE FROM external_alphafold_cache WHERE last_updated < CURRENT_TIMESTAMP - INTERVAL '90 days';
-- DELETE FROM external_pdb_cache WHERE last_updated < CURRENT_TIMESTAMP - INTERVAL '180 days';

-- Analyze tables
ANALYZE external_uniprot_cache;
ANALYZE external_alphafold_cache;
ANALYZE external_pdb_cache;
ANALYZE project_external_links;
