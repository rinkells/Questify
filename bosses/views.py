from django.conf import settings
from django.db import transaction
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from core.models import Character
from core.serializers import DetailSerializer
from quests.views import OwnerViewSetMixin

from .models import Boss, BossStep
from .serializers import BossSerializer, BossStepCreateSerializer, bosses_for_display
from .services import BossConflictError, BossService, BossStepError


class BossViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
    queryset = Boss.objects.all()
    serializer_class = BossSerializer

    def get_queryset(self):
        bosses = super().get_queryset().filter(archived_at__isnull=True)
        if self.action in ('list', 'retrieve'):
            # Annotated, so never locked through it (see with_progress());
            # BossService locks the plain rows.
            return bosses_for_display(bosses)
        # The other actions only look the boss up, and answer with
        # _displayed() once they are done.
        return bosses

    def _displayed(self, boss_id):
        return bosses_for_display(Boss.objects.all()).get(pk=boss_id)

    @transaction.atomic
    def perform_create(self, serializer):
        Character.locked_for_user_id(self.request.user.pk)
        bosses = Boss.objects.filter(user=self.request.user)
        # Archived bosses count too, as archived quests do.
        if bosses.count() >= settings.QUESTIFY_MAX_BOSSES_PER_USER:
            raise DRFValidationError({
                'detail': 'Boss limit reached for this account.',
            })
        if bosses.filter(
            archived_at__isnull=True,
            defeat__isnull=True,
        ).count() >= settings.QUESTIFY_MAX_ACTIVE_BOSSES:
            raise DRFValidationError({
                'detail': 'Too many bosses in progress; defeat or archive one first.',
            })
        boss = serializer.save(user=self.request.user)
        serializer.instance = self._displayed(boss.pk)

    # Title and description stay editable after a defeat; only the steps
    # are frozen.
    @transaction.atomic
    def perform_update(self, serializer):
        boss = Boss.objects.select_for_update().filter(
            pk=serializer.instance.pk,
            user=self.request.user,
            archived_at__isnull=True,
        ).first()
        if boss is None:
            # Archived after get_object(), as for quests.
            raise Http404
        serializer.instance = boss
        serializer.save()
        serializer.instance = self._displayed(boss.pk)

    def perform_destroy(self, instance):
        try:
            BossService.archive_boss(self.request.user, instance.pk)
        except Boss.DoesNotExist:
            raise Http404 from None

    @extend_schema(
        request=BossStepCreateSerializer,
        responses={
            201: BossSerializer,
            400: DetailSerializer,
            404: DetailSerializer,
            409: DetailSerializer,
        },
    )
    @action(detail=True, methods=('post',))
    def steps(self, request, pk=None):
        boss = self.get_object()
        # Parsed before any lock is taken.
        body = BossStepCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            BossService.attach_step(
                request.user,
                boss.pk,
                body.validated_data['quest'],
            )
        except Boss.DoesNotExist:
            raise Http404 from None
        except BossConflictError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        except BossStepError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            self.get_serializer(self._displayed(boss.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=None,
        responses={
            204: None,
            404: DetailSerializer,
            409: DetailSerializer,
        },
    )
    @action(
        detail=True,
        methods=('delete',),
        url_path=r'steps/(?P<quest_id>\d+)',
        url_name='step-detail',
    )
    def detach_step(self, request, pk=None, quest_id=None):
        boss = self.get_object()
        try:
            BossService.detach_step(request.user, boss.pk, int(quest_id))
        except (Boss.DoesNotExist, BossStep.DoesNotExist):
            raise Http404 from None
        except BossConflictError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)
