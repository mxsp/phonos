/**
 * IoT UI Builder
 * Grid-based dashboard designer for IoT devices
 */

const WIDGET_TYPES = {
    // Input widgets (control device)
    toggle: {
        category: 'input',
        icon: 'bi-toggle-on',
        label: window.i18n.t('iot.widgets.toggle'),
        defaultSize: { w: 1, h: 1 },
        configFields: ['label', 'binding', 'on_label', 'off_label']
    },
    slider: {
        category: 'input',
        icon: 'bi-sliders',
        label: window.i18n.t('iot.widgets.slider'),
        defaultSize: { w: 2, h: 1 },
        configFields: ['label', 'binding', 'min', 'max', 'step', 'unit']
    },
    button: {
        category: 'input',
        icon: 'bi-square',
        label: window.i18n.t('iot.widgets.button'),
        defaultSize: { w: 1, h: 1 },
        configFields: ['label', 'binding', 'action', 'color', 'confirm']
    },
    select: {
        category: 'input',
        icon: 'bi-list',
        label: window.i18n.t('iot.widgets.select'),
        defaultSize: { w: 2, h: 1 },
        configFields: ['label', 'binding', 'options']
    },
    number: {
        category: 'input',
        icon: 'bi-hash',
        label: window.i18n.t('iot.widgets.number'),
        defaultSize: { w: 1, h: 1 },
        configFields: ['label', 'binding', 'min', 'max', 'step']
    },

    // Output widgets (display data)
    gauge: {
        category: 'output',
        icon: 'bi-speedometer2',
        label: window.i18n.t('iot.widgets.gauge'),
        defaultSize: { w: 2, h: 2 },
        configFields: ['label', 'binding', 'min', 'max', 'unit', 'color']
    },
    indicator: {
        category: 'output',
        icon: 'bi-circle-fill',
        label: window.i18n.t('iot.widgets.indicator'),
        defaultSize: { w: 1, h: 1 },
        configFields: ['label', 'binding', 'on_color', 'off_color']
    },
    text: {
        category: 'output',
        icon: 'bi-fonts',
        label: window.i18n.t('iot.widgets.text'),
        defaultSize: { w: 2, h: 1 },
        configFields: ['label', 'binding', 'format']
    },
    value: {
        category: 'output',
        icon: 'bi-123',
        label: window.i18n.t('iot.widgets.value'),
        defaultSize: { w: 1, h: 1 },
        configFields: ['label', 'binding', 'unit', 'precision']
    },
    chart: {
        category: 'output',
        icon: 'bi-graph-up',
        label: window.i18n.t('iot.widgets.chart'),
        defaultSize: { w: 3, h: 2 },
        configFields: ['label', 'binding', 'history_points', 'color']
    },
    progress: {
        category: 'output',
        icon: 'bi-bar-chart-fill',
        label: window.i18n.t('iot.widgets.progress'),
        defaultSize: { w: 2, h: 1 },
        configFields: ['label', 'binding', 'min', 'max', 'color']
    }
};

// Safe text escaping helper
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.textContent;
}

// Safe color validation
function sanitizeColor(color) {
    if (!color) return '#3b82f6';
    // Only allow valid hex colors or CSS variables
    if (/^#[0-9A-Fa-f]{6}$/.test(color)) return color;
    if (/^var\(--[a-z0-9-]+\)$/.test(color)) return color;
    return '#3b82f6';
}

// Safe number validation
function sanitizeNumber(value, defaultVal = 0, min = null, max = null) {
    const num = parseFloat(value);
    if (isNaN(num)) return defaultVal;
    if (min !== null && num < min) return min;
    if (max !== null && num > max) return max;
    return num;
}

class IoTUIBuilder {
    constructor(container, deviceId) {
        this.container = typeof container === 'string'
            ? document.querySelector(container)
            : container;
        this.deviceId = deviceId;
        this.widgets = [];
        this.selectedWidget = null;
        this.gridConfig = { columns: 4, rowHeight: 100, gap: 8 };
        this.mode = 'edit';
        this.deviceSchema = { inputs: [], outputs: [] };
        this.deviceState = {};
        this.pollInterval = null;
        this.widgetIdCounter = 0;

        this.init();
    }

    async init() {
        this.render();
        await this.loadDashboard();
        this.setupEventListeners();
    }

    render() {
        // Clear container safely
        while (this.container.firstChild) {
            this.container.removeChild(this.container.firstChild);
        }

        const builder = document.createElement('div');
        builder.className = 'iot-builder';

        // Toolbar
        const toolbar = this.createToolbar();
        builder.appendChild(toolbar);

        // Palette
        const palette = this.createPalette();
        builder.appendChild(palette);

        // Canvas
        const canvas = document.createElement('div');
        canvas.className = 'iot-builder-canvas';
        canvas.id = 'builder-canvas';
        const grid = document.createElement('div');
        grid.className = 'iot-grid';
        grid.id = 'widget-grid';
        canvas.appendChild(grid);
        builder.appendChild(canvas);

        // Inspector
        const inspector = this.createInspector();
        builder.appendChild(inspector);

        this.container.appendChild(builder);
        this.renderGrid();
    }

    createToolbar() {
        const toolbar = document.createElement('div');
        toolbar.className = 'iot-builder-toolbar';

        // Left section
        const left = document.createElement('div');
        left.className = 'iot-builder-toolbar-left';

        const backBtn = document.createElement('button');
        backBtn.className = 'btn btn-ghost btn-sm';
        backBtn.onclick = () => window.history.back();
        const backIcon = document.createElement('i');
        backIcon.className = 'bi bi-arrow-left';
        backBtn.appendChild(backIcon);
        left.appendChild(backBtn);

        const titleContainer = document.createElement('div');
        const title = document.createElement('div');
        title.className = 'iot-builder-title';
        title.textContent = window.i18n.t('iot.builderTitle');
        const deviceName = document.createElement('div');
        deviceName.className = 'iot-builder-device-name';
        deviceName.id = 'builder-device-name';
        deviceName.textContent = window.i18n.t('common.loading');
        titleContainer.appendChild(title);
        titleContainer.appendChild(deviceName);
        left.appendChild(titleContainer);

        toolbar.appendChild(left);

        // Center section
        const center = document.createElement('div');
        center.className = 'iot-builder-toolbar-center';

        const editBtn = document.createElement('button');
        editBtn.className = `btn btn-sm ${this.mode === 'edit' ? 'btn-primary' : 'btn-ghost'}`;
        editBtn.id = 'btn-edit-mode';
        const editIcon = document.createElement('i');
        editIcon.className = 'bi bi-pencil';
        editBtn.appendChild(editIcon);
        editBtn.appendChild(document.createTextNode(' ' + window.i18n.t('common.edit')));
        center.appendChild(editBtn);

        const previewBtn = document.createElement('button');
        previewBtn.className = `btn btn-sm ${this.mode === 'preview' ? 'btn-primary' : 'btn-ghost'}`;
        previewBtn.id = 'btn-preview-mode';
        const previewIcon = document.createElement('i');
        previewIcon.className = 'bi bi-eye';
        previewBtn.appendChild(previewIcon);
        previewBtn.appendChild(document.createTextNode(' ' + window.i18n.t('common.preview')));
        center.appendChild(previewBtn);

        toolbar.appendChild(center);

        // Right section
        const right = document.createElement('div');
        right.className = 'iot-builder-toolbar-right';

        const resetBtn = document.createElement('button');
        resetBtn.className = 'btn btn-ghost btn-sm';
        resetBtn.id = 'btn-reset';
        const resetIcon = document.createElement('i');
        resetIcon.className = 'bi bi-arrow-counterclockwise';
        resetBtn.appendChild(resetIcon);
        right.appendChild(resetBtn);

        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn btn-primary btn-sm';
        saveBtn.id = 'btn-save';
        const saveIcon = document.createElement('i');
        saveIcon.className = 'bi bi-check-lg';
        saveBtn.appendChild(saveIcon);
        saveBtn.appendChild(document.createTextNode(' ' + window.i18n.t('common.save')));
        right.appendChild(saveBtn);

        toolbar.appendChild(right);

        return toolbar;
    }

    createPalette() {
        const palette = document.createElement('div');
        palette.className = 'iot-builder-palette';

        // Input section
        const inputSection = document.createElement('div');
        inputSection.className = 'palette-section';
        const inputTitle = document.createElement('div');
        inputTitle.className = 'palette-section-title';
        inputTitle.textContent = window.i18n.t('iot.inputControls');
        inputSection.appendChild(inputTitle);
        const inputWidgets = document.createElement('div');
        inputWidgets.className = 'palette-widgets';
        inputWidgets.id = 'palette-inputs';
        inputSection.appendChild(inputWidgets);
        palette.appendChild(inputSection);

        // Output section
        const outputSection = document.createElement('div');
        outputSection.className = 'palette-section';
        const outputTitle = document.createElement('div');
        outputTitle.className = 'palette-section-title';
        outputTitle.textContent = window.i18n.t('iot.outputDisplays');
        outputSection.appendChild(outputTitle);
        const outputWidgets = document.createElement('div');
        outputWidgets.className = 'palette-widgets';
        outputWidgets.id = 'palette-outputs';
        outputSection.appendChild(outputWidgets);
        palette.appendChild(outputSection);

        this.renderPaletteWidgets(palette);

        return palette;
    }

    renderPaletteWidgets(palette) {
        const inputsContainer = palette.querySelector('#palette-inputs');
        const outputsContainer = palette.querySelector('#palette-outputs');

        Object.entries(WIDGET_TYPES).forEach(([type, config]) => {
            const widget = document.createElement('div');
            widget.className = 'palette-widget';
            widget.dataset.type = type;
            widget.dataset.category = config.category;
            widget.draggable = true;

            const icon = document.createElement('i');
            icon.className = `${config.icon} palette-widget-icon`;
            widget.appendChild(icon);

            const label = document.createElement('span');
            label.className = 'palette-widget-label';
            label.textContent = config.label;
            widget.appendChild(label);

            if (config.category === 'input') {
                inputsContainer.appendChild(widget);
            } else {
                outputsContainer.appendChild(widget);
            }
        });
    }

    createInspector() {
        const inspector = document.createElement('div');
        inspector.className = 'iot-builder-inspector';

        const header = document.createElement('div');
        header.className = 'inspector-header';
        const title = document.createElement('div');
        title.className = 'inspector-title';
        title.textContent = 'Properties';
        header.appendChild(title);
        inspector.appendChild(header);

        const content = document.createElement('div');
        content.id = 'inspector-content';
        const empty = document.createElement('div');
        empty.className = 'inspector-empty';
        empty.textContent = 'Select a widget to edit its properties';
        content.appendChild(empty);
        inspector.appendChild(content);

        return inspector;
    }

    renderGrid() {
        const grid = document.getElementById('widget-grid');
        if (!grid) return;

        // Clear grid safely
        while (grid.firstChild) {
            grid.removeChild(grid.firstChild);
        }

        // Create grid cells
        const totalCells = this.gridConfig.columns * 6;
        for (let i = 0; i < totalCells; i++) {
            const cell = document.createElement('div');
            cell.className = 'iot-grid-cell';
            cell.dataset.index = i;
            grid.appendChild(cell);
        }

        // Render widgets
        this.widgets.forEach(widget => this.renderWidget(widget));

        // Show empty state if no widgets
        if (this.widgets.length === 0 && this.mode === 'edit') {
            const empty = document.createElement('div');
            empty.className = 'iot-builder-empty';
            empty.style.gridColumn = '1/-1';

            const iconContainer = document.createElement('div');
            iconContainer.className = 'iot-builder-empty-icon';
            const icon = document.createElement('i');
            icon.className = 'bi bi-grid-3x3-gap';
            iconContainer.appendChild(icon);
            empty.appendChild(iconContainer);

            const text = document.createElement('div');
            text.className = 'iot-builder-empty-text';
            text.textContent = 'Drag widgets from the palette to start building your dashboard';
            empty.appendChild(text);

            grid.appendChild(empty);
        }
    }

    renderWidget(widget) {
        const grid = document.getElementById('widget-grid');
        if (!grid) return;

        const widgetEl = document.createElement('div');
        widgetEl.className = `iot-widget ${widget.id === this.selectedWidget?.id ? 'selected' : ''}`;
        widgetEl.dataset.widgetId = widget.id;
        widgetEl.style.gridColumn = `${widget.x + 1} / span ${widget.w}`;
        widgetEl.style.gridRow = `${widget.y + 1} / span ${widget.h}`;
        widgetEl.draggable = this.mode === 'edit';

        const config = widget.config || {};
        const typeConfig = WIDGET_TYPES[widget.type];
        const value = this.getWidgetValue(widget);

        // Header
        const header = document.createElement('div');
        header.className = 'iot-widget-header';

        const labelSpan = document.createElement('span');
        labelSpan.className = 'iot-widget-label';
        labelSpan.textContent = escapeHtml(config.label || typeConfig.label);
        header.appendChild(labelSpan);

        const actions = document.createElement('div');
        actions.className = 'iot-widget-actions';
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'iot-widget-action delete';
        deleteBtn.dataset.action = 'delete';
        deleteBtn.title = 'Delete';
        const deleteIcon = document.createElement('i');
        deleteIcon.className = 'bi bi-trash';
        deleteBtn.appendChild(deleteIcon);
        actions.appendChild(deleteBtn);
        header.appendChild(actions);

        widgetEl.appendChild(header);

        // Content
        const content = document.createElement('div');
        content.className = 'iot-widget-content';
        this.renderWidgetContent(content, widget, value);
        widgetEl.appendChild(content);

        grid.appendChild(widgetEl);
        this.attachWidgetEvents(widgetEl, widget);
    }

    renderWidgetContent(container, widget, value) {
        const config = widget.config || {};

        switch (widget.type) {
            case 'toggle':
                this.renderToggleWidget(container, config, value);
                break;
            case 'slider':
                this.renderSliderWidget(container, config, value);
                break;
            case 'button':
                this.renderButtonWidget(container, config);
                break;
            case 'select':
                this.renderSelectWidget(container, config, value);
                break;
            case 'number':
                this.renderNumberWidget(container, config, value);
                break;
            case 'gauge':
                this.renderGaugeWidget(container, config, value);
                break;
            case 'indicator':
                this.renderIndicatorWidget(container, config, value);
                break;
            case 'text':
                this.renderTextWidget(container, config, value);
                break;
            case 'value':
                this.renderValueWidget(container, config, value);
                break;
            case 'chart':
                this.renderChartWidget(container, config);
                break;
            case 'progress':
                this.renderProgressWidget(container, config, value);
                break;
            default:
                const unknown = document.createElement('span');
                unknown.textContent = `Unknown widget type: ${escapeHtml(widget.type)}`;
                container.appendChild(unknown);
        }
    }

    renderToggleWidget(container, config, value) {
        const isOn = !!value;
        const wrapper = document.createElement('div');
        wrapper.className = 'iot-toggle-container';

        const toggle = document.createElement('div');
        toggle.className = `iot-toggle ${isOn ? 'active' : ''}`;
        toggle.dataset.action = 'toggle';

        const knob = document.createElement('div');
        knob.className = 'iot-toggle-knob';
        toggle.appendChild(knob);
        wrapper.appendChild(toggle);

        const label = document.createElement('span');
        label.className = 'iot-toggle-label';
        label.textContent = isOn ? escapeHtml(config.on_label || 'On') : escapeHtml(config.off_label || 'Off');
        wrapper.appendChild(label);

        container.appendChild(wrapper);
    }

    renderSliderWidget(container, config, value) {
        const sliderVal = sanitizeNumber(value ?? config.default ?? 50, 50);
        const min = sanitizeNumber(config.min, 0);
        const max = sanitizeNumber(config.max, 100);
        const step = sanitizeNumber(config.step, 1);

        const wrapper = document.createElement('div');
        wrapper.className = 'iot-slider-container';

        const slider = document.createElement('input');
        slider.type = 'range';
        slider.className = 'iot-slider';
        slider.min = min;
        slider.max = max;
        slider.step = step;
        slider.value = sliderVal;
        slider.dataset.action = 'slider';
        wrapper.appendChild(slider);

        const valueDiv = document.createElement('div');
        valueDiv.className = 'iot-slider-value';

        const minSpan = document.createElement('span');
        minSpan.textContent = min;
        valueDiv.appendChild(minSpan);

        const currentSpan = document.createElement('span');
        currentSpan.textContent = `${sliderVal}${escapeHtml(config.unit || '')}`;
        valueDiv.appendChild(currentSpan);

        const maxSpan = document.createElement('span');
        maxSpan.textContent = max;
        valueDiv.appendChild(maxSpan);

        wrapper.appendChild(valueDiv);
        container.appendChild(wrapper);
    }

    renderButtonWidget(container, config) {
        const btn = document.createElement('button');
        btn.className = 'iot-button';
        btn.style.background = sanitizeColor(config.color);
        btn.dataset.action = 'button';
        btn.textContent = escapeHtml(config.label || 'Action');
        container.appendChild(btn);
    }

    renderSelectWidget(container, config, value) {
        const select = document.createElement('select');
        select.className = 'iot-select';
        select.dataset.action = 'select';

        const options = config.options || [];
        options.forEach(opt => {
            const option = document.createElement('option');
            option.value = opt.value;
            option.textContent = escapeHtml(opt.label);
            if (value === opt.value) option.selected = true;
            select.appendChild(option);
        });

        container.appendChild(select);
    }

    renderNumberWidget(container, config, value) {
        const input = document.createElement('input');
        input.type = 'number';
        input.className = 'inspector-input';
        if (config.min !== undefined) input.min = config.min;
        if (config.max !== undefined) input.max = config.max;
        input.step = config.step ?? 1;
        input.value = value ?? '';
        input.dataset.action = 'number';
        input.style.textAlign = 'center';
        input.style.fontSize = '1.25rem';
        container.appendChild(input);
    }

    renderGaugeWidget(container, config, value) {
        const gaugeVal = sanitizeNumber(value, 0);
        const gaugeMin = sanitizeNumber(config.min, 0);
        const gaugeMax = sanitizeNumber(config.max, 100);
        const gaugePercent = ((gaugeVal - gaugeMin) / (gaugeMax - gaugeMin)) * 100;
        const gaugeColor = sanitizeColor(config.color);

        const wrapper = document.createElement('div');
        wrapper.className = 'iot-widget-gauge';

        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'iot-gauge-svg');
        svg.setAttribute('viewBox', '0 0 100 60');

        const bgPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        bgPath.setAttribute('d', 'M 10 50 A 40 40 0 0 1 90 50');
        bgPath.setAttribute('fill', 'none');
        bgPath.setAttribute('stroke', 'var(--zinc-700)');
        bgPath.setAttribute('stroke-width', '8');
        bgPath.setAttribute('stroke-linecap', 'round');
        svg.appendChild(bgPath);

        const fgPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        fgPath.setAttribute('d', 'M 10 50 A 40 40 0 0 1 90 50');
        fgPath.setAttribute('fill', 'none');
        fgPath.setAttribute('stroke', gaugeColor);
        fgPath.setAttribute('stroke-width', '8');
        fgPath.setAttribute('stroke-linecap', 'round');
        fgPath.setAttribute('stroke-dasharray', `${gaugePercent * 1.26} 126`);
        svg.appendChild(fgPath);

        wrapper.appendChild(svg);

        const valueDiv = document.createElement('div');
        valueDiv.className = 'iot-gauge-value';
        valueDiv.textContent = gaugeVal;

        const unitSpan = document.createElement('span');
        unitSpan.className = 'iot-gauge-unit';
        unitSpan.textContent = escapeHtml(config.unit || '');
        valueDiv.appendChild(unitSpan);

        wrapper.appendChild(valueDiv);
        container.appendChild(wrapper);
    }

    renderIndicatorWidget(container, config, value) {
        const indicatorOn = !!value;
        const wrapper = document.createElement('div');
        wrapper.className = 'iot-indicator';

        const dot = document.createElement('div');
        dot.className = `iot-indicator-dot ${indicatorOn ? 'on' : 'off'}`;
        if (indicatorOn && config.on_color) {
            dot.style.background = sanitizeColor(config.on_color);
        }
        wrapper.appendChild(dot);

        const label = document.createElement('span');
        label.textContent = indicatorOn ? 'Active' : 'Inactive';
        wrapper.appendChild(label);

        container.appendChild(wrapper);
    }

    renderTextWidget(container, config, value) {
        const textDiv = document.createElement('div');
        textDiv.style.fontSize = '1rem';
        textDiv.style.color = 'var(--zinc-200)';
        textDiv.textContent = escapeHtml(value ?? config.format ?? '--');
        container.appendChild(textDiv);
    }

    renderValueWidget(container, config, value) {
        const displayVal = value ?? '--';
        const precision = sanitizeNumber(config.precision, 0, 0, 6);
        const formattedVal = typeof displayVal === 'number' ? displayVal.toFixed(precision) : displayVal;

        const wrapper = document.createElement('div');
        wrapper.className = 'iot-value-display';

        const numberSpan = document.createElement('span');
        numberSpan.className = 'iot-value-number';
        numberSpan.textContent = formattedVal;
        wrapper.appendChild(numberSpan);

        const unitSpan = document.createElement('span');
        unitSpan.className = 'iot-value-unit';
        unitSpan.textContent = escapeHtml(config.unit || '');
        wrapper.appendChild(unitSpan);

        container.appendChild(wrapper);
    }

    renderChartWidget(container, config) {
        const placeholder = document.createElement('div');
        placeholder.className = 'iot-chart-placeholder';

        const icon = document.createElement('i');
        icon.className = 'bi bi-graph-up';
        placeholder.appendChild(icon);

        placeholder.appendChild(document.createTextNode(` Chart: ${escapeHtml(config.binding || 'No binding')}`));
        container.appendChild(placeholder);
    }

    renderProgressWidget(container, config, value) {
        const progressVal = sanitizeNumber(value, 0);
        const progressMin = sanitizeNumber(config.min, 0);
        const progressMax = sanitizeNumber(config.max, 100);
        const progressPercent = ((progressVal - progressMin) / (progressMax - progressMin)) * 100;

        const wrapper = document.createElement('div');
        wrapper.className = 'iot-progress-container';

        const bar = document.createElement('div');
        bar.className = 'iot-progress-bar';

        const fill = document.createElement('div');
        fill.className = 'iot-progress-fill';
        fill.style.width = `${progressPercent}%`;
        fill.style.background = sanitizeColor(config.color);
        bar.appendChild(fill);
        wrapper.appendChild(bar);

        const labelDiv = document.createElement('div');
        labelDiv.className = 'iot-progress-label';

        const minSpan = document.createElement('span');
        minSpan.textContent = progressMin;
        labelDiv.appendChild(minSpan);

        const currentSpan = document.createElement('span');
        currentSpan.textContent = `${progressVal}${escapeHtml(config.unit || '')}`;
        labelDiv.appendChild(currentSpan);

        const maxSpan = document.createElement('span');
        maxSpan.textContent = progressMax;
        labelDiv.appendChild(maxSpan);

        wrapper.appendChild(labelDiv);
        container.appendChild(wrapper);
    }

    getWidgetValue(widget) {
        const binding = widget.config?.binding;
        if (binding && this.deviceState) {
            return this.deviceState[binding];
        }
        return widget.config?.default;
    }

    attachWidgetEvents(element, widget) {
        // Select on click
        element.addEventListener('click', (e) => {
            if (e.target.closest('[data-action]')) return;
            this.selectWidget(widget);
        });

        // Delete button
        element.querySelector('[data-action="delete"]')?.addEventListener('click', () => {
            this.deleteWidget(widget.id);
        });

        // Interactive elements in preview mode
        if (this.mode === 'preview') {
            const toggle = element.querySelector('[data-action="toggle"]');
            if (toggle) {
                toggle.addEventListener('click', () => this.handleWidgetAction(widget, 'toggle'));
            }

            const slider = element.querySelector('[data-action="slider"]');
            if (slider) {
                slider.addEventListener('change', (e) => this.handleWidgetAction(widget, 'slider', e.target.value));
            }

            const button = element.querySelector('[data-action="button"]');
            if (button) {
                button.addEventListener('click', () => this.handleWidgetAction(widget, 'button'));
            }

            const select = element.querySelector('[data-action="select"]');
            if (select) {
                select.addEventListener('change', (e) => this.handleWidgetAction(widget, 'select', e.target.value));
            }

            const numberInput = element.querySelector('[data-action="number"]');
            if (numberInput) {
                numberInput.addEventListener('change', (e) => this.handleWidgetAction(widget, 'number', e.target.value));
            }
        }

        // Drag events for edit mode
        if (this.mode === 'edit') {
            element.addEventListener('dragstart', (e) => {
                e.dataTransfer.setData('widget-id', widget.id);
                element.classList.add('dragging');
            });

            element.addEventListener('dragend', () => {
                element.classList.remove('dragging');
            });
        }
    }

    async handleWidgetAction(widget, action, value) {
        const binding = widget.config?.binding;
        if (!binding) return;

        let newValue;
        switch (action) {
            case 'toggle':
                newValue = !this.deviceState[binding];
                break;
            case 'slider':
            case 'number':
                newValue = parseFloat(value);
                break;
            case 'select':
                newValue = value;
                break;
            case 'button':
                newValue = true;
                break;
        }

        try {
            const response = await fetch(`/api/iot/v2/devices/${this.deviceId}/command`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    type: 'set_value',
                    target: binding,
                    payload: { value: newValue }
                })
            });

            if (response.ok) {
                // Optimistic update
                this.deviceState[binding] = newValue;
                this.renderGrid();
            }
        } catch (error) {
            console.error('Failed to send command:', error);
        }
    }

    selectWidget(widget) {
        this.selectedWidget = widget;
        document.querySelectorAll('.iot-widget').forEach(el => el.classList.remove('selected'));
        document.querySelector(`[data-widget-id="${widget.id}"]`)?.classList.add('selected');
        this.renderInspector();
    }

    renderInspector() {
        const content = document.getElementById('inspector-content');
        if (!content) return;

        // Clear content
        while (content.firstChild) {
            content.removeChild(content.firstChild);
        }

        if (!this.selectedWidget) {
            const empty = document.createElement('div');
            empty.className = 'inspector-empty';
            empty.textContent = 'Select a widget to edit its properties';
            content.appendChild(empty);
            return;
        }

        const widget = this.selectedWidget;
        const typeConfig = WIDGET_TYPES[widget.type];
        const config = widget.config || {};

        // Widget type section
        const typeSection = this.createInspectorSection('Widget');
        const typeField = this.createInspectorField('Type', 'text', 'disabled', typeConfig.label, true);
        typeSection.appendChild(typeField);
        content.appendChild(typeSection);

        // Configuration section
        const configSection = this.createInspectorSection('Configuration');

        // Label field
        configSection.appendChild(this.createInspectorField('Label', 'text', 'config-label', config.label || '', false, 'Widget label'));

        // Binding field
        const bindingField = document.createElement('div');
        bindingField.className = 'inspector-field';

        const bindingLabel = document.createElement('label');
        bindingLabel.className = 'inspector-label';
        bindingLabel.textContent = 'Data Binding';
        bindingField.appendChild(bindingLabel);

        const bindingSelect = document.createElement('select');
        bindingSelect.className = 'inspector-select';
        bindingSelect.id = 'config-binding';

        const defaultOption = document.createElement('option');
        defaultOption.value = '';
        defaultOption.textContent = `-- Select ${typeConfig.category} --`;
        bindingSelect.appendChild(defaultOption);

        const schemaItems = typeConfig.category === 'input' ? this.deviceSchema.inputs : this.deviceSchema.outputs;
        (schemaItems || []).forEach(item => {
            const option = document.createElement('option');
            option.value = item.id;
            option.textContent = escapeHtml(item.label || item.id);
            if (config.binding === item.id) option.selected = true;
            bindingSelect.appendChild(option);
        });

        bindingField.appendChild(bindingSelect);
        configSection.appendChild(bindingField);

        // Type-specific fields
        this.addTypeSpecificFields(configSection, widget);

        content.appendChild(configSection);

        // Layout section
        const layoutSection = this.createInspectorSection('Layout');
        layoutSection.appendChild(this.createInspectorField('Width (columns)', 'number', 'config-width', widget.w, false, '', 1, 4));
        layoutSection.appendChild(this.createInspectorField('Height (rows)', 'number', 'config-height', widget.h, false, '', 1, 4));
        content.appendChild(layoutSection);

        // Attach change listeners
        content.querySelectorAll('input, select').forEach(input => {
            input.addEventListener('change', () => this.updateWidgetConfig());
        });
    }

    createInspectorSection(title) {
        const section = document.createElement('div');
        section.className = 'inspector-section';

        const titleEl = document.createElement('div');
        titleEl.className = 'inspector-section-title';
        titleEl.textContent = title;
        section.appendChild(titleEl);

        return section;
    }

    createInspectorField(labelText, type, id, value, disabled = false, placeholder = '', min = null, max = null) {
        const field = document.createElement('div');
        field.className = 'inspector-field';

        const label = document.createElement('label');
        label.className = 'inspector-label';
        label.textContent = labelText;
        field.appendChild(label);

        const input = document.createElement('input');
        input.type = type;
        input.className = type === 'color' ? 'inspector-color-input' : 'inspector-input';
        input.id = id;
        input.value = value;
        if (disabled) input.disabled = true;
        if (placeholder) input.placeholder = placeholder;
        if (min !== null) input.min = min;
        if (max !== null) input.max = max;

        field.appendChild(input);
        return field;
    }

    addTypeSpecificFields(section, widget) {
        const config = widget.config || {};

        switch (widget.type) {
            case 'toggle':
                section.appendChild(this.createInspectorField('On Label', 'text', 'config-on_label', config.on_label || 'On'));
                section.appendChild(this.createInspectorField('Off Label', 'text', 'config-off_label', config.off_label || 'Off'));
                break;

            case 'slider':
            case 'progress':
            case 'gauge':
                section.appendChild(this.createInspectorField('Min Value', 'number', 'config-min', config.min ?? 0));
                section.appendChild(this.createInspectorField('Max Value', 'number', 'config-max', config.max ?? 100));
                section.appendChild(this.createInspectorField('Unit', 'text', 'config-unit', config.unit || '', false, 'e.g., %, \u00B0C'));
                if (widget.type === 'slider') {
                    section.appendChild(this.createInspectorField('Step', 'number', 'config-step', config.step ?? 1));
                }
                section.appendChild(this.createInspectorField('Color', 'color', 'config-color', config.color || '#3b82f6'));
                break;

            case 'value':
                section.appendChild(this.createInspectorField('Unit', 'text', 'config-unit', config.unit || '', false, 'e.g., %, \u00B0C'));
                section.appendChild(this.createInspectorField('Decimal Places', 'number', 'config-precision', config.precision ?? 0, false, '', 0, 6));
                break;

            case 'indicator':
                section.appendChild(this.createInspectorField('On Color', 'color', 'config-on_color', config.on_color || '#22c55e'));
                section.appendChild(this.createInspectorField('Off Color', 'color', 'config-off_color', config.off_color || '#52525b'));
                break;

            case 'button':
                section.appendChild(this.createInspectorField('Button Color', 'color', 'config-color', config.color || '#3b82f6'));

                const confirmField = document.createElement('div');
                confirmField.className = 'inspector-field';
                const confirmGroup = document.createElement('div');
                confirmGroup.className = 'inspector-checkbox-group';

                const confirmCheckbox = document.createElement('input');
                confirmCheckbox.type = 'checkbox';
                confirmCheckbox.className = 'inspector-checkbox';
                confirmCheckbox.id = 'config-confirm';
                confirmCheckbox.checked = !!config.confirm;
                confirmGroup.appendChild(confirmCheckbox);

                const confirmLabel = document.createElement('label');
                confirmLabel.className = 'inspector-label';
                confirmLabel.style.margin = '0';
                confirmLabel.textContent = 'Require confirmation';
                confirmGroup.appendChild(confirmLabel);

                confirmField.appendChild(confirmGroup);
                section.appendChild(confirmField);
                break;

            case 'chart':
                section.appendChild(this.createInspectorField('History Points', 'number', 'config-history_points', config.history_points ?? 50, false, '', 10, 500));
                section.appendChild(this.createInspectorField('Line Color', 'color', 'config-color', config.color || '#3b82f6'));
                break;
        }
    }

    updateWidgetConfig() {
        if (!this.selectedWidget) return;

        const widget = this.selectedWidget;

        // Update config from inspector fields
        widget.config = widget.config || {};

        const configFields = [
            'label', 'binding', 'min', 'max', 'step', 'unit', 'color',
            'on_label', 'off_label', 'on_color', 'off_color',
            'precision', 'history_points', 'confirm'
        ];

        configFields.forEach(field => {
            const input = document.getElementById(`config-${field}`);
            if (input) {
                if (input.type === 'checkbox') {
                    widget.config[field] = input.checked;
                } else if (input.type === 'number') {
                    widget.config[field] = parseFloat(input.value) || 0;
                } else {
                    widget.config[field] = input.value;
                }
            }
        });

        // Update layout
        const widthInput = document.getElementById('config-width');
        const heightInput = document.getElementById('config-height');
        if (widthInput) widget.w = parseInt(widthInput.value) || 1;
        if (heightInput) widget.h = parseInt(heightInput.value) || 1;

        this.renderGrid();
    }

    addWidget(type, x = 0, y = 0) {
        const typeConfig = WIDGET_TYPES[type];
        if (!typeConfig) return;

        const widget = {
            id: `widget_${++this.widgetIdCounter}`,
            type,
            x,
            y,
            w: typeConfig.defaultSize.w,
            h: typeConfig.defaultSize.h,
            config: {
                label: typeConfig.label
            }
        };

        this.widgets.push(widget);
        this.renderGrid();
        this.selectWidget(widget);
    }

    deleteWidget(widgetId) {
        this.widgets = this.widgets.filter(w => w.id !== widgetId);
        if (this.selectedWidget?.id === widgetId) {
            this.selectedWidget = null;
            this.renderInspector();
        }
        this.renderGrid();
    }

    setupEventListeners() {
        // Mode toggle buttons
        document.getElementById('btn-edit-mode')?.addEventListener('click', () => this.setMode('edit'));
        document.getElementById('btn-preview-mode')?.addEventListener('click', () => this.setMode('preview'));

        // Save button
        document.getElementById('btn-save')?.addEventListener('click', () => this.saveDashboard());

        // Reset button
        document.getElementById('btn-reset')?.addEventListener('click', () => {
            if (confirm('Reset dashboard? All unsaved changes will be lost.')) {
                this.loadDashboard();
            }
        });

        // Palette drag events
        document.querySelectorAll('.palette-widget').forEach(widget => {
            widget.addEventListener('dragstart', (e) => {
                e.dataTransfer.setData('widget-type', widget.dataset.type);
            });
        });

        // Canvas drop events
        const canvas = document.getElementById('builder-canvas');
        canvas?.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'copy';
        });

        canvas?.addEventListener('drop', (e) => {
            e.preventDefault();
            const widgetType = e.dataTransfer.getData('widget-type');
            if (widgetType) {
                // Calculate grid position from drop coordinates
                const rect = canvas.getBoundingClientRect();
                const x = Math.floor((e.clientX - rect.left) / (rect.width / this.gridConfig.columns));
                const y = Math.floor((e.clientY - rect.top) / this.gridConfig.rowHeight);
                this.addWidget(widgetType, Math.max(0, x), Math.max(0, y));
            }
        });

        // Grid cell drop events
        const grid = document.getElementById('widget-grid');
        grid?.addEventListener('dragover', (e) => {
            e.preventDefault();
            const cell = e.target.closest('.iot-grid-cell');
            if (cell) {
                cell.classList.add('drag-over');
            }
        });

        grid?.addEventListener('dragleave', (e) => {
            const cell = e.target.closest('.iot-grid-cell');
            if (cell) {
                cell.classList.remove('drag-over');
            }
        });

        grid?.addEventListener('drop', (e) => {
            document.querySelectorAll('.iot-grid-cell').forEach(c => c.classList.remove('drag-over'));
        });

        // Click outside to deselect
        canvas?.addEventListener('click', (e) => {
            if (e.target === canvas || e.target.classList.contains('iot-grid') || e.target.classList.contains('iot-grid-cell')) {
                this.selectedWidget = null;
                document.querySelectorAll('.iot-widget').forEach(el => el.classList.remove('selected'));
                this.renderInspector();
            }
        });
    }

    setMode(mode) {
        this.mode = mode;
        const builder = this.container.querySelector('.iot-builder');

        if (mode === 'preview') {
            builder?.classList.add('preview-mode');
            this.startPolling();
        } else {
            builder?.classList.remove('preview-mode');
            this.stopPolling();
        }

        // Update toolbar buttons
        const editBtn = document.getElementById('btn-edit-mode');
        const previewBtn = document.getElementById('btn-preview-mode');

        editBtn?.classList.toggle('btn-primary', mode === 'edit');
        editBtn?.classList.toggle('btn-ghost', mode !== 'edit');
        previewBtn?.classList.toggle('btn-primary', mode === 'preview');
        previewBtn?.classList.toggle('btn-ghost', mode !== 'preview');

        this.renderGrid();
    }

    startPolling() {
        this.pollData();
        this.pollInterval = setInterval(() => this.pollData(), 2000);
    }

    stopPolling() {
        if (this.pollInterval) {
            clearInterval(this.pollInterval);
            this.pollInterval = null;
        }
    }

    async pollData() {
        try {
            const response = await fetch(`/api/iot/v2/devices/${this.deviceId}/dashboard/data`);
            if (response.ok) {
                const data = await response.json();
                this.deviceState = data.state || {};
                if (this.mode === 'preview') {
                    this.renderGrid();
                }
            }
        } catch (error) {
            console.error('Failed to poll device data:', error);
        }
    }

    async loadDashboard() {
        try {
            const response = await fetch(`/api/iot/v2/devices/${this.deviceId}/dashboard`);
            if (response.ok) {
                const data = await response.json();

                // Update device info
                const deviceNameEl = document.getElementById('builder-device-name');
                if (deviceNameEl) {
                    deviceNameEl.textContent = data.device?.device_name || 'Unknown Device';
                }

                // Load schema
                this.deviceSchema = data.device?.schema || { inputs: [], outputs: [] };
                this.deviceState = data.device?.current_state || {};

                // Load layout
                const layout = data.dashboard?.layout || { widgets: [] };
                this.widgets = layout.widgets || [];
                this.gridConfig = layout.grid || this.gridConfig;

                // Update widget ID counter
                this.widgetIdCounter = this.widgets.reduce((max, w) => {
                    const num = parseInt(w.id.replace('widget_', '')) || 0;
                    return Math.max(max, num);
                }, 0);

                this.renderGrid();
            }
        } catch (error) {
            console.error('Failed to load dashboard:', error);
        }
    }

    async saveDashboard() {
        const layout = {
            widgets: this.widgets,
            grid: this.gridConfig
        };

        try {
            const response = await fetch(`/api/iot/v2/devices/${this.deviceId}/dashboard`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ layout })
            });

            if (response.ok) {
                this.showNotification('Dashboard saved successfully', 'success');
            } else {
                const error = await response.json();
                this.showNotification(error.error || 'Failed to save dashboard', 'error');
            }
        } catch (error) {
            console.error('Failed to save dashboard:', error);
            this.showNotification('Failed to save dashboard', 'error');
        }
    }

    showNotification(message, type = 'info') {
        const notification = document.createElement('div');
        notification.className = `notification notification-${type}`;
        notification.textContent = message;
        notification.style.cssText = `
            position: fixed;
            bottom: 1rem;
            right: 1rem;
            padding: 0.75rem 1rem;
            background: ${type === 'success' ? 'var(--green-600)' : type === 'error' ? 'var(--red-600)' : 'var(--zinc-700)'};
            color: white;
            border-radius: var(--r-ui, 0px);
            z-index: 1000;
        `;
        document.body.appendChild(notification);
        setTimeout(() => notification.remove(), 3000);
    }

    destroy() {
        this.stopPolling();
        while (this.container.firstChild) {
            this.container.removeChild(this.container.firstChild);
        }
    }
}

// Export for use
window.IoTUIBuilder = IoTUIBuilder;
