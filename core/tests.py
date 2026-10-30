from datetime import timedelta
import hashlib
import io
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from django.db import IntegrityError, migrations
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from redis.exceptions import RedisError
from rest_framework.test import APIClient

from .models import HashedAPIToken, Stat, StatType
from .rollback_guards import refuse_unsafe_rollbacks
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


class HardeningTests(TestCase):
	password = 'Cobalt-River-Planets-482!'

	def setUp(self):
		cache.clear()
		self.addCleanup(cache.clear)

	def test_usernames_are_unique_case_insensitively_in_the_database(self):
		user_model = get_user_model()
		user_model.objects.create_user(username='Bob', password=self.password)

		with self.assertRaises(IntegrityError):
			user_model.objects.create_user(username='bob', password=self.password)

	def test_oversized_passwords_are_rejected(self):
		client = APIClient()
		register = client.post(
			'/api/auth/register/',
			{'username': 'long-pw', 'password': 'a1B!' * 200},
			format='json',
		)
		login = client.post(
			'/api/auth/login/',
			{'username': 'long-pw', 'password': 'a' * 600},
			format='json',
		)

		self.assertEqual(register.status_code, 400)
		self.assertEqual(login.status_code, 400)

	def test_deeply_nested_json_is_a_400_not_a_500(self):
		response = APIClient().generic(
			'POST',
			'/api/auth/login/',
			'[' * 100_000 + ']' * 100_000,
			content_type='application/json',
		)

		self.assertEqual(response.status_code, 400)

	def test_expired_token_is_indistinguishable_from_an_unknown_one(self):
		user = get_user_model().objects.create_user(
			username='stale-token', password=self.password,
		)
		token, raw_token = HashedAPIToken.issue_for_user(user)
		HashedAPIToken.objects.filter(pk=token.pk).update(
			created_at=timezone.now() - timedelta(days=settings.AUTH_TOKEN_TTL_DAYS + 1),
		)
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')

		response = client.get('/api/character/')

		self.assertEqual(response.status_code, 401)
		self.assertEqual(str(response.data['detail']), 'Invalid token.')

	def test_login_accepts_any_case_of_the_username(self):
		get_user_model().objects.create_user('Erin', password=self.password)

		response = APIClient().post(
			'/api/auth/login/',
			{'username': 'erin', 'password': self.password},
			format='json',
		)

		self.assertEqual(response.status_code, 200)

	def test_retry_after_reports_the_time_left_in_the_window(self):
		# A different username per attempt, so only the per-IP counter blocks.
		client = APIClient()
		for attempt in range(6):
			response = client.post(
				'/api/auth/login/',
				{'username': f'nobody-{attempt}', 'password': 'wrong'},
				format='json',
			)
		self.assertEqual(response.status_code, 429)
		redis_key = cache.make_and_validate_key('throttle_login_ip:127.0.0.1')
		cache._cache.get_client(redis_key).expire(redis_key, 7)

		response = client.post(
			'/api/auth/login/',
			{'username': 'nobody-last', 'password': 'wrong'},
			format='json',
		)

		self.assertEqual(response.status_code, 429)
		self.assertLessEqual(int(response['Retry-After']), 7)

	def test_suite_uses_its_own_redis_database(self):
		self.assertTrue(settings.CACHES['default']['LOCATION'].endswith('/15'))

	def test_registration_does_not_reveal_whether_an_email_has_an_account(self):
		get_user_model().objects.create_user('known', 'known@example.com', self.password)
		client = APIClient()
		response = client.post(
			'/api/auth/register/',
			{'username': 'probe', 'email': 'KNOWN@example.com', 'password': self.password},
			format='json',
		)

		self.assertEqual(response.status_code, 201)

	def test_password_change_revokes_existing_tokens(self):
		user = get_user_model().objects.create_user('rotator', password=self.password)
		_, raw_token = HashedAPIToken.issue_for_user(user)
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')
		self.assertEqual(client.get('/api/character/').status_code, 200)

		user.set_password('An0ther-Str0ng-pass-71')
		user.save()

		self.assertEqual(client.get('/api/character/').status_code, 401)
		self.assertFalse(HashedAPIToken.objects.filter(user=user).exists())

	def test_token_survives_unrelated_user_changes(self):
		user = get_user_model().objects.create_user('steady', password=self.password)
		_, raw_token = HashedAPIToken.issue_for_user(user)
		user.first_name = 'Steady'
		user.save()
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')

		self.assertEqual(client.get('/api/character/').status_code, 200)

	def test_api_responses_are_not_cacheable(self):
		get_user_model().objects.create_user('cachey', password=self.password)
		response = APIClient().post(
			'/api/auth/login/',
			{'username': 'cachey', 'password': self.password},
			format='json',
		)

		self.assertEqual(response.status_code, 200)
		self.assertIn('no-store', response['Cache-Control'])

	def test_missing_trailing_slash_is_a_plain_404(self):
		response = APIClient().post('/api/auth/login', {}, format='json')

		self.assertEqual(response.status_code, 404)

	def test_character_update_parses_the_body_before_taking_the_row_lock(self):
		user = get_user_model().objects.create_user('slow', password=self.password)
		client = APIClient()
		client.force_authenticate(user)
		events = []

		from core import views
		original_get_object = views.CharacterView.get_object

		def recording_get_object(view):
			events.append('lock')
			return original_get_object(view)

		with patch.object(views.CharacterView, 'get_object', recording_get_object), 				patch('rest_framework.request.Request._parse', autospec=True,
				      side_effect=lambda req: (events.append('parse'), ({}, {}))[1]):
			client.patch('/api/character/', {}, format='json')

		self.assertEqual(events[:2], ['parse', 'lock'])

	def test_multipart_uploads_are_not_accepted(self):
		response = APIClient().post(
			'/api/auth/login/',
			{'username': 'a', 'password': 'b', 'upload': io.BytesIO(b'z' * 1024)},
			format='multipart',
		)

		self.assertEqual(response.status_code, 415)

	def test_oversized_request_bodies_are_rejected(self):
		body = '{"username": "a", "password": "b", "pad": "' + 'x' * 300_000 + '"}'

		response = APIClient().generic(
			'POST', '/api/auth/login/', body, content_type='application/json',
		)

		self.assertEqual(response.status_code, 413)
		self.assertEqual(response.json(), {'detail': 'Request body too large.'})

	def test_throttled_clients_bodies_are_not_parsed(self):
		client = APIClient()
		for attempt in range(5):
			client.post(
				'/api/auth/login/',
				{'username': f'nobody-{attempt}', 'password': 'wrong'},
				format='json',
			)

		with patch('rest_framework.request.Request._parse') as parse:
			response = client.post(
				'/api/auth/login/',
				{'username': 'nobody-x', 'password': 'wrong'},
				format='json',
			)

		self.assertEqual(response.status_code, 429)
		parse.assert_not_called()


class ConcurrentRegistrationTests(TransactionTestCase):
	password = 'Cobalt-River-Planets-482!'

	def setUp(self):
		cache.clear()
		self.addCleanup(cache.clear)

	def test_parallel_registrations_differing_only_in_case_create_one_account(self):
		import threading
		from django.db import connection

		barrier = threading.Barrier(2)
		statuses = []

		def register(name):
			try:
				barrier.wait()
				response = APIClient().post(
					'/api/auth/register/',
					{'username': name, 'password': self.password},
					format='json',
				)
				statuses.append(response.status_code)
			finally:
				connection.close()

		threads = [threading.Thread(target=register, args=(n,)) for n in ('Racer', 'racer')]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()

		self.assertEqual(
			get_user_model().objects.filter(username__iexact='racer').count(),
			1,
		)
		self.assertEqual(sorted(statuses), [201, 400])

	def test_parallel_registrations_equal_only_under_upper_create_one_account(self):
		import threading
		from django.db import connection

		barrier = threading.Barrier(2)
		statuses = []

		def register(name):
			try:
				barrier.wait()
				response = APIClient().post(
					'/api/auth/register/',
					{'username': name, 'password': self.password},
					format='json',
				)
				statuses.append(response.status_code)
			finally:
				connection.close()

		# 'ı' (dotless i) upper-cases to 'I', so username__iexact sees one
		# name, but LOWER() keeps them apart.
		names = ('audit-race', 'aud\u0131t-race')
		threads = [threading.Thread(target=register, args=(n,)) for n in names]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()

		self.assertEqual(
			get_user_model().objects.filter(username__iexact='audit-race').count(),
			1,
		)
		self.assertEqual(sorted(statuses), [201, 400])


class AuthAuditFixTests(TestCase):
	def setUp(self):
		cache.clear()
		self.addCleanup(cache.clear)

	def _client(self, user):
		_, raw_token = HashedAPIToken.issue_for_user(user)
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')
		return client

	def test_host_dependent_timezone_names_are_rejected(self):
		user = get_user_model().objects.create_user(username='tz-localtime')

		response = self._client(user).patch(
			'/api/character/', {'timezone': 'localtime'}, format='json',
		)

		self.assertEqual(response.status_code, 400)

	def test_a_failed_quest_also_blocks_timezone_changes_for_24_hours(self):
		from quests.models import Quest, QuestCompletion

		user = get_user_model().objects.create_user(username='tz-failed')
		quest = Quest.objects.create(
			user=user, title='q', quest_type='daily', xp_reward=25,
		)
		QuestCompletion.objects.create(
			user=user, quest=quest, status='failed', xp_awarded=0,
		)

		response = self._client(user).patch(
			'/api/character/', {'timezone': 'Asia/Bangkok'}, format='json',
		)

		self.assertEqual(response.status_code, 400)
		self.assertIn('timezone', response.json())

	def test_repeated_bad_tokens_are_throttled_per_ip(self):
		with patch.dict(
			'rest_framework.throttling.SimpleRateThrottle.THROTTLE_RATES',
			{'auth_failure': '3/minute'},
		):
			client = APIClient()
			codes = []
			for attempt in range(5):
				client.credentials(HTTP_AUTHORIZATION=f'Token bad-{attempt}')
				codes.append(client.get('/api/character/').status_code)

		self.assertEqual(codes, [401, 401, 401, 429, 429])

	def test_requests_without_credentials_do_not_spend_the_failure_budget(self):
		with patch.dict(
			'rest_framework.throttling.SimpleRateThrottle.THROTTLE_RATES',
			{'auth_failure': '2/minute'},
		):
			codes = [
				APIClient().get('/api/character/').status_code for _ in range(5)
			]

		self.assertEqual(codes, [401] * 5)

	@override_settings(
		ADMIN_ALLOWED_NETWORKS=(ip_network('198.51.100.0/24'),),
	)
	def test_admin_login_fails_closed_when_the_cache_is_down(self):
		with patch.object(
			caches['default'].__class__,
			'add',
			side_effect=RedisError('down'),
		):
			response = APIClient().post(
				'/admin/login/',
				{'username': 'admin', 'password': 'incorrect'},
				REMOTE_ADDR='198.51.100.92',
			)

		self.assertEqual(response.status_code, 503)


class SecurityAuditFixTests(TestCase):
	password = 'Cobalt-River-Planets-482!'

	def setUp(self):
		cache.clear()
		self.addCleanup(cache.clear)

	def _login(self, username, remote_addr='198.51.100.50'):
		return APIClient().post(
			'/api/auth/login/',
			{'username': username, 'password': 'incorrect'},
			format='json',
			REMOTE_ADDR=remote_addr,
		)

	def test_usernames_equal_under_upper_are_unique_in_the_database(self):
		user_model = get_user_model()
		user_model.objects.create_user(username='aud\u0131t-db', password=self.password)

		with self.assertRaises(IntegrityError):
			user_model.objects.create_user(username='audit-db', password=self.password)

	def test_login_prefers_the_exact_username_over_another_upper_match(self):
		from django.db import connection

		# Rows from before migration 0010, which refuses to run while such
		# pairs exist; the test's transaction restores the index.
		with connection.cursor() as cursor:
			cursor.execute('DROP INDEX auth_user_username_upper_uniq')
		user_model = get_user_model()
		user_model.objects.create_user(username='aud\u0131t-x', password='Other-Pass-777!')
		user_model.objects.create_user(username='audit-x', password=self.password)

		response = APIClient().post(
			'/api/auth/login/',
			{'username': 'audit-x', 'password': self.password},
			format='json',
		)

		self.assertEqual(response.status_code, 200)

	def test_inactive_users_token_is_indistinguishable_and_revoked(self):
		user = get_user_model().objects.create_user(username='inactive-token')
		_, raw_token = HashedAPIToken.issue_for_user(user)
		user.is_active = False
		user.save(update_fields=('is_active',))
		client = APIClient()
		client.credentials(HTTP_AUTHORIZATION=f'Token {raw_token}')

		response = client.get('/api/character/')

		self.assertEqual(response.status_code, 401)
		self.assertEqual(response.json(), {'detail': 'Invalid token.'})
		self.assertFalse(HashedAPIToken.objects.filter(user=user).exists())

	def test_overflowing_json_numbers_are_a_400_not_a_500(self):
		user = get_user_model().objects.create_user(username='overflow')
		client = APIClient()
		client.force_authenticate(user=user)

		responses = [
			client.generic(
				'POST',
				'/api/quests/',
				'{"title": "t", "quest_type": "daily", "category": %s}' % number,
				content_type='application/json',
			)
			for number in ('1e999', '-1e999')
		]

		self.assertEqual([r.status_code for r in responses], [400, 400])
		self.assertIn('out of range', responses[0].json()['detail'])

	def test_ipv6_addresses_in_one_64_share_a_login_bucket(self):
		codes = [
			self._login('ipv6-user', f'2001:db8:1:2::{index:x}').status_code
			for index in range(1, 7)
		]

		self.assertEqual(codes, [400, 400, 400, 400, 400, 429])

	def test_ipv4_mapped_addresses_share_the_ipv4_bucket(self):
		codes = [
			self._login('mapped-user', address).status_code
			for address in ['198.51.100.60', '::ffff:198.51.100.60'] * 3
		]

		self.assertEqual(codes, [400, 400, 400, 400, 400, 429])

	def test_login_network_ceiling_refuses_only_new_networks(self):
		with patch.dict(
			'rest_framework.throttling.SimpleRateThrottle.THROTTLE_RATES',
			{'login_networks': '3/hour'},
		):
			first = [
				self._login('targeted', f'203.0.113.{index}').status_code
				for index in range(1, 4)
			]
			new_network = self._login('targeted', '203.0.113.4')
			known_network = self._login('targeted', '203.0.113.1')
			other_account = self._login('someone-else', '203.0.113.4')

		self.assertEqual(first, [400, 400, 400])
		self.assertEqual(new_network.status_code, 429)
		self.assertIn('Retry-After', new_network)
		self.assertEqual(known_network.status_code, 400)
		self.assertEqual(other_account.status_code, 400)

	def test_login_network_ceiling_counts_every_spelling_of_one_account(self):
		# Login NFKC-normalizes the name and matches it case-insensitively, so
		# all of these try the password of the same account.
		get_user_model().objects.create_user(username='Spelled', password=self.password)
		fullwidth = 'ｓｐｅｌｌｅｄ'
		bold = '\U0001d42c\U0001d429\U0001d41e\U0001d425\U0001d425\U0001d41e\U0001d41d'
		with patch.dict(
			'rest_framework.throttling.SimpleRateThrottle.THROTTLE_RATES',
			{'login_networks': '3/hour'},
		):
			lowercase = self._login('spelled', '203.0.113.1')
			uppercase = self._login('SPELLED', '203.0.113.2')
			correct = APIClient().post(
				'/api/auth/login/',
				{'username': fullwidth, 'password': self.password},
				format='json',
				REMOTE_ADDR='203.0.113.3',
			)
			fourth_network = self._login(bold, '203.0.113.4')

		self.assertEqual([lowercase.status_code, uppercase.status_code], [400, 400])
		self.assertEqual(correct.status_code, 200)
		self.assertEqual(fourth_network.status_code, 429)

	def test_login_with_unusable_username_is_a_400_not_a_500(self):
		# The throttle reads the name before the serializer rejects it.
		for username in ('nul\x00name', 'x' * 10_000, ['list'], 12345):
			with self.subTest(username=repr(username)[:20]):
				self.assertEqual(self._login(username).status_code, 400)
				cache.clear()

	def test_login_network_ceiling_fails_closed_when_the_cache_is_down(self):
		calls = []
		real_add = caches['default'].__class__.add

		def add(cache_self, key, *args, **kwargs):
			calls.append(key)
			if 'account-network:' in key:
				raise RedisError('down')
			return real_add(cache_self, key, *args, **kwargs)

		with patch.object(caches['default'].__class__, 'add', add):
			response = self._login('cache-down')

		self.assertEqual(response.status_code, 429)
		self.assertTrue(any('account-network:' in key for key in calls))


class RollbackGuardTests(TestCase):
	def test_guards_of_reverted_migrations_run_first_in_plan_order(self):
		calls = []

		def guard(name):
			return lambda state, schema_editor: calls.append((name, state))

		plan = [
			(SimpleNamespace(rollback_guards=(guard('applied'),)), False),
			(SimpleNamespace(rollback_guards=(guard('first'), guard('second'))), True),
			(SimpleNamespace(), True),
			(SimpleNamespace(rollback_guards=(guard('third'),)), True),
		]

		refuse_unsafe_rollbacks(None, plan=plan, apps='state', using='default')

		self.assertEqual(calls, [
			('first', 'state'),
			('second', 'state'),
			('third', 'state'),
		])

	def test_a_refusal_stops_before_the_later_guards(self):
		def refuse(state, schema_editor):
			raise RuntimeError('refused')

		later = []
		plan = [
			(SimpleNamespace(rollback_guards=(refuse,)), True),
			(SimpleNamespace(rollback_guards=(lambda *args: later.append(args),)), True),
		]

		with self.assertRaisesMessage(RuntimeError, 'refused'):
			refuse_unsafe_rollbacks(None, plan=plan, apps=None, using='default')
		self.assertEqual(later, [])

	def test_every_reverse_only_guard_is_listed_in_rollback_guards(self):
		# A guard missing from the list would run only at its own step, after
		# the migrations reverted before it had committed.
		# Only this project's apps: Django's own reverse-only RunPython
		# operations change data.
		project_apps = {
			config.label
			for config in apps.get_app_configs()
			if Path(config.path).parent == settings.BASE_DIR
		}
		loader = MigrationLoader(None, ignore_no_migrations=True)
		checked = []
		for key, migration in loader.disk_migrations.items():
			if key[0] not in project_apps:
				continue
			for operation in migration.operations:
				if not isinstance(operation, migrations.RunPython):
					continue
				if operation.code is not migrations.RunPython.noop:
					continue
				checked.append(key)
				self.assertIn(
					operation.reverse_code,
					getattr(migration, 'rollback_guards', ()),
					f'{key} has a rollback guard missing from rollback_guards',
				)
		self.assertIn(('wallet', '0003_daily_bonus_transactions'), checked)
		self.assertIn(('wallet', '0004_boss_reward_transactions'), checked)
