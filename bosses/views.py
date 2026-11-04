from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.exceptions import validation_error
from core.serializers import DetailSerializer
from quests.views import OwnerViewSetMixin

from .models import Boss, BossStep
from .selectors import bosses_for_display, bosses_for_user, displayed_boss
from .serializers import BossSerializer, BossStepCreateSerializer
from .services import (
    BossConflictError,
    BossLimitError,
    BossService,
    BossStepError,
)


class BossViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
    queryset = Boss.objects.all()
    serializer_class = BossSerializer

    def get_queryset(self):
        bosses = bosses_for_user(self.request.user)
        if self.action in ('list', 'retrieve'):
            # Annotated, so never locked through it (see with_progress());
            # BossService locks the plain rows.
            return bosses_for_display(bosses)
        # The other actions only look the boss up, and answer with
        # displayed_boss() once they are done.
        return bosses

    def perform_create(self, serializer):
        try:
            boss = BossService.create_boss(
                self.request.user,
                **serializer.validated_data,
            )
        except BossLimitError as exc:
            raise validation_error(exc) from exc
        serializer.instance = displayed_boss(boss.pk)

    # Title and description stay editable after a defeat; only the steps
    # are frozen.
    def perform_update(self, serializer):
        try:
            boss = BossService.update_boss(
                self.request.user,
                serializer.instance.pk,
                **serializer.validated_data,
            )
        except Boss.DoesNotExist:
            # Archived after get_object(), as for quests.
            raise Http404 from None
        serializer.instance = displayed_boss(boss.pk)

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
            self.get_serializer(displayed_boss(boss.pk)).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=None,
        responses={
            204: None,
            400: DetailSerializer,
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
        except BossStepError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
