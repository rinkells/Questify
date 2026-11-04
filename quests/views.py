from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.errors import NotFoundError
from core.exceptions import validation_error
from core.serializers import DetailSerializer

from .models import Quest, QuestCategory
from .permissions import IsOwner
from .selectors import categories_for_user, quests_for_user
from .serializers import (
	DailyBonusStatusSerializer,
	QuestCategorySerializer,
	QuestCompleteResponseSerializer,
	QuestFailResponseSerializer,
	QuestSerializer,
)
from .services import (
	CategoryService,
	QuestAlreadyCompletedError,
	QuestAlreadyFailedError,
	QuestInputError,
	QuestRuleError,
	QuestService,
)

# What complete/fail answer with a 400 {'detail': message}. A Django
# ValidationError can still come from an event handler.
REFUSALS = (
	QuestAlreadyCompletedError,
	QuestAlreadyFailedError,
	QuestRuleError,
	DjangoValidationError,
)


def refusal_message(exc):
	if isinstance(exc, DjangoValidationError):
		return ' '.join(exc.messages)
	return str(exc)


class OwnerViewSetMixin:
	permission_classes = (IsAuthenticated, IsOwner)

	def get_queryset(self):
		return super().get_queryset().filter(
			user=self.request.user,
		).order_by('pk')


class QuestViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
	queryset = Quest.objects.all()
	serializer_class = QuestSerializer

	def get_queryset(self):
		return quests_for_user(self.request.user)

	def perform_create(self, serializer):
		try:
			serializer.instance = QuestService.create_quest(
				self.request.user,
				**serializer.validated_data,
			)
		except QuestInputError as exc:
			raise validation_error(exc) from exc

	def perform_update(self, serializer):
		try:
			serializer.instance = QuestService.update_quest(
				self.request.user,
				serializer.instance.pk,
				**serializer.validated_data,
			)
		except NotFoundError:
			raise Http404 from None
		except QuestInputError as exc:
			raise validation_error(exc) from exc

	def perform_destroy(self, instance):
		try:
			QuestService.archive_quest(self.request.user, instance.pk)
		except NotFoundError:
			raise Http404 from None

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
			result = QuestService.complete_for_user(request.user, pk)
		except NotFoundError:
			raise Http404 from None
		except REFUSALS as exc:
			return Response(
				{'detail': refusal_message(exc)},
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
			completion = QuestService.fail_for_user(request.user, pk)
		except NotFoundError:
			raise Http404 from None
		except REFUSALS as exc:
			return Response(
				{'detail': refusal_message(exc)},
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

	def get_queryset(self):
		return categories_for_user(self.request.user)

	def perform_create(self, serializer):
		try:
			serializer.instance = CategoryService.create_category(
				self.request.user,
				**serializer.validated_data,
			)
		except QuestInputError as exc:
			raise validation_error(exc) from exc

	def perform_update(self, serializer):
		# A 404 when a concurrent DELETE removed the category after
		# get_object(), as for quests.
		try:
			serializer.instance = CategoryService.update_category(
				self.request.user,
				serializer.instance.pk,
				**serializer.validated_data,
			)
		except NotFoundError:
			raise Http404 from None
		except QuestInputError as exc:
			raise validation_error(exc) from exc

	def perform_destroy(self, instance):
		try:
			CategoryService.delete_category(self.request.user, instance.pk)
		except NotFoundError:
			raise Http404 from None
