"""A user's own monthly summary, computed on the fly from their history.

Nothing is stored, so a past month changes with the history it is read
from. This app only reads: it imports quests, wallet, bosses, achievements
and skills, and none of them imports it, so it adds no dependency between
them.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

from achievements.models import UserAchievement
from bosses.models import BossDefeat
from core.models import Character
from quests.models import QuestCompletion
from skills.models import UserSkill
from wallet.models import WalletTransaction

MAX_MONTHS_BACK = 24

# ASCII digits only: \d also matches other scripts' digits, which int() reads.
MONTH_PATTERN = re.compile(r'([0-9]{4})-([0-9]{2})')


class MonthError(ValueError):
    """A month that is malformed, in the future or before summaries begin."""


@dataclass(frozen=True)
class XPBySource:
    quests: int
    daily_bonus: int
    bosses: int

    @property
    def total(self):
        return self.quests + self.daily_bonus + self.bosses


@dataclass(frozen=True)
class Unlocks:
    # In the order they were unlocked.
    names: tuple[str, ...]

    @property
    def count(self):
        return len(self.names)


@dataclass(frozen=True)
class MonthlySummary:
    month: date
    timezone: str
    starts_at: datetime
    ends_at: datetime
    partial: bool
    quests_completed: int
    quests_failed: int
    estimated_completions: int
    xp: XPBySource
    daily_bonuses: int
    coins_earned: int
    coins_spent: int
    balance_end: int
    bosses_defeated: int
    achievements_unlocked: Unlocks
    skills_unlocked: Unlocks
    current_level: int


def format_month(month):
    return f'{month.year:04d}-{month.month:02d}'


def parse_month(value):
    """Return the first day of a ``YYYY-MM`` month."""
    match = MONTH_PATTERN.fullmatch(value)
    if match is None:
        raise MonthError('month must be YYYY-MM.')
    year, month = map(int, match.groups())
    try:
        return date(year, month, 1)
    except ValueError:
        raise MonthError(f'{value} is not a month.') from None


def _month_index(day):
    return day.year * 12 + day.month - 1


def month_bounds(month, user_timezone):
    """Local midnight of the month's first day and of the next month's.

    Each month ends where the next begins, so every instant falls in one.
    """
    next_index = _month_index(month) + 1
    next_month = date(next_index // 12, next_index % 12 + 1, 1)
    return (
        timezone.make_aware(
            datetime.combine(month.replace(day=1), time.min),
            user_timezone,
        ),
        timezone.make_aware(
            datetime.combine(next_month, time.min),
            user_timezone,
        ),
    )


def monthly_summary(user, month=None, *, now=None):
    """Summarize a calendar month of ``user``'s history in their own timezone.

    ``month`` is any date in the month, the current one when omitted. Every
    field is read by time over ``[starts_at, ends_at)``. XP and coins come
    from the wallet ledger, which pays one coin per XP: exact, with no
    estimates, but only from the ledger's start. Six queries, however much
    history there is.

    Raises MonthError for a month in the future, more than MAX_MONTHS_BACK
    before the current one, or before the ledger starts.
    """
    character = Character.objects.filter(user=user).values(
        'timezone',
        'level',
    ).first()
    # A missing character is recreated with the defaults.
    timezone_name = character['timezone'] if character else 'UTC'
    current_level = character['level'] if character else 1
    user_timezone = ZoneInfo(timezone_name)
    current = timezone.localtime(now or timezone.now(), user_timezone).date()
    month = current if month is None else month
    months_back = _month_index(current) - _month_index(month)
    if months_back < 0:
        raise MonthError(f'{format_month(month)} is in the future.')
    if months_back > MAX_MONTHS_BACK:
        raise MonthError(
            f'Summaries go back at most {MAX_MONTHS_BACK} months.',
        )
    starts_at, ends_at = month_bounds(month, user_timezone)

    Kind = WalletTransaction.Kind
    in_month = Q(created_at__gte=starts_at, created_at__lt=ends_at)
    wallet = WalletTransaction.objects.filter(user=user).aggregate(
        first_at=Min('created_at'),
        initial_grants=Count('pk', filter=Q(kind=Kind.INITIAL_GRANT)),
        xp_quests=Sum('amount', filter=in_month & Q(kind=Kind.QUEST_REWARD)),
        xp_daily_bonus=Sum(
            'amount',
            filter=in_month & Q(kind=Kind.DAILY_BONUS),
        ),
        daily_bonuses=Count('pk', filter=in_month & Q(kind=Kind.DAILY_BONUS)),
        xp_bosses=Sum('amount', filter=in_month & Q(kind=Kind.BOSS_REWARD)),
        # Every credit but the initial grant, which repaid XP from before
        # the wallet rather than anything earned in the month.
        coins_earned=Sum(
            'amount',
            filter=in_month & Q(amount__gt=0) & ~Q(kind=Kind.INITIAL_GRANT),
        ),
        coins_spent=Sum('amount', filter=in_month & Q(kind=Kind.REDEMPTION)),
        # A user's amounts sum to the balance, so those before the end sum
        # to the balance then; for the current month, the balance now.
        balance_end=Sum('amount', filter=Q(created_at__lt=ends_at)),
    )
    # The initial grant lumps all XP earned before the wallet into one row,
    # so such a ledger itemizes earnings only from its first row on. Without
    # a grant the user earned nothing before the wallet, and the ledger has
    # been complete since the account was created.
    if wallet['initial_grants']:
        ledger_start = wallet['first_at']
        partial = starts_at < ledger_start
    else:
        ledger_start = user.date_joined
        partial = False
    first_month = timezone.localtime(ledger_start, user_timezone).date()
    if _month_index(month) < _month_index(first_month):
        raise MonthError(f'Summaries start at {format_month(first_month)}.')

    Status = QuestCompletion.CompletionStatus
    completions = QuestCompletion.objects.filter(
        user=user,
        completed_at__gte=starts_at,
        completed_at__lt=ends_at,
    ).aggregate(
        completed=Count('pk', filter=Q(status=Status.COMPLETED)),
        failed=Count('pk', filter=Q(status=Status.FAILED)),
        estimated=Count(
            'pk',
            filter=Q(status=Status.COMPLETED, xp_awarded_is_estimate=True),
        ),
    )
    bosses_defeated = BossDefeat.objects.filter(
        user=user,
        defeated_at__gte=starts_at,
        defeated_at__lt=ends_at,
    ).count()
    achievements = UserAchievement.objects.filter(
        user=user,
        unlocked_at__gte=starts_at,
        unlocked_at__lt=ends_at,
    ).order_by('unlocked_at', 'pk').values_list('achievement__name', flat=True)
    skills = UserSkill.objects.filter(
        user=user,
        unlocked_at__gte=starts_at,
        unlocked_at__lt=ends_at,
    ).order_by('unlocked_at', 'pk').values_list('skill__name', flat=True)

    return MonthlySummary(
        month=month.replace(day=1),
        timezone=timezone_name,
        starts_at=starts_at,
        ends_at=ends_at,
        partial=partial,
        quests_completed=completions['completed'],
        quests_failed=completions['failed'],
        estimated_completions=completions['estimated'],
        xp=XPBySource(
            quests=wallet['xp_quests'] or 0,
            daily_bonus=wallet['xp_daily_bonus'] or 0,
            bosses=wallet['xp_bosses'] or 0,
        ),
        daily_bonuses=wallet['daily_bonuses'],
        coins_earned=wallet['coins_earned'] or 0,
        coins_spent=-(wallet['coins_spent'] or 0),
        balance_end=wallet['balance_end'] or 0,
        bosses_defeated=bosses_defeated,
        achievements_unlocked=Unlocks(names=tuple(achievements)),
        skills_unlocked=Unlocks(names=tuple(skills)),
        current_level=current_level,
    )
