"""Promo code service for granting premium access without Stripe."""

from __future__ import annotations

import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from config import Config
from database import get_db_connection

logger = logging.getLogger(__name__)


class PromoCodeService:
    """Service for managing promo codes that grant premium access."""

    # Valid promo codes from environment
    VALID_PROMO_CODES = set()

    @classmethod
    def load_promo_codes(cls):
        """Load promo codes from environment variable."""
        # Load from PROMO_CODES env variable (comma-separated)
        promo_codes_str = getattr(Config, 'PROMO_CODES', '')
        if promo_codes_str:
            cls.VALID_PROMO_CODES = set(code.strip().upper() for code in promo_codes_str.split(',') if code.strip())
            logger.info(f"Loaded {len(cls.VALID_PROMO_CODES)} promo codes from environment")
        else:
            logger.warning("No PROMO_CODES configured in environment")

    @staticmethod
    def validate_promo_code(code: str) -> bool:
        """
        Validate if a promo code is valid.

        Args:
            code: The promo code to validate

        Returns:
            True if valid, False otherwise
        """
        if not code:
            return False

        code_upper = code.strip().upper()

        # Check against env-configured codes
        if code_upper in PromoCodeService.VALID_PROMO_CODES:
            return True

        return False

    @staticmethod
    def apply_promo_code(user_id: int, code: str, username: str) -> Dict[str, Any]:
        """
        Apply a promo code to grant premium access.

        Args:
            user_id: User ID to apply promo code to
            code: The promo code
            username: Username for logging

        Returns:
            Dict with success status and message
        """
        # Validate promo code
        if not PromoCodeService.validate_promo_code(code):
            logger.warning(f"Invalid promo code attempt by user {username}: {code}")
            return {
                "success": False,
                "message": "Invalid promo code"
            }

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Check if user already has premium
            cursor.execute(
                "SELECT plan, credits, storage_quota FROM users WHERE id = ?",
                (user_id,)
            )
            user_data = cursor.fetchone()

            if not user_data:
                return {
                    "success": False,
                    "message": "User not found"
                }

            current_plan = user_data[0]

            # Check if already premium/enterprise
            if current_plan in ['Premium', 'Managed Premium', 'Enterprise']:
                logger.info(f"User {username} already has {current_plan} plan")
                return {
                    "success": False,
                    "message": f"You already have a {current_plan} plan"
                }

            # Grant Premium for 1 month
            new_plan = "Premium"
            new_credits = 1000  # Premium credits
            new_storage_quota = 10 * 1024 * 1024 * 1024  # 10 GB in bytes

            # Calculate expiration date (30 days from now)
            expiration_date = datetime.now() + timedelta(days=30)

            # Update user plan
            cursor.execute(
                """
                UPDATE users
                SET plan = ?,
                    credits = credits + ?,
                    storage_quota = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_plan, new_credits, new_storage_quota, user_id)
            )

            # Log promo code usage
            cursor.execute(
                """
                INSERT INTO promo_code_usage
                (user_id, code_hash, applied_at, expires_at, plan_granted)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    hashlib.sha256(code.encode()).hexdigest(),
                    datetime.now(),
                    expiration_date,
                    new_plan
                )
            )

            conn.commit()
            conn.close()

            logger.info(f"Promo code applied successfully for user {username}. Granted {new_plan} until {expiration_date}")

            return {
                "success": True,
                "message": f"Premium access granted for 30 days!",
                "plan": new_plan,
                "credits_added": new_credits,
                "storage_quota": new_storage_quota,
                "expires_at": expiration_date.isoformat()
            }

        except Exception as e:
            logger.error(f"Error applying promo code for user {username}: {e}", exc_info=True)
            if conn:
                conn.rollback()
                conn.close()
            return {
                "success": False,
                "message": "An error occurred while applying the promo code"
            }

    @staticmethod
    def get_promo_code_usage(user_id: int) -> list:
        """
        Get promo code usage history for a user.

        Args:
            user_id: User ID

        Returns:
            List of promo code usage records
        """
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT applied_at, expires_at, plan_granted, is_active
                FROM promo_code_usage
                WHERE user_id = ?
                ORDER BY applied_at DESC
                """,
                (user_id,)
            )

            rows = cursor.fetchall()
            conn.close()

            return [
                {
                    "applied_at": row[0],
                    "expires_at": row[1],
                    "plan_granted": row[2],
                    "is_active": bool(row[3])
                }
                for row in rows
            ]

        except Exception as e:
            logger.error(f"Error fetching promo code usage for user {user_id}: {e}")
            return []

    @staticmethod
    def check_expired_promo_access():
        """
        Check for expired promo code access and downgrade users.
        Should be run periodically (e.g., daily cron job).
        """
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Find users with expired promo access
            cursor.execute(
                """
                SELECT DISTINCT u.id, u.username, u.plan, pcu.expires_at
                FROM users u
                JOIN promo_code_usage pcu ON u.id = pcu.user_id
                WHERE pcu.is_active = 1
                AND pcu.expires_at < ?
                AND u.plan IN ('Premium', 'Managed Premium')
                AND u.stripe_subscription_id IS NULL
                """,
                (datetime.now(),)
            )

            expired_users = cursor.fetchall()

            for user in expired_users:
                user_id, username, current_plan, expired_at = user

                # Downgrade to Standard
                cursor.execute(
                    """
                    UPDATE users
                    SET plan = 'Standard',
                        credits = 100,
                        storage_quota = 1073741824
                    WHERE id = ?
                    """,
                    (user_id,)
                )

                # Mark promo code as inactive
                cursor.execute(
                    """
                    UPDATE promo_code_usage
                    SET is_active = 0
                    WHERE user_id = ? AND is_active = 1
                    """,
                    (user_id,)
                )

                logger.info(f"Downgraded user {username} from {current_plan} to Standard (promo expired at {expired_at})")

            conn.commit()
            conn.close()

            logger.info(f"Processed {len(expired_users)} expired promo code users")
            return len(expired_users)

        except Exception as e:
            logger.error(f"Error checking expired promo access: {e}", exc_info=True)
            if conn:
                conn.rollback()
                conn.close()
            return 0


# Load promo codes on module import
PromoCodeService.load_promo_codes()
