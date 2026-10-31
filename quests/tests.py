import pytest
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo
from unittest.mock import patch
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient
from django.utils import timezone

from core.models import Character, Stat, StatType

from .admin import DailyBonusAdmin, QuestAdmin, QuestCompletionAdmin
from .models import (
	DailyBonus,
	Quest,
	QuestCategory,
	QuestCompletion,
	QuestStatReward,
	Streak,
)
from .serializers import QuestSerializer
from .services import (
	QuestAlreadyCompletedError,
	QuestService,
	StreakService,
	local_day,
)


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
	assert response.data['xp_reward'] == 25
	assert Quest.objects.filter(user=user, title='New quest').exists()


@pytest.mark.django_db
def test_quest_type_change_recomputes_reward_and_freezes_after_completion(
	api_client,
	user,
	quest,
):
	api_client.force_authenticate(user=user)

	change_response = api_client.patch(
		f'/api/quests/{quest.id}/',
		{'quest_type': Quest.QuestType.DAILY},
		format='json',
	)
	assert change_response.status_code == 200
	assert change_response.data['xp_reward'] == 25

	completion_response = api_client.post(f'/api/quests/{quest.id}/complete/')
	assert completion_response.status_code == 200
	blocked_change = api_client.patch(
		f'/api/quests/{quest.id}/',
		{'quest_type': Quest.QuestType.ONE_TIME},
		format='json',
	)
	assert blocked_change.status_code == 400
	quest.refresh_from_db()
	assert quest.quest_type == Quest.QuestType.DAILY
	assert quest.xp_reward == 25


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_QUESTS_PER_USER=1)
def test_quest_creation_enforces_per_user_quota(api_client, user):
	api_client.force_authenticate(user=user)
	payload = {'title': 'Limited quest', 'quest_type': 'daily'}

	first = api_client.post('/api/quests/', payload, format='json')
	second = api_client.post('/api/quests/', payload, format='json')

	assert first.status_code == 201
	assert second.status_code == 400
	assert Quest.objects.filter(user=user).count() == 1


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_QUESTS_PER_USER=1)
def test_deleting_quest_archives_it_and_does_not_restore_lifetime_quota(
	api_client,
	user,
):
	api_client.force_authenticate(user=user)
	payload = {'title': 'Lifetime-limited quest', 'quest_type': 'daily'}
	created = api_client.post('/api/quests/', payload, format='json')
	quest_id = created.data['id']

	deleted = api_client.delete(f'/api/quests/{quest_id}/')
	archived_quest = Quest.objects.get(pk=quest_id)
	new_quest = api_client.post('/api/quests/', payload, format='json')
	archived_detail = api_client.get(f'/api/quests/{quest_id}/')

	assert created.status_code == 201
	assert deleted.status_code == 204
	assert archived_quest.archived_at is not None
	assert archived_quest.is_active is False
	assert new_quest.status_code == 400
	assert archived_detail.status_code == 404
	assert Quest.objects.filter(user=user).count() == 1


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_CATEGORIES_PER_USER=1)
def test_category_creation_enforces_per_user_quota(api_client, user):
	api_client.force_authenticate(user=user)

	first = api_client.post('/api/quest-categories/', {'name': 'First'}, format='json')
	second = api_client.post('/api/quest-categories/', {'name': 'Second'}, format='json')

	assert first.status_code == 201
	assert second.status_code == 400
	assert QuestCategory.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_quest_description_has_a_bounded_length(api_client, user):
	api_client.force_authenticate(user=user)
	response = api_client.post(
		'/api/quests/',
		{
			'title': 'Long description',
			'description': 'x' * 10_001,
			'quest_type': 'daily',
		},
		format='json',
	)

	assert response.status_code == 400
	assert not Quest.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_database_rejects_quest_reward_above_hard_cap(user):
	with pytest.raises(IntegrityError):
		with transaction.atomic():
			Quest.objects.create(
				user=user,
				title='Over-cap quest',
				quest_type=Quest.QuestType.ONE_TIME,
				xp_reward=1_001,
			)


@pytest.mark.django_db
def test_database_rejects_unbounded_stat_rewards(user, quest):
	stat_type = StatType.objects.create(name='LCK', display_name='Luck')
	with pytest.raises(IntegrityError):
		with transaction.atomic():
			QuestStatReward.objects.create(
				quest=quest,
				stat_type=stat_type,
				amount=1_000_001,
			)


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
	fail_response = api_client.post(f'/api/quests/{quest.id}/fail/')
	assert fail_response.status_code == 400
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
def test_daily_fail_is_single_per_day_and_excludes_completion(api_client, user):
	quest = Quest.objects.create(
		user=user,
		title='Daily fail quest',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=10,
	)
	api_client.force_authenticate(user=user)

	first_fail = api_client.post(f'/api/quests/{quest.id}/fail/')
	second_fail = api_client.post(f'/api/quests/{quest.id}/fail/')
	completion_after_fail = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert first_fail.status_code == 201
	assert second_fail.status_code == 400
	assert completion_after_fail.status_code == 400
	assert quest.completions.count() == 1
	assert quest.completions.get().status == QuestCompletion.CompletionStatus.FAILED


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER_PER_DAY=1)
def test_daily_failure_limit_applies_across_different_quests(api_client, user):
	first_quest = Quest.objects.create(
		user=user,
		title='First failure',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)
	second_quest = Quest.objects.create(
		user=user,
		title='Second failure',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)
	api_client.force_authenticate(user=user)

	first = api_client.post(f'/api/quests/{first_quest.id}/fail/')
	second = api_client.post(f'/api/quests/{second_quest.id}/fail/')

	assert first.status_code == 201
	assert second.status_code == 400
	assert 'daily failed-quest limit' in str(second.data).lower()
	assert QuestCompletion.objects.filter(
		user=user,
		status=QuestCompletion.CompletionStatus.FAILED,
	).count() == 1


@pytest.mark.django_db
@override_settings(QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER=1)
def test_lifetime_failure_limit_applies_across_quests(api_client, user):
	first_quest = Quest.objects.create(
		user=user,
		title='First one-time failure',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	second_quest = Quest.objects.create(
		user=user,
		title='Second one-time failure',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	api_client.force_authenticate(user=user)

	first = api_client.post(f'/api/quests/{first_quest.id}/fail/')
	second = api_client.post(f'/api/quests/{second_quest.id}/fail/')

	assert first.status_code == 201
	assert second.status_code == 400
	assert 'lifetime failed-quest limit' in str(second.data).lower()
	assert QuestCompletion.objects.filter(
		user=user,
		status=QuestCompletion.CompletionStatus.FAILED,
	).count() == 1


def test_admin_cannot_delete_quests_or_mutate_completion_journal():
	quest_admin = QuestAdmin(Quest, django_admin.site)
	completion_admin = QuestCompletionAdmin(QuestCompletion, django_admin.site)

	assert quest_admin.has_delete_permission(None) is False
	assert quest_admin.has_add_permission(None) is False
	assert completion_admin.has_add_permission(None) is False
	assert completion_admin.has_change_permission(None) is False
	assert completion_admin.has_delete_permission(None) is False


def superuser_request():
	request = RequestFactory().post('/admin/')
	request.user = User.objects.create_superuser(username='root', password='password')
	return request


def quest_admin_form(quest, data):
	"""A superuser's bound change form for ``quest``, with its ModelAdmin."""
	request = superuser_request()
	model_admin = QuestAdmin(Quest, django_admin.site)
	form = model_admin.get_form(request, quest)(data=data, instance=quest)
	assert form.is_valid(), form.errors
	return model_admin, request, form


def quest_admin_save(model_admin, request, form):
	model_admin.save_model(request, form.save(commit=False), form, change=True)


@pytest.mark.django_db
def test_admin_keeps_the_rules_the_api_applies_to_a_quest(quest):
	request = superuser_request()
	form_class = QuestAdmin(Quest, django_admin.site).get_form(request, quest)

	# Not form fields, so an admin edit cannot change them.
	for name in ('user', 'quest_type', 'xp_reward', 'deadline', 'archived_at'):
		assert name not in form_class.base_fields


@pytest.mark.django_db
def test_admin_save_does_not_restore_a_quest_archived_meanwhile(
	api_client, user, quest,
):
	api_client.force_authenticate(user=user)
	model_admin, request, form = quest_admin_form(
		quest,
		{'title': 'Renamed', 'is_active': 'on'},
	)
	# The owner deletes (archives) the quest while the admin's POST is handled.
	assert api_client.delete(f'/api/quests/{quest.id}/').status_code == 204

	quest_admin_save(model_admin, request, form)

	quest.refresh_from_db()
	assert (quest.title, quest.is_active) == ('Renamed', False)
	assert quest.archived_at is not None


@pytest.mark.django_db
def test_admin_cannot_reactivate_an_archived_quest(user):
	quest = Quest.objects.create(
		user=user,
		title='Archived',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
		is_active=False,
		archived_at=timezone.now(),
	)
	model_admin, request, form = quest_admin_form(
		quest,
		{'title': 'Archived', 'is_active': 'on'},
	)

	quest_admin_save(model_admin, request, form)

	quest.refresh_from_db()
	assert quest.is_active is False
	assert quest.archived_at is not None


@pytest.mark.django_db
def test_admin_offers_only_the_owners_categories(user, other_user, quest):
	own = QuestCategory.objects.create(user=user, name='Own')
	QuestCategory.objects.create(user=other_user, name='Foreign')
	request = superuser_request()
	form_class = QuestAdmin(Quest, django_admin.site).get_form(request, quest)

	assert list(form_class.base_fields['category'].queryset) == [own]


@pytest.mark.django_db
def test_admin_does_not_write_a_category_deleted_meanwhile(user, quest):
	category = QuestCategory.objects.create(user=user, name='Study')
	model_admin, request, form = quest_admin_form(
		quest,
		{'title': quest.title, 'is_active': 'on', 'category': category.pk},
	)
	category.delete()

	with patch.object(model_admin, 'message_user') as message_user:
		quest_admin_save(model_admin, request, form)

	quest.refresh_from_db()
	assert quest.category_id is None
	message_user.assert_called_once()


@pytest.mark.django_db
def test_daily_failure_uses_one_timestamp_for_day_check_and_record(user):
	quest = Quest.objects.create(
		user=user,
		title='Midnight failure',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)
	before_midnight = datetime(
		2026, 10, 24, 23, 59, 59, tzinfo=datetime_timezone.utc,
	)
	after_midnight = datetime(
		2026, 10, 25, 0, 0, 1, tzinfo=datetime_timezone.utc,
	)

	with patch(
		'quests.services.timezone.now',
		side_effect=[before_midnight, after_midnight],
	):
		completion = QuestService.fail_quest(quest.id, user)

	assert completion.completed_at == before_midnight


@pytest.mark.django_db
def test_fail_after_daily_completion_is_rejected(api_client, user):
	quest = Quest.objects.create(
		user=user,
		title='Daily completed quest',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=10,
	)
	api_client.force_authenticate(user=user)

	completion = api_client.post(f'/api/quests/{quest.id}/complete/')
	failure = api_client.post(f'/api/quests/{quest.id}/fail/')

	assert completion.status_code == 200
	assert failure.status_code == 400
	assert quest.completions.count() == 1


@pytest.mark.django_db
def test_one_time_fail_is_single_and_blocks_later_completion(api_client, user):
	quest = Quest.objects.create(
		user=user,
		title='One-time failed quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=10,
	)
	api_client.force_authenticate(user=user)

	first_failure = api_client.post(f'/api/quests/{quest.id}/fail/')
	second_failure = api_client.post(f'/api/quests/{quest.id}/fail/')
	completion = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert first_failure.status_code == 201
	assert second_failure.status_code == 400
	assert completion.status_code == 400
	assert quest.completions.count() == 1


@pytest.mark.django_db
def test_expired_quest_can_be_failed_but_not_completed(api_client, user):
	quest = Quest.objects.create(
		user=user,
		title='Expired quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
		deadline=timezone.now() - timedelta(seconds=1),
	)
	api_client.force_authenticate(user=user)

	completed = api_client.post(f'/api/quests/{quest.id}/complete/')
	failed = api_client.post(f'/api/quests/{quest.id}/fail/')
	failed_again = api_client.post(f'/api/quests/{quest.id}/fail/')

	assert completed.status_code == 400
	assert 'deadline' in completed.data['detail'].lower()
	assert failed.status_code == 201
	assert failed.data['status'] == QuestCompletion.CompletionStatus.FAILED
	# The usual one-time rule still applies after the deadline.
	assert failed_again.status_code == 400
	assert list(quest.completions.values_list('status', 'xp_awarded')) == [
		(QuestCompletion.CompletionStatus.FAILED, 0),
	]


@pytest.mark.django_db
def test_24_hour_xp_cap_applies_across_different_quests(api_client, user):
	first_quest = Quest.objects.create(
		user=user,
		title='First high reward quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=1_000,
	)
	second_quest = Quest.objects.create(
		user=user,
		title='Second high reward quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=1_000,
	)
	api_client.force_authenticate(user=user)

	first_response = api_client.post(f'/api/quests/{first_quest.id}/complete/')
	second_response = api_client.post(f'/api/quests/{second_quest.id}/complete/')

	assert first_response.status_code == 200
	assert first_response.data['xp_gained'] == 1_000
	assert second_response.status_code == 400
	assert 'last 24 hours' in str(second_response.data).lower()
	user.character.refresh_from_db()
	assert user.character.xp == 1_000


@pytest.mark.django_db
def test_estimated_legacy_xp_counts_conservatively_toward_cap(api_client, user):
	legacy_quest = Quest.objects.create(
		user=user,
		title='Legacy quest with unknown reward history',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	new_quest = Quest.objects.create(
		user=user,
		title='New quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	QuestCompletion.objects.create(
		user=user,
		quest=legacy_quest,
		status=QuestCompletion.CompletionStatus.COMPLETED,
		xp_awarded=50,
		xp_awarded_is_estimate=True,
	)
	api_client.force_authenticate(user=user)

	response = api_client.post(f'/api/quests/{new_quest.id}/complete/')

	assert response.status_code == 400
	assert 'last 24 hours' in str(response.data).lower()


@pytest.mark.django_db
def test_deleting_completed_quest_archives_without_resetting_xp_cap(api_client, user):
	completed_quest = Quest.objects.create(
		user=user,
		title='Quest whose history must survive deletion',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=1_000,
	)
	next_quest = Quest.objects.create(
		user=user,
		title='Second quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=1_000,
	)
	api_client.force_authenticate(user=user)

	first_completion = api_client.post(
		f'/api/quests/{completed_quest.id}/complete/',
	)
	delete_response = api_client.delete(
		f'/api/quests/{completed_quest.id}/',
	)
	second_completion = api_client.post(
		f'/api/quests/{next_quest.id}/complete/',
	)

	assert first_completion.status_code == 200
	assert delete_response.status_code == 204
	assert second_completion.status_code == 400
	completed_quest.refresh_from_db()
	assert completed_quest.archived_at is not None
	assert completed_quest.is_active is False
	assert QuestCompletion.objects.filter(
		user=user,
		quest=completed_quest,
		xp_awarded=1_000,
	).exists()


@pytest.mark.django_db
def test_cannot_complete_foreign_quest(api_client, other_user, quest):
	api_client.force_authenticate(user=other_user)

	response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert response.status_code == 404


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
	assert 'does not exist' in str(create_response.data).lower()
	assert update_response.status_code == 400
	assert 'does not exist' in str(update_response.data).lower()
	assert quest.category_id is None


@pytest.mark.django_db
def test_null_category_is_allowed_and_foreign_category_is_rejected(
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
def test_quest_list_is_paginated(api_client, user):
	Quest.objects.bulk_create([
		Quest(
			user=user,
			title=f'Quest {index}',
			quest_type=Quest.QuestType.DAILY,
			xp_reward=1,
		)
		for index in range(21)
	])
	api_client.force_authenticate(user=user)

	response = api_client.get('/api/quests/')

	assert response.status_code == 200
	assert response.data['count'] == 21
	assert len(response.data['results']) == 20
	assert response.data['next'] is not None


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


@pytest.mark.django_db
def test_timezone_cannot_be_changed_soon_after_daily_completion(
	api_client,
	user,
):
	quest = Quest.objects.create(
		user=user,
		title='Timezone protected daily quest',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)
	api_client.force_authenticate(user=user)

	completion_response = api_client.post(f'/api/quests/{quest.id}/complete/')
	timezone_response = api_client.patch(
		'/api/character/',
		{'timezone': 'Pacific/Kiritimati'},
		format='json',
	)

	assert completion_response.status_code == 200
	assert timezone_response.status_code == 400
	user.character.refresh_from_db()
	assert user.character.timezone == 'UTC'


@pytest.mark.django_db
def test_timezone_can_be_set_before_first_quest_completion(api_client, user):
	api_client.force_authenticate(user=user)

	response = api_client.patch(
		'/api/character/',
		{'timezone': 'Pacific/Kiritimati'},
		format='json',
	)

	assert response.status_code == 200
	assert response.data['timezone'] == 'Pacific/Kiritimati'


@pytest.mark.django_db
def test_timezone_can_be_changed_after_24_hour_cooldown(api_client, user):
	quest = Quest.objects.create(
		user=user,
		title='Old completed quest',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	QuestCompletion.objects.create(
		user=user,
		quest=quest,
		category_name_at_completion='',
		completed_at=timezone.now() - timedelta(hours=25),
		xp_awarded=50,
		status=QuestCompletion.CompletionStatus.COMPLETED,
	)
	api_client.force_authenticate(user=user)

	response = api_client.patch(
		'/api/character/',
		{'timezone': 'Pacific/Kiritimati'},
		format='json',
	)

	assert response.status_code == 200
	assert response.data['timezone'] == 'Pacific/Kiritimati'


@pytest.mark.django_db
def test_streak_uses_users_local_date(user):
	character = user.character
	character.timezone = 'America/Los_Angeles'
	character.save(update_fields=('timezone',))

	completed_at = datetime(2026, 10, 24, 6, 30, tzinfo=datetime_timezone.utc)
	streak = StreakService.record_completion(user, completed_at)

	assert streak.last_completed_date == date(2026, 10, 23)


@pytest.mark.django_db
def test_character_api_reports_expired_streak_as_zero(api_client, user):
	api_client.force_authenticate(user=user)
	character = user.character
	character.timezone = 'Pacific/Kiritimati'
	character.save(update_fields=('timezone',))
	today = timezone.localdate(timezone=ZoneInfo(character.timezone))
	Streak.objects.create(
		user=user,
		current_length=8,
		longest_length=8,
		last_completed_date=today - timedelta(days=3),
	)

	response = api_client.get('/api/character/')

	assert response.status_code == 200
	assert response.data['current_streak'] == 0


@pytest.mark.django_db
def test_character_api_keeps_streak_during_freeze_window(api_client, user):
	api_client.force_authenticate(user=user)
	character = user.character
	character.timezone = 'Pacific/Kiritimati'
	character.save(update_fields=('timezone',))
	today = timezone.localdate(timezone=ZoneInfo(character.timezone))
	Streak.objects.create(
		user=user,
		current_length=8,
		longest_length=8,
		freezes_available=1,
		last_completed_date=today - timedelta(days=2),
	)

	response = api_client.get('/api/character/')

	assert response.status_code == 200
	assert response.data['current_streak'] == 8


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=10)
def test_xp_cap_follows_the_active_settings(user):
	quest = Quest.objects.create(
		user=user,
		title='Capped',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)

	result = QuestService.complete_quest(quest.id, user)

	assert result['xp_gained'] == 10


@pytest.mark.django_db
def test_missing_character_is_recreated_by_the_api(api_client, user):
	Character.objects.filter(user=user).delete()
	api_client.force_authenticate(user=user)

	character_response = api_client.get('/api/character/')
	Character.objects.filter(user=user).delete()
	quest_response = api_client.post(
		'/api/quests/',
		{'title': 'After delete', 'quest_type': 'daily'},
		format='json',
	)

	assert character_response.status_code == 200
	assert quest_response.status_code == 201


@pytest.mark.django_db
def test_missing_character_does_not_break_quest_completion(user):
	quest = Quest.objects.create(
		user=user,
		title='No character',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=25,
	)
	Character.objects.filter(user=user).delete()

	result = QuestService.complete_quest(quest.id, user)

	assert result['xp_gained'] == 25


@pytest.mark.django_db(transaction=True)
@override_settings(QUESTIFY_XP_24H_CAP=200)
def test_xp_cap_holds_under_concurrent_completions(user):
	import threading
	from django.db import connection

	quests = [
		Quest.objects.create(
			user=user,
			title=f'Parallel {index}',
			quest_type=Quest.QuestType.ONE_TIME,
			xp_reward=50,
		)
		for index in range(40)
	]
	barrier = threading.Barrier(len(quests))

	def complete(quest):
		client = APIClient()
		client.force_authenticate(user=user)
		try:
			barrier.wait()
			client.post(f'/api/quests/{quest.id}/complete/')
		finally:
			connection.close()

	threads = [threading.Thread(target=complete, args=(q,)) for q in quests]
	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()

	total = sum(
		QuestCompletion.objects.filter(user=user).values_list('xp_awarded', flat=True),
	)
	assert total == 200


@pytest.mark.django_db
def test_timezone_change_cannot_shrink_a_streak_gap(api_client, user):
	quest = Quest.objects.create(
		user=user,
		title='Daily',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)
	api_client.force_authenticate(user=user)
	monday = datetime(2026, 3, 2, 0, 30, tzinfo=datetime_timezone.utc)
	wednesday = monday + timedelta(days=2, minutes=5)

	with patch('django.utils.timezone.now', return_value=monday):
		assert api_client.post(f'/api/quests/{quest.id}/complete/').status_code == 200
	with patch('django.utils.timezone.now', return_value=wednesday):
		change = api_client.patch(
			'/api/character/',
			{'timezone': 'Pacific/Pago_Pago'},
			format='json',
		)
		response = api_client.post(f'/api/quests/{quest.id}/complete/')

	assert change.status_code == 200
	assert response.status_code == 200
	assert response.data['streak_length'] == 1


@pytest.mark.django_db
def test_complete_error_detail_is_always_a_string(api_client, user):
	inactive = Quest.objects.create(
		user=user,
		title='Inactive',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
		is_active=False,
	)
	api_client.force_authenticate(user=user)

	response = api_client.post(f'/api/quests/{inactive.id}/complete/')

	assert response.status_code == 400
	assert isinstance(response.data['detail'], str)


@pytest.mark.django_db
@pytest.mark.parametrize('deadline', ['0001-01-01T00:00:00Z', '9999-12-31T23:59:59Z'])
def test_deadline_outside_supported_range_is_rejected(api_client, user, deadline):
	api_client.force_authenticate(user=user)

	response = api_client.post(
		'/api/quests/',
		{'title': 'Far', 'quest_type': 'daily', 'deadline': deadline},
		format='json',
	)

	assert response.status_code == 400
	assert 'deadline' in response.data


@pytest.mark.django_db
def test_database_rejects_a_second_completion_of_a_one_time_quest(user):
	quest = Quest.objects.create(
		user=user,
		title='Once',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	QuestService.complete_quest(quest.id, user)

	with pytest.raises(IntegrityError), transaction.atomic():
		QuestCompletion.objects.create(
			user=user,
			quest=quest,
			status=QuestCompletion.CompletionStatus.COMPLETED,
			xp_awarded=50,
			is_one_time=True,
		)


@pytest.mark.django_db
def test_daily_quest_completions_are_not_marked_one_time(user):
	quest = Quest.objects.create(
		user=user,
		title='Daily',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)

	QuestService.complete_quest(quest.id, user)

	assert not QuestCompletion.objects.get(quest=quest).is_one_time


@pytest.mark.django_db
def test_quest_cannot_be_assigned_a_category_deleted_after_validation(user):
	from rest_framework.exceptions import ValidationError as DRFValidationError
	from .views import QuestViewSet

	gone = QuestCategory.objects.create(user=user, name='gone')
	stale = QuestCategory(pk=gone.pk, user=user, name='gone')
	gone.delete()

	view = QuestViewSet()
	view.request = type('Request', (), {'user': user})()
	serializer = type('Serializer', (), {
		'validated_data': {'category': stale, 'quest_type': 'daily'},
	})()

	with pytest.raises(DRFValidationError) as excinfo:
		view.perform_create(serializer)

	assert 'category' in excinfo.value.detail
	assert not Quest.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_deleting_a_category_detaches_its_quests(api_client, user):
	category = QuestCategory.objects.create(user=user, name='temp')
	quest = Quest.objects.create(
		user=user, title='q', quest_type='daily', xp_reward=25, category=category,
	)
	api_client.force_authenticate(user=user)

	response = api_client.delete(f'/api/quest-categories/{category.pk}/')

	assert response.status_code == 204
	quest.refresh_from_db()
	assert quest.category_id is None


@pytest.mark.django_db
def test_other_users_category_cannot_be_deleted(api_client, user, other_user):
	category = QuestCategory.objects.create(user=other_user, name='theirs')
	api_client.force_authenticate(user=user)

	response = api_client.delete(f'/api/quest-categories/{category.pk}/')

	assert response.status_code == 404
	assert QuestCategory.objects.filter(pk=category.pk).exists()


@pytest.mark.django_db
def test_deadline_must_lie_in_the_future(api_client, user):
	api_client.force_authenticate(user=user)

	response = api_client.post(
		'/api/quests/',
		{'title': 'late', 'quest_type': 'daily', 'deadline': '2001-01-01T00:00:00Z'},
		format='json',
	)

	assert response.status_code == 400
	assert 'deadline' in response.json()


@pytest.mark.django_db
def test_expired_quest_deadline_cannot_be_extended(api_client, user):
	expired = Quest.objects.create(
		user=user,
		title='expired',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
		deadline=timezone.now() - timedelta(days=1),
	)
	api_client.force_authenticate(user=user)

	response = api_client.patch(
		f'/api/quests/{expired.pk}/',
		{'deadline': (timezone.now() + timedelta(days=1)).isoformat()},
		format='json',
	)
	completion = api_client.post(f'/api/quests/{expired.pk}/complete/')

	assert response.status_code == 400
	assert completion.status_code == 400


@pytest.mark.django_db
def test_backfill_flags_only_the_earliest_completed_row_per_one_time_quest(user):
	import importlib
	from django.apps import apps

	migration = importlib.import_module('quests.migrations.0014_backfill_is_one_time')
	one_time = Quest.objects.create(
		user=user, title='ot', quest_type='one_time', xp_reward=50,
	)
	daily = Quest.objects.create(
		user=user, title='d', quest_type='daily', xp_reward=25,
	)
	first = QuestCompletion.objects.create(
		user=user, quest=one_time, status='completed', xp_awarded=50,
	)
	duplicate = QuestCompletion.objects.create(
		user=user, quest=one_time, status='completed', xp_awarded=50,
	)
	other = QuestCompletion.objects.create(
		user=user, quest=daily, status='completed', xp_awarded=25,
	)

	migration.flag_one_time_completions(apps, None)

	flags = {
		row.pk: row.is_one_time
		for row in QuestCompletion.objects.filter(pk__in=[first.pk, duplicate.pk, other.pk])
	}
	assert flags == {first.pk: True, duplicate.pk: False, other.pk: False}


@pytest.mark.django_db(transaction=True)
def test_category_delete_races_complete_and_assign_without_server_errors(user):
	import threading
	from django.db import connection

	statuses = []
	failures = []

	def call(barrier, method, url, payload=None):
		client = APIClient(raise_request_exception=False)
		client.force_authenticate(user=user)
		try:
			barrier.wait()
			response = getattr(client, method)(url, payload, format='json')
			statuses.append(response.status_code)
		except Exception as exc:  # pragma: no cover - reported below
			failures.append(exc)
		finally:
			connection.close()

	for index in range(8):
		category = QuestCategory.objects.create(user=user, name=f'race-{index}')
		quest = Quest.objects.create(
			user=user,
			title='racer',
			quest_type=Quest.QuestType.ONE_TIME,
			xp_reward=50,
			category=category,
		)
		barrier = threading.Barrier(3)
		jobs = [
			('delete', f'/api/quest-categories/{category.pk}/', None),
			('post', f'/api/quests/{quest.pk}/complete/', None),
			('post', '/api/quests/', {
				'title': 'new', 'quest_type': 'daily', 'category': category.pk,
			}),
		]
		threads = [
			threading.Thread(target=call, args=(barrier, *job)) for job in jobs
		]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()

	assert not failures
	assert statuses and max(statuses) < 500


@pytest.mark.django_db
@pytest.mark.parametrize('action', ['complete', 'fail'])
@pytest.mark.parametrize('quest_id', ['abc', '%00'])
def test_malformed_quest_id_is_a_404_not_a_500(api_client, user, action, quest_id):
	api_client.force_authenticate(user=user)

	response = api_client.post(f'/api/quests/{quest_id}/{action}/')

	assert response.status_code == 404


@pytest.mark.django_db
def test_fractional_category_id_is_rejected_not_truncated(api_client, user):
	category = QuestCategory.objects.create(user=user, name='Study')
	api_client.force_authenticate(user=user)

	response = api_client.post(
		'/api/quests/',
		{'title': 'q', 'quest_type': 'daily', 'category': category.pk + 0.5},
		format='json',
	)

	assert response.status_code == 400
	assert 'category' in response.data
	assert not Quest.objects.filter(user=user).exists()


@pytest.mark.django_db(transaction=True)
def test_category_update_racing_a_delete_is_a_404(user):
	import threading
	from django.db import connection

	category = QuestCategory.objects.create(user=user, name='Racing')
	outcome = []

	def rename():
		client = APIClient(raise_request_exception=False)
		client.force_authenticate(user=user)
		try:
			outcome.append(client.patch(
				f'/api/quest-categories/{category.pk}/',
				{'name': 'Renamed'},
				format='json',
			).status_code)
		finally:
			connection.close()

	# The DELETE locks the row and commits while the PATCH, past
	# get_object(), waits for that lock in perform_update().
	with transaction.atomic():
		QuestCategory.objects.select_for_update().get(pk=category.pk)
		thread = threading.Thread(target=rename, daemon=True)
		thread.start()
		thread.join(timeout=0.5)
		assert thread.is_alive(), outcome
		QuestCategory.objects.filter(pk=category.pk).delete()
	thread.join(timeout=10)

	assert outcome == [404]


@pytest.mark.django_db
def test_completion_does_not_relock_the_character_needlessly(user, quest):
	from django.db import connection
	from django.test.utils import CaptureQueriesContext

	with CaptureQueriesContext(connection) as queries:
		QuestService.complete_quest(quest.id, user)

	character_locks = [
		query['sql']
		for query in queries
		if 'FROM "core_character"' in query['sql'] and 'FOR UPDATE' in query['sql']
	]
	# complete_quest, LevelCheckHandler and StreakService.record_completion;
	# the XP handler's UPDATE locks the row without reading it first.
	assert len(character_locks) == 3
	user.character.refresh_from_db()
	assert user.character.xp == quest.xp_reward


@pytest.mark.parametrize(('action', 'success'), [('complete', '200'), ('fail', '201')])
def test_complete_and_fail_are_documented_without_a_request_body(action, success):
	from drf_spectacular.generators import SchemaGenerator

	operation = SchemaGenerator().get_schema(public=True)['paths'][
		f'/api/quests/{{id}}/{action}/'
	]['post']

	assert 'requestBody' not in operation
	assert set(operation['responses']) == {success, '400', '404'}


@pytest.mark.django_db
def test_fail_response_matches_its_schema(api_client, user, quest):
	api_client.force_authenticate(user=user)

	response = api_client.post(f'/api/quests/{quest.id}/fail/', format='json')

	assert response.status_code == 201
	completion = QuestCompletion.objects.get(quest=quest)
	assert response.data == {'id': completion.id, 'status': 'failed'}


# --- Daily bonus -------------------------------------------------------------


def make_bonus_quests(user, count=3, quest_type=Quest.QuestType.DAILY):
	return [
		Quest.objects.create(
			user=user,
			title=f'Bonus quest {index}',
			quest_type=quest_type,
			xp_reward=25,
		)
		for index in range(count)
	]


def complete_all(user, quests):
	return [QuestService.complete_quest(quest.id, user) for quest in quests]


def local_day_start(user):
	character = Character.objects.get(user=user)
	return local_day(timezone.now(), character.timezone)[1]


@pytest.mark.django_db
def test_completing_every_daily_and_habit_quest_pays_the_daily_bonus(
	api_client,
	user,
):
	quests = [
		*make_bonus_quests(user, 2),
		*make_bonus_quests(user, 1, quest_type=Quest.QuestType.HABIT),
	]
	api_client.force_authenticate(user=user)

	responses = [
		api_client.post(f'/api/quests/{quest.id}/complete/', format='json')
		for quest in quests
	]

	assert [response.data['daily_bonus'] for response in responses] == [
		None,
		None,
		{'xp': 25, 'coins': 25},
	]
	# The quest's own reward is reported apart from the bonus.
	assert responses[-1].data['xp_gained'] == 25
	assert responses[-1].data['coins_gained'] == 25
	bonus = DailyBonus.objects.get(user=user)
	assert bonus.xp_awarded == 25
	assert bonus.local_date == datetime.now(datetime_timezone.utc).date()
	assert bonus.quest_completion == QuestCompletion.objects.get(quest=quests[-1])
	character = Character.objects.get(user=user)
	assert character.xp == character.coins == 100


@pytest.mark.django_db
def test_daily_bonus_xp_reaches_the_level_check(user):
	results = complete_all(user, make_bonus_quests(user, 3))

	# 75 quest XP stays below the 100 of level 2; the bonus crosses it.
	assert results[-1]['leveled_up'] is True
	assert results[-1]['new_level'] == 2
	assert Character.objects.get(user=user).level == 2


@pytest.mark.django_db
@override_settings(QUESTIFY_DAILY_BONUS_XP=40, QUESTIFY_DAILY_BONUS_MIN_QUESTS=1)
def test_daily_bonus_follows_the_active_settings(user):
	results = complete_all(user, make_bonus_quests(user, 1))

	assert results[-1]['daily_bonus'] == {'xp': 40, 'coins': 40}


@pytest.mark.django_db
def test_daily_bonus_needs_the_minimum_number_of_quests(api_client, user):
	results = complete_all(user, make_bonus_quests(user, 2))
	api_client.force_authenticate(user=user)

	response = api_client.get('/api/quests/daily-bonus/')

	assert [result['daily_bonus'] for result in results] == [None, None]
	assert not DailyBonus.objects.exists()
	assert response.status_code == 200
	assert (
		response.data['total'],
		response.data['completed'],
		response.data['required'],
		response.data['awarded'],
	) == (2, 2, 3, False)


@pytest.mark.django_db
def test_a_quest_failed_today_blocks_the_daily_bonus(user):
	first, *others = make_bonus_quests(user, 3)

	QuestService.fail_quest(first.id, user)
	results = complete_all(user, others)

	assert [result['daily_bonus'] for result in results] == [None, None]
	assert not DailyBonus.objects.exists()


@pytest.mark.django_db
def test_daily_bonus_ignores_one_time_inactive_archived_and_expired_quests(user):
	quests = make_bonus_quests(user, 3)
	Quest.objects.create(
		user=user,
		title='One-time',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	Quest.objects.create(
		user=user,
		title='Paused',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
		is_active=False,
	)
	Quest.objects.create(
		user=user,
		title='Archived',
		quest_type=Quest.QuestType.HABIT,
		xp_reward=25,
		is_active=False,
		archived_at=timezone.now(),
	)
	# Its deadline passed just before the local day began.
	Quest.objects.create(
		user=user,
		title='Expired',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
		deadline=local_day_start(user) - timedelta(seconds=1),
	)

	results = complete_all(user, quests)

	assert results[-1]['daily_bonus'] == {'xp': 25, 'coins': 25}


@pytest.mark.django_db
def test_quest_completed_before_its_deadline_stays_in_the_daily_bonus_set(user):
	with_deadline, *others = make_bonus_quests(user, 3)
	Quest.objects.filter(pk=with_deadline.pk).update(
		deadline=timezone.now() + timedelta(hours=1),
	)
	QuestService.complete_quest(with_deadline.id, user)
	# The deadline passes after the completion, within the same day.
	Quest.objects.filter(pk=with_deadline.pk).update(
		deadline=timezone.now() - timedelta(seconds=1),
	)

	results = complete_all(user, others)

	assert results[-1]['daily_bonus'] == {'xp': 25, 'coins': 25}


@pytest.mark.django_db
def test_deadline_at_the_start_of_the_local_day_leaves_the_daily_bonus_set(user):
	quests = make_bonus_quests(user, 3)
	# Due at local midnight: counted as due before the day began.
	Quest.objects.create(
		user=user,
		title='Due at midnight',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
		deadline=local_day_start(user),
	)

	results = complete_all(user, quests)

	assert QuestService.daily_bonus_status(user).total == 3
	assert results[-1]['daily_bonus'] == {'xp': 25, 'coins': 25}


@pytest.mark.django_db
def test_deadline_a_second_into_the_local_day_keeps_the_quest_in_the_set(user):
	quests = make_bonus_quests(user, 3)
	Quest.objects.create(
		user=user,
		title='Due a second after midnight',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
		deadline=local_day_start(user) + timedelta(seconds=1),
	)

	results = complete_all(user, quests)

	assert QuestService.daily_bonus_status(user).total == 4
	assert results[-1]['daily_bonus'] is None


def move_deadline(quest, delta):
	Quest.objects.filter(pk=quest.pk).update(deadline=timezone.now() + delta)


def assert_the_missed_quest_blocked_the_bonus(api_client, user, results):
	# Three of four quests completed; the fourth missed its deadline today.
	api_client.force_authenticate(user=user)
	response = api_client.get('/api/quests/daily-bonus/')

	assert [result['daily_bonus'] for result in results] == [None, None, None]
	assert not DailyBonus.objects.exists()
	assert response.status_code == 200
	assert (
		response.data['total'],
		response.data['completed'],
		response.data['awarded'],
	) == (4, 3, False)


@pytest.mark.django_db
def test_deadline_missed_today_after_the_others_blocks_the_daily_bonus(
	api_client,
	user,
):
	*others, missed = make_bonus_quests(user, 4)
	move_deadline(missed, timedelta(hours=1))
	results = complete_all(user, others)

	# The deadline passes later the same day, with nothing left to complete.
	move_deadline(missed, -timedelta(seconds=1))

	assert_the_missed_quest_blocked_the_bonus(api_client, user, results)


@pytest.mark.django_db
def test_deadline_missed_today_before_the_others_blocks_the_daily_bonus(
	api_client,
	user,
):
	*others, missed = make_bonus_quests(user, 4)
	move_deadline(missed, -timedelta(seconds=1))

	results = complete_all(user, others)

	# The same outcome as when the deadline passes after the completions.
	assert_the_missed_quest_blocked_the_bonus(api_client, user, results)


@pytest.mark.django_db
def test_quest_failed_before_its_deadline_blocks_the_daily_bonus_after_it(
	api_client,
	user,
):
	*others, failed = make_bonus_quests(user, 4)
	move_deadline(failed, timedelta(hours=1))
	QuestService.fail_quest(failed.id, user)
	move_deadline(failed, -timedelta(seconds=1))

	results = complete_all(user, others)

	assert_the_missed_quest_blocked_the_bonus(api_client, user, results)


@pytest.mark.django_db
def test_daily_bonus_is_paid_once_per_local_day(user):
	complete_all(user, make_bonus_quests(user, 3))
	added_later = Quest.objects.create(
		user=user,
		title='Added after the bonus',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)

	result = QuestService.complete_quest(added_later.id, user)

	assert result['xp_gained'] == 25
	assert result['daily_bonus'] is None
	assert DailyBonus.objects.filter(user=user).count() == 1
	assert Character.objects.get(user=user).xp == 4 * 25 + 25


@pytest.mark.django_db
def test_a_local_date_repeated_by_a_timezone_change_gets_no_second_bonus(user):
	# Paid for this date in an earlier zone, more than 24 hours ago.
	DailyBonus.objects.create(
		user=user,
		local_date=datetime.now(datetime_timezone.utc).date(),
		awarded_at=timezone.now() - timedelta(hours=25),
		xp_awarded=25,
	)

	results = complete_all(user, make_bonus_quests(user, 3))

	assert results[-1]['daily_bonus'] is None
	assert DailyBonus.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_database_keeps_one_paid_daily_bonus_per_user_and_date(user):
	day = date(2026, 10, 28)
	DailyBonus.objects.create(user=user, local_date=day, xp_awarded=25)

	with pytest.raises(IntegrityError), transaction.atomic():
		DailyBonus.objects.create(user=user, local_date=day, xp_awarded=25)
	with pytest.raises(IntegrityError), transaction.atomic():
		DailyBonus.objects.create(
			user=user,
			local_date=day + timedelta(days=1),
			xp_awarded=0,
		)


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=90)
def test_daily_bonus_is_cut_to_the_xp_left_under_the_cap(user):
	results = complete_all(user, make_bonus_quests(user, 3))

	assert results[-1]['daily_bonus'] == {'xp': 15, 'coins': 15}
	assert DailyBonus.objects.get(user=user).xp_awarded == 15
	character = Character.objects.get(user=user)
	assert character.xp == character.coins == 90


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=110)
def test_daily_bonus_counts_toward_the_24_hour_cap(user):
	quests = make_bonus_quests(user, 3)
	one_time = Quest.objects.create(
		user=user,
		title='After the bonus',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=50,
	)
	complete_all(user, quests)

	result = QuestService.complete_quest(one_time.id, user)

	# 75 quest XP and the 25 bonus leave 10.
	assert result['xp_gained'] == 10


@pytest.mark.django_db
@override_settings(QUESTIFY_XP_24H_CAP=125)
def test_bonus_the_cap_left_no_room_for_waits_for_a_later_daily_completion(user):
	earlier = QuestCompletion.objects.create(
		user=user,
		status=QuestCompletion.CompletionStatus.COMPLETED,
		completed_at=timezone.now() - timedelta(hours=23),
		xp_awarded=50,
	)
	one_time = Quest.objects.create(
		user=user,
		title='One-time',
		quest_type=Quest.QuestType.ONE_TIME,
		xp_reward=10,
	)

	results = complete_all(user, make_bonus_quests(user, 3))
	assert results[-1]['daily_bonus'] is None
	assert not DailyBonus.objects.exists()

	# The earlier XP leaves the 24-hour window.
	QuestCompletion.objects.filter(pk=earlier.pk).update(
		completed_at=timezone.now() - timedelta(hours=25),
	)
	one_time_result = QuestService.complete_quest(one_time.id, user)
	added_later = Quest.objects.create(
		user=user,
		title='Added later',
		quest_type=Quest.QuestType.DAILY,
		xp_reward=25,
	)
	late_result = QuestService.complete_quest(added_later.id, user)

	# A one-time completion never finishes the day, even with room left.
	assert one_time_result['daily_bonus'] is None
	# 75 + 10 + 25 of 125 leaves 15.
	assert late_result['daily_bonus'] == {'xp': 15, 'coins': 15}
	assert DailyBonus.objects.get(user=user).quest_completion == (
		QuestCompletion.objects.get(quest=added_later)
	)


@pytest.mark.django_db
def test_completion_paying_the_daily_bonus_takes_no_extra_character_locks(user):
	from django.db import connection
	from django.test.utils import CaptureQueriesContext

	*earlier, last = make_bonus_quests(user, 3)
	complete_all(user, earlier)

	with CaptureQueriesContext(connection) as queries:
		result = QuestService.complete_quest(last.id, user)

	assert result['daily_bonus'] is not None
	character_locks = [
		query['sql']
		for query in queries
		if 'FROM "core_character"' in query['sql'] and 'FOR UPDATE' in query['sql']
	]
	assert len(character_locks) == 3


@pytest.mark.django_db
def test_daily_bonus_status_reports_progress_in_the_users_timezone(api_client, user):
	Character.objects.filter(user=user).update(timezone='Pacific/Kiritimati')
	first, *others = make_bonus_quests(user, 3)
	api_client.force_authenticate(user=user)

	QuestService.complete_quest(first.id, user)
	before = api_client.get('/api/quests/daily-bonus/')
	complete_all(user, others)
	after = api_client.get('/api/quests/daily-bonus/')

	local_today = timezone.localdate(timezone=ZoneInfo('Pacific/Kiritimati'))
	assert before.status_code == 200
	assert before.data == {
		'date': local_today.isoformat(),
		'required': 3,
		'completed': 1,
		'total': 3,
		'awarded': False,
		'xp': 25,
	}
	assert after.data == {
		'date': local_today.isoformat(),
		'required': 3,
		'completed': 3,
		'total': 3,
		'awarded': True,
		'xp': 25,
	}


def test_daily_bonus_status_is_documented_as_one_object():
	from drf_spectacular.generators import SchemaGenerator

	operation = SchemaGenerator().get_schema(public=True)['paths'][
		'/api/quests/daily-bonus/'
	]['get']

	assert operation['responses']['200']['content']['application/json'][
		'schema'
	] == {'$ref': '#/components/schemas/DailyBonusStatus'}


def test_admin_shows_daily_bonuses_read_only():
	bonus_admin = DailyBonusAdmin(DailyBonus, django_admin.site)

	assert bonus_admin.has_add_permission(None) is False
	assert bonus_admin.has_change_permission(None) is False
	assert bonus_admin.has_delete_permission(None) is False
