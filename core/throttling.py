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
		if self._increment(self.key) <= self.num_requests:
			return True
		self._blocked_keys = [self.key]
		return False

	def _remaining_seconds(self, key):
		"""Seconds left in a counter's window, or None if the cache can't say."""
		try:
			client = self.cache._cache.get_client(key)
			ttl = client.ttl(self.cache.make_and_validate_key(key))
		except (AttributeError, RedisError):
			# Non-Redis backends (tests, local fallback) expose no TTL.
			return None
		return ttl if ttl > 0 else None

	def wait(self):
		# Report the real time left in the fixed window, not its full length.
		remaining = [
			seconds
			for seconds in map(
				self._remaining_seconds,
				getattr(self, '_blocked_keys', ()),
			)
			if seconds
		]
		return max(remaining) if remaining else getattr(self, 'duration', None)


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

		self._blocked_keys = []
		for identifier in identifiers:
			key = self.cache_format % {
				'scope': self.scope,
				'ident': identifier,
			}
			if self._increment(key) > self.num_requests:
				self._blocked_keys.append(key)
		return not self._blocked_keys
