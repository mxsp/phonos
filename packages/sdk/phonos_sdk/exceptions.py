"""Phonos SDK exceptions."""


class PhonosException(Exception):
    """Base exception for Phonos SDK."""
    pass


class AuthenticationError(PhonosException):
    """Raised when authentication fails."""
    pass


class APIError(PhonosException):
    """Raised when API returns an error."""

    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class ValidationError(PhonosException):
    """Raised when input validation fails."""
    pass


class NotFoundError(APIError):
    """Raised when a resource is not found."""
    pass


class RateLimitError(APIError):
    """Raised when rate limit is exceeded."""
    pass


class PermissionError(APIError):
    """Raised when user lacks permissions."""
    pass
