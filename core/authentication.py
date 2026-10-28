from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from rest_framework.authentication import TokenAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, Throttled

from .models import HashedAPIToken
from .throttling import AuthFailureThrottle


class ExpiringTokenAuthentication(TokenAuthentication):
	"""Authenticate bearer tokens while storing only their SHA-256 digests."""

	def authenticate(self, request):
		header = get_authorization_header(request).split()
		if not header or header[0].lower() != self.keyword.lower().encode():
			return super().authenticate(request)
		# Bad tokens never reach the anon/user throttles (authentication runs
		# first), so cap them per IP here, before touching the database.
		throttle = AuthFailureThrottle()
		if throttle.is_blocked(request):
			raise Throttled(wait=throttle.wait())
		try:
			return super().authenticate(request)
		except AuthenticationFailed:
			throttle.record_failure(request)
			raise

	def authenticate_credentials(self, raw_token):
		try:
			digest = HashedAPIToken.digest(raw_token)
		except UnicodeEncodeError as exc:
			raise AuthenticationFailed('Invalid token.') from exc
		try:
			token = HashedAPIToken.objects.select_related('user').get(
				token_digest=digest,
			)
		except HashedAPIToken.DoesNotExist as exc:
			raise AuthenticationFailed('Invalid token.') from exc

		expires_at = token.created_at + timedelta(
			days=settings.AUTH_TOKEN_TTL_DAYS,
		)
		if expires_at <= timezone.now():
			token.delete()
			# Same message as an unknown token, so a stale token reveals nothing.
			raise AuthenticationFailed('Invalid token.')
		if not token.user.is_active:
			# Same answer as an unknown token, so a token reveals nothing
			# about the account's state; reactivation needs a new login.
			token.delete()
			raise AuthenticationFailed('Invalid token.')
		# A password change or reset must cut off tokens issued before it.
		if not constant_time_compare(
			token.auth_hash,
			token.user.get_session_auth_hash(),
		):
			token.delete()
			raise AuthenticationFailed('Invalid token.')
		return token.user, token

	def authenticate_header(self, request):
		return 'Token'
