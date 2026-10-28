from django.conf import settings
from drf_spectacular.utils import extend_schema
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework import status, viewsets
# DRF's version also turns a malformed id (ValueError/TypeError) into a
# 404; Django's raises it, a 500 on /api/quests/abc/complete/.
from rest_framework.generics import get_object_or_404
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Character
from core.serializers import DetailSerializer

from .models import Quest, QuestCategory, QuestCompletion
from .permissions import IsOwner
from .serializers import (
	DailyBonusStatusSerializer,
	QuestCategorySerializer,
	QuestCompleteResponseSerializer,
	QuestFailResponseSerializer,
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

	def get_locked_object(self):
		"""get_object(), but with the row locked FOR UPDATE."""
		lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
		queryset = self.filter_queryset(
			self.get_queryset().select_for_update(),
		)
		obj = get_object_or_404(
			queryset,
			**{lookup_url_kwarg: self.kwargs[lookup_url_kwarg]},
		)
		self.check_object_permissions(self.request, obj)
		return obj


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

	def _lock_category(self, serializer):
		"""Lock the chosen category so a concurrent DELETE cannot orphan the FK.

		The serializer validated the category before this transaction began;
		without the lock a category deleted in between surfaces as a 500.
		"""
		category = serializer.validated_data.get('category')
		if category is None:
			return
		locked = QuestCategory.objects.select_for_update().filter(
			pk=category.pk,
			user=self.request.user,
		).first()
		if locked is None:
			raise DRFValidationError({
				'category': 'This category no longer exists.',
			})

	@transaction.atomic
	def perform_create(self, serializer):
		Character.locked_for_user_id(self.request.user.pk)
		if Quest.objects.filter(user=self.request.user).count() >= (
			settings.QUESTIFY_MAX_QUESTS_PER_USER
		):
			raise DRFValidationError({
				'detail': 'Quest limit reached for this account.',
			})
		self._lock_category(serializer)
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
		if (
			'deadline' in serializer.validated_data
			and quest.deadline is not None
			and quest.deadline < timezone.now()
			and serializer.validated_data['deadline'] != quest.deadline
		):
			raise DRFValidationError({
				'deadline': 'The deadline has passed and cannot be changed.',
			})
		self._lock_category(serializer)
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

	# Neither action reads a body; without request=None the schema would ask
	# for the viewset's QuestSerializer.
	@extend_schema(
		request=None,
		responses={
			200: QuestCompleteResponseSerializer,
			400: DetailSerializer,
			404: DetailSerializer,
		},
	)
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
			message = (
				' '.join(exc.messages)
				if isinstance(exc, DjangoValidationError)
				else str(exc)
			)
			return Response(
				{'detail': message},
				status=status.HTTP_400_BAD_REQUEST,
			)
		return Response(QuestCompleteResponseSerializer(result).data)

	@extend_schema(
		request=None,
		responses={
			201: QuestFailResponseSerializer,
			400: DetailSerializer,
			404: DetailSerializer,
		},
	)
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
			message = (
				' '.join(exc.messages)
				if isinstance(exc, DjangoValidationError)
				else str(exc)
			)
			return Response(
				{'detail': message},
				status=status.HTTP_400_BAD_REQUEST,
			)
		return Response(
			QuestFailResponseSerializer(completion).data,
			status=status.HTTP_201_CREATED,
		)

	@extend_schema(responses={200: DailyBonusStatusSerializer})
	@action(detail=False, methods=('get',), url_path='daily-bonus')
	def daily_bonus(self, request):
		bonus_status = QuestService.daily_bonus_status(request.user)
		return Response(DailyBonusStatusSerializer(bonus_status).data)


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
		# A 404 when a concurrent DELETE removed the category after
		# get_object(), as for quests.
		category = get_object_or_404(
			QuestCategory.objects.select_for_update().filter(
				user=self.request.user,
			),
			pk=serializer.instance.pk,
		)
		serializer.instance = category
		serializer.save()

	@transaction.atomic
	def perform_destroy(self, instance):
		# Serialize with quest create/update, which lock the category row too.
		# Lock order must match complete/fail (quest rows, then category):
		# taking the category first deadlocks against a concurrent completion.
		list(
			Quest.objects.select_for_update().filter(
				category_id=instance.pk,
			).order_by('pk').values_list('pk', flat=True)
		)
		category = get_object_or_404(
			QuestCategory.objects.select_for_update().filter(
				user=self.request.user,
			),
			pk=instance.pk,
		)
		category.delete()
