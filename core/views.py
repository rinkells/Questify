from django.db import transaction
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated

from .selectors import get_character
from .serializers import CharacterSerializer


class CharacterView(RetrieveUpdateAPIView):
	serializer_class = CharacterSerializer
	permission_classes = (IsAuthenticated,)

	def get_object(self):
		return get_character(
			self.request.user,
			for_update=self.request.method in ('PUT', 'PATCH'),
		)

	def update(self, request, *args, **kwargs):
		# Parse the body before opening the transaction: get_object() takes the
		# row lock, and a slow client must not hold it while the body trickles in.
		request.data
		with transaction.atomic():
			return super().update(request, *args, **kwargs)
