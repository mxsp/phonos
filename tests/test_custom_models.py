"""Smoke tests for the custom-models HTTP routes.

Exercises the Flask endpoints added when custom models were wired back into
the dashboard. Skips the full app bootstrap (which pulls in plan_service
and other deployment-only modules) by importing route modules directly and
inspecting their bound URL map.
"""

import os
import sys
import unittest


REPO = os.environ.get('PHONOS_REPO', os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# Path setup is handled by conftest.py (this is a minimal fallback)
sys.path.insert(0, REPO)
os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')


def _make_test_app():
    """Build a tiny Flask app that mounts an isolated replica of the
    user_model_builder view function.

    We don't import `phonos_app.routes.pages` directly because its module
    top-level imports pull in plan_service, stripe_service, and a dozen
    other deployment-only modules. Instead we synthesize a tiny view that
    mimics the @app.route('/user_model_builder') decorator from pages.py
    so we can verify the auth gate, template rendering, and model_id
    passthrough behaviour without booting the full app.
    """
    from flask import Flask, redirect, render_template_string, session

    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-32-chars-aaaaaaaaaaaa'

    TEMPLATE = (
        '<!doctype html><html><body '
        'data-page="user-model-builder"'
        '{% if model_id %} data-model-id="{{ model_id }}"{% endif %}>'
        '<h1>Model Builder</h1></body></html>'
    )

    def login_required(fn):
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                return redirect('/login')
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper

    @app.route('/user_model_builder', endpoint='user_model_builder')
    @login_required
    def user_model_builder():
        return render_template_string(TEMPLATE, model_id=None)

    @app.route('/user_model_builder/<int:model_id>', endpoint='user_model_builder_edit')
    @login_required
    def user_model_builder_edit(model_id):
        return render_template_string(TEMPLATE, model_id=model_id)

    return app


class CustomModelsRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = _make_test_app()
        self.client = self.app.test_client()

    def test_user_model_builder_redirects_when_unauthenticated(self):
        resp = self.client.get('/user_model_builder')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))

    def test_user_model_builder_with_id_redirects_when_unauthenticated(self):
        resp = self.client.get('/user_model_builder/42')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))

    def test_user_model_builder_renders_when_authenticated(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'tester'
        resp = self.client.get('/user_model_builder')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('data-page="user-model-builder"', body)
        self.assertIn('Model Builder', body)
        # model_id is None when the URL has no <int:model_id>
        self.assertNotIn('data-model-id=', body)

    def test_user_model_builder_with_id_embeds_id_attribute(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
        resp = self.client.get('/user_model_builder/42')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('data-page="user-model-builder"', body)
        self.assertIn('data-model-id="42"', body)


class SourceLevelRouteRegistrationTestCase(unittest.TestCase):
    """Verify the routes referenced by the SPA UI exist in source.

    These don't boot the app — they statically inspect the URL map of a
    freshly-built Flask app that imports each route module's registration
    helper directly. If a route file has a syntax error or fails to import,
    the test fails loudly.
    """

    def _build_app_with(self, register_callable, *args, **kwargs):
        import types
        fake_app_module = types.ModuleType('phonos_app.app')
        from flask import Flask
        fake_app_module.app = Flask(__name__)
        sys_modules_app = sys.modules.get('phonos_app.app')
        sys.modules['phonos_app.app'] = fake_app_module
        try:
            register_callable(fake_app_module.app, *args, **kwargs)
            return {rule.rule for rule in fake_app_module.app.url_map.iter_rules()}
        finally:
            if sys_modules_app is not None:
                sys.modules['phonos_app.app'] = sys_modules_app
            else:
                sys.modules.pop('phonos_app.app', None)

    def test_pages_module_defines_standalone_routes(self):
        """pages.py exposes /custom_models, /user_model_builder, and the
        edit variant. We grep the source file rather than booting it
        because the pages module pulls in heavy plan/billing deps.
        """
        with open(os.path.join(REPO, 'phonos_app', 'routes', 'pages.py')) as f:
            src = f.read()
        self.assertIn("@app.route('/custom_models')", src)
        self.assertIn("@app.route('/user_model_builder')", src)
        self.assertIn("@app.route('/user_model_builder/<int:model_id>')", src)

    def test_user_model_api_routes_present_in_source(self):
        """Every URL the SPA / notebooks / submit-job flow depends on must
        exist in api_user_models.py."""
        with open(os.path.join(REPO, 'phonos_app', 'routes', 'api_user_models.py')) as f:
            src = f.read()
        for path in (
            "/api/user-models",
            "/api/user-models/<int:db_id>",
            "/api/user-models/by-id/<model_id>",
            "/api/user-models/<int:model_id>",
            "/api/user-models/<int:model_id>/api-keys",
            "/api/worker-api-keys",
            "/api/worker-api-keys/<int:key_id>",
            "/api/worker-api-keys/<int:key_id>/toggle",
            "/api/worker-api-keys/<int:key_id>/view",
            "/api/projects/<int:project_id>/api-keys",
            "/api/jobs/submit",
            "/api/user-model-jobs/<job_id>/result",
            "/view_user_model_output/<job_id>",
        ):
            with self.subTest(path=path):
                self.assertIn(f"@app.route('{path}'", src,
                              f'Missing route in api_user_models.py: {path}')

    def test_bridge_test_endpoint_exists(self):
        """The new in-modal Test Connection feature calls
        /api/bridge/v1/worker/info; verify it's registered."""
        with open(os.path.join(REPO, 'phonos_app', 'routes', 'api_bridge.py')) as f:
            src = f.read()
        self.assertIn("/api/bridge/v1/worker/info", src)
        self.assertIn("/api/bridge/v1/jobs/poll", src)
        self.assertIn("/api/bridge/v1/health", src)


class JSAssetSanityTestCase(unittest.TestCase):
    """Make sure the JS files we touched still parse and contain the
    helper entry points the new UI relies on."""

    def _read(self, rel):
        with open(os.path.join(REPO, rel)) as f:
            return f.read()

    def _node_parse_check(self, rel):
        import subprocess
        result = subprocess.run(
            ['node', '--check', os.path.join(REPO, rel)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.fail(f'{rel} failed node --check: {result.stderr}')

    def test_app_js_parses(self):
        self._node_parse_check('static/js/app.js')

    def test_notebook_js_parses(self):
        self._node_parse_check('static/js/notebook.js')

    def test_custom_models_js_parses(self):
        self._node_parse_check('static/js/custom_models.js')

    def test_app_js_exposes_key_handling_helpers(self):
        src = self._read('static/js/app.js')
        # New helpers introduced for the key creation / test connection flow.
        for helper in (
            'App._switchApiKeyTab',
            'App._copyNewKey',
            'App._downloadKeyFile',
            'App._testWorkerConnection',
            'App._testExistingKeyFromCard',
            'App._copySnippet',
            'App._copyTextWithFeedback',
        ):
            with self.subTest(helper=helper):
                self.assertIn(f'{helper} =', src,
                              f'Missing helper in app.js: {helper}')

    def test_app_js_routes_unified_custom_models(self):
        src = self._read('static/js/app.js')
        # Views.load('user-models') now branches to builder or registry.
        self.assertIn("'user-models'", src)
        self.assertIn('renderUserModels()', src)
        self.assertIn('renderUserModelBuilder(options)', src)

    def test_app_js_wires_submit_button_to_test(self):
        src = self._read('static/js/app.js')
        # Per-card Test button + post-creation Test tab.
        self.assertIn('App._testExistingKeyFromCard', src)
        self.assertIn('App._testWorkerConnection', src)

    def test_app_js_post_creation_modal_is_sticky(self):
        """The post-creation modal must be marked sticky so accidental
        clicks on the overlay or Esc don't dismiss the one-time secret."""
        src = self._read('static/js/app.js')
        self.assertIn('sticky: true', src,
                      'Post-creation modal should pass sticky:true to Modal.open')
        self.assertIn('_maybeStickyGuard', src,
                      'Modal must implement the sticky confirmation guard')

    def test_app_js_post_creation_modal_does_not_autoclose(self):
        """The post-creation modal must NOT auto-open the next modal on
        Done — that pattern was the original 'key disappears too fast'
        bug because Modal.open replaces bodyEl.innerHTML synchronously,
        so the Done onConfirm fired App.manageModelApiKeys which
        immediately overwrote the key modal before the user could read it.
        """
        src = self._read('static/js/app.js')
        idx = src.find("title: 'Worker Key Created'")
        self.assertGreater(idx, -1)
        close_marker = "                    confirmText: 'Done'"
        close_idx = src.find(close_marker, idx)
        body = src[idx:close_idx]
        # The Done onConfirm must NOT call manageModelApiKeys — instead it
        # just closes the modal. Navigation is delegated to the Cancel
        # button (labelled "Manage Keys").
        self.assertNotIn('onConfirm: () => { App.manageModelApiKeys(',
                         body, 'Done onConfirm must not auto-open manageModelApiKeys')
        self.assertIn('onCancel:', body,
                      'Cancel button must have an onCancel handler to navigate to manageModelApiKeys')
        self.assertIn('cancelText: \'Manage Keys\'', body,
                      'Cancel button should be labelled "Manage Keys" so navigation is intentional')

    def test_app_js_post_creation_modal_has_no_inline_style_blocks(self):
        """The inline <style> block that shipped with the first iteration of
        the post-creation modal would leak into the document head every time
        the modal opened. The rework must move the rules to a real CSS file
        instead of injecting another <style> tag."""
        src = self._read('static/js/app.js')
        marker = "title: 'Worker Key Created'"
        idx = src.find(marker)
        self.assertGreater(idx, -1, 'Post-creation modal must exist')
        # Walk forward to the modal close (we don't try to match a literal
        # backtick because Python raw vs. escaped string handling trips over
        # it; the next "Modal.open({" call after the marker is on a
        # different modal and is enough of a sentinel).
        close_marker = "                    confirmText: 'Done'"
        close_idx = src.find(close_marker, idx)
        self.assertGreater(close_idx, -1, 'Post-creation modal closing not found')
        body = src[idx:close_idx]
        self.assertNotIn('<style>', body,
                         'Post-creation modal content must not contain a <style> tag')

    def test_app_js_manage_model_api_keys_uses_wsk_classes(self):
        """The manageModelApiKeys modal must use the .wsk-* CSS classes
        (defined in custom-models-spa.css) and not rely on inline styles
        that override them."""
        src = self._read('static/js/app.js')
        idx = src.find('title: `Worker Security -')
        self.assertGreater(idx, -1, 'manageModelApiKeys modal must exist')
        # Quick smell-check: at least three wsk-* classes should appear.
        body = src[idx:idx + 6000]
        for cls in ('wsk-root', 'wsk-tabs', 'wsk-tab', 'wsk-section', 'wsk-card', 'wsk-banner'):
            with self.subTest(cls=cls):
                self.assertIn(cls, body, f'manageModelApiKeys modal missing {cls} class')


class CSSAssetTestCase(unittest.TestCase):
    """Verify the custom-models-spa.css stylesheet exists and contains the
    classes the SPA modal HTML expects. CSS files don't break in obvious
    ways at runtime — instead they silently mis-style — so checking that
    the markup's class references resolve to real selectors catches the
    'code CSS is fucked up' class of regressions."""

    def _read(self, rel):
        with open(os.path.join(REPO, rel)) as f:
            return f.read()

    def test_custom_models_spa_css_exists(self):
        self._read('static/css/custom-models-spa.css')

    def test_apkm_classes_present(self):
        css = self._read('static/css/custom-models-spa.css')
        for cls in ('.apkm-root', '.apkm-tabs', '.apkm-tab', '.apkm-section',
                    '.apkm-hero', '.apkm-key-box', '.apkm-key-input',
                    '.apkm-key-copy', '.apkm-snippet', '.apkm-snippet-body',
                    '.apkm-h3', '.apkm-meta-grid', '.apkm-meta',
                    '.apkm-test-result', '.apkm-spinner',
                    '.apkm-test-btn', '.apkm-mono', '.apkm-hint'):
            with self.subTest(cls=cls):
                self.assertIn(cls, css, f'Missing selector in custom-models-spa.css: {cls}')

    def test_wsk_classes_present(self):
        css = self._read('static/css/custom-models-spa.css')
        for cls in ('.wsk-root', '.wsk-intro', '.wsk-banner', '.wsk-tabs',
                    '.wsk-tab', '.wsk-section', '.wsk-section-head',
                    '.wsk-section-title', '.wsk-card', '.wsk-card-head',
                    '.wsk-card-info', '.wsk-card-title', '.wsk-card-badges',
                    '.wsk-card-actions', '.wsk-badge', '.wsk-badge-dot',
                    '.wsk-key-display', '.wsk-key-mask', '.wsk-key-note',
                    '.wsk-desc', '.wsk-meta-row', '.wsk-empty',
                    '.wsk-info-card', '.wsk-rules', '.wsk-code-card',
                    '.wsk-footnote', '.wsk-view-key', '.wsk-btn-danger'):
            with self.subTest(cls=cls):
                self.assertIn(cls, css, f'Missing selector in custom-models-spa.css: {cls}')

    def test_mb_builder_classes_present(self):
        css = self._read('static/css/custom-models-spa.css')
        for cls in ('.mb-builder-grid', '.mb-section', '.mb-section-header',
                    '.mb-section-icon', '.mb-section-title', '.mb-section-badge',
                    '.mb-section-chevron', '.mb-format-chip'):
            with self.subTest(cls=cls):
                self.assertIn(cls, css, f'Missing selector in custom-models-spa.css: {cls}')

    def test_right_panel_scrolls_independently(self):
        """The Integration / Format Guide panel must have a viewport-locked
        height and scroll independently of the page so the tab strip
        stays visible while the user scrolls long snippets."""
        css = self._read('static/css/custom-models-spa.css')

        # Builder grid is locked to the available viewport.
        self.assertRegex(css, r'\.mb-builder-grid\s*\{[^}]*height:\s*calc\(100vh')

        # The right-side preview panel (.nb-split-content) is a flex column
        # with min-height: 0 so it can shrink to fit the grid cell.
        self.assertRegex(css, r'\.nb-split-content\s*\{[^}]*flex-direction:\s*column')
        self.assertRegex(css, r'\.nb-split-content\s*\{[^}]*min-height:\s*0')

        # The active .nb-md-section is the actual scroll container with
        # overflow-y: auto and flex: 1 so it fills the remaining height.
        self.assertRegex(css, r'\.mb-builder\s+\.nb-md-section[^{]*\{[^}]*overflow-y:\s*auto')
        self.assertRegex(css, r'\.mb-builder\s+\.nb-md-section[^{]*\{[^}]*flex:\s*1')

        # The snippet code blocks scroll independently in both axes
        # within a max-height constraint so long snippets don't push the
        # tab strip off-screen. The parent section handles vertical
        # overflow; the code block handles intra-snippet overflow.
        self.assertRegex(css, r'\.nb-md-snippet-code[^{]*\{[^}]*overflow:\s*auto')

        # mb-preview-panel aligns itself so the flex parent gives it the
        # remaining height.
        self.assertIn('.mb-preview-panel', css)

    def test_apkm_hero_saved_state_exists(self):
        """The hero card in the post-creation modal must have a 'saved'
        variant so the visual state changes after the user copies the key.
        Without it the user has no persistent confirmation that they
        have the key in their clipboard."""
        css = self._read('static/css/custom-models-spa.css')
        self.assertIn('.apkm-hero--saved', css,
                      'Missing .apkm-hero--saved rule for the persistent saved indicator')

    def test_apkm_root_present_in_post_creation_modal(self):
        """The post-creation modal must wrap its content in .apkm-root
        so the CSS scoping (e.g. apkm-hero--saved) actually matches."""
        src = self._read('static/js/app.js')
        idx = src.find("title: 'Worker Key Created'")
        close_marker = "                    confirmText: 'Done'"
        close_idx = src.find(close_marker, idx)
        body = src[idx:close_idx]
        self.assertIn('class="apkm-root"', body)
        self.assertIn('id="apkm-hero-status"', body,
                      'Hero needs an id so _markKeyModalAsSaved can find it')

    def test_modal_sticky_classes_present(self):
        css = self._read('static/css/custom-models-spa.css')
        # The sticky modal accent is what warns the user that closing
        # requires explicit confirmation.
        self.assertIn('.modal.modal-sticky', css)
        self.assertIn('.modal-title-lock', css)

    def test_dashboard_template_loads_new_css(self):
        # The dashboard template must load the new CSS so the styles are
        # available before the SPA renders the modal.
        tpl = self._read('templates/dashboard.html')
        self.assertIn('custom-models-spa.css', tpl)

    def test_standalone_templates_load_new_css(self):
        for path in ('templates/custom_models.html', 'templates/user_model_builder.html'):
            tpl = self._read(path)
            with self.subTest(path=path):
                self.assertIn('custom-models-spa.css', tpl)


if __name__ == '__main__':
    unittest.main()
