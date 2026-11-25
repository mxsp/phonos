/**
 * PHONOS Theme Manager — FORCED DARK MODE ONLY
 * All theme switching removed. Always forces dark mode.
 */

const ThemeManager = {
    currentTheme: 'dark',
    actualTheme: 'dark',

    init() {
        this._forceDark();
    },

    _forceDark() {
        this.currentTheme = 'dark';
        this.actualTheme = 'dark';
        localStorage.setItem('phonos-theme', 'dark');
        localStorage.setItem('theme', 'dark');
        document.documentElement.setAttribute('data-theme', 'dark');
        document.documentElement.classList.add('dark');
    },

    loadTheme() {
        this._forceDark();
    },

    applyTheme() {
        this._forceDark();
    },

    setTheme() {
        this._forceDark();
    },

    toggle() {
        this._forceDark();
    },

    addThemeToggleToUI() {},

    createSettingsUI() {
        return document.createElement('div');
    }
};

// Initialize immediately
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { ThemeManager.init(); });
} else {
    ThemeManager.init();
}

if (typeof window !== 'undefined') {
    window.ThemeManager = ThemeManager;
}
