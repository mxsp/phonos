"""Complete account deletion and data purge service."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

logger = logging.getLogger(__name__)


def get_db():
    """Get database connection - import here to avoid circular imports."""
    from phonos_app.helpers.database import get_db as _get_db
    return _get_db()


class AccountDeletionService:
    """Service for completely deleting user accounts and all associated data."""

    @staticmethod
    def delete_account(user_id: int, initiated_by: Optional[str] = None) -> tuple[bool, str]:
        """
        Completely delete a user account and all associated data.

        This will permanently remove:
        - User profile and credentials
        - All projects (including shared/collaborative projects)
        - All jobs and job results
        - All files and uploads
        - All messages and comments
        - All team memberships and invitations
        - All subscriptions and billing data
        - All user-generated content

        Args:
            user_id: The ID of the user to delete
            initiated_by: Username of who initiated the deletion (for logging)

        Returns:
            Tuple of (success: bool, message: str)
        """
        db = get_db()
        try:
            # First, verify the user exists and get their info
            user = db.execute(
                "SELECT id, username, user_id FROM users WHERE id = %s",
                (user_id,)
            ).fetchone()

            if not user:
                return False, "User not found"

            username = user['username']
            logger.info(f"Starting account deletion for user {username} (ID: {user_id}), initiated by: {initiated_by or 'self'}")

            # Get user's file storage directory
            user_uuid = user['user_id']
            storage_dir = os.path.join(os.getcwd(), 'job_outputs', user_uuid)

            try:
                # 1. Delete job outputs and files from filesystem
                if os.path.exists(storage_dir):
                    try:
                        shutil.rmtree(storage_dir)
                        logger.info(f"Deleted storage directory for user {username}")
                    except Exception as e:
                        logger.error(f"Failed to delete storage directory: {e}")
                        # Continue with database deletion even if file deletion fails

                # 2. Delete profile picture if exists
                profile_pic = db.execute(
                    "SELECT profile_picture_url FROM users WHERE id = %s",
                    (user_id,)
                ).fetchone()

                if profile_pic and profile_pic['profile_picture_url']:
                    pic_path = os.path.join(os.getcwd(), profile_pic['profile_picture_url'].lstrip('/'))
                    if os.path.exists(pic_path):
                        try:
                            os.remove(pic_path)
                            logger.info(f"Deleted profile picture for user {username}")
                        except Exception as e:
                            logger.error(f"Failed to delete profile picture: {e}")

                # 3. Cancel any active Stripe subscriptions
                try:
                    stripe_customer = db.execute(
                        "SELECT stripe_customer_id, stripe_subscription_id FROM users WHERE id = %s",
                        (user_id,)
                    ).fetchone()

                    if stripe_customer and stripe_customer['stripe_subscription_id']:
                        # Cancel subscription in Stripe
                        try:
                            from stripe_service import StripeService
                            import stripe

                            if stripe_customer['stripe_subscription_id']:
                                stripe.Subscription.delete(stripe_customer['stripe_subscription_id'])
                                logger.info(f"Canceled Stripe subscription for user {username}")
                        except Exception as e:
                            logger.error(f"Failed to cancel Stripe subscription: {e}")
                            # Continue with deletion anyway
                except Exception as e:
                    logger.error(f"Error handling Stripe subscription: {e}")

                # 4. Delete database records (CASCADE will handle related records)
                # The user record deletion will CASCADE to:
                # - user_credentials
                # - two_factor_auth
                # - login_history
                # - team_members
                # - project_assignments
                # - jobs
                # - files
                # - direct_messages
                # - project_messages
                # - notifications
                # - help_tickets
                # - budget_alerts
                # - api_keys
                # - usage_analytics
                # - activity_log
                # - user_privacy_settings
                # - dashboard_preferences
                # - public_profile_settings

                # Delete user-owned teams and their projects
                teams = db.execute(
                    "SELECT id FROM teams WHERE creator_id = %s",
                    (user_id,)
                ).fetchall()

                for team in teams:
                    db.execute("DELETE FROM teams WHERE id = %s", (team['id'],))

                # Delete user-owned projects
                projects = db.execute(
                    "SELECT id FROM project_assignments WHERE user_id = %s",
                    (user_id,)
                ).fetchall()

                for project in projects:
                    # Check if user is the only member, if so delete the entire project
                    member_count = db.execute(
                        "SELECT COUNT(*) as count FROM project_assignments WHERE project_id = %s",
                        (project['id'],)
                    ).fetchone()['count']

                    if member_count == 1:
                        db.execute("DELETE FROM projects WHERE id = %s", (project['id'],))

                # Finally, delete the user record (will cascade to user_credentials and other tables)
                db.execute("DELETE FROM users WHERE id = %s", (user_id,))

                # Commit the transaction
                db.commit()

                logger.info(f"Successfully deleted account for user {username} (ID: {user_id})")
                return True, f"Account {username} has been permanently deleted"

            except Exception as e:
                # Rollback on any error
                db.rollback()
                logger.error(f"Error during account deletion for {username}: {e}", exc_info=True)
                return False, f"Failed to delete account: {str(e)}"

        except Exception as e:
            logger.error(f"Error initiating account deletion: {e}", exc_info=True)
            return False, f"Failed to initiate account deletion: {str(e)}"

    @staticmethod
    def anonymize_account(user_id: int) -> tuple[bool, str]:
        """
        Anonymize user account instead of deleting it.

        This removes personal information but keeps aggregated data for analytics.
        Useful for regulatory compliance (GDPR, CCPA) while maintaining data integrity.

        Args:
            user_id: The ID of the user to anonymize

        Returns:
            Tuple of (success: bool, message: str)
        """
        db = get_db()
        try:
            # Get current user info
            user = db.execute(
                "SELECT username FROM users WHERE id = %s",
                (user_id,)
            ).fetchone()

            if not user:
                return False, "User not found"

            username = user['username']
            anon_username = f"deleted_user_{user_id}"
            anon_email = f"deleted_{user_id}@anonymized.local"

            try:
                # Anonymize user record
                db.execute(
                    """UPDATE users
                       SET username = %s,
                           legal_name = '[Deleted User]',
                           address = NULL,
                           company = NULL,
                           university = NULL,
                           age = NULL,
                           profile_picture_url = NULL,
                           last_login_ip = NULL
                       WHERE id = %s""",
                    (anon_username, user_id)
                )

                # Anonymize credentials
                db.execute(
                    """UPDATE user_credentials
                       SET email = %s,
                           password = '',
                           email_verified = 0,
                           verification_token = NULL,
                           tos_ip_address = NULL
                       WHERE user_id = %s""",
                    (anon_email, user_id)
                )

                # Remove 2FA
                db.execute("DELETE FROM two_factor_auth WHERE user_id = %s", (user_id,))

                # Anonymize login history
                db.execute(
                    "UPDATE login_history SET ip_address = '[anonymized]', user_agent = '[anonymized]', device_idx = NULL WHERE user_id = %s",
                    (user_id,)
                )

                db.commit()

                logger.info(f"Successfully anonymized account for user {username} (ID: {user_id})")
                return True, "Account has been anonymized"

            except Exception as e:
                db.rollback()
                logger.error(f"Error during account anonymization: {e}", exc_info=True)
                return False, f"Failed to anonymize account: {str(e)}"

        except Exception as e:
            logger.error(f"Error initiating account anonymization: {e}", exc_info=True)
            return False, f"Failed to initiate account anonymization: {str(e)}"
