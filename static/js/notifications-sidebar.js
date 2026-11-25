/**
 * Notifications Sidebar (Static version for legacy dashboard)
 */

(function() {
    const NotificationsSidebar = {
        isOpen: false,
        sidebar: null,
        toggleBtn: null,
        badge: null,
        list: null,
        pollTimer: null,

        init() {
            this.sidebar = document.getElementById('notifications-sidebar');
            this.toggleBtn = document.getElementById('notifications-btn');
            this.badge = document.getElementById('notification-count');
            this.list = document.getElementById('notifications-list');

            if (!this.sidebar || !this.toggleBtn) return;

            this.attachEvents();
            this.updateBadge();
            this.startPolling();
        },

        // Fallback when the socket is down: refresh the unread badge every 60s.
        // Skipped while the socket is connected so push updates aren't double-counted.
        startPolling() {
            if (this.pollTimer) return;
            this.pollTimer = setInterval(() => {
                if (window.socket && window.socket.connected) return;
                this.updateBadge();
            }, 60000);
            window.addEventListener('beforeunload', () => this.stopPolling());
        },

        stopPolling() {
            if (this.pollTimer) {
                clearInterval(this.pollTimer);
                this.pollTimer = null;
            }
        },

        attachEvents() {
            this.toggleBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.toggle();
            });

            const closeBtn = document.getElementById('close-notifications-btn');
            if (closeBtn) {
                closeBtn.addEventListener('click', () => this.close());
            }

            const markAllBtn = document.getElementById('mark-all-read-btn');
            if (markAllBtn) {
                markAllBtn.addEventListener('click', () => this.markAllRead());
            }

            // Bind socket events. The app's socket may not exist yet when this
            // runs, so bind now if available and again on the ready event.
            this.bindSocket();
            window.addEventListener('phonos:socket-ready', () => this.bindSocket());

            // Listen for custom event from app.js
            window.addEventListener('notification', (e) => {
                this.handleNewNotification(e.detail);
            });
            
            // Close on Escape
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && this.isOpen) this.close();
            });
        },

        // Attach real-time listeners once the socket exists (idempotent).
        bindSocket() {
            if (this._socketBound || !window.socket) return;
            this._socketBound = true;
            window.socket.on('notification', (data) => {
                this.handleNewNotification(data);
            });
            // Some backend versions use notification_update
            window.socket.on('notification_update', () => {
                this.updateBadge();
                if (this.isOpen) this.fetch();
            });
        },

        toggle() {
            this.isOpen ? this.close() : this.open();
        },

        open() {
            this.sidebar.classList.remove('hidden');
            setTimeout(() => {
                this.sidebar.classList.add('active');
                this.isOpen = true;
                this.fetch();
            }, 10);
        },

        close() {
            this.sidebar.classList.remove('active');
            this.isOpen = false;
            setTimeout(() => {
                if (!this.isOpen) this.sidebar.classList.add('hidden');
            }, 300);
        },

        async fetch() {
            if (!this.list) return;
            this.list.innerHTML = '<div class="notifications-loading">Loading notifications...</div>';

            try {
                const response = await fetch('/api/notifications');
                const data = await response.json();
                this.render(data.notifications || []);
                this.updateBadge(data.unread_count);
            } catch (e) {
                this.list.innerHTML = '<div class="notifications-error">Failed to load notifications</div>';
            }
        },

        // Escape server-provided strings before interpolating into innerHTML.
        escapeHtml(str) {
            if (str === null || str === undefined) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        },

        render(notifications) {
            if (notifications.length === 0) {
                this.list.innerHTML = '<div class="notifications-empty">No notifications yet</div>';
                return;
            }

            this.list.innerHTML = notifications.map(n => `
                <div class="notification-item ${n.is_read ? '' : 'unread'}" data-id="${this.escapeHtml(n.id)}"${n.related_job_id ? ` data-job-id="${this.escapeHtml(n.related_job_id)}"` : ''}>
                    <div class="notification-icon ${this.escapeHtml(n.category) || 'system'}">
                        ${this.getIcon(n.category)}
                    </div>
                    <div class="notification-content">
                        <div class="notification-title">${this.escapeHtml(n.title)}</div>
                        <div class="notification-message">${this.escapeHtml(n.message)}</div>
                        <div class="notification-time">${this.formatDate(n.created_at)}</div>
                    </div>
                    ${n.is_read ? '' : '<div class="unread-indicator"></div>'}
                </div>
            `).join('');

            this.list.querySelectorAll('.notification-item').forEach(item => {
                item.addEventListener('click', async () => {
                    await this.markRead(item.dataset.id);
                    // Deep-link job notifications to the job's output view
                    // (same URL the dashboard uses: /view_output/<job_id>).
                    const jobId = item.dataset.jobId;
                    if (jobId) {
                        window.location.href = `/view_output/${encodeURIComponent(jobId)}`;
                    }
                });
            });
        },

        async markRead(id) {
            const item = this.list.querySelector(`[data-id="${id}"]`);
            if (item && item.classList.contains('unread')) {
                try {
                    await fetch('/api/notifications/mark_read', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.PHONOS_CONFIG?.csrfToken },
                        body: JSON.stringify({ ids: [id] })
                    });
                    item.classList.remove('unread');
                    const indicator = item.querySelector('.unread-indicator');
                    if (indicator) indicator.remove();
                    this.updateBadge();
                } catch (e) {}
            }
        },

        async markAllRead() {
            try {
                await fetch('/api/notifications/mark_read', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.PHONOS_CONFIG?.csrfToken },
                    body: JSON.stringify({ all: true })
                });
                this.fetch();
                this.updateBadge(0);
            } catch (e) {}
        },

        handleNewNotification(data) {
            this.updateBadge();
            if (this.isOpen) {
                this.fetch();
            }
        },

        updateBadge(count) {
            if (!this.badge) return;
            
            if (count === undefined) {
                fetch('/api/notifications?limit=1')
                    .then(r => r.json())
                    .then(d => this.updateBadge(d.unread_count))
                    .catch(() => {});
                return;
            }

            if (count > 0) {
                this.badge.textContent = count > 99 ? '99+' : count;
                this.badge.style.display = 'flex';
            } else {
                this.badge.style.display = 'none';
            }
        },

        getIcon(cat) {
            if (cat === 'job') return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>';
            return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';
        },

        formatDate(dateStr) {
            const date = new Date(dateStr);
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ' ' + date.toLocaleDateString();
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => NotificationsSidebar.init());
    } else {
        NotificationsSidebar.init();
    }
})();
