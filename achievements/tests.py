from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from events.dispatcher import dispatcher
from events.event_types import AchievementUnlockedEvent
from quests.models import Quest, QuestCategory, QuestCompletion, Streak
from quests.services import QuestService

from .conditions import (
    LevelReachedEvaluator,
    QuestCountByCategoryEvaluator,
    QuestCountEvaluator,
    StreakLengthEvaluator,
    TotalXPEvaluator,
)
from .handlers import AchievementCheckHandler
from .models import Achievement, UserAchievement


User = get_user_model()


@pytest.fixture
def achievement_user(db):
    return User.objects.create_user(username='achievement-user')


@pytest.fixture
def achievement_category(achievement_user):
    return QuestCategory.objects.create(user=achievement_user, name='study')


def create_completed_quest(user, category=None, xp_reward=10):
    quest = Quest.objects.create(
        user=user,
        category=category,
        title='Completed quest',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=xp_reward,
    )
    QuestCompletion.objects.create(
        quest=quest,
        status=QuestCompletion.CompletionStatus.COMPLETED,
    )
    return quest


@pytest.mark.django_db
def test_total_xp_evaluator(achievement_user):
    achievement_user.character.xp = 100
    achievement_user.character.save(update_fields=('xp',))

    assert TotalXPEvaluator().evaluate(achievement_user, {'threshold': 100})


@pytest.mark.django_db
def test_quest_count_evaluator(achievement_user):
    create_completed_quest(achievement_user)
    create_completed_quest(achievement_user)

    assert QuestCountEvaluator().evaluate(achievement_user, {'count': 2})


@pytest.mark.django_db
def test_quest_count_by_category_evaluator(
    achievement_user,
    achievement_category,
):
    create_completed_quest(achievement_user, achievement_category)

    assert QuestCountByCategoryEvaluator().evaluate(
        achievement_user,
        {'category': 'study', 'count': 1},
    )


@pytest.mark.django_db
def test_streak_length_evaluator(achievement_user):
    Streak.objects.create(
        user=achievement_user,
        current_length=4,
        longest_length=5,
    )

    assert StreakLengthEvaluator().evaluate(
        achievement_user,
        {'threshold': 5},
    )


@pytest.mark.django_db
def test_level_reached_evaluator(achievement_user):
    achievement_user.character.level = 3
    achievement_user.character.save(update_fields=('level',))

    assert LevelReachedEvaluator().evaluate(
        achievement_user,
        {'threshold': 3},
    )


@pytest.mark.django_db
def test_quest_completion_unlocks_achievement_and_publishes_event(
    achievement_user,
):
    achievement = Achievement.objects.create(
        name='First XP',
        description='Earn XP',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'threshold': 25},
    )
    quest = Quest.objects.create(
        user=achievement_user,
        title='Earn XP',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=25,
    )

    event_spy = Mock()
    dispatcher.register(AchievementUnlockedEvent, event_spy)
    result = QuestService.complete_quest(quest.id, achievement_user)

    assert result['xp_gained'] == 25
    assert result['achievements_unlocked'] == [achievement.id]
    assert UserAchievement.objects.filter(
        user=achievement_user,
        achievement=achievement,
    ).exists()
    event_spy.assert_called_once_with(
        AchievementUnlockedEvent(achievement_user.id, achievement.id),
    )


@pytest.mark.django_db

def test_hidden_locked_achievement_is_masked(achievement_user):
    Achievement.objects.create(
        name='Secret achievement',
        description='Secret description',
        icon='secret',
        is_hidden=True,
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'threshold': 1000},
    )
    client = APIClient()
    client.force_authenticate(user=achievement_user)

    response = client.get('/api/achievements/')

    assert response.status_code == 200
    assert response.data[0]['name'] == '???'
    assert response.data[0]['description'] is None
