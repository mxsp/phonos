"""
Optimized Query Helpers for Phonos
Fixes N+1 queries and provides efficient batch loading patterns
"""

from typing import List, Dict, Any, Optional
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class BatchLoader:
    """
    DataLoader-style batch loading to prevent N+1 queries
    Collects IDs and fetches related data in batches
    """

    def __init__(self, db_manager):
        self.db_manager = db_manager
        self._batch_cache = defaultdict(dict)

    def load_attachments_for_messages(self, message_ids: List[int]) -> Dict[int, List[Dict]]:
        """
        Load all attachments for multiple messages in one query

        Args:
            message_ids: List of message IDs

        Returns:
            Dict mapping message_id to list of attachments
        """
        if not message_ids:
            return {}

        placeholders = ','.join(['%s'] * len(message_ids))
        query = f"""
            SELECT
                message_id,
                id,
                file_name,
                file_url,
                file_size,
                file_type,
                created_at
            FROM message_attachments
            WHERE message_id IN ({placeholders})
            ORDER BY created_at ASC
        """

        results = self.db_manager.execute_query(query, tuple(message_ids), readonly=True)

        # Group by message_id
        attachments_by_message = defaultdict(list)
        for row in results or []:
            attachments_by_message[row['message_id']].append(dict(row))

        return dict(attachments_by_message)

    def load_reactions_for_messages(self, message_ids: List[int]) -> Dict[int, List[Dict]]:
        """
        Load all reactions for multiple messages in one query with aggregation

        Args:
            message_ids: List of message IDs

        Returns:
            Dict mapping message_id to list of reaction summaries
        """
        if not message_ids:
            return {}

        placeholders = ','.join(['%s'] * len(message_ids))
        query = f"""
            SELECT
                mr.message_id,
                mr.reaction,
                COUNT(*) as count,
                ARRAY_AGG(u.username ORDER BY mr.created_at) as users
            FROM message_reactions mr
            JOIN users u ON mr.user_id = u.id
            WHERE mr.message_id IN ({placeholders})
            GROUP BY mr.message_id, mr.reaction
            ORDER BY COUNT(*) DESC
        """

        results = self.db_manager.execute_query(query, tuple(message_ids), readonly=True)

        # Group by message_id
        reactions_by_message = defaultdict(list)
        for row in results or []:
            reactions_by_message[row['message_id']].append({
                'reaction': row['reaction'],
                'count': row['count'],
                'users': row['users'][:5]  # Limit to first 5 users to prevent overflow
            })

        return dict(reactions_by_message)

    def load_members_for_projects(self, project_ids: List[int]) -> Dict[int, List[Dict]]:
        """
        Load all members for multiple projects efficiently

        Args:
            project_ids: List of project IDs

        Returns:
            Dict mapping project_id to list of members
        """
        if not project_ids:
            return {}

        placeholders = ','.join(['%s'] * len(project_ids))
        query = f"""
            SELECT
                pa.project_id,
                pa.role,
                u.id as user_id,
                u.username,
                u.profile_picture_url,
                pa.created_at as joined_at
            FROM project_assignments pa
            JOIN users u ON pa.user_id = u.id
            WHERE pa.project_id IN ({placeholders})
            ORDER BY pa.created_at ASC
        """

        results = self.db_manager.execute_query(query, tuple(project_ids), readonly=True)

        members_by_project = defaultdict(list)
        for row in results or []:
            members_by_project[row['project_id']].append(dict(row))

        return dict(members_by_project)

    def load_job_stats_for_projects(self, project_ids: List[int]) -> Dict[int, Dict]:
        """
        Load job statistics for multiple projects in one query

        Args:
            project_ids: List of project IDs

        Returns:
            Dict mapping project_id to job stats
        """
        if not project_ids:
            return {}

        placeholders = ','.join(['%s'] * len(project_ids))
        query = f"""
            SELECT
                project_id,
                COUNT(*) as total_jobs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed_jobs,
                COUNT(*) FILTER (WHERE status = 'failed') as failed_jobs,
                COUNT(*) FILTER (WHERE status IN ('submitted', 'queued', 'processing', 'running')) as active_jobs,
                COALESCE(SUM(credits_used), 0) as total_credits_used
            FROM jobs
            WHERE project_id IN ({placeholders})
            GROUP BY project_id
        """

        results = self.db_manager.execute_query(query, tuple(project_ids), readonly=True)

        return {row['project_id']: dict(row) for row in (results or [])}

    def load_file_stats_for_projects(self, project_ids: List[int]) -> Dict[int, Dict]:
        """
        Load file statistics for multiple projects in one query

        Args:
            project_ids: List of project IDs

        Returns:
            Dict mapping project_id to file stats
        """
        if not project_ids:
            return {}

        placeholders = ','.join(['%s'] * len(project_ids))
        query = f"""
            SELECT
                project_id,
                COUNT(*) as file_count,
                COALESCE(SUM(size_bytes), 0) as total_storage_bytes
            FROM files
            WHERE project_id IN ({placeholders})
            GROUP BY project_id
        """

        results = self.db_manager.execute_query(query, tuple(project_ids), readonly=True)

        return {row['project_id']: dict(row) for row in (results or [])}


class OptimizedQueries:
    """
    Collection of optimized queries to replace problematic patterns
    """

    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.batch_loader = BatchLoader(db_manager)

    def get_messages_with_details(
        self,
        project_id: int,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict]:
        """
        Optimized message loading with attachments and reactions
        Replaces N+1 query pattern in messages.py

        Args:
            project_id: Project ID
            limit: Number of messages to fetch
            offset: Pagination offset

        Returns:
            List of messages with attachments and reactions embedded
        """
        # Fetch messages
        messages_query = """
            SELECT
                pm.*,
                u.username as author_username,
                u.profile_picture_url as author_picture
            FROM project_messages pm
            JOIN users u ON pm.user_id = u.id
            WHERE pm.project_id = %s
            ORDER BY pm.created_at DESC
            LIMIT %s OFFSET %s
        """

        messages = self.db_manager.execute_query(
            messages_query,
            (project_id, limit, offset),
            readonly=True
        )

        if not messages:
            return []

        # Extract message IDs
        message_ids = [msg['id'] for msg in messages]

        # Batch load attachments and reactions
        attachments_map = self.batch_loader.load_attachments_for_messages(message_ids)
        reactions_map = self.batch_loader.load_reactions_for_messages(message_ids)

        # Combine data
        for message in messages:
            msg_id = message['id']
            message['attachments'] = attachments_map.get(msg_id, [])
            message['reactions'] = reactions_map.get(msg_id, [])

        return messages

    def get_projects_with_stats(
        self,
        user_id: int,
        include_archived: bool = False
    ) -> List[Dict]:
        """
        Optimized project loading with all stats in minimal queries
        Replaces multiple subqueries in project routes

        Args:
            user_id: User ID
            include_archived: Include archived projects

        Returns:
            List of projects with stats
        """
        # Fetch projects
        archived_filter = "" if include_archived else "AND p.is_archived = 0"
        projects_query = f"""
            SELECT
                p.*,
                pa.role as user_role
            FROM projects p
            JOIN project_assignments pa ON p.id = pa.project_id
            WHERE pa.user_id = %s {archived_filter}
            ORDER BY p.created_at DESC
        """

        projects = self.db_manager.execute_query(
            projects_query,
            (user_id,),
            readonly=True
        )

        if not projects:
            return []

        # Extract project IDs
        project_ids = [proj['id'] for proj in projects]

        # Batch load all stats
        members_map = self.batch_loader.load_members_for_projects(project_ids)
        job_stats_map = self.batch_loader.load_job_stats_for_projects(project_ids)
        file_stats_map = self.batch_loader.load_file_stats_for_projects(project_ids)

        # Combine data
        for project in projects:
            proj_id = project['id']
            project['members'] = members_map.get(proj_id, [])
            project['member_count'] = len(project['members'])

            job_stats = job_stats_map.get(proj_id, {})
            project['total_jobs'] = job_stats.get('total_jobs', 0)
            project['completed_jobs'] = job_stats.get('completed_jobs', 0)
            project['failed_jobs'] = job_stats.get('failed_jobs', 0)
            project['active_jobs'] = job_stats.get('active_jobs', 0)
            project['total_credits_used'] = job_stats.get('total_credits_used', 0)

            file_stats = file_stats_map.get(proj_id, {})
            project['file_count'] = file_stats.get('file_count', 0)
            project['total_storage_bytes'] = file_stats.get('total_storage_bytes', 0)

        return projects

    def get_usage_summary_optimized(
        self,
        user_id: int,
        start_date: str,
        end_date: str
    ) -> Dict[str, Any]:
        """
        Optimized analytics query with single query instead of multiple

        Args:
            user_id: User ID
            start_date: Start date (ISO format)
            end_date: End date (ISO format)

        Returns:
            Complete usage summary
        """
        query = """
            WITH usage_by_type AS (
                SELECT
                    event_type,
                    COUNT(*) as count,
                    SUM(credits_used) as credits,
                    SUM(compute_time) as compute_time
                FROM usage_analytics
                WHERE user_id = %s
                  AND created_at BETWEEN %s AND %s
                GROUP BY event_type
            ),
            usage_by_model AS (
                SELECT
                    metadata::jsonb->>'model' as model,
                    COUNT(*) as count,
                    SUM(credits_used) as credits,
                    SUM(compute_time) as compute_time
                FROM usage_analytics
                WHERE user_id = %s
                  AND created_at BETWEEN %s AND %s
                  AND event_type = 'job_completed'
                GROUP BY metadata::jsonb->>'model'
            ),
            daily_usage AS (
                SELECT
                    DATE(created_at) as date,
                    COUNT(*) as events,
                    SUM(credits_used) as credits,
                    SUM(compute_time) as compute_time
                FROM usage_analytics
                WHERE user_id = %s
                  AND created_at BETWEEN %s AND %s
                GROUP BY DATE(created_at)
                ORDER BY date ASC
            )
            SELECT
                (SELECT json_agg(row_to_json(usage_by_type.*)) FROM usage_by_type) as by_event_type,
                (SELECT json_agg(row_to_json(usage_by_model.*)) FROM usage_by_model) as by_model,
                (SELECT json_agg(row_to_json(daily_usage.*)) FROM daily_usage) as daily
        """

        result = self.db_manager.execute_query(
            query,
            (user_id, start_date, end_date, user_id, start_date, end_date, user_id, start_date, end_date),
            readonly=True,
            fetch='one'
        )

        return result or {}

    def get_notifications_efficient(
        self,
        user_id: int,
        limit: int = 50,
        unread_only: bool = False
    ) -> List[Dict]:
        """
        Efficient notification loading with read status filtering

        Args:
            user_id: User ID
            limit: Number of notifications
            unread_only: Only fetch unread notifications

        Returns:
            List of notifications
        """
        read_filter = "AND is_read = 0" if unread_only else ""

        query = f"""
            SELECT *
            FROM notifications
            WHERE user_id = %s {read_filter}
            ORDER BY created_at DESC
            LIMIT %s
        """

        return self.db_manager.execute_query(
            query,
            (user_id, limit),
            readonly=True
        ) or []


class DatabaseMetrics:
    """
    Database health and performance metrics
    """

    def __init__(self, db_manager):
        self.db_manager = db_manager

    def get_connection_stats(self) -> Dict[str, Any]:
        """Get connection pool statistics"""
        query = """
            SELECT
                sum(numbackends) as active_connections,
                max(datconnlimit) as connection_limit
            FROM pg_stat_database
            WHERE datname = current_database()
        """

        return self.db_manager.execute_query(query, readonly=True, fetch='one') or {}

    def get_slow_queries(self, min_duration_ms: int = 1000) -> List[Dict]:
        """Get slow queries from pg_stat_statements (if enabled)"""
        query = """
            SELECT
                query,
                calls,
                total_time,
                mean_time,
                max_time
            FROM pg_stat_statements
            WHERE mean_time > %s
            ORDER BY mean_time DESC
            LIMIT 20
        """

        try:
            return self.db_manager.execute_query(
                query,
                (min_duration_ms,),
                readonly=True
            ) or []
        except Exception:
            logger.warning("pg_stat_statements not available")
            return []

    def get_table_sizes(self) -> List[Dict]:
        """Get size of all tables"""
        query = """
            SELECT
                schemaname,
                tablename,
                pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size,
                pg_total_relation_size(schemaname||'.'||tablename) as size_bytes
            FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
        """

        return self.db_manager.execute_query(query, readonly=True) or []

    def get_index_usage(self) -> List[Dict]:
        """Get index usage statistics"""
        query = """
            SELECT
                schemaname,
                tablename,
                indexname,
                idx_scan,
                idx_tup_read,
                idx_tup_fetch
            FROM pg_stat_user_indexes
            WHERE schemaname = 'public'
            ORDER BY idx_scan ASC
            LIMIT 50
        """

        return self.db_manager.execute_query(query, readonly=True) or []
