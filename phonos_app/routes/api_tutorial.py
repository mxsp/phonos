"""API routes for tutorial/onboarding functionality."""

from flask import jsonify, request, session

from ..app import app, logger
from ..helpers import login_required
from ..helpers.tutorial import (
    get_tutorial_progress,
    update_tutorial_step,
    complete_tutorial,
    dismiss_tutorial,
    reset_tutorial,
    advance_tutorial_step,
    should_show_tutorial,
)


@app.route('/api/tutorial/progress', methods=['GET'])
@login_required
def api_get_tutorial_progress():
    """Get the current user's tutorial progress."""
    user_id = session['user_id']
    progress = get_tutorial_progress(user_id)

    return jsonify({
        'success': True,
        'progress': {
            'tutorial_step': progress['step'],
            'tutorial_completed': 1 if progress['completed'] else 0,
            'tutorial_dismissed': 1 if progress['dismissed'] else 0,
        },
        'should_show': should_show_tutorial(user_id)
    })


@app.route('/api/tutorial/step', methods=['PUT'])
@login_required
def api_update_tutorial_step():
    """Update the current tutorial step."""
    user_id = session['user_id']
    data = request.get_json()

    if not data or 'step' not in data:
        return jsonify({'success': False, 'error': 'Missing step parameter'}), 400

    try:
        step = int(data['step'])
        if step < 0:
            return jsonify({'success': False, 'error': 'Step must be non-negative'}), 400
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'Invalid step value'}), 400

    if update_tutorial_step(user_id, step):
        return jsonify({'success': True, 'step': step})
    else:
        return jsonify({'success': False, 'error': 'Failed to update step'}), 500


@app.route('/api/tutorial/complete', methods=['POST'])
@login_required
def api_complete_tutorial():
    """Mark the tutorial as completed."""
    user_id = session['user_id']

    if complete_tutorial(user_id):
        return jsonify({'success': True, 'message': 'Tutorial completed'})
    else:
        return jsonify({'success': False, 'error': 'Failed to complete tutorial'}), 500


@app.route('/api/tutorial/dismiss', methods=['POST'])
@login_required
def api_dismiss_tutorial():
    """Dismiss/skip the tutorial."""
    user_id = session['user_id']

    if dismiss_tutorial(user_id):
        return jsonify({'success': True, 'message': 'Tutorial dismissed'})
    else:
        return jsonify({'success': False, 'error': 'Failed to dismiss tutorial'}), 500


@app.route('/api/tutorial/reset', methods=['POST'])
@login_required
def api_reset_tutorial():
    """Reset tutorial progress to start over."""
    user_id = session['user_id']

    if reset_tutorial(user_id):
        return jsonify({'success': True, 'message': 'Tutorial reset'})
    else:
        return jsonify({'success': False, 'error': 'Failed to reset tutorial'}), 500


@app.route('/api/tutorial/advance', methods=['POST'])
@login_required
def api_advance_tutorial():
    """Advance to the next tutorial step."""
    user_id = session['user_id']

    new_step = advance_tutorial_step(user_id)
    if new_step is not None:
        return jsonify({'success': True, 'step': new_step})
    else:
        return jsonify({'success': False, 'error': 'Failed to advance step'}), 500
