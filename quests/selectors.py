"""Read-only aggregates over a user's quest completion history.

``QuestCompletion`` is the record of progress: it keeps the XP actually awarded
and the category name at completion time, and it survives quest archiving and
deletion (``quest`` becomes NULL, ``user`` stays). Achievements and skills both
read progress through these helpers, so neither app imports the other.
"""
from dataclasses import dataclass

from django.db.models import Count

from .models import QuestCompletion


def completed_quests(user_id):
    return QuestCompletion.objects.filter(
        user_id=user_id,
        status=QuestCompletion.CompletionStatus.COMPLETED,
    )


def category_key(name):
    """Normalize a category name for matching: trimmed, then lowercased.

    Achievements, skills and the skill requirement admin all match categories
    through this key, so they agree with each other, and none of them repeats
    the normalization. Category names are stored trimmed, so 'Study ' would
    otherwise never match anything. Lowercasing is ``str.lower()`` rather
    than ``casefold()``, which would merge 'Straße' and 'STRASSE'.
    Skill requirements store it (``SkillRequirement.category_key``) and are
    unique on the stored value, so the database never applies its own,
    locale-dependent ``lower()``.

    Legacy completions whose category was never recorded keep ``None`` as
    their key: they are neither uncategorized ('') nor in a named category.
    """
    if name is None:
        return None
    return name.strip().lower()


def completed_count(user_id):
    return completed_quests(user_id).count()


def completed_count_in_category(user_id, category_name):
    """Count completions recorded under ``category_name`` ('' = uncategorized).

    Goes through ``completed_counts_by_category`` so it matches exactly what
    the precomputed achievement context matches.
    """
    return completed_counts_by_category(user_id).get(
        category_key(category_name),
        0,
    )


def completed_counts_by_category(user_id):
    """Map ``category_key`` of the name recorded at completion to a count."""
    return completion_stats(user_id).counts_by_category


@dataclass(frozen=True)
class CompletionStats:
    completed_count: int
    counts_by_category: dict[str | None, int]


def completion_stats(user_id):
    """Return the completed count and per-category counts in one query.

    Categories are keyed by ``category_key``, so names differing only in case
    share one count.
    """
    rows = (
        completed_quests(user_id)
        .order_by()
        .values('category_name_at_completion')
        .annotate(completed_count=Count('id'))
        .values_list('category_name_at_completion', 'completed_count')
    )
    counts_by_category = {}
    for name, count in rows:
        key = category_key(name)
        counts_by_category[key] = counts_by_category.get(key, 0) + count
    return CompletionStats(
        completed_count=sum(counts_by_category.values()),
        counts_by_category=counts_by_category,
    )
