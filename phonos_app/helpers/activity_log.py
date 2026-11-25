"""Helper functions for logging user activities to the dashboard operations log."""

import json
from typing import Optional, Any


def log_activity(
    db: Any,  # PostgreSQLConnection wrapper
    user_id: int,
    activity_type: str,
    activity_description: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[int] = None,
    metadata: Optional[dict] = None
) -> None:
    """
    Log a user activity to the activity_log table.

    Args:
        db: Database connection
        user_id: ID of the user performing the activity
        activity_type: Type of activity (e.g., 'invite_accepted', 'project_created')
        activity_description: Human-readable description of the activity
        resource_type: Optional type of resource involved (e.g., 'team', 'project')
        resource_id: Optional ID of the resource involved
        metadata: Optional additional metadata as a dictionary
    """
    metadata_json = json.dumps(metadata) if metadata else None

    db.execute(
        '''
        INSERT INTO activity_log
        (user_id, activity_type, activity_description, resource_type, resource_id, metadata)
        VALUES (%s, %s, %s, %s, %s, %s)
        ''',
        (user_id, activity_type, activity_description, resource_type, resource_id, metadata_json)
    )
