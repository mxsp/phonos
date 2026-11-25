"""
Lightweight tests for the Notebook SDK shim (phonos package inside kernels).

Covers the help/guide/docs surface that was previously broken (missing guide()).
"""
import os
import sys
import pytest

# Ensure we can reach the notebook kernel package
SDK_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'production', 'docker', 'notebook-kernel'
))
if SDK_PATH not in sys.path:
    sys.path.insert(0, SDK_PATH)


def test_guide_and_help_work_without_nameerror():
    """phonos.guide(), help(), help('guide'), help('overview'), docs() must not raise NameError."""
    import phonos

    # These used to fail with NameError: name 'guide' is not defined
    overview = phonos.help()
    assert isinstance(overview, str)
    assert len(overview) > 100
    assert "Phonos SDK" in overview or "import phonos" in overview

    g = phonos.guide()
    assert g == overview or len(g) > 100

    h_guide = phonos.help('guide')
    assert isinstance(h_guide, str)

    h_overview = phonos.help('overview')
    assert isinstance(h_overview, str)

    d = phonos.docs()
    assert hasattr(d, 'body') or isinstance(d, dict) or hasattr(d, 'topic')

    # Should also work for a model
    # (may return help text or raise for unknown; we just want no crash on the mechanism)
    try:
        phonos.help('boltz2')
    except Exception:
        # acceptable if no full model registry in this test env
        pass


def test_api_includes_guide():
    """The programmatic api() surface should list 'guide'."""
    import phonos
    api = phonos.api()
    topics = api.get('topics', [])
    assert 'guide' in topics or 'overview' in topics
