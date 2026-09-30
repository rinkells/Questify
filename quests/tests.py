import pytest
from datetime import date, timedelta
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from .models import Quest, Streak
from .services import StreakService


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
	user.character.refresh_from_db()
	assert user.character.xp == 35


@pytest.mark.django_db
def test_cannot_complete_foreign_quest(api_client, other_user, quest):
	api_client.force_authenticate(user=other_user)

	response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert response.status_code == 403


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
