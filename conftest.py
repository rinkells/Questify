import pytest
from django.conf import settings
from django.test import override_settings


# Redis database reserved for the test suite. Tests call cache.clear(), which
# must never wipe the live server's throttle counters in the shared database.
TEST_REDIS_DB = 15


@pytest.fixture(autouse=True, scope='session')
def isolated_test_cache():
	cache_config = settings.CACHES['default']
	location = cache_config.get('LOCATION', '')
	if not cache_config['BACKEND'].endswith('RedisCache') or not location:
		yield
		return

	test_location = f"{location.rsplit('/', 1)[0]}/{TEST_REDIS_DB}"
	with override_settings(
		CACHES={'default': {**cache_config, 'LOCATION': test_location}},
	):
		yield
