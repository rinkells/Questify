import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from .models import Quest


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
