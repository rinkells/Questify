import threading
from io import StringIO
from unittest.mock import Mock, patch

import pytest
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.forms.models import inlineformset_factory
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from achievements.models import Achievement, UserAchievement
from core.models import Character, StatType
from core.throttling import AtomicUserRateThrottle
from events.dispatcher import dispatcher
from events.event_types import EventResult, SkillUnlockedEvent
from quests.models import Quest, QuestCategory, QuestCompletion

from .admin import SkillAdmin, SkillRequirementInline, UserSkillAdmin
from .models import Skill, SkillRequirement, UserSkill, lock_skill_catalog
from .serializers import (
    SkillRequirementProgressSerializer,
    SkillTreeNodeSerializer,
)
from .services import SkillRequirementsNotMetError, SkillService
from .views import SkillUnlockView


User = get_user_model()
RequirementType = SkillRequirement.RequirementType


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def user(db):
    return User.objects.create_user(username='skill-user', password='password')


@pytest.fixture
def stat_type(db):
    return StatType.objects.create(
        name='INT',
        display_name='Intelligence',
        icon='brain',
    )


@pytest.fixture
def skill_events():
    spy = Mock()
    dispatcher.register(SkillUnlockedEvent, spy)
    yield spy
    dispatcher._handlers.remove((SkillUnlockedEvent, spy))


def make_skill(stat_type, name, *requirements, parent=None):
    skill = Skill.objects.create(name=name, category=stat_type, parent=parent)
    for requirement_type, threshold, *category in requirements:
        SkillRequirement.objects.create(
            skill=skill,
            requirement_type=requirement_type,
            threshold=threshold,
            category_name=category[0] if category else '',
        )
    return skill


def record_completion(user, *, xp_awarded=25, category_name='', **fields):
    return QuestCompletion.objects.create(
        user=user,
        status=QuestCompletion.CompletionStatus.COMPLETED,
        xp_awarded=xp_awarded,
        category_name_at_completion=category_name,
        **fields,
    )


def tree_node(user, name):
    pending = list(SkillService.get_tree(user))
    while pending:
        node = pending.pop()
        if node['name'] == name:
            return node
        pending.extend(node['children'])
    raise AssertionError(f'{name} is not in the tree')


@pytest.mark.django_db
def test_archived_quest_completion_still_counts_toward_requirement(
    api_client,
    user,
    stat_type,
):
    category = QuestCategory.objects.create(user=user, name='Study')
    quest = Quest.objects.create(
        user=user,
        category=category,
        title='Read a chapter',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=50,
    )
    assert api_client.post(f'/api/quests/{quest.id}/complete/').status_code == 200
    assert api_client.delete(f'/api/quests/{quest.id}/').status_code == 204
    quest.refresh_from_db()
    assert quest.archived_at is not None
    skill = make_skill(
        stat_type,
        'Scholar',
        (RequirementType.QUEST_COUNT, 1),
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 1, 'Study'),
    )

    node = tree_node(user, 'Scholar')

    assert node['status'] == 'available'
    assert [item['current'] for item in node['requirements']] == [1, 1]
    assert api_client.post(f'/api/skills/{skill.id}/unlock/').status_code == 200


@pytest.mark.django_db
def test_category_requirement_uses_name_recorded_at_completion(user, stat_type):
    category = QuestCategory.objects.create(user=user, name='Study')
    quest = Quest.objects.create(
        user=user,
        category=category,
        title='Read',
        quest_type=Quest.QuestType.DAILY,
        xp_reward=25,
    )
    record_completion(user, quest=quest, category_name='Study')
    record_completion(user, quest=quest, category_name='STUDY')
    record_completion(user, quest=quest, category_name='study')
    category.name = 'Maths'
    category.save()
    quest.delete()
    make_skill(
        stat_type,
        'Scholar',
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 3, 'Study'),
    )

    requirement = tree_node(user, 'Scholar')['requirements'][0]

    assert requirement == {
        'type': 'quest_count_by_category',
        'category': 'Study',
        'current': 3,
        'required': 3,
        'met': True,
    }


@pytest.mark.django_db
def test_xp_requirement_uses_character_xp(api_client, user, stat_type):
    # Completion rows are not the source: character XP is what levels and
    # the total_xp achievement use, including back-filled legacy XP.
    record_completion(user, xp_awarded=1000)
    user.character.xp = 40
    user.character.save(update_fields=('xp',))
    skill = make_skill(stat_type, 'Rich', (RequirementType.TOTAL_XP, 50))

    requirement = tree_node(user, 'Rich')['requirements'][0]
    response = api_client.post(f'/api/skills/{skill.id}/unlock/')

    assert (requirement['current'], requirement['met']) == (40, False)
    assert response.status_code == 400
    assert response.data['detail'] == 'Requirements not met: XP 40/50.'

    user.character.xp = 50
    user.character.save(update_fields=('xp',))

    assert tree_node(user, 'Rich')['status'] == 'available'
    assert api_client.post(f'/api/skills/{skill.id}/unlock/').status_code == 200


@pytest.mark.django_db
def test_unlock_with_unmet_requirements_explains_what_is_missing(
    api_client,
    user,
    stat_type,
    skill_events,
):
    record_completion(user, category_name='Health')
    user.character.xp = 25
    user.character.save(update_fields=('xp',))
    skill = make_skill(
        stat_type,
        'Athlete',
        (RequirementType.QUEST_COUNT, 3),
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 2, 'Health'),
        (RequirementType.TOTAL_XP, 25),
    )

    response = api_client.post(f'/api/skills/{skill.id}/unlock/')

    assert response.status_code == 400
    assert response.data['detail'] == (
        'Requirements not met: completed quests 1/3; '
        'completed quests in "Health" 1/2.'
    )
    assert not UserSkill.objects.filter(user=user).exists()
    skill_events.assert_not_called()


@pytest.mark.django_db
def test_skill_unlocks_only_after_its_parent(api_client, user, stat_type):
    parent = make_skill(stat_type, 'Parent')
    child = make_skill(stat_type, 'Child', parent=parent)

    assert tree_node(user, 'Child')['status'] == 'locked'
    response = api_client.post(f'/api/skills/{child.id}/unlock/')
    assert response.status_code == 409
    assert response.data['detail'] == 'Unlock "Parent" before "Child".'

    assert api_client.post(f'/api/skills/{parent.id}/unlock/').status_code == 200
    assert tree_node(user, 'Child')['status'] == 'available'
    assert api_client.post(f'/api/skills/{child.id}/unlock/').status_code == 200
    assert tree_node(user, 'Child')['status'] == 'unlocked'


@pytest.mark.django_db
def test_repeated_unlock_returns_200_and_publishes_event_once(
    api_client,
    user,
    stat_type,
    skill_events,
):
    skill = make_skill(stat_type, 'Once')

    first = api_client.post(f'/api/skills/{skill.id}/unlock/')
    second = api_client.post(f'/api/skills/{skill.id}/unlock/')

    assert first.status_code == second.status_code == 200
    assert first.data == second.data
    assert first.data['id'] == skill.id
    assert first.data['status'] == 'unlocked'
    assert UserSkill.objects.filter(user=user, skill=skill).count() == 1
    skill_events.assert_called_once_with(
        SkillUnlockedEvent(user.id, skill.id, EventResult()),
    )


@pytest.mark.django_db(transaction=True)
def test_parallel_unlocks_create_one_user_skill(user, stat_type, skill_events):
    skill = make_skill(stat_type, 'Contested')
    barrier = threading.Barrier(8)
    errors = []

    def unlock():
        try:
            barrier.wait()
            SkillService.unlock_skill(user, skill.id)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=unlock) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert UserSkill.objects.filter(user=user, skill=skill).count() == 1
    skill_events.assert_called_once_with(
        SkillUnlockedEvent(user.id, skill.id, EventResult()),
    )


@pytest.mark.django_db
def test_database_rejects_a_duplicate_user_skill(user, stat_type):
    skill = make_skill(stat_type, 'Unique')
    UserSkill.objects.create(user=user, skill=skill)

    with pytest.raises(IntegrityError), transaction.atomic():
        UserSkill.objects.create(user=user, skill=skill)


@pytest.mark.django_db
def test_failing_handler_rolls_back_the_unlock(user, stat_type):
    skill = make_skill(stat_type, 'Fragile')
    achievement = Achievement.objects.create(
        name='First skill',
        condition_type=Achievement.ConditionType.SKILLS_UNLOCKED_COUNT,
        condition_config={'threshold': 1},
    )
    failing = Mock(side_effect=RuntimeError('handler failed'))
    dispatcher.register(SkillUnlockedEvent, failing)
    try:
        with pytest.raises(RuntimeError):
            SkillService.unlock_skill(user, skill.id)
    finally:
        dispatcher._handlers.remove((SkillUnlockedEvent, failing))

    assert not UserSkill.objects.filter(user=user).exists()
    assert not UserAchievement.objects.filter(
        user=user,
        achievement=achievement,
    ).exists()


@pytest.mark.django_db
def test_skill_unlock_awards_skills_unlocked_count_achievement(user, stat_type):
    achievement = Achievement.objects.create(
        name='Two skills',
        condition_type=Achievement.ConditionType.SKILLS_UNLOCKED_COUNT,
        condition_config={'threshold': 2},
    )
    first = make_skill(stat_type, 'First')
    second = make_skill(stat_type, 'Second')

    SkillService.unlock_skill(user, first.id)
    assert not UserAchievement.objects.filter(achievement=achievement).exists()
    SkillService.unlock_skill(user, second.id)

    assert UserAchievement.objects.filter(
        user=user,
        achievement=achievement,
    ).exists()


@pytest.mark.django_db
def test_unlock_response_reports_achievements_it_unlocked(api_client, user, stat_type):
    achievement = Achievement.objects.create(
        name='First skill',
        condition_type=Achievement.ConditionType.SKILLS_UNLOCKED_COUNT,
        condition_config={'threshold': 1},
    )
    skill = make_skill(stat_type, 'Rewarding')

    first = api_client.post(f'/api/skills/{skill.id}/unlock/')
    repeated = api_client.post(f'/api/skills/{skill.id}/unlock/')

    assert first.status_code == repeated.status_code == 200
    assert first.data['achievements_unlocked'] == [achievement.id]
    assert repeated.data['achievements_unlocked'] == []


@pytest.mark.django_db
def test_deleting_an_unlocked_skill_is_refused(user, stat_type):
    skill = make_skill(stat_type, 'Kept')
    SkillService.unlock_skill(user, skill.id)

    with pytest.raises(ProtectedError), transaction.atomic():
        skill.delete()

    assert UserSkill.objects.filter(user=user, skill=skill).exists()


@pytest.mark.django_db
def test_category_matching_lowercases_without_casefolding(user, stat_type):
    record_completion(user, category_name='Straße')
    make_skill(
        stat_type,
        'Walker',
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 1, 'STRASSE'),
    )
    make_skill(
        stat_type,
        'Local',
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 1, 'STRAßE'),
    )

    assert tree_node(user, 'Walker')['requirements'][0]['current'] == 0
    assert tree_node(user, 'Local')['requirements'][0]['current'] == 1


def requirement_formset(skill, *rows):
    """Bind the admin's requirement formset to POST-like data.

    Each row is (type, threshold, category) for a new requirement, or
    (type, threshold, category, existing_requirement[, 'delete']) to edit
    one. As in the admin, rows editing existing requirements come first.
    """
    formset_class = inlineformset_factory(
        Skill,
        SkillRequirement,
        formset=SkillRequirementInline.formset,
        fields=SkillRequirementInline.fields,
        extra=0,
    )
    prefix = formset_class.get_default_prefix()
    data = {
        f'{prefix}-TOTAL_FORMS': str(len(rows)),
        f'{prefix}-INITIAL_FORMS': str(sum(len(row) > 3 for row in rows)),
    }
    for index, (requirement_type, threshold, category_name, *existing) in (
        enumerate(rows)
    ):
        data[f'{prefix}-{index}-requirement_type'] = requirement_type
        data[f'{prefix}-{index}-threshold'] = str(threshold)
        data[f'{prefix}-{index}-category_name'] = category_name
        if existing:
            data[f'{prefix}-{index}-id'] = str(existing[0].pk)
            if existing[1:] == ['delete']:
                data[f'{prefix}-{index}-DELETE'] = 'on'
    return formset_class(data, instance=skill)


def check_deferred_constraints():
    # Deferred constraints are checked at commit, which a test that rolls
    # back never reaches; this runs the check now.
    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')


def admin_request():
    request = RequestFactory().post('/')
    request.user = User.objects.create_superuser(username='admin', password='x')
    return request


def admin_post(path, data):
    """A superuser's POST that admin views can answer with messages."""
    request = RequestFactory().post(path, data)
    request.user = User.objects.create_superuser(username='admin', password='x')
    request._dont_enforce_csrf_checks = True
    SessionMiddleware(lambda request: None).process_request(request)
    request._messages = FallbackStorage(request)
    return request


def admin_message(request):
    return str(next(iter(get_messages(request))))


@pytest.mark.django_db
def test_admin_rejects_requirements_that_differ_only_in_case(stat_type):
    skill = make_skill(stat_type, 'Edited')
    by_category = RequirementType.QUEST_COUNT_BY_CATEGORY

    duplicate = requirement_formset(
        skill,
        (by_category, 5, 'Study'),
        (by_category, 10, 'study '),
    )
    distinct = requirement_formset(
        skill,
        (by_category, 5, 'Study'),
        (by_category, 10, 'Health'),
        (RequirementType.TOTAL_XP, 100, ''),
        # Distinct under str.lower(), equal under PostgreSQL lower().
        (by_category, 1, 'İstanbul'),
        (by_category, 1, 'istanbul'),
    )

    assert not duplicate.is_valid()
    assert duplicate.non_form_errors()
    assert distinct.is_valid(), distinct.errors
    distinct.save()
    check_deferred_constraints()
    assert skill.requirements.count() == 5


def skill_with_study_and_health(stat_type):
    by_category = RequirementType.QUEST_COUNT_BY_CATEGORY
    skill = make_skill(
        stat_type,
        'Edited',
        (by_category, 1, 'Study'),
        (by_category, 2, 'Health'),
    )
    return skill, *skill.requirements.all()


@pytest.mark.django_db
def test_admin_rejects_a_duplicate_of_a_requirement_saved_from_another_tab(
    stat_type,
):
    by_category = RequirementType.QUEST_COUNT_BY_CATEGORY
    skill = make_skill(stat_type, 'Edited', (by_category, 5, 'Study'))

    # A page rendered before "Study" was added posts no form for it.
    stale_page = requirement_formset(skill, (by_category, 10, 'study'))

    assert not stale_page.is_valid()
    assert stale_page.non_form_errors()


@pytest.mark.django_db
def test_admin_checks_a_requirement_left_unchanged_as_stored(stat_type):
    by_category = RequirementType.QUEST_COUNT_BY_CATEGORY
    skill, study, health = skill_with_study_and_health(stat_type)

    # The posted "Health" row is unchanged, so it is not saved and its stored
    # value stays; renaming the other row onto it is a duplicate.
    rename_onto_kept = requirement_formset(
        skill,
        (by_category, 1, 'Health', study),
        (by_category, 2, 'Health', health),
    )

    assert not rename_onto_kept.is_valid()
    assert rename_onto_kept.non_form_errors()


@pytest.mark.django_db(transaction=True)
def test_parallel_requirement_edits_cannot_add_a_duplicate(stat_type):
    by_category = RequirementType.QUEST_COUNT_BY_CATEGORY
    skill = make_skill(stat_type, 'Root skill')

    def second_admin():
        with transaction.atomic():
            return requirement_formset(
                skill,
                (by_category, 10, 'study'),
            ).is_valid()

    # Each admin saves in the transaction it validated in.
    with transaction.atomic():
        first = requirement_formset(skill, (by_category, 5, 'Study'))
        assert first.is_valid(), first.errors
        thread, outcome = assert_waits_for_transaction(second_admin)
        first.save()
    thread.join(timeout=10)

    assert outcome == [False]
    assert skill.requirements.count() == 1


@pytest.mark.django_db
def test_admin_can_swap_requirement_names_in_one_save(stat_type):
    by_category = RequirementType.QUEST_COUNT_BY_CATEGORY
    skill, study, health = skill_with_study_and_health(stat_type)

    swap = requirement_formset(
        skill,
        (by_category, 1, 'Health', study),
        (by_category, 2, 'Study', health),
    )
    assert swap.is_valid(), swap.errors
    swap.save()
    check_deferred_constraints()
    assert sorted(skill.requirements.values_list('threshold', 'category_name')) == [
        (1, 'Health'),
        (2, 'Study'),
    ]


@pytest.mark.django_db
def test_admin_can_rename_onto_a_requirement_deleted_in_the_same_save(stat_type):
    by_category = RequirementType.QUEST_COUNT_BY_CATEGORY
    skill, study, health = skill_with_study_and_health(stat_type)

    rename_over_deleted = requirement_formset(
        skill,
        (by_category, 3, 'Health', study),
        (by_category, 2, 'Health', health, 'delete'),
    )
    assert rename_over_deleted.is_valid(), rename_over_deleted.errors
    rename_over_deleted.save()
    check_deferred_constraints()
    assert list(skill.requirements.values_list('threshold', 'category_name')) == [
        (3, 'Health'),
    ]


@pytest.mark.django_db
def test_database_rejects_requirements_with_the_same_category_key(stat_type):
    skill = make_skill(
        stat_type,
        'Checked',
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 1, 'Study'),
    )

    assert skill.requirements.get().category_key == 'study'
    with pytest.raises(IntegrityError), transaction.atomic():
        SkillRequirement.objects.create(
            skill=skill,
            requirement_type=RequirementType.QUEST_COUNT_BY_CATEGORY,
            threshold=2,
            category_name='STUDY',
        )
        check_deferred_constraints()


@pytest.mark.django_db
def test_admin_refuses_to_revoke_a_parent_while_a_child_stays(user, stat_type):
    parent = make_skill(stat_type, 'Parent')
    child = make_skill(stat_type, 'Child', parent=parent)
    SkillService.unlock_skill(user, parent.id)
    SkillService.unlock_skill(user, child.id)
    request = admin_request()
    model_admin = admin.site.get_model_admin(UserSkill)
    parent_unlock = UserSkill.objects.get(user=user, skill=parent)

    *_, parent_only = model_admin.get_deleted_objects([parent_unlock], request)
    *_, with_child = model_admin.get_deleted_objects(
        UserSkill.objects.filter(user=user),
        request,
    )

    assert len(parent_only) == 1
    assert 'Child' in parent_only[0]
    assert with_child == []


@pytest.mark.django_db
def test_admin_delete_checks_children_again_when_deleting(user, stat_type):
    parent = make_skill(stat_type, 'Parent')
    child = make_skill(stat_type, 'Child', parent=parent)
    SkillService.unlock_skill(user, parent.id)
    # Unlocked after the admin opened the confirmation page.
    SkillService.unlock_skill(user, child.id)
    request = admin_request()
    model_admin = admin.site.get_model_admin(UserSkill)

    with pytest.raises(ProtectedError), transaction.atomic():
        model_admin.delete_model(
            request,
            UserSkill.objects.get(user=user, skill=parent),
        )
    assert UserSkill.objects.filter(user=user).count() == 2

    model_admin.delete_queryset(request, UserSkill.objects.filter(user=user))
    assert not UserSkill.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_a_skill_cannot_move_under_a_parent_its_holders_lack(user, stat_type):
    first = make_skill(stat_type, 'First')
    second = make_skill(stat_type, 'Second')
    SkillService.unlock_skill(user, second.id)

    second.parent = first
    with pytest.raises(ValidationError) as error:
        second.full_clean()
    assert 'parent' in error.value.message_dict
    with pytest.raises(ValidationError), transaction.atomic():
        second.save()

    SkillService.unlock_skill(user, first.id)
    second.full_clean()
    second.save()
    assert tree_node(user, 'Second')['parent_id'] == first.id


@pytest.mark.django_db(transaction=True)
def test_parallel_parent_edits_cannot_form_a_cycle(stat_type):
    first = make_skill(stat_type, 'First')
    second = make_skill(stat_type, 'Second')
    barrier = threading.Barrier(2)
    errors = []

    def move(skill_id, parent_id):
        try:
            skill = Skill.objects.get(pk=skill_id)
            skill.parent_id = parent_id
            barrier.wait()
            skill.save()
        except ValidationError as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=move, args=(first.id, second.id)),
        threading.Thread(target=move, args=(second.id, first.id)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(errors) == 1
    parents = dict(Skill.objects.values_list('id', 'parent_id'))
    assert None in (parents[first.id], parents[second.id])


def start_in_thread(target):
    """Run ``target`` in a daemon thread and collect its result or error.

    Daemon, and joined with timeouts by the callers, so a regression fails
    the test instead of hanging the whole run.
    """
    outcome = []

    def run():
        try:
            outcome.append(target())
        except Exception as exc:
            outcome.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def assert_waits_for_transaction(target):
    """Start ``target`` inside the caller's open transaction and check that
    it blocks until that transaction ends. Returns its outcome list."""
    thread, outcome = start_in_thread(target)
    thread.join(timeout=0.5)
    assert thread.is_alive(), outcome
    return thread, outcome


@pytest.mark.django_db(transaction=True)
def test_unlock_waits_for_a_requirement_edit_in_progress(user, stat_type):
    skill = make_skill(stat_type, 'Raised', (RequirementType.QUEST_COUNT, 0))

    # The requirement alone, in bulk: the skill row is not touched.
    with transaction.atomic():
        SkillRequirement.objects.filter(skill=skill).update(threshold=1)
        thread, outcome = assert_waits_for_transaction(
            lambda: SkillService.unlock_skill(user, skill.id),
        )
    thread.join(timeout=10)

    assert len(outcome) == 1
    assert isinstance(outcome[0], SkillRequirementsNotMetError)


@pytest.mark.django_db(transaction=True)
def test_unlock_waiting_for_the_catalog_does_not_hold_the_character_row(
    user,
    stat_type,
):
    skill = make_skill(stat_type, 'Waiting')

    with transaction.atomic():
        Skill.objects.filter(pk=skill.pk).update(description='Being edited')
        thread, outcome = assert_waits_for_transaction(
            lambda: SkillService.unlock_skill(user, skill.id),
        )
        # Quest completions lock this row; they must not wait for the edit.
        Character.objects.select_for_update(nowait=True).get(user=user)
    thread.join(timeout=10)

    assert len(outcome) == 1
    assert outcome[0].user_skill.skill_id == skill.id


@pytest.mark.django_db(transaction=True)
def test_revoking_waits_for_a_catalog_edit_in_progress(user, stat_type):
    skill = make_skill(stat_type, 'Revoked')
    SkillService.unlock_skill(user, skill.id)
    request = admin_request()
    model_admin = admin.site.get_model_admin(UserSkill)

    with transaction.atomic():
        Skill.objects.filter(pk=skill.pk).update(description='Being edited')
        thread, outcome = assert_waits_for_transaction(
            lambda: model_admin.delete_queryset(
                request,
                UserSkill.objects.filter(user=user),
            ),
        )
    thread.join(timeout=10)

    assert outcome == [None]
    assert not UserSkill.objects.filter(user=user).exists()


@pytest.mark.django_db(transaction=True)
def test_validating_a_parent_change_locks_the_catalog_until_commit(
    user,
    stat_type,
):
    parent = make_skill(stat_type, 'Parent')
    moved = make_skill(stat_type, 'Moved')
    other = make_skill(stat_type, 'Other')

    # The admin validates and saves in one transaction.
    with transaction.atomic():
        moved.parent = parent
        moved.full_clean()
        thread, outcome = assert_waits_for_transaction(
            lambda: SkillService.unlock_skill(user, other.id),
        )
        moved.save()
    thread.join(timeout=10)

    assert len(outcome) == 1
    assert outcome[0].user_skill.skill_id == other.id


@pytest.mark.django_db
def test_admin_reports_a_child_unlocked_after_the_confirmation_page(
    user,
    stat_type,
):
    parent = make_skill(stat_type, 'Parent')
    child = make_skill(stat_type, 'Child', parent=parent)
    SkillService.unlock_skill(user, parent.id)
    SkillService.unlock_skill(user, child.id)
    parent_unlock = UserSkill.objects.get(user=user, skill=parent)
    path = f'/admin/skills/userskill/{parent_unlock.pk}/delete/'
    request = admin_post(path, {'post': 'yes'})
    model_admin = admin.site.get_model_admin(UserSkill)

    # The page was rendered before the child was unlocked, so it listed
    # nothing protected; only the check in delete_queryset sees the child.
    with patch.object(
        UserSkillAdmin,
        'get_deleted_objects',
        admin.ModelAdmin.get_deleted_objects,
    ):
        response = model_admin.delete_view(request, str(parent_unlock.pk))

    assert response.status_code == 302
    assert response['Location'] == path
    assert 'Nothing was deleted' in admin_message(request)
    assert UserSkill.objects.filter(user=user).count() == 2


@pytest.mark.django_db
def test_refused_bulk_revoke_leaves_no_deletion_in_the_history(
    user,
    stat_type,
):
    parent = make_skill(stat_type, 'Parent')
    child = make_skill(stat_type, 'Child', parent=parent)
    SkillService.unlock_skill(user, parent.id)
    SkillService.unlock_skill(user, child.id)
    parent_unlock = UserSkill.objects.get(user=user, skill=parent)
    request = admin_post('/admin/skills/userskill/', {
        'action': 'delete_selected',
        '_selected_action': [parent_unlock.pk],
        'post': 'yes',
    })
    model_admin = admin.site.get_model_admin(UserSkill)

    with patch.object(
        UserSkillAdmin,
        'get_deleted_objects',
        admin.ModelAdmin.get_deleted_objects,
    ):
        response = model_admin.response_action(
            request,
            UserSkill.objects.all(),
        )

    assert response.status_code == 302
    assert 'Nothing was deleted' in admin_message(request)
    assert UserSkill.objects.filter(user=user).count() == 2
    assert not LogEntry.objects.exists()


@pytest.mark.django_db
def test_admin_reports_a_skill_unlocked_after_the_confirmation_page(
    user,
    stat_type,
):
    skill = make_skill(stat_type, 'Deleted')
    SkillService.unlock_skill(user, skill.id)
    path = f'/admin/skills/skill/{skill.pk}/delete/'
    request = admin_post(path, {'post': 'yes'})
    model_admin = admin.site.get_model_admin(Skill)

    # The page was rendered before the unlock, so it listed nothing protected.
    with patch.object(
        SkillAdmin,
        'get_deleted_objects',
        return_value=([], {}, set(), []),
    ):
        response = model_admin.delete_view(request, str(skill.pk))

    assert response.status_code == 302
    assert response['Location'] == path
    assert 'Nothing was deleted' in admin_message(request)
    assert Skill.objects.filter(pk=skill.pk).exists()
    assert not LogEntry.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_adding_a_child_skill_leaves_the_catalog_editable(stat_type):
    parent = make_skill(stat_type, 'Parent')
    other = make_skill(stat_type, 'Other')

    # As in seed_skills, which builds the whole tree in one transaction.
    with transaction.atomic():
        child = Skill(name='New child', category=stat_type, parent=parent)
        child.full_clean()
        child.save()
        thread, outcome = start_in_thread(
            lambda: Skill.objects.filter(pk=other.pk).update(
                description='Edited meanwhile',
            ),
        )
        thread.join(timeout=10)
        assert not thread.is_alive(), 'the edit waited for the transaction'

    assert outcome == [1]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('validated', [True, False], ids=['admin', 'save'])
def test_new_child_of_a_skill_being_deleted_is_refused(stat_type, validated):
    parent = make_skill(stat_type, 'Deleted parent')
    parent_id = parent.id

    def add_child():
        with transaction.atomic():
            child = Skill(name='Child', category=stat_type, parent_id=parent_id)
            if validated:
                child.full_clean()
            child.save()

    with transaction.atomic():
        parent.delete()
        thread, outcome = assert_waits_for_transaction(add_child)
    thread.join(timeout=10)

    # A form error (or an error from save()), not a foreign key violation
    # at commit.
    assert len(outcome) == 1
    assert isinstance(outcome[0], ValidationError), outcome
    assert 'parent' in outcome[0].message_dict
    assert not Skill.objects.exists()


@pytest.mark.django_db
def test_saving_other_fields_ignores_a_stale_parent(stat_type):
    first = make_skill(stat_type, 'First')
    second = make_skill(stat_type, 'Second')
    moved = make_skill(stat_type, 'Moved', parent=first)
    stale = Skill.objects.get(pk=moved.pk)

    # Meanwhile another admin moves it under Second and First under it.
    moved.parent = second
    moved.save()
    first.parent = moved
    first.save()

    stale.order = 5
    stale.save(update_fields=['order'])
    assert Skill.objects.get(pk=moved.pk).order == 5
    # Writing the stale parent back would form a cycle and is still refused.
    with pytest.raises(ValidationError):
        stale.save()
    assert Skill.objects.get(pk=moved.pk).parent_id == second.id


@pytest.mark.django_db
def test_requirement_saved_without_clean_is_trimmed(user, stat_type):
    record_completion(user, category_name='Study')
    skill = make_skill(
        stat_type,
        'Trimmed',
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 1, 'Study '),
    )

    requirement = skill.requirements.get()
    assert (requirement.category_name, requirement.category_key) == (
        'Study',
        'study',
    )
    assert tree_node(user, 'Trimmed')['requirements'][0]['current'] == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        SkillRequirement.objects.create(
            skill=skill,
            requirement_type=RequirementType.QUEST_COUNT_BY_CATEGORY,
            threshold=2,
            category_name=' study',
        )
        check_deferred_constraints()


@pytest.mark.django_db
def test_does_not_exist_from_a_handler_is_not_reported_as_unknown_skill(
    api_client,
    stat_type,
):
    skill = make_skill(stat_type, 'Handled')
    failing = Mock(side_effect=Skill.DoesNotExist('looked up by a handler'))
    dispatcher.register(SkillUnlockedEvent, failing)
    try:
        with pytest.raises(Skill.DoesNotExist):
            api_client.post(f'/api/skills/{skill.id}/unlock/')
    finally:
        dispatcher._handlers.remove((SkillUnlockedEvent, failing))


def test_catalog_lock_accepts_only_known_modes():
    with pytest.raises(ValueError):
        lock_skill_catalog('SHARE MODE; DROP TABLE skills_skill; --')


@pytest.mark.django_db
def test_unlock_reads_completions_only_for_quest_count_requirements(
    user,
    stat_type,
):
    xp_only = make_skill(stat_type, 'XP only', (RequirementType.TOTAL_XP, 0))
    counted = make_skill(stat_type, 'Counted', (RequirementType.QUEST_COUNT, 0))

    def completion_queries(skill):
        with CaptureQueriesContext(connection) as queries:
            SkillService.unlock_skill(user, skill.id)
        return [
            query['sql']
            for query in queries
            if 'quests_questcompletion' in query['sql']
        ]

    assert completion_queries(xp_only) == []
    assert completion_queries(xp_only) == []
    assert len(completion_queries(counted)) == 1
    assert completion_queries(counted) == []


@pytest.mark.django_db
def test_parent_check_reads_the_catalog_once_however_deep(stat_type):
    chain = [make_skill(stat_type, 'Level 0')]
    for depth in range(1, 8):
        chain.append(make_skill(stat_type, f'Level {depth}', parent=chain[-1]))
    moved = make_skill(stat_type, 'Moved', parent=chain[0])
    moved.parent = chain[-1]

    with CaptureQueriesContext(connection) as queries:
        assert moved._parent_error() is None

    # The old parent, the new parent's existence and the whole catalog.
    assert sum('FROM "skills_skill"' in q['sql'] for q in queries) == 3


@pytest.mark.django_db
def test_parent_cycles_are_rejected(stat_type):
    root = make_skill(stat_type, 'Root')
    middle = make_skill(stat_type, 'Middle', parent=root)
    leaf = make_skill(stat_type, 'Leaf', parent=middle)

    root.parent = leaf
    with pytest.raises(ValidationError) as cycle_error:
        root.full_clean()
    assert 'parent' in cycle_error.value.message_dict

    root.parent = root
    with pytest.raises(ValidationError):
        root.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        Skill.objects.filter(pk=root.pk).update(parent=root)

    leaf.parent = root
    leaf.full_clean()


@pytest.mark.django_db
def test_requirement_category_must_match_its_type(stat_type):
    skill = make_skill(stat_type, 'Checked')

    with pytest.raises(ValidationError):
        SkillRequirement(
            skill=skill,
            requirement_type=RequirementType.QUEST_COUNT_BY_CATEGORY,
            threshold=1,
            category_name='  ',
        ).full_clean()
    with pytest.raises(ValidationError):
        SkillRequirement(
            skill=skill,
            requirement_type=RequirementType.TOTAL_XP,
            threshold=1,
            category_name='Study',
        ).full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        SkillRequirement.objects.create(
            skill=skill,
            requirement_type=RequirementType.QUEST_COUNT,
            threshold=1,
            category_name='Study',
        )


@pytest.mark.django_db
def test_tree_uses_a_constant_number_of_queries(
    user,
    stat_type,
    django_assert_num_queries,
):
    record_completion(user, category_name='Study')
    user.character.xp = 10
    user.character.save(update_fields=('xp',))
    parent = None
    for index in range(10):
        parent = make_skill(
            stat_type,
            f'Skill {index}',
            (RequirementType.TOTAL_XP, 10),
            (RequirementType.QUEST_COUNT_BY_CATEGORY, 1, 'Study'),
            parent=parent,
        )

    with django_assert_num_queries(5):
        tree = SkillService.get_tree(user)

    root = tree[0]
    assert set(root) == {
        'id',
        'name',
        'description',
        'category',
        'parent_id',
        'status',
        'requirements',
        'children',
    }
    assert root['parent_id'] is None
    assert root['status'] == 'available'
    assert root['children'][0]['parent_id'] == root['id']
    assert root['children'][0]['status'] == 'locked'


@pytest.mark.django_db
def test_tree_matches_the_documented_schema(user, stat_type):
    root = make_skill(
        stat_type,
        'Root',
        (RequirementType.QUEST_COUNT_BY_CATEGORY, 1, 'Study'),
    )
    child = make_skill(stat_type, 'Child', (RequirementType.TOTAL_XP, 1), parent=root)
    make_skill(stat_type, 'Grandchild', (RequirementType.QUEST_COUNT, 1), parent=child)
    node_fields = set(SkillTreeNodeSerializer().fields)
    requirement_fields = set(SkillRequirementProgressSerializer().fields)

    pending = list(SkillService.get_tree(user))
    seen = 0
    while pending:
        node = pending.pop()
        seen += 1
        assert set(node) == node_fields
        for requirement in node['requirements']:
            assert set(requirement) == requirement_fields
        pending.extend(node['children'])
    assert seen == 3


@pytest.mark.django_db
def test_unknown_skill_returns_404(api_client):
    response = api_client.post('/api/skills/999999/unlock/')

    assert response.status_code == 404
    assert response.data['detail'] == 'Skill not found.'


def test_unlock_endpoint_uses_the_default_user_throttle():
    throttles = SkillUnlockView().get_throttles()

    assert any(isinstance(item, AtomicUserRateThrottle) for item in throttles)


@pytest.mark.django_db
def test_seed_skills_builds_the_example_tree_once(user):
    call_command('seed_skills', stdout=StringIO())
    call_command('seed_skills', stdout=StringIO())

    tree = SkillService.get_tree(user)

    assert Skill.objects.count() == 7
    assert [node['name'] for node in tree] == ['First Steps']
    assert [node['name'] for node in tree[0]['children']] == [
        'Routine Builder',
        'Scholar',
        'Athlete',
    ]
    assert tree[0]['status'] == 'locked'
