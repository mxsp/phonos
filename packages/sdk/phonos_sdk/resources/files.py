"""Files resource for Phonos SDK."""
from typing import Optional, Dict, Any, List
import os


class FilesResource:
    """Manage project files."""

    def __init__(self, client):
        self.client = client

    def upload(
        self,
        project_id: int,
        file_path: str,
        file_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload a file to a project.

        Args:
            project_id: Project ID
            file_path: Local path to the file
            file_type: Optional file type

        Returns:
            Uploaded file info dict

        Example:
            >>> file_info = client.files.upload(
            ...     project_id=1,
            ...     file_path="sequences.fasta"
            ... )
            >>> print(file_info['file_id'])
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        filename = os.path.basename(file_path)

        with open(file_path, 'rb') as f:
            files = {'file': (filename, f)}
            data = {'project_id': project_id}
            if file_type:
                data['file_type'] = file_type

            return self.client._request(
                'POST',
                '/api/files/upload',
                data=data,
                files=files
            )

    def list(self, project_id: int) -> List[Dict[str, Any]]:
        """List all files in a project.

        Args:
            project_id: Project ID

        Returns:
            List of file dicts

        Example:
            >>> files = client.files.list(project_id=1)
            >>> for file in files:
            ...     print(f"{file['id']}: {file['filename']}")
        """
        response = self.client._request(
            'GET',
            f'/api/projects/{project_id}/files'
        )
        return response.get('files', [])

    def get_content(self, file_id: int) -> str:
        """Get file content.

        Args:
            file_id: File ID

        Returns:
            File content as string

        Example:
            >>> content = client.files.get_content(file_id=123)
        """
        response = self.client._request('GET', f'/api/files/{file_id}/content')
        return response.get('content', '')

    def update_content(self, file_id: int, content: str) -> Dict[str, Any]:
        """Update file content.

        Args:
            file_id: File ID
            content: New file content

        Returns:
            Response dict

        Example:
            >>> client.files.update_content(
            ...     file_id=123,
            ...     content=">seq1\\nMKTAYIAK..."
            ... )
        """
        return self.client._request(
            'PUT',
            f'/api/files/{file_id}/content',
            json={'content': content}
        )

    def rename(self, file_id: int, new_name: str) -> Dict[str, Any]:
        """Rename a file.

        Args:
            file_id: File ID
            new_name: New filename

        Returns:
            Response dict

        Example:
            >>> client.files.rename(file_id=123, new_name="sequences_v2.fasta")
        """
        return self.client._request(
            'PATCH',
            f'/api/files/{file_id}/rename',
            json={'new_name': new_name}
        )

    def delete(self, file_id: int) -> Dict[str, Any]:
        """Delete a file.

        Args:
            file_id: File ID

        Returns:
            Response dict

        Example:
            >>> client.files.delete(file_id=123)
        """
        return self.client._request('DELETE', f'/api/files/{file_id}')

    def download(self, file_id: int, output_path: str) -> None:
        """Download a file.

        Args:
            file_id: File ID
            output_path: Local path to save the file

        Example:
            >>> client.files.download(file_id=123, output_path="sequences.fasta")
        """
        url = f"{self.client.base_url}/download_file/{file_id}"
        response = self.client.session.get(url, timeout=self.client.timeout)
        response.raise_for_status()

        with open(output_path, 'wb') as f:
            f.write(response.content)

    def get_all(self) -> List[Dict[str, Any]]:
        """Get all files across all projects.

        Returns:
            List of all files

        Example:
            >>> all_files = client.files.get_all()
        """
        response = self.client._request('GET', '/api/files/all')
        return response.get('files', [])
