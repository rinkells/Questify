from datetime import timedelta
import hashlib
from ipaddress import ip_network
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from redis.exceptions import RedisError
from rest_framework.test import APIClient

from .models import HashedAPIToken, Stat, StatType
from .throttling import AtomicUserRateThrottle


class HealthCheckTests(TestCase):
	# The test runner forces DEBUG=False, so the probe must send the token.
	@override_settings(DEBUG=False, HEALTHCHECK_TOKEN='test-health-token-0123456789-abcdefghijkl')
	def test_health_check_reports_ready(self):
		response = APIClient().get(
			'/healthz/',
			HTTP_X_HEALTHCHECK_TOKEN='test-health-token-0123456789-abcdefghijkl',
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'status': 'ok'})

	@override_settings(DEBUG=True)
	def test_health_check_is_open_in_debug_mode(self):
		response = APIClient().get('/healthz/')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {'status': 'ok'})

	@override_settings(DEBUG=False, HEALTHCHECK_TOKEN='test-health-token-0123456789-abcdefghijkl')
	def test_production_health_check_requires_the_internal_token(self):
		client = APIClient()

		missing_token = client.get('/healthz/')
		wrong_token = client.get(
			'/healthz/',
			HTTP_X_HEALTHCHECK_TOKEN='wrong-token',
		)
		valid_token = client.get(
			'/healthz/',
			HTTP_X_HEALTHCHECK_TOKEN='test-health-token-0123456789-abcdefghijkl',
		)

		self.assertEqual(missing_token.status_code, 404)
		self.assertEqual(wrong_token.status_code, 404)
		self.assertEqual(valid_token.status_code, 200)

	@override_settings(DEBUG=False, HEALTHCHECK_TOKEN='test-health-token-0123456789-abcdefghijkl')
	def test_production_health_check_rejects_non_ascii_token_without_error(self):
		response = APIClient().get(
			'/healthz/',
			HTTP_X_HEALTHCHECK_TOKEN='caf\xe9',
		)

		self.assertEqual(response.status_code, 404)


class CharacterTimezoneValidationTests(TestCase):
	def test_invalid_timezone_names_are_rejected_with_400(self):
		user = get_user_model().objects.create_user(username='tz-user')
		_, raw_token = HashedAPIToken.issue_for_user(user)
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')

		for name in ('America', 'Etc', 'zone.tab', '../UTC', '/etc/passwd', ''):
			with self.subTest(name=name):
				response = client.patch(
					'/api/character/',
					{'timezone': name},
					format='json',
				)
				self.assertEqual(response.status_code, 400)

		valid = client.patch(
			'/api/character/',
			{'timezone': 'Europe/Warsaw'},
			format='json',
		)
		self.assertEqual(valid.status_code, 200)


class APIAuthenticationTests(TestCase):
	def test_user_can_register_and_obtain_token(self):
		client = APIClient()
		password = 'Cobalt-River-Planets-482!'
		response = client.post(
			'/api/auth/register/',
			{
				'username': 'new-api-user',
				'email': 'api-user@example.com',
				'password': password,
			},
			format='json',
		)

		self.assertEqual(response.status_code, 201)
		self.assertNotIn('password', response.data)
		user = get_user_model().objects.get(username='new-api-user')
		old_token, old_raw_token = HashedAPIToken.issue_for_user(user)

		login_response = client.post(
			'/api/auth/login/',
			{'username': 'new-api-user', 'password': password},
			format='json',
		)
		self.assertEqual(login_response.status_code, 200)
		self.assertTrue(login_response.data['token'])
		self.assertNotEqual(login_response.data['token'], old_raw_token)
		self.assertFalse(HashedAPIToken.objects.filter(pk=old_token.pk).exists())
		self.assertEqual(
			HashedAPIToken.objects.get(user=user).token_digest,
			hashlib.sha256(
				login_response.data['token'].encode('ascii'),
			).hexdigest(),
		)
		self.assertNotIn(
			login_response.data['token'],
			HashedAPIToken.objects.get(user=user).token_digest,
		)
		self.assertIn('expires_at', login_response.data)

	def test_registration_rejects_password_similar_to_username(self):
		response = APIClient().post(
			'/api/auth/register/',
			{
				'username': 'same-as-password',
				'email': 'similar@example.com',
				'password': 'same-as-password',
			},
			format='json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertIn('password', response.data)

	def test_expired_token_is_rejected_and_deleted(self):
		user = get_user_model().objects.create_user(
			username='expired-token-user',
			password='Strong-Password-543!',
		)
		token, raw_token = HashedAPIToken.issue_for_user(user)
		HashedAPIToken.objects.filter(pk=token.pk).update(
			created_at=timezone.now() - timedelta(days=settings.AUTH_TOKEN_TTL_DAYS + 1),
		)
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')

		response = client.get('/api/character/')

		self.assertEqual(response.status_code, 401)
		self.assertFalse(HashedAPIToken.objects.filter(pk=token.pk).exists())

	def test_logout_revokes_token(self):
		user = get_user_model().objects.create_user(
			username='logout-user',
			password='Strong-Password-543!',
		)
		token, raw_token = HashedAPIToken.issue_for_user(user)
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')

		logout_response = client.post('/api/auth/logout/')
		protected_response = client.get('/api/character/')

		self.assertEqual(logout_response.status_code, 204)
		self.assertEqual(protected_response.status_code, 401)
		self.assertFalse(HashedAPIToken.objects.filter(pk=token.pk).exists())

	def test_login_is_rate_limited(self):
		cache.clear()
		client = APIClient()
		responses = [
			client.post(
				'/api/auth/login/',
				{'username': 'missing-user', 'password': 'incorrect'},
				format='json',
				REMOTE_ADDR='198.51.100.42',
			)
			for _ in range(6)
		]
		cache.clear()

		self.assertEqual(
			[response.status_code for response in responses],
			[400, 400, 400, 400, 400, 429],
		)

	def test_login_rate_limit_is_scoped_to_account_and_client_ip(self):
		cache.clear()
		client = APIClient()
		responses = [
			client.post(
				'/api/auth/login/',
				{'username': 'same-account', 'password': 'incorrect'},
				format='json',
				REMOTE_ADDR='198.51.100.7',
			)
			for _ in range(6)
		]
		cache.clear()

		self.assertEqual(
			[response.status_code for response in responses],
			[400, 400, 400, 400, 400, 429],
		)

	def test_third_party_cannot_lock_owner_out_of_login(self):
		cache.clear()
		password = 'Strong-Password-543!'
		get_user_model().objects.create_user(
			username='lockout-victim',
			password=password,
		)
		client = APIClient()
		for index in range(1, 7):
			client.post(
				'/api/auth/login/',
				{'username': 'lockout-victim', 'password': 'incorrect'},
				format='json',
				REMOTE_ADDR=f'203.0.113.{index}',
			)

		response = client.post(
			'/api/auth/login/',
			{'username': 'lockout-victim', 'password': password},
			format='json',
			REMOTE_ADDR='198.51.100.9',
		)
		cache.clear()

		self.assertEqual(response.status_code, 200)

	def test_registration_rejects_case_and_unicode_duplicate_usernames(self):
		cache.clear()
		get_user_model().objects.create_user(
			username='dupe-user',
			password='Strong-Password-543!',
		)
		client = APIClient()
		responses = [
			client.post(
				'/api/auth/register/',
				{'username': username, 'password': 'Cobalt-River-Planets-482!'},
				format='json',
			)
			for username in ('Dupe-User', 'ｄupe-user')
		]
		cache.clear()

		self.assertEqual([r.status_code for r in responses], [400, 400])
		self.assertEqual(get_user_model().objects.count(), 1)

	def test_registration_integrity_error_returns_400(self):
		cache.clear()
		with patch.object(
			get_user_model().objects,
			'create_user',
			side_effect=IntegrityError,
		):
			response = APIClient().post(
				'/api/auth/register/',
				{'username': 'racing-user', 'password': 'Cobalt-River-Planets-482!'},
				format='json',
			)
		cache.clear()

		self.assertEqual(response.status_code, 400)
		self.assertIn('username', response.data)


class AdminLoginRateLimitTests(TestCase):
	def setUp(self):
		cache.clear()

	@override_settings(
		ADMIN_ALLOWED_NETWORKS=(ip_network('198.51.100.0/24'),),
	)
	def test_admin_login_is_limited_by_client_ip(self):
		client = APIClient()
		responses = [
			client.post(
				'/admin/login/',
				{'username': 'missing-admin', 'password': 'incorrect'},
				REMOTE_ADDR='198.51.100.92',
			)
			for _ in range(11)
		]

		self.assertEqual(
			[response.status_code for response in responses],
			[200] * 10 + [429],
		)
		self.assertEqual(responses[-1]['Retry-After'], '900')

	@override_settings(
		DEBUG=False,
		ADMIN_ALLOWED_NETWORKS=(ip_network('198.51.100.0/24'),),
	)
	def test_admin_paths_require_a_trusted_client_ip_in_production(self):
		client = APIClient()

		blocked = client.get('/admin/', REMOTE_ADDR='203.0.113.25')
		allowed = client.get('/admin/', REMOTE_ADDR='198.51.100.92')

		self.assertEqual(blocked.status_code, 404)
		self.assertNotEqual(allowed.status_code, 404)


class AdminAllowlistCheckTests(TestCase):
	def run_check(self):
		from .checks import check_admin_allowlist_sees_real_client_ip

		return check_admin_allowlist_sees_real_client_ip(None)

	@override_settings(
		DEBUG=False,
		ADMIN_ALLOWED_NETWORKS=(ip_network('127.0.0.1/32'),),
		REST_FRAMEWORK={**settings.REST_FRAMEWORK, 'NUM_PROXIES': 0},
	)
	def test_warns_when_loopback_is_allowed_without_trusted_proxies(self):
		self.assertEqual([w.id for w in self.run_check()], ['core.W001'])

	@override_settings(
		DEBUG=False,
		ADMIN_ALLOWED_NETWORKS=(ip_network('127.0.0.1/32'),),
		REST_FRAMEWORK={**settings.REST_FRAMEWORK, 'NUM_PROXIES': 1},
	)
	def test_no_warning_when_a_trusted_proxy_is_configured(self):
		self.assertEqual(self.run_check(), [])

	@override_settings(
		DEBUG=False,
		ADMIN_ALLOWED_NETWORKS=(ip_network('198.51.100.0/24'),),
		REST_FRAMEWORK={**settings.REST_FRAMEWORK, 'NUM_PROXIES': 0},
	)
	def test_no_warning_for_non_loopback_allowlist(self):
		self.assertEqual(self.run_check(), [])


class ThrottleCacheOutageTests(TestCase):
	def setUp(self):
		cache.clear()
		self.addCleanup(cache.clear)

	def test_general_api_stays_available_when_the_cache_is_down(self):
		user = get_user_model().objects.create_user(username='cache-outage-user')
		client = APIClient()
		client.force_authenticate(user=user)

		with patch.object(
			caches['default'].__class__,
			'add',
			side_effect=RedisError('down'),
		):
			response = client.get('/api/character/')

		self.assertEqual(response.status_code, 200)

	def test_login_fails_closed_when_the_cache_is_down(self):
		with patch.object(
			caches['default'].__class__,
			'add',
			side_effect=RedisError('down'),
		):
			response = APIClient().post(
				'/api/auth/login/',
				{'username': 'anyone', 'password': 'incorrect'},
				format='json',
			)

		self.assertEqual(response.status_code, 429)


class AtomicGeneralThrottleTests(TestCase):
	def test_authenticated_api_uses_the_atomic_general_throttle(self):
		cache.clear()
		user = get_user_model().objects.create_user(username='throttled-reader')
		client = APIClient()
		client.force_authenticate(user=user)

		with patch.object(
			AtomicUserRateThrottle,
			'THROTTLE_RATES',
			{'user': '1/hour'},
		):
			first = client.get('/api/character/')
			second = client.get('/api/character/')
		cache.clear()

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 429)


class CharacterSerializerPerformanceTests(TestCase):
	def test_character_stats_are_serialized_without_per_stat_queries(self):
		user = get_user_model().objects.create_user(username='stats-reader')
		for index in range(4):
			stat_type = StatType.objects.create(
				name=f'S{index}',
				display_name=f'Stat {index}',
			)
			Stat.objects.create(
				character=user.character,
				stat_type=stat_type,
				value=index,
			)
		client = APIClient()
		client.force_authenticate(user=user)

		with self.assertNumQueries(3):
			response = client.get('/api/character/')

		self.assertEqual(response.status_code, 200)
		self.assertEqual(len(response.data['stats']), 4)
