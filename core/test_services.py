"""Unit tests of core's services and selectors (the web UI calls them directly)."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from quests.models import Quest, QuestCompletion, Streak

from .errors import DomainError, NotFoundError
from .levels import level_for_xp, xp_progress, xp_threshold
from .models import Character, Stat, StatType
from .selectors import character_overview, get_character, get_current_streak
from .services import (
    TimezoneChangeLockedError,
    change_timezone,
    check_timezone_change,
    ensure_character,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(username='core-service-user', password='x')


def test_domain_error_carries_a_code_params_and_the_english_text():
    error = DomainError('Not enough.', code='not_enough', balance=3, cost=5)

    assert str(error) == error.message == 'Not enough.'
    assert error.code == 'not_enough'
    assert error.params == {'balance': 3, 'cost': 5}
    assert error.field is None
    # Subclasses fix a default code; the class stays a ValueError for callers
    # that already caught that.
    assert DomainError('x').code == 'error'
    assert isinstance(error, ValueError)
    assert isinstance(NotFoundError('x'), LookupError)


def test_thresholds_follow_the_documented_formula():
    assert xp_threshold(0) == 0
    assert xp_threshold(1) == 100
    assert xp_threshold(2) == round(100 * 2**1.2)


@pytest.mark.parametrize('xp', [0, 1, 99, 100, 101, 229, 230, 10_000, 10**9])
def test_level_for_xp_agrees_with_the_thresholds(xp):
    level = level_for_xp(xp)

    assert xp_threshold(level - 1) <= xp < xp_threshold(level)


def test_xp_progress_reports_both_thresholds_of_the_level():
    first = xp_progress(1, 40)
    assert (first.level_start_xp, first.next_level_xp) == (0, 100)
    assert (first.xp_in_level, first.xp_to_next_level, first.percent) == (40, 60, 40)

    second = xp_progress(2, 150)
    assert second.level_start_xp == 100
    assert second.next_level_xp == xp_threshold(2)
    assert second.xp_in_level == 50
    assert second.xp_for_level == xp_threshold(2) - 100


def test_xp_progress_never_leaves_the_bar():
    # The stored level may be above what the XP alone gives; the bar is empty,
    # not negative, and a level exactly at its threshold is full.
    assert xp_progress(3, 0).percent == 0
    assert xp_progress(3, 0).xp_in_level == 0
    assert xp_progress(1, 10_000).percent == 100
    assert xp_progress(1, 10_000).xp_to_next_level == 0
    assert xp_progress(0, 5).level == 1


def test_ensure_character_recreates_a_missing_row(user):
    Character.objects.filter(user=user).delete()

    character = ensure_character(user)

    assert character.pk is not None
    assert character.timezone == 'UTC'
    assert Character.objects.filter(user=user).count() == 1
    assert ensure_character(user).pk == character.pk


def test_get_character_recreates_a_missing_row(user):
    Character.objects.filter(user=user).delete()

    character = get_character(user)

    assert character.user_id == user.pk


def test_get_character_for_update_locks_only_the_character_row(user):
    with transaction.atomic(), CaptureQueriesContext(connection) as queries:
        get_character(user, for_update=True)

    locking = [q['sql'] for q in queries if 'FOR UPDATE' in q['sql']]
    assert len(locking) == 1
    assert 'OF "core_character"' in locking[0]


def streak_for(user, **fields):
    return Streak.objects.create(user=user, quest_category=None, **fields)


def test_current_streak_is_zero_without_a_streak(user):
    assert get_current_streak(get_character(user)) == 0


def test_current_streak_counts_today_and_yesterday(user):
    today = timezone.localdate(timezone=None)
    streak = streak_for(user, current_length=4, last_completed_date=today)
    assert get_current_streak(get_character(user)) == 4

    streak.last_completed_date = today - timedelta(days=1)
    streak.save()
    assert get_current_streak(get_character(user)) == 4


def test_current_streak_lapses_after_a_gap_unless_a_freeze_bridges_it(user):
    today = timezone.localdate(timezone=None)
    streak = streak_for(
        user,
        current_length=4,
        last_completed_date=today - timedelta(days=2),
    )
    assert get_current_streak(get_character(user)) == 0

    streak.freezes_available = 1
    streak.save()
    assert get_current_streak(get_character(user)) == 4

    streak.last_completed_date = today - timedelta(days=3)
    streak.save()
    assert get_current_streak(get_character(user)) == 0


def test_current_streak_without_the_prefetched_copy_queries_the_user(user):
    today = timezone.localdate(timezone=None)
    streak_for(user, current_length=2, last_completed_date=today)
    character = Character.objects.select_related('user').get(user=user)

    assert not hasattr(character.user, 'prefetched_streaks')
    assert get_current_streak(character) == 2


def test_character_overview_gathers_what_the_dashboard_shows(
    user,
    django_assert_num_queries,
):
    earned = StatType.objects.create(name='STR', display_name='Strength', icon='💪')
    StatType.objects.create(name='INT', display_name='Intellect', icon='📚')
    Stat.objects.create(character=user.character, stat_type=earned, value=7)
    Character.objects.filter(user=user).update(xp=40, coins=12)
    streak_for(
        user,
        current_length=3,
        last_completed_date=timezone.localdate(timezone=None),
    )

    with django_assert_num_queries(4):
        overview = character_overview(user)

    assert (overview.level, overview.xp, overview.coins) == (1, 40, 12)
    assert overview.timezone == 'UTC'
    assert overview.current_streak == 3
    assert overview.progress.percent == 40
    # Every stat type, in creation order, 0 for the one not earned yet.
    assert [(s.name, s.display_name, s.value) for s in overview.stats] == [
        ('STR', 'Strength', 7),
        ('INT', 'Intellect', 0),
    ]


def test_change_timezone_saves_and_returns_the_character(user):
    character = change_timezone(user, 'Europe/Kyiv')

    assert character.timezone == 'Europe/Kyiv'
    assert Character.objects.get(user=user).timezone == 'Europe/Kyiv'


def test_change_timezone_to_the_same_zone_is_allowed_after_a_completion(user):
    QuestCompletion.objects.create(
        user=user,
        status=QuestCompletion.CompletionStatus.FAILED,
        completed_at=timezone.now(),
    )

    assert change_timezone(user, 'UTC').timezone == 'UTC'


@pytest.mark.parametrize(
    'status',
    [QuestCompletion.CompletionStatus.COMPLETED, QuestCompletion.CompletionStatus.FAILED],
)
def test_change_timezone_is_refused_within_24_hours_of_a_completion(user, status):
    QuestCompletion.objects.create(
        user=user,
        status=status,
        completed_at=timezone.now() - timedelta(hours=23),
    )

    with pytest.raises(TimezoneChangeLockedError) as refusal:
        change_timezone(user, 'Europe/Kyiv')

    assert refusal.value.code == 'timezone_change_locked'
    assert Character.objects.get(user=user).timezone == 'UTC'


def test_change_timezone_is_allowed_after_24_hours(user):
    QuestCompletion.objects.create(
        user=user,
        status=QuestCompletion.CompletionStatus.COMPLETED,
        completed_at=timezone.now() - timedelta(hours=25),
    )

    assert change_timezone(user, 'Asia/Tokyo').timezone == 'Asia/Tokyo'


def test_check_timezone_change_does_not_save(user):
    check_timezone_change(user.character, 'Asia/Tokyo')

    assert Character.objects.get(user=user).timezone == 'UTC'


def test_change_timezone_rejects_a_name_that_is_no_zone(user):
    with pytest.raises(ValidationError):
        change_timezone(user, 'Mars/Olympus')

    assert Character.objects.get(user=user).timezone == 'UTC'


def test_change_timezone_realigns_the_streaks_last_day(user):
    completed_at = timezone.now() - timedelta(hours=30)
    quest = Quest.objects.create(
        user=user,
        title='q',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=50,
    )
    QuestCompletion.objects.create(
        user=user,
        quest=quest,
        status=QuestCompletion.CompletionStatus.COMPLETED,
        completed_at=completed_at,
    )
    streak = streak_for(
        user,
        current_length=1,
        last_completed_date=date(2000, 1, 1),
    )

    change_timezone(user, 'Pacific/Kiritimati')

    streak.refresh_from_db()
    assert streak.last_completed_date == timezone.localtime(
        completed_at,
        ZoneInfo('Pacific/Kiritimati'),
    ).date()
