from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from django.utils import timezone

from events.dispatcher import dispatcher
from events.event_types import AchievementUnlockedEvent, QuestCompletedEvent
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
from .serializers import AchievementSerializer
from .views import achievements_for_user


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
def test_category_achievement_uses_completion_time_snapshot(
	achievement_user,
	achievement_category,
):
	create_completed_quest(achievement_user, achievement_category)
	achievement_category.name = 'renamed later'
	achievement_category.save(update_fields=('name',))

	assert QuestCountByCategoryEvaluator().evaluate(
		achievement_user,
		{'category': 'study', 'count': 1},
	)
	assert not QuestCountByCategoryEvaluator().evaluate(
		achievement_user,
		{'category': 'renamed later', 'count': 1},
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
def test_level_up_achievements_are_reported_in_completion_result(
    achievement_user,
):
    level_achievement = Achievement.objects.create(
        name='Level 2',
        condition_type=Achievement.ConditionType.LEVEL_REACHED,
        condition_config={'threshold': 2},
    )
    count_achievement = Achievement.objects.create(
        name='First quest',
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 1},
    )
    achievement_user.character.xp = 90
    achievement_user.character.save(update_fields=('xp',))
    quest = Quest.objects.create(
        user=achievement_user,
        title='Level up',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=25,
    )

    result = QuestService.complete_quest(quest.id, achievement_user)

    assert result['leveled_up'] is True
    assert sorted(result['achievements_unlocked']) == sorted(
        [level_achievement.id, count_achievement.id],
    )
    assert UserAchievement.objects.filter(user=achievement_user).count() == 2


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
def test_achievement_checks_batch_database_reads_for_many_conditions(
    achievement_user,
    django_assert_num_queries,
):
    for index in range(8):
        Achievement.objects.create(
            name=f'Quest count {index}',
            condition_type=Achievement.ConditionType.QUEST_COUNT,
            condition_config={'threshold': 100},
        )
        Achievement.objects.create(
            name=f'Category count {index}',
            condition_type=Achievement.ConditionType.QUEST_COUNT_BY_CATEGORY,
            condition_config={'category': 'study', 'threshold': 100},
        )
        Achievement.objects.create(
            name=f'Streak {index}',
            condition_type=Achievement.ConditionType.STREAK_LENGTH,
            condition_config={'threshold': 100},
        )

    event = QuestCompletedEvent(
        quest_id=1,
        user_id=achievement_user.id,
        completed_at=timezone.now(),
    )
    with django_assert_num_queries(4):
        AchievementCheckHandler().handle(event)


@pytest.mark.django_db
def test_streak_achievement_unlocks_after_quest_completion(achievement_user):
    achievement = Achievement.objects.create(
        name='Daily starter',
        condition_type=Achievement.ConditionType.STREAK_LENGTH,
        condition_config={'threshold': 1},
    )
    quest = Quest.objects.create(
        user=achievement_user,
        title='Daily quest',
        quest_type=Quest.QuestType.DAILY,
        xp_reward=1,
    )

    result = QuestService.complete_quest(quest.id, achievement_user)

    assert result['streak_length'] == 1
    assert UserAchievement.objects.filter(
        user=achievement_user,
        achievement=achievement,
    ).exists()


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
    assert response.data['results'][0]['name'] == '???'
    assert response.data['results'][0]['description'] is None
    assert response.data['results'][0]['condition_type'] is None
    assert 'id' not in response.data['results'][0]
    assert 'icon' not in response.data['results'][0]
    assert 'is_hidden' not in response.data['results'][0]


@pytest.mark.django_db
def test_achievement_serializer_uses_prefetched_unlock_state_without_n_plus_one(
    achievement_user,
    django_assert_num_queries,
):
    locked = Achievement.objects.create(
        name='Secret achievement',
        description='Secret description',
        is_hidden=True,
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'threshold': 1000},
    )
    unlocked = Achievement.objects.create(
        name='Known achievement',
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'count': 1},
    )
    UserAchievement.objects.create(
        user=achievement_user,
        achievement=unlocked,
    )
    with django_assert_num_queries(1):
        achievements = list(achievements_for_user(achievement_user))
    with django_assert_num_queries(0):
        data = AchievementSerializer(
            achievements,
            many=True,
            context={},
        ).data

    hidden_representation = next(item for item in data if item['name'] == '???')
    unlocked_representation = next(
        item for item in data if item['name'] == 'Known achievement'
    )
    assert hidden_representation['condition_type'] is None
    assert 'id' not in hidden_representation
    assert unlocked_representation['condition_type'] == 'quest_count'


@pytest.mark.django_db
@pytest.mark.parametrize('config', [
    {'threshold': 'ten'},
    {'threshold': -1},
    {'threshold': True},
    {'threshold': None},
    {'threshold': [1]},
    [],
    'oops',
])
def test_malformed_condition_config_never_breaks_quest_completion(
    achievement_user,
    config,
):
    for condition_type in Achievement.ConditionType.values:
        Achievement.objects.create(
            name=f'bad {condition_type}',
            condition_type=condition_type,
            condition_config=config,
        )
    quest = Quest.objects.create(
        user=achievement_user,
        title='Still completes',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=25,
    )

    result = QuestService.complete_quest(quest.id, achievement_user)

    assert result['xp_gained'] == 25
    assert result['achievements_unlocked'] == []


@pytest.mark.django_db
def test_category_config_must_be_a_string_to_match(achievement_user):
    quest = Quest.objects.create(
        user=achievement_user,
        title='Category type',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=25,
    )
    Achievement.objects.create(
        name='bad category',
        condition_type=Achievement.ConditionType.QUEST_COUNT_BY_CATEGORY,
        condition_config={'category': ['x'], 'threshold': 0},
    )

    assert QuestService.complete_quest(quest.id, achievement_user)['xp_gained'] == 25


@pytest.mark.django_db
def test_achievement_clean_rejects_malformed_config():
    from django.core.exceptions import ValidationError

    bad = Achievement(
        name='bad',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'threshold': 'ten'},
    )
    good = Achievement(
        name='good',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'threshold': 10},
    )

    with pytest.raises(ValidationError):
        bad.full_clean()
    good.full_clean()
