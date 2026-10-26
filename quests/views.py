from django.conf import settings
from drf_spectacular.utils import extend_schema
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Character

from .models import Quest, QuestCategory, QuestCompletion
from .permissions import IsOwner
from .serializers import (
	QuestCategorySerializer,
	QuestCompleteResponseSerializer,
	QuestSerializer,
)
from .services import (
	QuestAlreadyCompletedError,
	QuestAlreadyFailedError,
	QuestService,
)


class OwnerViewSetMixin:
	permission_classes = (IsAuthenticated, IsOwner)

	def get_queryset(self):
		return super().get_queryset().filter(
			user=self.request.user,
		).order_by('pk')


class QuestViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
	queryset = Quest.objects.all()
	serializer_class = QuestSerializer
	XP_REWARD_BY_TYPE = {
		Quest.QuestType.DAILY: 25,
		Quest.QuestType.HABIT: 25,
		Quest.QuestType.ONE_TIME: 50,
	}

	def get_queryset(self):
		return super().get_queryset().filter(archived_at__isnull=True)

	def get_locked_object(self):
		lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
		queryset = self.filter_queryset(
			self.get_queryset().select_for_update(),
		)
		quest = get_object_or_404(
			queryset,
			**{lookup_url_kwarg: self.kwargs[lookup_url_kwarg]},
		)
		self.check_object_permissions(self.request, quest)
		return quest

	@transaction.atomic
	def perform_create(self, serializer):
		Character.locked_for_user_id(self.request.user.pk)
		if Quest.objects.filter(user=self.request.user).count() >= (
			settings.QUESTIFY_MAX_QUESTS_PER_USER
		):
			raise DRFValidationError({
				'detail': 'Quest limit reached for this account.',
			})
		quest_type = serializer.validated_data['quest_type']
		serializer.save(
			user=self.request.user,
			xp_reward=self.XP_REWARD_BY_TYPE[quest_type],
		)

	@transaction.atomic
	def perform_update(self, serializer):
		quest = get_object_or_404(
			Quest.objects.select_for_update().filter(
				user=self.request.user,
				archived_at__isnull=True,
			),
			pk=serializer.instance.pk,
		)
		new_type = serializer.validated_data.get('quest_type', quest.quest_type)
		if new_type != quest.quest_type and QuestCompletion.objects.filter(
			quest=quest,
		).exists():
			raise DRFValidationError({
				'quest_type': 'Quest type cannot change after it has a completion.',
			})

		serializer.instance = quest
		if new_type != quest.quest_type:
			serializer.save(xp_reward=self.XP_REWARD_BY_TYPE[new_type])
		else:
			serializer.save()

	@transaction.atomic
	def perform_destroy(self, instance):
		quest = get_object_or_404(
			Quest.objects.select_for_update().filter(
				user=self.request.user,
				archived_at__isnull=True,
			),
			pk=instance.pk,
		)
		quest.is_active = False
		quest.archived_at = timezone.now()
		quest.save(update_fields=('is_active', 'archived_at', 'updated_at'))

	@extend_schema(responses=QuestCompleteResponseSerializer)
	@action(detail=True, methods=('post',))
	def complete(self, request, pk=None):
		try:
			with transaction.atomic():
				quest = self.get_locked_object()
				result = QuestService.complete_quest(
					quest.pk,
					request.user,
					locked_quest=quest,
				)
		except (
			QuestAlreadyCompletedError,
			QuestAlreadyFailedError,
			DjangoValidationError,
		) as exc:
			message = exc.messages if isinstance(exc, DjangoValidationError) else str(exc)
			return Response(
				{'detail': message},
				status=status.HTTP_400_BAD_REQUEST,
			)
		return Response(QuestCompleteResponseSerializer(result).data)

	@action(detail=True, methods=('post',))
	def fail(self, request, pk=None):
		try:
			with transaction.atomic():
				quest = self.get_locked_object()
				completion = QuestService.fail_quest(
					quest.pk,
					request.user,
					locked_quest=quest,
				)
		except (
			QuestAlreadyCompletedError,
			QuestAlreadyFailedError,
			DjangoValidationError,
		) as exc:
			message = exc.messages if isinstance(exc, DjangoValidationError) else str(exc)
			return Response(
				{'detail': message},
				status=status.HTTP_400_BAD_REQUEST,
			)
		return Response(
			{'id': completion.id, 'status': completion.status},
			status=status.HTTP_201_CREATED,
		)


class QuestCategoryViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
	queryset = QuestCategory.objects.all()
	serializer_class = QuestCategorySerializer

	@transaction.atomic
	def perform_create(self, serializer):
		Character.locked_for_user_id(self.request.user.pk)
		if QuestCategory.objects.filter(user=self.request.user).count() >= (
			settings.QUESTIFY_MAX_CATEGORIES_PER_USER
		):
			raise DRFValidationError({
				'detail': 'Category limit reached for this account.',
			})
		serializer.save(user=self.request.user)

	@transaction.atomic
	def perform_update(self, serializer):
		category = QuestCategory.objects.select_for_update().get(
			pk=serializer.instance.pk,
			user=self.request.user,
		)
		serializer.instance = category
		serializer.save()
