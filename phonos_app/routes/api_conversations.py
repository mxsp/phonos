"""
API routes for managing AI search conversations
"""

import psycopg2
from flask import jsonify, request, session
from ..app import app, logger
from ..helpers import login_required, get_db


@app.route('/api/conversations', methods=['GET'])
@login_required
def get_conversations():
    """Get all conversations for the current user, ordered by most recent"""
    try:
        user_id = session['user_id']
        db = get_db()

        # Get last 10 conversations with message count
        conversations = db.execute('''
            SELECT
                c.id,
                c.title,
                c.created_at,
                c.updated_at,
                COUNT(m.id) as message_count,
                MAX(m.created_at) as last_message_at
            FROM ai_conversations c
            LEFT JOIN ai_conversation_messages m ON c.id = m.conversation_id
            WHERE c.user_id = %s
            GROUP BY c.id, c.title, c.created_at, c.updated_at
            ORDER BY c.updated_at DESC
            LIMIT 10
        ''', (user_id,)).fetchall()

        result = []
        for conv in conversations:
            result.append({
                'id': conv['id'],
                'title': conv['title'] or 'Untitled Conversation',
                'created_at': str(conv['created_at']),
                'updated_at': str(conv['updated_at']),
                'message_count': conv['message_count'],
                'last_message_at': str(conv['last_message_at']) if conv['last_message_at'] else None
            })

        return jsonify({
            'status': 'success',
            'conversations': result
        })

    except Exception as e:
        logger.error(f"Error fetching conversations: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/conversations/<int:conversation_id>', methods=['GET'])
@login_required
def get_conversation(conversation_id):
    """Get a specific conversation with all its messages"""
    try:
        user_id = session['user_id']
        db = get_db()

        # Verify user owns this conversation
        conversation = db.execute('''
            SELECT id, title, created_at, updated_at
            FROM ai_conversations
            WHERE id = %s AND user_id = %s
        ''', (conversation_id, user_id)).fetchone()

        if not conversation:
            return jsonify({'status': 'error', 'message': 'Conversation not found'}), 404

        # Get all messages
        messages = db.execute('''
            SELECT id, role, content, created_at
            FROM ai_conversation_messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC
        ''', (conversation_id,)).fetchall()

        return jsonify({
            'status': 'success',
            'conversation': {
                'id': conversation['id'],
                'title': conversation['title'],
                'created_at': str(conversation['created_at']),
                'updated_at': str(conversation['updated_at']),
                'messages': [{
                    'id': msg['id'],
                    'role': msg['role'],
                    'content': msg['content'],
                    'created_at': str(msg['created_at'])
                } for msg in messages]
            }
        })

    except Exception as e:
        logger.error(f"Error fetching conversation: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/conversations', methods=['POST'])
@login_required
def create_conversation():
    """Create a new conversation or save messages to existing one"""
    try:
        user_id = session['user_id']
        data = request.get_json()

        conversation_id = data.get('conversation_id')
        title = data.get('title', 'New Conversation')
        messages = data.get('messages', [])

        db = get_db()

        # If conversation_id is provided, update existing conversation
        if conversation_id:
            # Verify user owns this conversation
            conv = db.execute('''
                SELECT id FROM ai_conversations
                WHERE id = %s AND user_id = %s
            ''', (conversation_id, user_id)).fetchone()

            if not conv:
                return jsonify({'status': 'error', 'message': 'Conversation not found'}), 404

            # Update timestamp
            db.execute('''
                UPDATE ai_conversations
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            ''', (conversation_id,))
        else:
            # Create new conversation
            result = db.execute('''
                INSERT INTO ai_conversations (user_id, title, created_at, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
            ''', (user_id, title))
            # Use lastrowid property instead of fetchone() since the cursor consumes it in __init__
            conversation_id = result.lastrowid
            if not conversation_id:
                raise ValueError("Failed to create conversation")

        # Add messages
        for msg in messages:
            db.execute('''
                INSERT INTO ai_conversation_messages (conversation_id, role, content, created_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ''', (conversation_id, msg['role'], msg['content']))

        db.commit()

        return jsonify({
            'status': 'success',
            'conversation_id': conversation_id
        })

    except Exception as e:
        logger.error(f"Error creating/updating conversation: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/conversations/<int:conversation_id>', methods=['DELETE'])
@login_required
def delete_conversation(conversation_id):
    """Delete a conversation"""
    try:
        user_id = session['user_id']
        db = get_db()

        # Verify user owns this conversation
        conv = db.execute('''
            SELECT id FROM ai_conversations
            WHERE id = %s AND user_id = %s
        ''', (conversation_id, user_id)).fetchone()

        if not conv:
            return jsonify({'status': 'error', 'message': 'Conversation not found'}), 404

        # Delete messages first (if not using CASCADE)
        db.execute('DELETE FROM ai_conversation_messages WHERE conversation_id = %s', (conversation_id,))

        # Delete conversation
        db.execute('DELETE FROM ai_conversations WHERE id = %s', (conversation_id,))

        db.commit()

        return jsonify({
            'status': 'success',
            'message': 'Conversation deleted'
        })

    except Exception as e:
        logger.error(f"Error deleting conversation: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/conversations/<int:conversation_id>/title', methods=['PUT'])
@login_required
def update_conversation_title(conversation_id):
    """Update conversation title"""
    try:
        user_id = session['user_id']
        data = request.get_json()
        title = data.get('title', '').strip()

        if not title:
            return jsonify({'status': 'error', 'message': 'Title is required'}), 400

        db = get_db()

        # Verify user owns this conversation and update title
        result = db.execute('''
            UPDATE ai_conversations
            SET title = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND user_id = %s
            RETURNING id
        ''', (title, conversation_id, user_id))

        if not result.fetchone():
            return jsonify({'status': 'error', 'message': 'Conversation not found'}), 404

        db.commit()

        return jsonify({
            'status': 'success',
            'message': 'Title updated'
        })

    except Exception as e:
        logger.error(f"Error updating conversation title: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500
