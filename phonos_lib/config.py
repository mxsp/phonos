"""
Configuration management for Phonos Project Library.
Handles project detection and environment setup.
"""
import os
import json
from typing import Optional, Dict, Any

class ProjectConfig:
    """Manages project configuration and environment detection."""
    
    def __init__(self, project_id: Optional[str] = None, auto_detect: bool = True):
        self.project_id = project_id
        self._project_info = None
        
        if auto_detect and not project_id:
            self._detect_project()
            
        if not self.project_id:
            raise ValueError("Could not detect project ID. Please provide project_id explicitly.")
    
    def _detect_project(self):
        """Auto-detect project ID from environment variables or context."""
        # Check environment variables
        if 'PHONOS_PROJECT_ID' in os.environ:
            self.project_id = os.environ['PHONOS_PROJECT_ID']
            return
            
        # Check notebook session metadata
        if 'PHONOS_NOTEBOOK_ID' in os.environ:
            # Look up project from notebook
            self.project_id = self._get_project_from_notebook(os.environ['PHONOS_NOTEBOOK_ID'])
            return
            
        # Check current working directory structure
        cwd = os.getcwd()
        if '/project_' in cwd:
            # Extract project ID from path
            parts = cwd.split('/')
            for part in parts:
                if part.startswith('project_'):
                    self.project_id = part.replace('project_', '')
                    return
                    
        # Check for project metadata file
        metadata_file = os.path.join(cwd, '.phonos_project.json')
        if os.path.exists(metadata_file):
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
                self.project_id = metadata.get('project_id')
                return
    
    def _get_project_from_notebook(self, notebook_id: str) -> Optional[str]:
        """Get project ID from notebook database."""
        try:
            # This would connect to the Phonos database
            # For now, return None
            return None
        except Exception:
            return None
    
    def get_project_info(self) -> Dict[str, Any]:
        """Get detailed information about the current project."""
        if self._project_info:
            return self._project_info
            
        try:
            # Query database for project info
            self._project_info = {
                'id': self.project_id,
                'name': f'Project {self.project_id}',  # Would fetch from DB
                'created_at': None,
                'member_count': 0,
                'storage_used_mb': 0
            }
        except Exception as e:
            self._project_info = {
                'id': self.project_id,
                'name': f'Project {self.project_id}',
                'error': str(e)
            }
            
        return self._project_info
    
    def get_api_endpoint(self) -> str:
        """Get the Phonos API endpoint for this project."""
        return os.environ.get('PHONOS_API_URL', 'http://localhost:5000')
    
    def get_auth_token(self) -> Optional[str]:
        """Get authentication token for API calls."""
        return os.environ.get('PHONOS_AUTH_TOKEN')
    
    def get_workspace_path(self) -> str:
        """Get the workspace path for the current project."""
        base_path = os.environ.get('PHONOS_WORKSPACE', '/data/notebooks')
        return os.path.join(base_path, f'project_{self.project_id}')
    
    def get_user_id(self) -> Optional[str]:
        """Get current user ID."""
        return os.environ.get('PHONOS_USER_ID')