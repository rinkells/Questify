import hashlib
import logging
import time
from ipaddress import ip_address, ip_network

from redis.exceptions import RedisError
from rest_framework.throttling import (
	AnonRateThrottle,
	ScopedRateThrottle,
	SimpleRateThrottle,
	UserRateThrottle,
)


logger = logging.getLogger(__name__)

# An IPv6 client is usually given a whole /64, so keying on the full address
# would let one host rotate through billions of fresh budgets.
IPV6_CLIENT_PREFIX = 64


def client_network(ident):
	"""Return the throttle identity for a client address.

	IPv4 addresses (including IPv4-mapped IPv6) stay as they are; IPv6
	addresses become their /64. Anything that does not parse is returned
	unchanged. Only for counting: allow-lists must use the real address.
	"""
	try:
		address = ip_address(ident)
	except ValueError:
		return ident
	if address.version == 6:
		if address.ipv4_mapped is not None:
			return str(address.ipv4_mapped)
		return str(ip_network(f'{address}/{IPV6_CLIENT_PREFIX}', strict=False))
	return str(address)


class AtomicThrottleMixin:
	"""Use atomic cache add/incr counters instead of DRF's history list."""

	# When the cache backend is unreachable, general API throttles let requests
	# through (the API stays up) while brute-force-sensitive scopes deny them.
	fail_open = True

	def get_ident(self, request):
		return client_network(super().get_ident(request))

	def _increment(self, key, timeout=None):
		try:
			return self._increment_counter(key, timeout or self.duration)
		except RedisError:
			logger.exception('Throttle cache unavailable for %s', type(self).__name__)
			return 0 if self.fail_open else float('inf')

	def _increment_counter(self, key, timeout):
		if self.cache.add(key, 1, timeout=timeout):
			return 1

		try:
			return self.cache.incr(key)
		except ValueError:
			# The key may have expired between add() and incr(). Recreate it.
			if self.cache.add(key, 1, timeout=timeout):
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
		self._blocked_keys = []

		# Count the per-IP bucket first: once it is exhausted the request body
		# is never parsed, so a throttled client cannot make the server spend
		# CPU decoding large payloads.
		ip_key = self._key(f'ip:{client_ip}')
		if self._increment(ip_key) > self.num_requests:
			self._blocked_keys.append(ip_key)
			return False

		if self.scope == 'login':
			data = request.data
			username = data.get('username', '') if hasattr(data, 'get') else ''
			username_digest = hashlib.sha256(
				self._account_name(username).casefold().encode('utf-8'),
			).hexdigest()
			# Scope the account counter to the client IP. A counter keyed on the
			# username alone lets any third party lock the owner out of login.
			account_key = self._key(f'account:{client_ip}:{username_digest}')
			if self._increment(account_key) > self.num_requests:
				self._blocked_keys.append(account_key)
			else:
				self._check_account_networks(username_digest, client_ip)
		return not self._blocked_keys

	def _check_account_networks(self, username_digest, client_ip):
		"""Bound how many networks may try one account's password per window.

		The account counter above is per client network, so a third party
		cannot lock the owner out from one address; but alone it lets a
		botnet guess without limit. This ceiling (the 'login_networks' rate)
		counts the distinct networks trying the account in a fixed window.
		Networks already counted keep their own budget; a new one beyond the
		ceiling is refused until the window ends. Locking the owner out of a
		new network therefore takes as many networks as the ceiling.
		"""
		rate = self.THROTTLE_RATES.get('login_networks')
		if rate is None:
			return
		max_networks, window = self.parse_rate(rate)
		# Both keys carry the window number, so a network seen in one window
		# is counted again in the next and the markers expire with it.
		window_id = int(time.time() // window)
		count_key = self._key(f'account-networks:{window_id}:{username_digest}')
		marker_key = self._key(
			f'account-network:{window_id}:{username_digest}:{client_ip}',
		)
		try:
			if not self.cache.add(marker_key, 1, timeout=window):
				return
		except RedisError:
			logger.exception('Throttle cache unavailable for login networks')
			self._blocked_keys.append(count_key)
			return
		if self._increment(count_key, timeout=window) > max_networks:
			# Not counted after all, so the network stays refused.
			try:
				self.cache.delete(marker_key)
			except RedisError:
				logger.exception('Throttle cache unavailable for login networks')
			self._blocked_keys.append(count_key)

	@staticmethod
	def _account_name(username):
		"""The account a login attempt targets, as login itself resolves it.

		Keyed on the raw name, every NFKC or case variant ('ｅｒｉｎ', 'ERIN')
		of one account would get its own counters, and the network ceiling
		would never fill. Input the login serializer rejects anyway (too long,
		NUL characters, which PostgreSQL refuses) is not looked up.
		"""
		from .auth_serializers import MAX_USERNAME_LENGTH, resolve_login_username

		# Trimmed like the serializer's CharField trims it.
		username = str(username).strip()
		if len(username) > MAX_USERNAME_LENGTH or '\x00' in username:
			return username
		return resolve_login_username(username)

	def _key(self, identifier):
		return self.cache_format % {'scope': self.scope, 'ident': identifier}


class AuthFailureThrottle(AtomicThrottleMixin, SimpleRateThrottle):
	"""Per-IP budget for failed token authentications.

	DRF authenticates before it throttles, so requests with bad tokens never
	reach the anon/user throttles. The authenticator consults this one instead:
	``is_blocked`` before the token lookup, ``record_failure`` after a miss.
	"""

	scope = 'auth_failure'

	def get_cache_key(self, request, view):
		return self.cache_format % {
			'scope': self.scope,
			'ident': self.get_ident(request),
		}

	def is_blocked(self, request):
		key = self.get_cache_key(request, None)
		try:
			blocked = (self.cache.get(key) or 0) >= self.num_requests
		except RedisError:
			logger.exception('Throttle cache unavailable for AuthFailureThrottle')
			return False
		if blocked:
			self._blocked_keys = [key]
		return blocked

	def record_failure(self, request):
		self._increment(self.get_cache_key(request, None))
