from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Quest, QuestCategory
from .permissions import IsOwner
from .serializers import (
	QuestCategorySerializer,
	QuestCompleteResponseSerializer,
	QuestSerializer,
)
from .services import QuestService


class OwnerViewSetMixin:
	permission_classes = (IsAuthenticated, IsOwner)

	def get_queryset(self):
		queryset = super().get_queryset()
		if self.action in ('list', 'create'):
			return queryset.filter(user=self.request.user)
		return queryset


class QuestViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
	queryset = Quest.objects.all()
	serializer_class = QuestSerializer

	def perform_create(self, serializer):
		serializer.save(user=self.request.user)

	@extend_schema(responses=QuestCompleteResponseSerializer)
	@action(detail=True, methods=('post',))
	def complete(self, request, pk=None):
		self.get_object()
		result = QuestService.complete_quest(pk, request.user)
		return Response(QuestCompleteResponseSerializer(result).data)

	@action(detail=True, methods=('post',))
	def fail(self, request, pk=None):
		self.get_object()
		completion = QuestService.fail_quest(pk, request.user)
		return Response(
			{'id': completion.id, 'status': completion.status},
			status=status.HTTP_201_CREATED,
		)


class QuestCategoryViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
	queryset = QuestCategory.objects.all()
	serializer_class = QuestCategorySerializer

	def perform_create(self, serializer):
		serializer.save(user=self.request.user)
