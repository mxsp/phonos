
/**
 * System Warning Manager
 * Fetches and displays system-wide warnings and announcements
 */

// Security: HTML escape function to prevent XSS
function escapeHtml(text) {
    if (typeof text !== 'string') return text;
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

const SystemWarningManager = {
    init() {
        this.fetchWarnings();
        // Poll every 5 minutes
        setInterval(() => this.fetchWarnings(), 5 * 60 * 1000);
    },

    async fetchWarnings() {
        try {
            const response = await fetch('/api/system/warnings', {
                method: 'GET',
                credentials: 'same-origin',
                cache: 'no-store'
            });
            if (!response.ok) {
                if (response.status === 401 || response.status === 403) {
                    this.renderWarnings([]);
                }
                return;
            }
            const warnings = await response.json();
            this.renderWarnings(warnings);
        } catch (error) {
            console.error('Failed to fetch system warnings:', error);
        }
    },

    renderWarnings(warnings) {
        const container = document.getElementById('system-warnings-container');
        if (!container) return;

        container.innerHTML = '';
        
        if (warnings.length === 0) {
            container.style.display = 'none';
            return;
        }

        container.style.display = 'block';

        warnings.forEach(warning => {
            const alertDiv = document.createElement('div');
            // Map level to bootstrap class (warning -> warning, error -> danger, info -> info)
            const alertClass = warning.level === 'error' ? 'danger' : (warning.level || 'warning');
            
            alertDiv.className = `alert alert-${alertClass}`;
            alertDiv.style.marginBottom = 'var(--space-2)';
            alertDiv.style.textAlign = 'center';
            
            // Add icon based on level
            let iconSvg = '';
            if (alertClass === 'danger' || alertClass === 'warning') {
                iconSvg = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align: middle; margin-right: 8px;">
                    <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                    <line x1="12" y1="9" x2="12" y2="13"/>
                    <line x1="12" y1="17" x2="12.01" y2="17"/>
                </svg>`;
            } else {
                iconSvg = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align: middle; margin-right: 8px;">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="16" x2="12" y2="12"></line>
                    <line x1="12" y1="8" x2="12.01" y2="8"></line>
                </svg>`;
            }

            alertDiv.innerHTML = `${iconSvg}<span>${escapeHtml(warning.message)}</span>`;
            container.appendChild(alertDiv);
        });
    }
};

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    SystemWarningManager.init();
});
