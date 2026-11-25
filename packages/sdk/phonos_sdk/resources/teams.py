"""Teams/Organizations resource for Phonos SDK."""
from typing import Optional, Dict, Any, List


class TeamsResource:
    """Manage teams and organizations."""

    def __init__(self, client):
        self.client = client

    def list(self) -> List[Dict[str, Any]]:
        """List all teams.

        Returns:
            List of team dicts

        Example:
            >>> teams = client.teams.list()
            >>> for team in teams:
            ...     print(f"{team['id']}: {team['name']}")
        """
        response = self.client._request('GET', '/api/teams')
        return response.get('teams', [])

    def create(
        self,
        name: str,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new team.

        Args:
            name: Team name
            description: Team description

        Returns:
            Created team dict

        Example:
            >>> team = client.teams.create(
            ...     name="Research Lab",
            ...     description="Computational biology research"
            ... )
        """
        data = {'name': name}
        if description:
            data['description'] = description

        return self.client._request('POST', '/api/teams', json=data)

    def get(self, team_id: int) -> Dict[str, Any]:
        """Get team details.

        Args:
            team_id: Team ID

        Returns:
            Team dict
        """
        return self.client._request('GET', f'/api/teams/{team_id}')

    def list_members(self, team_id: int) -> List[Dict[str, Any]]:
        """List team members.

        Args:
            team_id: Team ID

        Returns:
            List of member dicts

        Example:
            >>> members = client.teams.list_members(team_id=1)
        """
        response = self.client._request('GET', f'/api/teams/{team_id}/members')
        return response.get('members', [])

    def invite_user(
        self,
        team_id: int,
        email: str,
        role: str = "Member"
    ) -> Dict[str, Any]:
        """Invite a user to a team.

        Args:
            team_id: Team ID
            email: User email to invite
            role: Role to assign (Owner, Admin, Member, Billing)

        Returns:
            Response dict

        Example:
            >>> client.teams.invite_user(
            ...     team_id=1,
            ...     email="user@example.com",
            ...     role="Admin"
            ... )
        """
        return self.client._request(
            'POST',
            f'/api/teams/{team_id}/invitations',
            json={'email': email, 'role': role}
        )

    def remove_member(self, team_id: int, member_id: int) -> Dict[str, Any]:
        """Remove a member from a team.

        Args:
            team_id: Team ID
            member_id: Member ID to remove

        Returns:
            Response dict
        """
        return self.client._request(
            'DELETE',
            f'/api/teams/{team_id}/members/{member_id}'
        )

    def update_member_role(
        self,
        team_id: int,
        member_id: int,
        role: str
    ) -> Dict[str, Any]:
        """Update a member's role in a team.

        Args:
            team_id: Team ID
            member_id: Member ID
            role: New role

        Returns:
            Response dict
        """
        return self.client._request(
            'PATCH',
            f'/api/teams/{team_id}/members/{member_id}',
            json={'role': role}
        )

    # Organization methods (aliases for teams in the API)
    def list_organizations(self) -> List[Dict[str, Any]]:
        """List all organizations.

        Returns:
            List of organization dicts
        """
        response = self.client._request('GET', '/api/organizations')
        return response.get('organizations', [])

    def create_organization(
        self,
        name: str,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new organization.

        Args:
            name: Organization name
            description: Organization description

        Returns:
            Created organization dict
        """
        data = {'name': name}
        if description:
            data['description'] = description

        return self.client._request('POST', '/api/organizations', json=data)

    def get_organization(self, organization_id: int) -> Dict[str, Any]:
        """Get organization details.

        Args:
            organization_id: Organization ID

        Returns:
            Organization dict
        """
        return self.client._request('GET', f'/api/organizations/{organization_id}')

    def list_organization_members(self, organization_id: int) -> List[Dict[str, Any]]:
        """List organization members.

        Args:
            organization_id: Organization ID

        Returns:
            List of member dicts
        """
        response = self.client._request(
            'GET',
            f'/api/organizations/{organization_id}/members'
        )
        return response.get('members', [])
