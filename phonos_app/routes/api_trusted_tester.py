"""Trusted Tester Program routes: apply, check status, admin review, graduate to regular access.

The program page (/trusted-tester) is a server-rendered form, so it works for applicants the
access gate keeps out of the dashboard SPA. The JSON API serves the SPA and the admin panel.
The tier and status rules live in phonos_app/helpers/trusted_tester.py.
"""
from __future__ import annotations

from flask import jsonify, redirect, render_template, request, session, url_for
from markupsafe import escape

from ..app import app, logger, socketio
from ..helpers import admin_required, get_db, login_required
from ..helpers.emails import send_email
from ..helpers.notifications import create_user_notification
from ..helpers.security import csrf_protect, rate_limit
from ..helpers import trusted_tester as tt


def _clean(value, limit):
    return (str(value).strip()[:limit] or None) if value is not None else None


def _serialize(row):
    if not row:
        return None
    out = dict(row)
    for key in ('applied_at', 'reviewed_at', 'graduated_at'):
        if out.get(key) is not None and hasattr(out[key], 'isoformat'):
            out[key] = out[key].isoformat()
    return out


def _status_payload(db, user_id):
    application = tt.get_application(db, user_id)
    tier = tt.get_access_tier(db, user_id) or tt.TIER_REGULAR
    return {
        'gate_enabled': tt.gate_enabled(),
        'access_tier': tier,
        'is_trusted_tester': bool(application) and application['status'] == tt.STATUS_APPROVED,
        'has_regular_access': tier != tt.TIER_APPLICANT,
        'application': _serialize(application),
    }


def _user_email(db, user_id):
    row = db.execute(
        'SELECT uc.email, u.username FROM users u JOIN user_credentials uc ON uc.user_id = u.id WHERE u.id = %s',
        (user_id,),
    ).fetchone()
    return (row['email'], row['username']) if row else (None, None)


def _notify(db, user_id, title, message):
    """In-app notification plus email; a delivery failure never undoes the state change."""
    try:
        create_user_notification(db, user_id, title, message, category='system',
                                 metadata={'source': 'trusted_tester'})
        db.commit()
    except Exception:
        logger.exception('Trusted tester: notification for user %s failed', user_id)
    email, username = _user_email(db, user_id)
    if email:
        body = (f'<p>Hello {escape(username)},</p><p>{escape(message)}</p>'
                f'<p><a href="{url_for("trusted_tester_page", _external=True)}">Trusted Tester Program</a></p>'
                '<p>— The Phonos team, FyreFly Systems GbR</p>')
        try:
            send_email(email, f'Phonos: {title}', body)
        except Exception:
            logger.exception('Trusted tester: email to user %s failed', user_id)


def _apply(db, user_id, data):
    """Create or re-open an application. Returns (http_status, payload)."""
    motivation = _clean(data.get('motivation'), tt.MOTIVATION_MAX)
    if not motivation or len(motivation) < 20:
        return 400, {'status': 'error', 'message': 'Tell us in a few sentences (at least 20 characters) what you want to test.'}
    institution = _clean(data.get('institution'), tt.FIELD_MAX)
    use_case = _clean(data.get('use_case'), tt.FIELD_MAX)

    existing = tt.get_application(db, user_id)
    if existing and existing['status'] in (tt.STATUS_PENDING, tt.STATUS_APPROVED, tt.STATUS_GRADUATED):
        return 409, {'status': 'error', 'message': f'You already have an application ({existing["status"]}).',
                     'application': _serialize(existing)}

    if existing:  # rejected or revoked: apply again
        db.execute(
            'UPDATE trusted_tester_applications SET status = %s, motivation = %s, institution = %s, use_case = %s, '
            'applied_at = CURRENT_TIMESTAMP, reviewed_by = NULL, reviewed_at = NULL, review_note = NULL '
            'WHERE user_id = %s',
            (tt.STATUS_PENDING, motivation, institution, use_case, user_id),
        )
    else:
        db.execute(
            'INSERT INTO trusted_tester_applications (user_id, status, motivation, institution, use_case) '
            'VALUES (%s, %s, %s, %s, %s)',
            (user_id, tt.STATUS_PENDING, motivation, institution, use_case),
        )
    db.commit()

    try:
        for admin in db.execute('SELECT id FROM users WHERE is_admin = 1').fetchall():
            create_user_notification(db, admin['id'], 'New Trusted Tester application',
                                     f'User #{user_id} applied to the Trusted Tester Program.',
                                     category='system', metadata={'source': 'trusted_tester', 'user_id': user_id})
        db.commit()
    except Exception:
        logger.exception('Trusted tester: admin notification failed')

    logger.info('Trusted tester application from user %s', user_id)
    return 201, {'status': 'success', 'message': 'Application received. We will email you when it is reviewed.',
                 **_status_payload(db, user_id)}


# ── Program page (server-rendered, reachable by applicants) ─────────────────

@app.route('/trusted-tester', methods=['GET'])
@login_required
def trusted_tester_page():
    db = get_db()
    return render_template('trusted_tester.html', state=_status_payload(db, session['user_id']),
                           notice=request.args.get('notice'), error=None, form={})


@app.route('/trusted-tester', methods=['POST'])
@login_required
@csrf_protect
@rate_limit(limit=5, window=3600, action='trusted_tester_apply')
def trusted_tester_page_apply():
    db = get_db()
    code, payload = _apply(db, session['user_id'], request.form)
    if code == 201:
        return redirect(url_for('trusted_tester_page', notice='applied'))
    return render_template('trusted_tester.html', state=_status_payload(db, session['user_id']),
                           notice=None, error=payload['message'], form=request.form), code


# ── JSON API ────────────────────────────────────────────────────────────────

@app.route('/api/trusted-tester/status', methods=['GET'])
@login_required
def trusted_tester_status():
    return jsonify({'status': 'success', **_status_payload(get_db(), session['user_id'])})


@app.route('/api/trusted-tester/apply', methods=['POST'])
@login_required
@csrf_protect
@rate_limit(limit=5, window=3600, action='trusted_tester_apply')
def trusted_tester_apply():
    code, payload = _apply(get_db(), session['user_id'], request.get_json(silent=True) or {})
    return jsonify(payload), code


@app.route('/api/admin/trusted-testers', methods=['GET'])
@admin_required
def admin_trusted_testers():
    status = request.args.get('status')
    if status and status not in tt.STATUSES:
        return jsonify({'status': 'error', 'message': 'Unknown status filter.'}), 400
    sql = ('SELECT a.id, a.user_id, a.status, a.motivation, a.institution, a.use_case, a.applied_at, '
           'a.reviewed_at, a.review_note, a.graduated_at, u.username, u.access_tier, uc.email '
           'FROM trusted_tester_applications a JOIN users u ON u.id = a.user_id '
           'LEFT JOIN user_credentials uc ON uc.user_id = u.id')
    params = ()
    if status:
        sql += ' WHERE a.status = %s'
        params = (status,)
    sql += ' ORDER BY a.applied_at DESC, a.id DESC'
    rows = get_db().execute(sql, params).fetchall()
    return jsonify({'status': 'success', 'gate_enabled': tt.gate_enabled(),
                    'applications': [_serialize(r) for r in rows]})


@app.route('/api/admin/trusted-testers/<int:application_id>/<action>', methods=['POST'])
@admin_required
@csrf_protect
@rate_limit(limit=120, window=3600, action='admin_trusted_tester_review')
def admin_trusted_tester_review(application_id, action):
    if action not in tt.ACTIONS:
        return jsonify({'status': 'error', 'message': 'Unknown action.'}), 404
    allowed_from, new_status = tt.ACTIONS[action]
    note = _clean((request.get_json(silent=True) or {}).get('note'), tt.MOTIVATION_MAX)

    db = get_db()
    row = db.execute('SELECT id, user_id, status FROM trusted_tester_applications WHERE id = %s',
                     (application_id,)).fetchone()
    if not row:
        return jsonify({'status': 'error', 'message': 'Application not found.'}), 404
    if row['status'] not in allowed_from:
        return jsonify({'status': 'error',
                        'message': f'Cannot {action} an application that is {row["status"]}.'}), 409

    user_id = row['user_id']
    tier = tt.get_access_tier(db, user_id)
    new_tier = tier
    if action == 'approve' and tier == tt.TIER_APPLICANT:
        new_tier = tt.TIER_TESTER
    elif action == 'graduate':
        new_tier = tt.TIER_REGULAR
    elif action == 'revoke' and tier == tt.TIER_TESTER:
        new_tier = tt.TIER_APPLICANT

    graduated = ', graduated_at = CURRENT_TIMESTAMP' if action == 'graduate' else ''
    db.execute(
        'UPDATE trusted_tester_applications SET status = %s, reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP, '
        f'review_note = %s{graduated} WHERE id = %s',
        (new_status, session['user_id'], note, application_id),
    )
    if new_tier != tier:
        db.execute('UPDATE users SET access_tier = %s WHERE id = %s', (new_tier, user_id))
    db.commit()
    logger.info('Trusted tester application %s: %s by admin %s (tier %s -> %s)',
                application_id, action, session['user_id'], tier, new_tier)

    messages = {
        'approve': ('You are a Trusted Tester',
                    'Your Trusted Tester application was approved. You now have access to Phonos; '
                    'please report anything that breaks or confuses you through the help desk.'),
        'reject': ('Trusted Tester application',
                   'Thank you for applying. We cannot offer you a Trusted Tester place right now; '
                   'you are welcome to apply again later.'),
        'graduate': ('Welcome to regular access',
                     'Thank you for testing Phonos. Your account now has regular access.'),
        'revoke': ('Trusted Tester access ended',
                   'Your Trusted Tester access has ended. Contact us if you think this is a mistake.'),
    }
    title, message = messages[action]
    if note:
        message += f' Note from the team: {note}'
    _notify(db, user_id, title, message)
    socketio.emit('user_info_update', room=f'user_{user_id}')

    return jsonify({'status': 'success', 'application_id': application_id, 'application_status': new_status,
                    'access_tier': new_tier})


__all__: list[str] = []
