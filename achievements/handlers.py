from django.contrib.auth import get_user_model

from events.dispatcher import dispatcher
from events.event_types import (
    AchievementUnlockedEvent,
    BossDefeatedEvent,
    Event,
    QuestCompletedEvent,
    SkillUnlockedEvent,
)
from quests import selectors
from quests.models import Streak

from .conditions import (
    AchievementEvaluationContext,
    EVALUATOR_REGISTRY,
    bosses_defeated_count,
    skills_unlocked_count,
)
from .models import Achievement, UserAchievement


User = get_user_model()

# Locked conditions each event is checked against. A quest completion checks
# every type, so an achievement added later still reaches users who already
# meet it on their next completion; that includes level_reached, since the
# level-up happens earlier in the same completion. A skill unlock changes only
# the unlocked-skill count, a boss defeat only the defeated-boss count (its XP
# is checked by the completion that dispatched it).
CONDITION_TYPES_BY_EVENT = {
    SkillUnlockedEvent: {Achievement.ConditionType.SKILLS_UNLOCKED_COUNT},
    BossDefeatedEvent: {Achievement.ConditionType.BOSSES_DEFEATED_COUNT},
}


class AchievementCheckHandler:
    def handle(self, event: Event):
        if not isinstance(
            event,
            (QuestCompletedEvent, SkillUnlockedEvent, BossDefeatedEvent),
        ):
            return

        user = User.objects.select_related('character').get(pk=event.user_id)
        unlocked_ids = UserAchievement.objects.filter(
            user=user,
        ).values_list('achievement_id', flat=True)
        achievements = Achievement.objects.exclude(id__in=unlocked_ids)
        event_condition_types = CONDITION_TYPES_BY_EVENT.get(type(event))
        if event_condition_types is not None:
            achievements = achievements.filter(
                condition_type__in=event_condition_types,
            )
        achievements = list(achievements)
        if not achievements:
            return

        condition_types = {achievement.condition_type for achievement in achievements}
        context = AchievementEvaluationContext()
        if 'quest_count_by_category' in condition_types:
            stats = selectors.completion_stats(user.id)
            context.completed_quest_count = stats.completed_count
            context.completed_quests_by_category = stats.counts_by_category
        elif 'quest_count' in condition_types:
            # A plain COUNT; the per-category aggregate is not needed.
            context.completed_quest_count = selectors.completed_count(user.id)

        if 'skills_unlocked_count' in condition_types:
            context.skills_unlocked_count = skills_unlocked_count(user)

        if 'bosses_defeated_count' in condition_types:
            context.bosses_defeated_count = bosses_defeated_count(user)

        if 'streak_length' in condition_types:
            context.longest_streak = (
                Streak.objects.filter(
                    user=user,
                    quest_category=None,
                ).values_list('longest_length', flat=True).first()
                or 0
            )

        for achievement in achievements:
            evaluator = EVALUATOR_REGISTRY.get(achievement.condition_type)
            if evaluator is None or not evaluator.evaluate_with_context(
                user,
                achievement.condition_config,
                context,
            ):
                continue
            user_achievement = UserAchievement.objects.create(
                user=user,
                achievement=achievement,
            )
            # SkillUnlockedEvent carries the unlock's result, so achievements
            # it unlocks are reported in the unlock response; BossDefeatedEvent
            # carries the completion's.
            if event.result is not None:
                event.result.achievements_unlocked.append(achievement.id)
            dispatcher.dispatch(
                AchievementUnlockedEvent(
                    user_id=user.id,
                    achievement_id=user_achievement.achievement_id,
                )
            )
