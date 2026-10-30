from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from bosses.models import BossDefeat
from quests.models import DailyBonus, QuestCompletion

MAX_REWARD_COST = 100_000


class Reward(models.Model):
    """Something users promise themselves for coins, such as a film night."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='rewards',
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    # At least 1: a free reward could be redeemed without limit.
    cost = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(MAX_REWARD_COST)],
    )
    is_active = models.BooleanField(default=True)
    archived_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=('user', 'is_active')),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(cost__gte=1, cost__lte=MAX_REWARD_COST),
                name='reward_cost_between_1_and_100000',
            ),
            models.CheckConstraint(
                condition=Q(archived_at__isnull=True) | Q(is_active=False),
                name='archived_reward_must_be_inactive',
            ),
        ]

    def __str__(self):
        return self.title


class RewardRedemption(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reward_redemptions',
        editable=False,
    )
    # The API only archives rewards; SET_NULL keeps the history should a row
    # be deleted anyway.
    reward = models.ForeignKey(
        Reward,
        on_delete=models.SET_NULL,
        related_name='redemptions',
        null=True,
        blank=True,
        editable=False,
    )
    # Snapshots, so renaming or repricing a reward does not rewrite history.
    title_at_redemption = models.CharField(max_length=255, editable=False)
    cost_at_redemption = models.PositiveIntegerField(editable=False)
    redeemed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [
            models.Index(
                fields=('user', 'redeemed_at'),
                name='rr_user_date_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(cost_at_redemption__gte=1),
                name='redemption_cost_at_least_1',
            ),
        ]

    def __str__(self):
        return f'{self.title_at_redemption} (-{self.cost_at_redemption})'


class WalletTransaction(models.Model):
    """One change of ``Character.coins``; a user's amounts sum to the balance."""

    class Kind(models.TextChoices):
        QUEST_REWARD = 'quest_reward', 'Quest reward'
        REDEMPTION = 'redemption', 'Redemption'
        INITIAL_GRANT = 'initial_grant', 'Initial grant'
        DAILY_BONUS = 'daily_bonus', 'Daily bonus'
        BOSS_REWARD = 'boss_reward', 'Boss reward'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='wallet_transactions',
        editable=False,
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, editable=False)
    amount = models.BigIntegerField(editable=False)
    balance_after = models.PositiveBigIntegerField(editable=False)
    quest_completion = models.ForeignKey(
        QuestCompletion,
        on_delete=models.SET_NULL,
        related_name='wallet_transactions',
        null=True,
        blank=True,
        editable=False,
    )
    redemption = models.OneToOneField(
        RewardRedemption,
        on_delete=models.SET_NULL,
        related_name='wallet_transaction',
        null=True,
        blank=True,
        editable=False,
    )
    # One-to-one, so the database pays a daily bonus once.
    daily_bonus = models.OneToOneField(
        DailyBonus,
        on_delete=models.SET_NULL,
        related_name='wallet_transaction',
        null=True,
        blank=True,
        editable=False,
    )
    # One-to-one, so the database pays a boss once.
    boss_defeat = models.OneToOneField(
        BossDefeat,
        on_delete=models.SET_NULL,
        related_name='wallet_transaction',
        null=True,
        blank=True,
        editable=False,
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [
            models.Index(
                fields=('user', 'created_at'),
                name='wt_user_date_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(kind='redemption', amount__lt=0)
                    | (~Q(kind='redemption') & Q(amount__gt=0))
                ),
                name='wallet_transaction_sign_matches_kind',
            ),
            # A completion pays out once, even if a code path skips the lock.
            models.UniqueConstraint(
                fields=('quest_completion',),
                condition=Q(kind='quest_reward'),
                name='one_reward_transaction_per_completion',
            ),
            models.UniqueConstraint(
                fields=('user',),
                condition=Q(kind='initial_grant'),
                name='one_initial_grant_per_user',
            ),
        ]

    def __str__(self):
        return f'{self.user}: {self.amount:+} ({self.kind})'
