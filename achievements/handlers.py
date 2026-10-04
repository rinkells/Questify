from django.contrib.auth import get_user_model

from events.dispatcher import dispatcher
from events.event_types import (
    AchievementUnlockedEvent,
    Event,
    LevelUpEvent,
    QuestCompletedEvent,
)

from .conditions import EVALUATOR_REGISTRY
from .models import Achievement, UserAchievement


User = get_user_model()


class AchievementCheckHandler:
    def handle(self, event: Event):
        if not isinstance(event, (QuestCompletedEvent, LevelUpEvent)):
            return

        user = User.objects.get(pk=event.user_id)
        unlocked_ids = UserAchievement.objects.filter(
            user=user,
        ).values_list('achievement_id', flat=True)
        achievements = Achievement.objects.exclude(id__in=unlocked_ids)

        for achievement in achievements:
            evaluator = EVALUATOR_REGISTRY.get(achievement.condition_type)
            if evaluator is None or not evaluator.evaluate(
                user,
                achievement.condition_config,
            ):
                continue
            user_achievement = UserAchievement.objects.create(
                user=user,
                achievement=achievement,
            )
            if (
                isinstance(event, QuestCompletedEvent)
                and event.result is not None
            ):
                event.result.achievements_unlocked.append(achievement.id)
            dispatcher.dispatch(
                AchievementUnlockedEvent(
                    user_id=user.id,
                    achievement_id=user_achievement.achievement_id,
                )
            )
