from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth_serializers import UserRegistrationSerializer
from .authentication import ExpiringTokenAuthentication
from .models import HashedAPIToken
from .throttling import AtomicScopedRateThrottle


class RegisterView(generics.CreateAPIView):
	serializer_class = UserRegistrationSerializer
	permission_classes = (permissions.AllowAny,)
	authentication_classes = ()
	throttle_classes = (AtomicScopedRateThrottle,)
	throttle_scope = 'register'


class TokenLoginView(ObtainAuthToken):
	permission_classes = (permissions.AllowAny,)
	authentication_classes = ()
	throttle_classes = (AtomicScopedRateThrottle,)
	throttle_scope = 'login'

	def post(self, request, *args, **kwargs):
		serializer = self.get_serializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		user = serializer.validated_data['user']
		user_model = get_user_model()

		# A user-row lock makes token rotation safe when two logins race.
		with transaction.atomic():
			locked_user = user_model.objects.select_for_update().get(pk=user.pk)
			HashedAPIToken.objects.filter(user=locked_user).delete()
			token, raw_token = HashedAPIToken.issue_for_user(locked_user)

		return Response({
			'token': raw_token,
			'expires_at': token.created_at + timedelta(
				days=settings.AUTH_TOKEN_TTL_DAYS,
			),
		})


class TokenLogoutView(APIView):
	permission_classes = (permissions.IsAuthenticated,)
	authentication_classes = (ExpiringTokenAuthentication,)

	@extend_schema(request=None, responses={204: None})
	def post(self, request):
		request.auth.delete()
		return Response(status=status.HTTP_204_NO_CONTENT)
