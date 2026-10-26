import hashlib
import secrets

from django.conf import settings
from django.db import models

from .validators import validate_timezone_name


class Character(models.Model):
	user = models.OneToOneField(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='character',
	)
	level = models.PositiveIntegerField(default=1)
	xp = models.PositiveBigIntegerField(default=0)
	xp_to_next_level = models.PositiveBigIntegerField(default=100)
	timezone = models.CharField(
		max_length=64,
		default='UTC',
		validators=[validate_timezone_name],
	)

	@classmethod
	def locked_for_user_id(cls, user_id):
		"""Return the user's row locked FOR UPDATE, recreating it if it is missing."""
		cls.objects.get_or_create(user_id=user_id)
		return cls.objects.select_for_update().get(user_id=user_id)

	def __str__(self):
		return f'{self.user} character'


class HashedAPIToken(models.Model):
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='api_tokens',
	)
	token_digest = models.CharField(max_length=64, unique=True, editable=False)
	# Django's session-auth hash of the owner's password when the token was
	# issued; a later password change makes the stored value stop matching.
	auth_hash = models.CharField(max_length=64, editable=False, default='')
	created_at = models.DateTimeField(auto_now_add=True)

	@staticmethod
	def digest(raw_token):
		return hashlib.sha256(raw_token.encode('ascii')).hexdigest()

	@classmethod
	def issue_for_user(cls, user):
		raw_token = secrets.token_urlsafe(32)
		token = cls.objects.create(
			user=user,
			token_digest=cls.digest(raw_token),
			auth_hash=user.get_session_auth_hash(),
		)
		return token, raw_token

	def __str__(self):
		return f'API token for {self.user_id}'


class StatType(models.Model):
	name = models.CharField(max_length=10, unique=True)
	display_name = models.CharField(max_length=100)
	icon = models.CharField(max_length=100, blank=True)

	def __str__(self):
		return self.display_name


class Stat(models.Model):
	character = models.ForeignKey(
		Character,
		on_delete=models.CASCADE,
		related_name='stats',
	)
	stat_type = models.ForeignKey(
		StatType,
		on_delete=models.CASCADE,
		related_name='stats',
	)
	value = models.BigIntegerField(default=0)

	class Meta:
		unique_together = ('character', 'stat_type')

	def __str__(self):
		return f'{self.character}: {self.stat_type} ({self.value})'
