/**
 * PHONOS Desktop Notifications System
 * Handles browser push notifications for job completions, mentions, and updates.
 */

const DesktopNotifications = {
    enabled: false,
    permission: 'default',
    soundEnabled: false,
    notificationSound: null,
    _audioUnlocked: false,

    init() {
        // Guard each step independently: a single failing step (e.g. a thrown
        // error in loadPreferences or setupSocketListeners) must NOT abort the
        // remaining steps and leave the module half-wired — that previously
        // could skip preloadSound()/setupAudioUnlock() and kill all sound.
        const step = (label, fn) => {
            try { fn(); } catch (e) {
                console.error('[Notifications] init step failed: ' + label, e);
            }
        };
        step('checkSupport', () => this.checkSupport());
        step('loadPreferences', () => this.loadPreferences());
        step('setupSocketListeners', () => this.setupSocketListeners());
        // The app's socket may be created after this module initializes; bind
        // again once it signals readiness (idempotent guard below).
        step('socketReadyListener', () => {
            window.addEventListener('phonos:socket-ready', () => this.setupSocketListeners());
        });
        step('preloadSound', () => this.preloadSound());
        step('setupAudioUnlock', () => this.setupAudioUnlock());
    },

    checkSupport() {
        if (!('Notification' in window)) {
            console.log('This browser does not support desktop notifications');
            return false;
        }
        this.permission = Notification.permission;
        return true;
    },

    loadPreferences() {
        if (typeof API !== 'undefined' && API.get) {
            API.get('/api/user/preferences').then(prefs => {
                if (prefs) {
                    this.enabled = prefs.desktop_notifications_enabled;
                    this.soundEnabled = prefs.sound_notifications_enabled;

                    // Sync DOM checkboxes if they exist
                    const dnCheckbox = document.getElementById('desktop-notifications');
                    if (dnCheckbox) dnCheckbox.checked = !!this.enabled;
                    const snCheckbox = document.getElementById('sound-notifications');
                    if (snCheckbox) snCheckbox.checked = !!this.soundEnabled;

                    // Auto-request permission if enabled but not granted
                    if (this.enabled && this.permission === 'default') {
                        this.requestPermission();
                    }
                }
            }).catch(err => {
                console.warn('[Notifications] Could not load preferences:', err);
            });
        } else {
            // Retry later if API isn't ready
            setTimeout(() => this.loadPreferences(), 500);
        }
    },

    preloadSound() {
        try {
            this.notificationSound = new Audio('/static/sounds/notification.wav');
            this.notificationSound.volume = 0.5;
        } catch (e) {
            // Sound not available
        }
    },

    // Browsers block programmatic Audio.play() until the page receives a user
    // gesture, so socket-triggered notification sounds would silently fail.
    // Prime the audio element on the first gesture so later plays succeed.
    setupAudioUnlock() {
        if (this._audioUnlocked) return;
        // touchstart is required to count as a user gesture on iOS/Safari, where
        // pointerdown may not unlock autoplay; keep click/keydown for desktop.
        const events = ['pointerdown', 'touchstart', 'keydown', 'click'];
        const prime = () => {
            events.forEach((evt) => document.removeEventListener(evt, prime, true));
            this.primeAudio();
        };
        events.forEach((evt) => document.addEventListener(evt, prime, { once: true, capture: true }));
    },

    primeAudio() {
        if (this._audioUnlocked) return;
        // Recover if preloadSound() failed or ran out of order, so the very
        // first gesture still unlocks a usable audio element.
        if (!this.notificationSound) this.preloadSound();
        if (!this.notificationSound) return;
        this._audioUnlocked = true;
        const sound = this.notificationSound;
        try {
            sound.volume = 0;
            const attempt = sound.play();
            if (attempt && typeof attempt.then === 'function') {
                attempt.then(() => {
                    sound.pause();
                    sound.currentTime = 0;
                    sound.volume = 0.5;
                }).catch(() => {
                    sound.volume = 0.5;
                });
            } else {
                sound.pause();
                sound.currentTime = 0;
                sound.volume = 0.5;
            }
        } catch (e) {
            try { sound.volume = 0.5; } catch (e2) { /* ignore */ }
        }
    },

    // Play the custom notification sound. Independent of desktop-notification
    // permission: the sound has its own toggle and should fire when a job
    // finishes even if the browser-notification permission was never granted.
    // Relies on primeAudio() having unlocked autoplay on an earlier gesture.
    playSound() {
        if (!this.soundEnabled) return;
        // Lazily (re)create the element if preloadSound() never ran or failed,
        // so an enabled sound toggle is never silently dead. Never gated on the
        // Notification API / permission — sound is fully independent of desktop
        // notifications and must work over plain HTTP too.
        if (!this.notificationSound) {
            this.preloadSound();
            if (!this.notificationSound) return;
        }
        try {
            // Rewind so rapid back-to-back notifications each play from the start.
            this.notificationSound.currentTime = 0;
        } catch (e) { /* not yet seekable */ }
        try {
            const attempt = this.notificationSound.play();
            if (attempt && typeof attempt.catch === 'function') attempt.catch(() => {});
        } catch (e) { /* autoplay blocked or element busy */ }
    },

    async requestPermission() {
        if (!('Notification' in window)) return false;

        try {
            const permission = await Notification.requestPermission();
            this.permission = permission;

            if (permission === 'granted') {
                this.enabled = true;
                this.savePreference(true);
                this.show('Notifications Enabled', {
                    body: 'You will now receive notifications for job completions and updates.',
                    icon: '/static/favicon.ico'
                });
                return true;
            }
            return false;
        } catch (error) {
            console.error('Error requesting notification permission:', error);
            return false;
        }
    },

    savePreference(enabled) {
        if (typeof API !== 'undefined') {
            API.post('/api/user/preferences', {
                desktop_notifications_enabled: enabled
            }).catch(() => {});
        }
    },

    show(title, options = {}) {
        // Play our own sound first, before the permission gate, so the audible
        // cue still fires when desktop notifications are disabled or were never
        // granted (the sound toggle is independent of the desktop toggle).
        this.playSound();

        if (!this.enabled || this.permission !== 'granted') return null;

        const defaultOptions = {
            icon: '/static/favicon.ico',
            badge: '/static/favicon.ico',
            tag: options.tag || 'phonos-notification',
            renotify: true,
            requireInteraction: false,
            // We always play our own notification.wav (see playSound above), so
            // keep the OS notification silent to avoid a double beep.
            silent: true
        };

        const mergedOptions = { ...defaultOptions, ...options };

        try {
            const notification = new Notification(title, mergedOptions);

            // Handle click
            notification.onclick = () => {
                window.focus();
                if (options.onClick) options.onClick();
                if (options.url) window.location.href = options.url;
                notification.close();
            };

            // Auto-close after duration
            if (options.duration) {
                setTimeout(() => notification.close(), options.duration);
            }

            return notification;
        } catch (error) {
            console.error('Error showing notification:', error);
            return null;
        }
    },

    // Notification types
    jobCompleted(job) {
        this.show('Job Completed', {
            body: (job.name || 'Job ' + job.job_id) + ' has finished successfully.',
            tag: 'job-' + job.job_id,
            icon: '/static/favicon.ico',
            onClick: () => {
                if (typeof Views !== 'undefined') {
                    Views.load('jobs');
                }
            }
        });
    },

    jobFailed(job) {
        this.show('Job Failed', {
            body: (job.name || 'Job ' + job.job_id) + ' has failed. Click to view details.',
            tag: 'job-' + job.job_id,
            icon: '/static/favicon.ico',
            requireInteraction: true,
            onClick: () => {
                if (typeof Views !== 'undefined') {
                    Views.load('jobs');
                }
            }
        });
    },

    mention(data) {
        this.show('You were mentioned', {
            body: data.mentioner + ' mentioned you in ' + (data.project_name || 'a project'),
            tag: 'mention-' + data.id,
            icon: '/static/favicon.ico',
            onClick: () => {
                if (data.project_id && typeof Views !== 'undefined') {
                    Views.load('project-chat', { projectId: data.project_id });
                }
            }
        });
    },

    projectUpdate(data) {
        this.show('Project Updated', {
            body: data.message || 'A project you follow has been updated.',
            tag: 'project-' + data.project_id,
            icon: '/static/favicon.ico'
        });
    },

    teamInvitation(data) {
        this.show('Team Invitation', {
            body: 'You have been invited to join ' + (data.team_name || 'a team'),
            tag: 'invite-' + data.id,
            icon: '/static/favicon.ico',
            requireInteraction: true,
            onClick: () => {
                if (typeof Views !== 'undefined') {
                    Views.load('dashboard');
                }
            }
        });
    },

    storageWarning(percent) {
        if (percent >= 90) {
            this.show('Storage Almost Full', {
                body: 'You have used ' + percent + '% of your storage. Consider cleaning up old files.',
                tag: 'storage-warning',
                icon: '/static/favicon.ico',
                requireInteraction: true
            });
        }
    },

    creditsLow(credits) {
        if (credits <= 10) {
            this.show('Credits Running Low', {
                body: 'You have ' + credits + ' credits remaining. Credits reset monthly.',
                tag: 'credits-warning',
                icon: '/static/favicon.ico'
            });
        }
    },

    // Socket.io integration
    setupSocketListeners() {
        const socket = window.socket;
        // Bind only once, and only when the socket actually exists.
        if (socket && !this._socketBound) {
            this._socketBound = true;
            // NOTE: job_update (completed/failed → desktop + sound) is driven
            // from app.js's own job_update handler instead of here. That handler
            // is guaranteed to be bound to the live socket, whereas this module's
            // binding races with Socket.init() and could silently stay attached
            // to no socket / a stale one — which left job-finish notifications
            // and sound dead even though the toggle previews worked.
            socket.on('mention', (data) => {
                if (data) {
                    this.mention(data);
                }
            });

            socket.on('team_invitation', (data) => {
                if (data) {
                    this.teamInvitation(data);
                }
            });

            socket.on('storage_warning', (data) => {
                if (data && data.percent) {
                    this.storageWarning(data.percent);
                }
            });
        }
    },

    // Settings UI helper
    createSettingsUI() {
        const container = document.createElement('div');
        container.className = 'notification-settings';

        // Enable/disable toggle
        const enableRow = document.createElement('div');
        enableRow.className = 'setting-row';

        const enableLabel = document.createElement('label');
        enableLabel.className = 'setting-label';
        enableLabel.textContent = 'Desktop Notifications';

        const enableToggle = document.createElement('button');
        enableToggle.className = 'toggle-btn' + (this.enabled ? ' active' : '');
        enableToggle.textContent = this.enabled ? 'Enabled' : 'Disabled';
        enableToggle.onclick = async () => {
            if (this.permission !== 'granted') {
                const granted = await this.requestPermission();
                if (granted) {
                    enableToggle.classList.add('active');
                    enableToggle.textContent = 'Enabled';
                }
            } else {
                this.enabled = !this.enabled;
                this.savePreference(this.enabled);
                enableToggle.classList.toggle('active', this.enabled);
                enableToggle.textContent = this.enabled ? 'Enabled' : 'Disabled';
            }
        };

        enableRow.appendChild(enableLabel);
        enableRow.appendChild(enableToggle);
        container.appendChild(enableRow);

        // Sound toggle
        const soundRow = document.createElement('div');
        soundRow.className = 'setting-row';

        const soundLabel = document.createElement('label');
        soundLabel.className = 'setting-label';
        soundLabel.textContent = 'Notification Sound';

        const soundToggle = document.createElement('button');
        soundToggle.className = 'toggle-btn' + (this.soundEnabled ? ' active' : '');
        soundToggle.textContent = this.soundEnabled ? 'On' : 'Off';
        soundToggle.onclick = () => {
            this.soundEnabled = !this.soundEnabled;
            if (typeof API !== 'undefined') {
                API.post('/api/user/preferences', {
                    sound_notifications_enabled: this.soundEnabled
                }).catch(() => {});
            }
            soundToggle.classList.toggle('active', this.soundEnabled);
            soundToggle.textContent = this.soundEnabled ? 'On' : 'Off';
        };

        soundRow.appendChild(soundLabel);
        soundRow.appendChild(soundToggle);
        container.appendChild(soundRow);

        // Test notification button
        const testRow = document.createElement('div');
        testRow.className = 'setting-row';

        const testBtn = document.createElement('button');
        testBtn.className = 'btn btn-secondary';
        testBtn.textContent = 'Test Notification';
        testBtn.onclick = () => {
            // This click is a user gesture, so unlock autoplay and force an
            // audible preview even if the sound toggle is currently off — a
            // "Test" should always be verifiable. show() also plays the sound
            // when enabled, but we play here directly so it is never silent.
            this._audioUnlocked = true;
            if (!this.notificationSound) this.preloadSound();
            if (this.notificationSound) {
                try { this.notificationSound.currentTime = 0; } catch (e) { /* not seekable */ }
                this.notificationSound.volume = 0.5;
                const attempt = this.notificationSound.play();
                if (attempt && typeof attempt.catch === 'function') attempt.catch(() => {});
            }
            this.show('Test Notification', {
                body: 'This is a test notification from Phonos.',
                duration: 5000
            });
        };

        testRow.appendChild(testBtn);
        container.appendChild(testRow);

        return container;
    }
};

// Export for use in other modules FIRST — must happen before init() so the
// global is always defined even if init() throws. Otherwise App.toggle* falls
// into the `!DN` branch and wrongly reports "browser does not support".
if (typeof window !== 'undefined') {
    window.DesktopNotifications = DesktopNotifications;
}

// Initialize when DOM is ready. Contain any init error so a single failing
// step can't leave the module half-wired.
function _initDesktopNotifications() {
    try {
        DesktopNotifications.init();
    } catch (e) {
        console.error('[Notifications] init failed', e);
    }
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initDesktopNotifications);
} else {
    _initDesktopNotifications();
}
