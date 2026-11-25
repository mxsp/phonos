/**
 * BureauProjects — Project management panel for the Bureau workspace.
 *
 * Bureau and Phonos are two products over one backend: the same PostgreSQL
 * database, the same project storage, the same files. This module brings the
 * project-management surface that used to live only in the Phonos dashboard
 * into the Bureau sidebar, so a Bureau user never has to cross over into the
 * Phonos SPA to create a project, see who is on it, or invite a collaborator.
 *
 * It talks to the shared project API directly (/api/projects...) rather than
 * duplicating any state — whatever changes here shows up in Phonos, and vice
 * versa, because there is only one source of truth.
 *
 * Security: no innerHTML with interpolated values anywhere. Every node is
 * built with createElement + textContent.
 */
(function (global) {
    'use strict';

    var ROOT_ID = 'bureau-projects-list';

    function t(key, fallback) {
        return (global.i18n && global.i18n.t) ? global.i18n.t('bureau.' + key) : fallback;
    }

    function csrfToken() {
        var token = (global.PHONOS_CONFIG && global.PHONOS_CONFIG.csrfToken) || '';
        if (!token) {
            var meta = document.querySelector('meta[name="csrf-token"]');
            if (meta) token = meta.getAttribute('content') || '';
        }
        return token;
    }

    function jsonRequest(url, method, body) {
        return fetch(url, {
            method: method || 'GET',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken()
            },
            body: body === undefined ? undefined : JSON.stringify(body)
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (data) {
                if (!res.ok) {
                    throw new Error(data.message || data.error || ('HTTP ' + res.status));
                }
                return data;
            });
        });
    }

    function mk(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                if (key === 'class') node.className = attrs[key];
                else if (key === 'text') node.textContent = attrs[key];
                else if (key === 'style') node.style.cssText = attrs[key];
                else if (key.indexOf('data-') === 0) node.setAttribute(key, attrs[key]);
                else node[key] = attrs[key];
            });
        }
        (Array.isArray(children) ? children : (children ? [children] : [])).forEach(function (child) {
            if (child) node.appendChild(child);
        });
        return node;
    }

    function toast(message, duration) {
        if (global.NotebookModule && typeof NotebookModule.showToast === 'function') {
            NotebookModule.showToast(message, duration || 2500);
        }
    }

    /* Relative timestamps keep the cards readable at sidebar width. */
    function relativeTime(iso) {
        if (!iso) return '';
        var then = new Date(iso).getTime();
        if (isNaN(then)) return '';
        var secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
        if (secs < 60) return t('justNow', 'just now');
        var mins = Math.floor(secs / 60);
        if (mins < 60) return mins + t('minutesShort', 'm ago');
        var hours = Math.floor(mins / 60);
        if (hours < 24) return hours + t('hoursShort', 'h ago');
        var days = Math.floor(hours / 24);
        if (days < 30) return days + t('daysShort', 'd ago');
        return new Date(iso).toLocaleDateString();
    }

    function initials(name) {
        var parts = String(name || '?').trim().split(/[\s._-]+/).filter(Boolean);
        if (!parts.length) return '?';
        if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
        return (parts[0][0] + parts[1][0]).toUpperCase();
    }

    var BureauProjects = {
        projects: [],
        expandedId: null,
        rosters: {},
        filter: '',
        _loading: false,
        _bound: false,

        init: function () {
            if (this._bound) return;
            this._bound = true;

            var search = document.getElementById('bureau-project-search');
            if (search) {
                var self = this;
                search.addEventListener('input', function () {
                    self.filter = search.value.trim().toLowerCase();
                    self.render();
                });
            }
            var newBtn = document.getElementById('bureau-new-project-btn');
            if (newBtn) newBtn.addEventListener('click', this.createProject.bind(this));
            var refreshBtn = document.getElementById('bureau-refresh-projects-btn');
            if (refreshBtn) refreshBtn.addEventListener('click', this.refresh.bind(this, true));
        },

        /* Load the project list. Reuses NotebookModule's already-fetched list on
           the first paint so opening the panel is instant, then refreshes. */
        refresh: function (force) {
            var self = this;
            this.init();
            if (!force && global.NotebookModule && Array.isArray(NotebookModule.projects) && NotebookModule.projects.length) {
                this.projects = NotebookModule.projects;
                this.render();
            }
            if (this._loading) return Promise.resolve();
            this._loading = true;
            return fetch('/api/projects', { credentials: 'same-origin' })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    self.projects = Array.isArray(data) ? data : (data.projects || []);
                    // Keep the shared list in step so the project dropdown and the
                    // file/notebook panels never disagree with this panel.
                    if (global.NotebookModule) NotebookModule.projects = self.projects;
                    self.render();
                })
                .catch(function (e) {
                    console.error('[Bureau] Failed to load projects', e);
                    self.renderError(e.message);
                })
                .then(function () { self._loading = false; });
        },

        getRoot: function () {
            return document.getElementById(ROOT_ID);
        },

        renderError: function (message) {
            var root = this.getRoot();
            if (!root) return;
            root.replaceChildren(mk('div', {
                'class': 'nb-empty-state',
                text: t('loadFailed', 'Could not load projects: ') + message
            }));
        },

        render: function () {
            var root = this.getRoot();
            if (!root) return;

            var self = this;
            var term = this.filter;
            var visible = this.projects.filter(function (p) {
                if (!term) return true;
                return String(p.name || '').toLowerCase().indexOf(term) !== -1 ||
                       String(p.description || '').toLowerCase().indexOf(term) !== -1;
            });

            if (!visible.length) {
                root.replaceChildren(mk('div', {
                    'class': 'nb-empty-state',
                    text: this.projects.length
                        ? t('noMatches', 'No projects match your search')
                        : t('noProjects', 'No projects yet — create one to get started')
                }));
                return;
            }

            var activeId = global.NotebookModule ? String(NotebookModule.projectId || '') : '';
            root.replaceChildren.apply(root, visible.map(function (project) {
                return self.renderCard(project, activeId);
            }));
        },

        renderCard: function (project, activeId) {
            var self = this;
            var isActive = String(project.id) === activeId;
            var isExpanded = String(project.id) === String(this.expandedId);

            var card = mk('div', {
                'class': 'bureau-project' + (isActive ? ' active' : '') + (isExpanded ? ' expanded' : '')
            });

            // --- Header row: name, role, member count -----------------------
            var head = mk('button', { 'class': 'bureau-project-head', type: 'button' });
            var titleWrap = mk('div', { 'class': 'bureau-project-titles' });
            titleWrap.appendChild(mk('span', { 'class': 'bureau-project-name', text: project.name || t('untitled', 'Untitled project') }));

            var meta = mk('div', { 'class': 'bureau-project-meta' });
            if (project.role) meta.appendChild(mk('span', { 'class': 'bureau-role', text: project.role }));
            var memberCount = (project.members && project.members.length) || 1;
            meta.appendChild(mk('span', {
                'class': 'bureau-project-metaitem',
                text: memberCount + ' ' + (memberCount === 1 ? t('member', 'member') : t('members', 'members'))
            }));
            var updated = relativeTime(project.updated_at || project.created_at);
            if (updated) meta.appendChild(mk('span', { 'class': 'bureau-project-metaitem', text: updated }));
            titleWrap.appendChild(meta);
            head.appendChild(titleWrap);

            var chevron = mk('span', { 'class': 'bureau-chevron', text: isExpanded ? '–' : '+' });
            head.appendChild(chevron);

            head.addEventListener('click', function () {
                // Selecting a project is the primary action; expanding is the
                // secondary one, and both happen on the same click so a single
                // tap both switches context and reveals the detail.
                self.selectProject(project.id);
                self.expandedId = isExpanded ? null : project.id;
                self.render();
                if (!isExpanded) self.loadRoster(project.id);
            });
            card.appendChild(head);

            if (project.description) {
                card.appendChild(mk('div', { 'class': 'bureau-project-desc', text: project.description }));
            }

            if (isExpanded) card.appendChild(this.renderDetail(project));
            return card;
        },

        renderDetail: function (project) {
            var self = this;
            var detail = mk('div', { 'class': 'bureau-project-detail' });

            // --- Quick actions ----------------------------------------------
            var actions = mk('div', { 'class': 'bureau-action-row' });
            actions.appendChild(this.actionButton(t('openFiles', 'Files'), function () {
                self.selectProject(project.id);
                if (global.NotebookModule) NotebookModule._switchPanel('files');
            }));
            actions.appendChild(this.actionButton(t('openNotebooks', 'Notebooks'), function () {
                self.selectProject(project.id);
                if (global.NotebookModule) NotebookModule._switchPanel('notebooks');
            }));
            actions.appendChild(this.actionButton(t('download', 'Archive'), function () {
                global.location.href = '/api/projects/' + project.id + '/download';
            }));
            detail.appendChild(actions);

            // --- Members ------------------------------------------------------
            var roster = this.rosters[project.id];
            detail.appendChild(mk('div', { 'class': 'bureau-detail-title', text: t('team', 'Team') }));

            if (!roster) {
                detail.appendChild(mk('div', { 'class': 'bureau-detail-hint', text: t('loadingTeam', 'Loading team…') }));
                return detail;
            }

            var list = mk('div', { 'class': 'bureau-member-list' });
            (roster.members || []).forEach(function (member) {
                var row = mk('div', { 'class': 'bureau-member' });
                row.appendChild(mk('span', { 'class': 'bureau-avatar', text: initials(member.display_name || member.username) }));
                var names = mk('div', { 'class': 'bureau-member-names' });
                names.appendChild(mk('span', { 'class': 'bureau-member-name', text: member.display_name || member.username }));
                names.appendChild(mk('span', { 'class': 'bureau-member-role', text: member.role || '' }));
                row.appendChild(names);
                list.appendChild(row);
            });
            (roster.pending_invitations || []).forEach(function (invite) {
                var row = mk('div', { 'class': 'bureau-member pending' });
                row.appendChild(mk('span', { 'class': 'bureau-avatar', text: '@' }));
                var names = mk('div', { 'class': 'bureau-member-names' });
                names.appendChild(mk('span', { 'class': 'bureau-member-name', text: invite.email }));
                names.appendChild(mk('span', { 'class': 'bureau-member-role', text: t('invitePending', 'invite pending') }));
                row.appendChild(names);
                list.appendChild(row);
            });
            if (!list.childNodes.length) {
                list.appendChild(mk('div', { 'class': 'bureau-detail-hint', text: t('noMembers', 'No members yet') }));
            }
            detail.appendChild(list);

            // --- Manager-only controls ---------------------------------------
            if (roster.is_owner) {
                var manage = mk('div', { 'class': 'bureau-action-row' });
                manage.appendChild(this.actionButton(t('invite', 'Invite'), function () {
                    self.invite(project);
                }));
                manage.appendChild(this.actionButton(t('rename', 'Rename'), function () {
                    self.renameProject(project);
                }));
                detail.appendChild(manage);
            }

            return detail;
        },

        actionButton: function (label, onClick) {
            var btn = mk('button', { 'class': 'bureau-action', type: 'button', text: label });
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                onClick();
            });
            return btn;
        },

        /* Switch the whole Bureau workspace to a project: the shared selector,
           the sidebar panels and the breadcrumb all follow from this one call,
           exactly as if the user had used the project dropdown. */
        selectProject: function (projectId) {
            if (!global.NotebookModule) return;
            var id = String(projectId);
            if (String(NotebookModule.projectId) === id) return;
            NotebookModule.projectId = id;
            var select = document.getElementById('project-selector');
            if (select) select.value = id;
            try { localStorage.setItem('phonos-notebook-project', id); } catch (e) {}
            if (typeof NotebookModule.updateMetaProject === 'function') NotebookModule.updateMetaProject();
            if (typeof NotebookModule._invalidateSidebarPanelCache === 'function') {
                NotebookModule._invalidateSidebarPanelCache();
            }
            if (typeof NotebookModule.refreshFileExplorer === 'function') NotebookModule.refreshFileExplorer(true);
            if (typeof NotebookModule.refreshNotebooksList === 'function') NotebookModule.refreshNotebooksList();
        },

        loadRoster: function (projectId) {
            var self = this;
            return jsonRequest('/api/projects/' + projectId + '/assignments')
                .then(function (data) {
                    self.rosters[projectId] = data;
                    if (String(self.expandedId) === String(projectId)) self.render();
                })
                .catch(function (e) {
                    self.rosters[projectId] = { members: [], pending_invitations: [], is_owner: false };
                    console.error('[Bureau] Failed to load roster', e);
                    if (String(self.expandedId) === String(projectId)) self.render();
                });
        },

        createProject: function () {
            var self = this;
            var name = global.prompt(t('newProjectPrompt', 'Project name'));
            if (!name || !name.trim()) return;
            jsonRequest('/api/projects', 'POST', { name: name.trim(), project_type: 'private' })
                .then(function (project) {
                    toast(t('projectCreated', 'Project created'));
                    return self.refresh(true).then(function () {
                        if (project && project.id) self.selectProject(project.id);
                    });
                })
                .catch(function (e) { toast(t('createFailed', 'Could not create project: ') + e.message, 4000); });
        },

        renameProject: function (project) {
            var self = this;
            var name = global.prompt(t('renamePrompt', 'New project name'), project.name || '');
            if (!name || !name.trim() || name.trim() === project.name) return;
            jsonRequest('/api/projects/' + project.id, 'PATCH', { name: name.trim() })
                .then(function () {
                    toast(t('projectRenamed', 'Project renamed'));
                    return self.refresh(true);
                })
                .catch(function (e) { toast(t('renameFailed', 'Could not rename: ') + e.message, 4000); });
        },

        invite: function (project) {
            var self = this;
            var email = global.prompt(t('invitePrompt', 'Email address to invite'));
            if (!email || !email.trim()) return;
            jsonRequest('/api/projects/' + project.id + '/invitations', 'POST', {
                email: email.trim(),
                role: 'collaborator'
            })
                .then(function (data) {
                    toast(data.message || t('inviteSent', 'Invitation sent'));
                    return self.loadRoster(project.id);
                })
                .catch(function (e) { toast(t('inviteFailed', 'Could not invite: ') + e.message, 4000); });
        }
    };

    global.BureauProjects = BureauProjects;
})(window);
