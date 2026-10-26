from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.db import connections
from django.db.utils import DatabaseError
from rest_framework.settings import api_settings


@register(Tags.database)
def check_postgresql_version(app_configs, **kwargs):
	"""Require a PostgreSQL version that enforces NULL-aware uniqueness."""
	errors = []
	for alias in kwargs.get('databases') or ():
		connection = connections[alias]
		if connection.vendor != 'postgresql':
			errors.append(Error(
				'Questify requires PostgreSQL 15 or newer.',
				hint=f'Database alias "{alias}" uses {connection.vendor}.',
				id='core.E001',
			))
			continue
		try:
			version = connection.pg_version
		except DatabaseError as exc:
			errors.append(Error(
				'Could not determine the PostgreSQL server version.',
				hint=str(exc),
				id='core.E002',
			))
			continue
		if version < 150000:
			errors.append(Error(
				'Questify requires PostgreSQL 15 or newer.',
				hint=f'Database alias "{alias}" is PostgreSQL {version // 10000}.',
				id='core.E003',
			))
	return errors


@register(Tags.security)
def check_admin_allowlist_sees_real_client_ip(app_configs, **kwargs):
	"""Warn when a loopback allow-list cannot tell real clients from the proxy.

	With no trusted proxy configured the client address is REMOTE_ADDR. Behind
	a same-host reverse proxy that is always loopback, so an allow-list that
	contains loopback admits every client the proxy forwards.
	"""
	if settings.DEBUG or api_settings.NUM_PROXIES != 0:
		return []
	if not any(
		network.is_loopback
		for network in settings.ADMIN_ALLOWED_NETWORKS
	):
		return []
	return [Warning(
		'ADMIN_ALLOWED_IPS contains a loopback address while DRF_NUM_PROXIES is 0.',
		hint=(
			'Behind a same-host reverse proxy every request appears to come '
			'from loopback, so the allow-list admits all clients. Set '
			'DRF_NUM_PROXIES to the number of trusted proxies, or replace '
			'loopback with the real admin IPs.'
		),
		id='core.W001',
	)]
