"""
External Database Integration for Phonos
Integrates AlphaFold2, UniProt, PDB, and other bioinformatics databases
"""

import requests
import time
import logging
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from functools import lru_cache
import json
import gzip
import io

logger = logging.getLogger(__name__)


class ExternalDBError(Exception):
    """Base exception for external database errors"""
    pass


class RateLimitError(ExternalDBError):
    """Raised when API rate limit is exceeded"""
    pass


class NotFoundError(ExternalDBError):
    """Raised when entry is not found in external database"""
    pass


class ExternalDatabaseClient:
    """Base class for external database clients"""

    def __init__(self, cache_ttl: int = 86400, max_retries: int = 3):
        self.cache_ttl = cache_ttl  # 24 hours default
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Phonos/1.0 (https://phonos.app; contact@phonos.app)'
        })

    def _make_request(
        self,
        url: str,
        method: str = 'GET',
        **kwargs
    ) -> requests.Response:
        """Make HTTP request with retry logic"""
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    logger.warning(f"Rate limited. Waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response

            except requests.exceptions.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise ExternalDBError(f"Request failed after {self.max_retries} attempts: {e}")
                time.sleep(2 ** attempt)  # Exponential backoff

        raise ExternalDBError("Max retries exceeded")


class AlphaFoldDBClient(ExternalDatabaseClient):
    """
    Client for AlphaFold Protein Structure Database
    API: https://alphafold.ebi.ac.uk/api/
    """

    BASE_URL = "https://alphafold.ebi.ac.uk/api"
    FILES_URL = "https://alphafold.ebi.ac.uk/files"

    def search_by_uniprot_id(self, uniprot_id: str) -> Dict[str, Any]:
        """
        Search AlphaFold DB by UniProt ID

        Args:
            uniprot_id: UniProt accession (e.g., 'P12345')

        Returns:
            Protein structure prediction metadata
        """
        url = f"{self.BASE_URL}/prediction/{uniprot_id}"

        try:
            response = self._make_request(url)
            data = response.json()

            return {
                'source': 'alphafold',
                'uniprot_id': uniprot_id,
                'entry_id': data[0]['entryId'],
                'gene': data[0].get('gene'),
                'organism': data[0].get('uniprotDescription'),
                'model_version': data[0].get('latestVersion'),
                'model_date': data[0].get('modelCreatedDate'),
                'confidence': self._parse_confidence(data[0]),
                'pdb_url': f"{self.FILES_URL}/AF-{uniprot_id}-F1-model_v{data[0].get('latestVersion', 4)}.pdb",
                'cif_url': f"{self.FILES_URL}/AF-{uniprot_id}-F1-model_v{data[0].get('latestVersion', 4)}.cif",
                'pae_url': f"{self.FILES_URL}/AF-{uniprot_id}-F1-predicted_aligned_error_v{data[0].get('latestVersion', 4)}.json",
                'metadata': data[0]
            }

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise NotFoundError(f"No AlphaFold prediction found for {uniprot_id}")
            raise ExternalDBError(f"AlphaFold API error: {e}")

    def download_structure(self, uniprot_id: str, format: str = 'pdb') -> bytes:
        """
        Download structure file

        Args:
            uniprot_id: UniProt accession
            format: 'pdb' or 'cif'

        Returns:
            Structure file content as bytes
        """
        info = self.search_by_uniprot_id(uniprot_id)
        url = info['pdb_url'] if format == 'pdb' else info['cif_url']

        response = self._make_request(url)
        return response.content

    def get_confidence_metrics(self, uniprot_id: str) -> Dict[str, Any]:
        """Get pLDDT confidence scores and PAE data"""
        info = self.search_by_uniprot_id(uniprot_id)

        try:
            pae_response = self._make_request(info['pae_url'])
            pae_data = pae_response.json()

            return {
                'has_pae': True,
                'pae_data': pae_data,
                'max_pae': max(max(row) for row in pae_data[0]['predicted_aligned_error']),
                'mean_plddt': pae_data[0].get('plddt', {}).get('mean')
            }
        except Exception as e:
            logger.warning(f"Could not fetch PAE data for {uniprot_id}: {e}")
            return {'has_pae': False}

    @staticmethod
    def _parse_confidence(data: Dict) -> Dict[str, Any]:
        """Parse confidence information from AlphaFold metadata"""
        return {
            'plddt_available': 'plddt' in data,
            'pae_available': 'pae' in data,
            'global_metric': data.get('globalMetricValue')
        }


class UniProtClient(ExternalDatabaseClient):
    """
    Client for UniProt Protein Database
    API: https://www.uniprot.org/help/api
    """

    BASE_URL = "https://rest.uniprot.org/uniprotkb"

    def search_by_id(self, uniprot_id: str) -> Dict[str, Any]:
        """
        Get protein information by UniProt ID

        Args:
            uniprot_id: UniProt accession

        Returns:
            Protein metadata
        """
        url = f"{self.BASE_URL}/{uniprot_id}.json"

        try:
            response = self._make_request(url)
            data = response.json()

            return {
                'source': 'uniprot',
                'uniprot_id': uniprot_id,
                'protein_name': data.get('proteinDescription', {}).get('recommendedName', {}).get('fullName', {}).get('value'),
                'gene_name': self._extract_gene_name(data),
                'organism': data.get('organism', {}).get('scientificName'),
                'organism_id': data.get('organism', {}).get('taxonId'),
                'length': data.get('sequence', {}).get('length'),
                'sequence': data.get('sequence', {}).get('value'),
                'mass': data.get('sequence', {}).get('molWeight'),
                'function': self._extract_function(data),
                'keywords': [kw.get('name') for kw in data.get('keywords', [])],
                'go_terms': self._extract_go_terms(data),
                'cross_references': self._extract_xrefs(data),
                'metadata': data
            }

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise NotFoundError(f"UniProt entry {uniprot_id} not found")
            raise ExternalDBError(f"UniProt API error: {e}")

    def search_by_name(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Search proteins by name or keyword

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching proteins
        """
        url = f"{self.BASE_URL}/search"
        params = {
            'query': query,
            'format': 'json',
            'size': limit
        }

        response = self._make_request(url, params=params)
        data = response.json()

        return [
            {
                'uniprot_id': result.get('primaryAccession'),
                'protein_name': result.get('proteinDescription', {}).get('recommendedName', {}).get('fullName', {}).get('value'),
                'gene_name': self._extract_gene_name(result),
                'organism': result.get('organism', {}).get('scientificName'),
                'reviewed': result.get('entryType') == 'UniProtKB reviewed (Swiss-Prot)'
            }
            for result in data.get('results', [])
        ]

    def get_sequence(self, uniprot_id: str, format: str = 'fasta') -> str:
        """Get protein sequence in specified format"""
        url = f"{self.BASE_URL}/{uniprot_id}.{format}"
        response = self._make_request(url)
        return response.text

    @staticmethod
    def _extract_gene_name(data: Dict) -> Optional[str]:
        """Extract primary gene name"""
        genes = data.get('genes', [])
        if genes and len(genes) > 0:
            gene_names = genes[0].get('geneName', {})
            return gene_names.get('value')
        return None

    @staticmethod
    def _extract_function(data: Dict) -> Optional[str]:
        """Extract function description"""
        comments = data.get('comments', [])
        for comment in comments:
            if comment.get('commentType') == 'FUNCTION':
                texts = comment.get('texts', [])
                if texts:
                    return texts[0].get('value')
        return None

    @staticmethod
    def _extract_go_terms(data: Dict) -> List[Dict[str, str]]:
        """Extract Gene Ontology terms"""
        go_terms = []
        xrefs = data.get('uniProtKBCrossReferences', [])
        for xref in xrefs:
            if xref.get('database') == 'GO':
                go_terms.append({
                    'go_id': xref.get('id'),
                    'term': xref.get('properties', [{}])[0].get('value')
                })
        return go_terms

    @staticmethod
    def _extract_xrefs(data: Dict) -> Dict[str, List[str]]:
        """Extract cross-references to other databases"""
        xrefs = {}
        for xref in data.get('uniProtKBCrossReferences', []):
            db = xref.get('database')
            if db not in xrefs:
                xrefs[db] = []
            xrefs[db].append(xref.get('id'))
        return xrefs


class PDBClient(ExternalDatabaseClient):
    """
    Client for Protein Data Bank (RCSB PDB)
    API: https://data.rcsb.org/
    """

    BASE_URL = "https://data.rcsb.org/rest/v1/core"
    SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
    FILES_URL = "https://files.rcsb.org/download"

    def search_by_id(self, pdb_id: str) -> Dict[str, Any]:
        """
        Get structure information by PDB ID

        Args:
            pdb_id: PDB identifier (e.g., '1ABC')

        Returns:
            Structure metadata
        """
        pdb_id = pdb_id.upper()
        url = f"{self.BASE_URL}/entry/{pdb_id}"

        try:
            response = self._make_request(url)
            data = response.json()

            return {
                'source': 'pdb',
                'pdb_id': pdb_id,
                'title': data.get('struct', {}).get('title'),
                'experimental_method': data.get('exptl', [{}])[0].get('method'),
                'resolution': self._extract_resolution(data),
                'release_date': data.get('rcsb_accession_info', {}).get('initial_release_date'),
                'deposition_date': data.get('rcsb_accession_info', {}).get('deposit_date'),
                'authors': self._extract_authors(data),
                'organism': self._extract_organism(data),
                'chains': self._extract_chains(data),
                'pdb_url': f"{self.FILES_URL}/{pdb_id}.pdb",
                'cif_url': f"{self.FILES_URL}/{pdb_id}.cif",
                'metadata': data
            }

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise NotFoundError(f"PDB entry {pdb_id} not found")
            raise ExternalDBError(f"PDB API error: {e}")

    def search_by_uniprot(self, uniprot_id: str) -> List[Dict[str, Any]]:
        """
        Find PDB structures for a UniProt ID

        Args:
            uniprot_id: UniProt accession

        Returns:
            List of PDB structures
        """
        query = {
            "query": {
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                    "operator": "exact_match",
                    "value": uniprot_id
                }
            },
            "return_type": "entry",
            "request_options": {
                "return_all_hits": True
            }
        }

        response = self._make_request(
            self.SEARCH_URL,
            method='POST',
            json=query,
            headers={'Content-Type': 'application/json'}
        )

        results = response.json()
        pdb_ids = [hit['identifier'] for hit in results.get('result_set', [])]

        return [self.search_by_id(pdb_id) for pdb_id in pdb_ids[:10]]  # Limit to 10

    def download_structure(self, pdb_id: str, format: str = 'pdb') -> bytes:
        """Download structure file"""
        pdb_id = pdb_id.upper()
        url = f"{self.FILES_URL}/{pdb_id}.{format}"
        response = self._make_request(url)
        return response.content

    @staticmethod
    def _extract_resolution(data: Dict) -> Optional[float]:
        """Extract resolution value"""
        refine = data.get('refine', [])
        if refine and len(refine) > 0:
            return refine[0].get('ls_d_res_high')
        return None

    @staticmethod
    def _extract_authors(data: Dict) -> List[str]:
        """Extract author list"""
        audit_author = data.get('audit_author', [])
        return [author.get('name') for author in audit_author]

    @staticmethod
    def _extract_organism(data: Dict) -> Optional[str]:
        """Extract source organism"""
        entity_src_gen = data.get('entity_src_gen', [])
        if entity_src_gen:
            return entity_src_gen[0].get('pdbx_gene_src_scientific_name')
        return None

    @staticmethod
    def _extract_chains(data: Dict) -> List[str]:
        """Extract chain identifiers"""
        struct_asym = data.get('struct_asym', [])
        return [asym.get('id') for asym in struct_asym]


class NCBIClient(ExternalDatabaseClient):
    """
    Client for NCBI databases (requires API key for higher rate limits)
    API: https://www.ncbi.nlm.nih.gov/home/develop/api/
    """

    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, api_key: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key
        if api_key:
            self.session.params = {'api_key': api_key}

    def search_protein(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Search NCBI Protein database"""
        # Search for IDs
        search_url = f"{self.BASE_URL}/esearch.fcgi"
        params = {
            'db': 'protein',
            'term': query,
            'retmax': limit,
            'retmode': 'json'
        }

        response = self._make_request(search_url, params=params)
        data = response.json()

        id_list = data.get('esearchresult', {}).get('idlist', [])

        # Fetch summaries
        if not id_list:
            return []

        return self.fetch_protein_summaries(id_list)

    def fetch_protein_summaries(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Fetch protein summaries by IDs"""
        summary_url = f"{self.BASE_URL}/esummary.fcgi"
        params = {
            'db': 'protein',
            'id': ','.join(ids),
            'retmode': 'json'
        }

        response = self._make_request(summary_url, params=params)
        data = response.json()

        results = []
        for uid, info in data.get('result', {}).items():
            if uid == 'uids':
                continue
            results.append({
                'source': 'ncbi',
                'ncbi_id': uid,
                'accession': info.get('accessionversion'),
                'title': info.get('title'),
                'organism': info.get('organism'),
                'length': info.get('slen')
            })

        return results


class ExternalDBManager:
    """
    Unified manager for all external databases with caching
    """

    def __init__(self, db_manager=None, ncbi_api_key: Optional[str] = None):
        self.alphafold = AlphaFoldDBClient()
        self.uniprot = UniProtClient()
        self.pdb = PDBClient()
        self.ncbi = NCBIClient(api_key=ncbi_api_key)
        self.db_manager = db_manager

    def search_protein(
        self,
        query: str,
        databases: Optional[List[str]] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Search across multiple databases

        Args:
            query: Search query (UniProt ID, gene name, etc.)
            databases: List of databases to search ['uniprot', 'pdb', 'ncbi']

        Returns:
            Dict with results from each database
        """
        if databases is None:
            databases = ['uniprot', 'pdb', 'alphafold']

        results = {}

        # Try as UniProt ID first
        if 'uniprot' in databases:
            try:
                results['uniprot'] = [self.uniprot.search_by_id(query)]
            except NotFoundError:
                # Try search
                results['uniprot'] = self.uniprot.search_by_name(query)
            except Exception as e:
                logger.error(f"UniProt search failed: {e}")
                results['uniprot'] = []

        # AlphaFold (requires UniProt ID)
        if 'alphafold' in databases:
            try:
                results['alphafold'] = [self.alphafold.search_by_uniprot_id(query)]
            except (NotFoundError, ExternalDBError) as e:
                logger.debug(f"AlphaFold not found: {e}")
                results['alphafold'] = []

        # PDB
        if 'pdb' in databases:
            try:
                # Try as PDB ID
                if len(query) == 4:
                    results['pdb'] = [self.pdb.search_by_id(query)]
                else:
                    # Try as UniProt ID
                    results['pdb'] = self.pdb.search_by_uniprot(query)
            except Exception as e:
                logger.error(f"PDB search failed: {e}")
                results['pdb'] = []

        return results

    def get_all_structures_for_protein(
        self,
        uniprot_id: str
    ) -> Dict[str, Any]:
        """
        Get all available structures for a protein

        Args:
            uniprot_id: UniProt accession

        Returns:
            Comprehensive structure information
        """
        result = {
            'uniprot_id': uniprot_id,
            'protein_info': None,
            'alphafold': None,
            'experimental_structures': []
        }

        # Get UniProt info
        try:
            result['protein_info'] = self.uniprot.search_by_id(uniprot_id)
        except Exception as e:
            logger.error(f"Could not fetch UniProt info: {e}")

        # Get AlphaFold prediction
        try:
            result['alphafold'] = self.alphafold.search_by_uniprot_id(uniprot_id)
        except NotFoundError:
            logger.info(f"No AlphaFold structure for {uniprot_id}")

        # Get experimental structures from PDB
        try:
            result['experimental_structures'] = self.pdb.search_by_uniprot(uniprot_id)
        except Exception as e:
            logger.error(f"Could not fetch PDB structures: {e}")

        return result

    def import_structure_to_project(
        self,
        project_id: int,
        user_id: int,
        source: str,
        identifier: str,
        format: str = 'pdb'
    ) -> Dict[str, Any]:
        """
        Import structure from external database to project

        Args:
            project_id: Project ID
            user_id: User ID
            source: 'alphafold', 'pdb', 'uniprot'
            identifier: ID in external database
            format: File format ('pdb' or 'cif')

        Returns:
            Import result with file metadata
        """
        # Download structure
        if source == 'alphafold':
            content = self.alphafold.download_structure(identifier, format)
            filename = f"AF-{identifier}.{format}"
            metadata = self.alphafold.search_by_uniprot_id(identifier)

        elif source == 'pdb':
            content = self.pdb.download_structure(identifier, format)
            filename = f"{identifier}.{format}"
            metadata = self.pdb.search_by_id(identifier)

        else:
            raise ExternalDBError(f"Unsupported source: {source}")

        # Store in database if db_manager available
        if self.db_manager:
            # This would integrate with your file storage system
            # You'd save to files table and link to project
            pass

        return {
            'filename': filename,
            'size': len(content),
            'source': source,
            'identifier': identifier,
            'metadata': metadata,
            'content': content  # Or save to file system
        }
