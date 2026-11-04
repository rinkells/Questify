"""One-time form tokens, for an action that must not run twice: spending coins.

A token is issued when the form is rendered and claimed once when it is posted.
The tokens live in the user's session, but are changed in its database row, in a
transaction holding that row, rather than through ``request.session``:

- Two requests with one token both load the session before either saves it, so
  their copies cannot tell that the other has used it. A fresh read under the lock
  can, so of two parallel posts only one gets past ``claim``.
- Saving through ``request.session`` would renew it, and web sessions last a fixed
  30 days from login (config/settings.py). This way the row keeps its
  ``expire_date`` and the cookie is not sent again.

It therefore needs the database session backend, which is the default.
"""
import secrets

from django.contrib.sessions.models import Session
from django.db import transaction

SESSION_KEY = 'form_tokens'
# More than the forms a person keeps open. The oldest make way, so the session stays
# small however many forms are opened and left.
MAX_TOKENS = 20


def _locked_row(request):
    return Session.objects.select_for_update().filter(
        pk=request.session.session_key,
    ).first()


def _save(request, row, data):
    row.session_data = request.session.encode(data)
    row.save(update_fields=('session_data',))


def issue(request, scope):
    """A new token for a form that does ``scope``; call when rendering the form."""
    token = secrets.token_urlsafe(16)
    with transaction.atomic():
        row = _locked_row(request)
        if row is not None:
            data = request.session.decode(row.session_data)
            tokens = {**data.get(SESSION_KEY, {}), token: scope}
            data[SESSION_KEY] = dict(list(tokens.items())[-MAX_TOKENS:])
            _save(request, row, data)
    return token


def claim(request, scope, token):
    """Whether ``token`` was issued for ``scope`` and not used yet; it is used now."""
    with transaction.atomic():
        row = _locked_row(request)
        if row is None:
            return False
        data = request.session.decode(row.session_data)
        tokens = data.get(SESSION_KEY, {})
        if tokens.get(token) != scope:
            return False
        del tokens[token]
        _save(request, row, data)
    # This request's own copy, without marking it changed, in case something still
    # saves it: messages that overflow their cookie, say.
    request.session.get(SESSION_KEY, {}).pop(token, None)
    return True
