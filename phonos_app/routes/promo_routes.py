"""Promo code routes for granting premium access."""

import logging
from flask import jsonify, request, session

from promo_code_service import PromoCodeService

logger = logging.getLogger(__name__)


def register_promo_routes(app):
    """Register promo code routes with the Flask app."""

    @app.route('/api/promo/apply', methods=['POST'])
    def apply_promo_code():
        """Apply a promo code to grant premium access."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            data = request.get_json()
            code = data.get('code', '').strip()

            if not code:
                return jsonify({"error": "Promo code is required"}), 400

            user_id = session['user_id']
            username = session.get('username', 'Unknown')

            # Apply the promo code
            result = PromoCodeService.apply_promo_code(user_id, code, username)

            if result['success']:
                return jsonify(result), 200
            else:
                return jsonify(result), 400

        except Exception as e:
            logger.error(f"Error applying promo code: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/api/promo/history', methods=['GET'])
    def get_promo_history():
        """Get promo code usage history for the current user."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            user_id = session['user_id']
            history = PromoCodeService.get_promo_code_usage(user_id)

            return jsonify({"history": history}), 200

        except Exception as e:
            logger.error(f"Error fetching promo history: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @app.route('/api/promo/validate', methods=['POST'])
    def validate_promo_code():
        """Validate if a promo code is valid (without applying it)."""
        if 'user_id' not in session:
            return jsonify({"error": "Not authenticated"}), 401

        try:
            data = request.get_json()
            code = data.get('code', '').strip()

            if not code:
                return jsonify({"valid": False, "message": "Promo code is required"}), 400

            is_valid = PromoCodeService.validate_promo_code(code)

            if is_valid:
                return jsonify({
                    "valid": True,
                    "message": "Valid promo code! Click 'Redeem' to activate."
                }), 200
            else:
                return jsonify({
                    "valid": False,
                    "message": "Invalid promo code"
                }), 200

        except Exception as e:
            logger.error(f"Error validating promo code: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
