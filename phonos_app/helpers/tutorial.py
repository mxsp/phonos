"""Tutorial helper module for user onboarding.

This module provides functions to manage tutorial progress for new users.
The tutorial state is stored in the users table with the following columns:
- tutorial_step: Current step in the tutorial (0 = not started)
- tutorial_completed: Boolean flag indicating completion (0/1)
- tutorial_dismissed: Boolean flag indicating if user skipped (0/1)
"""

from typing import Optional, TypedDict
from ..helpers import get_db
import logging

logger = logging.getLogger(__name__)


class TutorialProgress(TypedDict):
    """Tutorial progress data structure."""
    step: int
    completed: bool
    dismissed: bool


def get_tutorial_progress(user_id: int) -> TutorialProgress:
    """Get the tutorial progress for a user.

    Args:
        user_id: The user's ID

    Returns:
        TutorialProgress dict with step, completed, and dismissed status
    """
    db = get_db()
    try:
        row = db.execute(
            'SELECT tutorial_step, tutorial_completed, tutorial_dismissed FROM users WHERE id = %s',
            (user_id,)
        ).fetchone()

        if row:
            return {
                'step': row['tutorial_step'] or 0,
                'completed': bool(row['tutorial_completed']),
                'dismissed': bool(row['tutorial_dismissed'])
            }
    except Exception as e:
        logger.error(f"Error getting tutorial progress for user {user_id}: {e}")

    return {'step': 0, 'completed': False, 'dismissed': False}


def update_tutorial_step(user_id: int, step: int) -> bool:
    """Update the current tutorial step for a user.

    Args:
        user_id: The user's ID
        step: The new step number (must be >= 0)

    Returns:
        True if successful, False otherwise
    """
    if step < 0:
        return False

    db = get_db()
    try:
        db.execute(
            'UPDATE users SET tutorial_step = %s WHERE id = %s',
            (step, user_id)
        )
        db.commit()
        return True
    except Exception as e:
        logger.error(f"Error updating tutorial step for user {user_id}: {e}")
        db.rollback()
        return False


def complete_tutorial(user_id: int) -> bool:
    """Mark the tutorial as completed for a user.

    Args:
        user_id: The user's ID

    Returns:
        True if successful, False otherwise
    """
    db = get_db()
    try:
        db.execute(
            'UPDATE users SET tutorial_completed = true, tutorial_dismissed = 0, tutorial_step = 0 WHERE id = %s',
            (user_id,)
        )
        db.commit()
        logger.info(f"Tutorial completed for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error completing tutorial for user {user_id}: {e}")
        db.rollback()
        return False


def dismiss_tutorial(user_id: int) -> bool:
    """Dismiss/skip the tutorial for a user.

    Args:
        user_id: The user's ID

    Returns:
        True if successful, False otherwise
    """
    db = get_db()
    try:
        db.execute(
            'UPDATE users SET tutorial_dismissed = 1 WHERE id = %s',
            (user_id,)
        )
        db.commit()
        logger.info(f"Tutorial dismissed for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error dismissing tutorial for user {user_id}: {e}")
        db.rollback()
        return False


def reset_tutorial(user_id: int) -> bool:
    """Reset tutorial progress for a user to start over.

    Args:
        user_id: The user's ID

    Returns:
        True if successful, False otherwise
    """
    db = get_db()
    try:
        db.execute(
            'UPDATE users SET tutorial_step = 0, tutorial_completed = false, tutorial_dismissed = 0 WHERE id = %s',
            (user_id,)
        )
        db.commit()
        logger.info(f"Tutorial reset for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error resetting tutorial for user {user_id}: {e}")
        db.rollback()
        return False


def should_show_tutorial(user_id: int) -> bool:
    """Check if the tutorial should be shown to a user.

    The tutorial is shown when:
    - User has not completed the tutorial
    - User has not dismissed the tutorial

    Args:
        user_id: The user's ID

    Returns:
        True if tutorial should be shown, False otherwise
    """
    progress = get_tutorial_progress(user_id)
    return not progress['completed'] and not progress['dismissed']


def advance_tutorial_step(user_id: int) -> Optional[int]:
    """Advance to the next tutorial step.

    Args:
        user_id: The user's ID

    Returns:
        The new step number, or None if failed
    """
    progress = get_tutorial_progress(user_id)
    new_step = progress['step'] + 1

    if update_tutorial_step(user_id, new_step):
        return new_step
    return None
