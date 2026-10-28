import logging

from django.db.models import F

from core.models import Character, Stat
from quests.models import Quest, QuestStatReward
from quests.services import StreakService

from .dispatcher import dispatcher
from .event_types import Event, LevelUpEvent, QuestCompletedEvent


logger = logging.getLogger(__name__)


class XPRewardHandler:
    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        xp_awarded = event.xp_awarded
        if xp_awarded is None:
            xp_awarded = Quest.objects.get(pk=event.quest_id).xp_reward
        # The UPDATE locks the row itself; reading it first is not needed.
        updated = Character.objects.filter(user_id=event.user_id).update(
            xp=F('xp') + xp_awarded,
        )
        if not updated:
            raise Character.DoesNotExist(f'No character for user {event.user_id}')
        if event.result is not None:
            event.result.xp_gained = xp_awarded


class StatUpdateHandler:
    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        rewards = list(
            QuestStatReward.objects.filter(quest_id=event.quest_id)
            .select_related('stat_type')
            .order_by('pk')
        )
        if not rewards:
            return
        character = Character.objects.select_for_update().get(user_id=event.user_id)

        # A constant number of queries however many rewards the quest has.
        # The character row is locked, so no one else can create these stats.
        stats = {
            stat.stat_type_id: stat
            for stat in Stat.objects.select_for_update().filter(
                character=character,
                stat_type_id__in={reward.stat_type_id for reward in rewards},
            )
        }
        new_stats = {}
        for reward in rewards:
            stat = stats.get(reward.stat_type_id)
            if stat is None:
                stat = stats[reward.stat_type_id] = new_stats[reward.stat_type_id] = Stat(
                    character=character,
                    stat_type=reward.stat_type,
                    value=0,
                )
            stat.value += reward.amount
            if event.result is not None:
                event.result.stats_updated[reward.stat_type.name] = stat.value

        if new_stats:
            Stat.objects.bulk_create(new_stats.values())
        existing = [s for s in stats.values() if s.stat_type_id not in new_stats]
        if existing:
            Stat.objects.bulk_update(existing, ['value'])


class LevelCheckHandler:
    @staticmethod
    def _xp_threshold(level):
        return round(100 * level**1.2)

    @classmethod
    def _level_for_xp(cls, xp):
        low = 0
        high = 1
        while cls._xp_threshold(high) <= xp:
            low = high
            high *= 2

        while low + 1 < high:
            middle = (low + high) // 2
            if cls._xp_threshold(middle) <= xp:
                low = middle
            else:
                high = middle
        return low + 1

    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        character = Character.objects.select_for_update().get(user_id=event.user_id)
        old_level = character.level
        new_level = max(old_level, self._level_for_xp(character.xp))
        if new_level == old_level:
            return

        character.level = new_level
        character.xp_to_next_level = self._xp_threshold(new_level)
        character.save(update_fields=('level', 'xp_to_next_level'))
        if event.result is not None:
            event.result.leveled_up = True
            event.result.new_level = new_level
        dispatcher.dispatch(
            LevelUpEvent(
                user_id=event.user_id,
                old_level=old_level,
                new_level=new_level,
                result=event.result,
            )
        )


class StreakUpdateHandler:
    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        streak = StreakService.record_completion(
            event.user_id,
            event.completed_at,
        )
        if event.result is not None:
            event.result.streak_length = streak.current_length


class StreakFreezeHandler:
    def handle(self, event: Event):
        if not isinstance(event, LevelUpEvent):
            return
        freeze_count = event.new_level // 5 - event.old_level // 5
        if freeze_count:
            StreakService.grant_freeze(event.user_id, freeze_count)


class LoggingHandler:
    def handle(self, event: Event):
        logger.info('Event dispatched: %s (%s)', type(event).__name__, event)
