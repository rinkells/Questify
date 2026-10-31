from datetime import date, datetime, timezone as datetime_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Sum
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from achievements.models import Achievement, UserAchievement
from bosses.models import Boss, BossDefeat, BossStep
from core.models import Character, StatType
from quests.models import Quest, QuestCompletion
from quests.services import QuestService
from skills.models import Skill, UserSkill
from wallet.models import WalletTransaction

from .selectors import MonthError, monthly_summary
from .serializers import MonthlySummarySerializer
from .views import SummaryRateThrottle


User = get_user_model()
Kind = WalletTransaction.Kind
Status = QuestCompletion.CompletionStatus

# Mid-November 2025 in every zone the tests use.
NOW = datetime(2025, 11, 15, 12, 0, tzinfo=datetime_timezone.utc)
OCTOBER = date(2025, 10, 1)
NOVEMBER = date(2025, 11, 1)
URL = '/api/summary/monthly/'


def local(timezone_name, *args):
    return datetime(*args, tzinfo=ZoneInfo(timezone_name))


def utc(*args):
    return datetime(*args, tzinfo=datetime_timezone.utc)


def make_user(username='summary-user', timezone_name='UTC', joined=utc(2025, 1, 1)):
    user = User.objects.create_user(
        username=username,
        password='password',
        date_joined=joined,
    )
    Character.objects.filter(user=user).update(timezone=timezone_name)
    return user


def credit(user, kind, amount, at):
    """Write a ledger row with a consistent running balance."""
    balance = WalletTransaction.objects.filter(user=user).aggregate(
        total=Sum('amount'),
    )['total'] or 0
    return WalletTransaction.objects.create(
        user=user,
        kind=kind,
        amount=amount,
        balance_after=balance + amount,
        created_at=at,
    )


def make_quest(user, title, quest_type=Quest.QuestType.ONE_TIME):
    return Quest.objects.create(
        user=user,
        title=title,
        quest_type=quest_type,
        xp_reward=50 if quest_type == Quest.QuestType.ONE_TIME else 25,
    )


def midday_timezone(now):
    """A fixed-offset zone where ``now`` falls between noon and 1 p.m.

    Hours from any local midnight, so neither a day nor a month can turn
    during a test that runs on the real clock.
    """
    offset = 12 - now.astimezone(datetime_timezone.utc).hour
    if offset == 0:
        return 'Etc/GMT'
    # POSIX signs: Etc/GMT-3 is three hours ahead of UTC.
    return f'Etc/GMT{-offset:+d}'


def complete(user, at, status=Status.COMPLETED, xp=25, estimate=False):
    return QuestCompletion.objects.create(
        user=user,
        completed_at=at,
        status=status,
        xp_awarded=xp if status == Status.COMPLETED else 0,
        xp_awarded_is_estimate=estimate,
    )


def defeat_boss(user, at, title='Dragon'):
    boss = Boss.objects.create(user=user, title=title)
    return BossDefeat.objects.create(
        boss=boss,
        user=user,
        defeated_at=at,
        xp_awarded=75,
    )


def unlock_achievement(user, name, at):
    achievement = Achievement.objects.create(
        name=name,
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 1},
    )
    unlock = UserAchievement.objects.create(user=user, achievement=achievement)
    # unlocked_at is auto_now_add, so it can only be moved afterwards.
    UserAchievement.objects.filter(pk=unlock.pk).update(unlocked_at=at)
    return achievement


def unlock_skill(user, name, at):
    stat_type, _ = StatType.objects.get_or_create(
        name='WIS',
        defaults={'display_name': 'Wisdom'},
    )
    skill = Skill.objects.create(name=name, category=stat_type)
    unlock = UserSkill.objects.create(user=user, skill=skill)
    UserSkill.objects.filter(pk=unlock.pk).update(unlocked_at=at)
    return skill


@pytest.fixture
def user(db):
    return make_user()


@pytest.fixture
def api_client(user):
    cache.clear()
    client = APIClient()
    client.force_authenticate(user=user)
    yield client
    cache.clear()


def get_summary(client, **params):
    with patch('summary.selectors.timezone.now', return_value=NOW):
        return client.get(URL, params)


# --- Month bounds ------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize('timezone_name', [
    'UTC',
    # Leaves summer time on 2025-10-26: October starts at +03:00, ends at +02:00.
    'Europe/Kyiv',
    'America/Los_Angeles',
    'Pacific/Kiritimati',
    'Asia/Kathmandu',
])
def test_a_month_runs_from_local_midnight_to_local_midnight(timezone_name):
    user = make_user(timezone_name=timezone_name)
    instants = [
        local(timezone_name, 2025, 9, 30, 23, 59),
        local(timezone_name, 2025, 10, 1, 0, 0),
        local(timezone_name, 2025, 10, 31, 23, 59),
        local(timezone_name, 2025, 11, 1, 0, 0),
    ]
    # Distinct powers of ten show exactly which rows a month takes in.
    for index, at in enumerate(instants):
        credit(user, Kind.QUEST_REWARD, 10**index, at)
        complete(user, at)
        complete(user, at, status=Status.FAILED)
        defeat_boss(user, at, title=f'Boss {index}')
        unlock_achievement(user, f'Achievement {index}', at)
        unlock_skill(user, f'Skill {index}', at)

    summary = monthly_summary(user, OCTOBER, now=NOW)

    assert summary.starts_at == instants[1]
    assert summary.ends_at == instants[3]
    assert summary.xp.quests == 110
    assert summary.coins_earned == 110
    assert summary.balance_end == 111
    assert summary.quests_completed == 2
    assert summary.quests_failed == 2
    assert summary.bosses_defeated == 2
    assert summary.achievements_unlocked.names == ('Achievement 1', 'Achievement 2')
    assert summary.skills_unlocked.names == ('Skill 1', 'Skill 2')


@pytest.mark.django_db
def test_bounds_are_reported_in_the_users_offset(api_client, user):
    Character.objects.filter(user=user).update(timezone='Europe/Kyiv')

    response = get_summary(api_client, month='2025-10')

    assert response.status_code == 200
    assert response.data['month'] == '2025-10'
    assert response.data['timezone'] == 'Europe/Kyiv'
    assert response.data['starts_at'] == '2025-10-01T00:00:00+03:00'
    assert response.data['ends_at'] == '2025-11-01T00:00:00+02:00'


@pytest.mark.django_db
def test_utc_bounds_end_in_z(api_client):
    response = get_summary(api_client, month='2025-10')

    assert response.data['starts_at'] == '2025-10-01T00:00:00Z'
    assert response.data['ends_at'] == '2025-11-01T00:00:00Z'


@pytest.mark.django_db
def test_without_a_month_the_current_local_month_is_summarized(api_client, user):
    # 2025-10-31 12:00 UTC is already 02:00 on November 1st in Kiritimati.
    Character.objects.filter(user=user).update(timezone='Pacific/Kiritimati')

    with patch(
        'summary.selectors.timezone.now',
        return_value=utc(2025, 10, 31, 12, 0),
    ):
        response = api_client.get(URL)

    assert response.status_code == 200
    assert response.data['month'] == '2025-11'


# --- Wallet ------------------------------------------------------------------


@pytest.mark.django_db
def test_xp_by_source_adds_up_to_the_coins_earned(user):
    other = make_user('summary-other')
    credit(user, Kind.INITIAL_GRANT, 500, utc(2025, 9, 1))
    credit(user, Kind.QUEST_REWARD, 50, utc(2025, 10, 2))
    credit(user, Kind.QUEST_REWARD, 25, utc(2025, 10, 3))
    credit(user, Kind.DAILY_BONUS, 25, utc(2025, 10, 3))
    credit(user, Kind.BOSS_REWARD, 75, utc(2025, 10, 4))
    credit(user, Kind.REDEMPTION, -30, utc(2025, 10, 5))
    credit(other, Kind.QUEST_REWARD, 1000, utc(2025, 10, 2))

    summary = monthly_summary(user, OCTOBER, now=NOW)

    assert summary.xp.quests == 75
    assert summary.xp.daily_bonus == 25
    assert summary.xp.bosses == 75
    assert summary.xp.total == 175
    assert summary.coins_earned == summary.xp.total
    assert summary.coins_spent == 30
    assert summary.daily_bonuses == 1
    assert summary.balance_end == 500 + 175 - 30


@pytest.mark.django_db
def test_the_initial_grant_is_not_xp_earned_in_its_month(user):
    credit(user, Kind.INITIAL_GRANT, 500, utc(2025, 10, 10))

    summary = monthly_summary(user, OCTOBER, now=NOW)

    assert summary.xp.total == 0
    assert summary.coins_earned == 0
    assert summary.balance_end == 500


@pytest.mark.django_db
def test_xp_stays_in_the_month_of_its_completion(user):
    # The month's very last instant. The credits are written after the
    # completion, the bonus and the defeat, but dated like them.
    last_instant = utc(2025, 10, 31, 23, 59, 59, 999999)
    daily = [
        make_quest(user, f'Daily {index}', Quest.QuestType.DAILY)
        for index in range(3)
    ]
    boss = Boss.objects.create(user=user, title='Dragon')
    steps = [make_quest(user, f'Step {index}') for index in range(3)]
    for quest in steps:
        BossStep.objects.create(boss=boss, quest=quest)

    with patch('quests.services.timezone.now', return_value=last_instant):
        for quest in (*daily, *steps):
            QuestService.complete_quest(quest.id, user)
    october = monthly_summary(user, OCTOBER, now=NOW)
    november = monthly_summary(user, NOVEMBER, now=NOW)

    assert october.quests_completed == 6
    assert october.daily_bonuses == 1
    assert october.bosses_defeated == 1
    assert october.xp.quests == 3 * 25 + 3 * 50
    assert october.xp.daily_bonus == 25
    assert october.xp.bosses == 3 * 25
    assert october.coins_earned == october.xp.total
    assert november.coins_earned == 0


@pytest.mark.django_db
def test_the_balance_at_the_end_of_a_past_month_ignores_later_rows(user):
    credit(user, Kind.QUEST_REWARD, 50, utc(2025, 10, 2))
    credit(user, Kind.QUEST_REWARD, 25, utc(2025, 11, 2))
    credit(user, Kind.REDEMPTION, -60, utc(2025, 11, 3))

    assert monthly_summary(user, OCTOBER, now=NOW).balance_end == 50
    assert monthly_summary(user, NOVEMBER, now=NOW).balance_end == 15


# --- Partial months and the start of the ledger --------------------------------


@pytest.mark.django_db
def test_the_month_of_the_initial_grant_is_partial():
    user = make_user(timezone_name='Europe/Kyiv', joined=utc(2024, 1, 1))
    # Before the wallet: its XP is only in the grant, and one is an estimate.
    complete(user, utc(2025, 10, 3), estimate=True)
    complete(user, utc(2025, 10, 5))
    credit(user, Kind.INITIAL_GRANT, 500, utc(2025, 10, 10))
    complete(user, utc(2025, 10, 12), xp=50)
    credit(user, Kind.QUEST_REWARD, 50, utc(2025, 10, 12))

    october = monthly_summary(user, OCTOBER, now=NOW)
    november = monthly_summary(user, NOVEMBER, now=NOW)

    assert october.partial is True
    assert october.quests_completed == 3
    assert october.estimated_completions == 1
    assert october.xp.total == 50
    assert october.coins_earned == 50
    assert october.balance_end == 550
    assert november.partial is False
    assert november.xp.total == 0
    assert november.balance_end == 550


@pytest.mark.django_db
def test_no_summary_before_the_month_of_the_initial_grant():
    user = make_user(timezone_name='Europe/Kyiv', joined=utc(2024, 1, 1))
    credit(user, Kind.INITIAL_GRANT, 500, utc(2025, 10, 10))

    with pytest.raises(MonthError, match='start at 2025-10'):
        monthly_summary(user, date(2025, 9, 1), now=NOW)


@pytest.mark.django_db
def test_the_initial_grant_month_is_taken_in_the_users_zone():
    # 2025-09-30 22:30 UTC is already October in Kyiv.
    user = make_user(timezone_name='Europe/Kyiv', joined=utc(2024, 1, 1))
    credit(user, Kind.INITIAL_GRANT, 500, utc(2025, 9, 30, 22, 30))

    assert monthly_summary(user, OCTOBER, now=NOW).partial is True
    with pytest.raises(MonthError):
        monthly_summary(user, date(2025, 9, 1), now=NOW)


@pytest.mark.django_db
def test_without_an_initial_grant_the_ledger_starts_with_the_account():
    # Nothing was earned before the wallet, so the first month is whole even
    # though the first row comes weeks after the start of the month.
    user = make_user(joined=utc(2025, 10, 20))
    credit(user, Kind.QUEST_REWARD, 50, utc(2025, 10, 25))

    assert monthly_summary(user, OCTOBER, now=NOW).partial is False
    with pytest.raises(MonthError, match='start at 2025-10'):
        monthly_summary(user, date(2025, 9, 1), now=NOW)


@pytest.mark.django_db
def test_a_new_account_without_any_row_has_its_current_month():
    user = make_user(joined=utc(2025, 11, 14))

    summary = monthly_summary(user, now=NOW)

    assert summary.month == NOVEMBER
    assert summary.partial is False
    assert summary.balance_end == 0


# --- History -----------------------------------------------------------------


@pytest.mark.django_db
def test_failed_quests_are_counted_apart(user):
    complete(user, utc(2025, 10, 2))
    complete(user, utc(2025, 10, 3), status=Status.FAILED)
    complete(user, utc(2025, 10, 4), status=Status.FAILED)

    summary = monthly_summary(user, OCTOBER, now=NOW)

    assert summary.quests_completed == 1
    assert summary.quests_failed == 2
    assert summary.estimated_completions == 0


@pytest.mark.django_db
def test_an_achievement_counts_in_the_month_it_was_unlocked(user):
    # The condition was met in October, but the achievement only appeared in
    # November, and the next completion unlocked it then.
    complete(user, utc(2025, 10, 2))
    unlock_achievement(user, 'Late Bloomer', utc(2025, 11, 3))

    october = monthly_summary(user, OCTOBER, now=NOW)
    november = monthly_summary(user, NOVEMBER, now=NOW)

    assert october.quests_completed == 1
    assert october.achievements_unlocked.count == 0
    assert november.achievements_unlocked.count == 1
    assert november.achievements_unlocked.names == ('Late Bloomer',)


@pytest.mark.django_db
def test_the_summary_follows_real_completions(api_client, user):
    # End to end on the real clock; in a zone where it is midday, neither
    # the day (and its bonus) nor the month can turn during the test.
    Character.objects.filter(user=user).update(
        timezone=midday_timezone(datetime.now(datetime_timezone.utc)),
    )
    quests = [
        Quest.objects.create(
            user=user,
            title=f'Daily {index}',
            quest_type=Quest.QuestType.DAILY,
            xp_reward=25,
        )
        for index in range(3)
    ]
    failed = Quest.objects.create(
        user=user,
        title='Missed',
        quest_type=Quest.QuestType.ONE_TIME,
        xp_reward=50,
    )
    for quest in quests:
        completed = api_client.post(
            f'/api/quests/{quest.id}/complete/',
            format='json',
        )
        assert completed.status_code == 200
    missed = api_client.post(f'/api/quests/{failed.id}/fail/', format='json')
    assert missed.status_code == 201

    response = api_client.get(URL)

    assert response.status_code == 200
    assert response.data['quests_completed'] == 3
    assert response.data['quests_failed'] == 1
    assert response.data['xp'] == {
        'total': 100,
        'quests': 75,
        'daily_bonus': 25,
        'bosses': 0,
    }
    assert response.data['daily_bonuses'] == 1
    assert response.data['coins_earned'] == 100
    assert response.data['balance_end'] == Character.objects.get(user=user).coins
    assert response.data['current_level'] == Character.objects.get(user=user).level


# --- Requests ----------------------------------------------------------------


@pytest.mark.django_db
def test_a_future_month_is_refused(api_client):
    response = get_summary(api_client, month='2025-12')

    assert response.status_code == 400
    assert response.data == {'detail': '2025-12 is in the future.'}


@pytest.mark.django_db
def test_summaries_go_back_24_months(api_client, user):
    # On the instance: the client is authenticated with this very object.
    user.date_joined = utc(2020, 1, 1)
    user.save(update_fields=('date_joined',))

    oldest = get_summary(api_client, month='2023-11')
    too_old = get_summary(api_client, month='2023-10')

    assert oldest.status_code == 200
    assert too_old.status_code == 400
    assert too_old.data == {'detail': 'Summaries go back at most 24 months.'}


@pytest.mark.django_db
def test_a_month_before_the_ledger_is_refused(api_client):
    response = get_summary(api_client, month='2024-12')

    assert response.status_code == 400
    assert response.data == {'detail': 'Summaries start at 2025-01.'}


@pytest.mark.django_db
@pytest.mark.parametrize('month', [
    '2025-13',
    '2025-00',
    '0000-01',
    '2025-1',
    '25-10',
    '2025-10-01',
    '2025/10',
    '2025-10\n',
    '',
    'october',
    # Digits of other scripts, which int() would read.
    '٢٠٢٥-١٠',
    '２０２５-１０',
])
def test_a_malformed_month_is_refused(api_client, month):
    response = get_summary(api_client, month=month)

    assert response.status_code == 400
    assert set(response.data) == {'detail'}


@pytest.mark.django_db
def test_anonymous_requests_are_refused():
    response = APIClient().get(URL)

    assert response.status_code == 401


@pytest.mark.django_db
def test_the_summary_has_a_throttle_of_its_own(api_client):
    with patch.object(
        SummaryRateThrottle,
        'THROTTLE_RATES',
        {'summary': '1/hour'},
    ):
        first = get_summary(api_client)
        second = get_summary(api_client)

    assert first.status_code == 200
    assert second.status_code == 429


# --- Queries and schema --------------------------------------------------------


@pytest.mark.django_db
def test_six_queries_however_much_history(user, django_assert_num_queries):
    credit(user, Kind.INITIAL_GRANT, 500, utc(2025, 9, 1))
    for day in range(1, 6):
        at = utc(2025, 10, day)
        credit(user, Kind.QUEST_REWARD, 25, at)
        credit(user, Kind.DAILY_BONUS, 25, at)
        complete(user, at)
        complete(user, at, status=Status.FAILED)
        defeat_boss(user, at, title=f'Boss {day}')
        unlock_achievement(user, f'Achievement {day}', at)
        unlock_skill(user, f'Skill {day}', at)

    with django_assert_num_queries(6):
        summary = monthly_summary(user, OCTOBER, now=NOW)

    assert summary.achievements_unlocked.count == 5
    assert summary.skills_unlocked.count == 5


@pytest.mark.django_db
def test_the_endpoint_takes_six_queries(api_client, django_assert_num_queries):
    with django_assert_num_queries(6):
        response = get_summary(api_client)

    assert response.status_code == 200


@pytest.mark.django_db
def test_the_response_matches_its_schema(api_client):
    operation = SchemaGenerator().get_schema(public=True)['paths'][URL]['get']
    schema = operation['responses']['200']['content']['application/json']['schema']

    response = get_summary(api_client)

    assert schema == {'$ref': '#/components/schemas/MonthlySummary'}
    assert set(response.data) == set(MonthlySummarySerializer().fields)
    assert set(operation['responses']) == {'200', '400'}
    assert [parameter['name'] for parameter in operation['parameters']] == ['month']
