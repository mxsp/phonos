"""
Internationalization (i18n) helper for Phonos
Provides language detection and translation support
"""

import json
import os


def get_user_language():
    """
    Get the user's preferred language
    Priority: JS-set cookie > session > browser accept-language > default (en)
    """
    from flask import request, session

    # Check phonos-language cookie (set by client-side JS i18n)
    cookie_lang = request.cookies.get('phonos-language')
    if cookie_lang in ('en', 'de'):
        return cookie_lang

    # Check session
    if 'language' in session:
        return session.get('language')

    # Check browser language
    browser_lang = request.accept_languages.best_match(['en', 'de'])
    if browser_lang:
        return browser_lang

    # Default to English
    return 'en'


def set_user_language(lang):
    """Set user's language preference in session"""
    from flask import session

    if lang in ['en', 'de']:
        session['language'] = lang
        return True
    return False


def load_translations(lang='en'):
    """Load translation file for given language"""
    translations_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        'static',
        'locales',
        f'{lang}.json'
    )

    try:
        with open(translations_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        # Fallback to English
        if lang != 'en':
            return load_translations('en')
        return {}


def get_translation(key, lang=None, params=None):
    """
    Get translation for a key

    Args:
        key: Translation key (supports dot notation: 'nav.dashboard')
        lang: Language code (defaults to user's language)
        params: Optional dict for string interpolation

    Returns:
        Translated string or key if not found
    """
    if lang is None:
        lang = get_user_language()

    translations = load_translations(lang)

    # Navigate through nested keys
    keys = key.split('.')
    value = translations

    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            # Fallback to key if not found
            return key

    # Handle string interpolation
    if isinstance(value, str) and params:
        for param_key, param_value in params.items():
            value = value.replace(f'{{{param_key}}}', str(param_value))

    return value


# Shorter alias
t = get_translation


def init_i18n(app):
    """Initialize i18n support for Flask app"""
    from flask import request, jsonify

    @app.context_processor
    def inject_i18n():
        """Inject i18n functions into all templates"""
        return {
            'get_language': get_user_language,
            't': get_translation,
            'user_language': get_user_language()
        }

    @app.route('/api/language', methods=['GET', 'POST'])
    def api_language():
        """API endpoint to get/set language preference"""
        if request.method == 'POST':
            data = request.get_json()
            lang = data.get('language')

            if set_user_language(lang):
                return jsonify({'success': True, 'language': lang})
            else:
                return jsonify({
                    'success': False,
                    'error': 'Invalid language'
                }), 400
        else:
            return jsonify({
                'language': get_user_language(),
                'available': ['en', 'de']
            })
