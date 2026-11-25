-- Migration 017: Molecule image/SDF cache for MolMIM
-- Caches PubChem 2D images and SDF data keyed by SMILES hash

CREATE TABLE IF NOT EXISTS molecule_cache (
    id SERIAL PRIMARY KEY,
    smiles_hash VARCHAR(64) NOT NULL UNIQUE,
    smiles TEXT NOT NULL,
    image_png BYTEA,
    sdf_data TEXT,
    properties_json TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    last_accessed TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_molecule_cache_hash ON molecule_cache (smiles_hash);
CREATE INDEX IF NOT EXISTS idx_molecule_cache_accessed ON molecule_cache (last_accessed);
