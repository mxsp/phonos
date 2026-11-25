/**
 * Phonos Wolfram UI helpers — syntax highlighting, error parsing,
 * and a structured error panel for .nb preview cells.
 *
 * Loaded as a sibling script to wolfram_nb.js. Exposes
 * window.PhonosWolframUI = {
 *   highlightSource(src, opts) -> HTMLElement
 *   parseWolframError(message, code) -> {category, head, body, hint, line, col}
 *   renderError(err, opts) -> HTMLElement
 *   showToast(text, kind?) -> void
 *   classifyErrorCode(errorCode) -> category string
 * }
 *
 * Pure DOM helpers — no notebook state. Safe to call from anywhere.
 */
(function () {
    'use strict';

    // ---- Wolfram symbol tables (used for fallback highlighter) -------------
    const WL_BUILTINS = new Set([
        'True','False','None','Null','All','Automatic','Infinity','I','Pi','E',
        'Indeterminate','ComplexInfinity','DirectedInfinity','Missing'
    ]);
    const WL_KEYWORDS = new Set([
        'If','Then','Else','Which','While','For','Do','Module','Block','With',
        'Function','Map','Apply','Table','Range','Length','Part','Select','Cases',
        'Sort','Flatten','Join','Append','Prepend','Insert','Delete','Take','Drop',
        'Position','Count','MemberQ','FreeQ','MatchQ','ToString','ToExpression',
        'Import','Export','Print','Return','Break','Continue','Catch','Throw',
        'Check','Abort','Quiet','Message','SetDelayed','Set','RuleDelayed','Rule',
        'Unset','Clear','ClearAll','Definition','Information','Hold','HoldFirst',
        'HoldAll','ReleaseHold','Evaluate','Unevaluated','List','Association',
        'Dataset','KeyValueMap','Keys','Values','Lookup','Merge','Normal','Counts',
        'GroupBy','SelectFirst','First','Last','Most','Rest','Reverse','RotateLeft',
        'RotateRight','Total','Accumulate','Differences','Tuples','Subsets','Permutations',
        'Complement','Union','Intersection'
    ]);
    const WL_VISUAL = new Set([
        'Plot','Plot3D','ListPlot','ListLinePlot','ListLogPlot','ListLogLogPlot',
        'BarChart','PieChart','Histogram','ContourPlot','DensityPlot','Show',
        'Graphics','Image','Manipulate','Animate','ParametricPlot','RegionPlot',
        'StreamPlot','VectorPlot','PlotLabel','AxesLabel','FrameLabel','Legended'
    ]);
    const WL_MATH = new Set([
        'Sin','Cos','Tan','ArcSin','ArcCos','ArcTan','Sinh','Cosh','Tanh',
        'Exp','Log','Log10','Log2','Sqrt','Abs','Round','Floor','Ceiling','Max','Min',
        'Mean','Median','Variance','StandardDeviation','Norm','Conjugate','Re','Im',
        'Arg','Mod','Quotient','GCD','LCM','Power','Times','Plus','Subtract','Divide',
        'Derivative','Integrate','D','DSolve','NDSolve','Solve','NSolve','FindRoot',
        'Minimize','Maximize','LinearProgramming','Fit','FindFit','Interpolation',
        'Simplify','FullSimplify','Expand','Factor','Together','Apart','Cancel',
        'TrigReduce','TrigExpand','Coefficient','Exponent','Part','Sum','Product',
        'Limit','Series','MatrixForm','Inverse','Transpose','Det','Eigenvalues',
        'Eigenvectors','LinearSolve','NullSpace','RowReduce','MatrixPower','Dot',
        'Cross','Norm','VectorQ','MatrixQ','TensorProduct','KroneckerProduct'
    ]);

    // ---- escapeHtml (mirrors wolfram_nb.js) -------------------------------
    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ====================================================================
    // 1. SYNTAX HIGHLIGHTING
    // ====================================================================

    /**
     * Highlight Wolfram source into a styled <pre>.
     * Returns an HTMLElement. If opts.lineErrors is set (array of 1-indexed
     * line numbers) those lines get a red marker stripe.
     */
    function highlightSource(src, opts) {
        opts = opts || {};
        const pre = document.createElement('pre');
        pre.className = 'wolfram-nb-source';
        pre.tabSize = 4;
        const lines = String(src || '').split('\n');
        const errorSet = new Set(opts.lineErrors || []);
        const maxLines = opts.maxLines || 5000;

        if (lines.length > maxLines) {
            const note = document.createElement('div');
            note.style.cssText = 'color:#8a98a8;font-size:11px;padding:6px 12px;';
            note.textContent = `Showing first ${maxLines} of ${lines.length} lines.`;
            pre.appendChild(note);
            lines.length = maxLines;
        }

        lines.forEach((line, i) => {
            const ln = i + 1;
            const lineEl = document.createElement('span');
            lineEl.className = 'wolfram-nb-source-line';
            if (errorSet.has(ln)) lineEl.setAttribute('data-line-error', '1');
            const num = document.createElement('span');
            num.className = 'wolfram-nb-line-num';
            num.textContent = ln;
            lineEl.appendChild(num);
            lineEl.appendChild(document.createTextNode(_highlightLine(line)));
            lineEl.appendChild(document.createTextNode('\n'));
            pre.appendChild(lineEl);
        });
        return pre;
    }

    // Tokenize and emit HTML for a single line. We process the line in
    // chunks so we don't lose ordering. Single-pass would be faster but
    // a regex tokenizer is plenty for preview-length cells (< 10k lines).
    function _highlightLine(line) {
        let i = 0;
        let out = '';
        const len = line.length;
        while (i < len) {
            const ch = line[i];

            // Line comment
            if (ch === '(' && line[i + 1] === '*') {
                const end = line.indexOf('*)', i + 2);
                const stop = end === -1 ? len : end + 2;
                out += `<span class="wl-comment">${escapeHtml(line.slice(i, stop))}</span>`;
                i = stop;
                continue;
            }
            // Strings: "..." and '...' but '...' is rarely used as Wolfram string;
            // treat both the same for safety.
            if (ch === '"' || ch === "'") {
                const quote = ch;
                let j = i + 1;
                while (j < len) {
                    if (line[j] === '\\') { j += 2; continue; }
                    if (line[j] === quote) { j++; break; }
                    j++;
                }
                out += `<span class="wl-string">${escapeHtml(line.slice(i, j))}</span>`;
                i = j;
                continue;
            }
            // Numbers
            const numMatch = /^(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)/.exec(line.slice(i));
            if (numMatch) {
                out += `<span class="wl-number">${escapeHtml(numMatch[0])}</span>`;
                i += numMatch[0].length;
                continue;
            }
            // Symbols (identifiers)
            if (/[a-zA-Z\$]/.test(ch)) {
                const m = /^[a-zA-Z_\$][a-zA-Z0-9\$]*/.exec(line.slice(i));
                if (m) {
                    const word = m[0];
                    let cls;
                    if (WL_BUILTINS.has(word)) cls = 'wl-builtin';
                    else if (WL_KEYWORDS.has(word)) cls = 'wl-keyword';
                    else if (WL_VISUAL.has(word)) cls = 'wl-keyword';
                    else if (WL_MATH.has(word)) cls = 'wl-atom';
                    else cls = 'wl-variable';
                    out += `<span class="${cls}">${escapeHtml(word)}</span>`;
                    i += word.length;
                    continue;
                }
            }
            // Patterns: #, ##, #1, #2, #name, _, __, ___
            if (ch === '#') {
                const m = /^#+(?:[a-zA-Z_][a-zA-Z0-9_]*|\d+)?/.exec(line.slice(i));
                if (m) {
                    out += `<span class="wl-slot">${escapeHtml(m[0])}</span>`;
                    i += m[0].length;
                    continue;
                }
            }
            if (ch === '_') {
                const m = /^_{1,3}/.exec(line.slice(i));
                if (m) {
                    out += `<span class="wl-pattern">${escapeHtml(m[0])}</span>`;
                    i += m[0].length;
                    continue;
                }
            }
            // Operators and brackets
            if (/[{}()\[\]]/.test(ch)) {
                out += `<span class="wl-bracket">${escapeHtml(ch)}</span>`;
                i++;
                continue;
            }
            if (/<-|->|:=|==|===?|!=|<=|>=|\|\||&&|!|\+|-|\*|\/|\^|;|,|\./.test(ch)) {
                const m = /^(?:<-|->|:=|==|===?|!=|<=|>=|\|\||&&|!|\+|-|\*|\/|\^|;|,|\.\.)/.exec(line.slice(i));
                if (m) {
                    out += `<span class="wl-operator">${escapeHtml(m[0])}</span>`;
                    i += m[0].length;
                    continue;
                }
            }
            // Fallback
            out += escapeHtml(ch);
            i++;
        }
        return out;
    }

    // ====================================================================
    // 2. ERROR PARSING
    // ====================================================================

    /**
     * Classify a backend error_code (from /api/notebooks/wolfram/evaluate)
     * into one of our display categories.
     */
    function classifyErrorCode(code) {
        if (!code) return 'general';
        switch (code) {
            case 'missing_credentials':
            case 'auth_failed':
                return 'auth';
            case 'unreachable':
            case 'http_error':
                return 'network';
            case 'timeout':
                return 'timeout';
            case 'internal':
                return 'general';
            default:
                return 'general';
        }
    }

    /**
     * Parse a Wolfram-style error string. Wolfram errors look like:
     *   Solve::nsmet: Unable to solve for ...
     *     (line 3 of "<cell source>")
     *   Syntax::sntxi: Incomplete expression; more input is needed.
     *   General::stop: Further output of ... will be suppressed ...
     *   Tag::tag: ... encountered ...
     *   String::strse: Invalid special character ...
     *
     * Also handles Python-style multi-line tracebacks (from network failures
     * or wrapper errors).
     */
    function parseWolframError(message, code) {
        const raw = String(message || '').trim();
        if (!raw) {
            return { category: 'general', tag: null, head: 'Error', body: '', hint: '', line: null, col: null };
        }

        // Python traceback detection
        if (/^Traceback \(most recent call last\)/m.test(raw) || /^  File "/.test(raw)) {
            const last = raw.split('\n').filter(Boolean).pop() || raw;
            return {
                category: 'general',
                tag: 'Traceback',
                head: last.replace(/^.+?: /, '').slice(0, 200) || 'Server error',
                body: raw,
                hint: 'This looks like a server-side traceback, not a Wolfram-language error.',
                line: null,
                col: null
            };
        }

        // Leading tag pattern: Tag::msg: body
        let m = /^([A-Za-z][A-Za-z0-9]*)::([a-zA-Z0-9]+):\s*(.+)$/m.exec(raw);

        // Sometimes the tag is on a single line but extra context follows
        const headLine = raw.split('\n', 1)[0];
        let tag = null, msgName = null, bodyText = '';
        if (m && m.index === 0) {
            tag = m[1];
            msgName = m[2];
            bodyText = m[3];
        } else {
            // Headless error - just take the first line
            bodyText = headLine;
        }

        // Heuristic category
        let category = 'general';
        const tagLower = (tag || '').toLowerCase();
        const msgLower = (msgName || '').toLowerCase();
        const fullLower = raw.toLowerCase();
        if (tagLower === 'syntax' || msgLower.startsWith('sntx') || /syntax\s*error/i.test(raw)) {
            category = 'syntax';
        } else if (msgLower === 'noval' || msgLower === 'sym' || tagLower === 'symbol' ||
                   /^symbol .* (is not .* )?(known|defined)/i.test(raw) ||
                   /unknown (symbol|option|option name)/i.test(raw) ||
                   /cannot find .* (symbol|function|option)/i.test(raw)) {
            category = 'undefined';
        } else if (msgLower === 'strse' || msgLower === 'strs' || msgLower === 'str') {
            category = 'syntax';
        }

        // Line/column extraction: "at line 3" or "(line 3)" or "Line 3:" etc.
        let lineNum = null, colNum = null;
        const lineMatch = /(?:at\s+line|line)\s+(\d+)(?:\s*[:,]\s*(\d+))?/i.exec(raw);
        if (lineMatch) {
            lineNum = parseInt(lineMatch[1], 10);
            if (lineMatch[2]) colNum = parseInt(lineMatch[2], 10);
        }

        // Hint generator
        let hint = '';
        if (category === 'syntax') {
            hint = 'Check brackets, commas, and that every <code>[</code> has a matching <code>]</code>. Wolfram needs <code>;</code> to suppress output, not to end a statement.';
        } else if (category === 'undefined') {
            hint = 'A symbol is not defined. Make sure you loaded a package (e.g. <code>Needs["foo`"]</code>) or assigned the value earlier in the notebook.';
        } else if (msgName === 'nsmet' || /unable to (solve|compute)/i.test(raw)) {
            hint = 'Mathematica could not find an analytical solution. Try a numeric solver (e.g. <code>NSolve</code> / <code>FindRoot</code>) or simplify the expression first.';
        } else if (/recursion (limit|depth)/i.test(raw) || msgName === 'reclim') {
            hint = 'A function called itself too many times. Add a base case or break out with <code>Throw</code>/<code>Catch</code>.';
        } else if (/memory|allocat/i.test(raw)) {
            hint = 'The evaluation ran out of memory. Try working on a smaller slice of the data, e.g. with <code>Take</code> or <code>RandomSample</code>.';
        }

        const head = tag
            ? `${tag}::${msgName}: ${_trim(bodyText, 220)}`
            : _trim(bodyText, 240);

        return {
            category,
            tag: tag ? `${tag}::${msgName}` : (msgName || null),
            head,
            body: raw,
            hint,
            line: lineNum,
            col: colNum
        };
    }

    function _trim(s, n) {
        s = String(s || '');
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    // ====================================================================
    // 3. ERROR PANEL RENDERING
    // ====================================================================

    function _iconSvg() {
        return '<svg class="wolfram-nb-error-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
            '<circle cx="12" cy="12" r="10"/>' +
            '<line x1="12" y1="8" x2="12" y2="12"/>' +
            '<line x1="12" y1="16" x2="12.01" y2="16"/>' +
            '</svg>';
    }

    /**
     * Render a structured error panel.
     * opts: {
     *   parsed: parseWolframError() result (or we'll parse from rawText),
     *   rawText: string (used when opts.parsed isn't supplied),
     *   code: source string (for context / line highlighting),
     *   onCopy: function(text) - called for copy actions (optional)
     * }
     */
    function renderError(opts) {
        opts = opts || {};
        const parsed = opts.parsed || parseWolframError(opts.rawText || '');
        const category = opts.categoryOverride || parsed.category || 'general';
        const wrap = document.createElement('div');
        wrap.className = `wolfram-nb-error cat-${category}`;
        wrap.setAttribute('data-error-category', category);

        // Header
        const head = document.createElement('div');
        head.className = 'wolfram-nb-error-head';
        head.innerHTML =
            _iconSvg() +
            `<span class="wolfram-nb-error-title"></span>` +
            (parsed.tag ? `<span class="wolfram-nb-error-tag"></span>` : '');
        const titleEl = head.querySelector('.wolfram-nb-error-title');
        titleEl.textContent = parsed.head || 'Error';
        if (parsed.tag) {
            const tagEl = head.querySelector('.wolfram-nb-error-tag');
            if (tagEl) tagEl.textContent = parsed.tag;
        }
        wrap.appendChild(head);

        // Body
        if (parsed.body) {
            const body = document.createElement('div');
            body.className = 'wolfram-nb-error-body';
            body.textContent = parsed.body;
            wrap.appendChild(body);
        }

        // Hint
        if (parsed.hint) {
            const hint = document.createElement('div');
            hint.className = 'wolfram-nb-error-hint';
            hint.innerHTML = parsed.hint;
            wrap.appendChild(hint);
        }

        // Actions
        const actions = document.createElement('div');
        actions.className = 'wolfram-nb-error-actions';

        // Toggle traceback
        if (parsed.body && parsed.body.length > 240) {
            const toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.className = 'wolfram-nb-err-btn';
            toggle.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg> Collapse';
            toggle.addEventListener('click', function () {
                const collapsed = wrap.classList.toggle('is-collapsed');
                toggle.innerHTML = collapsed
                    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 15 12 9 18 15"/></svg> Expand'
                    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg> Collapse';
            });
            // Auto-collapse very long bodies
            if (parsed.body.length > 1500) {
                wrap.classList.add('is-collapsed');
            }
            actions.appendChild(toggle);
        }

        // Copy error
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'wolfram-nb-err-btn';
        copyBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy';
        copyBtn.addEventListener('click', function () {
            const text = (parsed.tag ? parsed.tag + ': ' : '') + (parsed.body || '');
            _copyText(text, copyBtn);
        });
        actions.appendChild(copyBtn);

        // Copy source button when code context is available
        if (opts.code) {
            const copySrc = document.createElement('button');
            copySrc.type = 'button';
            copySrc.className = 'wolfram-nb-err-btn';
            copySrc.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> Copy code';
            copySrc.addEventListener('click', function () {
                _copyText(opts.code, copySrc);
            });
            actions.appendChild(copySrc);
        }

        wrap.appendChild(actions);
        return wrap;
    }

    function _copyText(text, btn) {
        const finish = function (ok) {
            if (!btn) return;
            const orig = btn.innerHTML;
            btn.innerHTML = ok
                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Copied'
                : 'Copy failed';
            setTimeout(function () { btn.innerHTML = orig; }, 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () { finish(true); }, function () {
                _fallbackCopy(text); finish(true);
            });
        } else {
            _fallbackCopy(text); finish(true);
        }
    }
    function _fallbackCopy(text) {
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        } catch (e) { /* ignore */ }
    }

    // ====================================================================
    // 4. TOAST
    // ====================================================================
    let _toastEl = null;
    let _toastTimer = null;
    function showToast(text, kind) {
        if (!_toastEl) {
            _toastEl = document.createElement('div');
            _toastEl.className = 'wolfram-nb-toast';
            document.body.appendChild(_toastEl);
        }
        _toastEl.textContent = text;
        _toastEl.classList.remove('is-error');
        if (kind === 'error') _toastEl.classList.add('is-error');
        _toastEl.classList.add('is-show');
        clearTimeout(_toastTimer);
        _toastTimer = setTimeout(function () {
            _toastEl.classList.remove('is-show');
        }, kind === 'error' ? 6000 : 3500);
    }

    // ====================================================================
    // PUBLIC API
    // ====================================================================
    window.PhonosWolframUI = {
        highlightSource: highlightSource,
        parseWolframError: parseWolframError,
        classifyErrorCode: classifyErrorCode,
        renderError: renderError,
        showToast: showToast,
        escapeHtml: escapeHtml
    };
})();
