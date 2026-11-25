"""Stripe subscription and billing routes."""

import logging
from urllib.parse import quote
from flask import jsonify, request, session, redirect, url_for
from phonos_app.helpers.security import csrf_protect
import stripe

try:
    from phonos_app import socketio as _socketio
except Exception:
    _socketio = None

from config import Config
# Use relative imports for services that might be in production/ or parent directory
try:
    from production.stripe_service import StripeService
except ImportError:
    try:
        from stripe_service import StripeService
    except ImportError:
        # Fallback mock if service not found (for testing)
        class StripeService:
            pass

logger = logging.getLogger(__name__)


def _verified_stripe_event(payload, sig_header):
    """Return ``(event, None)`` for a correctly signed Stripe event, else ``(None, response)``.

    Without ``STRIPE_WEBHOOK_SECRET`` nothing is accepted. Until 2026-10 an empty
    secret meant "parse the body as JSON and trust it", so anyone who could POST
    here could send ``checkout.session.completed`` and get a paid plan.
    """
    secret = Config.STRIPE_WEBHOOK_SECRET
    if not secret:
        logger.error("STRIPE_WEBHOOK_SECRET not set - refusing webhook (unsigned events are never accepted)")
        return None, (jsonify({"error": "Webhook not configured"}), 503)
    try:
        return stripe.Webhook.construct_event(payload, sig_header, secret), None
    except ValueError as e:
        logger.error(f"Invalid webhook payload: {e}")
        return None, (jsonify({"error": "Invalid payload"}), 400)
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid webhook signature: {e}")
        return None, (jsonify({"error": "Invalid signature"}), 400)


def register_stripe_routes(app):
    """Register Stripe-related routes with the Flask app."""

    @app.route('/api/subscription/create-checkout-session', methods=['POST'])
    @csrf_protect
    def create_checkout_session():
        """Create a Stripe Checkout session for subscription."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            data = request.get_json()
            plan_name = data.get('plan_name')

            if not plan_name:
                return jsonify({"error": "Plan name is required"}), 400

            # Validate plan
            if plan_name not in ["Premium", "Managed Premium", "Enterprise"]:
                return jsonify({"error": "Invalid plan"}), 400

            user_id = session['user_id']

            # Build success and cancel URLs
            success_url = request.url_root.rstrip('/') + url_for('subscription_success')
            cancel_url = request.url_root.rstrip('/') + url_for('subscription_cancel')

            # Create checkout session
            result = StripeService.create_checkout_session(
                user_id=user_id,
                plan_name=plan_name,
                success_url=success_url,
                cancel_url=cancel_url
            )

            if not result:
                return jsonify({"error": "Failed to create checkout session"}), 500

            return jsonify(result), 200

        except Exception as e:
            logger.error(f"Error creating checkout session: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/api/subscription/payment-link', methods=['GET'])
    def get_payment_link():
        """Get the Premium Stripe Payment Link URL with user parameters (locked email)."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            # Import here to avoid circular import
            from phonos_app.helpers.database import get_db

            # Use configured payment link or fall back to hardcoded default
            payment_link_base = Config.STRIPE_PREMIUM_PAYMENT_LINK or ''

            # Get user details
            user_id = session['user_id']
            db = get_db()

            # Get user email from database
            user = db.execute(
                """SELECT u.id, u.username, uc.email
                   FROM users u
                   JOIN user_credentials uc ON u.user_credential_id = uc.id
                   WHERE u.id = %s""",
                (user_id,)
            ).fetchone()

            if not user:
                return jsonify({"error": "User not found"}), 404

            # Build payment link with user ID and LOCKED email (non-changeable)
            # Using 'customer_email' instead of 'prefilled_email' locks the field
            payment_link = f"{payment_link_base}?client_reference_id={user_id}&customer_email={quote(user['email'])}"

            return jsonify({
                "url": payment_link,
                "plan_name": "Premium"
            }), 200

        except Exception as e:
            logger.error(f"Error generating payment link: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/api/subscription/billing-portal', methods=['POST'])
    @csrf_protect
    def create_billing_portal():
        """Create a Stripe billing portal session."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            # Import get_db using absolute import to avoid relative import issues
            from phonos_app.helpers.database import get_db

            user_id = session['user_id']

            # Get return URL
            try:
                settings_url = url_for('oversight')
            except Exception:
                settings_url = '/'

            return_url = request.url_root.rstrip('/') + settings_url

            # Get user's Stripe customer ID from database
            db = get_db()
            user = db.execute(
                "SELECT stripe_customer_id FROM users WHERE id = %s",
                (user_id,)
            ).fetchone()

            if not user or not user['stripe_customer_id']:
                logger.error(f"No Stripe customer ID for user {user_id}")
                return jsonify({"error": "No Stripe subscription found. Please subscribe first."}), 400

            # Create billing portal session directly with Stripe API
            if not Config.STRIPE_SECRET_KEY:
                return jsonify({"error": "Stripe not configured"}), 503

            stripe.api_key = Config.STRIPE_SECRET_KEY
            portal_session = stripe.billing_portal.Session.create(
                customer=user['stripe_customer_id'],
                return_url=return_url,
            )

            logger.info(f"Created billing portal session for user {user_id}")
            return jsonify({"url": portal_session.url}), 200

        except stripe.StripeError as e:
            logger.error(f"Stripe error creating billing portal: {e}", exc_info=True)
            return jsonify({"error": f"Stripe error: {str(e)}"}), 400
        except Exception as e:
            logger.error(f"Error creating billing portal: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/api/subscription/status', methods=['GET'])
    def get_subscription_status():
        """Get the current user's subscription status."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            from ..helpers.database import get_db
            from datetime import datetime

            user_id = session['user_id']
            db = get_db()
            user = db.execute(
                """SELECT plan, subscription_status, subscription_end_date,
                          trial_end_date, stripe_subscription_id
                   FROM users
                   WHERE id = %s""",
                (user_id,)
            ).fetchone()

            if not user:
                return jsonify({"error": "User not found"}), 404

            # Determine effective status - if cancelled but still within billing period,
            # user should still have premium access
            effective_status = user['subscription_status']
            subscription_end = user['subscription_end_date']

            # Check if user is in "canceling" state but still has time remaining
            if effective_status == 'canceling' and subscription_end:
                if isinstance(subscription_end, str):
                    try:
                        subscription_end = datetime.fromisoformat(subscription_end.replace('Z', '+00:00'))
                    except ValueError:
                        pass
                if isinstance(subscription_end, datetime) and subscription_end > datetime.utcnow():
                    effective_status = 'active_until_period_end'

            return jsonify({
                "plan": user['plan'],
                "subscription_status": user['subscription_status'],
                "effective_status": effective_status,
                "subscription_end_date": str(user['subscription_end_date']) if user['subscription_end_date'] else None,
                "trial_end_date": str(user['trial_end_date']) if user['trial_end_date'] else None,
                "has_subscription": bool(user['stripe_subscription_id'])
            }), 200

        except Exception as e:
            logger.error(f"Error getting subscription status: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/api/webhooks/stripe', methods=['POST'])
    def stripe_webhook():
        """Handle Stripe webhook events."""
        event, error = _verified_stripe_event(request.data, request.headers.get('Stripe-Signature'))
        if error:
            return error

        # Handle the event
        event_type = event['type']
        data = event['data']['object']

        try:
            if event_type == 'checkout.session.completed':
                # Payment successful, link customer to user and update plan
                session_data = data
                customer_id = session_data.get('customer')
                metadata = session_data.get('metadata', {})
                user_id = metadata.get('user_id')
                plan_name = metadata.get('plan_name', 'Premium')
                subscription_id = session_data.get('subscription')

                # Support client_reference_id for Payment Links
                if not user_id:
                    client_ref = session_data.get('client_reference_id')
                    if client_ref:
                        user_id = client_ref

                logger.info(f"Checkout session completed: {session_data.get('id')} for user {user_id}")

                # If no user_id in metadata, try to find user by customer email
                if not user_id and customer_id:
                    from phonos_app.helpers.database import get_db

                    # Get customer email from Stripe
                    customer_email = session_data.get('customer_email') or session_data.get('customer_details', {}).get('email')

                    if customer_email:
                        db = get_db()
                        # Look up user by email or existing stripe_customer_id
                        user_result = db.execute(
                            """SELECT u.id FROM users u
                               JOIN user_credentials uc ON u.user_credential_id = uc.id
                               WHERE uc.email = %s OR u.stripe_customer_id = %s
                               LIMIT 1""",
                            (customer_email, customer_id)
                        ).fetchone()

                        if user_result:
                            user_id = str(user_result['id'])
                            logger.info(f"Found user {user_id} by email {customer_email}")

                if user_id and customer_id:
                    from phonos_app.helpers.database import get_db
                    from plan_service import get_plan_storage_quota, get_plan_credit_quota

                    db = get_db()
                    storage_quota = get_plan_storage_quota(plan_name)
                    monthly_credits = get_plan_credit_quota(plan_name)

                    # Link Stripe customer to user and upgrade plan
                    db.execute(
                        """UPDATE users
                           SET stripe_customer_id = %s,
                               stripe_subscription_id = %s,
                               plan = %s,
                               subscription_status = 'active',
                               storage_quota = %s,
                               credits = %s
                           WHERE id = %s""",
                        (customer_id, subscription_id, plan_name, storage_quota, monthly_credits, user_id)
                    )
                    db.commit()
                    logger.info(f"Upgraded user {user_id} to {plan_name}")
                    # Notify the user's browser in real-time
                    if _socketio:
                        try:
                            _socketio.emit('user_info_update', room=f'user_{user_id}')
                            _socketio.emit('subscription_updated', {'plan': plan_name}, room=f'user_{user_id}')
                        except Exception as se:
                            logger.warning(f"Could not emit subscription socket for user {user_id}: {se}")
                else:
                    logger.warning(f"Could not find user for checkout session {session_data.get('id')}. Customer: {customer_id}")

            elif event_type == 'customer.subscription.created':
                StripeService.handle_subscription_created(data)

            elif event_type == 'customer.subscription.updated':
                StripeService.handle_subscription_updated(data)

            elif event_type == 'customer.subscription.deleted':
                StripeService.handle_subscription_deleted(data)

            elif event_type == 'invoice.paid':
                StripeService.handle_invoice_paid(data)

            elif event_type == 'invoice.payment_failed':
                logger.warning(f"Payment failed for customer: {data.get('customer')}")
                # You might want to notify the user or take action here

            else:
                logger.info(f"Unhandled webhook event type: {event_type}")

            return jsonify({"status": "success"}), 200

        except Exception as e:
            logger.error(f"Error handling webhook event {event_type}: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/stripe_premium', methods=['POST'])
    def stripe_premium_webhook():
        """Handle Stripe webhook events at custom endpoint."""
        event, error = _verified_stripe_event(request.data, request.headers.get('Stripe-Signature'))
        if error:
            return error

        # Handle the event
        event_type = event['type']
        data = event['data']['object']

        try:
            if event_type == 'checkout.session.completed':
                # Payment successful, link customer to user and update plan
                session_data = data
                customer_id = session_data.get('customer')
                metadata = session_data.get('metadata', {})
                user_id = metadata.get('user_id')
                plan_name = metadata.get('plan_name', 'Premium')
                subscription_id = session_data.get('subscription')

                # Support client_reference_id for Payment Links
                if not user_id:
                    client_ref = session_data.get('client_reference_id')
                    if client_ref:
                        user_id = client_ref

                logger.info(f"Checkout session completed: {session_data.get('id')} for user {user_id}")

                # If no user_id in metadata, try to find user by customer email
                if not user_id and customer_id:
                    from phonos_app.helpers.database import get_db

                    # Get customer email from Stripe
                    customer_email = session_data.get('customer_email') or session_data.get('customer_details', {}).get('email')

                    if customer_email:
                        db = get_db()
                        # Look up user by email or existing stripe_customer_id
                        user_result = db.execute(
                            """SELECT u.id FROM users u
                               JOIN user_credentials uc ON u.user_credential_id = uc.id
                               WHERE uc.email = %s OR u.stripe_customer_id = %s
                               LIMIT 1""",
                            (customer_email, customer_id)
                        ).fetchone()

                        if user_result:
                            user_id = str(user_result['id'])
                            logger.info(f"Found user {user_id} by email {customer_email}")

                if user_id and customer_id:
                    from phonos_app.helpers.database import get_db
                    from plan_service import get_plan_storage_quota, get_plan_credit_quota

                    db = get_db()
                    storage_quota = get_plan_storage_quota(plan_name)
                    monthly_credits = get_plan_credit_quota(plan_name)

                    # Link Stripe customer to user and upgrade plan
                    db.execute(
                        """UPDATE users
                           SET stripe_customer_id = %s,
                               stripe_subscription_id = %s,
                               plan = %s,
                               subscription_status = 'active',
                               storage_quota = %s,
                               credits = %s
                           WHERE id = %s""",
                        (customer_id, subscription_id, plan_name, storage_quota, monthly_credits, user_id)
                    )
                    db.commit()
                    logger.info(f"Upgraded user {user_id} to {plan_name}")
                    # Notify the user's browser in real-time
                    if _socketio:
                        try:
                            _socketio.emit('user_info_update', room=f'user_{user_id}')
                            _socketio.emit('subscription_updated', {'plan': plan_name}, room=f'user_{user_id}')
                        except Exception as se:
                            logger.warning(f"Could not emit subscription socket for user {user_id}: {se}")
                else:
                    logger.warning(f"Could not find user for checkout session {session_data.get('id')}. Customer: {customer_id}")

            elif event_type == 'customer.subscription.created':
                StripeService.handle_subscription_created(data)

            elif event_type == 'customer.subscription.updated':
                StripeService.handle_subscription_updated(data)

            elif event_type == 'customer.subscription.deleted':
                StripeService.handle_subscription_deleted(data)

            elif event_type == 'invoice.paid':
                StripeService.handle_invoice_paid(data)

            elif event_type == 'invoice.payment_failed':
                logger.warning(f"Payment failed for customer: {data.get('customer')}")
                # You might want to notify the user or take action here

            else:
                logger.info(f"Unhandled webhook event type: {event_type}")

            return jsonify({"status": "success"}), 200

        except Exception as e:
            logger.error(f"Error handling webhook event {event_type}: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/subscription/success')
    def subscription_success():
        """Handle successful subscription."""
        return redirect(url_for('oversight') + '?subscription=success')

    @app.route('/subscription/cancel')
    def subscription_cancel():
        """Handle cancelled subscription."""
        return redirect(url_for('oversight') + '?subscription=cancelled')

    @app.route('/api/subscription/config', methods=['GET'])
    def get_stripe_config():
        """Get Stripe publishable key for frontend."""
        return jsonify({
            "publishable_key": Config.STRIPE_PUBLISHABLE_KEY,
            "stripe_enabled": bool(Config.STRIPE_SECRET_KEY)
        }), 200

    @app.route('/api/subscription/cancel', methods=['POST'])
    @csrf_protect
    def cancel_subscription():
        """Cancel the user's subscription."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            from ..helpers.database import get_db

            user_id = session['user_id']
            db = get_db()

            user = db.execute(
                """SELECT stripe_subscription_id, plan
                   FROM users
                   WHERE id = %s""",
                (user_id,)
            ).fetchone()

            if not user:
                return jsonify({"error": "User not found"}), 404

            if not user['stripe_subscription_id']:
                return jsonify({"error": "No active subscription found"}), 400

            # Cancel the subscription at period end (user keeps access until end of billing period)
            stripe.api_key = Config.STRIPE_SECRET_KEY
            subscription = stripe.Subscription.modify(
                user['stripe_subscription_id'],
                cancel_at_period_end=True
            )

            # Update user's subscription status
            db.execute(
                """UPDATE users
                   SET subscription_status = 'canceling'
                   WHERE id = %s""",
                (user_id,)
            )
            db.commit()

            logger.info(f"Subscription cancelled for user {user_id}")
            return jsonify({
                "message": "Subscription will be cancelled at the end of the billing period",
                "cancel_at": subscription.cancel_at
            }), 200

        except stripe.error.StripeError as e:
            logger.error(f"Stripe error cancelling subscription: {e}")
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Error cancelling subscription: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
