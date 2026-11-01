from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.errors import NotFoundError
from core.exceptions import validation_error
from core.serializers import DetailSerializer
from quests.views import OwnerViewSetMixin

from .models import Reward
from .selectors import rewards_for_user, transactions_for_user
from .serializers import (
    RedeemResponseSerializer,
    RewardSerializer,
    WalletTransactionSerializer,
)
from .services import RedemptionError, RewardLimitError, RewardService, WalletService


class RewardViewSet(OwnerViewSetMixin, viewsets.ModelViewSet):
    queryset = Reward.objects.all()
    serializer_class = RewardSerializer

    def get_queryset(self):
        return rewards_for_user(self.request.user)

    def perform_create(self, serializer):
        try:
            serializer.instance = RewardService.create_reward(
                self.request.user,
                **serializer.validated_data,
            )
        except RewardLimitError as exc:
            raise validation_error(exc) from exc

    # Update and archive lock the reward again, so an edit waits for a
    # redemption reading the cost, and a reward archived after get_object()
    # is a 404, as for quests.
    def perform_update(self, serializer):
        try:
            serializer.instance = RewardService.update_reward(
                self.request.user,
                serializer.instance.pk,
                **serializer.validated_data,
            )
        except NotFoundError:
            raise Http404 from None

    def perform_destroy(self, instance):
        try:
            RewardService.archive_reward(self.request.user, instance.pk)
        except NotFoundError:
            raise Http404 from None

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
            redemption = WalletService.redeem_reward(request.user, pk)
        except NotFoundError:
            raise Http404 from None
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
        return transactions_for_user(self.request.user)
