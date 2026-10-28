import importlib
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import DataError, IntegrityError, connection, transaction
from django.db.models import F, Sum
from django.test import RequestFactory, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.admin import CharacterAdmin
from core.models import Character
from events.dispatcher import dispatcher
from quests.models import Quest, QuestCompletion

from .admin import RewardAdmin
from .models import MAX_REWARD_COST, Reward, RewardRedemption, WalletTransaction
from .services import WalletService


User = get_user_model()
initial_balances = importlib.import_module('wallet.migrations.0002_initial_balances')


@pytest.fixture
def user(db):
    return User.objects.create_user(username='wallet-user', password='password')


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username='other-wallet-user', password='password')


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_quest(user, title='Wallet quest', xp_reward=50):
    return Quest.objects.create(
        user=user,
        title=title,
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=xp_reward,
    )


def make_reward(user, title='Film night', cost=30, **fields):
    return Reward.objects.create(user=user, title=title, cost=cost, **fields)


def give_coins(user, amount):
    return WalletService.credit_quest_reward(user.pk, amount)


def coins(user):
    return Character.objects.get(user=user).coins


def assert_ledger_matches(user):
    transactions = WalletTransaction.objects.filter(user=user)
    balance = coins(user)
    assert (transactions.aggregate(total=Sum('amount'))['total'] or 0) == balance
    last = transactions.order_by('-created_at', '-pk').first()
    assert (last.balance_after if last else 0) == balance


# --- Earning -----------------------------------------------------------------


@pytest.mark.django_db
def test_completion_credits_one_coin_per_xp_awarded(api_client, user):
    quest = make_quest(user)

    response = api_client.post(f'/api/quests/{quest.id}/complete/', format='json')

    assert response.status_code == 200
    assert response.data['xp_gained'] == 50
    assert response.data['coins_gained'] == 50
    completion = QuestCompletion.objects.get(quest=quest)
    transaction_row = WalletTransaction.objects.get(user=user)
    assert transaction_row.kind == WalletTransaction.Kind.QUEST_REWARD
    assert transaction_row.amount == 50
    assert transaction_row.balance_after == 50
    assert transaction_row.quest_completion == completion
    assert coins(user) == 50
    assert_ledger_matches(user)


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=60)
def test_xp_cap_limits_coins_too(api_client, user):
    first, second, third = (make_quest(user, f'Quest {n}') for n in range(3))

    gained = [
        api_client.post(f'/api/quests/{quest.id}/complete/', format='json')
        for quest in (first, second, third)
    ]

    assert [response.status_code for response in gained] == [200, 200, 400]
    assert [response.data.get('coins_gained') for response in gained[:2]] == [50, 10]
    assert coins(user) == Character.objects.get(user=user).xp == 60
    assert WalletTransaction.objects.filter(user=user).count() == 2
    assert_ledger_matches(user)


@pytest.mark.django_db
def test_failed_quest_credits_nothing(api_client, user):
    quest = make_quest(user)

    response = api_client.post(f'/api/quests/{quest.id}/fail/', format='json')

    assert response.status_code == 201
    assert coins(user) == 0
    assert not WalletTransaction.objects.exists()


@pytest.mark.django_db
def test_failing_wallet_credit_rolls_back_the_completion(user):
    from quests.services import QuestService

    quest = make_quest(user)
    # The credit overflows bigint after XPRewardHandler has already run.
    Character.objects.filter(user=user).update(coins=2**63 - 10)

    with pytest.raises(DataError):
        QuestService.complete_quest(quest.id, user)

    assert not QuestCompletion.objects.filter(quest=quest).exists()
    assert Character.objects.get(user=user).xp == 0


@pytest.mark.django_db
def test_wallet_handler_runs_before_the_achievement_check():
    names = [
        type(getattr(handler, '__self__', handler)).__name__
        for _, handler in dispatcher._handlers
    ]

    assert names.index('XPRewardHandler') < names.index('QuestRewardCoinsHandler')
    assert names.index('QuestRewardCoinsHandler') < names.index('AchievementCheckHandler')


@pytest.mark.django_db
def test_character_shows_coins_read_only(api_client, user):
    give_coins(user, 40)

    response = api_client.patch('/api/character/', {'coins': 999}, format='json')

    assert response.status_code == 200
    assert response.data['coins'] == 40
    assert coins(user) == 40


def superuser_request():
    request = RequestFactory().post('/admin/')
    request.user = User.objects.create_superuser(username='root', password='password')
    return request


def admin_form(admin_class, obj, data):
    """A superuser's bound change form for ``obj``, with its ModelAdmin."""
    request = superuser_request()
    model_admin = admin_class(type(obj), admin.site)
    form = model_admin.get_form(request, obj)(data=data, instance=obj)
    assert form.is_valid(), form.errors
    return model_admin, request, form


def admin_save(model_admin, request, form):
    model_admin.save_model(request, form.save(commit=False), form, change=True)


@pytest.mark.django_db
def test_character_admin_is_view_only(user):
    request = superuser_request()
    model_admin = CharacterAdmin(Character, admin.site)
    character = Character.objects.get(user=user)

    assert model_admin.has_view_permission(request, character)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request, character)
    assert not model_admin.has_delete_permission(request, character)


@pytest.mark.django_db
def test_admin_save_does_not_restore_a_reward_archived_meanwhile(api_client, user):
    reward = make_reward(user, 'Film night')
    model_admin, request, form = admin_form(
        RewardAdmin,
        reward,
        {'user': user.pk, 'title': 'Cinema', 'cost': 30, 'is_active': 'on'},
    )
    # The owner deletes (archives) the reward while the admin's POST is handled.
    assert api_client.delete(f'/api/rewards/{reward.id}/').status_code == 204

    admin_save(model_admin, request, form)

    reward.refresh_from_db()
    assert (reward.title, reward.is_active) == ('Cinema', False)
    assert reward.archived_at is not None


@pytest.mark.django_db
def test_admin_cannot_reactivate_an_archived_reward(user):
    reward = make_reward(user, is_active=False, archived_at=timezone.now())
    model_admin, request, form = admin_form(
        RewardAdmin,
        reward,
        {'user': user.pk, 'title': reward.title, 'cost': 30, 'is_active': 'on'},
    )

    admin_save(model_admin, request, form)

    reward.refresh_from_db()
    assert reward.is_active is False


# --- Database guarantees -----------------------------------------------------


@pytest.mark.django_db
def test_a_completion_pays_out_once_in_the_database(user):
    completion = QuestCompletion.objects.create(
        user=user,
        status=QuestCompletion.CompletionStatus.COMPLETED,
        xp_awarded=25,
    )
    WalletService.credit_quest_reward(user.pk, 25, completion.id)

    with pytest.raises(IntegrityError), transaction.atomic():
        WalletService.credit_quest_reward(user.pk, 25, completion.id)


@pytest.mark.django_db
def test_database_refuses_a_negative_balance(user):
    with pytest.raises(IntegrityError), transaction.atomic():
        Character.objects.filter(user=user).update(coins=F('coins') - 1)


@pytest.mark.django_db
@pytest.mark.parametrize(('kind', 'amount'), [
    (WalletTransaction.Kind.REDEMPTION, 5),
    (WalletTransaction.Kind.QUEST_REWARD, -5),
    (WalletTransaction.Kind.INITIAL_GRANT, 0),
])
def test_transaction_sign_must_match_its_kind(user, kind, amount):
    with pytest.raises(IntegrityError), transaction.atomic():
        WalletTransaction.objects.create(
            user=user,
            kind=kind,
            amount=amount,
            balance_after=10,
        )


# --- Rewards -----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize('cost', [1, MAX_REWARD_COST])
def test_reward_cost_bounds_are_accepted(api_client, cost):
    response = api_client.post(
        '/api/rewards/',
        {'title': 'Treat', 'cost': cost},
        format='json',
    )

    assert response.status_code == 201
    assert response.data['cost'] == cost


@pytest.mark.django_db
@pytest.mark.parametrize('cost', [0, -1, MAX_REWARD_COST + 1, 1.5, True, None, 'ten'])
def test_invalid_reward_cost_is_a_400(api_client, cost):
    response = api_client.post(
        '/api/rewards/',
        {'title': 'Treat', 'cost': cost},
        format='json',
    )

    assert response.status_code == 400
    assert 'cost' in response.data
    assert not Reward.objects.exists()


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_REWARDS_PER_USER=2)
def test_reward_limit_counts_archived_rewards(api_client, user):
    first = make_reward(user, 'First')
    make_reward(user, 'Second')
    assert api_client.delete(f'/api/rewards/{first.id}/').status_code == 204

    response = api_client.post(
        '/api/rewards/',
        {'title': 'Third', 'cost': 10},
        format='json',
    )

    assert response.status_code == 400
    assert response.data == {'detail': 'Reward limit reached for this account.'}


@pytest.mark.django_db
def test_delete_archives_the_reward_and_hides_it(api_client, user):
    reward = make_reward(user)

    response = api_client.delete(f'/api/rewards/{reward.id}/')

    assert response.status_code == 204
    reward.refresh_from_db()
    assert reward.archived_at is not None
    assert reward.is_active is False
    assert api_client.get('/api/rewards/').data['results'] == []
    assert api_client.get(f'/api/rewards/{reward.id}/').status_code == 404
    assert api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json').status_code == 404


@pytest.mark.django_db
def test_rewards_of_other_users_are_invisible(api_client, user, other_user):
    reward = make_reward(other_user)
    give_coins(user, 100)

    assert api_client.get('/api/rewards/').data['results'] == []
    assert api_client.get(f'/api/rewards/{reward.id}/').status_code == 404
    assert api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json').status_code == 404
    assert coins(user) == 100


@pytest.mark.django_db
@pytest.mark.parametrize('reward_id', ['abc', '1.5', '99999999999999999999'])
def test_malformed_reward_id_is_a_404_not_a_500(api_client, reward_id):
    response = api_client.post(f'/api/rewards/{reward_id}/redeem/', format='json')

    assert response.status_code == 404


# --- Redeeming ---------------------------------------------------------------


@pytest.mark.django_db
def test_redeem_spends_coins_and_keeps_a_snapshot(api_client, user):
    give_coins(user, 100)
    reward = make_reward(user, 'Film night', cost=30)

    response = api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json')

    assert response.status_code == 201
    assert response.data['reward'] == reward.id
    assert response.data['title'] == 'Film night'
    assert response.data['cost'] == 30
    assert response.data['balance'] == 70
    assert coins(user) == 70

    api_client.patch(
        f'/api/rewards/{reward.id}/',
        {'title': 'Cinema', 'cost': 80},
        format='json',
    )
    redemption = RewardRedemption.objects.get(pk=response.data['id'])
    assert (redemption.title_at_redemption, redemption.cost_at_redemption) == (
        'Film night',
        30,
    )
    spent = WalletTransaction.objects.get(redemption=redemption)
    assert (spent.kind, spent.amount, spent.balance_after) == ('redemption', -30, 70)
    assert_ledger_matches(user)


@pytest.mark.django_db
def test_redeem_without_enough_coins_is_refused(api_client, user):
    give_coins(user, 20)
    reward = make_reward(user, cost=30)

    response = api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json')

    assert response.status_code == 400
    assert response.data == {'detail': 'Not enough coins: 20/30.'}
    assert coins(user) == 20
    assert not RewardRedemption.objects.exists()
    assert_ledger_matches(user)


@pytest.mark.django_db
def test_inactive_reward_cannot_be_redeemed(api_client, user):
    give_coins(user, 100)
    reward = make_reward(user, is_active=False)

    response = api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json')

    assert response.status_code == 400
    assert response.data == {
        'detail': 'This reward is inactive and cannot be redeemed.',
    }
    assert coins(user) == 100


@pytest.mark.django_db
def test_rewards_can_be_redeemed_repeatedly(api_client, user):
    give_coins(user, 100)
    reward = make_reward(user, cost=30)

    statuses = [
        api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json').status_code
        for _ in range(4)
    ]

    assert statuses == [201, 201, 201, 400]
    assert coins(user) == 10
    assert_ledger_matches(user)


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_REDEMPTIONS_PER_USER_PER_24H=2)
def test_redemption_limit_is_per_rolling_24_hours(api_client, user):
    give_coins(user, 100)
    reward = make_reward(user, cost=1)
    url = f'/api/rewards/{reward.id}/redeem/'
    for _ in range(2):
        assert api_client.post(url, format='json').status_code == 201

    refused = api_client.post(url, format='json')
    # Where it is already the next local day, nothing reopens.
    moved = api_client.patch(
        '/api/character/',
        {'timezone': 'Pacific/Kiritimati'},
        format='json',
    )
    after_timezone_change = api_client.post(url, format='json')
    RewardRedemption.objects.update(redeemed_at=F('redeemed_at') - timedelta(hours=23))
    within_window = api_client.post(url, format='json')
    RewardRedemption.objects.update(
        redeemed_at=F('redeemed_at') - timedelta(hours=1, seconds=1),
    )
    after_window = api_client.post(url, format='json')

    assert refused.status_code == 400
    assert refused.data == {
        'detail': 'The redemption limit for the last 24 hours has been reached.',
    }
    assert moved.status_code == 200
    assert after_timezone_change.status_code == 400
    assert within_window.status_code == 400
    assert after_window.status_code == 201
    assert coins(user) == 97


@pytest.mark.django_db(transaction=True)
def test_parallel_redemptions_cannot_overspend(user):
    give_coins(user, 50)
    # Different rewards, so their row locks do not serialize the requests:
    # only the character lock stands between them and a negative balance.
    rewards = [make_reward(user, f'Treat {n}', cost=30) for n in range(6)]
    barrier = threading.Barrier(len(rewards))
    statuses = []

    def redeem(reward):
        client = APIClient()
        client.force_authenticate(user=user)
        try:
            barrier.wait()
            statuses.append(
                client.post(f'/api/rewards/{reward.id}/redeem/', format='json').status_code,
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=redeem, args=(reward,)) for reward in rewards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [201, 400, 400, 400, 400, 400]
    assert coins(user) == 20
    assert RewardRedemption.objects.filter(user=user).count() == 1
    assert_ledger_matches(user)


# --- History -----------------------------------------------------------------


@pytest.mark.django_db
def test_transaction_history_is_private_and_newest_first(api_client, user, other_user):
    give_coins(user, 50)
    give_coins(other_user, 70)
    reward = make_reward(user, 'Film night', cost=30)
    api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json')

    response = api_client.get('/api/wallet/transactions/')

    assert response.status_code == 200
    assert [
        (row['kind'], row['amount'], row['balance_after'], row['reward_title'])
        for row in response.data['results']
    ] == [
        ('redemption', -30, 20, 'Film night'),
        ('quest_reward', 50, 50, None),
    ]


# --- Initial balances (wallet migration 0002) --------------------------------


def run_migration(function):
    function(apps, SimpleNamespace(connection=connection))


@pytest.mark.django_db
def test_initial_grant_turns_earned_xp_into_coins_once(user, other_user):
    Character.objects.filter(user=user).update(xp=120)

    run_migration(initial_balances.grant_initial_balances)
    run_migration(initial_balances.grant_initial_balances)

    grant = WalletTransaction.objects.get(user=user)
    assert (grant.kind, grant.amount, grant.balance_after) == ('initial_grant', 120, 120)
    assert coins(user) == 120
    assert coins(other_user) == 0
    assert not WalletTransaction.objects.filter(user=other_user).exists()
    assert_ledger_matches(user)


@pytest.mark.django_db
def test_initial_grant_leaves_out_xp_already_paid_as_coins(api_client, user):
    # The wallet ran between migrations 0001 and 0002.
    Character.objects.filter(user=user).update(xp=120)
    quest = make_quest(user)
    assert api_client.post(
        f'/api/quests/{quest.id}/complete/',
        format='json',
    ).status_code == 200

    run_migration(initial_balances.grant_initial_balances)

    # 120 XP from before the wallet, 50 already paid by the quest reward.
    assert coins(user) == Character.objects.get(user=user).xp == 170
    assert WalletTransaction.objects.get(user=user, kind='initial_grant').amount == 120
    assert_ledger_matches(user)


@pytest.mark.django_db
def test_reverting_the_initial_grant(user, other_user):
    Character.objects.filter(user=user).update(xp=120)
    Character.objects.filter(user=other_user).update(xp=30)
    run_migration(initial_balances.grant_initial_balances)

    run_migration(initial_balances.revoke_initial_balances)

    assert (coins(user), coins(other_user)) == (0, 0)
    assert not WalletTransaction.objects.exists()


def complete_a_quest(api_client, user):
    quest = make_quest(user)
    api_client.post(f'/api/quests/{quest.id}/complete/', format='json')


def redeem_a_reward(api_client, user):
    reward = make_reward(user, cost=100)
    api_client.post(f'/api/rewards/{reward.id}/redeem/', format='json')


@pytest.mark.django_db
@pytest.mark.parametrize('use_wallet', [complete_a_quest, redeem_a_reward])
def test_a_used_wallet_cannot_be_reverted(api_client, user, use_wallet):
    Character.objects.filter(user=user).update(xp=120)
    run_migration(initial_balances.grant_initial_balances)
    use_wallet(api_client, user)
    balance = coins(user)

    with pytest.raises(RuntimeError, match='after it has been used'):
        run_migration(initial_balances.revoke_initial_balances)

    assert coins(user) == balance
    assert WalletTransaction.objects.filter(kind='initial_grant').exists()
