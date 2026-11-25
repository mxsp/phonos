"""Users resource for Phonos SDK."""
from typing import Optional, Dict, Any, List


class UsersResource:
    """Manage user account and settings."""

    def __init__(self, client):
        self.client = client

    def get_info(self) -> Dict[str, Any]:
        """Get current user information.

        Returns:
            User info dict with username, email, credits, etc.

        Example:
            >>> user = client.users.get_info()
            >>> print(f"Credits: {user['credits_balance']}")
        """
        return self.client._request('GET', '/api/user_info')

    def update_account(
        self,
        username: Optional[str] = None,
        email: Optional[str] = None,
        current_password: Optional[str] = None,
        new_password: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update account information.

        Args:
            username: New username
            email: New email
            current_password: Current password (required for email/password change)
            new_password: New password

        Returns:
            Response dict

        Example:
            >>> client.users.update_account(username="new_username")
        """
        data = {}
        if username:
            data['username'] = username
        if email:
            data['email'] = email
        if current_password:
            data['current_password'] = current_password
        if new_password:
            data['new_password'] = new_password

        return self.client._request('POST', '/api/account/update', json=data)

    def delete_account(self, password: str) -> Dict[str, Any]:
        """Delete user account.

        Args:
            password: User password for confirmation

        Returns:
            Response dict

        Example:
            >>> client.users.delete_account(password="my_password")
        """
        return self.client._request(
            'POST',
            '/api/account/delete',
            json={'password': password}
        )

    def get_plan(self) -> Dict[str, Any]:
        """Get current subscription plan.

        Returns:
            Plan information dict
        """
        return self.client._request('GET', '/api/account/plan')

    def upgrade_plan(self, plan: str) -> Dict[str, Any]:
        """Upgrade subscription plan.

        Args:
            plan: Plan to upgrade to (Premium, Enterprise)

        Returns:
            Response dict
        """
        if plan.lower() == 'premium':
            return self.client._request('POST', '/upgrade_to_premium')
        elif plan.lower() == 'enterprise':
            return self.client._request('POST', '/upgrade_to_enterprise')
        else:
            raise ValueError(f"Invalid plan: {plan}")

    def downgrade_plan(self) -> Dict[str, Any]:
        """Downgrade to Standard plan.

        Returns:
            Response dict
        """
        return self.client._request('POST', '/downgrade_to_standard')

    def get_settings(self) -> Dict[str, Any]:
        """Get user settings.

        Returns:
            Settings dict
        """
        return self.client._request('GET', '/api/settings')

    def update_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Update user settings.

        Args:
            settings: Settings dict to update

        Returns:
            Response dict

        Example:
            >>> client.users.update_settings({
            ...     'theme': 'dark',
            ...     'notifications_enabled': True
            ... })
        """
        return self.client._request('POST', '/api/settings', json=settings)

    def reset_settings(self) -> Dict[str, Any]:
        """Reset settings to defaults.

        Returns:
            Response dict
        """
        return self.client._request('POST', '/api/settings/reset')

    def get_api_keys(self) -> List[Dict[str, Any]]:
        """Get all API keys.

        Returns:
            List of API key dicts

        Example:
            >>> keys = client.users.get_api_keys()
            >>> for key in keys:
            ...     print(f"{key['name']}: {key['key_id']}")
        """
        return self.client._request('GET', '/api/api-keys')

    def get_notifications(self) -> List[Dict[str, Any]]:
        """Get user notifications.

        Returns:
            List of notification dicts
        """
        response = self.client._request('GET', '/api/notifications')
        return response.get('notifications', [])

    def clear_notifications(self) -> Dict[str, Any]:
        """Clear all notifications.

        Returns:
            Response dict
        """
        return self.client._request('POST', '/api/notifications/clear')

    def mark_notifications_read(
        self,
        notification_ids: List[int]
    ) -> Dict[str, Any]:
        """Mark notifications as read.

        Args:
            notification_ids: List of notification IDs to mark as read

        Returns:
            Response dict
        """
        return self.client._request(
            'POST',
            '/api/notifications/mark_read',
            json={'notification_ids': notification_ids}
        )

    def get_analytics(self) -> Dict[str, Any]:
        """Get usage analytics.

        Returns:
            Analytics data dict
        """
        return self.client._request('GET', '/api/analytics/usage')

    def upload_profile_picture(self, image_path: str) -> Dict[str, Any]:
        """Upload a profile picture.

        Args:
            image_path: Path to the image file

        Returns:
            Response dict
        """
        import os
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")

        filename = os.path.basename(image_path)
        with open(image_path, 'rb') as f:
            files = {'profile_picture': (filename, f)}
            return self.client._request(
                'POST',
                '/upload_profile_picture',
                files=files
            )
