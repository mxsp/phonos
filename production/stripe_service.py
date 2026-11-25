"""Stripe integration service for subscription management."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import stripe
from flask import current_app

from config import Config
from database import get_db_connection
from plan_service import PLAN_DEFINITIONS, resolve_plan

logger = logging.getLogger(__name__)

# Initialize Stripe
if Config.STRIPE_SECRET_KEY:
    stripe.api_key = Config.STRIPE_SECRET_KEY
else:
    logger.warning("STRIPE_SECRET_KEY not configured. Stripe functionality will be disabled.")


class StripeService:
    """Service for managing Stripe subscriptions and customers."""

    # Map plan names to Stripe Price IDs
    PLAN_TO_PRICE_ID = {
        "Premium": Config.STRIPE_PREMIUM_PRICE_ID,
        "Managed Premium": Config.STRIPE_MANAGED_PREMIUM_PRICE_ID,
        "Enterprise": Config.STRIPE_ENTERPRISE_PRICE_ID,
    }

    @staticmethod
    def create_customer(user_id: int, email: str, username: str, legal_name: Optional[str] = None) -> Optional[str]:
        """
        Create a Stripe customer for a user.

        Args:
            user_id: The internal user ID
            email: User's email address
            username: User's username
            legal_name: User's legal name (optional)

        Returns:
            Stripe customer ID if successful, None otherwise
        """
        if not Config.STRIPE_SECRET_KEY:
            logger.warning("Stripe not configured. Skipping customer creation.")
            return None

        try:
            customer = stripe.Customer.create(
                email=email,
                name=legal_name or username,
                metadata={
                    "user_id": str(user_id),
                    "username": username,
                }
            )

            # Update user record with Stripe customer ID
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET stripe_customer_id = %s WHERE id = %s",
                    (customer.id, user_id)
                )
                conn.commit()
                logger.info(f"Created Stripe customer {customer.id} for user {username}")
                return customer.id
            finally:
                conn.close()

        except stripe.StripeError as e:
            logger.error(f"Failed to create Stripe customer for user {username}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating Stripe customer: {e}", exc_info=True)
            return None

    @staticmethod
    def get_or_create_customer(user_id: int, email: str, username: str, legal_name: Optional[str] = None) -> Optional[str]:
        """
        Get existing Stripe customer ID or create a new customer.

        Args:
            user_id: The internal user ID
            email: User's email address
            username: User's username
            legal_name: User's legal name (optional)

        Returns:
            Stripe customer ID if successful, None otherwise
        """
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            result = cursor.execute(
                "SELECT stripe_customer_id FROM users WHERE id = %s",
                (user_id,)
            ).fetchone()

            if result and result['stripe_customer_id']:
                return result['stripe_customer_id']

            # Customer doesn't exist, create one
            return StripeService.create_customer(user_id, email, username, legal_name)
        finally:
            conn.close()

    @staticmethod
    def update_customer_email(user_id: int, new_email: str) -> bool:
        """
        Update the email address for an existing Stripe customer.

        Args:
            user_id: The internal user ID
            new_email: The new email address

        Returns:
            True if successful, False otherwise
        """
        if not Config.STRIPE_SECRET_KEY:
            logger.warning("Stripe not configured. Skipping customer email update.")
            return False

        try:
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                result = cursor.execute(
                    "SELECT stripe_customer_id, username FROM users WHERE id = %s",
                    (user_id,)
                ).fetchone()

                if not result or not result['stripe_customer_id']:
                    logger.info(f"No Stripe customer ID found for user {user_id}, skipping email update")
                    return False

                customer_id = result['stripe_customer_id']
                username = result['username']

                # Update the customer in Stripe
                stripe.Customer.modify(
                    customer_id,
                    email=new_email
                )

                logger.info(f"Updated Stripe customer {customer_id} email to {new_email} for user {username}")
                return True

            finally:
                conn.close()

        except stripe.StripeError as e:
            logger.error(f"Failed to update Stripe customer email for user {user_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating Stripe customer email: {e}", exc_info=True)
            return False

    @staticmethod
    def create_checkout_session(
        user_id: int,
        plan_name: str,
        success_url: str,
        cancel_url: str,
        trial_days: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a Stripe Checkout session for subscription.

        Args:
            user_id: The internal user ID
            plan_name: Name of the plan to subscribe to
            success_url: URL to redirect on success
            cancel_url: URL to redirect on cancel
            trial_days: Number of trial days (optional)

        Returns:
            Dictionary with checkout session details or None
        """
        if not Config.STRIPE_SECRET_KEY:
            logger.warning("Stripe not configured. Cannot create checkout session.")
            return None

        try:
            # Get user details
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                user = cursor.execute(
                    "SELECT u.id, u.stripe_customer_id, u.username, u.legal_name, uc.email "
                    "FROM users u "
                    "JOIN user_credentials uc ON u.id = uc.user_id "
                    "WHERE u.id = %s",
                    (user_id,)
                ).fetchone()

                if not user:
                    logger.error(f"User {user_id} not found")
                    return None

                # Get or create Stripe customer
                customer_id = user['stripe_customer_id']
                if not customer_id:
                    customer_id = StripeService.create_customer(
                        user['id'],
                        user['email'],
                        user['username'],
                        user['legal_name']
                    )
                    if not customer_id:
                        return None

                # Get price ID for the plan
                price_id = StripeService.PLAN_TO_PRICE_ID.get(plan_name)
                if not price_id:
                    logger.error(f"No Stripe price ID configured for plan: {plan_name}")
                    return None

                # Create checkout session parameters
                session_params = {
                    "customer": customer_id,
                    "mode": "subscription",
                    "line_items": [{
                        "price": price_id,
                        "quantity": 1,
                    }],
                    "success_url": success_url,
                    "cancel_url": cancel_url,
                    "metadata": {
                        "user_id": str(user_id),
                        "plan_name": plan_name,
                    },
                    "allow_promotion_codes": True,
                    # Enable Stripe Link for faster checkout with saved payment methods
                    "payment_method_types": ["card", "link"],
                    # Pre-fill customer email for Stripe Link
                    "customer_email": user['email'] if not customer_id else None,
                }

                # Add trial if specified
                if trial_days and trial_days > 0:
                    session_params["subscription_data"] = {
                        "trial_period_days": trial_days,
                        "metadata": {
                            "user_id": str(user_id),
                            "plan_name": plan_name,
                        }
                    }

                # Create the checkout session
                session = stripe.checkout.Session.create(**session_params)

                logger.info(f"Created checkout session {session.id} for user {user['username']}")
                return {
                    "session_id": session.id,
                    "url": session.url,
                }

            finally:
                conn.close()

        except stripe.StripeError as e:
            logger.error(f"Stripe error creating checkout session: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating checkout session: {e}", exc_info=True)
            return None

    @staticmethod
    def create_billing_portal_session(user_id: int, return_url: str) -> Optional[str]:
        """
        Create a Stripe billing portal session for managing subscriptions.

        Args:
            user_id: The internal user ID
            return_url: URL to return to after portal session

        Returns:
            Portal session URL if successful, None otherwise
        """
        if not Config.STRIPE_SECRET_KEY:
            logger.warning("Stripe not configured. Cannot create portal session.")
            return None

        try:
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT stripe_customer_id FROM users WHERE id = %s",
                    (user_id,)
                )
                result = cursor.fetchone()

                if not result or not result['stripe_customer_id']:
                    logger.error(f"No Stripe customer ID for user {user_id}")
                    return None

                # Create portal session
                session = stripe.billing_portal.Session.create(
                    customer=result['stripe_customer_id'],
                    return_url=return_url,
                )

                logger.info(f"Created billing portal session for user {user_id}")
                return session.url

            finally:
                conn.close()

        except stripe.StripeError as e:
            logger.error(f"Stripe error creating portal session: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating portal session: {e}", exc_info=True)
            return None

    @staticmethod
    def handle_subscription_created(subscription: Dict[str, Any]):
        """Handle subscription.created webhook event."""
        try:
            customer_id = subscription.get('customer')
            subscription_id = subscription.get('id')
            status = subscription.get('status')

            # Get trial end date if applicable
            trial_end = subscription.get('trial_end')
            trial_end_date = datetime.fromtimestamp(trial_end) if trial_end else None

            # Get subscription end date
            current_period_end = subscription.get('current_period_end')
            end_date = datetime.fromtimestamp(current_period_end) if current_period_end else None

            # Get plan name from metadata
            metadata = subscription.get('metadata', {})
            plan_name = metadata.get('plan_name', 'Premium')

            # Update user record
            conn = get_db_connection()
            try:
                cursor = conn.cursor()

                # Get plan details
                plan_def = PLAN_DEFINITIONS.get(plan_name)
                storage_quota = plan_def.storage_quota_bytes if plan_def else 10 * 1024 * 1024 * 1024

                # First try to update by stripe_customer_id
                cursor.execute(
                    """UPDATE users
                       SET stripe_subscription_id = %s,
                           subscription_status = %s,
                           subscription_end_date = %s,
                           trial_end_date = %s,
                           plan = %s,
                           storage_quota = %s
                       WHERE stripe_customer_id = %s""",
                    (subscription_id, status, end_date, trial_end_date, plan_name, storage_quota, customer_id)
                )

                # If no user found by customer_id, try to find by customer email
                if cursor.rowcount == 0:
                    # Fetch customer email from Stripe
                    try:
                        customer = stripe.Customer.retrieve(customer_id)
                        customer_email = customer.get('email')

                        if customer_email:
                            # Find user by email and link the customer
                            user_result = cursor.execute(
                                """SELECT u.id FROM users u
                                   JOIN user_credentials uc ON u.id = uc.user_id
                                   WHERE uc.email = %s
                                   LIMIT 1""",
                                (customer_email,)
                            ).fetchone()

                            if user_result:
                                cursor.execute(
                                    """UPDATE users
                                       SET stripe_customer_id = %s,
                                           stripe_subscription_id = %s,
                                           subscription_status = %s,
                                           subscription_end_date = %s,
                                           trial_end_date = %s,
                                           plan = %s,
                                           storage_quota = %s
                                       WHERE id = %s""",
                                    (customer_id, subscription_id, status, end_date, trial_end_date, plan_name, storage_quota, user_result['id'])
                                )
                                logger.info(f"Linked customer {customer_id} to user {user_result['id']} by email {customer_email}")
                    except stripe.StripeError as e:
                        logger.warning(f"Could not retrieve customer {customer_id} from Stripe: {e}")

                conn.commit()
                logger.info(f"Updated subscription {subscription_id} for customer {customer_id}")
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Error handling subscription.created: {e}", exc_info=True)

    @staticmethod
    def handle_subscription_updated(subscription: Dict[str, Any]):
        """Handle subscription.updated webhook event."""
        try:
            customer_id = subscription.get('customer')
            subscription_id = subscription.get('id')
            status = subscription.get('status')
            cancel_at_period_end = subscription.get('cancel_at_period_end', False)

            # Get subscription end date
            current_period_end = subscription.get('current_period_end')
            end_date = datetime.fromtimestamp(current_period_end) if current_period_end else None

            # Check if this is a cancellation scheduled for period end
            if cancel_at_period_end:
                # User has requested cancellation but keeps access until period end
                StripeService.handle_subscription_scheduled_cancellation(subscription)
                return

            # Update user record
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """UPDATE users
                       SET subscription_status = %s,
                           subscription_end_date = %s
                       WHERE stripe_customer_id = %s""",
                    (status, end_date, customer_id)
                )
                conn.commit()
                logger.info(f"Updated subscription status for customer {customer_id}: {status}")
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Error handling subscription.updated: {e}", exc_info=True)

    @staticmethod
    def handle_subscription_deleted(subscription: Dict[str, Any]):
        """Handle subscription.deleted webhook event.

        Note: This is called when the subscription is actually terminated.
        If the user cancelled with cancel_at_period_end=True, this event
        fires at the END of the billing period, not when they clicked cancel.
        At this point, we should downgrade them to Standard.
        """
        try:
            customer_id = subscription.get('customer')

            # Downgrade user to Standard plan - subscription has actually ended
            from plan_service import DEFAULT_PLAN, get_plan_storage_quota

            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """UPDATE users
                       SET plan = %s,
                           subscription_status = 'canceled',
                           storage_quota = %s,
                           stripe_subscription_id = NULL,
                           subscription_end_date = NULL
                       WHERE stripe_customer_id = %s""",
                    (DEFAULT_PLAN, get_plan_storage_quota(DEFAULT_PLAN), customer_id)
                )
                conn.commit()
                logger.info(f"Downgraded customer {customer_id} to {DEFAULT_PLAN} - subscription ended")
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Error handling subscription.deleted: {e}", exc_info=True)

    @staticmethod
    def handle_subscription_scheduled_cancellation(subscription: Dict[str, Any]):
        """Handle when a subscription is set to cancel at period end.

        The user keeps their premium status until subscription_end_date.
        This is triggered by subscription.updated with cancel_at_period_end=True.
        """
        try:
            customer_id = subscription.get('customer')
            cancel_at = subscription.get('cancel_at')
            current_period_end = subscription.get('current_period_end')

            # Determine when the subscription will end
            end_timestamp = cancel_at or current_period_end
            end_date = datetime.fromtimestamp(end_timestamp) if end_timestamp else None

            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                # Mark as 'canceling' but keep the plan active
                cursor.execute(
                    """UPDATE users
                       SET subscription_status = 'canceling',
                           subscription_end_date = %s
                       WHERE stripe_customer_id = %s""",
                    (end_date, customer_id)
                )
                conn.commit()
                logger.info(f"Customer {customer_id} subscription scheduled to cancel at {end_date}")
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Error handling subscription cancellation scheduling: {e}", exc_info=True)

    @staticmethod
    def handle_invoice_paid(invoice: Dict[str, Any]):
        """Handle invoice.paid webhook event."""
        try:
            customer_id = invoice.get('customer')
            subscription_id = invoice.get('subscription')

            # Reset monthly credits when invoice is paid
            conn = get_db_connection()
            try:
                cursor = conn.cursor()

                # Get user's plan
                user = cursor.execute(
                    "SELECT plan FROM users WHERE stripe_customer_id = %s",
                    (customer_id,)
                ).fetchone()

                if user:
                    from plan_service import get_plan_credit_quota
                    monthly_credits = get_plan_credit_quota(user['plan'])

                    cursor.execute(
                        """UPDATE users
                           SET credits = %s,
                               credits_last_reset = CURRENT_TIMESTAMP
                           WHERE stripe_customer_id = %s""",
                        (monthly_credits, customer_id)
                    )
                    conn.commit()
                    logger.info(f"Reset credits for customer {customer_id} to {monthly_credits}")
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Error handling invoice.paid: {e}", exc_info=True)

    @staticmethod
    def grant_free_trial(user_id: int, plan_name: str, days: int = 30):
        """
        Grant a free trial period to a user without requiring payment.
        Used for special promotions like hs-anhalt.de email users.

        Args:
            user_id: The internal user ID
            plan_name: Name of the plan to grant
            days: Number of days for the trial (default 30)
        """
        try:
            from plan_service import get_plan_storage_quota, get_plan_credit_quota

            trial_end = datetime.now() + timedelta(days=days)
            storage_quota = get_plan_storage_quota(plan_name)
            monthly_credits = get_plan_credit_quota(plan_name)

            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """UPDATE users
                       SET plan = %s,
                           trial_end_date = %s,
                           subscription_status = 'trialing',
                           storage_quota = %s,
                           credits = %s
                       WHERE id = %s""",
                    (plan_name, trial_end, storage_quota, monthly_credits, user_id)
                )
                conn.commit()
                logger.info(f"Granted {days}-day free trial of {plan_name} to user {user_id}")
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Error granting free trial: {e}", exc_info=True)

    @staticmethod
    def check_trial_expiration():
        """
        Check for expired trials and downgrade users to Standard plan.
        This should be run periodically (e.g., daily cron job).
        """
        try:
            from plan_service import DEFAULT_PLAN, get_plan_storage_quota

            conn = get_db_connection()
            try:
                cursor = conn.cursor()

                # Find users with expired trials
                expired_users = cursor.execute(
                    """SELECT id, username
                       FROM users
                       WHERE trial_end_date IS NOT NULL
                       AND trial_end_date < CURRENT_TIMESTAMP
                       AND subscription_status = 'trialing'
                       AND stripe_subscription_id IS NULL"""
                ).fetchall()

                # Downgrade expired trial users
                for user in expired_users:
                    cursor.execute(
                        """UPDATE users
                           SET plan = %s,
                               subscription_status = 'expired',
                               storage_quota = %s
                           WHERE id = %s""",
                        (DEFAULT_PLAN, get_plan_storage_quota(DEFAULT_PLAN), user['id'])
                    )
                    logger.info(f"Downgraded user {user['username']} after trial expiration")

                conn.commit()

            finally:
                conn.close()

        except Exception as e:
            logger.error(f"Error checking trial expiration: {e}", exc_info=True)
