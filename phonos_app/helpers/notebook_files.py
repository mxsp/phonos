"""
Phonos Notebook File Manager
Manages files within notebook workspaces (sub-filesystem for notebooks within projects).
"""
import os
import shutil
import mimetypes
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import current_app

logger = logging.getLogger(__name__)


class NotebookFileManager:
    """
    Manages files within notebook workspaces.

    Each notebook can have its own file storage, which can be:
    - Linked to a project (shared files)
    - Session-specific (temporary files during execution)
    - Notebook-specific (persistent files for this notebook)

    Security Features:
    - Path traversal prevention
    - User-project isolation
    - Scope validation for all file operations
    """

    ALLOWED_EXTENSIONS = {
        # Code files
        'py', 'ipynb', 'r', 'jl', 'sh',
        # Data files
        'json', 'csv', 'tsv', 'txt', 'md', 'yaml', 'yml', 'xml',
        # Scientific formats
        'pdb', 'cif', 'mmcif', 'fasta', 'fa', 'sdf', 'mol', 'mol2', 'xyz',
        # Wolfram Language notebooks and source
        'nb', 'wl', 'wls', 'm', 'mx', 'wxf', 'wdx',
        # Images
        'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp',
        # Documents
        'pdf', 'html',
        # Archives
        'zip', 'tar', 'gz',
        # Binary data formats (safe ones)
        'npy', 'npz', 'h5', 'hdf5', 'parquet'
    }

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file
    MAX_TOTAL_SIZE = 500 * 1024 * 1024  # 500MB per notebook

    def __init__(self, notebook_id: int, session_id: str = None, config: Dict[str, Any] = None, user_id: int = None, project_id: int = None):
        self.notebook_id = notebook_id
        self.session_id = session_id
        self.config = config or {}
        self._base_path = None
        self._session_path = None
        self.user_id = user_id
        self.project_id = project_id

    @property
    def base_path(self) -> str:
        """Get the base path for notebook files"""
        if self._base_path is None:
            notebooks_dir = self.config.get(
                'NOTEBOOK_FILES_DIR',
                current_app.config.get('NOTEBOOK_FILES_DIR', '/data/notebook_files')
            )
            self._base_path = os.path.join(notebooks_dir, str(self.notebook_id))
            os.makedirs(self._base_path, exist_ok=True)
        return self._base_path

    @property
    def session_path(self) -> Optional[str]:
        """Get the session workspace path"""
        if not self.session_id:
            return None

        if self._session_path is None:
            sessions_dir = self.config.get(
                'NOTEBOOK_WORKSPACE_BASE',
                current_app.config.get('NOTEBOOK_WORKSPACE_BASE', '/data/notebooks')
            )
            self._session_path = os.path.join(sessions_dir, f'session_{self.session_id}')

        return self._session_path

    def _validate_path(self, path: str) -> str:
        """Validate and normalize a path within the notebook workspace"""
        # Remove leading slashes and normalize
        path = path.lstrip('/').strip()

        # Prevent directory traversal - strict check
        if '..' in path or path.startswith('/'):
            raise ValueError("Invalid path: directory traversal not allowed")

        # Check for null bytes and other dangerous characters
        if '\x00' in path or '\r' in path or '\n' in path:
            raise ValueError("Invalid path: contains dangerous characters")

        # Ensure path doesn't escape the workspace using os.path.realpath
        try:
            full_path = self._get_full_path(path)
            real_base = os.path.realpath(self.base_path)
            real_full = os.path.realpath(full_path)
            if not real_full.startswith(real_base + os.sep) and real_full != real_base:
                raise ValueError("Invalid path: escape from workspace not allowed")
        except (OSError, ValueError):
            raise ValueError("Invalid path: path resolution failed")

        return path

    def _validate_path_within_scope(self, path: str, allowed_base: str) -> str:
        """Validate path is within the allowed scope (project/user isolation)"""
        path = self._validate_path(path)

        if not allowed_base:
            raise ValueError("Invalid scope: no base directory specified")

        full_path = os.path.join(allowed_base, path)

        # Ensure the resolved path is within the allowed base
        try:
            real_base = os.path.realpath(allowed_base)
            real_full = os.path.realpath(full_path)
            if not real_full.startswith(real_base + os.sep) and real_full != real_base:
                raise ValueError("Invalid path: access outside allowed scope")
        except (OSError, ValueError):
            raise ValueError("Invalid path: scope validation failed")

        return path

    def _get_full_path(self, path: str, use_session: bool = False) -> str:
        """Get full filesystem path"""
        path = self._validate_path(path)

        if use_session and self.session_path:
            return os.path.join(self.session_path, path)

        return os.path.join(self.base_path, path)

    def list_files(self, path: str = '', include_hidden: bool = False) -> List[Dict]:
        """
        List files in a directory.

        Args:
            path: Relative path within notebook workspace
            include_hidden: Include hidden files (starting with .)

        Returns:
            List of file info dicts
        """
        full_path = self._get_full_path(path)

        if not os.path.exists(full_path):
            return []

        if not os.path.isdir(full_path):
            raise ValueError(f"Not a directory: {path}")

        files = []
        try:
            for entry in os.scandir(full_path):
                if not include_hidden and entry.name.startswith('.'):
                    continue

                stat = entry.stat()

                files.append({
                    'name': entry.name,
                    'path': os.path.join(path, entry.name) if path else entry.name,
                    'is_dir': entry.is_dir(),
                    'size': stat.st_size if entry.is_file() else 0,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'created': datetime.fromtimestamp(stat.st_ctime).isoformat(),
                })

        except PermissionError:
            logger.error(f"Permission denied listing {full_path}")
            return []

        # Sort: directories first, then alphabetically
        return sorted(files, key=lambda x: (not x['is_dir'], x['name'].lower()))

    def list_session_files(self, path: str = '') -> List[Dict]:
        """List files in session workspace"""
        if not self.session_path:
            return []

        full_path = os.path.join(self.session_path, self._validate_path(path))

        if not os.path.exists(full_path):
            return []

        return self._scan_directory(full_path, path)

    def _scan_directory(self, full_path: str, relative_path: str) -> List[Dict]:
        """Scan a directory and return file info"""
        files = []
        try:
            for entry in os.scandir(full_path):
                if entry.name.startswith('.'):
                    continue

                stat = entry.stat()
                files.append({
                    'name': entry.name,
                    'path': os.path.join(relative_path, entry.name) if relative_path else entry.name,
                    'is_dir': entry.is_dir(),
                    'size': stat.st_size if entry.is_file() else 0,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        except Exception as e:
            logger.error(f"Error scanning directory: {e}")

        return sorted(files, key=lambda x: (not x['is_dir'], x['name'].lower()))

    def upload_file(self, file, path: str = '', to_session: bool = False) -> Dict:
        """
        Upload a file to notebook workspace.

        Args:
            file: File object from request.files
            path: Directory path within workspace
            to_session: If True, upload to session workspace

        Returns:
            Dict with file info
        """
        if not file or not file.filename:
            raise ValueError("No file provided")

        filename = secure_filename(file.filename)

        # Check extension
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if ext not in self.ALLOWED_EXTENSIONS:
            raise ValueError(f"File type not allowed: .{ext}")

        # Check file size
        file.seek(0, 2)
        size = file.tell()
        file.seek(0)

        if size > self.MAX_FILE_SIZE:
            raise ValueError(f"File too large. Maximum {self.MAX_FILE_SIZE // (1024*1024)}MB allowed")

        # Check total size limit
        current_size = self.get_total_size()
        if current_size + size > self.MAX_TOTAL_SIZE:
            raise ValueError(f"Notebook storage limit reached ({self.MAX_TOTAL_SIZE // (1024*1024)}MB)")

        # Determine destination
        path = self._validate_path(path)
        if to_session and self.session_path:
            dest_dir = os.path.join(self.session_path, path)
        else:
            dest_dir = os.path.join(self.base_path, path)

        os.makedirs(dest_dir, exist_ok=True)

        # Save file
        dest_path = os.path.join(dest_dir, filename)

        # Don't overwrite without explicit request
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(dest_path):
                filename = f"{base}_{counter}{ext}"
                dest_path = os.path.join(dest_dir, filename)
                counter += 1

        file.save(dest_path)

        # Get mime type
        mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        return {
            'name': filename,
            'path': os.path.join(path, filename) if path else filename,
            'size': size,
            'mime_type': mime_type,
            'in_session': to_session
        }

    def create_directory(self, path: str) -> Dict:
        """Create a directory in notebook workspace"""
        path = self._validate_path(path)

        if not path:
            raise ValueError("Path required")

        full_path = os.path.join(self.base_path, path)

        if os.path.exists(full_path):
            if os.path.isdir(full_path):
                return {'path': path, 'exists': True}
            raise ValueError("A file with that name already exists")

        os.makedirs(full_path, exist_ok=True)

        return {'path': path, 'created': True}

    def delete(self, path: str, from_session: bool = False) -> bool:
        """
        Delete a file or directory.

        Args:
            path: Path to delete
            from_session: If True, delete from session workspace

        Returns:
            True if deleted
        """
        path = self._validate_path(path)

        if not path:
            raise ValueError("Cannot delete root directory")

        full_path = self._get_full_path(path, use_session=from_session)

        if not os.path.exists(full_path):
            return False

        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)

        logger.info(f"Deleted: {full_path}")
        return True

    def rename(self, old_path: str, new_name: str) -> Dict:
        """Rename a file or directory"""
        old_path = self._validate_path(old_path)
        new_name = secure_filename(new_name)

        if not old_path or not new_name:
            raise ValueError("Invalid path or name")

        full_old_path = os.path.join(self.base_path, old_path)

        if not os.path.exists(full_old_path):
            raise FileNotFoundError(f"File not found: {old_path}")

        # New path is in same directory with new name
        parent_dir = os.path.dirname(full_old_path)
        full_new_path = os.path.join(parent_dir, new_name)

        if os.path.exists(full_new_path):
            raise ValueError("A file with that name already exists")

        os.rename(full_old_path, full_new_path)

        new_relative_path = os.path.join(os.path.dirname(old_path), new_name)

        return {
            'old_path': old_path,
            'new_path': new_relative_path,
            'new_name': new_name
        }

    def move(self, source_path: str, dest_path: str, from_session: bool = False) -> Dict:
        """Move a file or directory to another location within the workspace."""
        source_path = self._validate_path(source_path)
        dest_path = self._validate_path(dest_path)

        if not source_path:
            raise ValueError("Invalid source path")

        base = self.session_path if from_session and self.session_path else self.base_path
        full_source = os.path.join(base, source_path)
        full_dest = os.path.join(base, dest_path)

        if not os.path.exists(full_source):
            raise FileNotFoundError(f"Source not found: {source_path}")

        real_base = os.path.realpath(base)
        real_dest = os.path.realpath(full_dest)
        if not real_dest.startswith(real_base + os.sep) and real_dest != real_base:
            raise ValueError("Invalid destination: escape from workspace not allowed")

        os.makedirs(os.path.dirname(full_dest), exist_ok=True)
        if os.path.exists(full_dest):
            raise ValueError("Destination already exists")

        shutil.move(full_source, full_dest)

        return {
            'source_path': source_path,
            'dest_path': dest_path,
        }

    def duplicate(self, source_path: str, from_session: bool = False) -> Dict:
        """Duplicate a file or directory in the same directory."""
        source_path = self._validate_path(source_path)
        base = self.session_path if from_session and self.session_path else self.base_path
        full_source = os.path.join(base, source_path)

        if not os.path.exists(full_source):
            raise FileNotFoundError(f"Source not found: {source_path}")

        parent_dir = os.path.dirname(full_source)
        name = os.path.basename(full_source)
        stem, ext = os.path.splitext(name)

        counter = 1
        while True:
            new_name = f"{stem}_copy{counter}{ext}"
            full_dest = os.path.join(parent_dir, new_name)
            if not os.path.exists(full_dest):
                break
            counter += 1

        if os.path.isdir(full_source):
            shutil.copytree(full_source, full_dest)
        else:
            shutil.copy2(full_source, full_dest)

        return {
            'source_path': source_path,
            'new_path': os.path.join(os.path.dirname(source_path), new_name) if source_path else new_name,
            'new_name': new_name,
        }

    def exists(self, path: str, from_session: bool = False) -> bool:
        """Check whether a path exists within the workspace."""
        try:
            full_path = self._get_full_path(path, use_session=from_session)
            return os.path.exists(full_path)
        except ValueError:
            return False

    def search(self, query: str, path: str = '', from_session: bool = False) -> List[Dict]:
        """Search file and directory names by substring."""
        query = query.lower()
        if not query:
            return []

        base = self.session_path if from_session and self.session_path else self.base_path
        full_root = os.path.join(base, self._validate_path(path)) if path else base
        if not os.path.isdir(full_root):
            return []

        results = []
        for dirpath, dirnames, filenames in os.walk(full_root):
            # Skip hidden entries
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for name in filenames + dirnames:
                if name.startswith('.'):
                    continue
                if query in name.lower():
                    full_path = os.path.join(dirpath, name)
                    rel_path = os.path.relpath(full_path, base)
                    results.append({
                        'name': name,
                        'path': rel_path,
                        'is_dir': os.path.isdir(full_path),
                    })
        return results

    def get_download_path(self, path: str, from_session: bool = False) -> str:
        """Return the absolute filesystem path for a file download."""
        full_path = self._get_full_path(path, use_session=from_session)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"File not found: {path}")
        return full_path

    def read_file(self, path: str, from_session: bool = False) -> bytes:
        """Read file contents"""
        full_path = self._get_full_path(path, use_session=from_session)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"File not found: {path}")

        if os.path.isdir(full_path):
            raise ValueError("Cannot read directory")

        with open(full_path, 'rb') as f:
            return f.read()

    def read_text(self, path: str, from_session: bool = False, encoding: str = 'utf-8') -> str:
        """Read file as text"""
        content = self.read_file(path, from_session)
        return content.decode(encoding)

    def write_file(self, path: str, content: bytes, to_session: bool = False) -> Dict:
        """Write content to a file"""
        path = self._validate_path(path)
        full_path = self._get_full_path(path, use_session=to_session)

        # Create parent directories
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, 'wb') as f:
            f.write(content)

        return {
            'path': path,
            'size': len(content),
            'in_session': to_session
        }

    def copy_to_session(self, path: str) -> str:
        """Copy a file from notebook storage to session workspace"""
        if not self.session_path:
            raise ValueError("No active session")

        path = self._validate_path(path)
        src_path = os.path.join(self.base_path, path)

        if not os.path.exists(src_path):
            raise FileNotFoundError(f"File not found: {path}")

        # Create session directory if needed
        os.makedirs(self.session_path, exist_ok=True)

        # Copy to session with same relative path
        dest_path = os.path.join(self.session_path, path)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        if os.path.isdir(src_path):
            shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
        else:
            shutil.copy2(src_path, dest_path)

        return dest_path

    def copy_from_session(self, session_path: str, notebook_path: str = '') -> str:
        """Copy a file from session workspace to notebook storage"""
        if not self.session_path:
            raise ValueError("No active session")

        session_path = self._validate_path(session_path)
        notebook_path = self._validate_path(notebook_path) if notebook_path else ''

        src_path = os.path.join(self.session_path, session_path)

        if not os.path.exists(src_path):
            raise FileNotFoundError(f"Session file not found: {session_path}")

        # Destination in notebook storage
        filename = os.path.basename(session_path)
        dest_dir = os.path.join(self.base_path, notebook_path)
        os.makedirs(dest_dir, exist_ok=True)

        dest_path = os.path.join(dest_dir, filename)

        if os.path.isdir(src_path):
            shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
        else:
            shutil.copy2(src_path, dest_path)

        return os.path.join(notebook_path, filename) if notebook_path else filename

    def get_total_size(self) -> int:
        """Get total size of all files in notebook storage"""
        total = 0

        if os.path.exists(self.base_path):
            for dirpath, dirnames, filenames in os.walk(self.base_path):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    try:
                        total += os.path.getsize(filepath)
                    except:
                        pass

        return total

    def get_file_info(self, path: str, from_session: bool = False) -> Dict:
        """Get detailed info about a file"""
        full_path = self._get_full_path(path, use_session=from_session)

        if not os.path.exists(full_path):
            raise FileNotFoundError(f"File not found: {path}")

        stat = os.stat(full_path)
        is_dir = os.path.isdir(full_path)

        info = {
            'name': os.path.basename(path),
            'path': path,
            'is_dir': is_dir,
            'size': 0 if is_dir else stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            'created': datetime.fromtimestamp(stat.st_ctime).isoformat(),
        }

        if not is_dir:
            info['mime_type'] = mimetypes.guess_type(path)[0] or 'application/octet-stream'
            info['extension'] = path.rsplit('.', 1)[-1].lower() if '.' in path else ''

        return info

    def cleanup_session(self) -> bool:
        """Clean up session workspace"""
        if not self.session_path:
            return False

        if os.path.exists(self.session_path):
            try:
                shutil.rmtree(self.session_path)
                logger.info(f"Cleaned up session workspace: {self.session_path}")
                return True
            except Exception as e:
                logger.error(f"Failed to cleanup session: {e}")
                return False

        return True
