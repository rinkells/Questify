"""Unit tests of the reward services and selectors (the web UI calls them directly)."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from core.models import Character

from .models import RewardRedemption, WalletTransaction
from .selectors import (
    TRANSACTION_PAGE_SIZE,
    rewards_for_user,
    transaction_page,
    transactions_for_user,
)
from .services import RedemptionError, RewardLimitError, RewardService, WalletService

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(username='wallet-service-user', password='x')


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username='wallet-service-other', password='x')


def give_coins(user, coins):
    Character.objects.filter(user=user).update(coins=coins)


def codes(callable_, *args, **kwargs):
    with pytest.raises(Exception) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


def test_create_reward_belongs_to_the_user(user):
    reward = RewardService.create_reward(user, title='Film night', cost=40)

    assert (reward.user, reward.title, reward.cost) == (user, 'Film night', 40)
    assert reward.is_active


def test_create_reward_refuses_fields_it_does_not_know(user):
    with pytest.raises(TypeError):
        RewardService.create_reward(user, title='x', cost=1, archived_at=timezone.now())


@override_settings(QUESTIFY_MAX_REWARDS_PER_USER=2)
def test_create_reward_stops_at_the_quota_and_archived_rewards_count(user, other_user):
    RewardService.create_reward(other_user, title='theirs', cost=1)
    first = RewardService.create_reward(user, title='1', cost=1)
    RewardService.create_reward(user, title='2', cost=1)
    RewardService.archive_reward(user, first.pk)

    with pytest.raises(RewardLimitError) as refusal:
        RewardService.create_reward(user, title='3', cost=1)

    assert refusal.value.code == 'reward_limit_reached'
    assert refusal.value.field is None
    assert str(refusal.value) == 'Reward limit reached for this account.'


def test_update_reward_changes_it_and_reprices_future_redemptions(user):
    reward = RewardService.create_reward(user, title='Old', cost=10)
    give_coins(user, 100)
    WalletService.redeem_reward(user, reward.pk)

    RewardService.update_reward(user, reward.pk, title='New', cost=30)

    reward.refresh_from_db()
    assert (reward.title, reward.cost) == ('New', 30)
    # The redemption keeps what it paid for.
    redemption = RewardRedemption.objects.get()
    assert (redemption.title_at_redemption, redemption.cost_at_redemption) == ('Old', 10)


def test_rewards_the_user_cannot_reach_are_not_found(user, other_user):
    theirs = RewardService.create_reward(other_user, title='theirs', cost=1)
    archived = RewardService.create_reward(user, title='gone', cost=1)
    RewardService.archive_reward(user, archived.pk)

    for reward_id in (theirs.pk, archived.pk, 10**6, 'abc'):
        assert codes(RewardService.update_reward, user, reward_id, title='x') == (
            'reward_not_found'
        )
        assert codes(RewardService.archive_reward, user, reward_id) == 'reward_not_found'
        assert codes(WalletService.redeem_reward, user, reward_id) == 'reward_not_found'
    with pytest.raises(LookupError):
        RewardService.archive_reward(user, theirs.pk)


def test_archive_reward_hides_it_and_deactivates_it(user):
    reward = RewardService.create_reward(user, title='Cake', cost=5)

    RewardService.archive_reward(user, reward.pk)

    reward.refresh_from_db()
    assert reward.archived_at is not None
    assert not reward.is_active
    assert list(rewards_for_user(user)) == []


def test_rewards_for_user_lists_only_their_unarchived_rewards_by_id(user, other_user):
    first = RewardService.create_reward(user, title='1', cost=1)
    gone = RewardService.create_reward(user, title='gone', cost=1)
    second = RewardService.create_reward(user, title='2', cost=1)
    RewardService.create_reward(other_user, title='theirs', cost=1)
    RewardService.archive_reward(user, gone.pk)

    assert list(rewards_for_user(user)) == [first, second]


def test_redeem_reward_spends_the_coins_and_writes_the_ledger(user):
    reward = RewardService.create_reward(user, title='Cake', cost=30)
    give_coins(user, 100)

    outcome = WalletService.redeem_reward(user, reward.pk)

    assert outcome.balance == 70
    assert Character.objects.get(user=user).coins == 70
    ledger = WalletTransaction.objects.get()
    assert (ledger.kind, ledger.amount, ledger.balance_after) == ('redemption', -30, 70)
    assert ledger.redemption == outcome.redemption


def test_not_enough_coins_has_a_code_and_the_numbers_for_a_translation(user):
    reward = RewardService.create_reward(user, title='Cake', cost=30)
    give_coins(user, 12)

    with pytest.raises(RedemptionError) as refusal:
        WalletService.redeem_reward(user, reward.pk)

    assert refusal.value.code == 'not_enough_coins'
    assert refusal.value.params == {'balance': 12, 'cost': 30}
    assert str(refusal.value) == 'Not enough coins: 12/30.'
    assert Character.objects.get(user=user).coins == 12
    assert not WalletTransaction.objects.exists()


def test_an_inactive_reward_cannot_be_redeemed(user):
    reward = RewardService.create_reward(user, title='Cake', cost=1, is_active=False)
    give_coins(user, 10)

    assert codes(WalletService.redeem_reward, user, reward.pk) == 'reward_inactive'


@override_settings(QUESTIFY_MAX_REDEMPTIONS_PER_USER_PER_24H=1)
def test_the_redemption_limit_has_a_code_and_is_a_rolling_24_hours(user):
    reward = RewardService.create_reward(user, title='Cake', cost=1)
    give_coins(user, 10)
    WalletService.redeem_reward(user, reward.pk)

    assert codes(WalletService.redeem_reward, user, reward.pk) == (
        'redemption_limit_reached'
    )

    RewardRedemption.objects.update(redeemed_at=timezone.now() - timedelta(hours=25))
    assert WalletService.redeem_reward(user, reward.pk).balance == 8


def test_transactions_for_user_are_newest_first_and_only_the_users(user, other_user):
    reward = RewardService.create_reward(user, title='Cake', cost=1)
    give_coins(user, 10)
    WalletService.redeem_reward(user, reward.pk)
    WalletService.credit_quest_reward(user.pk, 5)
    WalletService.credit_quest_reward(other_user.pk, 5)

    history = list(transactions_for_user(user))

    assert [t.kind for t in history] == ['quest_reward', 'redemption']
    assert history[1].redemption.title_at_redemption == 'Cake'
    assert all(t.user_id == user.pk for t in history)


def test_transactions_for_user_loads_redemptions_without_a_query_each(
    user,
    django_assert_num_queries,
):
    reward = RewardService.create_reward(user, title='Cake', cost=1)
    give_coins(user, 10)
    for _ in range(3):
        WalletService.redeem_reward(user, reward.pk)

    with django_assert_num_queries(1):
        titles = [
            t.redemption.title_at_redemption
            for t in transactions_for_user(user)
            if t.redemption
        ]

    assert titles == ['Cake'] * 3



# -- pages of the history -------------------------------------------------------


def add_rows(user, count):
    start = timezone.now() - timedelta(days=count)
    rows = [
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.QUEST_REWARD,
            amount=index + 1,
            balance_after=index + 1,
            created_at=start + timedelta(hours=index),
        )
        for index in range(count)
    ]
    return list(reversed(rows))


def test_transaction_pages_cover_the_history_once_in_its_order(user, other_user):
    add_rows(other_user, 2)
    newest_first = add_rows(user, 5)

    first = transaction_page(user, limit=2)
    second = transaction_page(user, before=first.next_before, limit=2)
    third = transaction_page(user, before=second.next_before, limit=2)

    assert first.items == newest_first[:2]
    assert second.items == newest_first[2:4]
    assert third.items == newest_first[4:]
    assert third.next_before is None


def test_a_transaction_page_of_rows_with_one_timestamp_loses_none(user):
    when = timezone.now()
    rows = [
        WalletTransaction.objects.create(
            user=user,
            kind=WalletTransaction.Kind.QUEST_REWARD,
            amount=1,
            balance_after=index + 1,
            created_at=when,
        )
        for index in range(3)
    ]

    first = transaction_page(user, limit=2)
    second = transaction_page(user, before=first.next_before, limit=2)

    assert [row.pk for row in first.items + second.items] == sorted(
        (row.pk for row in rows),
        reverse=True,
    )


def test_a_transaction_page_ignores_a_cursor_that_is_not_the_users(user, other_user):
    add_rows(user, 1)
    theirs = add_rows(other_user, 1)[0]

    assert len(transaction_page(user, before=theirs.pk).items) == 1
    assert len(transaction_page(user, before=987654).items) == 1


def test_a_transaction_page_takes_one_query_and_two_with_a_cursor(
    user,
    django_assert_num_queries,
):
    rows = add_rows(user, TRANSACTION_PAGE_SIZE + 1)
    reward = RewardService.create_reward(user, title='Film night', cost=1)
    give_coins(user, 10)
    WalletService.redeem_reward(user, reward.pk)

    with django_assert_num_queries(1):
        page = transaction_page(user)
        titles = [
            row.redemption.title_at_redemption if row.redemption else None
            for row in page.items
        ]
    assert titles[0] == 'Film night'

    with django_assert_num_queries(2):
        transaction_page(user, before=rows[0].pk)
