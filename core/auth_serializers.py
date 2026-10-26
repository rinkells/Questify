from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.authtoken.serializers import AuthTokenSerializer


User = get_user_model()

# Bound the work done hashing/validating a password supplied by a client.
MAX_PASSWORD_LENGTH = 128


class LoginSerializer(AuthTokenSerializer):
	username = serializers.CharField(write_only=True, max_length=150)
	# Looser than the registration limit so accounts created before that limit
	# existed can still log in; this only bounds the hashing work per request.
	password = serializers.CharField(
		write_only=True,
		trim_whitespace=False,
		max_length=4 * MAX_PASSWORD_LENGTH,
	)

	def validate(self, attrs):
		# Usernames are unique case-insensitively (core migration 0006), so
		# "erin" unambiguously means the account stored as "Erin".
		stored_username = User.objects.filter(
			username__iexact=User.normalize_username(attrs.get('username', '')),
		).values_list('username', flat=True).first()
		if stored_username is not None:
			attrs['username'] = stored_username
		return super().validate(attrs)


class UserRegistrationSerializer(serializers.ModelSerializer):
	password = serializers.CharField(
		write_only=True,
		trim_whitespace=False,
		max_length=MAX_PASSWORD_LENGTH,
	)

	class Meta:
		model = User
		fields = ('id', 'username', 'email', 'password')
		read_only_fields = ('id',)

	def validate_password(self, password):
		candidate_user = User(
			username=self.initial_data.get('username', ''),
			email=self.initial_data.get('email', ''),
		)
		try:
			password_validation.validate_password(password, user=candidate_user)
		except DjangoValidationError as exc:
			raise serializers.ValidationError(exc.messages) from exc
		return password

	def validate_username(self, username):
		# create_user() NFKC-normalizes usernames, so compare the normalized
		# form, and compare case-insensitively to block lookalike accounts.
		username = User.normalize_username(username)
		if User.objects.filter(username__iexact=username).exists():
			raise serializers.ValidationError(
				'A user with that username already exists.',
			)
		return username

	def create(self, validated_data):
		try:
			with transaction.atomic():
				return User.objects.create_user(**validated_data)
		except IntegrityError as exc:
			# Two concurrent registrations can both pass validation.
			raise serializers.ValidationError({
				'username': ['A user with that username already exists.'],
			}) from exc
