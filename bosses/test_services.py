"""Unit tests of the boss services and selectors (the web UI calls them directly)."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from quests.models import Quest, QuestCategory
from quests.services import CategoryService, QuestService

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


# -- create_boss_with_steps ---------------------------------------------------------


def test_create_boss_with_steps_attaches_the_quests_once_each(user):
    first = make_quest(user, 'First')
    second = make_quest(user, 'Second')

    boss = BossService.create_boss_with_steps(
        user,
        quest_ids=[second.pk, first.pk, second.pk],
        title='Dragon',
        description='Big',
    )

    assert (boss.title, boss.description) == ('Dragon', 'Big')
    shown = get_boss(user, boss.pk)
    assert (shown.hp, shown.max_hp) == (2, 2)
    assert [step.quest_id for step in shown.steps.all()] == [first.pk, second.pk]


def test_create_boss_with_steps_needs_no_steps(user):
    boss = BossService.create_boss_with_steps(user, title='Dragon')

    assert boss.steps.count() == 0


@pytest.mark.parametrize(
    ('make_refused', 'code'),
    [
        (lambda user: make_quest(user, quest_type=Quest.QuestType.DAILY), 'quest_not_one_time'),
        (lambda user: make_quest(user, is_active=False), 'quest_inactive'),
        (
            lambda user: make_quest(user, deadline=timezone.now() - timedelta(hours=1)),
            'quest_deadline_passed',
        ),
    ],
)
def test_a_step_refused_takes_the_new_boss_back_with_it(user, make_refused, code):
    fine = make_quest(user, 'Fine')
    refused = make_refused(user)

    assert code_of(
        BossService.create_boss_with_steps,
        user,
        quest_ids=[fine.pk, refused.pk],
        title='Dragon',
    ) == code

    assert not Boss.objects.exists()
    assert not BossStep.objects.exists()
    assert fine in attachable_quests(user)


def test_create_boss_with_steps_refuses_a_foreign_quest(user, other_user):
    theirs = make_quest(other_user)

    assert code_of(
        BossService.create_boss_with_steps,
        user,
        quest_ids=[theirs.pk],
        title='Dragon',
    ) == 'quest_not_found'

    assert not Boss.objects.exists()


@override_settings(QUESTIFY_MAX_BOSS_STEPS=2)
def test_create_boss_with_steps_refuses_too_many_before_taking_a_lock(user):
    quests = [make_quest(user, str(index)) for index in range(3)]

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(BossStepError) as refusal:
            BossService.create_boss_with_steps(
                user,
                quest_ids=[quest.pk for quest in quests],
                title='Dragon',
            )

    assert refusal.value.code == 'boss_steps_full'
    assert refusal.value.params == {'limit': 2}
    assert not any('FOR UPDATE' in query['sql'] for query in queries)
    assert not Boss.objects.exists()


@override_settings(QUESTIFY_MAX_ACTIVE_BOSSES=1)
def test_create_boss_with_steps_keeps_the_quotas(user):
    BossService.create_boss(user, title='First')
    quest = make_quest(user)

    assert code_of(
        BossService.create_boss_with_steps,
        user,
        quest_ids=[quest.pk],
        title='Second',
    ) == 'active_boss_limit_reached'

    assert Boss.objects.count() == 1
    assert quest in attachable_quests(user)


def test_create_boss_with_steps_locks_the_quests_before_the_character(user):
    # The order of complete_quest, or the two could deadlock.
    quests = [make_quest(user, str(index)) for index in range(2)]

    with CaptureQueriesContext(connection) as queries:
        BossService.create_boss_with_steps(
            user,
            quest_ids=[quest.pk for quest in quests],
            title='Dragon',
        )

    locks = [
        query['sql']
        for query in queries
        if 'FOR UPDATE' in query['sql']
    ]
    assert '"quests_quest"' in locks[0]
    assert 'ORDER BY' in locks[0]
    assert '"core_character"' in locks[1]


def test_detach_step_returns_the_quest_it_set_free(user):
    boss, quests = boss_with_steps(user, 4)

    assert BossService.detach_step(user, boss.pk, quests[0].pk) == quests[0]


# -- detach_step: the minimum of steps that count ------------------------------------


def boss_with_steps(user, count, title='Dragon'):
    """A boss with this many steps; rows, so also one the rules no longer allow."""
    boss = Boss.objects.create(user=user, title=title)
    quests = [make_quest(user, f'{title} {index}') for index in range(count)]
    BossStep.objects.bulk_create(BossStep(boss=boss, quest=quest) for quest in quests)
    return boss, quests


def live_count(user, boss):
    return get_boss(user, boss.pk).max_hp


def test_a_step_that_counts_can_go_while_enough_are_left(user):
    boss, quests = boss_with_steps(user, 4)

    BossService.detach_step(user, boss.pk, quests[0].pk)

    assert live_count(user, boss) == 3
    assert quests[0] in attachable_quests(user)


def test_a_step_that_counts_stays_when_too_few_would_be_left(user):
    boss, quests = boss_with_steps(user, 3)

    with pytest.raises(BossStepError) as refusal:
        BossService.detach_step(user, boss.pk, quests[0].pk)

    assert refusal.value.code == 'boss_min_steps'
    assert refusal.value.params == {'minimum': 3}
    assert str(refusal.value) == 'A boss must keep at least 3 steps.'
    assert live_count(user, boss) == 3


@pytest.mark.parametrize('minimum', [2, 5])
def test_the_minimum_in_the_refusal_is_the_setting(user, settings, minimum):
    settings.QUESTIFY_BOSS_MIN_STEPS = minimum
    boss, quests = boss_with_steps(user, minimum)

    with pytest.raises(BossStepError) as refusal:
        BossService.detach_step(user, boss.pk, quests[0].pk)

    assert str(refusal.value) == f'A boss must keep at least {minimum} steps.'
    assert refusal.value.params == {'minimum': minimum}


@pytest.mark.parametrize('how', ['archived', 'made daily'])
def test_a_step_that_does_not_count_can_always_go(user, how):
    boss, quests = boss_with_steps(user, 3)
    extra = make_quest(user, 'Extra')
    BossStep.objects.create(boss=boss, quest=extra)
    if how == 'archived':
        QuestService.archive_quest(user, extra.pk)
    else:
        Quest.objects.filter(pk=extra.pk).update(quest_type=Quest.QuestType.DAILY)
    assert live_count(user, boss) == 3

    BossService.detach_step(user, boss.pk, extra.pk)

    assert live_count(user, boss) == 3
    assert not BossStep.objects.filter(quest=extra).exists()


def test_an_old_boss_below_the_minimum_sheds_only_what_does_not_count(user):
    boss, (counted,) = boss_with_steps(user, 1)
    extra = make_quest(user, 'Archived')
    BossStep.objects.create(boss=boss, quest=extra)
    QuestService.archive_quest(user, extra.pk)

    assert code_of(BossService.detach_step, user, boss.pk, counted.pk) == 'boss_min_steps'

    BossService.detach_step(user, boss.pk, extra.pk)
    assert live_count(user, boss) == 1
    assert code_of(BossService.detach_step, user, boss.pk, counted.pk) == 'boss_min_steps'


def test_a_failed_step_at_the_minimum_needs_another_step_first(user):
    boss, quests = boss_with_steps(user, 3)
    QuestService.fail_quest(quests[0].pk, user)

    assert code_of(BossService.detach_step, user, boss.pk, quests[0].pk) == 'boss_min_steps'

    BossService.attach_step(user, boss.pk, make_quest(user, 'Instead').pk)
    BossService.detach_step(user, boss.pk, quests[0].pk)
    assert live_count(user, boss) == 3


def test_the_earlier_refusals_come_before_the_minimum(user):
    boss, quests = boss_with_steps(user, 3)
    QuestService.complete_quest(quests[0].pk, user)

    assert code_of(BossService.detach_step, user, boss.pk, quests[0].pk) == 'step_completed'

    QuestService.complete_quest(quests[1].pk, user)
    QuestService.complete_quest(quests[2].pk, user)
    assert code_of(BossService.detach_step, user, boss.pk, quests[2].pk) == 'boss_defeated'


def test_a_foreign_boss_at_the_minimum_reads_as_missing(user, other_user):
    boss, quests = boss_with_steps(other_user, 3)

    with pytest.raises(Boss.DoesNotExist):
        BossService.detach_step(user, boss.pk, quests[0].pk)

    assert live_count(other_user, boss) == 3


def test_detaching_reads_the_steps_after_taking_the_locks_in_their_order(user):
    boss, quests = boss_with_steps(user, 4)

    with CaptureQueriesContext(connection) as queries:
        BossService.detach_step(user, boss.pk, quests[0].pk)

    sql = [query['sql'] for query in queries]

    def first(*parts):
        return next(
            index
            for index, statement in enumerate(sql)
            if all(part in statement for part in parts)
        )

    quest_lock = first('FROM "quests_quest"', 'FOR UPDATE')
    character_lock = first('FROM "core_character"', 'FOR UPDATE')
    boss_lock = first('FROM "bosses_boss"', 'FOR UPDATE')
    read = first('FROM "bosses_bossstep"', '"archived_at" IS NULL')
    assert quest_lock < character_lock < boss_lock < read
    assert sum('FROM "core_character"' in s and 'FOR UPDATE' in s for s in sql) == 1


# -- add_new_step --------------------------------------------------------------------


def defeated_boss(user):
    boss, quests = boss_with_steps(user, 3)
    for quest in quests:
        QuestService.complete_quest(quest.pk, user)
    assert BossDefeat.objects.filter(boss=boss).exists()
    return boss


def test_add_new_step_creates_a_one_time_quest_and_attaches_it(user):
    boss = BossService.create_boss(user, title='Dragon')

    step = BossService.add_new_step(user, boss.pk, {'title': 'Sword'})

    assert step.boss == boss
    assert (step.quest.user, step.quest.title) == (user, 'Sword')
    assert step.quest.quest_type == Quest.QuestType.ONE_TIME
    assert step.quest.xp_reward == 50
    assert live_count(user, boss) == 1


def test_add_new_step_makes_a_one_time_quest_whatever_the_data_names(user):
    boss = BossService.create_boss(user, title='Dragon')

    step = BossService.add_new_step(
        user,
        boss.pk,
        {'title': 'Sword', 'quest_type': Quest.QuestType.DAILY},
    )

    assert step.quest.quest_type == Quest.QuestType.ONE_TIME
    assert step.quest.xp_reward == 50


def test_add_new_step_takes_a_category_and_a_deadline(user):
    boss = BossService.create_boss(user, title='Dragon')
    category = CategoryService.create_category(user, name='Study')
    deadline = timezone.now() + timedelta(days=1)

    step = BossService.add_new_step(
        user,
        boss.pk,
        {'title': 'Sword', 'category': category, 'deadline': deadline},
    )

    assert (step.quest.category, step.quest.deadline) == (category, deadline)


def test_add_new_step_brings_a_new_category_or_finds_the_same_name(user):
    boss = BossService.create_boss(user, title='Dragon')

    first = BossService.add_new_step(
        user,
        boss.pk,
        {'title': 'One', 'new_category': 'Study'},
    )
    second = BossService.add_new_step(
        user,
        boss.pk,
        {'title': 'Two', 'new_category': ' study '},
    )

    assert first.quest.category.name == 'Study'
    assert second.quest.category == first.quest.category


@override_settings(QUESTIFY_MAX_QUESTS_PER_USER=1)
def test_add_new_step_past_the_quest_quota_leaves_nothing(user):
    boss = BossService.create_boss(user, title='Dragon')
    make_quest(user, 'Takes the last place')

    code = code_of(
        BossService.add_new_step,
        user,
        boss.pk,
        {'title': 'Sword', 'new_category': 'Study'},
    )

    assert code == 'quest_limit_reached'
    assert list(Quest.objects.values_list('title', flat=True)) == ['Takes the last place']
    assert not QuestCategory.objects.exists()
    assert not BossStep.objects.exists()


@override_settings(QUESTIFY_MAX_BOSS_STEPS=1)
def test_add_new_step_past_the_step_limit_leaves_no_quest_or_category(user):
    boss, quests = boss_with_steps(user, 1)

    code = code_of(
        BossService.add_new_step,
        user,
        boss.pk,
        {'title': 'Sword', 'new_category': 'Study'},
    )

    assert code == 'boss_steps_full'
    assert list(Quest.objects.all()) == quests
    assert not QuestCategory.objects.exists()


@override_settings(QUESTIFY_MAX_CATEGORIES_PER_USER=0)
def test_add_new_step_past_the_category_quota_leaves_no_quest(user):
    boss = BossService.create_boss(user, title='Dragon')

    code = code_of(
        BossService.add_new_step,
        user,
        boss.pk,
        {'title': 'Sword', 'new_category': 'Study'},
    )

    assert code == 'category_limit_reached'
    assert not Quest.objects.exists()


def test_add_new_step_refuses_a_foreign_category_and_leaves_no_quest(user, other_user):
    boss = BossService.create_boss(user, title='Dragon')
    theirs = CategoryService.create_category(other_user, name='Theirs')

    code = code_of(
        BossService.add_new_step,
        user,
        boss.pk,
        {'title': 'Sword', 'category': theirs},
    )

    assert code == 'category_gone'
    assert not Quest.objects.exists()


def test_add_new_step_refuses_a_defeated_boss_and_leaves_no_quest(user):
    boss = defeated_boss(user)
    before = Quest.objects.count()

    code = code_of(BossService.add_new_step, user, boss.pk, {'title': 'Sword'})

    assert code == 'boss_defeated'
    assert Quest.objects.count() == before


@pytest.mark.parametrize('how', ['foreign', 'archived', 'missing'])
def test_add_new_step_reads_a_boss_not_on_offer_as_missing(user, other_user, how):
    owner = other_user if how == 'foreign' else user
    boss = BossService.create_boss(owner, title='Dragon')
    boss_id = boss.pk
    if how == 'archived':
        BossService.archive_boss(user, boss_id)
    elif how == 'missing':
        boss.delete()

    with pytest.raises(Boss.DoesNotExist):
        BossService.add_new_step(user, boss_id, {'title': 'Sword'})

    assert not Quest.objects.exists()


@override_settings(QUESTIFY_MAX_QUESTS_PER_USER=0)
def test_a_foreign_boss_is_missing_also_for_a_user_out_of_quests(user, other_user):
    boss = BossService.create_boss(other_user, title='Dragon')

    with pytest.raises(Boss.DoesNotExist):
        BossService.add_new_step(user, boss.pk, {'title': 'Sword'})


def test_a_defeated_boss_says_so_also_to_a_user_out_of_quests(user):
    boss = defeated_boss(user)

    with override_settings(QUESTIFY_MAX_QUESTS_PER_USER=0):
        code = code_of(BossService.add_new_step, user, boss.pk, {'title': 'Sword'})

    assert code == 'boss_defeated'


def test_add_new_step_locks_the_character_and_the_boss_before_it_creates(user):
    boss = BossService.create_boss(user, title='Dragon')

    with CaptureQueriesContext(connection) as queries:
        BossService.add_new_step(user, boss.pk, {'title': 'Sword'})

    sql = [query['sql'] for query in queries]
    created = next(
        index
        for index, statement in enumerate(sql)
        if statement.startswith('INSERT INTO "quests_quest"')
    )
    locks = [statement for statement in sql[:created] if 'FOR UPDATE' in statement]
    assert '"core_character"' in locks[0]
    assert '"bosses_boss"' in locks[1]
    assert not any('"quests_quest"' in statement for statement in locks)


# -- create_boss_with_steps: new steps -----------------------------------------------


def test_create_boss_with_steps_takes_quests_and_new_steps_together(user):
    free = make_quest(user, 'Free')

    boss = BossService.create_boss_with_steps(
        user,
        quest_ids=[free.pk],
        new_steps=[{'title': 'One'}, {'title': 'Two', 'new_category': 'Study'}],
        title='Dragon',
    )

    steps = boss.steps.order_by('pk').select_related('quest__category')
    assert [step.quest.title for step in steps] == ['Free', 'One', 'Two']
    assert steps[2].quest.category.name == 'Study'
    assert all(step.quest.quest_type == Quest.QuestType.ONE_TIME for step in steps)
    assert all(step.quest.xp_reward == 50 for step in steps)
    assert live_count(user, boss) == 3


@override_settings(QUESTIFY_MAX_BOSS_STEPS=2)
def test_new_steps_count_toward_the_most_allowed_before_any_lock(user):
    free = make_quest(user, 'Free')

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(BossStepError) as refusal:
            BossService.create_boss_with_steps(
                user,
                quest_ids=[free.pk],
                new_steps=[{'title': 'One'}, {'title': 'Two'}],
                title='Dragon',
            )

    assert refusal.value.code == 'boss_steps_full'
    assert not any('FOR UPDATE' in query['sql'] for query in queries)
    assert not Boss.objects.exists()
    assert Quest.objects.count() == 1


@override_settings(QUESTIFY_MAX_QUESTS_PER_USER=2)
def test_the_quest_quota_takes_the_boss_and_every_new_step_back(user):
    free = make_quest(user, 'Free')

    code = code_of(
        BossService.create_boss_with_steps,
        user,
        quest_ids=[free.pk],
        new_steps=[{'title': 'Fits', 'new_category': 'Study'}, {'title': 'Too many'}],
        title='Dragon',
    )

    assert code == 'quest_limit_reached'
    assert not Boss.objects.exists()
    assert not BossStep.objects.exists()
    assert not QuestCategory.objects.exists()
    assert list(Quest.objects.values_list('title', flat=True)) == ['Free']


def test_creating_with_new_steps_still_locks_the_quests_before_the_character(user):
    free = make_quest(user, 'Free')

    with CaptureQueriesContext(connection) as queries:
        BossService.create_boss_with_steps(
            user,
            quest_ids=[free.pk],
            new_steps=[{'title': 'One'}],
            title='Dragon',
        )

    locks = [query['sql'] for query in queries if 'FOR UPDATE' in query['sql']]
    assert '"quests_quest"' in locks[0]
    assert '"core_character"' in locks[1]
