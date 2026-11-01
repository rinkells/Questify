"""Unit tests of the quest services and selectors (the web UI calls them directly)."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.errors import NotFoundError
from core.models import Character

from .models import Quest, QuestCategory, QuestCompletion
from .selectors import TODAY_QUEST_LIMIT, categories_for_user, quests_for_user, today_quests
from .services import (
    XP_REWARD_BY_TYPE,
    CategoryService,
    QuestAlreadyCompletedError,
    QuestAlreadyFailedError,
    QuestInputError,
    QuestRuleError,
    QuestService,
)

User = get_user_model()
Type = Quest.QuestType
Status = QuestCompletion.CompletionStatus


@pytest.fixture
def user(db):
    return User.objects.create_user(username='quest-service-user', password='x')


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username='quest-service-other', password='x')


def make_quest(user, title='q', quest_type=Type.ONE_TIME, **fields):
    return Quest.objects.create(
        user=user,
        title=title,
        quest_type=quest_type,
        xp_reward=XP_REWARD_BY_TYPE[quest_type],
        **fields,
    )


def codes(callable_, *args, **kwargs):
    """The code of the error ``callable_`` raises."""
    with pytest.raises(Exception) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


# -- create / update / archive ------------------------------------------------


@pytest.mark.parametrize(
    ('quest_type', 'reward'),
    [(Type.DAILY, 25), (Type.HABIT, 25), (Type.ONE_TIME, 50)],
)
def test_create_quest_pays_the_servers_reward_for_its_type(user, quest_type, reward):
    quest = QuestService.create_quest(user, title='Run', quest_type=quest_type)

    assert quest.user == user
    assert quest.xp_reward == reward
    assert quest.is_active
    assert Quest.objects.get(pk=quest.pk).xp_reward == reward


def test_create_quest_refuses_fields_it_does_not_know(user):
    with pytest.raises(TypeError):
        QuestService.create_quest(
            user,
            title='Run',
            quest_type=Type.DAILY,
            xp_reward=1000,
        )


@override_settings(QUESTIFY_MAX_QUESTS_PER_USER=2)
def test_create_quest_stops_at_the_quota_and_archived_quests_count(user, other_user):
    make_quest(other_user)
    first = QuestService.create_quest(user, title='1', quest_type=Type.DAILY)
    QuestService.create_quest(user, title='2', quest_type=Type.DAILY)
    QuestService.archive_quest(user, first.pk)

    with pytest.raises(QuestInputError) as refusal:
        QuestService.create_quest(user, title='3', quest_type=Type.DAILY)

    assert refusal.value.code == 'quest_limit_reached'
    assert refusal.value.field is None
    assert str(refusal.value) == 'Quest limit reached for this account.'


def test_create_quest_refuses_a_category_deleted_after_validation(user):
    gone = QuestCategory.objects.create(user=user, name='gone')
    stale = QuestCategory(pk=gone.pk, user=user, name='gone')
    gone.delete()

    with pytest.raises(QuestInputError) as refusal:
        QuestService.create_quest(
            user,
            title='q',
            quest_type=Type.DAILY,
            category=stale,
        )

    assert (refusal.value.code, refusal.value.field) == ('category_gone', 'category')
    assert not Quest.objects.filter(user=user).exists()


def test_create_quest_refuses_another_users_category(user, other_user):
    theirs = QuestCategory.objects.create(user=other_user, name='theirs')

    assert codes(
        QuestService.create_quest,
        user,
        title='q',
        quest_type=Type.DAILY,
        category=theirs,
    ) == 'category_gone'


def test_update_quest_changes_the_reward_with_the_type(user):
    quest = make_quest(user, quest_type=Type.ONE_TIME)

    updated = QuestService.update_quest(user, quest.pk, quest_type=Type.DAILY)

    assert updated.xp_reward == 25
    quest.refresh_from_db()
    assert (quest.quest_type, quest.xp_reward) == (Type.DAILY, 25)


def test_update_quest_keeps_the_reward_when_the_type_stays(user):
    quest = make_quest(user, quest_type=Type.ONE_TIME)
    Quest.objects.filter(pk=quest.pk).update(xp_reward=35)

    QuestService.update_quest(user, quest.pk, quest_type=Type.ONE_TIME, title='New')

    quest.refresh_from_db()
    assert (quest.title, quest.xp_reward) == ('New', 35)


def test_update_quest_refuses_a_type_change_after_a_completion(user):
    quest = make_quest(user)
    QuestService.complete_quest(quest.pk, user)

    with pytest.raises(QuestInputError) as refusal:
        QuestService.update_quest(user, quest.pk, quest_type=Type.DAILY)

    assert (refusal.value.code, refusal.value.field) == (
        'quest_type_locked',
        'quest_type',
    )


def test_update_quest_refuses_to_move_a_passed_deadline_but_keeps_it_as_is(user):
    deadline = timezone.now() - timedelta(hours=1)
    quest = make_quest(user, deadline=deadline)

    with pytest.raises(QuestInputError) as refusal:
        QuestService.update_quest(user, quest.pk, deadline=timezone.now() + timedelta(days=1))
    assert (refusal.value.code, refusal.value.field) == ('deadline_locked', 'deadline')

    # Resubmitting the stored value, as a PUT does, is not a change.
    QuestService.update_quest(user, quest.pk, deadline=deadline, title='Renamed')
    quest.refresh_from_db()
    assert quest.title == 'Renamed'


def test_update_quest_is_not_found_for_foreign_archived_and_malformed_ids(
    user,
    other_user,
):
    theirs = make_quest(other_user)
    archived = make_quest(user)
    QuestService.archive_quest(user, archived.pk)

    for quest_id in (theirs.pk, archived.pk, 10**6, 'abc'):
        assert codes(QuestService.update_quest, user, quest_id, title='x') == (
            'quest_not_found'
        )


def test_archive_quest_hides_and_deactivates_but_keeps_history(user):
    quest = make_quest(user)
    QuestService.complete_quest(quest.pk, user)

    QuestService.archive_quest(user, quest.pk)

    quest.refresh_from_db()
    assert quest.archived_at is not None
    assert not quest.is_active
    assert QuestCompletion.objects.filter(quest=quest).count() == 1
    assert not quests_for_user(user).filter(pk=quest.pk).exists()
    assert codes(QuestService.archive_quest, user, quest.pk) == 'quest_not_found'


# -- complete / fail through the lock ------------------------------------------


def test_complete_for_user_returns_the_result_and_takes_three_character_locks(user):
    quest = make_quest(user)

    with CaptureQueriesContext(connection) as queries:
        result = QuestService.complete_for_user(user, quest.pk)

    assert result['xp_gained'] == 50
    locks = [
        q['sql']
        for q in queries
        if 'FROM "core_character"' in q['sql'] and 'FOR UPDATE' in q['sql']
    ]
    # complete_quest, LevelCheckHandler and StreakService.record_completion.
    assert len(locks) == 3
    assert Character.objects.get(user=user).xp == 50


def test_complete_and_fail_for_user_do_not_find_other_users_or_archived_quests(
    user,
    other_user,
):
    theirs = make_quest(other_user)
    archived = make_quest(user)
    QuestService.archive_quest(user, archived.pk)

    for quest_id in (theirs.pk, archived.pk, 'abc'):
        assert codes(QuestService.complete_for_user, user, quest_id) == 'quest_not_found'
        assert codes(QuestService.fail_for_user, user, quest_id) == 'quest_not_found'
    assert Character.objects.get(user=other_user).xp == 0


def test_refusals_carry_machine_codes(user):
    quest = make_quest(user)
    QuestService.complete_for_user(user, quest.pk)
    failed = make_quest(user, title='failed')
    QuestService.fail_for_user(user, failed.pk)
    inactive = make_quest(user, title='inactive', is_active=False)
    late = make_quest(user, title='late', deadline=timezone.now() - timedelta(hours=1))

    assert codes(QuestService.complete_for_user, user, quest.pk) == 'quest_already_completed'
    assert codes(QuestService.fail_for_user, user, quest.pk) == 'quest_already_completed'
    assert codes(QuestService.complete_for_user, user, failed.pk) == 'quest_already_failed'
    assert codes(QuestService.fail_for_user, user, failed.pk) == 'quest_already_failed'
    assert codes(QuestService.complete_for_user, user, inactive.pk) == 'quest_inactive'
    assert codes(QuestService.fail_for_user, user, inactive.pk) == 'quest_inactive'
    assert codes(QuestService.complete_for_user, user, late.pk) == 'quest_deadline_passed'
    # A missed deadline is exactly what a failure records.
    QuestService.fail_for_user(user, late.pk)


def test_refusal_texts_are_the_ones_the_api_has_always_sent(user):
    quest = make_quest(user)
    QuestService.complete_for_user(user, quest.pk)

    with pytest.raises(QuestAlreadyCompletedError) as refusal:
        QuestService.complete_for_user(user, quest.pk)
    assert str(refusal.value) == f'Quest {quest.pk} has already been completed.'

    daily = make_quest(user, quest_type=Type.DAILY)
    QuestService.fail_for_user(user, daily.pk)
    with pytest.raises(QuestAlreadyFailedError) as refusal:
        QuestService.complete_for_user(user, daily.pk)
    assert str(refusal.value) == 'This daily or habit quest has already been failed today.'


@override_settings(QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER_PER_DAY=1)
def test_fail_limits_have_their_own_codes(user):
    QuestService.fail_for_user(user, make_quest(user).pk)

    with pytest.raises(QuestRuleError) as refusal:
        QuestService.fail_for_user(user, make_quest(user, title='2').pk)

    assert refusal.value.code == 'daily_fail_limit_reached'


@override_settings(QUESTIFY_XP_24H_CAP=50)
def test_the_xp_cap_has_a_code(user):
    QuestService.complete_for_user(user, make_quest(user).pk)

    with pytest.raises(QuestRuleError) as refusal:
        QuestService.complete_for_user(user, make_quest(user, title='2').pk)

    assert refusal.value.code == 'xp_limit_reached'


# -- categories -----------------------------------------------------------------


def test_create_category_belongs_to_the_user(user):
    category = CategoryService.create_category(user, name='Study', icon='📚')

    assert (category.user, category.name, category.icon) == (user, 'Study', '📚')


@override_settings(QUESTIFY_MAX_CATEGORIES_PER_USER=1)
def test_create_category_stops_at_the_quota(user, other_user):
    CategoryService.create_category(other_user, name='theirs')
    CategoryService.create_category(user, name='one')

    with pytest.raises(QuestInputError) as refusal:
        CategoryService.create_category(user, name='two')

    assert refusal.value.code == 'category_limit_reached'
    assert str(refusal.value) == 'Category limit reached for this account.'


def test_update_category_changes_only_the_users_own(user, other_user):
    mine = CategoryService.create_category(user, name='old')
    theirs = CategoryService.create_category(other_user, name='theirs')

    CategoryService.update_category(user, mine.pk, name='new')

    mine.refresh_from_db()
    assert mine.name == 'new'
    assert codes(CategoryService.update_category, user, theirs.pk, name='x') == (
        'category_not_found'
    )
    assert codes(CategoryService.update_category, user, 'abc', name='x') == (
        'category_not_found'
    )


def test_delete_category_detaches_its_quests(user, other_user):
    category = CategoryService.create_category(user, name='temp')
    quest = make_quest(user, category=category)
    theirs = CategoryService.create_category(other_user, name='theirs')

    CategoryService.delete_category(user, category.pk)

    quest.refresh_from_db()
    assert quest.category_id is None
    assert not QuestCategory.objects.filter(pk=category.pk).exists()
    assert codes(CategoryService.delete_category, user, theirs.pk) == 'category_not_found'
    assert codes(CategoryService.delete_category, user, 'abc') == 'category_not_found'
    assert QuestCategory.objects.filter(pk=theirs.pk).exists()


def test_categories_for_user_lists_only_their_own(user, other_user):
    mine = CategoryService.create_category(user, name='mine')
    CategoryService.create_category(other_user, name='theirs')

    assert list(categories_for_user(user)) == [mine]


# -- the "today" list ---------------------------------------------------------------


def titles(items):
    return [item.quest.title for item in items]


def test_today_lists_all_routines_and_only_five_one_time_quests(user):
    now = timezone.now()
    daily = make_quest(user, 'daily', Type.DAILY)
    habit = make_quest(user, 'habit', Type.HABIT)
    for index in range(6):
        make_quest(user, f'once {index}', deadline=now + timedelta(days=index + 1))

    today = today_quests(user)

    assert [item.quest for item in today.routines] == [daily, habit]
    assert titles(today.one_time) == [f'once {index}' for index in range(5)]
    assert titles(today.items) == ['daily', 'habit'] + titles(today.one_time)
    assert today.total == 6
    assert today.has_more


def test_today_has_no_link_to_all_quests_up_to_five_one_time_quests(user):
    for index in range(5):
        make_quest(user, f'once {index}')

    today = today_quests(user)

    assert (len(today.one_time), today.total, today.has_more) == (5, 5, False)


def test_today_never_limits_the_routines(user):
    for index in range(9):
        make_quest(user, f'daily {index}', Type.DAILY if index % 2 else Type.HABIT)

    today = today_quests(user)

    assert len(today.routines) == 9
    assert (today.one_time, today.total, today.has_more) == ([], 0, False)


def test_today_one_time_quests_go_by_deadline_the_undated_last_then_by_id(user):
    now = timezone.now()
    undated_first = make_quest(user, 'undated 1')
    later = make_quest(user, 'later', deadline=now + timedelta(days=3))
    sooner = make_quest(user, 'sooner', deadline=now + timedelta(hours=2))
    undated_second = make_quest(user, 'undated 2')
    tied_second = make_quest(user, 'tied 2', deadline=now + timedelta(days=1))
    Quest.objects.filter(pk=tied_second.pk).update(deadline=later.deadline)

    quests = [item.quest for item in today_quests(user).one_time]

    assert quests == [sooner, later, tied_second, undated_first, undated_second]


def test_today_puts_open_routines_before_completed_and_failed_ones(user):
    done = make_quest(user, 'done', Type.DAILY)
    open_first = make_quest(user, 'open 1', Type.HABIT)
    failed = make_quest(user, 'failed', Type.DAILY)
    open_second = make_quest(user, 'open 2', Type.DAILY)
    QuestService.complete_quest(done.pk, user)
    QuestService.fail_quest(failed.pk, user)

    routines = today_quests(user).routines

    assert [(item.quest, item.state) for item in routines] == [
        (open_first, 'open'),
        (open_second, 'open'),
        (done, 'completed'),
        (failed, 'failed'),
    ]


def test_a_daily_completed_today_stays_in_the_list_as_completed(user):
    daily = make_quest(user, 'daily', Type.DAILY)
    QuestService.complete_quest(daily.pk, user)

    today = today_quests(user)

    assert [(item.quest, item.state) for item in today.routines] == [
        (daily, 'completed'),
    ]


def test_today_leaves_out_finished_inactive_archived_and_foreign_quests(
    user,
    other_user,
):
    done = make_quest(user, 'done')
    failed = make_quest(user, 'failed')
    QuestService.complete_quest(done.pk, user)
    QuestService.fail_quest(failed.pk, user)
    make_quest(user, 'inactive', is_active=False)
    make_quest(user, 'inactive daily', Type.DAILY, is_active=False)
    archived = make_quest(user, 'archived')
    QuestService.archive_quest(user, archived.pk)
    make_quest(other_user, 'theirs', Type.DAILY)
    make_quest(other_user, 'theirs once')
    open_one = make_quest(user, 'open')

    today = today_quests(user)

    assert today.routines == []
    assert [item.quest for item in today.one_time] == [open_one]
    assert today.total == 1


def test_today_marks_an_expired_one_time_quest_so_it_can_only_be_failed(user):
    late = make_quest(user, 'late', deadline=timezone.now() - timedelta(hours=1))

    item = today_quests(user).one_time[0]

    assert (item.quest, item.state, item.expired) == (late, 'open', True)


def test_today_limit_none_lists_every_one_time_quest(user):
    for index in range(7):
        make_quest(user, f'once {index}')

    today = today_quests(user, limit=None)

    assert (len(today.one_time), today.total, today.has_more) == (7, 7, False)


def test_today_follows_the_characters_local_day(user):
    daily = make_quest(user, 'daily', Type.DAILY)
    Character.objects.filter(user=user).update(timezone='Pacific/Kiritimati')
    now = timezone.now()
    # Completed 30 hours ago: on another local day in any zone.
    QuestCompletion.objects.create(
        user=user,
        quest=daily,
        status=Status.COMPLETED,
        completed_at=now - timedelta(hours=30),
    )

    assert today_quests(user, now=now).routines[0].state == 'open'

    QuestCompletion.objects.create(
        user=user,
        quest=daily,
        status=Status.COMPLETED,
        completed_at=now,
    )
    assert today_quests(user, now=now).routines[0].state == 'completed'


def test_today_takes_four_queries_whatever_the_number_of_quests(
    user,
    django_assert_num_queries,
):
    for index in range(8):
        make_quest(user, f'daily {index}', Type.DAILY)
        make_quest(user, f'once {index}')

    # The character, the routines, the one-time count and its page.
    with django_assert_num_queries(4):
        today_quests(user)
