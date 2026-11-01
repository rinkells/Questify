"""Reads of the character that more than one front end performs."""
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from django.db.models import Prefetch
from django.utils import timezone

from .levels import XPProgress, xp_progress
from .models import Character, Stat, StatType
from .services import ensure_character


def character_queryset():
    """Characters with what CharacterSerializer reads: user, stats, streaks.

    Three queries however many stats there are.
    """
    return Character.objects.select_related('user').prefetch_related(
        Prefetch(
            'stats',
            queryset=Stat.objects.select_related('stat_type'),
        ),
        Prefetch(
            'user__streaks',
            to_attr='prefetched_streaks',
        ),
    )


def get_character(user, *, for_update=False):
    """The user's character with its stats and streaks, recreated if missing.

    ``for_update`` locks the character row only (``of=('self',)``), so it must
    run inside a transaction.
    """
    queryset = character_queryset()
    if for_update:
        queryset = queryset.select_for_update(of=('self',))
    try:
        return queryset.get(user=user)
    except Character.DoesNotExist:
        # Recreate a missing character instead of failing every request.
        ensure_character(user)
        return queryset.get(user=user)


def get_current_streak(character):
    """The length of the character's running streak, 0 if it has lapsed.

    A streak is running when its last day is today or yesterday in the
    character's timezone, or the day before with a freeze to bridge it.
    Uses ``character.user.prefetched_streaks`` when ``get_character`` loaded it.
    """
    prefetched_streaks = getattr(character.user, 'prefetched_streaks', None)
    if prefetched_streaks is None:
        streak = character.user.streaks.filter(quest_category=None).first()
    else:
        streak = next(
            (item for item in prefetched_streaks if item.quest_category_id is None),
            None,
        )
    if streak is None or streak.last_completed_date is None:
        return 0

    today = timezone.localdate(timezone=ZoneInfo(character.timezone))
    days_since_completion = (today - streak.last_completed_date).days
    streak_is_current = 0 <= days_since_completion <= 1 or (
        days_since_completion == 2 and streak.freezes_available > 0
    )
    return streak.current_length if streak_is_current else 0


@dataclass(frozen=True)
class StatValue:
    name: str
    display_name: str
    icon: str
    value: int


@dataclass(frozen=True)
class CharacterOverview:
    character: Character
    level: int
    xp: int
    coins: int
    timezone: str
    progress: XPProgress
    current_streak: int
    # Every stat type, 0 for those the character has not earned yet.
    stats: list[StatValue]


def character_overview(user):
    """Level, XP progress, coins, streak and stats in four queries."""
    character = get_character(user)
    values = {stat.stat_type_id: stat.value for stat in character.stats.all()}
    stats = [
        StatValue(
            name=stat_type.name,
            display_name=stat_type.display_name,
            icon=stat_type.icon,
            value=values.get(stat_type.pk, 0),
        )
        for stat_type in StatType.objects.order_by('pk')
    ]
    return CharacterOverview(
        character=character,
        level=character.level,
        xp=character.xp,
        coins=character.coins,
        timezone=character.timezone,
        progress=xp_progress(character.level, character.xp),
        current_streak=get_current_streak(character),
        stats=stats,
    )
