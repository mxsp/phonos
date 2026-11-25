"""Data models for Phonos SDK."""
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Model:
    """Computational model definition."""
    id: str
    node_id: str
    name: str
    short_name: str
    description: str
    task: Optional[str]
    input_modes: List[str]
    output_extension: str
    available: bool
    credit_cost: int


@dataclass
class Job:
    """Computational job."""
    id: str
    project_id: int
    model_id: str
    status: str
    created_at: str
    user_id: int
    sequence: Optional[str] = None
    file_id: Optional[int] = None
    output_path: Optional[str] = None
    error_message: Optional[str] = None
    completed_at: Optional[str] = None
    job_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Project:
    """Project."""
    id: int
    name: str
    description: Optional[str]
    created_at: str
    owner_id: int
    team_id: Optional[int] = None
    is_active: bool = True


@dataclass
class File:
    """File resource."""
    id: int
    filename: str
    project_id: int
    uploaded_by: int
    uploaded_at: str
    file_size: int
    file_type: Optional[str] = None


@dataclass
class Team:
    """Team/Organization."""
    id: int
    name: str
    created_at: str
    owner_id: int
    description: Optional[str] = None


@dataclass
class User:
    """User information."""
    id: int
    username: str
    email: str
    plan: str
    created_at: str
    credits_balance: int = 0
    storage_used: int = 0


@dataclass
class APIKey:
    """API key."""
    id: int
    key_id: str
    name: str
    scopes: str
    created_at: str
    last_used_at: Optional[str] = None
    expires_at: Optional[str] = None
    is_active: bool = True
