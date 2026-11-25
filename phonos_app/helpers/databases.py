"""Public database integration helpers for PDB, UniProt, GenBank, and PubChem."""
from __future__ import annotations

import requests
from typing import Any


class DatabaseIntegrationError(Exception):
    """Base exception for database integration errors."""
    pass


def _require_openbabel():
    """Lazily import the optional OpenBabel dependency.

    OpenBabel is NOT a pip-only package — it needs the system libopenbabel plus
    matching Python bindings, which are absent on some hosts. Importing it at
    module top-level made the whole module unimportable there, which in turn
    crashed *every* job submission (single + batch, all models) because
    `_handle_job_submission` imports this module for its UniProt/PDB/PubChem
    helpers. Only the two ligand format-conversion functions actually need
    OpenBabel, so we import it on demand and fail with a clear, actionable
    message instead of a bare ModuleNotFoundError that surfaces as a generic
    "database error".
    """
    try:
        from openbabel import openbabel as ob
        return ob
    except ImportError as exc:
        raise DatabaseIntegrationError(
            "OpenBabel is not installed on this host, so ligand format "
            "conversion (SDF/MOL2 ↔ SMILES) is unavailable. Install the system "
            "package and Python bindings to enable it, e.g. "
            "`sudo dnf install -y openbabel openbabel-devel` then "
            "`pip install openbabel-wheel`."
        ) from exc


def _require_rdkit():
    """Lazily import RDKit, the preferred ligand-conversion backend.

    RDKit ships as a self-contained pip wheel (`pip install rdkit`) with no
    external system libraries, so it is far more portable than OpenBabel (whose
    wheel segfaults on hosts missing libXrender/libXext). We use RDKit first and
    fall back to OpenBabel only if RDKit is unavailable, so SMILES<->SDF/MOL2
    conversion keeps working on a bare host without any `sudo dnf` provisioning.
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem
    return Chem, AllChem


def _rdkit_ligand_to_smiles(file_content: str, file_format: str) -> str:
    Chem, _ = _require_rdkit()
    fmt = (file_format or 'sdf').lower().lstrip('.')
    if fmt in ('smi', 'smiles'):
        token = file_content.strip().split()
        if not token:
            raise ValueError("Empty SMILES content")
        return token[0]
    if fmt == 'mol2':
        mol = Chem.MolFromMol2Block(file_content, sanitize=True, removeHs=True)
    elif fmt in ('pdb', 'ent'):
        mol = Chem.MolFromPDBBlock(file_content, sanitize=True, removeHs=True)
    else:  # sdf, mol, mdl and unknowns: MDL molblock is the common case
        mol = Chem.MolFromMolBlock(file_content, sanitize=True, removeHs=True)
    if mol is None:
        raise ValueError(f"RDKit failed to parse {fmt.upper()} content")
    smiles = Chem.MolToSmiles(mol)
    if not smiles:
        raise ValueError("RDKit produced an empty SMILES")
    return smiles


def _rdkit_smiles_to_sdf(smiles: str) -> str:
    Chem, AllChem = _require_rdkit()
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("RDKit failed to parse SMILES string")
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = 0xf00d
    if AllChem.EmbedMolecule(mol, params) != 0:
        # Retry with random coordinates for hard/strained molecules
        if AllChem.EmbedMolecule(mol, randomSeed=0xf00d, useRandomCoords=True) != 0:
            raise ValueError("RDKit failed to generate 3D coordinates")
    try:
        AllChem.MMFFOptimizeMolecule(mol)
    except Exception:
        try:
            AllChem.UFFOptimizeMolecule(mol)
        except Exception:
            pass  # unoptimized embed is still usable for docking input
    sdf = Chem.MolToMolBlock(mol)
    if not sdf or ('V2000' not in sdf.upper() and 'V3000' not in sdf.upper()):
        raise ValueError("RDKit produced an SDF without a V2000/V3000 block")
    if not sdf.rstrip().endswith('$$$$'):
        sdf = sdf.rstrip() + '\n$$$$\n'
    return sdf


def _openbabel_ligand_to_smiles(file_content: str, file_format: str) -> str:
    ob = _require_openbabel()
    obConversion = ob.OBConversion()
    obConversion.SetInAndOutFormats(file_format, "smi")
    mol = ob.OBMol()
    if not obConversion.ReadString(mol, file_content):
        raise ValueError(f"Failed to parse {file_format.upper()} content")
    if mol.NumAtoms() == 0:
        raise ValueError("No atoms found in molecule")
    smiles = obConversion.WriteString(mol).strip()
    if not smiles:
        raise ValueError("Failed to generate SMILES from molecule")
    return smiles


def _openbabel_smiles_to_sdf(smiles: str) -> str:
    ob = _require_openbabel()
    obConversion = ob.OBConversion()
    obConversion.SetInAndOutFormats("smi", "sdf")
    mol = ob.OBMol()
    if not obConversion.ReadString(mol, smiles):
        raise ValueError("Failed to parse SMILES string")
    if mol.NumAtoms() == 0:
        raise ValueError("No atoms found in molecule")
    # Generate 3D coordinates (required for docking)
    builder = ob.OBBuilder()
    builder.Build(mol)
    # Try MMFF94 for optimization, fall back to UFF on failure
    try:
        ff = ob.OBForceField.FindForceField("mmff94")
        if ff:
            ff.Setup(mol)
            ff.ConjugateGradients(500)
            ff.GetCoordinates(mol)
    except Exception:
        ff_uff = ob.OBForceField.FindForceField("uff")
        if ff_uff:
            ff_uff.Setup(mol)
            ff_uff.ConjugateGradients(500)
            ff_uff.GetCoordinates(mol)
    sdf = obConversion.WriteString(mol).strip()
    if not sdf:
        raise ValueError("Failed to generate SDF from SMILES")
    if 'V2000' not in sdf.upper() and 'V3000' not in sdf.upper():
        raise ValueError("Generated SDF is missing V2000/V3000 block")
    if not sdf.endswith('$$$$'):
        sdf += '\n$$$$\n' if sdf.endswith('M  END') else '\nM  END\n$$$$\n'
    return sdf


def convert_ligand_to_smiles(file_content: str, file_format: str = 'sdf') -> str:
    """Convert ligand file content (SDF/MOL/MOL2/PDB) to a SMILES string.

    Uses RDKit when available (portable pip wheel), falling back to OpenBabel.

    Raises:
        ValueError: If conversion fails or no valid molecule found.
    """
    try:
        return _rdkit_ligand_to_smiles(file_content, file_format)
    except ImportError:
        pass  # RDKit not installed — try OpenBabel
    except Exception as e:
        raise ValueError(f"Error converting ligand to SMILES: {str(e)}")
    try:
        return _openbabel_ligand_to_smiles(file_content, file_format)
    except Exception as e:
        raise ValueError(f"Error converting ligand to SMILES: {str(e)}")


def convert_smiles_to_sdf(smiles: str) -> str:
    """Convert a SMILES string to SDF with generated 3D coordinates.

    Uses RDKit when available (portable pip wheel), falling back to OpenBabel.

    Raises:
        ValueError: If conversion fails.
    """
    try:
        return _rdkit_smiles_to_sdf(smiles)
    except ImportError:
        pass  # RDKit not installed — try OpenBabel
    except Exception as e:
        raise ValueError(f"Error converting SMILES to SDF: {str(e)}")
    try:
        return _openbabel_smiles_to_sdf(smiles)
    except Exception as e:
        raise ValueError(f"Error converting SMILES to SDF: {str(e)}")


class PDBIntegration:
    """Integration with Protein Data Bank (PDB)."""

    BASE_URL = "https://data.rcsb.org/rest/v1/core"
    SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
    FILES_URL = "https://files.rcsb.org/download"

    @classmethod
    def fetch_entry(cls, pdb_id: str) -> dict[str, Any]:
        """Fetch PDB entry data by ID."""
        url = f"{cls.BASE_URL}/entry/{pdb_id.upper()}"
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to fetch PDB entry {pdb_id}: {response.status_code}")

        return response.json()

    @classmethod
    def download_structure(cls, pdb_id: str, format: str = "pdb", return_format: bool = False):
        """Download PDB structure file in specified format (pdb, cif, xml).

        Falls back to CIF format if PDB format is not available, as many
        structures on RCSB are now only available in mmCIF format.

        Args:
            pdb_id: The PDB ID
            format: Desired format (pdb, cif, xml)
            return_format: If True, returns tuple of (content, actual_format)

        Returns:
            Structure content as string, or tuple (content, format) if return_format=True
        """
        pdb_id_upper = pdb_id.upper()
        url = f"{cls.FILES_URL}/{pdb_id_upper}.{format}"
        response = requests.get(url, timeout=30)
        actual_format = format

        # If PDB format fails with 404, try CIF format as fallback
        if response.status_code == 404 and format == "pdb":
            cif_url = f"{cls.FILES_URL}/{pdb_id_upper}.cif"
            cif_response = requests.get(cif_url, timeout=30)
            if cif_response.status_code == 200:
                content = cif_response.text
                actual_format = "cif"
                return (content, actual_format) if return_format else content
            # If CIF also fails, raise error with original format
            raise DatabaseIntegrationError(f"Failed to download PDB structure {pdb_id}: structure not available in PDB or CIF format")

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to download PDB structure {pdb_id}: {response.status_code}")

        content = response.text
        return (content, actual_format) if return_format else content

    @classmethod
    def search(cls, query: str, limit: int = 10) -> list[dict]:
        """Search PDB database using full_text service."""
        search_query = {
            "query": {
                "type": "terminal",
                "service": "full_text",
                "parameters": {
                    "value": query
                }
            },
            "return_type": "entry",
            "request_options": {
                "results_content_type": ["experimental"],
                "return_all_hits": False,
                "pager": {
                    "start": 0,
                    "rows": limit
                }
            }
        }

        response = requests.post(cls.SEARCH_URL, json=search_query, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"PDB search failed: {response.status_code}")

        data = response.json()
        return data.get("result_set", [])


class UniProtIntegration:
    """Integration with Universal Protein Resource (UniProt)."""

    BASE_URL = "https://rest.uniprot.org/uniprotkb"

    @classmethod
    def fetch_entry(cls, uniprot_id: str) -> dict[str, Any]:
        """Fetch UniProt entry data by ID."""
        url = f"{cls.BASE_URL}/{uniprot_id}.json"
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to fetch UniProt entry {uniprot_id}: {response.status_code}")

        return response.json()

    @classmethod
    def get_sequence(cls, uniprot_id: str) -> str:
        """Get protein sequence from UniProt."""
        url = f"{cls.BASE_URL}/{uniprot_id}.fasta"
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to fetch sequence for {uniprot_id}: {response.status_code}")

        return response.text

    @classmethod
    def search(cls, query: str, limit: int = 10) -> list[dict]:
        """Search UniProt database."""
        url = f"{cls.BASE_URL}/search"
        params = {
            "query": query,
            "format": "json",
            "size": limit
        }

        response = requests.get(url, params=params, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"UniProt search failed: {response.status_code}")

        data = response.json()
        return data.get("results", [])


class GenBankIntegration:
    """Integration with GenBank via NCBI E-utilities."""

    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    @classmethod
    def fetch_entry(cls, genbank_id: str, db: str = "nucleotide") -> dict[str, Any]:
        """Fetch GenBank entry data by ID."""
        url = f"{cls.BASE_URL}/efetch.fcgi"
        params = {
            "db": db,
            "id": genbank_id,
            "rettype": "gb",
            "retmode": "xml"
        }

        response = requests.get(url, params=params, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to fetch GenBank entry {genbank_id}: {response.status_code}")

        return {"xml": response.text, "id": genbank_id}

    @classmethod
    def get_sequence(cls, genbank_id: str, db: str = "nucleotide") -> str:
        """Get sequence from GenBank."""
        url = f"{cls.BASE_URL}/efetch.fcgi"
        params = {
            "db": db,
            "id": genbank_id,
            "rettype": "fasta",
            "retmode": "text"
        }

        response = requests.get(url, params=params, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to fetch sequence for {genbank_id}: {response.status_code}")

        return response.text

    @classmethod
    def search(cls, query: str, db: str = "nucleotide", limit: int = 10) -> list[dict]:
        """Search GenBank database."""
        # First, search for IDs
        search_url = f"{cls.BASE_URL}/esearch.fcgi"
        search_params = {
            "db": db,
            "term": query,
            "retmax": limit,
            "retmode": "json"
        }

        response = requests.get(search_url, params=search_params, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"GenBank search failed: {response.status_code}")

        data = response.json()
        id_list = data.get("esearchresult", {}).get("idlist", [])

        results = []
        for genbank_id in id_list:
            # Fetch summary for each ID
            summary_url = f"{cls.BASE_URL}/esummary.fcgi"
            summary_params = {
                "db": db,
                "id": genbank_id,
                "retmode": "json"
            }

            summary_response = requests.get(summary_url, params=summary_params, timeout=30)
            if summary_response.status_code == 200:
                summary_data = summary_response.json()
                results.append(summary_data.get("result", {}).get(genbank_id, {}))

        return results


class PubChemIntegration:
    """Integration with PubChem."""

    BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

    @classmethod
    def fetch_compound(cls, cid: int | str) -> dict[str, Any]:
        """Fetch PubChem compound data by CID."""
        url = f"{cls.BASE_URL}/compound/cid/{cid}/JSON"
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to fetch PubChem compound {cid}: {response.status_code}")

        return response.json()

    @classmethod
    def search_by_name(cls, name: str, limit: int = 10) -> list[dict]:
        """Search PubChem compounds by name."""
        # First get CIDs
        search_url = f"{cls.BASE_URL}/compound/name/{name}/cids/JSON"
        params = {"list_return": "listkey"}

        response = requests.get(search_url, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"PubChem search failed: {response.status_code}")

        data = response.json()
        cids = data.get("IdentifierList", {}).get("CID", [])[:limit]

        results = []
        for cid in cids:
            try:
                compound = cls.fetch_compound(cid)
                results.append(compound)
            except DatabaseIntegrationError:
                continue

        return results

    @classmethod
    def get_smiles(cls, cid: int | str) -> str:
        """Get SMILES string for a compound."""
        url = f"{cls.BASE_URL}/compound/cid/{cid}/property/CanonicalSMILES,IsomericSMILES/JSON"
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"Failed to fetch SMILES for {cid}: {response.status_code}")

        data = response.json()
        properties = data.get("PropertyTable", {}).get("Properties", [])

        if properties:
            prop = properties[0]
            # Try different SMILES field names (PubChem API returns different field names)
            return (prop.get("IsomericSMILES") or
                    prop.get("CanonicalSMILES") or
                    prop.get("SMILES") or
                    prop.get("ConnectivitySMILES") or
                    "")

        return ""

    @classmethod
    def search_by_smiles(cls, smiles: str) -> dict[str, Any]:
        """Search for a compound by SMILES string."""
        url = f"{cls.BASE_URL}/compound/smiles/{smiles}/JSON"
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            raise DatabaseIntegrationError(f"SMILES search failed: {response.status_code}")

        return response.json()


# Convenience functions for unified interface
def import_from_pdb(pdb_id: str, format: str = "pdb") -> dict:
    """Import structure from PDB.

    Returns:
        dict with keys:
            - source: 'pdb'
            - id: PDB ID
            - entry_data: Metadata about the entry
            - structure_data: The actual PDB file content (string)
            - format: File format actually used (pdb, cif, xml) - may differ from requested if fallback occurred
    """
    try:
        entry = PDBIntegration.fetch_entry(pdb_id)
        structure, actual_format = PDBIntegration.download_structure(pdb_id, format, return_format=True)

        return {
            "source": "pdb",
            "id": pdb_id.upper(),
            "entry_data": entry,
            "structure_data": structure,  # This is the file content to save
            "format": actual_format  # Actual format used (may be cif if pdb was not available)
        }
    except Exception as e:
        raise DatabaseIntegrationError(f"PDB import failed: {str(e)}")


def import_from_uniprot(uniprot_id: str) -> dict:
    """Import protein data from UniProt.

    Returns:
        dict with keys:
            - source: 'uniprot'
            - id: UniProt accession
            - entry_data: Metadata about the entry
            - sequence: FASTA formatted sequence (string)
    """
    try:
        from phonos_app.helpers.public_databases import db_manager

        uniprot_connector = db_manager.get_connector('uniprot')

        # Fetch metadata
        entry = uniprot_connector.fetch_entry(uniprot_id, format='json')

        # Fetch sequence
        sequence = uniprot_connector.fetch_entry(uniprot_id, format='fasta')

        if not sequence:
            raise DatabaseIntegrationError(f"Failed to fetch sequence for {uniprot_id}")

        return {
            "source": "uniprot",
            "id": uniprot_id,
            "entry_data": entry if isinstance(entry, dict) else {},
            "sequence": sequence  # This is the FASTA content to save
        }
    except Exception as e:
        raise DatabaseIntegrationError(f"UniProt import failed: {str(e)}")


def import_from_genbank(genbank_id: str, db: str = "nucleotide") -> dict:
    """Import sequence from GenBank.

    Returns:
        dict with keys:
            - source: 'genbank'
            - id: GenBank accession
            - database: Database type (nucleotide, protein)
            - entry_data: Metadata (XML format)
            - sequence: FASTA formatted sequence (string)
    """
    try:
        entry = GenBankIntegration.fetch_entry(genbank_id, db)
        sequence = GenBankIntegration.get_sequence(genbank_id, db)

        return {
            "source": "genbank",
            "id": genbank_id,
            "database": db,
            "entry_data": entry,
            "sequence": sequence  # This is the FASTA content to save
        }
    except Exception as e:
        raise DatabaseIntegrationError(f"GenBank import failed: {str(e)}")


def import_from_pubchem(cid: int | str) -> dict:
    """Import compound data from PubChem.

    Returns:
        dict with keys:
            - source: 'pubchem'
            - id: PubChem CID
            - compound_data: Full compound metadata
            - smiles: SMILES string representation
    """
    try:
        compound = PubChemIntegration.fetch_compound(cid)
        smiles = PubChemIntegration.get_smiles(cid)

        return {
            "source": "pubchem",
            "id": str(cid),
            "compound_data": compound,
            "smiles": smiles  # This is the SMILES string to save
        }
    except Exception as e:
        raise DatabaseIntegrationError(f"PubChem import failed: {str(e)}")


def fetch_protein_for_job(input_type: str, pdb_id: str = None, uniprot_id: str = None, file_content: str = None) -> tuple[str, dict]:
    """Fetch protein data for job submission from various sources.

    Args:
        input_type: 'file', 'pdb_id', or 'uniprot_id'
        pdb_id: PDB ID if input_type is 'pdb_id'
        uniprot_id: UniProt ID if input_type is 'uniprot_id'
        file_content: File content if input_type is 'file'

    Returns:
        tuple of (protein_data, metadata)
        - protein_data: PDB structure or sequence string
        - metadata: dict with source information
    """
    if input_type == 'pdb_id':
        if not pdb_id:
            raise ValueError('PDB ID is required when using PDB database input')

        pdb_data = import_from_pdb(pdb_id, format='pdb')
        return pdb_data['structure_data'], {
            'type': 'pdb',
            'id': pdb_data['id'],
            'entry_data': pdb_data['entry_data']
        }

    elif input_type == 'uniprot_id':
        if not uniprot_id:
            raise ValueError('UniProt ID is required when using UniProt database input')

        uniprot_data = import_from_uniprot(uniprot_id)
        return uniprot_data['sequence'], {
            'type': 'uniprot',
            'id': uniprot_data['id'],
            'entry_data': uniprot_data['entry_data']
        }

    elif input_type == 'file':
        if not file_content:
            raise ValueError('File content is required when using file input')
        return file_content, {'type': 'file'}

    else:
        raise ValueError(f'Unknown protein input type: {input_type}')


def fetch_ligand_for_job(input_type: str, smiles: str = None, pubchem_cid: str = None, file_content: str = None) -> tuple[str, dict]:
    """Fetch ligand data for job submission from various sources.

    Args:
        input_type: 'smiles', 'pubchem_cid', or 'file'
        smiles: SMILES string if input_type is 'smiles'
        pubchem_cid: PubChem CID if input_type is 'pubchem_cid'
        file_content: File content if input_type is 'file'

    Returns:
        tuple of (ligand_data, metadata)
        - ligand_data: SMILES string or file content
        - metadata: dict with source information
    """
    if input_type == 'pubchem_cid':
        if not pubchem_cid:
            raise ValueError('PubChem CID is required when using PubChem database input')

        pubchem_data = import_from_pubchem(pubchem_cid)
        if not pubchem_data['smiles']:
            raise ValueError(f'No SMILES found for PubChem CID {pubchem_cid}')

        return pubchem_data['smiles'], {
            'type': 'pubchem',
            'cid': pubchem_data['id'],
            'compound_data': pubchem_data['compound_data']
        }

    elif input_type == 'smiles':
        if not smiles:
            raise ValueError('SMILES string is required when using SMILES input')
        return smiles, {'type': 'smiles'}

    elif input_type == 'file':
        if not file_content:
            raise ValueError('File content is required when using file input')
        return file_content, {'type': 'file'}

    else:
        raise ValueError(f'Unknown ligand input type: {input_type}')
