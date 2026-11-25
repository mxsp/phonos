"""Projects resource for Phonos SDK."""
from typing import Optional, Dict, Any, List


class ProjectsResource:
    """Manage projects."""

    def __init__(self, client):
        self.client = client

    def list(self) -> List[Dict[str, Any]]:
        """List all projects.

        Returns:
            List of project dicts

        Example:
            >>> projects = client.projects.list()
            >>> for project in projects:
            ...     print(f"{project['id']}: {project['name']}")
        """
        response = self.client._request('GET', '/api/projects')
        return response.get('projects', [])

    def create(
        self,
        name: str,
        description: Optional[str] = None,
        team_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Create a new project.

        Args:
            name: Project name
            description: Project description
            team_id: Team ID (if creating under a team)

        Returns:
            Created project dict

        Example:
            >>> project = client.projects.create(
            ...     name="My Research Project",
            ...     description="Protein folding analysis"
            ... )
        """
        data = {'name': name}
        if description:
            data['description'] = description
        if team_id:
            data['team_id'] = team_id

        return self.client._request('POST', '/api/projects', json=data)

    def get(self, project_id: int) -> Dict[str, Any]:
        """Get project details (via listing).

        Args:
            project_id: Project ID

        Returns:
            Project dict
        """
        projects = self.list()
        for project in projects:
            if project.get('id') == project_id:
                return project
        raise ValueError(f"Project {project_id} not found")

    def update(
        self,
        project_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update a project.

        Args:
            project_id: Project ID
            name: New project name
            description: New project description

        Returns:
            Response dict

        Example:
            >>> client.projects.update(
            ...     project_id=1,
            ...     description="Updated description"
            ... )
        """
        data = {}
        if name:
            data['name'] = name
        if description:
            data['description'] = description

        return self.client._request(
            'PATCH',
            f'/api/projects/{project_id}',
            json=data
        )

    def delete(self, project_id: int) -> Dict[str, Any]:
        """Delete a project.

        Args:
            project_id: Project ID

        Returns:
            Response dict

        Example:
            >>> client.projects.delete(1)
        """
        return self.client._request('DELETE', f'/api/projects/{project_id}')

    def invite_user(
        self,
        project_id: int,
        email: str,
        role: str = "Viewer"
    ) -> Dict[str, Any]:
        """Invite a user to a project.

        Args:
            project_id: Project ID
            email: User email to invite
            role: Role to assign (Project Manager, Contributor, Viewer)

        Returns:
            Response dict

        Example:
            >>> client.projects.invite_user(
            ...     project_id=1,
            ...     email="user@example.com",
            ...     role="Contributor"
            ... )
        """
        return self.client._request(
            'POST',
            f'/api/projects/{project_id}/invitations',
            json={'email': email, 'role': role}
        )

    def list_assignments(self, project_id: int) -> List[Dict[str, Any]]:
        """List project assignments (members).

        Args:
            project_id: Project ID

        Returns:
            List of assignment dicts
        """
        response = self.client._request(
            'GET',
            f'/api/projects/{project_id}/assignments'
        )
        return response.get('assignments', [])

    def remove_user(self, project_id: int, user_id: int) -> Dict[str, Any]:
        """Remove a user from a project.

        Args:
            project_id: Project ID
            user_id: User ID to remove

        Returns:
            Response dict
        """
        return self.client._request(
            'DELETE',
            f'/api/projects/{project_id}/assignments/{user_id}'
        )

    def update_user_role(
        self,
        project_id: int,
        user_id: int,
        role: str
    ) -> Dict[str, Any]:
        """Update a user's role in a project.

        Args:
            project_id: Project ID
            user_id: User ID
            role: New role

        Returns:
            Response dict
        """
        return self.client._request(
            'PATCH',
            f'/api/projects/{project_id}/assignments/{user_id}',
            json={'role': role}
        )
