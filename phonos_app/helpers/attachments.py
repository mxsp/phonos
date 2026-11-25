"""
Helper functions for managing message attachments
"""
import os
import logging
from datetime import datetime
from PIL import Image
from werkzeug.utils import secure_filename
import hashlib

from .database import get_db

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}
ALLOWED_FILE_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'txt', 'csv', 'json', 'xml', 'zip', 'tar', 'gz',
    'py', 'js', 'html', 'css', 'md', 'pdb', 'mol', 'sdf',
    'nb', 'wl', 'wls', 'm', 'mx', 'wxf', 'wdx'
}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
THUMBNAIL_SIZE = (200, 200)


def allowed_file(filename, allowed_extensions=None):
    """Check if file extension is allowed"""
    if allowed_extensions is None:
        allowed_extensions = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_FILE_EXTENSIONS
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed_extensions


def is_image(filename):
    """Check if file is an image"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def generate_thumbnail(image_path, thumbnail_path):
    """Generate thumbnail for image"""
    try:
        with Image.open(image_path) as img:
            # Convert RGBA to RGB if needed
            if img.mode == 'RGBA':
                img = img.convert('RGB')
            img.thumbnail(THUMBNAIL_SIZE)
            img.save(thumbnail_path, 'JPEG', quality=85)
        return True
    except Exception as e:
        logger.warning("Error generating thumbnail: %s", e)
        return False


def save_attachment(file, upload_folder, project_id=None):
    """
    Save uploaded file and generate thumbnail if image
    Returns: (file_path, thumbnail_path, file_type, file_size)
    """
    if not file or file.filename == '':
        return None, None, None, None

    filename = secure_filename(file.filename)
    file_type = filename.rsplit('.', 1)[1].lower() if '.' in filename else 'unknown'

    # Create unique filename with timestamp and hash
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    unique_name = f"{timestamp}_{hashlib.md5(filename.encode()).hexdigest()[:8]}_{filename}"

    # Create directory structure
    if project_id:
        folder_path = os.path.join(upload_folder, 'attachments', str(project_id))
    else:
        folder_path = os.path.join(upload_folder, 'attachments', 'direct')

    os.makedirs(folder_path, exist_ok=True)

    # Save file
    file_path = os.path.join(folder_path, unique_name)
    file.save(file_path)

    # Get file size
    file_size = os.path.getsize(file_path)

    # Generate thumbnail for images
    thumbnail_path = None
    if is_image(filename):
        thumbnail_name = f"thumb_{unique_name.rsplit('.', 1)[0]}.jpg"
        thumbnail_path = os.path.join(folder_path, thumbnail_name)
        if not generate_thumbnail(file_path, thumbnail_path):
            thumbnail_path = None

    return file_path, thumbnail_path, file_type, file_size


def create_attachment(db_path, message_id, file_name, file_path, file_type, file_size, thumbnail_path=None):
    """Create attachment record in database"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    cursor = conn.execute('''
        INSERT INTO message_attachments
        (message_id, file_name, file_path, file_type, file_size, thumbnail_path)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (message_id, file_name, file_path, file_type, file_size, thumbnail_path))

    result = cursor.fetchone()  # id comes from the wrapper's RETURNING id
    attachment_id = result["id"] if result else None
    conn.commit()

    return attachment_id


def get_message_attachments(db_path, message_id):
    """Get all attachments for a message"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    cursor = conn.execute('''
        SELECT * FROM message_attachments
        WHERE message_id = %s
        ORDER BY uploaded_at DESC
    ''', (message_id,))

    attachments = [dict(row) for row in cursor.fetchall()]

    return attachments


def delete_attachment(db_path, attachment_id):
    """Delete attachment record and files"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    # Get attachment info
    cursor = conn.execute('SELECT * FROM message_attachments WHERE id = %s', (attachment_id,))
    attachment = cursor.fetchone()

    if attachment:
        # Delete files
        try:
            if os.path.exists(attachment['file_path']):
                os.remove(attachment['file_path'])
            if attachment['thumbnail_path'] and os.path.exists(attachment['thumbnail_path']):
                os.remove(attachment['thumbnail_path'])
        except Exception as e:
            logger.warning("Error deleting attachment files: %s", e)

        # Delete record
        cursor = conn.execute('DELETE FROM message_attachments WHERE id = %s', (attachment_id,))
        conn.commit()

    return bool(attachment)


def get_attachment_url(file_path, base_url):
    """Convert file path to URL"""
    # Convert absolute path to relative URL
    if file_path.startswith('/'):
        # Extract relative path from absolute
        parts = file_path.split('attachments/')
        if len(parts) > 1:
            return f"{base_url}/uploads/attachments/{parts[1]}"
    return f"{base_url}/uploads/{os.path.basename(file_path)}"
