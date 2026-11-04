"""Tests of the achievement selectors (the web UI lists achievements through them)."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Achievement, UserAchievement
from .selectors import (
    MASKED_NAME,
    achievement_cards,
    achievement_names,
    is_masked,
    next_achievement,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(username='achievement-user', password='x')


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username='achievement-other', password='x')


def make_achievement(name, *, hidden=False, **fields):
    return Achievement.objects.create(
        name=name,
        description=f'{name} description',
        icon='🏆',
        is_hidden=hidden,
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 1},
        **fields,
    )


def unlock(user, achievement, when=None):
    row = UserAchievement.objects.create(user=user, achievement=achievement)
    if when is not None:
        UserAchievement.objects.filter(pk=row.pk).update(unlocked_at=when)
    return row


def test_cards_carry_the_unlock_date_of_unlocked_achievements(user, other_user):
    first = make_achievement('First')
    second = make_achievement('Second')
    when = timezone.now() - timedelta(days=3)
    unlock(user, first, when)
    unlock(other_user, second)

    cards = achievement_cards(user)

    assert [(card.name, card.unlocked) for card in cards] == [
        ('First', True),
        ('Second', False),
    ]
    assert cards[0].unlocked_at == when
    assert cards[1].unlocked_at is None
    assert cards[0].id == first.pk
    assert cards[0].icon == '🏆'
    assert cards[0].condition_type == 'quest_count'


def test_a_hidden_locked_achievement_is_masked_down_to_nothing(user):
    make_achievement('Secret', hidden=True)

    card = achievement_cards(user)[0]

    assert card.masked
    assert card.name == MASKED_NAME == '???'
    assert (card.id, card.description, card.icon, card.condition_type) == (
        None,
        None,
        None,
        None,
    )
    assert not card.unlocked
    assert card.unlocked_at is None


def test_a_hidden_achievement_shows_itself_once_unlocked(user):
    secret = make_achievement('Secret', hidden=True)
    when = timezone.now() - timedelta(hours=1)
    unlock(user, secret, when)

    card = achievement_cards(user)[0]

    assert not card.masked
    assert (card.id, card.name, card.unlocked, card.unlocked_at) == (
        secret.pk,
        'Secret',
        True,
        when,
    )


def test_unlocked_only_lists_just_the_unlocked_ones(user):
    make_achievement('Locked')
    make_achievement('Secret locked', hidden=True)
    done = make_achievement('Done')
    unlock(user, done)

    assert [card.name for card in achievement_cards(user, unlocked_only=True)] == ['Done']


def test_cards_take_one_query_whatever_the_number_of_achievements(
    user,
    django_assert_num_queries,
):
    for index in range(6):
        achievement = make_achievement(f'A{index}', hidden=index % 2 == 0)
        if index % 3 == 0:
            unlock(user, achievement)

    with django_assert_num_queries(1):
        assert len(achievement_cards(user)) == 6


def test_is_masked_treats_an_unannotated_achievement_as_locked(db):
    assert is_masked(make_achievement('Secret', hidden=True))
    assert not is_masked(make_achievement('Plain'))


def test_next_achievement_is_the_first_visible_one_not_earned(
    user,
    other_user,
    django_assert_num_queries,
):
    make_achievement('Secret', hidden=True)
    earned = make_achievement('Earned')
    unlock(user, earned)
    expected = make_achievement('Next')
    make_achievement('Later')
    # Earned by someone else: still next for this user.
    unlock(other_user, expected)

    with django_assert_num_queries(1):
        assert next_achievement(user) == expected


def test_next_achievement_is_none_when_only_hidden_ones_are_left(user):
    make_achievement('Secret', hidden=True)

    assert next_achievement(user) is None


def test_achievement_names_take_one_query_and_none_without_ids(
    db,
    django_assert_num_queries,
):
    second = make_achievement('Second')
    first = make_achievement('First')
    make_achievement('Not asked for')

    with django_assert_num_queries(1):
        assert achievement_names([first.pk, second.pk]) == ['Second', 'First']
    with django_assert_num_queries(0):
        assert achievement_names([]) == []
