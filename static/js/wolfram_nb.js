/**
 * Wolfram Notebook (.nb / .wl / .wls / .m) renderer for the file preview sidebar.
 *
 * Parses the Wolfram expression format used in .nb files
 *   Notebook[{ Cell[BoxData[...], "Input"], Cell[..., "Output"], ... }, ...]
 * into a structured list of input cells, then renders each cell with a
 * 'Run' / 'Run All' button that POSTs the source to
 *   POST /api/notebooks/wolfram/evaluate
 * (which forwards to the running wolfram-svc container using the user's
 * saved Wolfram Cloud credentials).
 *
 * The renderer is intentionally lenient — malformed cells are skipped, not
 * fatal. Plain .wl/.wls/.m files (single-cell Wolfram source) are wrapped in
 * a single-cell notebook for uniform treatment.
 *
 * Exposed as window.PhonosWolframNb (module pattern) and as App helpers
 * (App._openWolframNotebookPreview, App._runWolframNotebookAll,
 *  App._runWolframNotebookCell).
 */
(function () {
    'use strict';

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // Lazily syntax-highlight a <pre> element. We replace the textContent
    // with the highlighted markup only when the cell is actually visible —
    // very long notebooks render in a snap and become highlighted on scroll.
    function _maybeHighlight(pre, source) {
        if (!pre || !source) return;
        const lines = source.split('\n').length;
        // Skip very large cells (1000+ lines) for the preview - they would
        // dominate the DOM and make scrolling choppy.
        if (lines > 1000) return;
        const UI = window.PhonosWolframUI;
        if (!UI || !UI.highlightSource) return;
        try {
            const highlighted = UI.highlightSource(source, { lineErrors: [] });
            pre.innerHTML = '';
            pre.textContent = ''; // clear before replacement so line counts reset
            while (highlighted.firstChild) {
                pre.appendChild(highlighted.firstChild);
            }
            // Preserve the decoded/raw toggle dataset.
            pre.dataset.decoded = source;
        } catch (e) {
            // Highlighter is best-effort — fall back silently to plain text.
            console.warn('Wolfram syntax highlight failed', e);
        }
    }

    // Re-render an already-highlighted cell with red marker stripes on the
    // lines reported in the error. Safe to call repeatedly.
    function _highlightWithLineErrors(cellEl, errorLine) {
        if (!cellEl || !errorLine) return;
        const pre = cellEl.querySelector('.wolfram-nb-cell-code');
        const src = pre && pre.dataset ? pre.dataset.decoded : null;
        if (!src || !window.PhonosWolframUI) return;
        const highlighted = window.PhonosWolframUI.highlightSource(src, { lineErrors: [errorLine] });
        pre.innerHTML = '';
        while (highlighted.firstChild) pre.appendChild(highlighted.firstChild);
    }

// ======================================================================
    // Faithful Wolfram Notebook parser (depth-scanner + box flattener).
    //
    // Real .nb files store code as a tree of *Box expressions:
    //   Notebook[{ Cell[BoxData[ RowBox[{"a","+","b"} ], "Input"], ... }]
    // Mathematica also groups related cells inside Cell[CellGroupData[{...}]].
    // The previous AST-based parser collapsed this structure and lost most
    // content. This implementation walks the raw string with a
    // bracket-matching scanner, flattens RowBox[{"a","op","b"}] -> "a op b",
    // recognises SuperscriptBox/SubscriptBox, and recurses into CellGroupData
    // so every Input cell is recovered as runnable Wolfram source.
    // ======================================================================

    const MAX_CELLS = 500;
    const MAX_CELL_SOURCE_CHARS = 100000;

    const CELL_STYLES = new Set([
        'Input', 'Output', 'Text', 'Title', 'Subtitle', 'Author',
        'Section', 'Subsection', 'Subsubsection', 'Chapter', 'Subchapter',
        'Code', 'Message', 'Print', 'Program', 'DisplayFormula',
        'ItemNumbered', 'Item', 'SubitemNumbered', 'Subitem', 'Subsubitem',
        'Notebook', 'OutputToBeSaved'
    ]);

    function _isWs(c) {
        return c === ' ' || c === '\t' || c === '\n' || c === '\r' || c === '\f' || c === '\v';
    }

    function _skipWs(src, i) {
        while (i < src.length && _isWs(src[i])) i++;
        return i;
    }

    // Find the matching close bracket for the opener at index ``b``.
    // Strings ("...") and (* comments *) are skipped transparently.
    function _matchBracket(src, b) {
        const open = src[b];
        const close = open === '[' ? ']' : open === '{' ? '}' : ')';
        let depth = 0, inS = false, inC = false;
        for (let i = b; i < src.length; i++) {
            const ch = src[i], nx = src[i + 1];
            if (inC) { if (ch === '*' && nx === ')') { inC = false; i++; } continue; }
            if (inS) { if (ch === '\\') { i++; continue; } if (ch === '"') inS = false; continue; }
            if (ch === '(' && nx === '*') { inC = true; i++; continue; }
            if (ch === '"') { inS = true; continue; }
            if (ch === '[' || ch === '{' || ch === '(') depth++;
            else if (ch === ']' || ch === '}' || ch === ')') {
                depth--;
                if (depth === 0) return i;
            }
        }
        return -1;
    }

    // Match a Wolfram symbol (letter-led identifier) at idx; returns name or null.
    function _matchSymbol(src, idx) {
        const n = src.length;
        if (idx >= n) return null;
        const c0 = src.charCodeAt(idx);
        const isLetter = (c0 >= 65 && c0 <= 90) || (c0 >= 97 && c0 <= 122);
        if (!isLetter) return null;
        let j = idx + 1;
        while (j < n) {
            const c = src.charCodeAt(j);
            const ok = (c >= 65 && c <= 90) || (c >= 97 && c <= 122) ||
                       (c >= 48 && c <= 57) || c === 36 || c === 95;
            if (!ok) break;
            j++;
        }
        return src.slice(idx, j);
    }

    // Wolfram named whitespace / line-break characters that appear as
    // ``\[Name]`` tokens inside string literals and RowBox child lists.
    // Converting them to real whitespace keeps the rendered code multi-line
    // and readable instead of one giant single-line cell with literal
    // ``\[IndentingNewLine]`` placeholders (which also clips in the editor).
    // Named characters that become real whitespace (line breaks, tabs, etc.)
    const NAMED_WHITESPACE = {
        'IndentingNewLine': '\n', 'NewLine': '\n', 'RawNewLine': '\n',
        'Return': '\n', 'RawReturn': '\r',
        'Tab': '\t', 'RawTab': '\t',
        'RawSpace': ' ',
        'NoBreak': '', 'SoftHyphen': '', 'Null': '',
        'NonBreakingSpace': '\u00A0',
        'PageBreakAbove': '\n', 'PageBreakBelow': '\n', 'PageBreakWithin': '\n',
        'Continuation': '\n',
        'DiscretionaryLineBreak': '\n', 'LineBreak': '\n',
        'ParagraphEnd': '\n', 'ParagraphSeparator': '\n',
        'AlignmentGUIDE': ''
    };

    // Named characters decoded to real Unicode for display in text/markdown
    // cells. These are NOT whitespace — they're printable characters
    // (German umlauts, accented letters, Greek letters used in prose,
    // math operators, etc.) that should render as proper Unicode in the
    // notebook instead of raw \[Name] escape sequences.
    //
    // IMPORTANT: Wolfram code symbols (Input cells) use these too —
    // \[Lambda] is valid runnable Wolfram. We have two modes:
    //   - Input/code cells: keep \[Lambda] verbatim (Wolfram understands it)
    //   - Text/markdown/Section cells: decode to Unicode for human reading
    // The caller passes a ``decodeNamed`` flag to choose.
    const NAMED_UNICODE = {
        // German umlauts and ß
        'ADoubleDot': '\u00E4',  // ä
        'ODoubleDot': '\u00F6',  // ö
        'UDoubleDot': '\u00FC',  // ü
        'CapitalADoubleDot': '\u00C4',  // Ä
        'CapitalODoubleDot': '\u00D6',  // Ö
        'CapitalUDoubleDot': '\u00DC',  // Ü
        'SZ': '\u00DF',  // ß
        // Greek letters (lowercase)
        'Alpha': '\u03B1', 'Beta': '\u03B2', 'Gamma': '\u03B3', 'Delta': '\u03B4',
        'Epsilon': '\u03B5', 'Zeta': '\u03B6', 'Eta': '\u03B7', 'Theta': '\u03B8',
        'Iota': '\u03B9', 'Kappa': '\u03BA', 'Lambda': '\u03BB', 'Mu': '\u03BC',
        'Nu': '\u03BD', 'Xi': '\u03BE', 'Omicron': '\u03BF', 'Pi': '\u03C0',
        'Rho': '\u03C1', 'Sigma': '\u03C3', 'Tau': '\u03C4', 'Upsilon': '\u03C5',
        'Phi': '\u03C6', 'Chi': '\u03C7', 'Psi': '\u03C8', 'Omega': '\u03C9',
        // Greek letters (uppercase)
        'CapitalAlpha': '\u0391', 'CapitalBeta': '\u0392', 'CapitalGamma': '\u0393',
        'CapitalDelta': '\u0394', 'CapitalEpsilon': '\u0395', 'CapitalZeta': '\u0396',
        'CapitalEta': '\u0397', 'CapitalTheta': '\u0398', 'CapitalIota': '\u0399',
        'CapitalKappa': '\u039A', 'CapitalLambda': '\u039B', 'CapitalMu': '\u039C',
        'CapitalNu': '\u039D', 'CapitalXi': '\u039E', 'CapitalOmicron': '\u039F',
        'CapitalPi': '\u03A0', 'CapitalRho': '\u03A1', 'CapitalSigma': '\u03A3',
        'CapitalTau': '\u03A4', 'CapitalUpsilon': '\u03A5', 'CapitalPhi': '\u03A6',
        'CapitalChi': '\u03A7', 'CapitalPsi': '\u03A8', 'CapitalOmega': '\u03A9',
        // Math symbols
        'Rule': '\u2192', 'RuleDelayed': '\u2192',
        'Equal': '=', 'NotEqual': '\u2260', 'LessEqual': '\u2264',
        'GreaterEqual': '\u2265', 'Less': '<', 'Greater': '>',
        'Element': '\u2208', 'NotElement': '\u2209',
        'Union': '\u222A', 'Intersection': '\u2229',
        'Inequality': '\u2260', 'TildeTilde': '\u2248',
        'PlusMinus': '\u00B1', 'Times': '\u00D7', 'Divide': '\u00F7',
        'InvisibleComma': '', 'InvisibleSpace': '',
        'Integral': '\u222B', 'DifferentialD': 'd',
        'Sum': '\u2211', 'Product': '\u220F',
        'PartialD': '\u2202', 'Del': '\u2207', 'SquareRoot': '\u221A',
        'Infinity': '\u221E', 'EmptySet': '\u2205',
        'ForAll': '\u2200', 'Exists': '\u2203', 'NotExists': '\u2204',
        'And': '\u2227', 'Or': '\u2228', 'Not': '\u00AC',
        'Implies': '\u21D2', 'Equivalent': '\u21D4',
        'Subset': '\u2282', 'Superset': '\u2283',
        'ApproximatelyEqual': '\u2245', 'Proportional': '\u221D',
        'Degree': '\u00B0', 'Cent': '\u00A2', 'Pound': '\u00A3',
        'Euro': '\u20AC', 'Yen': '\u00A5',
        'Copyright': '\u00A9', 'RegisteredTrademark': '\u00AE',
        // Escapes
        'AliasDelimiter': '',
        // Wolfram FullForm string delimiters \< ... \> — stripped to inner text
    };

    // Decode a parsed Wolfram string value: first strip \< \> delimiters,
    // then replace \[Name] sequences with Unicode where available,
    // optionally keeping verbatim for code cells.
    function _decodeStringValue(raw, decodeToUnicode) {
        // Strip Wolgraf FullForm string delimiters: \<content\> -> content
        var s = raw;
        // \<...</> wrapping
        s = s.replace(/\\</g, '').replace(/\\>/g, '');
        if (!decodeToUnicode) return s;
        // Decode \[Name] to Unicode for display in text/markdown cells
        s = s.replace(/\\\[([A-Za-z][A-Za-z0-9]*)\]/g, function(m, name) {
            if (Object.prototype.hasOwnProperty.call(NAMED_WHITESPACE, name)) {
                return NAMED_WHITESPACE[name];
            }
            if (Object.prototype.hasOwnProperty.call(NAMED_UNICODE, name)) {
                return NAMED_UNICODE[name];
            }
            return m; // unknown — keep verbatim
        });
        return s;
    }

    // Parse "..." string at idx (idx points at opening quote). Returns
    // { value, end } where value has Wolfram escape sequences decoded;
    // whitespace named-chars (\[IndentingNewLine] etc.) become real whitespace
    // and other \[Name] sequences (e.g. \[Lambda]) are preserved verbatim so
    // the Wolfram code stays runnable.
    function _parseString(src, idx) {
        let j = idx + 1;
        let out = '';
        while (j < src.length) {
            const ch = src[j];
            if (ch === '\\') {
                const nx = src[j + 1];
                if (nx === '[') {
                    const end = src.indexOf(']', j + 2);
                    if (end !== -1) {
                        const name = src.slice(j + 2, end);
                        if (Object.prototype.hasOwnProperty.call(NAMED_WHITESPACE, name)) {
                            out += NAMED_WHITESPACE[name];
                        } else {
                            out += src.slice(j, end + 1); // \[Lambda] etc. — keep verbatim
                        }
                        j = end + 1;
                        continue;
                    }
                    out += ch; j++; continue;
                }
                // Strip Wolgraf FullForm \< \> string delimiters in raw strings
                if (nx === '<') { j += 2; continue; }
                if (nx === '>') { j += 2; continue; }
                if (nx === 'n') { out += '\n'; j += 2; continue; }
                if (nx === 't') { out += '\t'; j += 2; continue; }
                if (nx === 'r') { out += '\r'; j += 2; continue; }
                if (nx === '\\') { out += '\\'; j += 2; continue; }
                if (nx === '"') { out += '"'; j += 2; continue; }
                if (nx === "'") { out += "'"; j += 2; continue; }
                if (nx === '0') { out += '\0'; j += 2; continue; }
                if (nx === '.') { out += '.'; j += 2; continue; }
                if (nx === ':') {
                    const hex = src.substr(j + 2, 4);
                    if (/^[0-9A-Fa-f]{1,4}$/.test(hex)) {
                        try { out += String.fromCodePoint(parseInt(hex, 16)); }
                        catch (e) { out += src.slice(j, j + 6); }
                        j += 2 + hex.length; continue;
                    }
                }
                out += ch; j++; continue;
            }
            if (ch === '"') { j++; return { value: out, end: j }; }
            out += ch; j++;
        }
        return { value: out, end: j };
    }

    // Flatten a *Box expression subtree (raw substring from..to) to readable
    // Wolfram source. RowBox[{"a","+","b"}] -> "a + b".
    function _flattenBox(src, from, to) {
        let i = from;
        let out = '';
        while (i < to) {
            const ch = src[i];
            if (_isWs(ch)) { i++; continue; }
            if (ch === '"') {
                const s = _parseString(src, i);
                out += s.value;
                i = s.end;
                continue;
            }
            if (ch === '(' && src[i + 1] === '*') {
                const end = src.indexOf('*)', i + 2);
                i = end === -1 ? to : end + 2;
                continue;
            }
            const sym = _matchSymbol(src, i);
            if (sym && src[i + sym.length] === '[') {
                const openIdx = i + sym.length;
                const closeIdx = _matchBracket(src, openIdx);
                if (closeIdx === -1) { out += sym; i = to; break; }
                const innerFrom = openIdx + 1, innerTo = closeIdx;
                out += _flattenHead(src, sym, innerFrom, innerTo);
                i = closeIdx + 1;
                continue;
            }
            // Bare { ... } list — RowBox children are held in a brace list. Children
            // are a TOKEN STREAM and must be CONCATENATED (the original
            // cosmetic spaces live inside the string tokens themselves), so
            // joining with a space would turn ``swing[m0_, n0_]`` into the
            // un-runnable ``swing [ m0_ , n0_ ]``. Use an empty separator.
            if (ch === '{') {
                const closeIdx = _matchBracket(src, i);
                if (closeIdx === -1) { out += ch; i++; continue; }
                out += _flattenList(src, i + 1, closeIdx, '');
                i = closeIdx + 1;
                continue;
            }
            // Bare ( ... ) grouping — flatten inner.
            if (ch === '(') {
                const closeIdx = _matchBracket(src, i);
                if (closeIdx === -1) { out += ch; i++; continue; }
                out += _flattenBox(src, i + 1, closeIdx);
                i = closeIdx + 1;
                continue;
            }
            // Bare atom (number, operator, greek \[Name], punctuation).
            out += ch; i++;
        }
        return out;
    }

    // Only the OUTERMOST RowBox in a cell's BoxData should auto-close
    // missing brackets. Nested RowBoxes inside StyleBox/TagBox/etc. are
    // display markup (e.g. a literal "[" character in text) and must NOT
    // get auto-closed. This flag tracks whether we're at the top level.
    var _rowBoxTopLevel = false;

    // Dispatch a known *Box head to the right flattening strategy.
    //
    // RowBox children are a TOKEN STREAM (Mathematica stores each literal
    // token as its own string, e.g. RowBox[{"swing","[",RowBox[...],"]"}]).
    // Joining with a space would turn ``swing[m0_, n0_]`` into the unusable
    // ``swing [ m0_ , n0_ ]``. The original cosmetic spaces live INSIDE the
    // string tokens, so children are CONCATENATED (empty separator) to
    // faithfully reconstruct the runnable Wolfram source.
    function _flattenHead(src, head, from, to) {
        if (head === 'RowBox' || head === 'GraphicsBox' ||
            head === 'Column' || head === 'Row' || head === 'SequenceBox' ||
            head === 'LineBox' || head === 'HighlightActionBox') {
            // Capture whether we're the outermost RowBox (direct from BoxData).
            var allowAutoClose = _rowBoxTopLevel;
            // Disable immediately BEFORE flattening children so nested
            // RowBoxes (inside StyleBox/TagBox/etc.) do NOT auto-close.
            _rowBoxTopLevel = false;
            // Flatten children with empty separator.
            var result = _flattenList(src, from, to, '');
            // Auto-close missing brackets ONLY at the top-level RowBox.
            if (allowAutoClose && head === 'RowBox') {
                var braceStart = _skipWs(src, from);
                if (src[braceStart] === '{') {
                    var braceEnd = _matchBracket(src, braceStart);
                    if (braceEnd !== -1) {
                        var kids = _splitArgs(src, braceStart + 1, braceEnd);
                        var opens = 0, closes = 0;
                        for (var ki = 0; ki < kids.length; ki++) {
                            var ks = _skipWs(src, kids[ki][0]);
                            if (ks < kids[ki][1] && src[ks] === '"') {
                                var kv = _parseString(src, ks);
                                if (kv.value.length === 1) {
                                    if ('[({'.indexOf(kv.value) !== -1) opens++;
                                    else if ('])}'.indexOf(kv.value) !== -1) closes++;
                                }
                            }
                        }
                        var gap = opens - closes;
                        if (gap > 0 && gap <= 2) {
                            for (var gi = 0; gi < gap; gi++) result += ']';
                        }
                    }
                }
            }
            return result;
        }
        // GridBox rows are separated; keep a space so matrices stay readable.
        if (head === 'GridBox') {
            return _flattenList(src, from, to, ' ');
        }
        if (head === 'SuperscriptBox') return _flattenBinary(src, from, to, '^');
        if (head === 'SubscriptBox')  return _flattenBinary(src, from, to, '_');
        if (head === 'SubsuperscriptBox') return _flattenBinary(src, from, to, '__');
        if (head === 'FractionBox')   return _flattenBinary(src, from, to, '/');
        if (head === 'SqrtBox' || head === 'RadicalBox') {
            return 'Sqrt[' + _flattenBox(src, from, to) + ']';
        }
        if (head === 'OverscriptBox' || head === 'UnderscriptBox' ||
            head === 'UnderoverscriptBox' || head === 'OverBarBox' ||
            head === 'UnderBarBox' || head === 'FrameBox' || head === 'CellFrameBox') {
            return _flattenBox(src, from, to);
        }
        // Embedded data blobs (compressed plot coords, bitmaps, raw arrays) —
        // these can be 100KB+ of base64. Skip the blob and return a short
        // placeholder so the source stays readable instead of flooding with
        // binary garbage.
        if (head === 'CompressedData' || head === 'Bitmap' || head === 'RawArray' ||
            head === 'ByteArray' || head === 'NumericArray' || head === 'SparseArray') {
            return '<' + head + '>';
        }
        // Dynamic/interactive content — the inner Typeset setup is 50KB+
        // of DynamicModule variable initialisation, not useful as text.
        // Return a short placeholder just like Graphics.
        if (head === 'DynamicBox' || head === 'DynamicModuleBox' ||
            head === 'ManipulateBox' || head === 'PaneSelectorBox') {
            return '-Dynamic-';
        }
        // Styling / semantic wrappers — show inner content.
        // CRITICAL: disable auto-close flag before entering display wrappers
        // so any RowBox inside (e.g. StyleBox[RowBox[{" ","["}]]) does NOT
        // get its brackets auto-closed — those are literal text characters.
        if (head === 'StyleBox' || head === 'FormBox' || head === 'TagBox' ||
            head === 'InterpretationBox' || head === 'TooltipBox' ||
            head === 'ButtonBox' ||
            head === 'InterpretationBox' || head === 'AdjustmentBox' ||
            head === 'TextData' || head === 'BoxData' || head === 'InlineRules') {
            _rowBoxTopLevel = false;
            return _flattenBox(src, from, to);
        }
        // Any other *Box — best effort: flatten inner.
        if (/Box$/.test(head)) return _flattenBox(src, from, to);
        // Unknown structural head: keep as a readable expression so the user
        // still sees structure instead of an empty cell.
        return head + '[' + _flattenBox(src, from, to) + ']';
    }

    // Flatten a comma-separated list from..to, joining items with ``sep``.
    function _flattenList(src, from, to, sep) {
        const parts = [];
        let i = from, depth = 0, inS = false, inC = false, start = from;
        while (i < to) {
            const ch = src[i], nx = src[i + 1];
            if (inC) { if (ch === '*' && nx === ')') { inC = false; i++; } i++; continue; }
            if (inS) { if (ch === '\\') { i += 2; continue; } if (ch === '"') inS = false; i++; continue; }
            if (ch === '(' && nx === '*') { inC = true; i += 2; continue; }
            if (ch === '"') { inS = true; i++; continue; }
            if (ch === '[' || ch === '{' || ch === '(') { depth++; i++; continue; }
            if (ch === ']' || ch === '}' || ch === ')') { depth--; i++; continue; }
            if (depth === 0 && ch === ',') { parts.push([start, i]); i++; start = i; continue; }
            i++;
        }
        if (start < to) parts.push([start, to]);
        return parts.map(p => _flattenBox(src, p[0], p[1])).join(sep);
    }

    // Flatten Binary[a,b] -> a<op>b (SuperscriptBox, SubscriptBox, FractionBox).
    function _flattenBinary(src, from, to, op) {
        const parts = [];
        let i = from, depth = 0, inS = false, inC = false, start = from;
        while (i < to) {
            const ch = src[i], nx = src[i + 1];
            if (inC) { if (ch === '*' && nx === ')') { inC = false; i++; } i++; continue; }
            if (inS) { if (ch === '\\') { i += 2; continue; } if (ch === '"') inS = false; i++; continue; }
            if (ch === '(' && nx === '*') { inC = true; i += 2; continue; }
            if (ch === '"') { inS = true; i++; continue; }
            if (ch === '[' || ch === '{' || ch === '(') { depth++; i++; continue; }
            if (ch === ']' || ch === '}' || ch === ')') { depth--; i++; continue; }
            if (depth === 0 && ch === ',') { parts.push([start, i]); i++; start = i; continue; }
            i++;
        }
        if (start < to) parts.push([start, to]);
        if (parts.length === 2) {
            return _flattenBox(src, parts[0][0], parts[0][1]) + op +
                   _flattenBox(src, parts[1][0], parts[1][1]);
        }
        return parts.map(p => _flattenBox(src, p[0], p[1])).join(op);
    }

    // Split a substring into top-level comma-separated arg spans [start,end].
    function _splitArgs(src, from, to) {
        const parts = [];
        let i = from, depth = 0, inS = false, inC = false, start = from;
        while (i < to) {
            const ch = src[i], nx = src[i + 1];
            if (inC) { if (ch === '*' && nx === ')') { inC = false; i++; } i++; continue; }
            if (inS) { if (ch === '\\') { i += 2; continue; } if (ch === '"') inS = false; i++; continue; }
            if (ch === '(' && nx === '*') { inC = true; i += 2; continue; }
            if (ch === '"') { inS = true; i++; continue; }
            if (ch === '[' || ch === '{' || ch === '(') { depth++; i++; continue; }
            if (ch === ']' || ch === '}' || ch === ')') { depth--; i++; continue; }
            if (depth === 0 && ch === ',') { parts.push([start, i]); i++; start = i; continue; }
            i++;
        }
        if (start < to) parts.push([start, to]);
        return parts;
    }

    // Parse a single Cell[...args...] -> push { style, source, rawStart, rawEnd }.
    // Returns the index past the matching ']' (or -1 on unbalanced).
    function _parseOneCell(src, from, out) {
        const closeIdx = _matchBracket(src, from);
        if (closeIdx === -1) return -1;
        const innerFrom = from + 1, innerTo = closeIdx;
        const args = _splitArgs(src, innerFrom, innerTo);

        // A Cell may wrap a CellGroupData[{...}] (Mathematica groups cells
        // sharing an evaluation timeline). Recurse into the group and emit
        // each nested Cell individually instead of rendering the group raw.
        if (args.length > 0) {
            const firstStart = _skipWs(src, args[0][0]);
            const sym = _matchSymbol(src, firstStart);
            if (sym === 'CellGroupData' && src[firstStart + sym.length] === '[') {
                const gOpen = firstStart + sym.length;
                const gClose = _matchBracket(src, gOpen);
                if (gClose !== -1) {
                    const gArgs = _splitArgs(src, gOpen + 1, gClose);
                    if (gArgs.length > 0) {
                        const listFrom = _skipWs(src, gArgs[0][0]);
                        if (src[listFrom] === '{') {
                            const listClose = _matchBracket(src, listFrom);
                            _collectCells(src, listFrom + 1,
                                listClose === -1 ? gClose : listClose, out);
                        } else {
                            _collectCells(src, gArgs[0][0], gArgs[0][1], out);
                        }
                    }
                    return closeIdx + 1;
                }
            }
        }

        let style = null;
        let boxText = null;
        let firstStringArg = null;
        for (let i = 0; i < args.length; i++) {
            const [af, at] = args[i];
            const s = _skipWs(src, af);
            if (s >= at) continue;
            const ch = src[s];
            if (ch === '"') {
                const parsed = _parseString(src, s);
                const v = parsed.value.trim();
                if (CELL_STYLES.has(v) && !style) style = v;
                else if (i === 0) firstStringArg = parsed.value;
                continue;
            }
            const sym = _matchSymbol(src, s);
            if (sym) {
                if (CELL_STYLES.has(sym) && !style) style = sym;
                if (src[s + sym.length] === '[' && (sym === 'BoxData' || sym === 'TextData')) {
                    const bOpen = s + sym.length;
                    const bClose = _matchBracket(src, bOpen);
                    if (bClose !== -1) {
                        // Enable RowBox auto-close only for the outermost
                        // RowBox under BoxData (the cell's main expression).
                        _rowBoxTopLevel = true;
                        boxText = _flattenBox(src, bOpen + 1, bClose);
                        _rowBoxTopLevel = false;
                    }
                }
            }
        }

        // Text/display cells (Section, Subsection, Text, Title, etc.) should
        // have their \[Name] sequences decoded to proper Unicode so the user
        // sees "Übersicht" instead of "\[UDoubleDot]bersicht". Input/code
        // cells keep \[Lambda] verbatim since Wolfram needs it runnable.
        var TEXT_STYLES = { 'Text': 1, 'Title': 1, 'Subtitle': 1, 'Author': 1,
            'Section': 1, 'Subsection': 1, 'Subsubsection': 1, 'Chapter': 1,
            'Subchapter': 1, 'Program': 1, 'DisplayFormula': 1,
            'ItemNumbered': 1, 'Item': 1, 'SubitemNumbered': 1,
            'Subitem': 1, 'Subsubitem': 1, 'Notebook': 1 };
        var decodeText = !!TEXT_STYLES[style];
        if (decodeText && firstStringArg != null) {
            firstStringArg = _decodeStringValue(firstStringArg, true);
        }
        if (decodeText && boxText != null) {
            boxText = _decodeStringValue(boxText, true);
        }

        if (!style) style = 'Input';
        let source = boxText != null ? boxText
            : (firstStringArg != null ? firstStringArg
               : (decodeText ? _decodeStringValue(src.slice(innerFrom, innerTo).trim(), true)
                  : src.slice(innerFrom, innerTo).trim()));
        out.push({ style: style, source: source, rawStart: from, rawEnd: closeIdx });
        return closeIdx + 1;
    }

    // Recursively walk a Notebook body span (from..to) collecting Cells,
    // expanding Cell[CellGroupData[{...}]] wrappers along the way.
    function _collectCells(src, from, to, out) {
        let i = from;
        let inS = false, inC = false;
        while (i < to) {
            const ch = src[i], nx = src[i + 1];
            if (inC) { if (ch === '*' && nx === ')') { inC = false; i++; } i++; continue; }
            if (inS) { if (ch === '\\') { i += 2; continue; } if (ch === '"') inS = false; i++; continue; }
            if (ch === '(' && nx === '*') { inC = true; i += 2; continue; }
            if (ch === '"') { inS = true; i++; continue; }
            if (/[A-Za-z]/.test(ch)) {
                const sym = _matchSymbol(src, i);
                if (sym && sym === 'Cell' && src[i + sym.length] === '[') {
                    const after = _parseOneCell(src, i + sym.length, out);
                    if (after === -1) return;
                    i = after; continue;
                }
                if (sym && sym === 'CellGroupData' && src[i + sym.length] === '[') {
                    const cOpen = i + sym.length;
                    const cClose = _matchBracket(src, cOpen);
                    if (cClose === -1) return;
                    const cArgs = _splitArgs(src, cOpen + 1, cClose);
                    if (cArgs.length > 0) {
                        const listFrom = _skipWs(src, cArgs[0][0]);
                        if (src[listFrom] === '{') {
                            const listClose = _matchBracket(src, listFrom);
                            _collectCells(src, listFrom + 1,
                                listClose === -1 ? cClose : listClose, out);
                        } else {
                            _collectCells(src, cArgs[0][0], cArgs[0][1], out);
                        }
                    }
                    i = cClose + 1; continue;
                }
                i += sym.length || 1; continue;
            }
            i++;
        }
    }

    function _isContinuationFragment(source) {
        const s = (source || '').trim();
        if (!s) return true;
        return s === ';' || s === ',';
    }
    /**
     * Parse a .nb file's source text into a list of cells.
     * Returns: {
     *   cells: [ { index, style: 'Input'|'Output'|'Text'|..., source: string } ],
     *   droppedFragments: number
     * }
     */
    function parseNotebook(src) {
        const raw = [];
        let droppedFragments = 0;
        const m = src.match(/Notebook\s*\[/);
        if (!m) {
            return {
                cells: [{ index: 0, style: 'Input', source: src.trim() }],
                droppedFragments: 0
            };
        }
        const nbOpen = m.index + m[0].length - 1;
        const nbClose = _matchBracket(src, nbOpen);
        const nbEnd = nbClose === -1 ? src.length : nbClose;
        const args = _splitArgs(src, nbOpen + 1, nbEnd);
        if (!args.length) return { cells: [], droppedFragments: 0 };
        const bodyFrom = _skipWs(src, args[0][0]);
        let bodyInnerFrom, bodyInnerTo;
        if (src[bodyFrom] === '{') {
            const bEnd = _matchBracket(src, bodyFrom);
            bodyInnerFrom = bodyFrom + 1;
            bodyInnerTo = bEnd === -1 ? nbEnd : bEnd;
        } else {
            bodyInnerFrom = args[0][0];
            bodyInnerTo = args[0][1];
        }
        _collectCells(src, bodyInnerFrom, bodyInnerTo, raw);
        const cells = [];
        let idx = 0;
        // Output/Print cells can be 100KB+ of dynamic data (FittedModel,
        // DataDistribution, ANOVA tables, etc.). They're regenerated when
        // the user re-runs the corresponding Input cell, so truncate them
        // aggressively to keep the notebook readable and import fast.
        const MAX_OUTPUT_CHARS = 5000;
        for (const r of raw) {
            const s = r.source || '';
            if (!s.trim()) continue;
            if (_isContinuationFragment(s)) { droppedFragments++; continue; }

            // Apply per-style truncation: Output/Print cells get a tight
            // cap (they're auto-generated dumps); Input cells keep the
            // full MAX_CELL_SOURCE_CHARS budget (they're the actual code).
            var maxLen = (r.style === 'Output' || r.style === 'Print' ||
                          r.style === 'Message')
                ? MAX_OUTPUT_CHARS
                : MAX_CELL_SOURCE_CHARS;
            var truncated = s.length > maxLen
                ? s.slice(0, maxLen) + '\n(* ... truncated ... *)'
                : s;
            cells.push({ index: idx++, style: r.style, source: truncated });
            if (cells.length >= MAX_CELLS) break;
        }
        return { cells: cells, droppedFragments: droppedFragments };
    }

    /**
     * Render cells into the preview sidebar body.
     * `meta` is the optional second return value of parseNotebook (carries
     * information about hidden continuation fragments).
     */
    function renderCells(container, cells, meta) {
        container.innerHTML = '';
        meta = meta || {};
        if (!cells.length) {
            container.innerHTML = '<div style="padding:24px;color:var(--text-tertiary);font-size:13px;">No input cells detected in this file.</div>';
            return;
        }
        // Toolbar
        const toolbar = document.createElement('div');
        toolbar.className = 'wolfram-nb-toolbar';
        const runAllBtn = document.createElement('button');
        runAllBtn.className = 'btn btn-primary btn-sm';
        runAllBtn.textContent = cells.length > 1 ? `Run All (${cells.length} cells)` : 'Run';
        runAllBtn.dataset.action = 'run-all';
        toolbar.appendChild(runAllBtn);

        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn btn-secondary btn-sm';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.style.marginLeft = '8px';
        cancelBtn.disabled = true;
        cancelBtn.dataset.action = 'cancel';
        toolbar.appendChild(cancelBtn);

        const summary = document.createElement('span');
        summary.className = 'wolfram-nb-summary';
        const inputCount = cells.filter(c => c.style === 'Input').length;
        let summaryText = `${cells.length} cell${cells.length === 1 ? '' : 's'} • ${inputCount} input`;
        if (meta.droppedFragments) {
            summaryText += ` • ${meta.droppedFragments} hidden fragment${meta.droppedFragments === 1 ? '' : 's'}`;
        }
        summary.textContent = summaryText;
        summary.style.marginLeft = '12px';
        summary.style.fontSize = '12px';
        summary.style.color = 'var(--text-tertiary)';
        toolbar.appendChild(summary);

        container.appendChild(toolbar);

        // Cells
        cells.forEach((cell) => {
            const cellEl = document.createElement('div');
            cellEl.className = 'wolfram-nb-cell';
            cellEl.dataset.cellIndex = String(cell.index);

            const head = document.createElement('div');
            head.className = 'wolfram-nb-cell-head';
            const tag = document.createElement('span');
            tag.className = 'wolfram-nb-cell-tag';
            tag.textContent = `[${cell.index + 1}] ${cell.style}`;
            head.appendChild(tag);

            const actions = document.createElement('span');
            actions.className = 'wolfram-nb-cell-actions';

            const copyBtn = document.createElement('button');
            copyBtn.className = 'wolfram-nb-icon-btn';
            copyBtn.type = 'button';
            copyBtn.title = 'Copy source';
            copyBtn.textContent = '⧉';
            copyBtn.dataset.action = 'copy-cell';
            actions.appendChild(copyBtn);

            const rawBtn = document.createElement('button');
            rawBtn.className = 'wolfram-nb-icon-btn';
            rawBtn.type = 'button';
            rawBtn.title = 'Toggle raw / decoded view';
            rawBtn.textContent = '⌥';
            rawBtn.dataset.action = 'toggle-raw';
            actions.appendChild(rawBtn);

            const runBtn = document.createElement('button');
            runBtn.className = 'btn btn-secondary btn-sm wolfram-nb-run-btn';
            runBtn.textContent = 'Run';
            runBtn.dataset.action = 'run-cell';
            actions.appendChild(runBtn);

            head.appendChild(actions);
            cellEl.appendChild(head);

            // Scroll container so very long sources don't overflow the split panel
            const scroll = document.createElement('div');
            scroll.className = 'wolfram-nb-cell-code-scroll';
            const code = document.createElement('pre');
            code.className = 'wolfram-nb-cell-code';
            code.dataset.decoded = cell.source;
            code.dataset.raw = ''; // populated lazily on toggle-raw
            code.textContent = cell.source;
            scroll.appendChild(code);
            cellEl.appendChild(scroll);

            const out = document.createElement('div');
            out.className = 'wolfram-nb-cell-output';
            cellEl.appendChild(out);

            container.appendChild(cellEl);

            // After mounting, try to syntax-highlight the source. We do this
            // lazily so very large notebooks stay responsive — the highlighter
            // is fast but each <pre> rebuild loses selection if run eagerly.
            _maybeHighlight(code, cell.source);


        });
    }

    /**
     * POST a single cell's source to /api/notebooks/wolfram/evaluate and
     * render the result into the cell's output area.
     */
    async function executeCell(cellEl, source) {
        const out = cellEl.querySelector('.wolfram-nb-cell-output');
        const runBtn = cellEl.querySelector('[data-action="run-cell"]');
        const UI = window.PhonosWolframUI;
        if (out) {
            out.innerHTML =
                '<div class="wolfram-nb-spinner">Running…</div>';
        }
        if (runBtn) runBtn.disabled = true;
        try {
            const resp = await fetch('/api/notebooks/wolfram/evaluate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: JSON.stringify({ code: source, timeout: 60 }),
            });
            const data = await resp.json().catch(() => ({ success: false, error: 'Invalid JSON response' }));
            if (data && !data.success && data.error) {
                // Pre-parse the error so the panel can categorise it without
                // doing the same regex work twice.
                if (UI && UI.parseWolframError && !data._parsed_error) {
                    const parsed = UI.parseWolframError(data.error);
                    const code = (data.error_code && UI.classifyErrorCode(data.error_code)) || parsed.category;
                    data._parsed_error = Object.assign({}, parsed, { category: code });
                }
            }
            renderCellOutput(out, data, resp.status, { source: source });
        } catch (e) {
            if (out) {
                if (UI && UI.renderError) {
                    const err = UI.renderError({
                        parsed: UI.parseWolframError('Network error: ' + String(e)),
                        categoryOverride: UI.classifyErrorCode('unreachable'),
                        code: source
                    });
                    out.innerHTML = '';
                    out.appendChild(err);
                } else {
                    out.innerHTML = `<div class="wolfram-nb-err">Network error: ${escapeHtml(String(e))}</div>`;
                }
            }
        } finally {
            if (runBtn) runBtn.disabled = false;
        }
    }

    function renderCellOutput(outEl, data, httpStatus, ctx) {
        if (!outEl) return;
        const UI = window.PhonosWolframUI;
        const esc = (UI && UI.escapeHtml) || escapeHtml;
        const parsed = data && data._parsed_error;

        if (data.error_code === 'missing_credentials') {
            if (UI && UI.renderError) {
                const err = UI.renderError({
                    parsed: {
                        category: 'auth',
                        tag: 'Auth',
                        head: 'No Wolfram Cloud account',
                        body: 'Open Notebook IDE Settings → Wolfram Account to add one. Admins use the server\u2019s global account.',
                        hint: 'Non-admin accounts need their own Wolfram Cloud credentials to run code.'
                    },
                    categoryOverride: 'auth',
                    code: (ctx && ctx.source) || ''
                });
                outEl.innerHTML = '';
                outEl.appendChild(err);
            } else {
                outEl.innerHTML = `<div class="wolfram-nb-err"><strong>No Wolfram Cloud account.</strong> Open Notebook IDE Settings → Wolfram Account to add one. Admins use the server's global account.</div>`;
            }
            return;
        }
        if (!data.success) {
            const raw = data.error || ('HTTP ' + httpStatus);
            if (UI && UI.renderError) {
                const category = UI.classifyErrorCode(data.error_code);
                const err = UI.renderError({
                    parsed: parsed || UI.parseWolframError(raw),
                    categoryOverride: category,
                    code: (ctx && ctx.source) || '',
                    rawText: raw
                });
                outEl.innerHTML = '';
                outEl.appendChild(err);
            } else {
                outEl.innerHTML = `<div class="wolfram-nb-err"><strong>Error:</strong> ${esc(raw)}</div>`;
            }
            return;
        }
        const parts = [];
        const text = (data.text || '').trim();
        if (text) {
            parts.push(`<pre class="wolfram-nb-out-text">${esc(text)}</pre>`);
        }
        if (data.image) {
            parts.push(`<div class="wolfram-nb-out-image"><img alt="Wolfram output" src="data:${data.image_type || 'image/png'};base64,${data.image}"></div>`);
        }
        if (!parts.length) {
            parts.push('<div class="wolfram-nb-out-empty">(no output)</div>');
        }
        const auth = data._auth_source ? `<span class="wolfram-nb-auth" title="Authenticated via ${esc(data._auth_source)}">via ${esc(data._auth_source)}</span>` : '';
        const dur = data._timings_ms ? `<span class="wolfram-nb-dur">${data._timings_ms} ms</span>` : '';
        outEl.innerHTML = parts.join('') + `<div class="wolfram-nb-meta">${dur} ${auth}</div>`;
    }

    function _getCellSource(cellEl) {
        if (!cellEl) return '';
        const code = cellEl.querySelector('.wolfram-nb-cell-code');
        if (!code) return '';
        // Prefer the decoded view (decoded) over raw escape sequences.
        if (code.dataset && code.dataset.decoded) return code.dataset.decoded;
        return code.textContent || '';
    }

    /**
     * Public entry: open a Wolfram notebook preview in the file sidebar.
     * Expects a container element (e.g. the sidebar body) and the file content.
     */
    async function openInSidebar(sidebarBody, content, ext) {
        const parsed = parseNotebook(content);
        const cells = parsed.cells || parsed;
        const meta = parsed.droppedFragments != null ? { droppedFragments: parsed.droppedFragments } : {};
        const wrap = document.createElement('div');
        wrap.className = 'wolfram-nb-wrap';
        wrap.dataset.ext = ext || 'nb';
        renderCells(wrap, cells, meta);
        sidebarBody.innerHTML = '';
        sidebarBody.appendChild(wrap);

        const state = { cancelRequested: false };
        wrap._state = state;

        wrap.addEventListener('click', async (ev) => {
            const target = ev.target.closest('[data-action]');
            if (!target) return;
            const action = target.dataset.action;
            if (action === 'run-cell') {
                const cellEl = target.closest('.wolfram-nb-cell');
                if (!cellEl) return;
                const idx = parseInt(cellEl.dataset.cellIndex, 10);
                const cell = cells.find(c => c.index === idx);
                if (cell) await executeCell(cellEl, cell.source);
            } else if (action === 'run-all') {
                const cancelBtn = wrap.querySelector('[data-action="cancel"]');
                const runAllBtn = target;
                runAllBtn.disabled = true;
                if (cancelBtn) cancelBtn.disabled = false;
                state.cancelRequested = false;
                const inputs = cells.filter(c => c.style === 'Input');
                for (const c of inputs) {
                    if (state.cancelRequested) break;
                    const cellEl = wrap.querySelector(`.wolfram-nb-cell[data-cell-index="${c.index}"]`);
                    if (!cellEl) continue;
                    cellEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    await executeCell(cellEl, c.source);
                }
                runAllBtn.disabled = false;
                if (cancelBtn) cancelBtn.disabled = true;
            } else if (action === 'cancel') {
                state.cancelRequested = true;
            } else if (action === 'copy-cell') {
                const cellEl = target.closest('.wolfram-nb-cell');
                if (!cellEl) return;
                const idx = parseInt(cellEl.dataset.cellIndex, 10);
                const cell = cells.find(c => c.index === idx);
                if (!cell) return;
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(cell.source);
                    } else {
                        const ta = document.createElement('textarea');
                        ta.value = cell.source;
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        document.body.removeChild(ta);
                    }
                    target.textContent = '✓';
                    setTimeout(function () { target.textContent = '⧉'; }, 1200);
                } catch (e) {
                    console.warn('Copy failed:', e);
                }
            } else if (action === 'toggle-raw') {
                const cellEl = target.closest('.wolfram-nb-cell');
                if (!cellEl) return;
                const code = cellEl.querySelector('.wolfram-nb-cell-code');
                if (!code) return;
                if (code.dataset.showingRaw === '1') {
                    code.textContent = code.dataset.decoded || '';
                    code.dataset.showingRaw = '0';
                    target.title = 'Toggle raw / decoded view';
                } else {
                    if (!code.dataset.raw) {
                        // Re-encode the visible decoded text back to literal Wolfram
                        // escape sequences so the user can see the raw source.
                        const src = code.dataset.decoded || '';
                        code.dataset.raw = src
                            .replace(/\\/g, '\\\\')
                            .replace(/\t/g, '\\t')
                            .replace(/\n/g, '\\n')
                            .replace(/\r/g, '\\r')
                            .replace(/\0/g, '\\0');
                    }
                    code.textContent = code.dataset.raw;
                    code.dataset.showingRaw = '1';
                    target.title = 'Toggle decoded / raw view';
                }
            }
        });
        return cells;
    }

    window.PhonosWolframNb = {
        parseNotebook: parseNotebook,
        openInSidebar: openInSidebar,
        renderCellOutput: renderCellOutput,
        executeCell: executeCell,
        highlightSource: _maybeHighlight
    };
})();
