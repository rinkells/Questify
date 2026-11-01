"""Errors the services raise for a refused request, with machine codes.

``str(exc)`` is the English text the API has always sent and must not change.
``code`` is stable and says what happened, so a client (the web UI) can show
its own text for it without parsing English. ``params`` carries the numbers
that text may need (a balance, a cost), and ``field`` names the input the
refusal is about; API views answer ``{field or 'detail': message}`` (see
``core.exceptions.validation_error``).

Internal ids stay out of the params a user would see: a code with its params
is what a translation is written from.
"""

class DomainError(ValueError):
    """A request the rules refuse. Subclasses fix a default ``code``."""

    code = 'error'

    def __init__(self, message='', *, code=None, field=None, **params):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.field = field
        self.params = params

    @property
    def message(self):
        return str(self)


class ConflictError(DomainError):
    """A change the current state refuses (HTTP 409)."""

    code = 'conflict'


class NotFoundError(DomainError, LookupError):
    """The row is not the user's, is archived or is gone (HTTP 404).

    Raised for the lookup alone, so a ``DoesNotExist`` from an event handler
    is not mistaken for it and stays an error.
    """

    code = 'not_found'
