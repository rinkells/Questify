from django.contrib.auth import get_user_model
from django.db.models import Count

from events.dispatcher import dispatcher
from events.event_types import (
    AchievementUnlockedEvent,
    Event,
    LevelUpEvent,
    QuestCompletedEvent,
)
from quests.models import QuestCompletion, Streak

from .conditions import AchievementEvaluationContext, EVALUATOR_REGISTRY
from .models import Achievement, UserAchievement


User = get_user_model()


class AchievementCheckHandler:
    def handle(self, event: Event):
        if not isinstance(event, (QuestCompletedEvent, LevelUpEvent)):
            return

        user = User.objects.select_related('character').get(pk=event.user_id)
        unlocked_ids = UserAchievement.objects.filter(
            user=user,
        ).values_list('achievement_id', flat=True)
        achievements = list(Achievement.objects.exclude(id__in=unlocked_ids))
        if not achievements:
            return

        condition_types = {achievement.condition_type for achievement in achievements}
        context = AchievementEvaluationContext()
        completed_quests = QuestCompletion.objects.filter(
            user=user,
            status=QuestCompletion.CompletionStatus.COMPLETED,
        )
        if 'quest_count_by_category' in condition_types:
            category_counts = dict(
                completed_quests
                .values('category_name_at_completion')
                .annotate(completed_count=Count('id'))
                .values_list(
                    'category_name_at_completion',
                    'completed_count',
                )
            )
            context.completed_quests_by_category = category_counts
            if 'quest_count' in condition_types:
                context.completed_quest_count = sum(category_counts.values())
        elif 'quest_count' in condition_types:
            context.completed_quest_count = completed_quests.count()

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
            # LevelUpEvent shares the completion's result so achievements
            # unlocked by a level-up are reported in the same response.
            if event.result is not None:
                event.result.achievements_unlocked.append(achievement.id)
            dispatcher.dispatch(
                AchievementUnlockedEvent(
                    user_id=user.id,
                    achievement_id=user_achievement.achievement_id,
                )
            )
