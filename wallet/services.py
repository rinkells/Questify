from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import Character

from .models import Reward, RewardRedemption, WalletTransaction


class RedemptionError(ValueError):
    """A redemption the rules refuse; the message is shown to the client."""


@dataclass(frozen=True)
class Redemption:
    redemption: RewardRedemption
    balance: int


class WalletService:
    @staticmethod
    # No savepoint inside complete_quest's transaction: a failure here
    # aborts the whole completion anyway.
    @transaction.atomic(savepoint=False)
    def credit_quest_reward(user_id, amount, completion_id=None):
        """Add a completion's coins and record them; returns the new balance.

        The UPDATE locks the character row (complete_quest already holds it),
        so the balance read back is this transaction's own and cannot change
        before the commit.
        """
        updated = Character.objects.filter(user_id=user_id).update(
            coins=F('coins') + amount,
        )
        if not updated:
            raise Character.DoesNotExist(f'No character for user {user_id}')
        balance = Character.objects.filter(user_id=user_id).values_list(
            'coins',
            flat=True,
        ).get()
        WalletTransaction.objects.create(
            user_id=user_id,
            kind=WalletTransaction.Kind.QUEST_REWARD,
            amount=amount,
            balance_after=balance,
            quest_completion_id=completion_id,
        )
        return balance

    @staticmethod
    @transaction.atomic
    def redeem(user, *, locked_reward):
        """Spend coins on one of the user's rewards and record it.

        Requests serialize on the character row, like quest completions, so
        parallel redemptions cannot together spend more than the balance or
        exceed the limit. The caller locks the reward row first, as quests
        are locked before the character.

        The limit is per rolling 24 hours, like the XP cap, not per local
        day: changing timezone is not blocked after a redemption, so a day
        boundary would let a user reopen the day.
        """
        reward = locked_reward
        if reward.user_id != user.pk:
            raise Reward.DoesNotExist
        character = Character.locked_for_user_id(user.pk)
        # Read only after the lock, as in complete_quest: an earlier
        # timestamp could miss redemptions committed by a request that
        # locked first.
        now = timezone.now()
        if reward.archived_at is not None or not reward.is_active:
            raise RedemptionError(
                'This reward is inactive and cannot be redeemed.'
            )

        if RewardRedemption.objects.filter(
            user=user,
            redeemed_at__gt=now - timedelta(hours=24),
        ).count() >= settings.QUESTIFY_MAX_REDEMPTIONS_PER_USER_PER_24H:
            raise RedemptionError(
                'The redemption limit for the last 24 hours has been reached.'
            )
        if character.coins < reward.cost:
            raise RedemptionError(
                f'Not enough coins: {character.coins}/{reward.cost}.'
            )

        character.coins -= reward.cost
        character.save(update_fields=('coins',))
        redemption = RewardRedemption.objects.create(
            user=user,
            reward=reward,
            title_at_redemption=reward.title,
            cost_at_redemption=reward.cost,
            redeemed_at=now,
        )
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.REDEMPTION,
            amount=-reward.cost,
            balance_after=character.coins,
            redemption=redemption,
            created_at=now,
        )
        return Redemption(redemption=redemption, balance=character.coins)
