/**
 * Phonos App Shell Enhancements
 * Handles Command Palette, Sidebar Contexts, and Unified Navigation
 */

class CommandPalette {
    constructor() {
        this.overlay = document.getElementById('command-palette-overlay');
        this.input = document.getElementById('command-palette-input');
        this.resultsContainer = document.getElementById('command-palette-results');
        this.isOpen = false;
        this.selectedIndex = 0;
        this.commands = this.getStaticCommands();
        this.filteredCommands = [];

        this.init();
    }

    init() {
        if (!this.overlay || !this.input) return;

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                this.toggle();
            }
            
            if (this.isOpen) {
                if (e.key === 'Escape') {
                    this.close();
                } else if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this.navigate(1);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this.navigate(-1);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    this.executeSelected();
                }
            }
        });

        // Click outside to close
        this.overlay.addEventListener('click', (e) => {
            if (e.target === this.overlay) {
                this.close();
            }
        });

        // Input handling
        this.input.addEventListener('input', () => {
            this.selectedIndex = 0;
            this.renderResults();
        });
    }

    getStaticCommands() {
        return [
            {
                id: 'new-job',
                title: 'New Job',
                subtitle: 'Submit a new protein folding job',
                icon: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
                action: () => App.switchView('submit-job')
            },
            {
                id: 'go-dashboard',
                title: 'Go to Dashboard',
                subtitle: 'View your lab overview',
                icon: '<rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/>',
                action: () => App.switchView('dashboard')
            },
            {
                id: 'go-projects',
                title: 'Go to Projects',
                subtitle: 'Browse your research projects',
                icon: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
                action: () => App.switchView('projects')
            },
            {
                id: 'go-visualizer',
                title: 'Open 3D Visualizer',
                subtitle: 'Interactive protein structure viewer',
                icon: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>',
                action: () => App.switchView('visualizer')
            },
            {
                id: 'toggle-theme',
                title: 'Toggle Theme',
                subtitle: 'Switch between dark and light mode',
                icon: '<circle cx="12" cy="12" r="5"/>',
                action: () => document.getElementById('theme-toggle-header').click()
            }
        ];
    }

    toggle() {
        if (this.isOpen) {
            this.close();
        } else {
            this.open();
        }
    }

    open() {
        this.isOpen = true;
        this.overlay.style.display = 'flex';
        this.input.value = '';
        this.input.focus();
        this.renderResults();
        
        // Add animation class
        requestAnimationFrame(() => {
            this.overlay.classList.add('active');
        });
    }

    close() {
        this.isOpen = false;
        this.overlay.classList.remove('active');
        setTimeout(() => {
            this.overlay.style.display = 'none';
        }, 200);
    }

    filterCommands() {
        const query = this.input.value.toLowerCase();
        if (!query) return this.commands;

        return this.commands.filter(cmd => 
            cmd.title.toLowerCase().includes(query) || 
            cmd.subtitle.toLowerCase().includes(query)
        );
    }

    navigate(direction) {
        const max = this.filteredCommands.length - 1;
        this.selectedIndex += direction;
        
        if (this.selectedIndex < 0) this.selectedIndex = max;
        if (this.selectedIndex > max) this.selectedIndex = 0;
        
        this.updateSelection();
    }

    updateSelection() {
        const items = this.resultsContainer.querySelectorAll('.command-palette-item');
        items.forEach((item, index) => {
            if (index === this.selectedIndex) {
                item.classList.add('selected');
                item.scrollIntoView({ block: 'nearest' });
            } else {
                item.classList.remove('selected');
            }
        });
    }

    executeSelected() {
        if (this.filteredCommands[this.selectedIndex]) {
            const cmd = this.filteredCommands[this.selectedIndex];
            cmd.action();
            this.close();
        }
    }

    renderResults() {
        this.filteredCommands = this.filterCommands();
        this.resultsContainer.innerHTML = '';

        if (this.filteredCommands.length === 0) {
            this.resultsContainer.innerHTML = `
                <div class="command-palette-empty">
                    No commands found matching "${this.input.value}"
                </div>
            `;
            return;
        }

        this.filteredCommands.forEach((cmd, index) => {
            const item = document.createElement('div');
            item.className = `command-palette-item ${index === this.selectedIndex ? 'selected' : ''}`;
            item.innerHTML = `
                <div class="command-palette-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        ${cmd.icon}
                    </svg>
                </div>
                <div class="command-palette-content">
                    <div class="command-palette-title">${cmd.title}</div>
                    <div class="command-palette-subtitle">${cmd.subtitle}</div>
                </div>
            `;
            
            item.addEventListener('mouseenter', () => {
                this.selectedIndex = index;
                this.updateSelection();
            });
            
            item.addEventListener('click', () => {
                this.executeSelected();
            });

            this.resultsContainer.appendChild(item);
        });
    }
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    window.CommandPaletteInstance = new CommandPalette();
    window.DrawerInstance = new Drawer();
});

/**
 * Drawer Component
 * Handles slide-over panels for detailed views
 */
class Drawer {
    constructor() {
        this.overlay = document.getElementById('drawer-overlay');
        this.drawer = document.getElementById('drawer');
        this.titleEl = document.getElementById('drawer-title');
        this.bodyEl = document.getElementById('drawer-body');
        this.footerEl = document.getElementById('drawer-footer');
        this.closeBtn = document.getElementById('drawer-close');
        
        if (this.overlay && this.closeBtn) {
            this.closeBtn.addEventListener('click', () => this.close());
            this.overlay.addEventListener('click', (e) => {
                if (e.target === this.overlay) this.close();
            });
            
            // Close on ESC
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && this.isOpen()) {
                    this.close();
                }
            });
        }
    }

    open({ title, content, footer = null, width = 'default' }) {
        if (!this.drawer) return;

        this.titleEl.textContent = title;
        this.bodyEl.innerHTML = content;
        
        if (footer) {
            this.footerEl.innerHTML = footer;
            this.footerEl.style.display = 'flex';
        } else {
            this.footerEl.style.display = 'none';
        }

        // Reset classes
        this.drawer.className = 'drawer';
        if (width === 'wide') this.drawer.classList.add('drawer-wide');
        if (width === 'full') this.drawer.classList.add('drawer-full');

        // Show
        this.overlay.classList.add('active');
        this.drawer.classList.add('active');
        document.body.style.overflow = 'hidden'; // Prevent scrolling
    }

    close() {
        if (!this.drawer) return;
        this.overlay.classList.remove('active');
        this.drawer.classList.remove('active');
        document.body.style.overflow = '';
        
        // Clear content after animation
        setTimeout(() => {
            this.bodyEl.innerHTML = '';
            this.footerEl.innerHTML = '';
        }, 300);
    }

    isOpen() {
        return this.overlay && this.overlay.classList.contains('active');
    }
}
