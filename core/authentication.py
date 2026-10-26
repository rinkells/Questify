from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import HashedAPIToken


class ExpiringTokenAuthentication(TokenAuthentication):
	"""Authenticate bearer tokens while storing only their SHA-256 digests."""

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
			raise AuthenticationFailed('User inactive or deleted.')
		return token.user, token

	def authenticate_header(self, request):
		return 'Token'
