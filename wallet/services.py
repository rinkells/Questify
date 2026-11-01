from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F
from django.utils import timezone

from core.errors import DomainError, NotFoundError
from core.models import Character

from .models import Reward, RewardRedemption, WalletTransaction


class RedemptionError(DomainError):
    """A redemption the rules refuse; the message is shown to the client.

    Codes: ``reward_inactive``, ``redemption_limit_reached`` and
    ``not_enough_coins`` (params ``balance`` and ``cost``).
    """

    code = 'redemption_refused'


class RewardLimitError(DomainError):
    """A new reward beyond the user's quota (400)."""

    code = 'reward_limit_reached'


REWARD_FIELDS = frozenset(('title', 'description', 'cost', 'is_active'))


@dataclass(frozen=True)
class Redemption:
    redemption: RewardRedemption
    balance: int


class WalletService:
    @staticmethod
    # No savepoint inside complete_quest's transaction: a failure here
    # aborts the whole completion anyway.
    @transaction.atomic(savepoint=False)
    def credit_quest_reward(
        user_id,
        amount,
        completion_id=None,
        *,
        created_at=None,
    ):
        """Add a completion's coins and record them; returns the new balance."""
        return WalletService._credit(
            user_id,
            amount,
            WalletTransaction.Kind.QUEST_REWARD,
            created_at=created_at,
            quest_completion_id=completion_id,
        )

    @staticmethod
    # No savepoint, as for credit_quest_reward: it runs inside complete_quest.
    @transaction.atomic(savepoint=False)
    def credit_daily_bonus(
        user_id,
        amount,
        daily_bonus_id=None,
        *,
        created_at=None,
    ):
        """Add a daily bonus's coins and record them; returns the new balance."""
        return WalletService._credit(
            user_id,
            amount,
            WalletTransaction.Kind.DAILY_BONUS,
            created_at=created_at,
            daily_bonus_id=daily_bonus_id,
        )

    @staticmethod
    # No savepoint either: it runs inside complete_quest, through
    # BossDefeatedEvent.
    @transaction.atomic(savepoint=False)
    def credit_boss_reward(
        user_id,
        amount,
        boss_defeat_id=None,
        *,
        created_at=None,
    ):
        """Add a boss reward's coins and record them; returns the new balance."""
        return WalletService._credit(
            user_id,
            amount,
            WalletTransaction.Kind.BOSS_REWARD,
            created_at=created_at,
            boss_defeat_id=boss_defeat_id,
        )

    @staticmethod
    def _credit(user_id, amount, kind, *, created_at=None, **links):
        """Add coins and write their transaction; returns the new balance.

        The UPDATE locks the character row (complete_quest already holds it),
        so the balance read back is this transaction's own and cannot change
        before the commit.

        The handlers pass the time of the completion, bonus or defeat paid
        for, so the row lands in the same month as its record; without it,
        the insert would come a few milliseconds later, possibly in the next
        month. complete_quest reads that time after the character lock, as
        redeem reads its own, so the ledger stays in balance order.
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
            kind=kind,
            amount=amount,
            balance_after=balance,
            created_at=created_at or timezone.now(),
            **links,
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
                'This reward is inactive and cannot be redeemed.',
                code='reward_inactive',
            )

        if RewardRedemption.objects.filter(
            user=user,
            redeemed_at__gt=now - timedelta(hours=24),
        ).count() >= settings.QUESTIFY_MAX_REDEMPTIONS_PER_USER_PER_24H:
            raise RedemptionError(
                'The redemption limit for the last 24 hours has been reached.',
                code='redemption_limit_reached',
            )
        if character.coins < reward.cost:
            raise RedemptionError(
                f'Not enough coins: {character.coins}/{reward.cost}.',
                code='not_enough_coins',
                balance=character.coins,
                cost=reward.cost,
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

    @staticmethod
    @transaction.atomic
    def redeem_reward(user, reward_id):
        """Lock the user's reward row, then redeem it.

        The reward is locked before the character, as quests are before a
        completion, so an edit waits for a redemption reading the cost.
        Raises ``NotFoundError`` for an archived or foreign reward.
        """
        reward = _locked_reward(user, reward_id)
        return WalletService.redeem(user, locked_reward=reward)


def _locked_reward(user, reward_id):
    try:
        return Reward.objects.select_for_update().filter(
            user=user,
            archived_at__isnull=True,
        ).get(pk=reward_id)
    except (
        Reward.DoesNotExist,
        TypeError,
        ValueError,
        DjangoValidationError,
    ):
        # A malformed id is a missing row, as in DRF's get_object_or_404.
        raise NotFoundError('Not found.', code='reward_not_found') from None


def _check_reward_fields(fields):
    unknown = set(fields) - REWARD_FIELDS
    if unknown:
        raise TypeError(f'Unexpected fields: {sorted(unknown)}')


class RewardService:
    @staticmethod
    @transaction.atomic
    def create_reward(user, **fields):
        """Create a reward within the user's quota (archived ones count)."""
        _check_reward_fields(fields)
        Character.locked_for_user_id(user.pk)
        if Reward.objects.filter(user=user).count() >= (
            settings.QUESTIFY_MAX_REWARDS_PER_USER
        ):
            raise RewardLimitError('Reward limit reached for this account.')
        return Reward.objects.create(user=user, **fields)

    @staticmethod
    @transaction.atomic
    def update_reward(user, reward_id, **fields):
        """Change an unarchived reward, waiting for a redemption in progress.

        A reward archived after the caller looked it up is a
        ``NotFoundError``, as for quests.
        """
        _check_reward_fields(fields)
        reward = _locked_reward(user, reward_id)
        for name, value in fields.items():
            setattr(reward, name, value)
        reward.save()
        return reward

    @staticmethod
    @transaction.atomic
    def archive_reward(user, reward_id):
        """Hide and deactivate the reward; its redemptions keep their snapshot."""
        reward = _locked_reward(user, reward_id)
        reward.is_active = False
        reward.archived_at = timezone.now()
        reward.save(update_fields=('is_active', 'archived_at', 'updated_at'))
        return reward
