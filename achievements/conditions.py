import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache

from django.apps import apps
from django.core.exceptions import ValidationError

from quests import selectors
from quests.models import Streak


logger = logging.getLogger(__name__)

MAX_THRESHOLD = 2**62


def _is_threshold(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_THRESHOLD
    )


def _threshold_keys(condition_type):
    if condition_type in ('quest_count', 'quest_count_by_category'):
        return ('threshold', 'count')
    return ('threshold',)


def _find_threshold(config, keys):
    """Read the threshold the way the evaluators do: the first key present.

    Returns ``(threshold, problem)``; ``problem`` explains a missing
    threshold and is None otherwise.
    """
    if not isinstance(config, dict):
        return None, 'Must be a JSON object.'
    for key in keys:
        if key in config:
            if _is_threshold(config[key]):
                return config[key], None
            return None, f'"{key}" must be a non-negative integer.'
    return None, ' or '.join(f'"{key}"' for key in keys) + ' is required.'


def _find_category(config):
    """Read a category name, '' meaning uncategorized quests.

    Returns ``(name, problem)`` like ``_find_threshold``. A missing or null
    category, or an empty string, means uncategorized. A whitespace-only
    name or a falsy non-string such as [] or 0 is a mistake, and treating it
    as '' would quietly count uncategorized quests instead.
    """
    category = config.get('category') if isinstance(config, dict) else None
    if category is None:
        return '', None
    if not isinstance(category, str):
        return None, '"category" must be a string.'
    if category and not category.strip():
        return None, (
            '"category" must not be blank; leave it empty for uncategorized '
            'quests.'
        )
    return category, None


@lru_cache(maxsize=256)
def _warn_once(message):
    # Cached on the message, so each unusable config is logged once per
    # process instead of on every event for every user.
    logger.warning(message)


def _usable(value, problem, config):
    if problem is not None:
        _warn_once(
            f'Achievement condition_config {config!r} can never be met: '
            f'{problem}'
        )
    return value


def _threshold(config, *keys):
    """Return the configured threshold, or None when the config is unusable.

    Achievement rows are admin-edited JSON, so a bad or missing value must
    make the achievement unattainable instead of failing every quest
    completion or granting it to everyone.
    """
    return _usable(*_find_threshold(config, keys), config)


def _category_name(config):
    return _usable(*_find_category(config), config)


def condition_config_problem(condition_type, config):
    """Return why the evaluators can never grant this config, or None.

    Reads the config exactly as they do, which is more lenient than
    validate_condition_config: {'threshold': 3, 'count': 'x'} is refused by
    the admin, yet unlocks after three quests, because only the first
    threshold key present is read.
    """
    _, problem = _find_threshold(config, _threshold_keys(condition_type))
    if problem is None and condition_type == 'quest_count_by_category':
        _, problem = _find_category(config)
    return problem


def validate_condition_config(condition_type, config):
    problem = condition_config_problem(condition_type, config)
    # Stricter than the evaluators: every threshold key given must be valid,
    # not only the one they read.
    for key in _threshold_keys(condition_type):
        if problem is None and key in config:
            _, problem = _find_threshold(config, (key,))
    if problem is not None:
        raise ValidationError({'condition_config': problem})


@dataclass
class AchievementEvaluationContext:
    completed_quest_count: int | None = None
    completed_quests_by_category: dict[str, int] | None = None
    longest_streak: int | None = None
    skills_unlocked_count: int | None = None
    bosses_defeated_count: int | None = None


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
        return selectors.completed_count(user.pk) >= threshold

    def evaluate_with_context(self, user, config, context):
        completed_count = context.completed_quest_count
        if completed_count is None:
            return self.evaluate(user, config)
        threshold = _threshold(config, 'threshold', 'count')
        return threshold is not None and completed_count >= threshold


class QuestCountByCategoryEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        threshold = _threshold(config, 'threshold', 'count')
        category_name = _category_name(config)
        if threshold is None or category_name is None:
            return False
        return selectors.completed_count_in_category(
            user.pk,
            category_name,
        ) >= threshold

    def evaluate_with_context(self, user, config, context):
        category_counts = context.completed_quests_by_category
        if category_counts is None:
            return self.evaluate(user, config)
        threshold = _threshold(config, 'threshold', 'count')
        category_name = _category_name(config)
        if threshold is None or category_name is None:
            return False
        return category_counts.get(
            selectors.category_key(category_name),
            0,
        ) >= threshold


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


def skills_unlocked_count(user):
    """Return how many skills the user has unlocked.

    Uses the reverse relation from skills.UserSkill, so this module does not
    import the skills app. Without that app nobody has unlocked a skill, so
    the count is 0 rather than an AttributeError that, re-raised by the
    dispatcher, would fail every quest completion.
    """
    if not apps.is_installed('skills'):
        _warn_once('skills_unlocked_count used without the skills app')
        return 0
    return user.user_skills.count()


class SkillsUnlockedCountEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        threshold = _threshold(config, 'threshold')
        return threshold is not None and skills_unlocked_count(user) >= threshold

    def evaluate_with_context(self, user, config, context):
        if context.skills_unlocked_count is None:
            return self.evaluate(user, config)
        threshold = _threshold(config, 'threshold')
        return (
            threshold is not None
            and context.skills_unlocked_count >= threshold
        )


def bosses_defeated_count(user):
    """Return how many bosses the user has defeated.

    Through the reverse relation from bosses.BossDefeat, as for skills, so
    this module does not import the bosses app. Unlike skills, bosses is
    always installed: wallet's models import it, so the project cannot
    start without it.
    """
    return user.boss_defeats.count()


class BossesDefeatedCountEvaluator(ConditionEvaluator):
    def evaluate(self, user, config):
        threshold = _threshold(config, 'threshold')
        return threshold is not None and bosses_defeated_count(user) >= threshold

    def evaluate_with_context(self, user, config, context):
        if context.bosses_defeated_count is None:
            return self.evaluate(user, config)
        threshold = _threshold(config, 'threshold')
        return (
            threshold is not None
            and context.bosses_defeated_count >= threshold
        )


EVALUATOR_REGISTRY = {
    'total_xp': TotalXPEvaluator(),
    'quest_count': QuestCountEvaluator(),
    'quest_count_by_category': QuestCountByCategoryEvaluator(),
    'streak_length': StreakLengthEvaluator(),
    'level_reached': LevelReachedEvaluator(),
    'skills_unlocked_count': SkillsUnlockedCountEvaluator(),
    'bosses_defeated_count': BossesDefeatedCountEvaluator(),
}
