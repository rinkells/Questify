import threading
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from achievements.models import Achievement, UserAchievement
from core.models import Character
from events.dispatcher import dispatcher
from events.event_types import LevelUpEvent
from quests import xp_cap
from quests.models import Quest, QuestCompletion
from quests.services import QuestService
from wallet.models import WalletTransaction

from .admin import BossAdmin, BossDefeatAdmin
from .models import Boss, BossDefeat, BossStep


User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(username='boss-user', password='password')


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username='other-boss-user', password='password')


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_quest(user, title='Step', **fields):
    fields.setdefault('quest_type', Quest.QuestType.ONE_TIME)
    fields.setdefault('xp_reward', 50)
    return Quest.objects.create(user=user, title=title, **fields)


def make_boss(user, steps=3, title='Dragon', xp_reward=50):
    boss = Boss.objects.create(user=user, title=title)
    quests = [
        make_quest(user, f'{title} step {index}', xp_reward=xp_reward)
        for index in range(steps)
    ]
    BossStep.objects.bulk_create(BossStep(boss=boss, quest=quest) for quest in quests)
    return boss, quests


def complete(user, quest):
    return QuestService.complete_quest(quest.id, user)


def complete_all(user, quests):
    return [complete(user, quest) for quest in quests]


def character(user):
    return Character.objects.get(user=user)


def attach(api_client, boss, quest):
    return api_client.post(
        f'/api/bosses/{boss.id}/steps/',
        {'quest': quest.id},
        format='json',
    )


def detach(api_client, boss, quest):
    return api_client.delete(f'/api/bosses/{boss.id}/steps/{quest.id}/')


def get_boss(api_client, boss):
    return api_client.get(f'/api/bosses/{boss.id}/')


def set_quest_type(api_client, quest, quest_type):
    response = api_client.patch(
        f'/api/quests/{quest.id}/',
        {'quest_type': quest_type},
        format='json',
    )
    assert response.status_code == 200, response.data


def character_locks(queries):
    return [
        query['sql']
        for query in queries
        if 'FROM "core_character"' in query['sql'] and 'FOR UPDATE' in query['sql']
    ]


def progress_queries(queries):
    """The with_progress() reads: the only grouped queries of bosses."""
    return [
        query['sql']
        for query in queries
        if 'FROM "bosses_boss"' in query['sql'] and 'GROUP BY' in query['sql']
    ]


def assert_ledger_matches(user):
    total = WalletTransaction.objects.filter(user=user).aggregate(
        total=Sum('amount'),
    )['total'] or 0
    assert total == character(user).coins


# --- Wiring ------------------------------------------------------------------


def test_boss_progress_runs_before_the_xp_and_level_handlers():
    # BossDefeatedEvent has no level check of its own: the completion's
    # LevelCheckHandler must come after the boss's XP.
    names = [
        type(getattr(handler, '__self__', handler)).__name__
        for _, handler in dispatcher._handlers
    ]

    assert names.index('BossProgressHandler') < names.index('XPRewardHandler')
    assert names.index('BossProgressHandler') < names.index('LevelCheckHandler')


def test_boss_rewards_are_the_one_xp_outside_the_cap():
    assert set(xp_cap._sources) == {
        xp_cap.completed_quest_xp,
        xp_cap.daily_bonus_xp,
    }


# --- Progress and defeat -----------------------------------------------------


@pytest.mark.django_db
def test_completing_a_step_reports_the_hit(user):
    boss, quests = make_boss(user)

    result = complete(user, quests[0])

    assert result['boss'] == {
        'id': boss.id,
        'title': 'Dragon',
        'hp': 2,
        'max_hp': 3,
        'defeated': None,
    }
    assert (result['xp_gained'], result['coins_gained']) == (50, 50)
    assert not BossDefeat.objects.exists()


@pytest.mark.django_db
def test_completing_a_quest_outside_any_boss_reports_none(user):
    make_boss(user)

    assert complete(user, make_quest(user, 'Loose'))['boss'] is None


@pytest.mark.django_db
def test_completing_the_last_step_defeats_the_boss(user):
    boss, quests = make_boss(user)

    *_, result = complete_all(user, quests)

    assert result['boss'] == {
        'id': boss.id,
        'title': 'Dragon',
        'hp': 0,
        'max_hp': 3,
        'defeated': {'xp': 75, 'coins': 75},
    }
    # The quest's own reward stays apart from the boss's.
    assert (result['xp_gained'], result['coins_gained']) == (50, 50)
    defeat = BossDefeat.objects.get(boss=boss)
    last = QuestCompletion.objects.get(quest=quests[-1])
    assert defeat.xp_awarded == 75
    assert defeat.quest_completion == last
    assert defeat.defeated_at == last.completed_at
    assert (character(user).xp, character(user).coins) == (225, 225)
    transaction = WalletTransaction.objects.get(kind='boss_reward')
    assert (transaction.amount, transaction.boss_defeat) == (75, defeat)
    assert_ledger_matches(user)


@pytest.mark.django_db
def test_the_completions_level_check_sees_the_boss_xp(user):
    _, quests = make_boss(user, xp_reward=10)

    *earlier, result = complete_all(user, quests)

    # 30 XP from the steps would stay at level 1; the boss's 75 cross 100.
    assert not any(item['leveled_up'] for item in earlier)
    assert (result['leveled_up'], result['new_level']) == (True, 2)
    assert character(user).level == 2


@pytest.mark.django_db
def test_quest_and_boss_xp_level_up_in_one_event(user):
    boss, quests = make_boss(user, xp_reward=10)
    Quest.objects.filter(pk=quests[-1].pk).update(xp_reward=200)
    complete_all(user, quests[:-1])
    spy = Mock()
    dispatcher.register(LevelUpEvent, spy)
    try:
        result = complete(user, quests[-1])
    finally:
        dispatcher._handlers.remove((LevelUpEvent, spy))

    # 220 XP from the steps reach level 2 alone, the boss's 75 level 3: one
    # check after both, so one event from 1 to 3 rather than two.
    assert (result['leveled_up'], result['new_level']) == (True, 3)
    spy.assert_called_once()
    event = spy.call_args.args[0]
    assert (event.old_level, event.new_level) == (1, 3)


@pytest.mark.django_db
def test_completing_a_step_takes_three_character_locks(user):
    _, quests = make_boss(user)

    with CaptureQueriesContext(connection) as queries:
        complete(user, quests[0])

    assert len(character_locks(queries)) == 3


@pytest.mark.django_db
def test_defeating_a_boss_takes_no_extra_character_locks(user):
    _, quests = make_boss(user)
    complete_all(user, quests[:-1])

    with CaptureQueriesContext(connection) as queries:
        result = complete(user, quests[-1])

    assert result['boss']['defeated'] is not None
    # complete_quest, LevelCheckHandler and StreakService.record_completion:
    # the boss's XP and coins are UPDATEs of the row already held.
    assert len(character_locks(queries)) == 3


@pytest.mark.django_db
def test_completing_a_daily_quest_does_not_look_for_a_boss(api_client, user):
    _, quests = make_boss(user, steps=4)
    # Not even a step turned daily: it is not live.
    set_quest_type(api_client, quests[3], 'daily')
    daily = make_quest(user, 'Daily', quest_type=Quest.QuestType.DAILY, xp_reward=25)

    with CaptureQueriesContext(connection) as queries:
        results = complete_all(user, [daily, quests[3]])

    assert [result['boss'] for result in results] == [None, None]
    assert not [query for query in queries if 'bosses_' in query['sql']]


@pytest.mark.django_db
def test_boss_writes_read_the_progress_only_for_their_response(api_client, user):
    boss, _ = make_boss(user)
    extra = make_quest(user, 'Fourth')
    url = f'/api/bosses/{boss.id}/'
    requests = [
        lambda: attach(api_client, boss, extra),
        lambda: detach(api_client, boss, extra),
        lambda: api_client.patch(url, {'title': 'Wyrm'}, format='json'),
        lambda: api_client.delete(url),
    ]

    counts = []
    for request in requests:
        with CaptureQueriesContext(connection) as queries:
            assert request().status_code in (200, 201, 204)
        counts.append(len(progress_queries(queries)))

    # Attaching and editing answer with the boss; looking it up is plain.
    assert counts == [1, 0, 1, 0]


@pytest.mark.django_db
def test_reward_is_paid_in_full_with_the_whole_cap_spent(user):
    complete(user, make_quest(user, 'Big one', xp_reward=850))
    boss, quests = make_boss(user)

    *_, result = complete_all(user, quests)

    # 850 + 3 x 50 spend the 1000 of the last 24 hours; the boss pays anyway.
    assert result['xp_gained'] == 50
    assert result['boss']['defeated'] == {'xp': 75, 'coins': 75}
    assert BossDefeat.objects.get(boss=boss).xp_awarded == 75
    assert (character(user).xp, character(user).coins) == (1075, 1075)
    assert_ledger_matches(user)


@pytest.mark.django_db
@override_settings(QUESTIFY_BOSS_XP_PER_STEP=400)
def test_reward_never_exceeds_the_largest_quest_reward(user):
    _, quests = make_boss(user)

    *_, result = complete_all(user, quests)

    assert result['boss']['defeated'] == {'xp': 1000, 'coins': 1000}


@pytest.mark.django_db
def test_the_database_refuses_a_defeat_that_pays_nothing(user):
    boss, _ = make_boss(user, steps=0)

    with pytest.raises(IntegrityError), transaction.atomic():
        BossDefeat.objects.create(boss=boss, user=user, xp_awarded=0)


@pytest.mark.django_db
def test_a_boss_needs_the_minimum_number_of_completed_steps(api_client, user):
    boss, quests = make_boss(user, steps=2)

    results = complete_all(user, quests)

    assert results[-1]['boss']['hp'] == 0
    assert results[-1]['boss']['defeated'] is None
    assert not BossDefeat.objects.exists()
    response = get_boss(api_client, boss)
    assert (response.data['hp'], response.data['defeated']) == (0, None)

    third = make_quest(user, 'Third')
    assert attach(api_client, boss, third).data['hp'] == 1
    result = complete(user, third)

    assert result['boss']['defeated'] == {'xp': 75, 'coins': 75}


@pytest.mark.django_db
def test_an_archived_step_drops_out_of_hp_and_reward(api_client, user):
    _, quests = make_boss(user, steps=4)
    assert api_client.delete(f'/api/quests/{quests[3].id}/').status_code == 204

    *_, result = complete_all(user, quests[:3])

    assert result['boss']['max_hp'] == 3
    assert result['boss']['defeated']['xp'] == 75


@pytest.mark.django_db
def test_archiving_the_last_open_step_leaves_the_boss_open(api_client, user):
    boss, quests = make_boss(user, steps=4)
    complete_all(user, quests[:3])

    assert api_client.delete(f'/api/quests/{quests[3].id}/').status_code == 204

    response = get_boss(api_client, boss)
    assert (response.data['hp'], response.data['max_hp']) == (0, 3)
    assert response.data['defeated'] is None
    assert not BossDefeat.objects.exists()

    # Beaten only at a completion: of a new step.
    new_step = make_quest(user, 'New step')
    assert attach(api_client, boss, new_step).status_code == 201
    result = complete(user, new_step)

    # Four completed live steps; the archived one gives no share.
    assert result['boss']['defeated']['xp'] == 100


@pytest.mark.django_db
def test_a_step_turned_daily_drops_out_like_an_archived_one(api_client, user):
    boss, quests = make_boss(user, steps=4)

    set_quest_type(api_client, quests[3], 'daily')
    *_, result = complete_all(user, quests[:3])

    assert result['boss']['max_hp'] == 3
    assert result['boss']['defeated']['xp'] == 75
    # Completing it later is no longer a hit.
    assert complete(user, quests[3])['boss'] is None
    step = get_boss(api_client, boss).data['steps'][3]
    assert (step['quest'], step['live']) == (quests[3].id, False)


@pytest.mark.django_db
def test_a_failed_step_blocks_the_boss_until_detached(api_client, user):
    boss, quests = make_boss(user, steps=4)
    QuestService.fail_quest(quests[3].id, user)

    *_, result = complete_all(user, quests[:3])

    # Failing does no damage.
    assert (result['boss']['hp'], result['boss']['defeated']) == (1, None)
    statuses = [step['status'] for step in get_boss(api_client, boss).data['steps']]
    assert statuses == ['completed', 'completed', 'completed', 'failed']

    assert detach(api_client, boss, quests[3]).status_code == 204
    assert not BossDefeat.objects.exists()
    new_step = make_quest(user, 'Retry')
    attach(api_client, boss, new_step)

    assert complete(user, new_step)['boss']['defeated']['xp'] == 100


@pytest.mark.django_db
def test_a_completed_step_cannot_be_detached(api_client, user):
    boss, quests = make_boss(user)
    complete(user, quests[0])

    response = detach(api_client, boss, quests[0])

    assert response.status_code == 409
    assert response.data == {'detail': 'A completed step cannot be detached.'}
    assert BossStep.objects.filter(quest=quests[0]).exists()


@pytest.mark.django_db
def test_a_defeated_boss_is_frozen_but_can_be_renamed_and_archived(api_client, user):
    boss, quests = make_boss(user, steps=4)
    # Not live, so it does not stop the defeat, and still attached after it.
    api_client.delete(f'/api/quests/{quests[3].id}/')
    complete_all(user, quests[:3])

    frozen = 'This boss has been defeated; its steps cannot change.'
    attached = attach(api_client, boss, make_quest(user, 'Too late'))
    detached = detach(api_client, boss, quests[3])
    renamed = api_client.patch(
        f'/api/bosses/{boss.id}/',
        {'title': 'Slain dragon'},
        format='json',
    )

    assert (attached.status_code, attached.data) == (409, {'detail': frozen})
    assert (detached.status_code, detached.data) == (409, {'detail': frozen})
    assert renamed.status_code == 200
    assert renamed.data['title'] == 'Slain dragon'
    assert renamed.data['defeated']['xp'] == 75
    assert api_client.delete(f'/api/bosses/{boss.id}/').status_code == 204
    assert BossDefeat.objects.filter(boss=boss).exists()


@pytest.mark.django_db
def test_a_defeated_boss_stays_as_it_was_beaten(api_client, user):
    boss, quests = make_boss(user, steps=4)
    set_quest_type(api_client, quests[3], 'daily')
    complete_all(user, quests[:3])

    # Allowed by quests, as the quest has no completion: it is live again
    # by the rule, but the boss was beaten without it.
    set_quest_type(api_client, quests[3], 'one_time')
    data = get_boss(api_client, boss).data

    assert (data['hp'], data['max_hp']) == (0, 3)
    assert data['defeated']['xp'] == 75
    assert [step['live'] for step in data['steps']] == [True, True, True, False]
    assert detach(api_client, boss, quests[3]).status_code == 409
    # Its completion hits nothing, and pays no second reward.
    assert complete(user, quests[3])['boss'] is None
    data = get_boss(api_client, boss).data
    assert (data['hp'], data['max_hp']) == (0, 3)
    assert data['steps'][3]['status'] == 'completed'
    assert WalletTransaction.objects.filter(kind='boss_reward').count() == 1


@pytest.mark.django_db
def test_archiving_a_step_after_the_defeat_changes_nothing(api_client, user):
    boss, quests = make_boss(user)
    complete_all(user, quests)

    assert api_client.delete(f'/api/quests/{quests[0].id}/').status_code == 204

    data = get_boss(api_client, boss).data
    assert (data['hp'], data['max_hp']) == (0, 3)
    assert all(step['live'] for step in data['steps'])
    counted = BossStep.objects.filter(boss=boss, counted=True)
    assert sorted(counted.values_list('quest', flat=True)) == [quest.id for quest in quests]


@pytest.mark.django_db
def test_archiving_a_boss_releases_its_unfinished_steps(api_client, user):
    boss, quests = make_boss(user)
    complete(user, quests[0])

    assert api_client.delete(f'/api/bosses/{boss.id}/').status_code == 204

    assert get_boss(api_client, boss).status_code == 404
    assert list(BossStep.objects.filter(boss=boss).values_list('quest', flat=True)) == [
        quests[0].id,
    ]
    other = Boss.objects.create(user=user, title='Hydra')
    assert attach(api_client, other, quests[1]).status_code == 201
    # A released quest completes as an ordinary one.
    assert complete(user, quests[2])['boss'] is None


@pytest.mark.django_db(transaction=True)
def test_parallel_completions_of_the_last_steps_defeat_the_boss_once(user):
    boss, quests = make_boss(user, steps=4)
    complete_all(user, quests[:2])
    barrier = threading.Barrier(2)
    statuses = []

    def run(quest):
        client = APIClient()
        client.force_authenticate(user=user)
        try:
            barrier.wait()
            statuses.append(
                client.post(f'/api/quests/{quest.id}/complete/', format='json').status_code,
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(quest,)) for quest in quests[2:]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [200, 200]
    assert BossDefeat.objects.get(boss=boss).xp_awarded == 100
    assert character(user).xp == 4 * 50 + 100
    assert WalletTransaction.objects.filter(kind='boss_reward').count() == 1
    assert_ledger_matches(user)


# --- The 24-hour cap ---------------------------------------------------------


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=250)
def test_boss_xp_leaves_the_cap_of_later_completions_alone(user):
    _, quests = make_boss(user)
    complete_all(user, quests)

    # Only the steps' 150 count, so 100 are left; the boss's 75 are not.
    assert complete(user, make_quest(user, 'Later'))['xp_gained'] == 50


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=250)
def test_boss_xp_leaves_the_cap_of_the_daily_bonus_alone(user):
    _, quests = make_boss(user)
    complete_all(user, quests)
    dailies = [
        make_quest(user, f'Daily {index}', quest_type=Quest.QuestType.DAILY, xp_reward=25)
        for index in range(3)
    ]

    results = complete_all(user, dailies)

    # 150 from the steps and 75 from the dailies leave the bonus its 25.
    assert [result['xp_gained'] for result in results] == [25, 25, 25]
    assert results[-1]['daily_bonus'] == {'xp': 25, 'coins': 25}


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=100)
def test_a_step_is_still_a_quest_under_the_cap(api_client, user):
    boss, quests = make_boss(user)
    complete_all(user, quests[:2])

    response = api_client.post(f'/api/quests/{quests[2].id}/complete/', format='json')

    assert response.status_code == 400
    assert response.data == {
        'detail': 'The XP limit for the last 24 hours has been reached.',
    }
    assert not QuestCompletion.objects.filter(quest=quests[2]).exists()
    assert not BossDefeat.objects.filter(boss=boss).exists()
    assert get_boss(api_client, boss).data['hp'] == 1


# --- Steps -------------------------------------------------------------------


@pytest.mark.django_db
def test_attaching_a_step_returns_the_boss(api_client, user):
    boss, _ = make_boss(user, steps=1)
    quest = make_quest(user, 'Second')

    response = attach(api_client, boss, quest)

    assert response.status_code == 201
    assert (response.data['hp'], response.data['max_hp']) == (2, 2)
    assert response.data['steps'][-1]['quest'] == quest.id
    assert response.data['steps'][-1]['status'] == 'open'


@pytest.mark.django_db
def test_attaching_refuses_unsuitable_quests(api_client, user, other_user):
    boss, _ = make_boss(user, steps=0)
    other_boss, (taken,) = make_boss(user, steps=1, title='Hydra')
    completed = make_quest(user, 'Done')
    complete(user, completed)
    failed = make_quest(user, 'Failed')
    QuestService.fail_quest(failed.id, user)
    cases = [
        (make_quest(user, 'Daily', quest_type=Quest.QuestType.DAILY),
         'Only one-time quests can be boss steps.'),
        (make_quest(user, 'Habit', quest_type=Quest.QuestType.HABIT),
         'Only one-time quests can be boss steps.'),
        (completed, 'This quest has already been completed or failed.'),
        (failed, 'This quest has already been completed or failed.'),
        (make_quest(user, 'Paused', is_active=False), 'This quest is inactive.'),
        (make_quest(user, 'Archived', is_active=False, archived_at=timezone.now()),
         'Quest not found.'),
        (make_quest(user, 'Late', deadline=timezone.now() - timedelta(hours=1)),
         'This quest deadline has passed.'),
        (make_quest(other_user, 'Foreign'), 'Quest not found.'),
        (taken, 'This quest is already a step of a boss.'),
    ]

    responses = [attach(api_client, boss, quest) for quest, _ in cases]

    assert [(r.status_code, r.data) for r in responses] == [
        (400, {'detail': message}) for _, message in cases
    ]
    assert not BossStep.objects.filter(boss=boss).exists()
    assert BossStep.objects.get(quest=taken).boss == other_boss


@pytest.mark.django_db
def test_a_quest_with_a_future_deadline_can_be_a_step(api_client, user):
    boss, _ = make_boss(user, steps=0)
    quest = make_quest(user, 'Soon', deadline=timezone.now() + timedelta(days=1))

    assert attach(api_client, boss, quest).status_code == 201


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_BOSS_STEPS=3)
def test_a_boss_has_a_step_limit(api_client, user):
    boss, _ = make_boss(user)

    response = attach(api_client, boss, make_quest(user, 'Fourth'))

    assert response.status_code == 400
    assert response.data == {'detail': 'This boss already has the most steps allowed.'}


@pytest.mark.django_db
def test_attaching_needs_a_quest_id(api_client, user):
    boss, _ = make_boss(user, steps=0)

    responses = [
        api_client.post(f'/api/bosses/{boss.id}/steps/', body, format='json')
        for body in ({}, {'quest': 'x'}, {'quest': 2**70})
    ]

    assert [response.status_code for response in responses] == [400, 400, 400]


@pytest.mark.django_db
def test_detaching_an_unknown_step_is_not_found(api_client, user):
    boss, _ = make_boss(user)
    elsewhere, (quest,) = make_boss(user, steps=1, title='Hydra')

    assert detach(api_client, boss, quest).status_code == 404
    assert api_client.delete(f'/api/bosses/{boss.id}/steps/999999/').status_code == 404
    assert BossStep.objects.filter(boss=elsewhere, quest=quest).exists()


@pytest.mark.django_db
def test_another_users_boss_is_not_found(api_client, user, other_user):
    boss, (quest,) = make_boss(other_user, steps=1)

    assert get_boss(api_client, boss).status_code == 404
    assert attach(api_client, boss, make_quest(user, 'Mine')).status_code == 404
    assert detach(api_client, boss, quest).status_code == 404
    assert api_client.delete(f'/api/bosses/{boss.id}/').status_code == 404
    assert Boss.objects.get(pk=boss.pk).archived_at is None


# --- Bosses API --------------------------------------------------------------


@pytest.mark.django_db
def test_create_and_list_bosses(api_client, user):
    response = api_client.post(
        '/api/bosses/',
        {'title': 'Hydra', 'description': 'Many heads'},
        format='json',
    )

    assert response.status_code == 201
    assert response.data['title'] == 'Hydra'
    assert (response.data['hp'], response.data['max_hp']) == (0, 0)
    assert (response.data['steps'], response.data['defeated']) == ([], None)
    listed = api_client.get('/api/bosses/').data['results']
    assert [boss['id'] for boss in listed] == [response.data['id']]


@pytest.mark.django_db
def test_boss_detail_derives_hp_from_its_steps(api_client, user):
    boss, quests = make_boss(user)
    complete(user, quests[0])
    QuestService.fail_quest(quests[1].id, user)

    data = get_boss(api_client, boss).data

    assert (data['hp'], data['max_hp']) == (2, 3)
    assert [step['status'] for step in data['steps']] == ['completed', 'failed', 'open']
    assert all(step['live'] for step in data['steps'])
    assert data['defeated'] is None


@pytest.mark.django_db
def test_steps_that_cannot_be_completed_say_why(api_client, user):
    boss, quests = make_boss(user, steps=5)
    passed = timezone.now() - timedelta(hours=1)
    Quest.objects.filter(pk=quests[1].pk).update(is_active=False)
    Quest.objects.filter(pk=quests[2].pk).update(deadline=passed)
    # Reactivating would not help this one: expired comes first.
    Quest.objects.filter(pk=quests[3].pk).update(is_active=False, deadline=passed)
    assert api_client.delete(f'/api/quests/{quests[4].id}/').status_code == 204

    data = get_boss(api_client, boss).data

    assert [(step['status'], step['live']) for step in data['steps']] == [
        ('open', True),
        ('inactive', True),
        ('expired', True),
        ('expired', True),
        ('inactive', False),
    ]
    # Live and never completed, so they block the boss.
    assert (data['hp'], data['max_hp']) == (4, 4)


@pytest.mark.django_db
def test_complete_endpoint_reports_the_boss(api_client, user):
    boss, quests = make_boss(user)

    response = api_client.post(f'/api/quests/{quests[0].id}/complete/', format='json')

    assert response.status_code == 200
    assert response.data['boss'] == {
        'id': boss.id,
        'title': 'Dragon',
        'hp': 2,
        'max_hp': 3,
        'defeated': None,
    }


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_BOSSES_PER_USER=2)
def test_archived_bosses_count_toward_the_lifetime_limit(api_client, user):
    first = api_client.post('/api/bosses/', {'title': 'One'}, format='json')
    api_client.delete(f"/api/bosses/{first.data['id']}/")
    api_client.post('/api/bosses/', {'title': 'Two'}, format='json')

    response = api_client.post('/api/bosses/', {'title': 'Three'}, format='json')

    assert response.status_code == 400
    assert response.data == {'detail': 'Boss limit reached for this account.'}


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_ACTIVE_BOSSES=1)
def test_only_bosses_in_progress_count_toward_the_active_limit(api_client, user):
    _, quests = make_boss(user)

    refused = api_client.post('/api/bosses/', {'title': 'Second'}, format='json')
    complete_all(user, quests)
    after_defeat = api_client.post('/api/bosses/', {'title': 'Second'}, format='json')
    api_client.delete(f"/api/bosses/{after_defeat.data['id']}/")
    after_archive = api_client.post('/api/bosses/', {'title': 'Third'}, format='json')

    assert refused.status_code == 400
    assert refused.data == {
        'detail': 'Too many bosses in progress; defeat or archive one first.',
    }
    assert (after_defeat.status_code, after_archive.status_code) == (201, 201)


@pytest.mark.django_db
def test_wallet_history_links_the_boss_reward(api_client, user):
    boss, quests = make_boss(user)
    complete_all(user, quests)

    transactions = api_client.get('/api/wallet/transactions/').data['results']

    boss_rewards = [item for item in transactions if item['kind'] == 'boss_reward']
    assert [item['boss_defeat'] for item in boss_rewards] == [boss.defeat.id]


# --- Achievements ------------------------------------------------------------


@pytest.mark.django_db
def test_a_defeat_unlocks_defeated_boss_achievements(user):
    achievement = Achievement.objects.create(
        name='Boss Slayer',
        condition_type=Achievement.ConditionType.BOSSES_DEFEATED_COUNT,
        condition_config={'threshold': 1},
    )
    _, quests = make_boss(user)

    results = complete_all(user, quests)

    assert [result['achievements_unlocked'] for result in results] == [
        [],
        [],
        [achievement.id],
    ]
    assert UserAchievement.objects.filter(user=user, achievement=achievement).count() == 1


@pytest.mark.django_db
def test_seed_achievements_creates_the_boss_achievement_once():
    for _ in range(2):
        call_command('seed_achievements', stdout=StringIO())

    achievement = Achievement.objects.get(name='Boss Slayer')
    assert achievement.condition_type == 'bosses_defeated_count'
    assert achievement.condition_config == {'threshold': 1}


# --- Admin -------------------------------------------------------------------


def superuser_request():
    request = RequestFactory().post('/admin/')
    request.user = User.objects.create_superuser(username='root', password='password')
    return request


@pytest.mark.django_db
def test_admin_keeps_defeats_view_only_and_bosses_undeletable(user):
    request = superuser_request()
    boss, quests = make_boss(user)
    complete_all(user, quests)
    defeat_admin = BossDefeatAdmin(BossDefeat, admin.site)
    boss_admin = BossAdmin(Boss, admin.site)

    assert not defeat_admin.has_add_permission(request)
    assert not defeat_admin.has_change_permission(request, boss.defeat)
    assert not defeat_admin.has_delete_permission(request, boss.defeat)
    assert not boss_admin.has_add_permission(request)
    assert not boss_admin.has_delete_permission(request, boss)


@pytest.mark.django_db
def test_admin_save_does_not_restore_a_boss_archived_meanwhile(api_client, user):
    boss, _ = make_boss(user, steps=0)
    request = superuser_request()
    model_admin = BossAdmin(Boss, admin.site)
    form = model_admin.get_form(request, boss)(
        data={'title': 'Renamed', 'description': ''},
        instance=boss,
    )
    assert form.is_valid(), form.errors
    # The owner archives the boss while the admin's POST is handled.
    assert api_client.delete(f'/api/bosses/{boss.id}/').status_code == 204

    model_admin.save_model(request, form.save(commit=False), form, change=True)

    boss.refresh_from_db()
    assert boss.title == 'Renamed'
    assert boss.archived_at is not None
