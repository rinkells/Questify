"""Writes on the character that more than one front end performs."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .errors import DomainError
from .models import Character
from .validators import validate_timezone_name


class TimezoneChangeLockedError(DomainError):
    code = 'timezone_change_locked'


def ensure_character(user):
    """Return the user's character, recreating the row if it is missing.

    Not locked: use ``Character.locked_for_user_id`` before a write.
    """
    character = Character.objects.filter(user=user).first()
    if character is None:
        character, _ = Character.objects.get_or_create(user=user)
    return character


def check_timezone_change(character, new_timezone):
    """Refuse a zone switch within 24 hours of a completion or failure.

    Failed completions count too: they occupy the day window just like
    completed ones, and a zone switch would otherwise reopen that day.
    """
    if new_timezone == character.timezone:
        return
    completed_recently = character.user.quest_completions.filter(
        completed_at__gte=timezone.now() - timedelta(hours=24),
    ).exists()
    if completed_recently:
        raise TimezoneChangeLockedError(
            'Timezone can be changed 24 hours after the last quest '
            'completion or failure.'
        )


def realign_streak(character):
    """Re-express the streak's last day in the character's new timezone.

    Streak gaps are measured in the current timezone. Leaving the stored
    date in the old zone would let a timezone switch shrink a real gap
    (up to ~26 hours) and keep a streak without spending a freeze.
    """
    from quests.models import Streak

    streak = Streak.objects.select_for_update().filter(
        user_id=character.user_id,
        quest_category=None,
    ).first()
    if streak is None or streak.last_completed_date is None:
        return
    last_completion = character.user.quest_completions.filter(
        status='completed',
    ).order_by('-completed_at').first()
    if last_completion is None:
        return
    streak.last_completed_date = timezone.localtime(
        last_completion.completed_at,
        ZoneInfo(character.timezone),
    ).date()
    streak.save(update_fields=('last_completed_date',))
    # The prefetched copy a view loaded is now stale.
    if hasattr(character.user, 'prefetched_streaks'):
        del character.user.prefetched_streaks


@transaction.atomic
def change_timezone(user, new_timezone):
    """Switch the user's timezone; returns the saved character.

    Locks the character row, so it serializes with quest completions, which
    the 24-hour rule reads. Raises ``django.core.exceptions.ValidationError``
    for a name that is no IANA zone and ``TimezoneChangeLockedError`` inside
    the 24-hour window. The API's CharacterSerializer performs the same two
    steps (``check_timezone_change`` and ``realign_streak``) around its own
    save, under the lock its view takes.
    """
    validate_timezone_name(new_timezone)
    character = Character.locked_for_user_id(user.pk)
    if new_timezone == character.timezone:
        return character
    check_timezone_change(character, new_timezone)
    character.timezone = new_timezone
    character.save(update_fields=('timezone',))
    realign_streak(character)
    return character
