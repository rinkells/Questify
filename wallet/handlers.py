from events.event_types import Event, QuestCompletedEvent
from events.handlers import awarded_xp

from .services import WalletService


class QuestRewardCoinsHandler:
    """Credit one coin per XP a completed quest awarded, daily bonus included.

    The amount comes from ``awarded_xp``, as XPRewardHandler's does, so it
    is already capped by the 24-hour XP limit and coins equal XP. The daily
    bonus, capped by complete_quest, gets a transaction of its own.
    """

    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        coins = awarded_xp(event)
        if coins > 0:
            WalletService.credit_quest_reward(
                event.user_id,
                coins,
                event.completion_id,
            )
            if event.result is not None:
                event.result.coins_gained = coins
        if event.bonus_xp > 0:
            WalletService.credit_daily_bonus(
                event.user_id,
                event.bonus_xp,
                event.daily_bonus_id,
            )
            if event.result is not None:
                event.result.bonus_coins_gained = event.bonus_xp
