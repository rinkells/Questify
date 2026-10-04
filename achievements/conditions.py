from abc import ABC, abstractmethod

from quests.models import QuestCompletion, Streak


class ConditionEvaluator(ABC):
    @abstractmethod
    def evaluate(self, user, config):
        raise NotImplementedError


class TotalXPEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        return user.character.xp >= config.get('threshold', 0)


class QuestCountEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        completed_count = QuestCompletion.objects.filter(
            quest__user=user,
            status=QuestCompletion.CompletionStatus.COMPLETED,
        ).count()
        return completed_count >= config.get('threshold', config.get('count', 0))


class QuestCountByCategoryEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        completed_count = QuestCompletion.objects.filter(
            quest__user=user,
            quest__category__name=config.get('category'),
            status=QuestCompletion.CompletionStatus.COMPLETED,
        ).count()
        return completed_count >= config.get('threshold', config.get('count', 0))


class StreakLengthEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        streak = Streak.objects.filter(
            user=user,
            quest_category=None,
        ).first()
        return bool(streak and streak.longest_length >= config.get('threshold', 0))


class LevelReachedEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        return user.character.level >= config.get('threshold', 0)


EVALUATOR_REGISTRY = {
    'total_xp': TotalXPEvaluator(),
    'quest_count': QuestCountEvaluator(),
    'quest_count_by_category': QuestCountByCategoryEvaluator(),
    'streak_length': StreakLengthEvaluator(),
    'level_reached': LevelReachedEvaluator(),
}

CONDITION_EVALUATORS = EVALUATOR_REGISTRY
