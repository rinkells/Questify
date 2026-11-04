"""Read-only aggregates over a user's quest completion history.

``QuestCompletion`` is the record of progress: it keeps the XP actually awarded
and the category name at completion time, and it survives quest archiving and
deletion (``quest`` becomes NULL, ``user`` stays). Achievements and skills both
read progress through these helpers, so neither app imports the other.
"""
from dataclasses import dataclass
from datetime import datetime

from django.db.models import (
    Count,
    Exists,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    prefetch_related_objects,
)
from django.utils import timezone

from core.services import ensure_character

from .models import Quest, QuestCategory, QuestCompletion, QuestStatReward
from .services import DAILY_BONUS_QUEST_TYPES, category_key, local_day
from .xp_cap import get_xp_24h_cap, xp_left_in_24h

# How many one-time quests the "today" screen lists; the rest are behind
# "all quests". Daily and habit quests are always all listed.
TODAY_QUEST_LIMIT = 5
# How many quests one page of the all-quests list holds.
QUEST_LIST_PAGE_SIZE = 20
# The tabs of the all-quests list.
QUEST_LIST_ACTIVE = 'active'
QUEST_LIST_COMPLETED = 'completed'
QUEST_LIST_ARCHIVE = 'archive'
# ``quest_list(category=...)``: quests with no category.
UNCATEGORIZED = 'none'


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
    # When it was last completed, on any day. Only the all-quests list shows it.
    last_done_at: datetime | None = None

    @property
    def can_complete(self):
        """Whether complete_quest would accept it, leaving aside the XP cap."""
        return self.state == 'open' and not self.expired and self.quest.is_active

    @property
    def can_fail(self):
        """Whether fail_quest would accept it: a missed deadline does not stop it."""
        return self.state == 'open' and self.quest.is_active


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


def _status_within(day_start, next_day_start):
    """The latest completion status of a quest within the day, as an annotation."""
    return Subquery(
        QuestCompletion.objects.filter(
            quest=OuterRef('pk'),
            completed_at__gte=day_start,
            completed_at__lt=next_day_start,
        ).order_by('-completed_at').values('status')[:1]
    )


def _today_bounds(user, now):
    return local_day(now, ensure_character(user).timezone)[1:]


def _today_entry(quest, state, now, *, last_done_at=None):
    return TodayQuest(
        quest=quest,
        state=state,
        expired=quest.deadline is not None and quest.deadline < now,
        last_done_at=last_done_at,
    )


def _with_states(quests, day_start, next_day_start):
    """Annotate what ``_state_entry`` reads, in the same query as the quests."""
    completions = QuestCompletion.objects.filter(quest=OuterRef('pk'))
    return quests.annotate(
        today_status=_status_within(day_start, next_day_start),
        last_status=Subquery(
            completions.order_by('-completed_at').values('status')[:1],
        ),
        last_done_at=Subquery(
            completions.filter(
                status=QuestCompletion.CompletionStatus.COMPLETED,
            ).order_by('-completed_at').values('completed_at')[:1],
        ),
    ).select_related('category')


def _state_entry(quest, now):
    """A row for a quest from ``_with_states``.

    A daily or habit quest is judged by today, a one-time quest by its whole
    history, since it can be finished only once.
    """
    state = (
        quest.today_status
        if quest.quest_type in DAILY_BONUS_QUEST_TYPES
        else quest.last_status
    )
    return _today_entry(quest, state or 'open', now, last_done_at=quest.last_done_at)


def today_quests(user, *, limit=TODAY_QUEST_LIMIT, now=None):
    """The quests to act on today.

    All active daily and habit quests come first, with today's state in the
    character's timezone and no limit; open ones before completed and failed
    ones, each group by id. Then come the active one-time quests that have no
    completion or failure yet, by deadline (the nearest first, none last, then
    by id), at most ``limit`` of them (``None`` lists all). Four queries.
    """
    now = now or timezone.now()
    day_start, next_day_start = _today_bounds(user, now)
    active = Quest.objects.filter(
        user=user,
        is_active=True,
        archived_at__isnull=True,
    )
    routines = [
        _today_entry(quest, quest.today_status or 'open', now)
        for quest in active.filter(
            quest_type__in=DAILY_BONUS_QUEST_TYPES,
        ).annotate(
            today_status=_status_within(day_start, next_day_start),
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
        one_time=[_today_entry(quest, 'open', now) for quest in page],
        total=total,
        limit=limit,
    )


def finished_one_time_today(user, *, now=None):
    """One-time quests completed or failed today, oldest id first.

    ``today_quests`` lists one-time quests only while they are open, so this is
    what keeps one finished today on the screen until the day ends. Two queries.
    """
    now = now or timezone.now()
    day_start, next_day_start = _today_bounds(user, now)
    quests = Quest.objects.filter(
        user=user,
        is_active=True,
        archived_at__isnull=True,
        quest_type=Quest.QuestType.ONE_TIME,
    ).annotate(
        today_status=_status_within(day_start, next_day_start),
    ).filter(today_status__isnull=False).select_related('category').order_by('pk')
    return [_today_entry(quest, quest.today_status, now) for quest in quests]


def _one_entry(user, quests, quest_id, now):
    now = now or timezone.now()
    day_start, next_day_start = _today_bounds(user, now)
    quest = _with_states(
        quests.filter(pk=quest_id),
        day_start,
        next_day_start,
    ).first()
    return None if quest is None else _state_entry(quest, now)


def today_quest(user, quest_id, *, now=None):
    """One unarchived quest of the user's with its state, or None.

    What a row shows after an action on it, whatever the action found. Two queries.
    """
    return _one_entry(
        user,
        Quest.objects.filter(user=user, archived_at__isnull=True),
        quest_id,
        now,
    )


def listed_quest(user, quest_id, *, now=None):
    """Like ``today_quest``, and an archived quest too: a row of the all-quests list."""
    return _one_entry(user, Quest.objects.filter(user=user), quest_id, now)


@dataclass(frozen=True)
class QuestPage:
    items: list[TodayQuest]
    # Pass as ``before`` for the next page; None on the last one.
    next_before: int | None


def quest_list(
    user,
    *,
    status=QUEST_LIST_ACTIVE,
    quest_type=None,
    category=None,
    before=None,
    limit=QUEST_LIST_PAGE_SIZE,
    now=None,
):
    """One page of the user's quests, the newest first. Two queries.

    Every quest is in some tab, or it could never be found again:

    - active: not archived, and either daily or habit, which come back every
      day, or one-time with no completion or failure. A quest switched off
      with ``is_active`` stays here, or nothing would lead back to it.
    - completed: not archived, and completed at least once, or one-time and
      failed, which ends it just the same. A daily quest done yesterday is
      therefore both here and among the active ones.
    - archive: archived, whatever else became of it.

    ``quest_type`` and ``category`` (an id or ``UNCATEGORIZED``) narrow it down.
    Pages go by id rather than by offset: a quest leaving the tab while the
    user reads, as an archived one does, would otherwise push a row they have
    not seen onto the page they already have.
    """
    now = now or timezone.now()
    day_start, next_day_start = _today_bounds(user, now)
    quests = _with_states(
        Quest.objects.filter(
            user=user,
            archived_at__isnull=status != QUEST_LIST_ARCHIVE,
        ),
        day_start,
        next_day_start,
    )
    one_time = Q(quest_type=Quest.QuestType.ONE_TIME)
    if status == QUEST_LIST_ACTIVE:
        quests = quests.filter(~one_time | Q(last_status__isnull=True))
    elif status == QUEST_LIST_COMPLETED:
        quests = quests.filter(
            Q(last_done_at__isnull=False)
            | (one_time & Q(last_status__isnull=False)),
        )
    if quest_type is not None:
        quests = quests.filter(quest_type=quest_type)
    if category == UNCATEGORIZED:
        quests = quests.filter(category__isnull=True)
    elif category is not None:
        quests = quests.filter(category_id=category)
    if before is not None:
        quests = quests.filter(pk__lt=before)
    # One more than the page, to know whether another follows without a count.
    found = list(quests.order_by('-pk')[:limit + 1])
    page = found[:limit]
    return QuestPage(
        items=[_state_entry(quest, now) for quest in page],
        next_before=page[-1].pk if len(found) > limit else None,
    )


def has_completions(quest):
    """Whether the quest was ever completed or failed: its type is then fixed."""
    return QuestCompletion.objects.filter(quest=quest).exists()


def prefetch_stat_rewards(quests):
    """Load what each quest pays in stats, in one query whatever their number."""
    prefetch_related_objects(
        list(quests),
        Prefetch(
            'stat_rewards',
            queryset=QuestStatReward.objects.select_related(
                'stat_type',
            ).order_by('pk'),
        ),
    )


@dataclass(frozen=True)
class XPUsage:
    earned: int
    cap: int


def xp_usage_24h(user, *, now=None):
    """XP counted toward the 24-hour cap, for display; no lock is taken.

    Reads the same sources as the cap itself, so boss rewards are left out. Legacy
    completions count at their highest possible value, which can leave the shown
    figure at the cap.
    """
    cap = get_xp_24h_cap()
    return XPUsage(
        earned=cap - xp_left_in_24h(user.pk, now or timezone.now()),
        cap=cap,
    )


def completed_quests(user_id):
    return QuestCompletion.objects.filter(
        user_id=user_id,
        status=QuestCompletion.CompletionStatus.COMPLETED,
    )


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
    or in their apostrophe share one count.
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
