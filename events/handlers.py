import logging

from core.models import Character, Stat
from quests.models import Quest, QuestStatReward

from .dispatcher import dispatcher
from .event_types import Event, LevelUpEvent, QuestCompletedEvent


logger = logging.getLogger(__name__)


class XPRewardHandler:
    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        quest = Quest.objects.get(pk=event.quest_id)
        character = Character.objects.get(user_id=event.user_id)
        character.xp += quest.xp_reward
        character.save(update_fields=('xp',))
        if event.result is not None:
            event.result.xp_gained = quest.xp_reward


class StatUpdateHandler:
    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        character = Character.objects.get(user_id=event.user_id)
        rewards = QuestStatReward.objects.filter(quest_id=event.quest_id)
        for reward in rewards:
            stat, _ = Stat.objects.get_or_create(
                character=character,
                stat_type=reward.stat_type,
            )
            stat.value += reward.amount
            stat.save(update_fields=('value',))
            if event.result is not None:
                event.result.stats_updated[reward.stat_type.name] = stat.value


class LevelCheckHandler:
    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        character = Character.objects.get(user_id=event.user_id)
        while character.xp >= character.xp_to_next_level:
            old_level = character.level
            character.level += 1
            character.xp_to_next_level = round(100 * character.level**1.2)
            character.save(update_fields=('level', 'xp_to_next_level'))
            if event.result is not None:
                event.result.leveled_up = True
                event.result.new_level = character.level
            dispatcher.dispatch(
                LevelUpEvent(
                    user_id=event.user_id,
                    old_level=old_level,
                    new_level=character.level,
                )
            )


class LoggingHandler:
    def handle(self, event: Event):
        logger.info('Event dispatched: %s (%s)', type(event).__name__, event)
