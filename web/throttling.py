"""The API's throttles, applied to the web's forms and actions.

Same classes, same rates, same counters as the API: a client has one budget
whichever door it uses. In the web only POSTs count against the ``user`` budget,
so reading pages does not use up what the API may spend.
"""
import math

from core.throttling import AtomicScopedRateThrottle, AtomicUserRateThrottle


class ThrottleUnavailable(Exception):
    """The counters cannot be reached, so a form that fails closed is refused."""


class _FormRequest:
    """A form post, with the ``data`` the throttle reads from a DRF request."""

    def __init__(self, request):
        self._request = request
        self.data = request.POST

    def __getattr__(self, name):
        return getattr(self._request, name)


class _Scope:
    def __init__(self, scope):
        self.throttle_scope = scope


def _seconds(throttle):
    return max(1, math.ceil(throttle.wait() or 1))


def throttled_for(request, scope):
    """Seconds to wait if ``request`` exceeds the ``scope`` budget, else None.

    ``scope`` is 'login' or 'register'. They fail closed like the API; here that
    raises ``ThrottleUnavailable``, because an outage is not the visitor's doing
    and must not read as too many attempts.
    """
    throttle = AtomicScopedRateThrottle()
    if throttle.allow_request(_FormRequest(request), _Scope(scope)):
        return None
    if throttle.cache_failed:
        raise ThrottleUnavailable
    return _seconds(throttle)


def user_budget_wait(request):
    """Count a logged-in POST against the API's ``user`` budget.

    Returns the seconds to wait when it is spent, else None. Fails open like the
    API: an action of the user's own is not worth refusing during an outage.
    """
    throttle = AtomicUserRateThrottle()
    if throttle.allow_request(_FormRequest(request), None):
        return None
    return _seconds(throttle)


def describe_wait(seconds):
    """'42 с' up to two minutes, then whole minutes ('15 хв')."""
    if seconds < 120:
        return f'{seconds} с'
    return f'{math.ceil(seconds / 60)} хв'
