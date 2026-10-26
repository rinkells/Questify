import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from quests.models import QuestCompletion, Streak


logger = logging.getLogger(__name__)

MAX_THRESHOLD = 2**62


def _is_threshold(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_THRESHOLD
    )


def _threshold(config, *keys):
    """Return the configured threshold, or None when the config is unusable.

    Achievement rows are admin-edited JSON, so a bad value must make the
    achievement unattainable instead of failing every quest completion.
    """
    if not isinstance(config, dict):
        logger.warning('Achievement condition_config is not an object: %r', config)
        return None
    for key in keys:
        if key in config:
            value = config[key]
            break
    else:
        return 0
    if not _is_threshold(value):
        logger.warning('Achievement has an invalid %s: %r', key, value)
        return None
    return value


def validate_condition_config(condition_type, config):
    if not isinstance(config, dict):
        raise ValidationError({'condition_config': 'Must be a JSON object.'})
    keys = ('threshold', 'count') if condition_type in (
        'quest_count',
        'quest_count_by_category',
    ) else ('threshold',)
    for key in keys:
        if key in config and not _is_threshold(config[key]):
            raise ValidationError({
                'condition_config': f'"{key}" must be a non-negative integer.',
            })
    if condition_type == 'quest_count_by_category' and not isinstance(
        config.get('category', ''),
        (str, type(None)),
    ):
        raise ValidationError({
            'condition_config': '"category" must be a string.',
        })


@dataclass
class AchievementEvaluationContext:
    completed_quest_count: int | None = None
    completed_quests_by_category: dict[str, int] | None = None
    longest_streak: int | None = None


class ConditionEvaluator(ABC):
    @abstractmethod
    def evaluate(self, user, config):
        raise NotImplementedError

    def evaluate_with_context(self, user, config, context):
        return self.evaluate(user, config)


class TotalXPEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        threshold = _threshold(config, 'threshold')
        return threshold is not None and user.character.xp >= threshold


class QuestCountEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        threshold = _threshold(config, 'threshold', 'count')
        if threshold is None:
            return False
        completed_count = QuestCompletion.objects.filter(
            user=user,
            status=QuestCompletion.CompletionStatus.COMPLETED,
        ).count()
        return completed_count >= threshold

    def evaluate_with_context(self, user, config, context):
        completed_count = context.completed_quest_count
        if completed_count is None:
            return self.evaluate(user, config)
        threshold = _threshold(config, 'threshold', 'count')
        return threshold is not None and completed_count >= threshold


def _category_name(config):
    category = (config.get('category') if isinstance(config, dict) else '') or ''
    return category if isinstance(category, str) else None


class QuestCountByCategoryEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        threshold = _threshold(config, 'threshold', 'count')
        category_name = _category_name(config)
        if threshold is None or category_name is None:
            return False
        completed_count = QuestCompletion.objects.filter(
            user=user,
            category_name_at_completion=category_name,
            status=QuestCompletion.CompletionStatus.COMPLETED,
        ).count()
        return completed_count >= threshold

    def evaluate_with_context(self, user, config, context):
        category_counts = context.completed_quests_by_category
        if category_counts is None:
            return self.evaluate(user, config)
        threshold = _threshold(config, 'threshold', 'count')
        category_name = _category_name(config)
        if threshold is None or category_name is None:
            return False
        return category_counts.get(category_name, 0) >= threshold


class StreakLengthEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        streak = Streak.objects.filter(
            user=user,
            quest_category=None,
        ).first()
        threshold = _threshold(config, 'threshold')
        return bool(
            streak
            and threshold is not None
            and streak.longest_length >= threshold
        )

    def evaluate_with_context(self, user, config, context):
        if context.longest_streak is None:
            return self.evaluate(user, config)
        threshold = _threshold(config, 'threshold')
        return threshold is not None and context.longest_streak >= threshold


class LevelReachedEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        threshold = _threshold(config, 'threshold')
        return threshold is not None and user.character.level >= threshold


EVALUATOR_REGISTRY = {
    'total_xp': TotalXPEvaluator(),
    'quest_count': QuestCountEvaluator(),
    'quest_count_by_category': QuestCountByCategoryEvaluator(),
    'streak_length': StreakLengthEvaluator(),
    'level_reached': LevelReachedEvaluator(),
}
