import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def healthz(request):
	"""Report readiness without exposing dependency or configuration details."""
	if not settings.DEBUG and not secrets.compare_digest(
		# compare_digest() raises TypeError for non-ASCII str, so compare bytes.
		request.headers.get('X-Healthcheck-Token', '').encode('utf-8'),
		settings.HEALTHCHECK_TOKEN.encode('utf-8'),
	):
		return JsonResponse({'status': 'not found'}, status=404)

	try:
		with connections['default'].cursor() as cursor:
			cursor.execute('SELECT 1')
		cache.set('questify-health-check', 'ok', timeout=5)
		if cache.get('questify-health-check') != 'ok':
			raise RuntimeError('Cache is not responding')
	except Exception:
		return JsonResponse({'status': 'unavailable'}, status=503)
	return JsonResponse({'status': 'ok'})
