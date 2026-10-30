"""The 24-hour XP cap and the sources of XP it counts.

Each kind of XP under the cap registers a source here from its app's
``ready()``: quest completions and daily bonuses (quests). The cap sums them
all, so it can count XP awarded by other apps without importing them; XP
whose app registers no source is outside the cap. Boss rewards are the one
such XP, deliberately: they pay for long work, are bounded by the boss and
step limits, and their steps fall under the cap themselves.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum

from .models import MAX_XP_REWARD, DailyBonus, QuestCompletion


_sources = []


def register_xp_source(source):
    """Register ``source(user_id, since)``: XP the user earned from ``since`` on."""
    if source not in _sources:
        _sources.append(source)


def get_xp_24h_cap():
    # Read at call time so the cap follows the active settings.
    return getattr(settings, 'QUESTIFY_XP_24H_CAP', 1_000)


def xp_left_in_24h(user_id, now):
    """The XP the user may still earn in the 24 hours up to ``now``.

    Callers hold the character lock and read ``now`` only after taking it:
    an earlier timestamp can predate rows committed by a request that locked
    first and hide their XP from the cap.
    """
    since = now - timedelta(hours=24)
    earned = sum(source(user_id, since) for source in _sources)
    return max(0, get_xp_24h_cap() - earned)


def completed_quest_xp(user_id, since):
    """XP of completions since then; an estimated legacy value counts as the most."""
    totals = QuestCompletion.objects.filter(
        user_id=user_id,
        status=QuestCompletion.CompletionStatus.COMPLETED,
        completed_at__gte=since,
    ).aggregate(
        exact=Sum('xp_awarded', filter=Q(xp_awarded_is_estimate=False)),
        estimated=Count('pk', filter=Q(xp_awarded_is_estimate=True)),
    )
    return (totals['exact'] or 0) + totals['estimated'] * MAX_XP_REWARD


def daily_bonus_xp(user_id, since):
    return DailyBonus.objects.filter(
        user_id=user_id,
        awarded_at__gte=since,
    ).aggregate(total=Sum('xp_awarded'))['total'] or 0
