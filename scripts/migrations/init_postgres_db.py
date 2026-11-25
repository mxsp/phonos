#!/usr/bin/env python3
"""Initialize PostgreSQL database with required schema."""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Default configuration
DEFAULT_PLAN = 'Standard'
DEFAULT_STORAGE_QUOTA = 1024 * 1024 * 1024  # 1 GB

def get_db_connection():
    """Establish a connection to the PostgreSQL database."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        # Fallback for testing if not set, though run_db.md will instruct to set it
        database_url = "sqlite:///phonos.db"
        print(f"WARNING: DATABASE_URL not set, using default: {database_url}")

    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = False
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        raise

def init_db():
    """Create the PostgreSQL database schema."""

    conn = get_db_connection()
    cursor = conn.cursor()

    print("Initializing PostgreSQL database schema...")

    try:
        # Enable UUID extension if needed (optional, but good for future)
        # cursor.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";")

        # Users table
        # Using TIMESTAMP (without time zone) to match naive datetimes used in application
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                user_id TEXT UNIQUE NOT NULL,
                user_credential_id INTEGER UNIQUE,
                username TEXT UNIQUE NOT NULL,
                legal_name TEXT,
                address TEXT,
                company TEXT,
                university TEXT,
                age INTEGER,
                plan TEXT NOT NULL DEFAULT '{DEFAULT_PLAN}',
                credits INTEGER NOT NULL DEFAULT 0,
                storage_quota BIGINT NOT NULL DEFAULT {DEFAULT_STORAGE_QUOTA},
                storage_used BIGINT NOT NULL DEFAULT 0,
                credits_last_reset TIMESTAMP,
                is_admin INTEGER NOT NULL DEFAULT 0,
                profile_picture_url TEXT,
                primary_color TEXT,
                background_image TEXT,
                layout_density TEXT NOT NULL DEFAULT 'cozy',
                table_style TEXT NOT NULL DEFAULT 'zebra',
                notifications_last_cleared TIMESTAMP,
                last_login_at TIMESTAMP,
                last_login_ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP,
                stripe_customer_id TEXT UNIQUE,
                stripe_subscription_id TEXT,
                subscription_status TEXT,
                subscription_end_date TIMESTAMP,
                trial_end_date TIMESTAMP,
                tutorial_step INTEGER NOT NULL DEFAULT 0,
                tutorial_completed INTEGER NOT NULL DEFAULT 0,
                tutorial_dismissed INTEGER NOT NULL DEFAULT 0
            );
        """)

        # User credentials table
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS user_credentials (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                email TEXT UNIQUE NOT NULL,
                password BYTEA NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                plan TEXT NOT NULL DEFAULT '{DEFAULT_PLAN}',
                email_notifications_enabled INTEGER NOT NULL DEFAULT 1,
                verification_token TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tos_accepted INTEGER NOT NULL DEFAULT 0,
                tos_accepted_at TIMESTAMP,
                tos_ip_address TEXT
            );
        """)

        # Two-factor auth table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS two_factor_auth (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                secret_key TEXT NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Login history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS login_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ip_address TEXT,
                user_agent TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Password reset tokens
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id SERIAL PRIMARY KEY,
                user_credential_id INTEGER NOT NULL REFERENCES user_credentials(id) ON DELETE CASCADE,
                token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Account lockouts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS account_lockouts (
                id SERIAL PRIMARY KEY,
                username TEXT,
                ip_address TEXT,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                first_failed_at TIMESTAMP,
                last_failed_at TIMESTAMP,
                locked_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, ip_address)
            );
        """)

        # Rate limits
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                id SERIAL PRIMARY KEY,
                identifier TEXT NOT NULL,
                action TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                window_start TIMESTAMP NOT NULL,
                last_attempt TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(identifier, action)
            );
        """)

        # JWT refresh tokens
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jwt_refresh_tokens (
                id SERIAL PRIMARY KEY,
                token_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revoked INTEGER NOT NULL DEFAULT 0
            );
        """)

        # Teams
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL UNIQUE,
                description TEXT,
                creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Team members
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS team_members (
                id SERIAL PRIMARY KEY,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'Member',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, user_id)
            );
        """)

        # Team invitations
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS team_invitations (
                id SERIAL PRIMARY KEY,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                inviter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                invitee_email TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                role TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Tags
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id SERIAL PRIMARY KEY,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, name)
            );
        """)

        # Project groups
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_groups (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT,
                color TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Project categories
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL UNIQUE,
                description TEXT,
                icon TEXT,
                color TEXT,
                parent_id INTEGER REFERENCES project_categories(id) ON DELETE SET NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Projects
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
                organization_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
                group_id INTEGER REFERENCES project_groups(id) ON DELETE SET NULL,
                name TEXT NOT NULL,
                description TEXT,
                is_private INTEGER NOT NULL DEFAULT 1,
                is_archived INTEGER NOT NULL DEFAULT 0,
                archived_at TIMESTAMP,
                collaboration_billing_model TEXT NOT NULL DEFAULT 'admin_credits',
                auto_open_chat INTEGER NOT NULL DEFAULT 0,
                display_anonymous INTEGER NOT NULL DEFAULT 0,
                category_id INTEGER,
                view_count INTEGER NOT NULL DEFAULT 0,
                download_count INTEGER NOT NULL DEFAULT 0,
                vote_score INTEGER NOT NULL DEFAULT 0,
                published_at TIMESTAMP,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Project assignments
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_assignments (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'Project Contributor',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, user_id)
            );
        """)

        # Project votes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_votes (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                vote_type INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, user_id)
            );
        """)

        # Project comments
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_comments (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                parent_comment_id INTEGER REFERENCES project_comments(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                vote_score INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Project comment votes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_comment_votes (
                id SERIAL PRIMARY KEY,
                comment_id INTEGER NOT NULL REFERENCES project_comments(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                vote_type INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(comment_id, user_id)
            );
        """)

        # Project tags
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_tags (
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (project_id, tag_id)
            );
        """)

        # Files
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                size BIGINT NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Jobs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                job_id TEXT UNIQUE NOT NULL,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT,
                model TEXT NOT NULL,
                sequence TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT,
                output_file TEXT,
                output_file_size BIGINT,
                output_display_name TEXT,
                execution_time REAL,
                cost INTEGER DEFAULT 0,
                input_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
                policy TEXT,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Notifications
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                category TEXT DEFAULT 'system',
                related_job_id TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Direct messages
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS direct_messages (
                id SERIAL PRIMARY KEY,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                attachment_url TEXT,
                attachment_name TEXT,
                attachment_size BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Project messages
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_messages (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
                gemini_response INTEGER NOT NULL DEFAULT 0,
                parent_message_id INTEGER REFERENCES project_messages(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Message attachments
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS message_attachments (
                id SERIAL PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES project_messages(id) ON DELETE CASCADE,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_size BIGINT NOT NULL,
                thumbnail_path TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Call sessions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS call_sessions (
                id SERIAL PRIMARY KEY,
                call_type TEXT NOT NULL,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                initiator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'active'
            );
        """)

        # Call participants
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS call_participants (
                id SERIAL PRIMARY KEY,
                call_id INTEGER NOT NULL REFERENCES call_sessions(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                left_at TIMESTAMP,
                is_muted INTEGER NOT NULL DEFAULT 0,
                is_video_on INTEGER NOT NULL DEFAULT 1
            );
        """)

        # Privacy settings
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_privacy_settings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                allow_invitations INTEGER NOT NULL DEFAULT 1,
                allow_project_chat_messages INTEGER NOT NULL DEFAULT 1,
                allow_direct_messages INTEGER NOT NULL DEFAULT 1,
                show_online_status INTEGER NOT NULL DEFAULT 1,
                show_profile_picture INTEGER NOT NULL DEFAULT 1,
                show_last_seen INTEGER NOT NULL DEFAULT 0,
                allow_team_discovery INTEGER NOT NULL DEFAULT 1,
                allow_project_discovery INTEGER NOT NULL DEFAULT 0,
                allow_email_notifications INTEGER NOT NULL DEFAULT 1,
                allow_collaboration_requests INTEGER NOT NULL DEFAULT 1,
                profile_visibility TEXT NOT NULL DEFAULT 'public',
                project_visibility_default TEXT NOT NULL DEFAULT 'private',
                data_sharing_analytics INTEGER NOT NULL DEFAULT 1,
                data_sharing_third_party INTEGER NOT NULL DEFAULT 0,
                display_username_public_projects INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Public profile settings
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS public_profile_settings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                show_bio INTEGER NOT NULL DEFAULT 1,
                show_affiliation INTEGER NOT NULL DEFAULT 1,
                show_location INTEGER NOT NULL DEFAULT 0,
                show_public_projects INTEGER NOT NULL DEFAULT 1,
                show_statistics INTEGER NOT NULL DEFAULT 1,
                show_badges INTEGER NOT NULL DEFAULT 1,
                show_activity INTEGER NOT NULL DEFAULT 0,
                show_social_links INTEGER NOT NULL DEFAULT 1,
                bio TEXT,
                affiliation TEXT,
                location TEXT,
                website_url TEXT,
                twitter_handle TEXT,
                github_handle TEXT,
                linkedin_url TEXT,
                custom_theme_color TEXT,
                custom_banner_image TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Promo code usage
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS promo_code_usage (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                code_hash TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                plan_granted TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
        """)

        # Help tickets
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS help_tickets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'Medium',
                category TEXT,
                status TEXT NOT NULL DEFAULT 'Open',
                assignee_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Shared links
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shared_links (
                id SERIAL PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                permissions TEXT NOT NULL DEFAULT 'read',
                expires_at TIMESTAMP,
                password TEXT,
                allow_downloads INTEGER NOT NULL DEFAULT 1,
                description TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # API Keys
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id SERIAL PRIMARY KEY,
                key_id TEXT UNIQUE NOT NULL,
                key_hash TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                scopes TEXT,
                last_used_at TIMESTAMP,
                expires_at TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # API Rate Limits
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_rate_limits (
                id SERIAL PRIMARY KEY,
                key_id TEXT NOT NULL REFERENCES api_keys(key_id) ON DELETE CASCADE,
                endpoint TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                window_start TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Collaboration sessions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS collaboration_sessions (
                id SERIAL PRIMARY KEY,
                session_id TEXT UNIQUE NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id INTEGER NOT NULL,
                host_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                collaboration_billing_model TEXT NOT NULL DEFAULT 'admin_credits'
            );
        """)

        # Collaboration participants
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS collaboration_participants (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES collaboration_sessions(session_id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                left_at TIMESTAMP
            );
        """)

        # Bulk job schedules
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bulk_job_schedules (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                cron_schedule TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_run_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Job pipelines
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_pipelines (
                id SERIAL PRIMARY KEY,
                pipeline_id TEXT UNIQUE NOT NULL,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Job pipeline steps
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_pipeline_steps (
                id SERIAL PRIMARY KEY,
                pipeline_id INTEGER NOT NULL REFERENCES job_pipelines(id) ON DELETE CASCADE,
                step_order INTEGER NOT NULL,
                job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                model TEXT NOT NULL,
                config TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                depends_on_step_id INTEGER REFERENCES job_pipeline_steps(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Model overrides
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS model_overrides (
                model_id TEXT PRIMARY KEY,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_visible INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Job tags
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_tags (
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (job_id, tag_id)
            );
        """)

        # Job logs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_logs (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Message reactions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS message_reactions (
                id SERIAL PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES project_messages(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reaction TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(message_id, user_id, reaction)
            );
        """)

        # Pinned messages
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pinned_messages (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL REFERENCES project_messages(id) ON DELETE CASCADE,
                pinned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, message_id)
            );
        """)

        # Invitations (generic?) - Note: team_invitations exists too
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS invitations (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                inviter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                invitee_email TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP
            );
        """)

        # Customer nodes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS customer_nodes (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                node_id TEXT NOT NULL UNIQUE,
                public_key TEXT,
                approved INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Team nodes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS team_nodes (
                id SERIAL PRIMARY KEY,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                display_name TEXT,
                worker_types TEXT,
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_id, node_id)
            );
        """)

        # Help ticket replies
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS help_ticket_replies (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER NOT NULL REFERENCES help_tickets(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Help ticket attachments
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS help_ticket_attachments (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER NOT NULL REFERENCES help_tickets(id) ON DELETE CASCADE,
                reply_id INTEGER REFERENCES help_ticket_replies(id) ON DELETE CASCADE,
                file_path TEXT NOT NULL,
                friendly_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Job templates
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_templates (
                id SERIAL PRIMARY KEY,
                template_id TEXT UNIQUE NOT NULL,
                creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT,
                model TEXT NOT NULL,
                config TEXT NOT NULL,
                is_public INTEGER NOT NULL DEFAULT 0,
                use_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Resource annotations
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS resource_annotations (
                id SERIAL PRIMARY KEY,
                resource_type TEXT NOT NULL,
                resource_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                position_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # File versions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS file_versions (
                id SERIAL PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                path TEXT NOT NULL,
                size BIGINT NOT NULL,
                checksum TEXT,
                created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(file_id, version_number)
            );
        """)

        # Dashboard preferences
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_preferences (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                widgets_config TEXT NOT NULL,
                layout_config TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Usage analytics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usage_analytics (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                credits_used INTEGER NOT NULL DEFAULT 0,
                storage_delta BIGINT NOT NULL DEFAULT 0,
                compute_time REAL,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Budget alerts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS budget_alerts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
                alert_type TEXT NOT NULL,
                threshold_value INTEGER NOT NULL,
                current_value INTEGER NOT NULL DEFAULT 0,
                is_triggered INTEGER NOT NULL DEFAULT 0,
                last_triggered_at TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Project permissions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_permissions (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                resource_type TEXT NOT NULL,
                resource_id INTEGER,
                permission_level TEXT NOT NULL,
                granted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Documentation pages
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documentation_pages (
                id SERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL,
                search_vector TEXT,
                view_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Activity log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                activity_type TEXT NOT NULL,
                activity_description TEXT NOT NULL,
                resource_type TEXT,
                resource_id INTEGER,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # User connections
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_connections (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                connected_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                connection_type TEXT NOT NULL DEFAULT 'contact',
                status TEXT NOT NULL DEFAULT 'accepted',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, connected_user_id)
            );
        """)

        # Call invitations (direct) - Note: check overlap with 'invitations' table logic
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS call_invitations (
                id SERIAL PRIMARY KEY,
                call_id INTEGER NOT NULL REFERENCES call_sessions(id) ON DELETE CASCADE,
                inviter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                invitee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending',
                responded_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Public profile settings (duplicate?) - It seems there are two tables in sqlite script: public_profile_settings and user_privacy_settings?
        # The sqlite script had public_profile_settings TWICE? No, I think I saw it once.
        # Checking my memory/sqlite script...
        # Yes, public_profile_settings was in sqlite script.

        # User preferences (QoL features)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                keyboard_shortcuts_enabled INTEGER NOT NULL DEFAULT 1,
                command_palette_enabled INTEGER NOT NULL DEFAULT 1,
                desktop_notifications_enabled INTEGER NOT NULL DEFAULT 0,
                sound_notifications_enabled INTEGER NOT NULL DEFAULT 0,
                email_digest_frequency TEXT NOT NULL DEFAULT 'none',
                auto_retry_failed_jobs INTEGER NOT NULL DEFAULT 0,
                auto_retry_max_attempts INTEGER NOT NULL DEFAULT 3,
                auto_retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
                default_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                sidebar_collapsed INTEGER NOT NULL DEFAULT 0,
                sidebar_width INTEGER NOT NULL DEFAULT 240,
                theme_mode TEXT NOT NULL DEFAULT 'system',
                saved_searches TEXT DEFAULT '[]',
                recent_searches TEXT DEFAULT '[]',
                pinned_items TEXT DEFAULT '[]',
                onboarding_checklist TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Search history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                query TEXT NOT NULL,
                search_type TEXT NOT NULL DEFAULT 'general',
                filters TEXT,
                result_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Saved searches
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS saved_searches (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                query TEXT NOT NULL,
                search_type TEXT NOT NULL DEFAULT 'general',
                filters TEXT,
                is_pinned INTEGER NOT NULL DEFAULT 0,
                use_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Mentions
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mentions (
                id SERIAL PRIMARY KEY,
                message_id INTEGER REFERENCES project_messages(id) ON DELETE CASCADE,
                mentioned_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mentioner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                context TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Scheduled jobs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                template_id INTEGER REFERENCES job_templates(id) ON DELETE SET NULL,
                name TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_config TEXT NOT NULL,
                next_run_at TIMESTAMP,
                last_run_at TIMESTAMP,
                last_run_status TEXT,
                run_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Webhooks
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhooks (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                secret TEXT,
                events TEXT NOT NULL,
                headers TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_triggered_at TIMESTAMP,
                last_response_code INTEGER,
                failure_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # File comments
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS file_comments (
                id SERIAL PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                parent_id INTEGER REFERENCES file_comments(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                position_data TEXT,
                is_resolved INTEGER NOT NULL DEFAULT 0,
                resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                resolved_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Undo actions (for undo/redo functionality)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS undo_actions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action_type TEXT NOT NULL,
                action_data TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                is_undone INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Project profile settings
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_profile_settings (
                id SERIAL PRIMARY KEY,
                project_id INTEGER UNIQUE NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                show_description INTEGER NOT NULL DEFAULT 1,
                show_collaborators INTEGER NOT NULL DEFAULT 1,
                show_files INTEGER NOT NULL DEFAULT 1,
                show_jobs INTEGER NOT NULL DEFAULT 1,
                show_statistics INTEGER NOT NULL DEFAULT 1,
                show_activity INTEGER NOT NULL DEFAULT 1,
                show_comments INTEGER NOT NULL DEFAULT 1,
                show_fork_button INTEGER NOT NULL DEFAULT 1,
                show_download_button INTEGER NOT NULL DEFAULT 1,
                custom_sections TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Legacy views creation - PostgreSQL supports VIEWs similar to SQLite
        # We can drop and recreate them

        cursor.execute("DROP VIEW IF EXISTS organizations CASCADE;")
        cursor.execute("""
            CREATE VIEW organizations AS
                SELECT
                    t.id,
                    t.name,
                    t.slug,
                    t.description,
                    t.creator_id AS owner_id,
                    t.created_at,
                    t.updated_at
                FROM teams t;
        """)

        cursor.execute("DROP VIEW IF EXISTS organization_members CASCADE;")
        cursor.execute("""
            CREATE VIEW organization_members AS
                SELECT
                    tm.id,
                    tm.team_id AS organization_id,
                    tm.user_id,
                    tm.role,
                    tm.joined_at
                FROM team_members tm;
        """)

        cursor.execute("DROP VIEW IF EXISTS organization_invitations CASCADE;")
        cursor.execute("""
            CREATE VIEW organization_invitations AS
                SELECT
                    ti.id,
                    ti.team_id AS organization_id,
                    ti.inviter_id,
                    ti.invitee_email,
                    ti.token,
                    ti.role,
                    ti.status,
                    ti.expires_at,
                    ti.created_at
                FROM team_invitations ti;
        """)

        cursor.execute("DROP VIEW IF EXISTS project_members CASCADE;")
        cursor.execute("""
            CREATE VIEW project_members AS
                SELECT
                    id,
                    project_id,
                    user_id,
                    CASE
                        WHEN role = 'Project Manager' THEN 'owner'
                        WHEN role = 'Project Contributor' THEN 'collaborator'
                        WHEN role = 'Project Viewer' THEN 'spectator'
                        ELSE role
                    END AS role,
                    created_at
                FROM project_assignments;
        """)

        # Create indexes
        print("Creating indexes...")

        # Function to safe create index
        def create_index(name, table, columns):
             cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns});")

        create_index("idx_users_username", "users", "username")
        create_index("idx_user_credentials_email", "user_credentials", "email")
        create_index("idx_jobs_project_id", "jobs", "project_id")
        create_index("idx_jobs_creator_id", "jobs", "creator_id")
        create_index("idx_jobs_status", "jobs", "status")
        create_index("idx_projects_category_id", "projects", "category_id")
        create_index("idx_projects_vote_score", "projects", "vote_score")
        create_index("idx_projects_is_private", "projects", "is_private")
        create_index("idx_project_messages_project_id", "project_messages", "project_id")
        create_index("idx_project_messages_created_at", "project_messages", "created_at")
        create_index("idx_message_attachments_message_id", "message_attachments", "message_id")
        create_index("idx_call_sessions_project_id", "call_sessions", "project_id")
        create_index("idx_call_participants_call_id", "call_participants", "call_id")
        create_index("idx_pinned_messages_project_id", "pinned_messages", "project_id")
        create_index("idx_call_invitations_call_id", "call_invitations", "call_id")
        create_index("idx_call_invitations_invitee_id", "call_invitations", "invitee_id")
        create_index("idx_call_invitations_status", "call_invitations", "status")
        create_index("idx_public_profile_settings_user_id", "public_profile_settings", "user_id")
        create_index("idx_project_profile_settings_project_id", "project_profile_settings", "project_id")
        create_index("idx_projects_published_at", "projects", "published_at")
        create_index("idx_project_votes_project_id", "project_votes", "project_id")
        create_index("idx_project_votes_user_id", "project_votes", "user_id")
        create_index("idx_project_comments_project_id", "project_comments", "project_id")
        create_index("idx_project_comments_parent_id", "project_comments", "parent_comment_id")
        create_index("idx_project_comment_votes_comment_id", "project_comment_votes", "comment_id")
        create_index("idx_pipeline_steps_pipeline_id", "job_pipeline_steps", "pipeline_id")
        create_index("idx_shared_links_token", "shared_links", "token")
        create_index("idx_annotations_resource", "resource_annotations", "resource_type, resource_id")
        create_index("idx_file_versions_file_id", "file_versions", "file_id")
        create_index("idx_usage_analytics_user_id", "usage_analytics", "user_id")
        create_index("idx_usage_analytics_created_at", "usage_analytics", "created_at")
        create_index("idx_api_keys_user_id", "api_keys", "user_id")
        create_index("idx_collaboration_sessions_resource", "collaboration_sessions", "resource_type, resource_id")
        create_index("idx_activity_log_user_id", "activity_log", "user_id")
        create_index("idx_activity_log_created_at", "activity_log", "created_at")
        create_index("idx_activity_log_activity_type", "activity_log", "activity_type")
        create_index("idx_user_connections_user_id", "user_connections", "user_id")
        create_index("idx_user_connections_status", "user_connections", "status")

        # QoL feature indexes
        create_index("idx_search_history_user_id", "search_history", "user_id")
        create_index("idx_search_history_created_at", "search_history", "created_at")
        create_index("idx_saved_searches_user_id", "saved_searches", "user_id")
        create_index("idx_mentions_mentioned_user_id", "mentions", "mentioned_user_id")
        create_index("idx_mentions_project_id", "mentions", "project_id")
        create_index("idx_mentions_is_read", "mentions", "is_read")
        create_index("idx_scheduled_jobs_user_id", "scheduled_jobs", "user_id")
        create_index("idx_scheduled_jobs_next_run_at", "scheduled_jobs", "next_run_at")
        create_index("idx_scheduled_jobs_is_active", "scheduled_jobs", "is_active")
        create_index("idx_webhooks_user_id", "webhooks", "user_id")
        create_index("idx_webhooks_project_id", "webhooks", "project_id")
        create_index("idx_file_comments_file_id", "file_comments", "file_id")
        create_index("idx_file_comments_project_id", "file_comments", "project_id")
        create_index("idx_undo_actions_user_id", "undo_actions", "user_id")
        create_index("idx_undo_actions_expires_at", "undo_actions", "expires_at")

        conn.commit()
        print("PostgreSQL database initialized successfully!")

    except Exception as e:
        conn.rollback()
        print(f"Error initializing database: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    print("=" * 60)
    print("PostgreSQL Database Initialization")
    print("=" * 60)
    print()
    init_db()
