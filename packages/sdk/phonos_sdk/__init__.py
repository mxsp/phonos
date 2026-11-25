"""Phonos SDK - Python client for the Phonos computational biology platform.

Example usage:
    >>> from phonos_sdk import PhonosClient
    >>> client = PhonosClient(api_key="your_api_key_here")
    >>> models = client.get_available_models()
    >>> job = client.jobs.submit(
    ...     project_id=1,
    ...     model_id="type-0",
    ...     sequence="MKTAYIAKQRQISFVK..."
    ... )
"""

from .client import PhonosClient
from .exceptions import (
    PhonosException,
    AuthenticationError,
    APIError,
    ValidationError,
    NotFoundError,
    RateLimitError,
    PermissionError,
)
from .models import (
    Model,
    Job,
    Project,
    File,
    Team,
    User,
    APIKey,
)

__version__ = "1.0.0"
__all__ = [
    "PhonosClient",
    "PhonosException",
    "AuthenticationError",
    "APIError",
    "ValidationError",
    "NotFoundError",
    "RateLimitError",
    "PermissionError",
    "Model",
    "Job",
    "Project",
    "File",
    "Team",
    "User",
    "APIKey",
]
