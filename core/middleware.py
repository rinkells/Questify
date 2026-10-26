import hashlib
from ipaddress import ip_address

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseNotFound
from django.utils.cache import patch_cache_control
from redis.exceptions import RedisError
from rest_framework.throttling import BaseThrottle


class ApiNoStoreMiddleware:
	"""Keep API responses (login tokens, personal data) out of shared caches."""

	prefix = '/api/'

	def __init__(self, get_response):
		self.get_response = get_response

	def __call__(self, request):
		response = self.get_response(request)
		if request.path_info.startswith(self.prefix):
			patch_cache_control(response, no_store=True, private=True)
		return response


class AdminLoginRateLimitMiddleware:
	"""Limit password attempts against Django Admin by the client IP."""

	path = '/admin/login/'
	admin_path = '/admin/'
	limit = 10
	duration = 15 * 60

	def __init__(self, get_response):
		self.get_response = get_response

	def __call__(self, request):
		is_admin_path = (
			request.path_info == self.admin_path.rstrip('/')
			or request.path_info.startswith(self.admin_path)
		)
		client_ip = None
		if is_admin_path:
			client_ip = BaseThrottle().get_ident(request)
			if not settings.DEBUG and not self._is_allowed_admin_ip(client_ip):
				return HttpResponseNotFound()

		if request.method != 'POST' or request.path_info != self.path:
			return self.get_response(request)

		ip_digest = hashlib.sha256(client_ip.encode('utf-8')).hexdigest()
		cache_key = f'questify:admin-login:{ip_digest}'
		try:
			attempts = self._increment(cache_key)
		except RedisError:
			# Fail closed: without the counter, password guessing is unbounded.
			return HttpResponse('Service temporarily unavailable.', status=503)
		if attempts > self.limit:
			response = HttpResponse(
				'Too many admin login attempts. Try again later.',
				status=429,
			)
			response['Retry-After'] = str(self.duration)
			return response

		response = self.get_response(request)
		if response.status_code in (302, 303):
			try:
				cache.delete(cache_key)
			except RedisError:
				pass
		return response

	@staticmethod
	def _is_allowed_admin_ip(client_ip):
		try:
			address = ip_address(client_ip)
		except ValueError:
			return False
		return any(
			address.version == network.version and address in network
			for network in settings.ADMIN_ALLOWED_NETWORKS
		)

	@staticmethod
	def _increment(cache_key):
		if cache.add(cache_key, 1, timeout=AdminLoginRateLimitMiddleware.duration):
			return 1
		try:
			return cache.incr(cache_key)
		except ValueError:
			# The key can expire between the atomic add and increment operations.
			if cache.add(
				cache_key,
				1,
				timeout=AdminLoginRateLimitMiddleware.duration,
			):
				return 1
			return cache.incr(cache_key)
