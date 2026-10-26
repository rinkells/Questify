from django.db import transaction
from django.db.models import Prefetch
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated

from .models import Character, Stat
from .serializers import CharacterSerializer


class CharacterView(RetrieveUpdateAPIView):
	serializer_class = CharacterSerializer
	permission_classes = (IsAuthenticated,)

	def get_object(self):
		queryset = Character.objects.select_related('user').prefetch_related(
			Prefetch(
				'stats',
				queryset=Stat.objects.select_related('stat_type'),
			),
			Prefetch(
				'user__streaks',
				to_attr='prefetched_streaks',
			),
		)
		if self.request.method in ('PUT', 'PATCH'):
			queryset = queryset.select_for_update(of=('self',))
		try:
			return queryset.get(user=self.request.user)
		except Character.DoesNotExist:
			# Recreate a missing character instead of failing every request.
			Character.objects.get_or_create(user=self.request.user)
			return queryset.get(user=self.request.user)

	@transaction.atomic
	def update(self, request, *args, **kwargs):
		return super().update(request, *args, **kwargs)
