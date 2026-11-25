(function (global) {
    'use strict';

    if (global.Phonos3DViewerQoL && global.Phonos3DViewerQoL.version) {
        return;
    }

    var STYLE_ID = 'phonos-viewer-qol-style';
    var DOCK_CLASS = 'phonos-viewer-qol-dock';
    var BG_STORAGE_KEY = 'phonos_3d_qol_bg_index';
    var viewerStateMap = new WeakMap();
    var nglStateMap = new WeakMap();
    var molPatchBlocked = false;
    var nglPatchBlocked = false;

    var BACKGROUNDS = [
        { name: 'dark', color: '#09090b' },
        { name: 'slate', color: '#111827' },
        { name: 'navy', color: '#0b1f34' },
        { name: 'light', color: '#f8fafc' }
    ];

    function ensureStyles() {
        if (document.getElementById(STYLE_ID)) {
            return;
        }

        var style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = [
            '.phonos-viewer-qol{position:relative;outline:none;}',
            '.' + DOCK_CLASS + '{position:absolute;top:10px;right:10px;display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px;max-width:min(92%,460px);align-items:center;padding:6px;background:rgba(8,11,17,0.72);border:1px solid rgba(255,255,255,0.16);border-radius: var(--r-ui, 0px);backdrop-filter:blur(6px);opacity:0;transform:translateY(-4px);transition:opacity .18s ease,transform .18s ease;z-index:30;pointer-events:none;}',
            '.phonos-viewer-qol:hover > .' + DOCK_CLASS + ', .phonos-viewer-qol.is-active > .' + DOCK_CLASS + '{opacity:1;transform:translateY(0);pointer-events:auto;}',
            '.phonos-viewer-qol-btn{padding:4px 8px;min-width:42px;border-radius: var(--r-ui, 0px);border:1px solid rgba(255,255,255,0.2);background:rgba(17,24,39,0.86);color:#f3f4f6;font-size:11px;font-weight:600;line-height:1;cursor:pointer;transition:background .18s ease,border-color .18s ease,color .18s ease;}',
            '.phonos-viewer-qol-btn:hover{background:rgba(29,78,216,0.35);border-color:rgba(96,165,250,0.9);}',
            '.phonos-viewer-qol-btn.is-on{background:rgba(14,116,144,0.48);border-color:rgba(125,211,252,0.95);}',
            '.phonos-viewer-qol-toast{position:absolute;left:10px;bottom:10px;padding:6px 9px;background:rgba(2,6,23,0.84);border:1px solid rgba(148,163,184,0.5);border-radius: var(--r-ui, 0px);color:#e2e8f0;font-size:11px;z-index:31;opacity:0;transform:translateY(4px);transition:opacity .18s ease,transform .18s ease;pointer-events:none;}',
            '.phonos-viewer-qol-toast.is-visible{opacity:1;transform:translateY(0);}',
            '@media (max-width: 768px){.phonos-viewer-qol-btn{min-width:36px;padding:4px 6px;font-size:10px;}.phonos-viewer-qol-toast{max-width:80%;}}'
        ].join('');
        document.head.appendChild(style);
    }

    function toElement(target) {
        if (!target) {
            return null;
        }

        if (typeof target === 'string') {
            return document.getElementById(target) || document.querySelector(target);
        }

        if (target.nodeType === 1) {
            return target;
        }

        if (target[0] && target[0].nodeType === 1) {
            return target[0];
        }

        return null;
    }

    function resolveViewerContainer(container, viewer) {
        var el = toElement(container);
        if (el) {
            return el;
        }

        if (viewer && typeof viewer.getCanvas === 'function') {
            var canvas = viewer.getCanvas();
            if (canvas && canvas.parentElement) {
                return canvas.parentElement;
            }
        }

        return null;
    }

    function nowStamp() {
        var d = new Date();
        var pad = function (n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + '' + pad(d.getMonth() + 1) + '' + pad(d.getDate()) + '-' + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
    }

    function triggerDownload(dataUrlOrBlob, baseName, ext) {
        var link = document.createElement('a');
        if (typeof dataUrlOrBlob === 'string') {
            link.href = dataUrlOrBlob;
        } else {
            link.href = URL.createObjectURL(dataUrlOrBlob);
        }
        link.download = baseName + '-' + nowStamp() + '.' + ext;
        document.body.appendChild(link);
        link.click();
        link.remove();

        if (typeof dataUrlOrBlob !== 'string') {
            setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
        }
    }

    function getSavedBackgroundIndex() {
        try {
            var raw = localStorage.getItem(BG_STORAGE_KEY);
            var parsed = Number(raw);
            if (Number.isInteger(parsed) && parsed >= 0 && parsed < BACKGROUNDS.length) {
                return parsed;
            }
        } catch (error) {}
        return 0;
    }

    function saveBackgroundIndex(index) {
        try {
            localStorage.setItem(BG_STORAGE_KEY, String(index));
        } catch (error) {}
    }

    function dataUrlToBlob(dataUrl) {
        if (typeof dataUrl !== 'string' || dataUrl.indexOf(',') === -1) {
            return null;
        }

        var parts = dataUrl.split(',');
        var meta = parts[0];
        var body = parts[1];
        var mimeMatch = /data:([^;]+)/.exec(meta);
        var mime = mimeMatch ? mimeMatch[1] : 'image/png';

        try {
            var binary = atob(body);
            var len = binary.length;
            var bytes = new Uint8Array(len);
            for (var i = 0; i < len; i += 1) {
                bytes[i] = binary.charCodeAt(i);
            }
            return new Blob([bytes], { type: mime });
        } catch (error) {
            return null;
        }
    }

    function copyBlobToClipboard(blob) {
        if (!blob || !navigator.clipboard || typeof ClipboardItem === 'undefined') {
            return Promise.reject(new Error('Clipboard API unavailable'));
        }
        return navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
    }

    function canHandleShortcut(event) {
        var target = event.target;
        if (!target) {
            return true;
        }

        var tagName = (target.tagName || '').toLowerCase();
        if (tagName === 'input' || tagName === 'textarea' || tagName === 'select') {
            return false;
        }

        if (target.isContentEditable) {
            return false;
        }

        return true;
    }

    function setActive(container, isActive) {
        if (!container) {
            return;
        }
        container.classList.toggle('is-active', Boolean(isActive));
    }

    function cycleBackgroundColor(state, setter) {
        state.backgroundIndex = (state.backgroundIndex + 1) % BACKGROUNDS.length;
        saveBackgroundIndex(state.backgroundIndex);
        var next = BACKGROUNDS[state.backgroundIndex];
        setter(next.color);
        showToast(state, (window.i18n ? window.i18n.t('phonosViewer.backgroundPrefix') : 'Background: ') + next.name);
    }

    function showToast(state, message) {
        if (!state || !state.toast) {
            return;
        }

        state.toast.textContent = message;
        state.toast.classList.add('is-visible');
        clearTimeout(state.toastTimer);
        state.toastTimer = setTimeout(function () {
            if (state.toast) {
                state.toast.classList.remove('is-visible');
            }
        }, 1800);
    }

    function createButton(label, title, onClick) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'phonos-viewer-qol-btn';
        button.textContent = label;
        button.title = title;
        button.addEventListener('click', function (event) {
            event.preventDefault();
            event.stopPropagation();
            onClick(button);
        });
        return button;
    }

    function addDock(container, state, actions) {
        ensureStyles();

        if (typeof container.__phonosViewerQolCleanup === 'function') {
            container.__phonosViewerQolCleanup();
        }

        var existing = container.querySelector('.' + DOCK_CLASS);
        if (existing) {
            existing.remove();
        }
        var existingToast = container.querySelector('.phonos-viewer-qol-toast');
        if (existingToast) {
            existingToast.remove();
        }

        var dock = document.createElement('div');
        dock.className = DOCK_CLASS;
        dock.setAttribute('role', 'toolbar');
        dock.setAttribute('aria-label', '3D viewer quick controls');

        var resetBtn = createButton('RST', 'Reset view (R)', function () {
            actions.reset();
            showToast(state, window.i18n ? window.i18n.t('phonosViewer.viewReset') : 'View reset');
        });

        var spinBtn = createButton('SPN', 'Toggle spin (S)', function (button) {
            var isOn = actions.spin();
            button.classList.toggle('is-on', isOn);
            showToast(state, isOn ? (window.i18n ? window.i18n.t('phonosViewer.spinOn') : 'Spin on') : (window.i18n ? window.i18n.t('phonosViewer.spinOff') : 'Spin off'));
        });

        var pngBtn = createButton('PNG', 'Capture screenshot (P)', function () {
            actions.screenshot();
        });

        var copyBtn = createButton('CPY', 'Copy screenshot (C)', function () {
            actions.copy();
        });

        var bgBtn = createButton('BKG', 'Cycle background (B)', function () {
            actions.background();
        });

        var fullBtn = createButton('FULL', 'Toggle fullscreen (F)', function () {
            actions.fullscreen();
        });

        dock.appendChild(resetBtn);
        dock.appendChild(spinBtn);
        dock.appendChild(pngBtn);
        dock.appendChild(copyBtn);
        dock.appendChild(bgBtn);
        dock.appendChild(fullBtn);

        container.classList.add('phonos-viewer-qol');
        container.setAttribute('data-phonos-3d-qol', 'enabled');
        if (!container.getAttribute('tabindex')) {
            container.setAttribute('tabindex', '0');
        }

        var toast = document.createElement('div');
        toast.className = 'phonos-viewer-qol-toast';
        container.appendChild(toast);
        state.toast = toast;
        state.spinButton = spinBtn;

        container.appendChild(dock);

        var activate = function () {
            setActive(container, true);
            state.active = true;
        };

        var deactivate = function () {
            setActive(container, false);
            state.active = false;
        };

        var onFocusOut = function () {
            if (!container.contains(document.activeElement)) {
                deactivate();
            }
        };

        var onKeyDown = function (event) {
            if (!canHandleShortcut(event)) {
                return;
            }

            var key = (event.key || '').toLowerCase();
            if (key === 'r') {
                event.preventDefault();
                actions.reset();
                showToast(state, window.i18n ? window.i18n.t('phonosViewer.viewReset') : 'View reset');
                return;
            }
            if (key === 's') {
                event.preventDefault();
                var spinning = actions.spin();
                state.spinButton.classList.toggle('is-on', spinning);
                showToast(state, spinning ? (window.i18n ? window.i18n.t('phonosViewer.spinOn') : 'Spin on') : (window.i18n ? window.i18n.t('phonosViewer.spinOff') : 'Spin off'));
                return;
            }
            if (key === 'p') {
                event.preventDefault();
                actions.screenshot();
                return;
            }
            if (key === 'c') {
                event.preventDefault();
                actions.copy();
                return;
            }
            if (key === 'b') {
                event.preventDefault();
                actions.background();
                return;
            }
            if (key === 'f') {
                event.preventDefault();
                actions.fullscreen();
                return;
            }
            if (key === '?') {
                event.preventDefault();
                showToast(state, window.i18n ? window.i18n.t('phonosViewer.keyboardShortcutsQol') : 'Shortcuts: R S P C B F');
            }
        };

        var onDoubleClick = function () {
            actions.reset();
            showToast(state, window.i18n ? window.i18n.t('phonosViewer.viewReset') : 'View reset');
        };

        var onPointerDown = function () {
            if (document.activeElement !== container && typeof container.focus === 'function') {
                try {
                    container.focus({ preventScroll: true });
                } catch (error) {
                    container.focus();
                }
            }
        };

        container.addEventListener('mouseenter', activate);
        container.addEventListener('mouseleave', deactivate);
        container.addEventListener('focusin', activate);
        container.addEventListener('focusout', onFocusOut);
        container.addEventListener('pointerdown', onPointerDown);
        container.addEventListener('keydown', onKeyDown);
        container.addEventListener('dblclick', onDoubleClick);

        container.__phonosViewerQolCleanup = function () {
            clearTimeout(state.toastTimer);
            container.removeEventListener('mouseenter', activate);
            container.removeEventListener('mouseleave', deactivate);
            container.removeEventListener('focusin', activate);
            container.removeEventListener('focusout', onFocusOut);
            container.removeEventListener('pointerdown', onPointerDown);
            container.removeEventListener('keydown', onKeyDown);
            container.removeEventListener('dblclick', onDoubleClick);
            container.classList.remove('is-active');
            if (dock.parentNode === container) {
                dock.remove();
            }
            if (toast.parentNode === container) {
                toast.remove();
            }
        };

        var shouldShowHint = false;
        try {
            shouldShowHint = sessionStorage.getItem('phonos_3d_qol_hint') !== '1';
            if (shouldShowHint) {
                sessionStorage.setItem('phonos_3d_qol_hint', '1');
            }
        } catch (error) {
            shouldShowHint = false;
        }

        if (shouldShowHint) {
            setTimeout(function () {
                showToast(state, window.i18n ? window.i18n.t('phonosViewer.keyboardShortcutsQol') : 'Shortcuts: R S P C B F');
            }, 450);
        }
    }

    function setupResize(container, onResize) {
        var resizeObserver = null;
        var rafId = 0;
        var scheduled = false;

        function scheduleResize() {
            if (scheduled) {
                return;
            }
            scheduled = true;
            rafId = window.requestAnimationFrame(function () {
                scheduled = false;
                onResize();
            });
        }

        if (typeof ResizeObserver !== 'undefined') {
            resizeObserver = new ResizeObserver(function () {
                scheduleResize();
            });
            resizeObserver.observe(container);
        }

        var onWindowResize = function () { scheduleResize(); };
        window.addEventListener('resize', onWindowResize);
        document.addEventListener('fullscreenchange', onWindowResize);
        scheduleResize();

        return function cleanup() {
            if (rafId) {
                window.cancelAnimationFrame(rafId);
            }
            if (resizeObserver) {
                resizeObserver.disconnect();
            }
            window.removeEventListener('resize', onWindowResize);
            document.removeEventListener('fullscreenchange', onWindowResize);
        };
    }

    function setupVisibility(container, onHidden, onVisible) {
        var intersectionObserver = null;
        var inViewport = true;
        var pageVisible = !document.hidden;
        var hiddenState = false;

        function evaluate() {
            var shouldHide = !inViewport || !pageVisible;
            if (shouldHide === hiddenState) {
                return;
            }
            hiddenState = shouldHide;
            if (hiddenState) {
                onHidden();
            } else {
                onVisible();
            }
        }

        if (typeof IntersectionObserver !== 'undefined') {
            intersectionObserver = new IntersectionObserver(function (entries) {
                if (!entries || entries.length === 0) {
                    return;
                }
                var entry = entries[0];
                inViewport = Boolean(entry.isIntersecting && entry.intersectionRatio > 0.02);
                evaluate();
            }, {
                threshold: [0, 0.02, 0.1]
            });
            intersectionObserver.observe(container);
        }

        var onPageVisibility = function () {
            pageVisible = !document.hidden;
            evaluate();
        };

        document.addEventListener('visibilitychange', onPageVisibility);

        return function cleanup() {
            if (intersectionObserver) {
                intersectionObserver.disconnect();
            }
            document.removeEventListener('visibilitychange', onPageVisibility);
        };
    }

    function toggleFullscreen(container) {
        if (!container) {
            return;
        }

        if (document.fullscreenElement === container) {
            if (document.exitFullscreen) {
                document.exitFullscreen();
            }
            return;
        }

        if (container.requestFullscreen) {
            container.requestFullscreen().catch(function () {});
        }
    }

    function safeRender3Dmol(viewer) {
        if (viewer && typeof viewer.render === 'function') {
            viewer.render();
        }
    }

    function enhance3DmolViewer(viewer, container) {
        if (!viewer || viewerStateMap.has(viewer)) {
            return;
        }

        var el = resolveViewerContainer(container, viewer);
        if (!el) {
            return;
        }

        var state = {
            type: '3dmol',
            active: false,
            spinning: false,
            backgroundIndex: getSavedBackgroundIndex(),
            restoreSpinOnVisible: false,
            toast: null,
            toastTimer: null,
            spinButton: null,
            cleanupResize: null,
            cleanupVisibility: null
        };

        function capture3DmolImageData() {
            var imageData = null;
            if (typeof viewer.pngURI === 'function') {
                imageData = viewer.pngURI();
            } else if (typeof viewer.getCanvas === 'function') {
                var canvas = viewer.getCanvas();
                if (canvas && typeof canvas.toDataURL === 'function') {
                    imageData = canvas.toDataURL('image/png');
                }
            }
            return imageData;
        }

        function applySpin(nextSpin) {
            state.spinning = Boolean(nextSpin);
            if (typeof viewer.spin === 'function') {
                viewer.spin(state.spinning);
            }
            safeRender3Dmol(viewer);
            if (state.spinButton) {
                state.spinButton.classList.toggle('is-on', state.spinning);
            }
            return state.spinning;
        }

        var actions = {
            reset: function () {
                applySpin(false);
                if (typeof viewer.zoomTo === 'function') {
                    viewer.zoomTo();
                }
                safeRender3Dmol(viewer);
            },
            spin: function () {
                return applySpin(!state.spinning);
            },
            screenshot: function () {
                try {
                    var imageData = capture3DmolImageData();

                    if (!imageData) {
                        showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotUnavailable') : 'Screenshot unavailable');
                        return;
                    }

                    triggerDownload(imageData, 'phonos-3d', 'png');
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotSaved') : 'Screenshot saved');
                } catch (error) {
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotFailed') : 'Screenshot failed');
                }
            },
            copy: function () {
                var imageData = capture3DmolImageData();
                if (!imageData) {
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.copyUnavailable') : 'Copy unavailable');
                    return;
                }

                var blob = dataUrlToBlob(imageData);
                copyBlobToClipboard(blob).then(function () {
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotCopied') : 'Screenshot copied');
                }).catch(function () {
                    triggerDownload(imageData, 'phonos-3d', 'png');
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.clipboardBlockedSavedPng') : 'Clipboard blocked, saved PNG');
                });
            },
            background: function () {
                cycleBackgroundColor(state, function (color) {
                    if (typeof viewer.setBackgroundColor === 'function') {
                        viewer.setBackgroundColor(color);
                    }
                    safeRender3Dmol(viewer);
                });
            },
            fullscreen: function () {
                toggleFullscreen(el);
            }
        };

        addDock(el, state, actions);

        state.cleanupResize = setupResize(el, function () {
            if (typeof viewer.resize === 'function') {
                viewer.resize();
            }
            safeRender3Dmol(viewer);
        });

        state.cleanupVisibility = setupVisibility(el, function () {
            if (state.spinning) {
                state.restoreSpinOnVisible = true;
                applySpin(false);
            }
        }, function () {
            if (state.restoreSpinOnVisible) {
                state.restoreSpinOnVisible = false;
                applySpin(true);
            }
        });

        if (state.backgroundIndex > 0 && typeof viewer.setBackgroundColor === 'function') {
            viewer.setBackgroundColor(BACKGROUNDS[state.backgroundIndex].color);
            safeRender3Dmol(viewer);
        }

        var dockCleanup = el.__phonosViewerQolCleanup;
        el.__phonosViewerQolCleanup = function () {
            if (typeof state.cleanupResize === 'function') {
                state.cleanupResize();
                state.cleanupResize = null;
            }
            if (typeof state.cleanupVisibility === 'function') {
                state.cleanupVisibility();
                state.cleanupVisibility = null;
            }
            if (typeof dockCleanup === 'function') {
                dockCleanup();
            }
        };

        viewerStateMap.set(viewer, state);
    }

    function safeRenderNGL(stage) {
        if (stage && stage.viewer && typeof stage.viewer.requestRender === 'function') {
            stage.viewer.requestRender();
        }
    }

    function tryReplaceFunction(target, key, replacement) {
        if (!target || !key) {
            return false;
        }

        try {
            target[key] = replacement;
            if (target[key] === replacement) {
                return true;
            }
        } catch (error) {}

        try {
            Object.defineProperty(target, key, {
                value: replacement,
                writable: true,
                configurable: true
            });
            return target[key] === replacement;
        } catch (error) {}

        return false;
    }

    function enhanceNGLStage(stage, container) {
        if (!stage || nglStateMap.has(stage)) {
            return;
        }

        var el = toElement(container);
        if (!el && stage.viewer && stage.viewer.container) {
            el = stage.viewer.container;
        }

        if (!el) {
            return;
        }

        var state = {
            type: 'ngl',
            active: false,
            spinning: false,
            backgroundIndex: getSavedBackgroundIndex(),
            restoreSpinOnVisible: false,
            toast: null,
            toastTimer: null,
            spinButton: null,
            cleanupResize: null,
            cleanupVisibility: null
        };

        function applySpin(nextSpin) {
            state.spinning = Boolean(nextSpin);
            if (typeof stage.setSpin === 'function') {
                stage.setSpin(state.spinning);
            }
            safeRenderNGL(stage);
            if (state.spinButton) {
                state.spinButton.classList.toggle('is-on', state.spinning);
            }
            return state.spinning;
        }

        var actions = {
            reset: function () {
                applySpin(false);
                if (typeof stage.autoView === 'function') {
                    stage.autoView();
                }
                safeRenderNGL(stage);
            },
            spin: function () {
                return applySpin(!state.spinning);
            },
            screenshot: function () {
                if (typeof stage.makeImage !== 'function') {
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotUnavailable') : 'Screenshot unavailable');
                    return;
                }

                stage.makeImage({ factor: 2, antialias: true, trim: false, transparent: false }).then(function (blob) {
                    triggerDownload(blob, 'phonos-ngl', 'png');
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotSaved') : 'Screenshot saved');
                }).catch(function () {
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotFailed') : 'Screenshot failed');
                });
            },
            copy: function () {
                if (typeof stage.makeImage !== 'function') {
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.copyUnavailable') : 'Copy unavailable');
                    return;
                }

                stage.makeImage({ factor: 2, antialias: true, trim: false, transparent: false }).then(function (blob) {
                    copyBlobToClipboard(blob).then(function () {
                        showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotCopied') : 'Screenshot copied');
                    }).catch(function () {
                        triggerDownload(blob, 'phonos-ngl', 'png');
                        showToast(state, window.i18n ? window.i18n.t('phonosViewer.clipboardBlockedSavedPng') : 'Clipboard blocked, saved PNG');
                    });
                }).catch(function () {
                    showToast(state, window.i18n ? window.i18n.t('phonosViewer.screenshotFailed') : 'Screenshot failed');
                });
            },
            background: function () {
                cycleBackgroundColor(state, function (color) {
                    if (typeof stage.setParameters === 'function') {
                        stage.setParameters({ backgroundColor: color });
                    }
                    safeRenderNGL(stage);
                });
            },
            fullscreen: function () {
                toggleFullscreen(el);
            }
        };

        addDock(el, state, actions);

        state.cleanupResize = setupResize(el, function () {
            if (typeof stage.handleResize === 'function') {
                stage.handleResize();
            }
            safeRenderNGL(stage);
        });

        state.cleanupVisibility = setupVisibility(el, function () {
            if (state.spinning) {
                state.restoreSpinOnVisible = true;
                applySpin(false);
            }
        }, function () {
            if (state.restoreSpinOnVisible) {
                state.restoreSpinOnVisible = false;
                applySpin(true);
            }
        });

        if (state.backgroundIndex > 0 && typeof stage.setParameters === 'function') {
            stage.setParameters({ backgroundColor: BACKGROUNDS[state.backgroundIndex].color });
            safeRenderNGL(stage);
        }

        var dockCleanup = el.__phonosViewerQolCleanup;
        el.__phonosViewerQolCleanup = function () {
            if (typeof state.cleanupResize === 'function') {
                state.cleanupResize();
                state.cleanupResize = null;
            }
            if (typeof state.cleanupVisibility === 'function') {
                state.cleanupVisibility();
                state.cleanupVisibility = null;
            }
            if (typeof dockCleanup === 'function') {
                dockCleanup();
            }
        };

        nglStateMap.set(stage, state);
    }

    function patch3DmolFactory() {
        if (molPatchBlocked) {
            return true;
        }

        if (!global.$3Dmol || typeof global.$3Dmol.createViewer !== 'function') {
            return false;
        }

        if (global.$3Dmol.createViewer.__phonosQolPatched) {
            return true;
        }

        var original = global.$3Dmol.createViewer;
        var patched = function patchedCreateViewer(container) {
            var viewer = original.apply(this, arguments);
            try {
                enhance3DmolViewer(viewer, container);
            } catch (error) {
                console.warn('[Phonos3DViewerQoL] Failed to enhance 3Dmol viewer:', error);
            }
            return viewer;
        };

        patched.__phonosQolPatched = true;
        patched.__phonosQolOriginal = original;
        if (!tryReplaceFunction(global.$3Dmol, 'createViewer', patched)) {
            molPatchBlocked = true;
            console.warn('[Phonos3DViewerQoL] Could not patch $3Dmol.createViewer (read-only/global getter).');
            return true;
        }
        return true;
    }

    function patchNGLStage() {
        if (nglPatchBlocked) {
            return true;
        }

        if (!global.NGL || typeof global.NGL.Stage !== 'function') {
            return false;
        }

        var OriginalStage = global.NGL.Stage;
        if (OriginalStage.__phonosQolPatched) {
            return true;
        }

        var PatchedStage = function () {
            var args = Array.prototype.slice.call(arguments);
            var BoundCtor = Function.prototype.bind.apply(OriginalStage, [null].concat(args));
            var stage = new BoundCtor();

            try {
                enhanceNGLStage(stage, args[0]);
            } catch (error) {
                console.warn('[Phonos3DViewerQoL] Failed to enhance NGL stage:', error);
            }

            return stage;
        };

        PatchedStage.prototype = OriginalStage.prototype;
        if (typeof Object.setPrototypeOf === 'function') {
            Object.setPrototypeOf(PatchedStage, OriginalStage);
        }
        PatchedStage.__phonosQolPatched = true;
        PatchedStage.__phonosQolOriginal = OriginalStage;

        if (!tryReplaceFunction(global.NGL, 'Stage', PatchedStage)) {
            nglPatchBlocked = true;
            console.warn('[Phonos3DViewerQoL] Could not patch NGL.Stage (read-only/global getter).');
            return true;
        }
        return true;
    }

    function installPatches() {
        return {
            mol: patch3DmolFactory(),
            ngl: patchNGLStage()
        };
    }

    function beginAutoPatch() {
        // If PhonosViewer is loaded, it provides its own QoL controls
        // (Focus panel, keyboard shortcuts, screenshot, spin, background cycling, etc.)
        // Skip polling for 3Dmol/NGL which are no longer loaded.
        if (global.PhonosViewer) {
            return;
        }

        installPatches();

        var attempts = 0;
        var maxAttempts = 40;
        var timer = setInterval(function () {
            attempts += 1;
            // Stop early if PhonosViewer loaded (supersedes QoL dock)
            if (global.PhonosViewer) {
                clearInterval(timer);
                return;
            }
            var status = installPatches();
            var complete = status.mol && status.ngl;
            if (complete || attempts >= maxAttempts) {
                clearInterval(timer);
            }
        }, 250);
    }

    global.Phonos3DViewerQoL = {
        version: '1.1.1',
        installPatches: installPatches,
        enhance3DmolViewer: enhance3DmolViewer,
        enhanceNGLStage: enhanceNGLStage
    };

    beginAutoPatch();
})(window);
