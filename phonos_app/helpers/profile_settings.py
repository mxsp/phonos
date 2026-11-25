"""
Helper functions for managing public profile settings
"""
import json
import logging

from .database import get_db

logger = logging.getLogger(__name__)


# ============================================================================
# USER PUBLIC PROFILE SETTINGS
# ============================================================================

def get_user_profile_settings(db_path, user_id):
    """Get user's public profile settings"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    cursor = conn.execute('''
        SELECT * FROM public_profile_settings WHERE user_id = %s
    ''', (user_id,))

    settings = cursor.fetchone()

    if settings:
        return dict(settings)
    else:
        # Return defaults
        return {
            'user_id': user_id,
            'show_bio': 1,
            'show_affiliation': 1,
            'show_location': 0,
            'show_public_projects': 1,
            'show_statistics': 1,
            'show_badges': 1,
            'show_activity': 0,
            'show_social_links': 1,
            'bio': None,
            'affiliation': None,
            'location': None,
            'website_url': None,
            'twitter_handle': None,
            'github_handle': None,
            'linkedin_url': None,
            'custom_theme_color': None,
            'custom_banner_image': None
        }


def update_user_profile_settings(db_path, user_id, settings):
    """Update user's public profile settings"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    try:
        # Check if settings exist
        cursor = conn.execute('SELECT id FROM public_profile_settings WHERE user_id = %s', (user_id,))
        exists = cursor.fetchone()

        if exists:
            # Update existing
            cursor = conn.execute('''
                UPDATE public_profile_settings
                SET show_bio = %s,
                    show_affiliation = %s,
                    show_location = %s,
                    show_public_projects = %s,
                    show_statistics = %s,
                    show_badges = %s,
                    show_activity = %s,
                    show_social_links = %s,
                    bio = %s,
                    affiliation = %s,
                    location = %s,
                    website_url = %s,
                    twitter_handle = %s,
                    github_handle = %s,
                    linkedin_url = %s,
                    custom_theme_color = %s,
                    custom_banner_image = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            ''', (
                settings.get('show_bio', 1),
                settings.get('show_affiliation', 1),
                settings.get('show_location', 0),
                settings.get('show_public_projects', 1),
                settings.get('show_statistics', 1),
                settings.get('show_badges', 1),
                settings.get('show_activity', 0),
                settings.get('show_social_links', 1),
                settings.get('bio'),
                settings.get('affiliation'),
                settings.get('location'),
                settings.get('website_url'),
                settings.get('twitter_handle'),
                settings.get('github_handle'),
                settings.get('linkedin_url'),
                settings.get('custom_theme_color'),
                settings.get('custom_banner_image'),
                user_id
            ))
        else:
            # Insert new
            cursor = conn.execute('''
                INSERT INTO public_profile_settings (
                    user_id, show_bio, show_affiliation, show_location,
                    show_public_projects, show_statistics, show_badges,
                    show_activity, show_social_links, bio, affiliation,
                    location, website_url, twitter_handle, github_handle,
                    linkedin_url, custom_theme_color, custom_banner_image
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                user_id,
                settings.get('show_bio', 1),
                settings.get('show_affiliation', 1),
                settings.get('show_location', 0),
                settings.get('show_public_projects', 1),
                settings.get('show_statistics', 1),
                settings.get('show_badges', 1),
                settings.get('show_activity', 0),
                settings.get('show_social_links', 1),
                settings.get('bio'),
                settings.get('affiliation'),
                settings.get('location'),
                settings.get('website_url'),
                settings.get('twitter_handle'),
                settings.get('github_handle'),
                settings.get('linkedin_url'),
                settings.get('custom_theme_color'),
                settings.get('custom_banner_image')
            ))

        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error updating profile settings: {e}")
        conn.rollback()
        return False


# ============================================================================
# PROJECT PROFILE SETTINGS
# ============================================================================

def get_project_profile_settings(db_path, project_id):
    """Get project's public profile settings"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    cursor = conn.execute('''
        SELECT * FROM project_profile_settings WHERE project_id = %s
    ''', (project_id,))

    settings = cursor.fetchone()

    if settings:
        result = dict(settings)
        # Parse custom_sections if it's JSON
        if result.get('custom_sections'):
            try:
                result['custom_sections'] = json.loads(result['custom_sections'])
            except:
                result['custom_sections'] = None
        return result
    else:
        # Return defaults
        return {
            'project_id': project_id,
            'show_description': 1,
            'show_collaborators': 1,
            'show_files': 1,
            'show_jobs': 1,
            'show_statistics': 1,
            'show_activity': 1,
            'show_comments': 1,
            'show_fork_button': 1,
            'show_download_button': 1,
            'custom_sections': None
        }


def update_project_profile_settings(db_path, project_id, settings):
    """Update project's public profile settings"""
    conn = get_db()  # request-scoped; db_path kept only for API compatibility
    try:
        # Check if settings exist
        cursor = conn.execute('SELECT id FROM project_profile_settings WHERE project_id = %s', (project_id,))
        exists = cursor.fetchone()

        # Convert custom_sections to JSON if it's a dict/list
        custom_sections = settings.get('custom_sections')
        if custom_sections and not isinstance(custom_sections, str):
            custom_sections = json.dumps(custom_sections)

        if exists:
            # Update existing
            cursor = conn.execute('''
                UPDATE project_profile_settings
                SET show_description = %s,
                    show_collaborators = %s,
                    show_files = %s,
                    show_jobs = %s,
                    show_statistics = %s,
                    show_activity = %s,
                    show_comments = %s,
                    show_fork_button = %s,
                    show_download_button = %s,
                    custom_sections = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE project_id = %s
            ''', (
                settings.get('show_description', 1),
                settings.get('show_collaborators', 1),
                settings.get('show_files', 1),
                settings.get('show_jobs', 1),
                settings.get('show_statistics', 1),
                settings.get('show_activity', 1),
                settings.get('show_comments', 1),
                settings.get('show_fork_button', 1),
                settings.get('show_download_button', 1),
                custom_sections,
                project_id
            ))
        else:
            # Insert new
            cursor = conn.execute('''
                INSERT INTO project_profile_settings (
                    project_id, show_description, show_collaborators,
                    show_files, show_jobs, show_statistics, show_activity,
                    show_comments, show_fork_button, show_download_button,
                    custom_sections
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                project_id,
                settings.get('show_description', 1),
                settings.get('show_collaborators', 1),
                settings.get('show_files', 1),
                settings.get('show_jobs', 1),
                settings.get('show_statistics', 1),
                settings.get('show_activity', 1),
                settings.get('show_comments', 1),
                settings.get('show_fork_button', 1),
                settings.get('show_download_button', 1),
                custom_sections
            ))

        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error updating project profile settings: {e}")
        conn.rollback()
        return False
