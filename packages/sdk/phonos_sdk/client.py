"""Main Phonos SDK client."""
import requests
from typing import Optional, Dict, Any
from .exceptions import (
    AuthenticationError,
    APIError,
    NotFoundError,
    RateLimitError,
    PermissionError,
)
from .resources.jobs import JobsResource
from .resources.projects import ProjectsResource
from .resources.teams import TeamsResource
from .resources.files import FilesResource
from .resources.users import UsersResource


class PhonosClient:
    """Phonos API client.

    Args:
        api_key: Your Phonos API key
        base_url: Base URL for the API (default: https://phonos.fyreflysystems.de)
        timeout: Request timeout in seconds (default: 30)

    Example:
        >>> client = PhonosClient(api_key="your_api_key_here")
        >>> models = client.get_available_models()
        >>> job = client.jobs.submit(
        ...     project_id=1,
        ...     model_id="type-0",
        ...     sequence="MKTAYIAK..."
        ... )
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://phonos.fyreflysystems.de",
        timeout: int = 30
    ):
        if not api_key:
            raise AuthenticationError("API key is required")

        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'phonos-sdk-python/1.0.0'
        })

        # Initialize resource endpoints
        self.jobs = JobsResource(self)
        self.projects = ProjectsResource(self)
        self.teams = TeamsResource(self)
        self.files = FilesResource(self)
        self.users = UsersResource(self)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make an HTTP request to the API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            endpoint: API endpoint path
            params: Query parameters
            json: JSON payload
            data: Form data
            files: Files to upload

        Returns:
            Response data as dict

        Raises:
            AuthenticationError: If authentication fails
            NotFoundError: If resource not found
            RateLimitError: If rate limit exceeded
            PermissionError: If permission denied
            APIError: For other API errors
        """
        url = f"{self.base_url}{endpoint}"

        try:
            # If files are being uploaded, don't send JSON content type
            headers = {}
            if files:
                headers = {'Content-Type': None}
                # Remove from session headers temporarily
                original_content_type = self.session.headers.pop('Content-Type', None)

            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json,
                data=data,
                files=files,
                timeout=self.timeout,
                headers=headers if files else None
            )

            # Restore content type if it was removed
            if files and original_content_type:
                self.session.headers['Content-Type'] = original_content_type

            # Handle different status codes
            if response.status_code == 401:
                raise AuthenticationError("Invalid or expired API key")
            elif response.status_code == 403:
                raise PermissionError(
                    "Permission denied",
                    status_code=403,
                    response=response
                )
            elif response.status_code == 404:
                raise NotFoundError(
                    "Resource not found",
                    status_code=404,
                    response=response
                )
            elif response.status_code == 429:
                raise RateLimitError(
                    "Rate limit exceeded",
                    status_code=429,
                    response=response
                )
            elif response.status_code >= 400:
                try:
                    error_data = response.json()
                    message = error_data.get('message', response.text)
                except Exception:
                    message = response.text

                raise APIError(
                    message,
                    status_code=response.status_code,
                    response=response
                )

            # Try to parse JSON response
            if response.content:
                try:
                    return response.json()
                except ValueError:
                    # Return text if not JSON
                    return {'data': response.text}

            return {}

        except requests.exceptions.Timeout:
            raise APIError(f"Request timeout after {self.timeout} seconds")
        except requests.exceptions.ConnectionError:
            raise APIError(f"Failed to connect to {self.base_url}")
        except (AuthenticationError, NotFoundError, RateLimitError, PermissionError, APIError):
            raise
        except Exception as e:
            raise APIError(f"Unexpected error: {str(e)}")

    def get_available_models(self):
        """Get list of available computational models.

        Returns:
            List of available models

        Example:
            >>> models = client.get_available_models()
            >>> for model in models:
            ...     print(f"{model['id']}: {model['name']}")
        """
        response = self._request('GET', '/api/available_models')
        return response.get('models', [])

    def get_user_info(self):
        """Get current user information.

        Returns:
            User information dict

        Example:
            >>> user = client.get_user_info()
            >>> print(f"Credits: {user['credits_balance']}")
        """
        return self._request('GET', '/api/user_info')

    def get_dashboard_stats(self):
        """Get dashboard statistics.

        Returns:
            Dashboard stats dict
        """
        return self._request('GET', '/api/dashboard_stats')

    def close(self):
        """Close the client session."""
        self.session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
