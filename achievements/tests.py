import importlib
from unittest.mock import Mock, patch

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from django.db import IntegrityError, connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from events.dispatcher import dispatcher
from events.event_types import (
    AchievementUnlockedEvent,
    QuestCompletedEvent,
    SkillUnlockedEvent,
)
from quests import selectors
from quests.models import Quest, QuestCategory, QuestCompletion, Streak
from quests.services import QuestService

from .checks import check_achievement_configs
from .conditions import (
    AchievementEvaluationContext,
    LevelReachedEvaluator,
    QuestCountByCategoryEvaluator,
    QuestCountEvaluator,
    SkillsUnlockedCountEvaluator,
    StreakLengthEvaluator,
    TotalXPEvaluator,
    _warn_once,
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
    {},
    {'treshold': 3},
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


@pytest.mark.django_db
@pytest.mark.parametrize('condition_type', Achievement.ConditionType.values)
def test_achievement_clean_requires_a_threshold(condition_type):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        Achievement(
            name='no threshold',
            condition_type=condition_type,
            condition_config={'category': 'study'},
        ).full_clean()


@pytest.mark.django_db
def test_category_achievement_matches_case_insensitively(
    achievement_user,
    achievement_category,
):
    create_completed_quest(achievement_user, achievement_category)
    config = {'category': 'Study', 'count': 1}
    context = AchievementEvaluationContext(
        completed_quests_by_category={'study': 1},
    )

    evaluator = QuestCountByCategoryEvaluator()
    assert evaluator.evaluate(achievement_user, config)
    assert evaluator.evaluate_with_context(achievement_user, config, context)


@pytest.mark.django_db
def test_skills_condition_counts_zero_without_the_skills_app(achievement_user):
    evaluator = SkillsUnlockedCountEvaluator()
    with patch('achievements.conditions.apps.is_installed', return_value=False):
        assert evaluator.evaluate(achievement_user, {'threshold': 0})
        assert not evaluator.evaluate(achievement_user, {'threshold': 1})


@pytest.mark.django_db
def test_uncategorized_achievement_ignores_legacy_unknown_categories(
    achievement_user,
):
    create_completed_quest(achievement_user)
    legacy = create_completed_quest(achievement_user)
    QuestCompletion.objects.filter(quest=legacy).update(
        category_name_at_completion=None,
    )
    config = {'threshold': 2}
    context = AchievementEvaluationContext(
        completed_quests_by_category=selectors.completed_counts_by_category(
            achievement_user.id,
        ),
    )

    evaluator = QuestCountByCategoryEvaluator()
    assert not evaluator.evaluate(achievement_user, config)
    assert not evaluator.evaluate_with_context(achievement_user, config, context)
    assert evaluator.evaluate(achievement_user, {'threshold': 1})


@pytest.mark.django_db
def test_migration_keeps_achievements_without_threshold_earnable():
    migration = importlib.import_module(
        'achievements.migrations.0003_explicit_zero_threshold',
    )
    missing = Achievement.objects.create(
        name='missing',
        condition_type=Achievement.ConditionType.QUEST_COUNT_BY_CATEGORY,
        condition_config={'category': 'study'},
    )
    empty = Achievement.objects.create(
        name='empty',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={},
    )
    with_count = Achievement.objects.create(
        name='count',
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'count': 3},
    )
    typo = Achievement.objects.create(
        name='typo',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'treshold': 3},
    )
    misplaced = Achievement.objects.create(
        name='misplaced',
        condition_type=Achievement.ConditionType.LEVEL_REACHED,
        condition_config={'count': 5},
    )
    malformed = Achievement.objects.create(
        name='malformed',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config='oops',
    )

    migration.add_explicit_zero_threshold(django_apps, None)

    for achievement in (missing, empty, with_count, typo, misplaced, malformed):
        achievement.refresh_from_db()
    assert missing.condition_config == {'category': 'study', 'threshold': 0}
    assert empty.condition_config == {'threshold': 0}
    # Left unattainable rather than granted to everyone.
    assert with_count.condition_config == {'count': 3}
    assert typo.condition_config == {'treshold': 3}
    assert misplaced.condition_config == {'count': 5}
    assert malformed.condition_config == 'oops'


@pytest.mark.django_db
def test_startup_check_reports_achievements_that_can_never_unlock():
    Achievement.objects.create(
        name='fine',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'threshold': 10},
    )
    typo = Achievement.objects.create(
        name='typo',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'treshold': 10},
    )
    unknown = Achievement.objects.create(
        name='unknown',
        condition_type='removed_type',
        condition_config={'threshold': 1},
    )

    warnings = check_achievement_configs(None, databases=['default'])

    assert [warning.obj for warning in warnings] == [typo, unknown]
    assert {warning.id for warning in warnings} == {'achievements.W001'}
    assert check_achievement_configs(None) == []


@pytest.mark.django_db
def test_skill_unlock_checks_only_skill_achievements(achievement_user):
    skills = Achievement.objects.create(
        name='skills',
        condition_type=Achievement.ConditionType.SKILLS_UNLOCKED_COUNT,
        condition_config={'threshold': 0},
    )
    Achievement.objects.create(
        name='quests',
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 0},
    )

    AchievementCheckHandler().handle(
        SkillUnlockedEvent(user_id=achievement_user.id, skill_id=1),
    )

    assert list(
        UserAchievement.objects.filter(user=achievement_user).values_list(
            'achievement_id',
            flat=True,
        )
    ) == [skills.id]


@pytest.mark.django_db
def test_category_achievement_ignores_surrounding_whitespace(
    achievement_user,
    achievement_category,
):
    create_completed_quest(achievement_user, achievement_category)
    config = {'category': ' Study ', 'count': 1}
    context = AchievementEvaluationContext(
        completed_quests_by_category=selectors.completed_counts_by_category(
            achievement_user.id,
        ),
    )

    evaluator = QuestCountByCategoryEvaluator()
    assert evaluator.evaluate(achievement_user, config)
    assert evaluator.evaluate_with_context(achievement_user, config, context)


@pytest.mark.django_db
def test_startup_check_only_runs_for_the_achievements_app():
    Achievement.objects.create(
        name='typo',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={'treshold': 1},
    )

    assert check_achievement_configs(
        [django_apps.get_app_config('skills')],
        databases=['default'],
    ) == []
    assert len(check_achievement_configs(
        [django_apps.get_app_config('achievements')],
        databases=['default'],
    )) == 1


@pytest.mark.django_db
def test_quest_count_alone_uses_a_plain_count(achievement_user):
    Achievement.objects.create(
        name='count',
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 100},
    )
    event = QuestCompletedEvent(
        quest_id=1,
        user_id=achievement_user.id,
        completed_at=timezone.now(),
    )

    with CaptureQueriesContext(connection) as queries:
        AchievementCheckHandler().handle(event)

    completion_sql = [
        query['sql']
        for query in queries
        if 'quests_questcompletion' in query['sql']
    ]
    assert len(completion_sql) == 1
    assert 'GROUP BY' not in completion_sql[0]
    assert 'COUNT(' in completion_sql[0]


@pytest.mark.django_db
@pytest.mark.parametrize('category', [' ', [], 0, False])
def test_blank_or_falsy_category_does_not_mean_uncategorized(
    achievement_user,
    category,
):
    from django.core.exceptions import ValidationError

    create_completed_quest(achievement_user)
    config = {'category': category, 'threshold': 1}
    achievement = Achievement.objects.create(
        name='bad category',
        condition_type=Achievement.ConditionType.QUEST_COUNT_BY_CATEGORY,
        condition_config=config,
    )
    context = AchievementEvaluationContext(
        completed_quests_by_category=selectors.completed_counts_by_category(
            achievement_user.id,
        ),
    )

    evaluator = QuestCountByCategoryEvaluator()
    assert not evaluator.evaluate(achievement_user, config)
    assert not evaluator.evaluate_with_context(achievement_user, config, context)
    with pytest.raises(ValidationError):
        achievement.full_clean()
    assert [
        warning.obj
        for warning in check_achievement_configs(None, databases=['default'])
    ] == [achievement]
    # A missing, null or empty category means uncategorized quests.
    assert evaluator.evaluate(achievement_user, {'threshold': 1})
    assert evaluator.evaluate(achievement_user, {'category': None, 'threshold': 1})
    assert evaluator.evaluate(achievement_user, {'category': '', 'threshold': 1})


@pytest.mark.django_db
def test_startup_check_reads_configs_as_the_evaluators_do(achievement_user):
    from django.core.exceptions import ValidationError

    create_completed_quest(achievement_user)
    second_key_bad = Achievement.objects.create(
        name='second key bad',
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 1, 'count': 'x'},
    )
    unknown = Achievement.objects.create(
        name='unknown',
        condition_type='removed_type',
        condition_config={'threshold': 1},
    )

    warnings = check_achievement_configs(None, databases=['default'])

    # The admin refuses the bad second key, but only the first one is read.
    with pytest.raises(ValidationError):
        second_key_bad.full_clean()
    assert QuestCountEvaluator().evaluate(
        achievement_user,
        second_key_bad.condition_config,
    )
    assert [warning.obj for warning in warnings] == [unknown]
    assert 'condition_type' in warnings[0].hint
    assert 'condition_config' not in warnings[0].hint


@pytest.mark.django_db
def test_startup_check_waits_for_pending_achievement_migrations():
    Achievement.objects.create(
        name='fixed by 0003',
        condition_type=Achievement.ConditionType.TOTAL_XP,
        condition_config={},
    )
    assert len(check_achievement_configs(None, databases=['default'])) == 1

    # As when migrate runs its checks before applying 0003 and the
    # migrations after it; the test's transaction rolls the records back.
    recorder = MigrationRecorder(connection)
    for name in (
        '0003_explicit_zero_threshold',
        '0004_achievement_condition_config_no_default',
    ):
        recorder.record_unapplied('achievements', name)

    assert check_achievement_configs(None, databases=['default']) == []


@pytest.mark.django_db
def test_unusable_config_is_logged_once(achievement_user, caplog):
    # The cache is per process, so an earlier evaluation of the same config
    # (another test, or a rerun of this one) would have used up the warning.
    _warn_once.cache_clear()
    config = {'threshold': 'logged once'}
    evaluator = TotalXPEvaluator()

    with caplog.at_level('WARNING', logger='achievements.conditions'):
        for _ in range(3):
            assert not evaluator.evaluate(achievement_user, config)

    assert len(caplog.records) == 1
    assert 'logged once' in caplog.records[0].getMessage()


@pytest.mark.django_db
def test_achievement_without_condition_config_is_refused():
    # With the old default of {} this created an achievement that could
    # never unlock, reported only by a startup warning.
    with pytest.raises(IntegrityError), transaction.atomic():
        Achievement.objects.create(
            name='Welcome',
            condition_type=Achievement.ConditionType.QUEST_COUNT,
        )
