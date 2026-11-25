"""The Stripe webhooks accept only correctly signed events.

With an empty STRIPE_WEBHOOK_SECRET the routes used to parse the body as JSON and
trust it, so a forged checkout.session.completed upgraded any account.
"""
import hashlib
import hmac
import json
import os
import time

import pytest
from flask import Flask

# Config refuses to import without these; dummy values, the file never touches a database.
os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key-not-used-anywhere-else')
os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')

from config import Config
from phonos_app.routes import stripe_routes

ROUTES = ['/api/webhooks/stripe', '/stripe_premium']
SECRET = 'whsec_unit_test_value'
EVENT = {'id': 'evt_1', 'object': 'event', 'type': 'charge.refunded', 'data': {'object': {'id': 'ch_1'}}}


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    stripe_routes.register_stripe_routes(app)
    return app.test_client()


def _sign(payload: bytes, secret: str = SECRET) -> str:
    ts = str(int(time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b'.' + payload, hashlib.sha256).hexdigest()
    return f't={ts},v1={mac}'


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('secret', ['', None])
def test_no_secret_refuses_everything(client, monkeypatch, route, secret):
    monkeypatch.setattr(Config, 'STRIPE_WEBHOOK_SECRET', secret)
    forged = dict(EVENT, type='checkout.session.completed',
                  data={'object': {'customer': 'cus_x', 'metadata': {'user_id': '1', 'plan_name': 'Premium'}}})
    r = client.post(route, data=json.dumps(forged), content_type='application/json')
    assert r.status_code == 503
    # even a request that carries a signature header cannot be checked, so it is refused too
    r = client.post(route, data=json.dumps(forged), headers={'Stripe-Signature': 't=1,v1=00'})
    assert r.status_code == 503


@pytest.mark.parametrize('route', ROUTES)
def test_with_secret_unsigned_or_wrongly_signed_events_are_rejected(client, monkeypatch, route):
    monkeypatch.setattr(Config, 'STRIPE_WEBHOOK_SECRET', SECRET)
    body = json.dumps(EVENT).encode()
    assert client.post(route, data=body).status_code == 400
    bad = _sign(body, 'whsec_another_secret')
    assert client.post(route, data=body, headers={'Stripe-Signature': bad}).status_code == 400


@pytest.mark.parametrize('route', ROUTES)
def test_with_secret_a_signed_event_is_processed(client, monkeypatch, route):
    monkeypatch.setattr(Config, 'STRIPE_WEBHOOK_SECRET', SECRET)
    body = json.dumps(EVENT).encode()
    r = client.post(route, data=body, headers={'Stripe-Signature': _sign(body)})
    assert r.status_code == 200
