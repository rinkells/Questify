from django.conf import settings
from rest_framework import serializers

from .models import Reward, WalletTransaction


class RewardSerializer(serializers.ModelSerializer):
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=settings.QUESTIFY_MAX_DESCRIPTION_LENGTH,
    )

    class Meta:
        model = Reward
        fields = (
            'id',
            'title',
            'description',
            'cost',
            'is_active',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class RedeemResponseSerializer(serializers.Serializer):
    """A ``services.Redemption``: the redemption and the balance left."""

    id = serializers.IntegerField(source='redemption.id')
    # Null once the reward row is gone; title and cost are kept as they were.
    reward = serializers.IntegerField(source='redemption.reward_id', allow_null=True)
    title = serializers.CharField(source='redemption.title_at_redemption')
    cost = serializers.IntegerField(source='redemption.cost_at_redemption')
    redeemed_at = serializers.DateTimeField(source='redemption.redeemed_at')
    balance = serializers.IntegerField()


class WalletTransactionSerializer(serializers.ModelSerializer):
    # What the coins were spent on, as it was called then; null otherwise.
    reward_title = serializers.CharField(
        source='redemption.title_at_redemption',
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = WalletTransaction
        fields = (
            'id',
            'kind',
            'amount',
            'balance_after',
            'quest_completion',
            'redemption',
            'reward_title',
            'created_at',
        )
        read_only_fields = fields
