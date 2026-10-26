import hashlib
import logging

from redis.exceptions import RedisError
from rest_framework.throttling import (
	AnonRateThrottle,
	ScopedRateThrottle,
	UserRateThrottle,
)


logger = logging.getLogger(__name__)


class AtomicThrottleMixin:
	"""Use atomic cache add/incr counters instead of DRF's history list."""

	# When the cache backend is unreachable, general API throttles let requests
	# through (the API stays up) while brute-force-sensitive scopes deny them.
	fail_open = True

	def _increment(self, key):
		try:
			return self._increment_counter(key)
		except RedisError:
			logger.exception('Throttle cache unavailable for %s', type(self).__name__)
			return 0 if self.fail_open else self.num_requests + 1

	def _increment_counter(self, key):
		if self.cache.add(key, 1, timeout=self.duration):
			return 1

		try:
			return self.cache.incr(key)
		except ValueError:
			# The key may have expired between add() and incr(). Recreate it.
			if self.cache.add(key, 1, timeout=self.duration):
				return 1
			return self.cache.incr(key)

	def _allow_atomic_request(self, request, view):
		self.rate = self.get_rate()
		if self.rate is None:
			return True

		self.num_requests, self.duration = self.parse_rate(self.rate)
		self.key = self.get_cache_key(request, view)
		if self.key is None:
			return True
		return self._increment(self.key) <= self.num_requests

	def wait(self):
		return getattr(self, 'duration', None)


class AtomicAnonRateThrottle(AtomicThrottleMixin, AnonRateThrottle):
	"""Atomic fixed-window rate limit for unauthenticated API traffic."""

	def allow_request(self, request, view):
		return self._allow_atomic_request(request, view)


class AtomicUserRateThrottle(AtomicThrottleMixin, UserRateThrottle):
	"""Atomic fixed-window rate limit for authenticated API traffic."""

	def allow_request(self, request, view):
		return self._allow_atomic_request(request, view)


class AtomicScopedRateThrottle(AtomicThrottleMixin, ScopedRateThrottle):
	"""Atomic fixed-window Redis counters for scoped auth endpoints."""

	fail_open = False

	def allow_request(self, request, view):
		self.scope = getattr(view, 'throttle_scope', None)
		if self.scope is None:
			return True

		self.rate = self.get_rate()
		if self.rate is None:
			return True

		self.num_requests, self.duration = self.parse_rate(self.rate)
		client_ip = self.get_ident(request)
		identifiers = [f'ip:{client_ip}']
		if self.scope == 'login':
			data = request.data
			username = data.get('username', '') if hasattr(data, 'get') else ''
			username_digest = hashlib.sha256(
				str(username).strip().casefold().encode('utf-8'),
			).hexdigest()
			# Scope the account counter to the client IP. A counter keyed on the
			# username alone lets any third party lock the owner out of login.
			identifiers.append(f'account:{client_ip}:{username_digest}')

		allowed = True
		for identifier in identifiers:
			key = self.cache_format % {
				'scope': self.scope,
				'ident': identifier,
			}
			allowed = self._increment(key) <= self.num_requests and allowed
		return allowed

	def wait(self):
		return self.duration
