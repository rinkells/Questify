"""Reads of a user's achievements, with hidden ones masked until unlocked."""
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Exists, OuterRef, Subquery

from .models import Achievement, UserAchievement

# What a hidden, still locked achievement shows in place of its name.
MASKED_NAME = '???'


def achievements_for_user(user):
    """All achievements, each annotated with ``is_unlocked`` for the user."""
    return Achievement.objects.order_by('pk').annotate(
        is_unlocked=Exists(
            UserAchievement.objects.filter(
                user=user,
                achievement_id=OuterRef('pk'),
            )
        )
    )


def is_masked(achievement):
    """Whether the user must not learn what the achievement is yet.

    ``achievement`` carries the ``is_unlocked`` annotation of
    ``achievements_for_user``; without it, it counts as locked.
    """
    return achievement.is_hidden and not getattr(
        achievement,
        'is_unlocked',
        False,
    )


@dataclass(frozen=True)
class AchievementCard:
    """One achievement as shown to a user.

    A masked one (hidden and still locked) has the name ``MASKED_NAME`` and
    no id, icon, description or condition: nothing that gives it away.
    """

    id: int | None
    name: str
    description: str | None
    icon: str | None
    condition_type: str | None
    unlocked: bool
    unlocked_at: datetime | None
    masked: bool


def achievement_cards(user, *, unlocked_only=False):
    """The achievements with unlock dates, masked where hidden and locked.

    One query. The unlock date is the cheap half of progress: the evaluators
    answer only yes or no, so there is no "17 of 20" yet.
    """
    queryset = achievements_for_user(user).annotate(
        unlocked_at=Subquery(
            UserAchievement.objects.filter(
                user=user,
                achievement_id=OuterRef('pk'),
            ).values('unlocked_at')[:1]
        ),
    )
    if unlocked_only:
        queryset = queryset.filter(is_unlocked=True)
    cards = []
    for achievement in queryset:
        if is_masked(achievement):
            cards.append(AchievementCard(
                id=None,
                name=MASKED_NAME,
                description=None,
                icon=None,
                condition_type=None,
                unlocked=False,
                unlocked_at=None,
                masked=True,
            ))
            continue
        cards.append(AchievementCard(
            id=achievement.pk,
            name=achievement.name,
            description=achievement.description,
            icon=achievement.icon,
            condition_type=achievement.condition_type,
            unlocked=achievement.is_unlocked,
            unlocked_at=achievement.unlocked_at,
            masked=False,
        ))
    return cards


def next_achievement(user):
    """The first visible achievement the user does not have yet, or None; one query.

    "First" is catalog order: evaluators answer only yes or no, so how close an
    achievement is cannot be measured yet. Hidden ones are never offered.
    """
    return achievements_for_user(user).filter(
        is_unlocked=False,
        is_hidden=False,
    ).first()


def achievement_names(achievement_ids):
    """The names of these achievements, for telling the user they earned them.

    Only pass ids the user has just unlocked: a hidden one's name is not masked.
    One query, none without ids; in id order.
    """
    if not achievement_ids:
        return []
    return list(
        Achievement.objects.filter(pk__in=achievement_ids).order_by(
            'pk',
        ).values_list('name', flat=True)
    )
