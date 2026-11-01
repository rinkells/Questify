"""Read-only aggregates over a user's quest completion history.

``QuestCompletion`` is the record of progress: it keeps the XP actually awarded
and the category name at completion time, and it survives quest archiving and
deletion (``quest`` becomes NULL, ``user`` stays). Achievements and skills both
read progress through these helpers, so neither app imports the other.
"""
from dataclasses import dataclass

from django.db.models import Count, Exists, F, OuterRef, Subquery
from django.utils import timezone

from core.services import ensure_character

from .models import Quest, QuestCategory, QuestCompletion
from .services import DAILY_BONUS_QUEST_TYPES, local_day

# How many one-time quests the "today" screen lists; the rest are behind
# "all quests". Daily and habit quests are always all listed.
TODAY_QUEST_LIMIT = 5


def quests_for_user(user):
    """The user's quests as the API and the quest list show them: unarchived."""
    return Quest.objects.filter(
        user=user,
        archived_at__isnull=True,
    ).order_by('pk')


def categories_for_user(user):
    """The user's own categories: the only ones a quest may be put in."""
    return QuestCategory.objects.filter(user=user).order_by('pk')


@dataclass(frozen=True)
class TodayQuest:
    quest: Quest
    # 'completed' or 'failed' today (a daily or habit quest, in the character's
    # local day), else 'open'. A one-time quest listed here is always open.
    state: str
    # The deadline has passed: it can no longer be completed, only failed.
    expired: bool


@dataclass(frozen=True)
class TodayQuests:
    # Every active daily and habit quest: those still open today first, then
    # the completed and failed ones, each group by id.
    routines: list[TodayQuest]
    # At most ``limit`` active one-time quests with no completion or failure,
    # the nearest deadline first and those without one last.
    one_time: list[TodayQuest]
    # All active one-time quests with no completion or failure, shown or not.
    total: int
    limit: int | None = TODAY_QUEST_LIMIT

    @property
    def items(self):
        return self.routines + self.one_time

    @property
    def has_more(self):
        """Whether to link "all quests": more one-time quests than listed."""
        return self.limit is not None and self.total > self.limit


def today_quests(user, *, limit=TODAY_QUEST_LIMIT, now=None):
    """The quests to act on today.

    All active daily and habit quests come first, with today's state in the
    character's timezone and no limit; open ones before completed and failed
    ones, each group by id. Then come the active one-time quests that have no
    completion or failure yet, by deadline (the nearest first, none last, then
    by id), at most ``limit`` of them (``None`` lists all). Four queries.
    """
    now = now or timezone.now()
    _, day_start, next_day_start = local_day(
        now,
        ensure_character(user).timezone,
    )
    active = Quest.objects.filter(
        user=user,
        is_active=True,
        archived_at__isnull=True,
    )

    def entry(quest, state):
        return TodayQuest(
            quest=quest,
            state=state,
            expired=quest.deadline is not None and quest.deadline < now,
        )

    routines = [
        entry(quest, quest.today_status or 'open')
        for quest in active.filter(
            quest_type__in=DAILY_BONUS_QUEST_TYPES,
        ).annotate(
            today_status=Subquery(
                QuestCompletion.objects.filter(
                    quest=OuterRef('pk'),
                    completed_at__gte=day_start,
                    completed_at__lt=next_day_start,
                ).order_by('-completed_at').values('status')[:1]
            ),
        ).select_related('category').order_by('pk')
    ]
    # sorted() is stable, so the id order holds inside each group.
    routines.sort(key=lambda item: item.state != 'open')

    one_time = active.filter(
        quest_type=Quest.QuestType.ONE_TIME,
    ).annotate(
        has_completion=Exists(
            QuestCompletion.objects.filter(quest=OuterRef('pk')),
        ),
    ).filter(has_completion=False)
    total = one_time.count()
    page = one_time.select_related('category').order_by(
        F('deadline').asc(nulls_last=True),
        'pk',
    )
    if limit is not None:
        page = page[:limit]
    return TodayQuests(
        routines=routines,
        one_time=[entry(quest, 'open') for quest in page],
        total=total,
        limit=limit,
    )


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
