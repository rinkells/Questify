"""Unit tests of the boss services and selectors (the web UI calls them directly)."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from quests.models import Quest
from quests.services import QuestService

from .models import Boss, BossDefeat, BossStep
from .selectors import (
    attachable_quests,
    get_boss,
    list_bosses,
    step_status,
)
from .services import (
    BossConflictError,
    BossLimitError,
    BossService,
    BossStepError,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(username='boss-service-user', password='x')


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username='boss-service-other', password='x')


def make_quest(user, title='Step', **fields):
    fields.setdefault('quest_type', Quest.QuestType.ONE_TIME)
    fields.setdefault('xp_reward', 50)
    return Quest.objects.create(user=user, title=title, **fields)


def code_of(callable_, *args, **kwargs):
    with pytest.raises(Exception) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


# -- create / update ----------------------------------------------------------------


def test_create_boss_returns_the_plain_row_and_shows_it_with_progress(user):
    boss = BossService.create_boss(user, title='Dragon', description='Big')

    assert (boss.user, boss.title, boss.description) == (user, 'Dragon', 'Big')
    shown = get_boss(user, boss.pk)
    assert (shown.hp, shown.max_hp) == (0, 0)


def test_create_boss_refuses_fields_it_does_not_know(user):
    with pytest.raises(TypeError):
        BossService.create_boss(user, title='x', archived_at=timezone.now())


@override_settings(QUESTIFY_MAX_BOSSES_PER_USER=2, QUESTIFY_MAX_ACTIVE_BOSSES=5)
def test_create_boss_counts_archived_bosses_toward_the_quota(user, other_user):
    BossService.create_boss(other_user, title='theirs')
    first = BossService.create_boss(user, title='1')
    BossService.create_boss(user, title='2')
    BossService.archive_boss(user, first.pk)

    with pytest.raises(BossLimitError) as refusal:
        BossService.create_boss(user, title='3')

    assert refusal.value.code == 'boss_limit_reached'
    assert refusal.value.field is None
    assert str(refusal.value) == 'Boss limit reached for this account.'


@override_settings(QUESTIFY_MAX_BOSSES_PER_USER=10, QUESTIFY_MAX_ACTIVE_BOSSES=1)
def test_create_boss_limits_the_bosses_in_progress_but_not_the_archived(user):
    first = BossService.create_boss(user, title='1')

    assert code_of(BossService.create_boss, user, title='2') == (
        'active_boss_limit_reached'
    )

    BossService.archive_boss(user, first.pk)
    assert BossService.create_boss(user, title='2').title == '2'


def test_update_boss_changes_title_and_description(user):
    boss = BossService.create_boss(user, title='Old')

    BossService.update_boss(user, boss.pk, title='New', description='Changed')

    boss.refresh_from_db()
    assert (boss.title, boss.description) == ('New', 'Changed')


def test_update_boss_is_a_does_not_exist_for_foreign_and_archived_bosses(
    user,
    other_user,
):
    theirs = BossService.create_boss(other_user, title='theirs')
    archived = BossService.create_boss(user, title='gone')
    BossService.archive_boss(user, archived.pk)

    for boss_id in (theirs.pk, archived.pk, 10**6, 'abc'):
        with pytest.raises(Boss.DoesNotExist):
            BossService.update_boss(user, boss_id, title='x')


# -- steps: codes ---------------------------------------------------------------------


def test_attach_step_refusals_carry_codes(user, other_user):
    boss = BossService.create_boss(user, title='Dragon')
    theirs = make_quest(other_user)
    daily = make_quest(user, quest_type=Quest.QuestType.DAILY)
    inactive = make_quest(user, is_active=False)
    late = make_quest(user, deadline=timezone.now() - timedelta(hours=1))
    failed = make_quest(user)
    QuestService.fail_quest(failed.pk, user)
    taken = make_quest(user)
    other_boss = BossService.create_boss(user, title='Other')
    BossService.attach_step(user, other_boss.pk, taken.pk)
    archived = make_quest(user)
    QuestService.archive_quest(user, archived.pk)

    attach = BossService.attach_step
    assert code_of(attach, user, boss.pk, theirs.pk) == 'quest_not_found'
    assert code_of(attach, user, boss.pk, archived.pk) == 'quest_not_found'
    assert code_of(attach, user, boss.pk, daily.pk) == 'quest_not_one_time'
    assert code_of(attach, user, boss.pk, inactive.pk) == 'quest_inactive'
    assert code_of(attach, user, boss.pk, late.pk) == 'quest_deadline_passed'
    assert code_of(attach, user, boss.pk, failed.pk) == 'quest_already_done'
    assert code_of(attach, user, boss.pk, taken.pk) == 'quest_already_step'
    assert not BossStep.objects.filter(boss=boss).exists()


@override_settings(QUESTIFY_MAX_BOSS_STEPS=3)
def test_a_full_boss_says_how_many_steps_it_allows(user):
    boss = BossService.create_boss(user, title='Dragon')
    for index in range(3):
        BossService.attach_step(user, boss.pk, make_quest(user, f's{index}').pk)

    with pytest.raises(BossStepError) as refusal:
        BossService.attach_step(user, boss.pk, make_quest(user, 'one more').pk)

    assert refusal.value.code == 'boss_steps_full'
    assert refusal.value.params == {'limit': 3}


def test_step_changes_of_a_defeated_boss_and_completed_steps_are_conflicts(user):
    boss = BossService.create_boss(user, title='Dragon')
    quests = [make_quest(user, f's{index}') for index in range(3)]
    for quest in quests:
        BossService.attach_step(user, boss.pk, quest.pk)
    QuestService.complete_quest(quests[0].pk, user)

    with pytest.raises(BossConflictError) as refusal:
        BossService.detach_step(user, boss.pk, quests[0].pk)
    assert refusal.value.code == 'step_completed'

    QuestService.complete_quest(quests[1].pk, user)
    QuestService.complete_quest(quests[2].pk, user)
    assert BossDefeat.objects.filter(boss=boss).exists()
    extra = make_quest(user, 'extra')
    assert code_of(BossService.attach_step, user, boss.pk, extra.pk) == 'boss_defeated'
    assert code_of(BossService.detach_step, user, boss.pk, quests[0].pk) == 'boss_defeated'


# -- selectors -----------------------------------------------------------------------------


def test_list_bosses_shows_hp_and_steps_in_a_fixed_number_of_queries(
    user,
    other_user,
    django_assert_num_queries,
):
    BossService.create_boss(other_user, title='theirs')
    for number in range(3):
        boss = BossService.create_boss(user, title=f'Boss {number}')
        for index in range(2):
            BossService.attach_step(user, boss.pk, make_quest(user, f'{number}-{index}').pk)
    gone = BossService.create_boss(user, title='gone')
    BossService.archive_boss(user, gone.pk)

    # The bosses, their steps (with quests) and their defeats.
    with django_assert_num_queries(3):
        bosses = list(list_bosses(user))
        steps = [step for boss in bosses for step in boss.steps.all()]
        [step_status(step) for step in steps]
        [boss.defeat for boss in bosses if hasattr(boss, 'defeat')]

    assert [boss.title for boss in bosses] == ['Boss 0', 'Boss 1', 'Boss 2']
    assert all((boss.hp, boss.max_hp) == (2, 2) for boss in bosses)
    assert len(steps) == 6


def test_get_boss_does_not_find_foreign_or_archived_bosses(user, other_user):
    theirs = BossService.create_boss(other_user, title='theirs')
    archived = BossService.create_boss(user, title='gone')
    BossService.archive_boss(user, archived.pk)

    for boss_id in (theirs.pk, archived.pk):
        with pytest.raises(Boss.DoesNotExist):
            get_boss(user, boss_id)


def test_step_status_covers_each_state(user):
    boss = BossService.create_boss(user, title='Dragon')
    open_quest = make_quest(user, 'open')
    done = make_quest(user, 'done')
    failed = make_quest(user, 'failed')
    late = make_quest(user, 'late', deadline=timezone.now() + timedelta(hours=1))
    off = make_quest(user, 'off')
    for quest in (open_quest, done, failed, late, off):
        BossService.attach_step(user, boss.pk, quest.pk)
    QuestService.complete_quest(done.pk, user)
    QuestService.fail_quest(failed.pk, user)
    Quest.objects.filter(pk=off.pk).update(is_active=False)

    steps = {
        step.quest.title: step
        for step in get_boss(user, boss.pk).steps.all()
    }

    assert {title: step_status(step) for title, step in steps.items()} == {
        'open': 'open',
        'done': 'completed',
        'failed': 'failed',
        'late': 'open',
        'off': 'inactive',
    }
    assert step_status(steps['late'], now=timezone.now() + timedelta(hours=2)) == 'expired'


def test_attachable_quests_match_what_attach_step_accepts(user, other_user):
    boss = BossService.create_boss(user, title='Dragon')
    good = make_quest(user, 'good')
    dated = make_quest(user, 'dated', deadline=timezone.now() + timedelta(days=1))
    candidates = {
        'good': good,
        'dated': dated,
        'theirs': make_quest(other_user, 'theirs'),
        'daily': make_quest(user, 'daily', quest_type=Quest.QuestType.DAILY),
        'inactive': make_quest(user, 'inactive', is_active=False),
        'late': make_quest(user, 'late', deadline=timezone.now() - timedelta(hours=1)),
        'failed': make_quest(user, 'failed'),
        'done': make_quest(user, 'done'),
        'taken': make_quest(user, 'taken'),
        'archived': make_quest(user, 'archived'),
    }
    QuestService.fail_quest(candidates['failed'].pk, user)
    QuestService.complete_quest(candidates['done'].pk, user)
    BossService.attach_step(user, BossService.create_boss(user, title='X').pk, candidates['taken'].pk)
    QuestService.archive_quest(user, candidates['archived'].pk)

    listed = list(attachable_quests(user))

    assert listed == [good, dated]
    for quest in listed:
        BossService.attach_step(user, boss.pk, quest.pk)
    for title in ('theirs', 'daily', 'inactive', 'late', 'failed', 'done', 'taken', 'archived'):
        assert code_of(
            BossService.attach_step,
            user,
            boss.pk,
            candidates[title].pk,
        ).startswith(('quest_', 'boss_')), title
    assert not attachable_quests(user).exists()
