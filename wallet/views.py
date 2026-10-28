from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Character
from core.serializers import DetailSerializer
from quests.views import OwnerViewSetMixin

from .models import Reward, WalletTransaction
from .serializers import (
    RedeemResponseSerializer,
    RewardSerializer,
    WalletTransactionSerializer,
)
from .services import RedemptionError, WalletService


class RewardViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
    queryset = Reward.objects.all()
    serializer_class = RewardSerializer

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)

    @transaction.atomic
    def perform_create(self, serializer):
        Character.locked_for_user_id(self.request.user.pk)
        # Archived rewards count too, as archived quests do.
        if Reward.objects.filter(user=self.request.user).count() >= (
            settings.QUESTIFY_MAX_REWARDS_PER_USER
        ):
            raise DRFValidationError({
                'detail': 'Reward limit reached for this account.',
            })
        serializer.save(user=self.request.user)

    # Both lock the reward again, so an edit waits for a redemption reading
    # the cost, and a reward archived after get_object() is a 404, as for
    # quests.
    @transaction.atomic
    def perform_update(self, serializer):
        serializer.instance = self.get_locked_object()
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        reward = self.get_locked_object()
        reward.is_active = False
        reward.archived_at = timezone.now()
        reward.save(update_fields=('is_active', 'archived_at', 'updated_at'))

    @extend_schema(
        request=None,
        responses={
            201: RedeemResponseSerializer,
            400: DetailSerializer,
            404: DetailSerializer,
        },
    )
    @action(detail=True, methods=('post',))
    def redeem(self, request, pk=None):
        try:
            with transaction.atomic():
                reward = self.get_locked_object()
                redemption = WalletService.redeem(
                    request.user,
                    locked_reward=reward,
                )
        except RedemptionError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            RedeemResponseSerializer(redemption).data,
            status=status.HTTP_201_CREATED,
        )


class WalletTransactionListView(ListAPIView):
    serializer_class = WalletTransactionSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        return WalletTransaction.objects.filter(
            user=self.request.user,
        ).select_related('redemption').order_by('-created_at', '-pk')
