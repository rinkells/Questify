import pytest
from datetime import date, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from core.models import Stat, StatType

from .models import Quest, QuestCategory, QuestCompletion, QuestStatReward, Streak
from .serializers import QuestSerializer
from .services import QuestAlreadyCompletedError, QuestService, StreakService


User = get_user_model()


@pytest.fixture
def api_client():
	return APIClient()


@pytest.fixture
def user(db):
	return User.objects.create_user(username='api-user', password='password')


@pytest.fixture
def other_user(db):
	return User.objects.create_user(username='other-api-user', password='password')


@pytest.fixture
def quest(user):
	return Quest.objects.create(
		user=user,
		title='API quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=35,
	)


@pytest.mark.django_db
def test_create_quest(api_client, user):
	api_client.force_authenticate(user=user)

	response = api_client.post(
		'/api/quests/',
		{
			'title': 'New quest',
			'quest_type': 'daily',
			'xp_reward': 20,
		},
		format='json',
	)

	assert response.status_code == 201
	assert response.data['title'] == 'New quest'
	assert Quest.objects.filter(user=user, title='New quest').exists()


@pytest.mark.django_db
def test_complete_quest_returns_xp(api_client, user, quest):
	api_client.force_authenticate(user=user)

	response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert response.status_code == 200
	assert response.data['xp_gained'] == 35
	assert response.data['leveled_up'] is False
	assert response.data['streak_length'] == 1
	user.character.refresh_from_db()
	assert user.character.xp == 35


@pytest.mark.django_db
def test_one_time_quest_rejects_repeated_completion(
	api_client,
	user,
	quest,
):
	stat_type = StatType.objects.create(
		name='STR',
		display_name='Strength',
		icon='sword',
	)
	QuestStatReward.objects.create(
		quest=quest,
		stat_type=stat_type,
		amount=3,
	)
	api_client.force_authenticate(user=user)

	first_response = api_client.post(f'/api/quests/{quest.id}/complete/')
	second_response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert first_response.status_code == 200
	assert second_response.status_code == 400
	assert 'already been completed' in second_response.data['detail']
	assert Quest.objects.get(pk=quest.id).completions.count() == 1
	user.character.refresh_from_db()
	assert user.character.xp == 35
	assert Stat.objects.get(
		character=user.character,
		stat_type=stat_type,
	).value == 3
	assert Streak.objects.get(user=user, quest_category=None).current_length == 1


@pytest.mark.django_db
def test_daily_quest_cannot_be_completed_repeatedly(api_client, user):
	quest = Quest.objects.create(
		user=user,
		title='Daily API quest',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=10,
	)
	api_client.force_authenticate(user=user)

	first_response = api_client.post(f'/api/quests/{quest.id}/complete/')
	second_response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert first_response.status_code == 200
	assert second_response.status_code == 400
	assert quest.completions.count() == 1


@pytest.mark.django_db
def test_cannot_complete_foreign_quest(api_client, other_user, quest):
	api_client.force_authenticate(user=other_user)

	response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert response.status_code == 403


@pytest.mark.django_db
def test_create_quest_with_own_category(api_client, user):
	category = QuestCategory.objects.create(user=user, name='Own')
	api_client.force_authenticate(user=user)

	response = api_client.post(
		'/api/quests/',
		{
			'title': 'Categorized',
			'category': category.id,
			'quest_type': 'daily',
			'xp_reward': 10,
		},
		format='json',
	)

	assert response.status_code == 201
	assert response.data['category'] == category.id


@pytest.mark.django_db
def test_foreign_category_is_rejected_on_create_and_update(
	api_client,
	user,
	other_user,
	quest,
):
	foreign_category = QuestCategory.objects.create(
		user=other_user,
		name='Foreign',
	)
	api_client.force_authenticate(user=user)

	create_response = api_client.post(
		'/api/quests/',
		{
			'title': 'Invalid category',
			'category': foreign_category.id,
			'quest_type': 'daily',
			'xp_reward': 10,
		},
		format='json',
	)
	update_response = api_client.patch(
		f'/api/quests/{quest.id}/',
		{'category': foreign_category.id},
		format='json',
	)

	assert create_response.status_code == 400
	assert 'current user' in str(create_response.data).lower()
	assert update_response.status_code == 400
	assert 'current user' in str(update_response.data).lower()
	assert quest.category_id is None


@pytest.mark.django_db
def test_null_category_is_allowed_and_service_cannot_bypass_ownership(
	api_client,
	user,
	other_user,
):
	api_client.force_authenticate(user=user)
	response = api_client.post(
		'/api/quests/',
		{
			'title': 'No category',
			'category': None,
			'quest_type': 'daily',
			'xp_reward': 10,
		},
		format='json',
	)
	foreign_category = QuestCategory.objects.create(
		user=other_user,
		name='Foreign service category',
	)

	assert response.status_code == 201
	with pytest.raises(ValueError, match='current user'):
		QuestService.create_quest(
			user,
			title='Bypass attempt',
			category=foreign_category,
			quest_type=Quest.QuestType.DAILY,
			xp_reward=10,
		)
	serializer = QuestSerializer(
		data={
			'title': 'Direct serializer bypass',
			'category': foreign_category.id,
			'quest_type': 'daily',
			'xp_reward': 10,
		},
		context={'user': user},
	)
	assert not serializer.is_valid()


@pytest.mark.django_db
def test_one_time_service_guard_raises_without_side_effects(user, quest):
	QuestService.complete_quest(quest.id, user)

	with pytest.raises(QuestAlreadyCompletedError):
		QuestService.complete_quest(quest.id, user)

	assert quest.completions.count() == 1


@pytest.mark.django_db
@patch(
	'events.handlers.StreakService.record_completion',
	side_effect=RuntimeError('streak failed'),
)
def test_api_returns_error_and_rolls_back_when_handler_fails(
	record_completion,
	api_client,
	user,
	quest,
):
	api_client.force_authenticate(user=user)
	api_client.raise_request_exception = False

	response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert response.status_code == 500
	assert QuestCompletion.objects.filter(quest=quest).count() == 0
	user.character.refresh_from_db()
	assert user.character.xp == 0
	record_completion.assert_called_once()


@pytest.mark.django_db
def test_streak_continues_on_next_day(user):
	start = date(2026, 9, 28)

	StreakService.record_completion(user, start)
	streak = StreakService.record_completion(user, start + timedelta(days=1))

	assert streak.current_length == 2
	assert streak.longest_length == 2
	assert streak.last_completed_date == date(2026, 9, 29)


@pytest.mark.django_db
def test_streak_uses_freeze_for_one_missed_day(user):
	start = date(2026, 9, 28)
	StreakService.record_completion(user, start)
	streak = Streak.objects.get(user=user, quest_category=None)
	streak.freezes_available = 1
	streak.save(update_fields=('freezes_available',))

	streak = StreakService.record_completion(user, start + timedelta(days=2))

	assert streak.current_length == 2
	assert streak.longest_length == 2
	assert streak.freezes_available == 0


@pytest.mark.django_db
def test_streak_resets_after_missed_day_without_freeze(user):
	start = date(2026, 9, 28)
	StreakService.record_completion(user, start)
	streak = StreakService.record_completion(user, start + timedelta(days=2))

	assert streak.current_length == 1
	assert streak.longest_length == 1


@pytest.mark.django_db
def test_same_date_is_idempotent_and_dates_are_timezone_safe(user):
	completion_date = date(2026, 9, 28)
	first = StreakService.record_completion(user, completion_date)
	second = StreakService.record_completion(user, completion_date)

	assert first.pk == second.pk
	assert second.current_length == 1
	assert second.last_completed_date == completion_date


@pytest.mark.django_db
def test_long_gap_resets_streak(user):
	start = date(2026, 9, 28)
	StreakService.record_completion(user, start)
	streak = StreakService.record_completion(user, start + timedelta(days=4))

	assert streak.current_length == 1
	assert streak.longest_length == 1
