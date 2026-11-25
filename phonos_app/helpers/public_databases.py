"""
Public Database Integrations for Phonos
Provides connectors for PDB, AlphaFold, UniProt, ChEMBL, PubChem, and more.
"""

import os
import requests
import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import time

logger = logging.getLogger(__name__)

# ============================================================================
# BASE DATABASE CONNECTOR
# ============================================================================

class DatabaseConnector:
    """Base class for public database connectors."""

    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Phonos/2.4.9 (Computational Biology Platform)'
        })

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        """Search the database. To be implemented by subclasses."""
        raise NotImplementedError

    def fetch_file(self, identifier: str, file_format: str = None) -> Tuple[bytes, str]:
        """
        Fetch a file from the database.
        Returns: (file_content, filename)
        """
        raise NotImplementedError

    def get_metadata(self, identifier: str) -> Dict:
        """Get metadata for an entry."""
        raise NotImplementedError

    def _safe_request(self, url: str, timeout: int = 30, max_retries: int = 3) -> requests.Response:
        """Make a safe HTTP request with retries."""
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=timeout)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
                time.sleep(1 * (attempt + 1))  # Exponential backoff


# ============================================================================
# PROTEIN DATA BANK (PDB / RCSB)
# ============================================================================

class PDBConnector(DatabaseConnector):
    """Connector for RCSB Protein Data Bank."""

    def __init__(self):
        super().__init__('PDB', 'https://data.rcsb.org')
        self.search_url = 'https://search.rcsb.org/rcsbsearch/v2/query'
        self.files_url = 'https://files.rcsb.org/download'

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Search PDB by various criteria.

        Examples:
            - "hemoglobin" - search by protein name
            - "4HHB" - search by PDB ID
            - "insulin" - search by description
        """
        try:
            # Construct RCSB search query using full_text service for general searches
            search_payload = {
                "query": {
                    "type": "terminal",
                    "service": "full_text",
                    "parameters": {
                        "value": query
                    }
                },
                "request_options": {
                    "return_all_hits": False,
                    "results_content_type": ["experimental"],
                    "sort": [
                        {
                            "sort_by": "score",
                            "direction": "desc"
                        }
                    ],
                    "scoring_strategy": "combined"
                },
                "return_type": "entry"
            }

            response = self.session.post(
                self.search_url,
                json=search_payload,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            pdb_ids = [r.get('identifier') for r in data.get('result_set', [])[:limit] if r.get('identifier')]

            if not pdb_ids:
                return []

            # Batch fetch metadata via GraphQL (single request instead of N+1)
            metadata_map = self._batch_metadata(pdb_ids)

            results = []
            for pdb_id in pdb_ids:
                meta = metadata_map.get(pdb_id, {})
                results.append({
                    'id': pdb_id,
                    'title': meta.get('title', pdb_id),
                    'description': meta.get('description', ''),
                    'organism': meta.get('organism', 'N/A'),
                    'method': meta.get('method', 'N/A'),
                    'resolution': meta.get('resolution', 'N/A'),
                    'database': 'PDB',
                    'url': f'https://www.rcsb.org/structure/{pdb_id}'
                })

            return results

        except Exception as e:
            logger.error(f"PDB search failed: {e}", exc_info=True)
            return []

    def _batch_metadata(self, pdb_ids: List[str]) -> Dict[str, Dict]:
        """Fetch metadata for multiple PDB entries in a single GraphQL request."""
        graphql_url = 'https://data.rcsb.org/graphql'
        query = '''
        query($ids: [String!]!) {
          entries(entry_ids: $ids) {
            rcsb_id
            struct {
              title
              pdbx_descriptor
            }
            exptl {
              method
            }
            refine {
              ls_d_res_high
            }
            rcsb_entity_source_organism {
              ncbi_scientific_name
            }
          }
        }
        '''

        try:
            response = self.session.post(
                graphql_url,
                json={'query': query, 'variables': {'ids': pdb_ids}},
                timeout=30
            )
            response.raise_for_status()
            gql_data = response.json()

            result_map = {}
            for entry in gql_data.get('data', {}).get('entries', []):
                pdb_id = entry.get('rcsb_id')
                struct = entry.get('struct') or {}
                exptl_list = entry.get('exptl') or [{}]
                refine_list = entry.get('refine') or [{}]
                organism_list = entry.get('rcsb_entity_source_organism') or []

                result_map[pdb_id] = {
                    'title': struct.get('title', pdb_id),
                    'description': struct.get('pdbx_descriptor', ''),
                    'method': exptl_list[0].get('method', 'N/A') if exptl_list else 'N/A',
                    'resolution': refine_list[0].get('ls_d_res_high', 'N/A') if refine_list else 'N/A',
                    'organism': organism_list[0].get('ncbi_scientific_name', 'N/A') if organism_list else 'N/A'
                }

            return result_map
        except Exception as e:
            logger.warning(f"GraphQL batch metadata failed, falling back: {e}")
            return {}

    def fetch_file(self, pdb_id: str, file_format: str = 'pdb') -> Tuple[bytes, str]:
        """
        Fetch a PDB structure file.

        Supported formats:
            - pdb: PDB format
            - cif: mmCIF format
            - xml: PDBML format
        """
        pdb_id = pdb_id.upper()

        format_extensions = {
            'pdb': 'pdb',
            'cif': 'cif',
            'mmcif': 'cif',
            'xml': 'xml'
        }

        ext = format_extensions.get(file_format.lower(), 'pdb')
        url = f"{self.files_url}/{pdb_id}.{ext}"

        try:
            response = self._safe_request(url)
            filename = f"{pdb_id}.{ext}"
            return response.content, filename

        except Exception as e:
            logger.error(f"Failed to fetch PDB file {pdb_id}: {e}")
            raise

    def get_metadata(self, pdb_id: str) -> Dict:
        """Get detailed metadata for a PDB entry."""
        try:
            url = f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id.upper()}"
            response = self._safe_request(url)
            data = response.json()

            # Extract relevant metadata
            struct = data.get('struct', {})
            exptl = data.get('exptl', [{}])[0]
            refine = data.get('refine', [{}])[0]
            audit = data.get('audit_author', [])

            return {
                'title': struct.get('title', 'N/A'),
                'description': struct.get('pdbx_descriptor', ''),
                'organism': data.get('rcsb_entity_source_organism', [{}])[0].get('ncbi_scientific_name', 'N/A'),
                'method': exptl.get('method', 'N/A'),
                'resolution': refine.get('ls_d_res_high', 'N/A'),
                'deposition_date': data.get('rcsb_accession_info', {}).get('deposit_date', 'N/A'),
                'authors': [a.get('name', '') for a in audit]
            }

        except Exception as e:
            logger.warning(f"Failed to get PDB metadata for {pdb_id}: {e}")
            return {}


# ============================================================================
# ALPHAFOLD PROTEIN STRUCTURE DATABASE
# ============================================================================

class AlphaFoldConnector(DatabaseConnector):
    """Connector for AlphaFold Protein Structure Database."""

    def __init__(self):
        super().__init__('AlphaFold', 'https://alphafold.ebi.ac.uk')
        self.api_url = 'https://alphafold.ebi.ac.uk/api'
        self.files_url = 'https://alphafold.ebi.ac.uk/files'

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Search AlphaFold by UniProt accession or gene name.

        Examples:
            - "P12345" - UniProt accession
            - "AF-P12345-F1" - AlphaFold ID
            - "BRCA1" - gene name (will search via UniProt first)
        """
        results = []

        try:
            # If query looks like a UniProt ID or AlphaFold ID
            if query.startswith('AF-') or (len(query) >= 6 and query[0].isalpha()):
                uniprot_id = query.replace('AF-', '').split('-')[0]

                # Get prediction from AlphaFold
                url = f"{self.api_url}/prediction/{uniprot_id}"
                try:
                    response = self._safe_request(url)
                    data = response.json()

                    if isinstance(data, list):
                        data = data[0] if data else {}

                    results.append({
                        'id': data.get('entryId', uniprot_id),
                        'uniprot_id': uniprot_id,
                        'title': f"AlphaFold prediction for {data.get('uniprotDescription', uniprot_id)}",
                        'description': data.get('uniprotDescription', ''),
                        'organism': data.get('organismScientificName', 'N/A'),
                        'gene': data.get('gene', 'N/A'),
                        'model_version': data.get('latestVersion', 'N/A'),
                        'confidence': 'Model confidence available',
                        'database': 'AlphaFold',
                        'url': f"https://alphafold.ebi.ac.uk/entry/{data.get('entryId', uniprot_id)}"
                    })
                except:
                    pass

            return results[:limit]

        except Exception as e:
            logger.error(f"AlphaFold search failed: {e}", exc_info=True)
            return []

    def fetch_file(self, identifier: str, file_format: str = 'pdb') -> Tuple[bytes, str]:
        """
        Fetch an AlphaFold prediction file.

        Supported formats:
            - pdb: PDB format
            - cif: mmCIF format
        """
        # Extract UniProt ID from AlphaFold ID
        uniprot_id = identifier.replace('AF-', '').split('-')[0]

        format_map = {
            'pdb': 'pdb',
            'cif': 'cif',
            'mmcif': 'cif'
        }

        ext = format_map.get(file_format.lower(), 'pdb')

        # First check the prediction API to get the correct version and verify entry exists
        model_version = 4
        try:
            meta_url = f"{self.api_url}/prediction/{uniprot_id}"
            meta_resp = self.session.get(meta_url, timeout=15)
            if meta_resp.status_code == 404:
                raise ValueError(
                    f"No AlphaFold prediction exists for '{uniprot_id}'. "
                    f"Verify the UniProt accession at https://www.uniprot.org/uniprotkb/{uniprot_id}"
                )
            if meta_resp.ok:
                meta_data = meta_resp.json()
                if isinstance(meta_data, list) and meta_data:
                    meta_data = meta_data[0]
                model_version = meta_data.get('latestVersion', 4)
        except ValueError:
            raise
        except Exception:
            pass  # Fall through to try v4 default

        # AlphaFold file naming: AF-{UNIPROT_ID}-F1-model_v{VERSION}.{ext}
        url = f"{self.files_url}/AF-{uniprot_id}-F1-model_v{model_version}.{ext}"

        try:
            response = self.session.get(url, timeout=30)
            if response.status_code == 404:
                raise ValueError(
                    f"AlphaFold structure file not found for '{uniprot_id}' (v{model_version}). "
                    f"This protein may not have a predicted structure available."
                )
            response.raise_for_status()
            filename = f"AF-{uniprot_id}-F1-model_v{model_version}.{ext}"
            return response.content, filename

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch AlphaFold file {identifier}: {e}")
            raise ValueError(
                f"Could not download AlphaFold structure for '{uniprot_id}'. "
                f"The server may be temporarily unavailable."
            )

    def get_metadata(self, identifier: str) -> Dict:
        """Get detailed metadata for an AlphaFold entry."""
        try:
            # Extract UniProt ID
            uniprot_id = identifier.replace('AF-', '').split('-')[0]
            
            url = f"{self.api_url}/prediction/{uniprot_id}"
            response = self._safe_request(url)
            data = response.json()
            
            if isinstance(data, list) and data:
                data = data[0]
            
            return {
                'id': identifier,
                'uniprot_id': uniprot_id,
                'title': f"AlphaFold prediction for {data.get('uniprotDescription', uniprot_id)}",
                'description': data.get('uniprotDescription', ''),
                'organism': data.get('organismScientificName', 'N/A'),
                'gene': data.get('gene', 'N/A'),
                'model_version': data.get('latestVersion', 'N/A'),
                'confidence': data.get('avgPlddt', 85),
                'sequence_length': data.get('uniprotEnd', 'N/A'),
                'all_properties': data # Keep everything else
            }
        except Exception as e:
            logger.error(f"Failed to get AlphaFold metadata for {identifier}: {e}")
            return {'id': identifier, 'uniprot_id': identifier.replace('AF-', '').split('-')[0]}


# ============================================================================
# UNIPROT (PROTEIN SEQUENCES)
# ============================================================================

class UniProtConnector(DatabaseConnector):
    """Connector for UniProt protein sequence database."""

    def __init__(self):
        super().__init__('UniProt', 'https://rest.uniprot.org')
        self.search_url = 'https://rest.uniprot.org/uniprotkb/search'

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Search UniProt by protein name, gene, organism, etc.

        Examples:
            - "insulin" - protein name
            - "BRCA1" - gene name
            - "P12345" - UniProt accession
        """
        try:
            params = {
                'query': query,
                'format': 'json',
                'size': limit
            }

            response = self._safe_request(f"{self.search_url}?{requests.compat.urlencode(params)}")
            data = response.json()

            results = []
            for entry in data.get('results', []):
                protein_name = entry.get('proteinDescription', {}).get('recommendedName', {}).get('fullName', {}).get('value', 'N/A')
                organism = entry.get('organism', {}).get('scientificName', 'N/A')
                gene_names = entry.get('genes', [])
                gene = gene_names[0].get('geneName', {}).get('value', 'N/A') if gene_names else 'N/A'

                results.append({
                    'id': entry.get('primaryAccession', ''),
                    'title': protein_name,
                    'description': f"{protein_name} from {organism}",
                    'organism': organism,
                    'gene': gene,
                    'sequence_length': entry.get('sequence', {}).get('length', 'N/A'),
                    'database': 'UniProt',
                    'url': f"https://www.uniprot.org/uniprotkb/{entry.get('primaryAccession', '')}"
                })

            return results

        except Exception as e:
            logger.error(f"UniProt search failed: {e}", exc_info=True)
            return []

    def fetch_file(self, uniprot_id: str, file_format: str = 'fasta') -> Tuple[bytes, str]:
        """
        Fetch a UniProt entry.

        Supported formats:
            - fasta: FASTA sequence
            - txt: UniProt text format
            - xml: UniProt XML
        """
        format_map = {
            'fasta': 'fasta',
            'txt': 'txt',
            'xml': 'xml'
        }

        ext = format_map.get(file_format.lower(), 'fasta')
        url = f"https://rest.uniprot.org/uniprotkb/{uniprot_id}.{ext}"

        try:
            response = self._safe_request(url)
            filename = f"{uniprot_id}.{ext}"
            return response.content, filename

        except Exception as e:
            logger.error(f"Failed to fetch UniProt entry {uniprot_id}: {e}")
            raise

    def get_metadata(self, uniprot_id: str) -> Dict:
        """Get detailed metadata for a UniProt entry."""
        try:
            url = f"https://rest.uniprot.org/uniprotkb/{uniprot_id}.json"
            response = self._safe_request(url)
            entry = response.json()

            protein_name = entry.get('proteinDescription', {}).get('recommendedName', {}).get('fullName', {}).get('value', 'N/A')
            organism = entry.get('organism', {}).get('scientificName', 'N/A')
            gene_names = entry.get('genes', [])
            gene = gene_names[0].get('geneName', {}).get('value', 'N/A') if gene_names else 'N/A'
            
            sequence = entry.get('sequence', {}).get('value', '')

            return {
                'id': uniprot_id,
                'title': protein_name,
                'description': f"{protein_name} from {organism}",
                'organism': organism,
                'gene_name': gene,
                'sequence_length': entry.get('sequence', {}).get('length', 'N/A'),
                'mass': entry.get('sequence', {}).get('molWeight', 'N/A'),
                'status': entry.get('entryType', 'N/A'),
                'sequence': sequence,
                'all_properties': entry
            }
        except Exception as e:
            logger.error(f"Failed to get UniProt metadata for {uniprot_id}: {e}")
            return {'id': uniprot_id, 'title': uniprot_id}


# ============================================================================
# PUBCHEM (SMALL MOLECULES)
# ============================================================================

class PubChemConnector(DatabaseConnector):
    """Connector for PubChem chemical database."""

    def __init__(self):
        super().__init__('PubChem', 'https://pubchem.ncbi.nlm.nih.gov')
        self.api_url = 'https://pubchem.ncbi.nlm.nih.gov/rest/pug'

    def search(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Search PubChem by compound name, formula, or CID.

        Examples:
            - "aspirin" - compound name
            - "C9H8O4" - molecular formula
            - "2244" - PubChem CID
        """
        try:
            # Search by name
            url = f"{self.api_url}/compound/name/{query}/cids/JSON"

            try:
                response = self._safe_request(url)
                data = response.json()
                cids = data.get('IdentifierList', {}).get('CID', [])[:limit]
            except:
                # If name search fails, try as CID
                if query.isdigit():
                    cids = [int(query)]
                else:
                    return []

            results = []
            for cid in cids:
                try:
                    # Get compound details
                    details_url = f"{self.api_url}/compound/cid/{cid}/property/MolecularFormula,MolecularWeight,IUPACName,Title/JSON"
                    details_response = self._safe_request(details_url)
                    details = details_response.json()

                    props = details.get('PropertyTable', {}).get('Properties', [{}])[0]

                    results.append({
                        'id': str(cid),
                        'title': props.get('Title', f"CID {cid}"),
                        'description': props.get('IUPACName', 'N/A'),
                        'formula': props.get('MolecularFormula', 'N/A'),
                        'molecular_weight': props.get('MolecularWeight', 'N/A'),
                        'database': 'PubChem',
                        'url': f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
                    })
                except:
                    continue

            return results

        except Exception as e:
            logger.error(f"PubChem search failed: {e}", exc_info=True)
            return []

    def fetch_file(self, cid: str, file_format: str = 'sdf') -> Tuple[bytes, str]:
        """
        Fetch a PubChem compound structure.

        Supported formats:
            - sdf: SDF format
            - pdb: PDB format (3D)
            - mol: MOL format
            - smiles: SMILES string
        """
        format_map = {
            'sdf': 'SDF',
            'pdb': 'PDB',
            'mol': 'SDF',
            'smiles': 'SMILES'
        }

        record_type = '3d' if file_format.lower() == 'pdb' else 'record'
        fmt = format_map.get(file_format.lower(), 'SDF')

        url = f"{self.api_url}/compound/cid/{cid}/{record_type}/{fmt}"

        try:
            response = self._safe_request(url)
            ext = file_format.lower()
            if ext == 'smiles':
                ext = 'smi'
            filename = f"CID_{cid}.{ext}"
            return response.content, filename

        except Exception as e:
            logger.error(f"Failed to fetch PubChem compound {cid}: {e}")
            raise

    def get_metadata(self, cid: str) -> Dict:
        """Get detailed metadata for a PubChem entry."""
        try:
            # Get properties
            props_url = f"{self.api_url}/compound/cid/{cid}/property/MolecularFormula,MolecularWeight,CanonicalSMILES,IsomericSMILES,InChIKey,IUPACName,XLogP,TPSA,HBondDonorCount,HBondAcceptorCount,Title/JSON"
            response = self._safe_request(props_url)
            data = response.json()
            props = data.get('PropertyTable', {}).get('Properties', [{}])[0]
            
            # Get description
            desc_url = f"{self.api_url}/compound/cid/{cid}/description/JSON"
            try:
                desc_res = self.session.get(desc_url, timeout=10)
                desc_data = desc_res.json()
                description = desc_data.get('InformationList', {}).get('Information', [{}])[0].get('Description', '')
            except:
                description = props.get('IUPACName', '')

            return {
                'id': cid,
                'title': props.get('Title', f"CID {cid}"),
                'description': description,
                'molecular_formula': props.get('MolecularFormula', 'N/A'),
                'molecular_weight': props.get('MolecularWeight', 'N/A'),
                'isomeric_smiles': props.get('IsomericSMILES', props.get('CanonicalSMILES', 'N/A')),
                'xlogp': props.get('XLogP', 'N/A'),
                'tpsa': props.get('TPSA', 'N/A'),
                'h_bond_donor_count': props.get('HBondDonorCount', 'N/A'),
                'h_bond_acceptor_count': props.get('HBondAcceptorCount', 'N/A'),
                'inchikey': props.get('InChIKey', 'N/A'),
                'all_properties': props
            }
        except Exception as e:
            logger.error(f"Failed to get PubChem metadata for {cid}: {e}")
            return {'id': cid, 'title': f"CID {cid}"}


# ============================================================================
# DATABASE MANAGER
# ============================================================================

class PublicDatabaseManager:
    """Manager for all public database connectors."""

    def __init__(self):
        self.databases = {
            'pdb': PDBConnector(),
            'alphafold': AlphaFoldConnector(),
            'uniprot': UniProtConnector(),
            'pubchem': PubChemConnector()
        }

    def get_connector(self, database: str) -> DatabaseConnector:
        """Get a database connector by name."""
        return self.databases.get(database.lower())

    def search(self, database: str, query: str, limit: int = 20) -> List[Dict]:
        """Search a public database."""
        connector = self.get_connector(database)
        if not connector:
            raise ValueError(f"Unknown database: {database}")

        return connector.search(query, limit)

    def fetch_file(self, database: str, identifier: str, file_format: str = None) -> Tuple[bytes, str]:
        """Fetch a file from a public database."""
        connector = self.get_connector(database)
        if not connector:
            raise ValueError(f"Unknown database: {database}")

        return connector.fetch_file(identifier, file_format)

    def get_available_databases(self) -> List[Dict]:
        """Get list of available databases with descriptions."""
        return [
            {
                'id': 'pdb',
                'name': 'Protein Data Bank (PDB)',
                'description': 'Crystal structures of proteins, nucleic acids, and complex assemblies',
                'icon': '🧬',
                'formats': ['pdb', 'cif', 'xml']
            },
            {
                'id': 'alphafold',
                'name': 'AlphaFold Database',
                'description': 'AI-predicted protein structures from DeepMind',
                'icon': '🤖',
                'formats': ['pdb', 'cif']
            },
            {
                'id': 'uniprot',
                'name': 'UniProt',
                'description': 'Comprehensive protein sequence and annotation database',
                'icon': '📊',
                'formats': ['fasta', 'txt', 'xml']
            },
            {
                'id': 'pubchem',
                'name': 'PubChem',
                'description': 'Chemical structures and their biological activities',
                'icon': '⚗️',
                'formats': ['sdf', 'pdb', 'mol', 'smiles']
            }
        ]
