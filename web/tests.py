import hashlib
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.db import connection
from django.db.models import F
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation
from redis.exceptions import RedisError
from rest_framework.exceptions import ErrorDetail
from rest_framework.test import APIClient

from config.settings import parse_csrf_trusted_origins
from core.checks import check_secure_cookies_have_an_https_redirect
from achievements.models import Achievement, UserAchievement
from bosses.models import Boss, BossDefeat, BossStep
from bosses.services import BossService, BossStepError
from core.errors import DomainError, NotFoundError
from core.models import Character, HashedAPIToken, Stat, StatType
from core.throttling import AtomicUserRateThrottle
from quests.models import (
    Quest,
    QuestCategory,
    QuestCompletion,
    QuestStatReward,
    Streak,
)
from quests.services import QuestAlreadyCompletedError, QuestInputError, QuestService
from skills.models import Skill, SkillRequirement, UserSkill
from skills.services import SkillService
from wallet.models import Reward, RewardRedemption, WalletTransaction
from wallet.selectors import TRANSACTION_PAGE_SIZE
from wallet.services import RedemptionError, RewardService, WalletService

from . import form_tokens
from .middleware import WebContextMiddleware
from .templatetags.web_extras import initials, uk_plural
from .texts import (
    DOMAIN_ERRORS,
    GENERIC_ERROR,
    domain_error_text,
    field_error_text,
    requirement_text,
)
from .toasts import completion_toasts
from .views import csrf_failure

User = get_user_model()

PASSWORD = 'Cobalt-River-Planets-482!'
FORM = 'application/x-www-form-urlencoded'
WEB_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEB_DIR.parent


def post(client, url, **data):
    """A form post; the test client's default multipart is refused here."""
    return client.post(url, urlencode(data), content_type=FORM)


def text(response):
    return response.content.decode()


def is_logged_in(client):
    return '_auth_user_id' in client.session


@pytest.fixture(autouse=True)
def clean_throttle_counters():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def fast_password_hasher(settings):
    # These tests log in constantly; PBKDF2 would make the module take minutes.
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


@pytest.fixture
def user(db):
    return User.objects.create_user(username='Erin', password=PASSWORD)


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


# -- login ---------------------------------------------------------------------


@pytest.mark.django_db
def test_login_page_is_in_ukrainian_with_the_design_assets(client):
    response = client.get('/login/')

    assert response.status_code == 200
    body = text(response)
    assert '<html lang="uk">' in body
    assert 'Вхід' in body
    assert 'web/css/questify.css' in body
    assert 'web/vendor/htmx/htmx.min.js' in body
    assert 'Вийти' not in body
    assert 'csrftoken' in response.cookies


def test_correct_login_opens_a_session_and_goes_home(client, user):
    response = post(client, '/login/', username='Erin', password=PASSWORD)

    assert response.status_code == 302
    assert response['Location'] == '/today/'
    assert int(client.session['_auth_user_id']) == user.pk


@pytest.mark.parametrize(
    ('username', 'password'),
    [('Erin', 'wrong-password'), ('nobody', PASSWORD)],
)
def test_wrong_credentials_get_one_answer_and_no_session(
    client,
    user,
    username,
    password,
):
    response = post(client, '/login/', username=username, password=password)

    assert response.status_code == 200
    body = text(response)
    assert 'Невірне ім’я користувача або пароль.' in body
    assert 'Unable to log in' not in body
    assert f'value="{username}"' in body
    assert password not in body
    assert not is_logged_in(client)


@pytest.mark.parametrize('typed', ['erin', 'ERIN', 'ｅｒｉｎ', ' Erin '])
def test_login_resolves_the_name_as_the_api_does(client, user, typed):
    response = post(client, '/login/', username=typed, password=PASSWORD)

    assert response.status_code == 302
    assert int(client.session['_auth_user_id']) == user.pk


def test_an_inactive_account_cannot_log_in(client, user):
    User.objects.filter(pk=user.pk).update(is_active=False)

    response = post(client, '/login/', username='Erin', password=PASSWORD)

    assert response.status_code == 200
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_empty_fields_are_reported_next_to_them(client):
    body = text(post(client, '/login/', username='', password=''))

    assert body.count('Це поле обов’язкове.') == 2
    assert 'field--error' in body
    assert 'aria-invalid="true"' in body


def test_too_many_logins_answer_429_with_the_real_wait(client, user):
    for _ in range(5):
        assert post(
            client,
            '/login/',
            username='Erin',
            password='wrong',
        ).status_code == 200

    response = post(client, '/login/', username='Erin', password=PASSWORD)

    assert response.status_code == 429
    wait = int(response['Retry-After'])
    assert 0 < wait <= 60
    assert (
        f'Забагато спроб входу. Спробуй знову через {wait} с.' in text(response)
    )
    assert not is_logged_in(client)


def test_web_and_api_logins_share_one_budget(client, user):
    api = APIClient()
    for _ in range(5):
        api.post(
            '/api/auth/login/',
            {'username': 'Erin', 'password': 'wrong'},
            format='json',
        )

    assert post(
        client,
        '/login/',
        username='Erin',
        password=PASSWORD,
    ).status_code == 429


def test_login_fails_closed_when_the_throttle_cache_is_down(client, user):
    with patch(
        'core.throttling.AtomicThrottleMixin._increment_counter',
        side_effect=RedisError,
    ):
        response = post(client, '/login/', username='Erin', password=PASSWORD)

    assert response.status_code == 503
    body = text(response)
    assert 'Сервіс тимчасово недоступний, спробуй пізніше' in body
    assert 'Забагато' not in body
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_registration_fails_closed_when_the_throttle_cache_is_down(client):
    with patch(
        'core.throttling.AtomicThrottleMixin._increment_counter',
        side_effect=RedisError,
    ):
        response = register(client)

    assert response.status_code == 503
    assert 'Сервіс тимчасово недоступний, спробуй пізніше' in text(response)
    assert not User.objects.filter(username='Newcomer').exists()


def test_the_api_still_answers_429_when_the_throttle_cache_is_down(user):
    with patch(
        'core.throttling.AtomicThrottleMixin._increment_counter',
        side_effect=RedisError,
    ):
        response = APIClient().post(
            '/api/auth/login/',
            {'username': 'Erin', 'password': PASSWORD},
            format='json',
        )

    assert response.status_code == 429


# -- next ------------------------------------------------------------------------


@pytest.mark.parametrize('target', ['/quests/', '/skills/?tab=open'])
def test_login_returns_to_a_page_of_this_site(client, user, target):
    page = client.get('/login/', {'next': target})
    assert 'name="next"' in text(page)

    response = post(
        client,
        '/login/',
        username='Erin',
        password=PASSWORD,
        next=target,
    )

    assert response.status_code == 302
    assert response['Location'] == target


@pytest.mark.parametrize(
    'target',
    [
        'https://evil.example/',
        '//evil.example/',
        '/\\evil.example/',
        '///evil.example/',
        'javascript:alert(1)',
        'http:evil.example',
    ],
)
def test_login_never_leaves_the_site(client, user, target):
    page = client.get('/login/', {'next': target})
    assert 'name="next"' not in text(page)

    response = post(
        client,
        '/login/',
        username='Erin',
        password=PASSWORD,
        next=target,
    )

    assert response.status_code == 302
    assert response['Location'] == '/today/'


def test_login_reads_next_from_the_query_string_too(client, user):
    response = client.post(
        '/login/?next=/skills/',
        urlencode({'username': 'Erin', 'password': PASSWORD}),
        content_type=FORM,
    )

    assert response['Location'] == '/skills/'


def test_a_logged_in_user_skips_the_login_page_safely(logged_in):
    assert logged_in.get('/login/')['Location'] == '/today/'
    assert logged_in.get('/login/', {'next': '/skills/'})['Location'] == '/skills/'
    assert logged_in.get('/login/', {'next': '//evil.example/'})['Location'] == '/today/'


# -- CSRF --------------------------------------------------------------------------


def test_login_without_a_csrf_token_is_refused(user):
    strict = Client(enforce_csrf_checks=True)

    response = post(strict, '/login/', username='Erin', password=PASSWORD)

    assert response.status_code == 403
    assert 'Сесія застаріла' in text(response)
    assert not is_logged_in(strict)


def test_login_with_the_pages_csrf_token_works(user):
    strict = Client(enforce_csrf_checks=True)
    page = strict.get('/login/')
    token = re.search(
        r'name="csrfmiddlewaretoken" value="([^"]+)"',
        text(page),
    ).group(1)

    response = post(
        strict,
        '/login/',
        username='Erin',
        password=PASSWORD,
        csrfmiddlewaretoken=token,
    )

    assert response.status_code == 302


def test_a_csrf_failure_on_htmx_is_a_fragment(user):
    strict = Client(enforce_csrf_checks=True)

    response = strict.post(
        '/logout/',
        content_type=FORM,
        headers={'HX-Request': 'true'},
    )

    assert response.status_code == 403
    assert '<html' not in text(response)
    assert 'Сесія застаріла' in text(response)


def test_a_csrf_failure_outside_the_web_keeps_djangos_page():
    response = csrf_failure(RequestFactory().post('/admin/login/'))

    assert response.status_code == 403
    assert 'Сесія застаріла' not in response.content.decode()


def test_the_body_carries_the_csrf_token_for_htmx(logged_in):
    body = text(logged_in.get('/today/'))

    assert re.search(r'''hx-headers='\{"X-CSRFToken": "\w+"\}\'''', body)


# -- logout ------------------------------------------------------------------------


def test_logout_only_accepts_post(logged_in):
    assert logged_in.get('/logout/').status_code == 405
    assert is_logged_in(logged_in)


def test_logout_ends_the_session(logged_in):
    response = logged_in.post('/logout/', content_type=FORM)

    assert response.status_code == 302
    assert response['Location'] == '/login/'
    assert not is_logged_in(logged_in)
    assert logged_in.get('/today/')['Location'] == '/login/?next=/today/'


def test_logout_needs_the_csrf_token(user):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)

    assert strict.post('/logout/', content_type=FORM).status_code == 403
    assert is_logged_in(strict)


# -- anonymous visitors ------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize('path', ['/today/', '/quests/', '/skills/', '/achievements/'])
def test_anonymous_visitors_are_sent_to_login(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert response['Location'] == f'/login/?next={path}'


def test_login_returns_where_the_visitor_was_going(client, user):
    target = client.get('/skills/')['Location']

    response = client.post(
        target,
        urlencode({'username': 'Erin', 'password': PASSWORD}),
        content_type=FORM,
    )

    assert response['Location'] == '/skills/'
    assert client.get(response['Location']).status_code == 200


@pytest.mark.django_db
def test_htmx_is_told_to_redirect_when_the_session_ended(client):
    response = client.get(
        '/skills/',
        headers={
            'HX-Request': 'true',
            'HX-Current-URL': 'http://testserver/quests/?tab=open',
        },
    )

    assert response.status_code == 200
    assert response.content == b''
    assert response['HX-Redirect'] == '/login/?next=/quests/%3Ftab%3Dopen'


@pytest.mark.django_db
def test_htmx_ignores_a_current_url_of_another_site(client):
    response = client.get(
        '/skills/',
        headers={
            'HX-Request': 'true',
            'HX-Current-URL': 'https://evil.example/phish',
        },
    )

    assert response['HX-Redirect'] == '/login/'


def test_a_plain_redirect_stays_one_for_htmx_when_it_is_not_the_login(logged_in):
    response = logged_in.get('/login/', headers={'HX-Request': 'true'})

    assert response.status_code == 302
    assert 'HX-Redirect' not in response


# -- pages -------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_root_leads_to_today(client):
    response = client.get('/')

    assert response.status_code == 302
    assert response['Location'] == '/today/'


def test_today_shows_the_name_and_level(logged_in, user):
    Character.objects.filter(user=user).update(level=7, xp=1000)

    response = logged_in.get('/today/')

    assert response.status_code == 200
    body = text(response)
    assert 'Erin' in body
    assert 'Lv 7' in body
    assert 'ER' in body
    assert 'Вийти' in body


def test_today_writes_the_date_in_ukrainian(logged_in):
    with translation.override('uk'):
        expected = timezone.localtime().strftime('%d').lstrip('0')
    body = text(logged_in.get('/today/'))
    months = (
        'січня', 'лютого', 'березня', 'квітня', 'травня', 'червня', 'липня',
        'серпня', 'вересня', 'жовтня', 'листопада', 'грудня',
    )

    assert any(f'{expected} {month}' in body for month in months)


@pytest.mark.parametrize(
    ('path', 'label'),
    [
        ('/today/', 'Сьогодні'),
        ('/quests/', 'Усі квести'),
        ('/bosses/', 'Боси'),
        ('/skills/', 'Навички'),
        ('/achievements/', 'Ачівки'),
    ],
)
def test_the_navigation_marks_the_current_page(logged_in, path, label):
    body = text(logged_in.get(path))

    assert body.count('aria-current="page"') == 1
    assert re.search(
        rf'nav__link--active" href="{re.escape(path)}" aria-current="page">{label}<',
        body,
    )


@pytest.mark.django_db
def test_user_names_are_escaped(client):
    hostile = User.objects.create_user(
        username='<script>alert(1)</script>',
        password=PASSWORD,
    )
    client.force_login(hostile)

    body = text(client.get('/today/'))

    assert '<script>alert(1)' not in body
    assert '&lt;script&gt;' in body


# -- registration ------------------------------------------------------------------


def register(client, username='Newcomer', password=PASSWORD, tz='Europe/Kyiv'):
    return post(
        client,
        '/register/',
        username=username,
        password=password,
        timezone=tz,
    )


@pytest.mark.django_db
def test_registration_logs_in_and_keeps_the_browsers_timezone(client):
    response = register(client)

    assert response.status_code == 302
    assert response['Location'] == '/today/'
    created = User.objects.get(username='Newcomer')
    assert int(client.session['_auth_user_id']) == created.pk
    assert created.check_password(PASSWORD)
    assert Character.objects.get(user=created).timezone == 'Europe/Kyiv'
    assert 'Ласкаво просимо' in text(client.get('/today/'))


@pytest.mark.django_db
def test_the_old_name_browsers_still_report_for_kyiv_is_kept(client):
    assert register(client, tz='Europe/Kiev').status_code == 302

    character = Character.objects.get(user__username='Newcomer')
    assert character.timezone == 'Europe/Kiev'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'tz',
    ['Mars/Olympus', '', '   ', '../../etc/passwd', 'Europe/Kyiv\x00'],
)
def test_a_bad_browser_timezone_becomes_utc(client, tz):
    assert register(client, tz=tz).status_code == 302

    character = Character.objects.get(user__username='Newcomer')
    assert character.timezone == 'UTC'


def test_a_name_taken_in_another_case_is_reported_next_to_the_field(client, user):
    response = register(client, username='ERIN')

    assert response.status_code == 200
    body = text(response)
    assert 'Це ім’я користувача вже зайняте.' in body
    assert 'id_username_error' in body
    assert 'already exists' not in body
    assert not User.objects.filter(username='ERIN').exists()
    assert not is_logged_in(client)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('password', 'expected'),
    [
        ('Ab1!', 'Пароль закороткий'),
        ('password', 'надто поширений'),
        ('8405729361', 'лише з цифр'),
        ('x' * 129, 'Пароль задовгий.'),
        ('Newcomer1', 'надто схожий'),
    ],
)
def test_the_apis_password_rules_apply_in_ukrainian(client, password, expected):
    response = register(client, password=password)

    assert response.status_code == 200
    assert expected in text(response)
    assert f'value="{password}"' not in text(response)
    assert not User.objects.filter(username='Newcomer').exists()


@pytest.mark.django_db
def test_an_invalid_username_has_its_own_text(client):
    response = register(client, username='no spaces allowed')

    assert 'Ім’я користувача може містити лише' in text(response)


@pytest.mark.django_db
def test_losing_a_registration_race_is_the_same_field_error(client):
    with patch.object(
        User.objects,
        'create_user',
        side_effect=IntegrityError,
    ):
        response = register(client)

    assert response.status_code == 200
    assert 'Це ім’я користувача вже зайняте.' in text(response)
    assert not is_logged_in(client)


@pytest.mark.django_db
def test_too_many_registrations_answer_429(client):
    for index in range(5):
        fresh = Client()
        assert register(fresh, username=f'user{index}').status_code == 302

    response = register(client, username='one-more')

    assert response.status_code == 429
    assert 'Забагато спроб реєстрації.' in text(response)
    assert response['Retry-After'].isdigit()
    assert not User.objects.filter(username='one-more').exists()


def test_a_logged_in_user_skips_the_registration_page(logged_in):
    assert logged_in.get('/register/')['Location'] == '/today/'


def test_the_api_keeps_its_english_registration_errors(user):
    api = APIClient()

    taken = api.post(
        '/api/auth/register/',
        {'username': 'ERIN', 'password': PASSWORD},
        format='json',
    )
    weak = api.post(
        '/api/auth/register/',
        {'username': 'Fresh', 'password': '123'},
        format='json',
    )

    assert taken.status_code == 400
    assert taken.json() == {
        'username': ['A user with that username already exists.'],
    }
    assert weak.status_code == 400
    assert weak.json() == {
        'password': [
            'This password is too short. It must contain at least 8 characters.',
            'This password is too common.',
            'This password is entirely numeric.',
        ],
    }


# -- sessions and tokens are independent -------------------------------------------


def test_a_web_login_does_not_revoke_the_api_token(client, user):
    api = APIClient()
    token = api.post(
        '/api/auth/login/',
        {'username': 'Erin', 'password': PASSWORD},
        format='json',
    ).json()['token']

    assert post(
        client,
        '/login/',
        username='Erin',
        password=PASSWORD,
    ).status_code == 302

    assert HashedAPIToken.objects.filter(user=user).count() == 1
    api.credentials(HTTP_AUTHORIZATION=f'Token {token}')
    assert api.get('/api/character/').status_code == 200


def test_a_token_login_does_not_end_web_sessions(logged_in):
    assert APIClient().post(
        '/api/auth/login/',
        {'username': 'Erin', 'password': PASSWORD},
        format='json',
    ).status_code == 200

    assert logged_in.get('/today/').status_code == 200


def test_an_account_may_hold_several_web_sessions(user):
    first, second = Client(), Client()
    post(first, '/login/', username='Erin', password=PASSWORD)
    post(second, '/login/', username='Erin', password=PASSWORD)

    assert first.get('/today/').status_code == 200
    assert second.get('/today/').status_code == 200
    post(second, '/logout/')
    assert first.get('/today/').status_code == 200


def test_a_web_logout_leaves_the_api_token_alone(logged_in, user):
    _, raw = HashedAPIToken.issue_for_user(user)

    logged_in.post('/logout/', content_type=FORM)

    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f'Token {raw}')
    assert api.get('/api/character/').status_code == 200


def test_the_session_lasts_a_fixed_30_days(client, user):
    response = post(client, '/login/', username='Erin', password=PASSWORD)

    assert response.cookies['sessionid']['max-age'] == 30 * 24 * 60 * 60
    assert response.cookies['sessionid']['httponly']
    assert settings.SESSION_COOKIE_AGE == 30 * 24 * 60 * 60
    assert settings.SESSION_SAVE_EVERY_REQUEST is False


def test_the_api_still_accepts_a_web_session(logged_in):
    assert logged_in.get('/api/character/').status_code == 200


def test_the_token_api_works_as_before(user):
    api = APIClient()

    login = api.post(
        '/api/auth/login/',
        {'username': 'Erin', 'password': PASSWORD},
        format='json',
    )

    assert login.status_code == 200
    assert set(login.json()) == {'token', 'expires_at'}
    api.credentials(HTTP_AUTHORIZATION=f"Token {login.json()['token']}")
    assert api.get('/api/character/').status_code == 200
    assert api.post('/api/auth/logout/').status_code == 204
    assert api.get('/api/character/').status_code == 401


# -- middleware --------------------------------------------------------------------


@pytest.mark.django_db
def test_multipart_is_refused_before_the_csrf_check():
    strict = Client(enforce_csrf_checks=True)

    response = strict.post('/login/', {'username': 'a', 'password': 'b'})

    assert response.status_code == 415


@pytest.mark.django_db
def test_multipart_on_the_api_still_gets_the_apis_answer():
    response = Client().post('/api/auth/login/', {'username': 'a'})

    assert response.status_code == 415
    assert 'detail' in response.json()


@pytest.mark.django_db
def test_web_pages_are_not_stored(client):
    assert 'no-store' in client.get('/login/')['Cache-Control']
    assert 'private' in client.get('/login/')['Cache-Control']
    assert 'no-store' in client.get('/today/')['Cache-Control']


def run_web_context(path, user):
    seen = {}

    def get_response(request):
        seen['zone'] = timezone.get_current_timezone_name()
        seen['language'] = translation.get_language()
        return None

    request = RequestFactory().get(path)
    request.user = user
    WebContextMiddleware(get_response)(request)
    return seen


def test_the_timezone_and_language_are_activated_on_web_urls_only(user):
    Character.objects.filter(user=user).update(timezone='Asia/Tokyo')
    default_language = translation.get_language()

    assert run_web_context('/today/', user) == {'zone': 'Asia/Tokyo', 'language': 'uk'}
    assert run_web_context('/api/character/', user) == {
        'zone': 'UTC',
        'language': default_language,
    }
    # Nothing leaks into the next request of this thread.
    assert timezone.get_current_timezone_name() == 'UTC'
    assert translation.get_language() == default_language


def test_a_broken_stored_timezone_does_not_break_the_page(user):
    Character.objects.filter(user=user).update(timezone='Mars/Olympus')

    assert run_web_context('/today/', user)['zone'] == 'UTC'


def test_htmx_keeps_no_snapshots_and_4xx_is_swapped(logged_in):
    body = text(logged_in.get('/today/'))

    # htmx calls pushState only with history on: off, hx-push-url does nothing.
    assert '"historyEnabled":true' in body
    # Nothing to restore, so Back reloads the page instead of showing a snapshot.
    assert '"historyCacheSize":0' in body
    assert '"refreshOnHistoryMiss":true' in body
    assert '{"code":"4..","swap":true,"error":true}' in body
    assert '{"code":"5..","swap":false,"error":true}' in body


# -- texts -------------------------------------------------------------------------


# Defaults of base classes, which are always raised with a code of their own.
ABSTRACT_CODES = {
    'error',
    'conflict',
    'not_found',
    'redemption_refused',
    'step_refused',
    'skill_unlock_refused',
}
CODE_PATTERNS = (
    re.compile(r"code='([a-z_]+)'"),
    re.compile(r"^\s+code = '([a-z_]+)'", re.MULTILINE),
    re.compile(r"'([a-z_]+_not_found)'"),
)


def test_every_code_the_services_raise_has_a_ukrainian_text():
    raised = set()
    for name in (
        'core/errors.py',
        'core/services.py',
        'quests/services.py',
        'wallet/services.py',
        'bosses/services.py',
        'skills/services.py',
    ):
        source = (PROJECT_DIR / name).read_text(encoding='utf-8')
        for pattern in CODE_PATTERNS:
            raised.update(pattern.findall(source))

    assert raised, 'the patterns found nothing'
    assert raised - ABSTRACT_CODES <= set(DOMAIN_ERRORS)


def test_a_text_is_chosen_by_code_and_never_shows_the_exceptions_english():
    error = QuestAlreadyCompletedError('Quest 12345 has already been completed.')

    assert domain_error_text(error) == 'Цей квест уже виконано.'
    assert '12345' not in domain_error_text(error)


def test_a_text_is_filled_from_the_params():
    error = RedemptionError(
        'Not enough coins: 3/10.',
        code='not_enough_coins',
        balance=3,
        cost=10,
    )

    assert domain_error_text(error) == 'Не вистачає монет: є 3, потрібно 10.'


@pytest.mark.parametrize(
    'error',
    [
        DomainError('secret 42', code='no_such_code'),
        # A text that needs params the raising code did not pass.
        DomainError('secret 42', code='not_enough_coins'),
        # The same when the missing param is the number a word agrees with.
        DomainError('secret 42', code='boss_min_steps'),
    ],
)
def test_an_unknown_code_or_missing_param_gets_the_generic_text(error):
    assert domain_error_text(error) == GENERIC_ERROR


@pytest.mark.parametrize(
    ('number', 'form'),
    [
        (0, 'днів'),
        (1, 'день'),
        (2, 'дні'),
        (4, 'дні'),
        (5, 'днів'),
        (11, 'днів'),
        (12, 'днів'),
        (14, 'днів'),
        (21, 'день'),
        (22, 'дні'),
        (101, 'день'),
        (111, 'днів'),
    ],
)
def test_ukrainian_plurals(number, form):
    assert uk_plural(number, 'день,дні,днів') == form


@pytest.mark.parametrize(
    ('username', 'expected'),
    [('Erin', 'ER'), ('anna_kovalenko', 'AK'), ('anna-maria x', 'AM'), ('я', 'Я')],
)
def test_initials(username, expected):
    assert initials(User(username=username)) == expected


# -- static files and templates ----------------------------------------------------


def test_the_vendored_htmx_is_the_documented_file():
    vendor = WEB_DIR / 'static/web/vendor/htmx'
    content = (vendor / 'htmx.min.js').read_bytes()

    digest = hashlib.sha256(content).hexdigest()
    assert digest in (vendor / 'README.md').read_text(encoding='utf-8')
    assert b'sourceMappingURL' not in content


def test_fonts_are_local_and_every_font_url_exists():
    css_path = WEB_DIR / 'static/web/css/questify.css'
    css = css_path.read_text(encoding='utf-8')

    urls = re.findall(r"url\('([^']+)'\)", css)
    assert len(urls) == 4
    for url in urls:
        assert not url.startswith(('http:', 'https:', '//'))
        assert (css_path.parent / url).resolve().is_file(), url
    for name in ('OFL-Onest.txt', 'OFL-Unbounded.txt'):
        assert (WEB_DIR / 'static/web/fonts' / name).is_file()


def template_sources():
    return [
        path
        for path in (WEB_DIR / 'templates').rglob('*.html')
    ]


def test_templates_load_nothing_from_other_sites():
    assert template_sources()
    for path in template_sources():
        source = path.read_text(encoding='utf-8')
        assert 'googleapis' not in source, path
        assert 'gstatic' not in source, path
        assert not re.search(r'(src|href)="https?://', source), path


def test_templates_have_no_inline_styles_and_no_unescaped_output():
    for path in template_sources():
        source = path.read_text(encoding='utf-8')
        assert ' style=' not in source, path
        assert '<style' not in source, path
        assert '|safe' not in source, path
        assert 'autoescape off' not in source, path


def test_hx_attributes_carry_no_user_data():
    for path in template_sources():
        source = path.read_text(encoding='utf-8')
        for attribute in re.findall(r"""hx-[\w-]+=('[^']*'|"[^"]*")""", source):
            without_token = attribute.replace('{{ csrf_token }}', '')
            assert '{{' not in without_token, (path, attribute)


def test_collectstatic_builds_the_production_manifest(tmp_path):
    production = {
        **settings.STORAGES,
        'staticfiles': {
            'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
        },
    }

    with override_settings(STATIC_ROOT=tmp_path, STORAGES=production):
        call_command('collectstatic', '--noinput', verbosity=0)

    built = list((tmp_path / 'web/css').glob('questify.*.css'))
    assert len(built) == 1
    css = built[0].read_text(encoding='utf-8')
    assert re.search(r'onest-cyrillic\.[0-9a-f]+\.woff2', css)
    assert list((tmp_path / 'web/vendor/htmx').glob('htmx.min.*.js'))


# -- settings and startup checks ---------------------------------------------------


def test_trusted_origins_in_production_are_exact_and_https():
    assert parse_csrf_trusted_origins(
        'https://a.example, https://b.example',
        debug=False,
    ) == ['https://a.example', 'https://b.example']
    assert parse_csrf_trusted_origins('', debug=False) == []
    for value in ('http://a.example', 'https://*.example', '*', 'a.example'):
        with pytest.raises(ImproperlyConfigured):
            parse_csrf_trusted_origins(value, debug=False)


def test_trusted_origins_in_development_may_be_plain_http():
    assert parse_csrf_trusted_origins(
        'http://localhost:8000',
        debug=True,
    ) == ['http://localhost:8000']


@pytest.mark.parametrize(
    ('overrides', 'warns'),
    [
        (
            {
                'DEBUG': False,
                'SECURE_SSL_REDIRECT': False,
                'SESSION_COOKIE_SECURE': True,
            },
            True,
        ),
        (
            {
                'DEBUG': False,
                'SECURE_SSL_REDIRECT': True,
                'SESSION_COOKIE_SECURE': True,
            },
            False,
        ),
        (
            {
                'DEBUG': True,
                'SECURE_SSL_REDIRECT': False,
                'SESSION_COOKIE_SECURE': True,
            },
            False,
        ),
        (
            {
                'DEBUG': False,
                'SECURE_SSL_REDIRECT': False,
                'SESSION_COOKIE_SECURE': False,
                'CSRF_COOKIE_SECURE': False,
            },
            False,
        ),
    ],
)
def test_the_secure_cookie_check(overrides, warns):
    with override_settings(**overrides):
        found = check_secure_cookies_have_an_https_redirect(None)

    assert [item.id for item in found] == (['core.W002'] if warns else [])


# -- the "today" screen ------------------------------------------------------------

DAILY = Quest.QuestType.DAILY
HABIT = Quest.QuestType.HABIT
ONE_TIME = Quest.QuestType.ONE_TIME
XP_REWARDS = {DAILY: 25, HABIT: 25, ONE_TIME: 50}
OOB_IDS = (
    'coins-chip',
    'character-card',
    'daily-bonus',
    'today-summary',
    'one-time-more',
    'today-aside',
)


def make_quest(user, title='Quest', quest_type=ONE_TIME, **fields):
    return Quest.objects.create(
        user=user,
        title=title,
        quest_type=quest_type,
        xp_reward=XP_REWARDS[quest_type],
        **fields,
    )


def htmx_post(client, url):
    return client.post(url, '', content_type=FORM, headers={'HX-Request': 'true'})


def complete(client, quest):
    return htmx_post(client, f'/quests/{quest.pk}/complete/')


def fail(client, quest):
    return htmx_post(client, f'/quests/{quest.pk}/fail/')


def row_html(body):
    """The first top-level block of an action's answer: the row itself."""
    return body[:body.index('hx-swap-oob')]


def test_today_splits_routines_from_one_time_quests(logged_in, user):
    make_quest(user, 'Вчити Python', DAILY)
    make_quest(user, 'Пити воду', HABIT)
    make_quest(user, 'Закінчити лабораторну')

    body = text(logged_in.get('/today/'))

    assert 'Щоденні та звички' in body
    assert 'Разові' in body
    assert body.index('Вчити Python') < body.index('Щоденні та звички') + 2000
    assert body.index('Щоденні та звички') < body.index('Пити воду')
    assert body.index('Пити воду') < body.index('Разові') < body.index('Закінчити')
    assert '+25 XP' in body
    assert '+50 XP' in body
    assert 'Щоденний' in body
    assert 'Звичка' in body
    assert 'Разовий' in body


def test_today_row_shows_its_category_and_stat_rewards(logged_in, user):
    category = QuestCategory.objects.create(user=user, name='Навчання')
    quest = make_quest(user, 'Вчити Python', DAILY, category=category)
    stat_type = StatType.objects.create(name='INT', display_name='Intelligence')
    QuestStatReward.objects.create(quest=quest, stat_type=stat_type, amount=5)

    body = text(logged_in.get('/today/'))

    assert 'Навчання · Щоденний' in body
    assert 'dot--c' in body
    assert '+5 INT' in body


def test_today_row_can_be_completed_or_failed_without_javascript(logged_in, user):
    quest = make_quest(user, 'Вчити Python', DAILY)

    body = text(logged_in.get('/today/'))

    assert f'action="/quests/{quest.pk}/complete/"' in body
    assert f'hx-post="/quests/{quest.pk}/complete/"' in body
    assert f'action="/quests/{quest.pk}/fail/"' in body
    assert 'Не вдалось' in body
    assert 'aria-label="Виконати: Вчити Python"' in body
    assert 'hx-disabled-elt="find button"' in body
    assert 'hx-sync="closest .quest-row:drop"' in body


def test_today_with_no_quests_says_so(logged_in):
    body = text(logged_in.get('/today/'))

    assert 'На сьогодні квестів немає.' in body
    assert 'Щоденні та звички' not in body
    assert 'Разові' not in body


@pytest.mark.parametrize(('count', 'linked'), [(5, False), (6, True)])
def test_all_quests_link_shows_only_when_more_one_time_quests_wait(
    logged_in,
    user,
    count,
    linked,
):
    for index in range(count):
        make_quest(user, f'Once {index}')

    body = text(logged_in.get('/today/'))

    assert ('class="link" href="/quests/">Усі квести<' in body) is linked
    assert body.count('quest-row__title') == min(count, 5)


def test_a_quest_finished_today_stays_on_the_screen_muted(logged_in, user):
    daily = make_quest(user, 'Daily done', DAILY)
    one_time = make_quest(user, 'One-time done')
    failed = make_quest(user, 'One-time failed')
    QuestService.complete_quest(daily.pk, user)
    QuestService.complete_quest(one_time.pk, user)
    QuestService.fail_quest(failed.pk, user)

    body = text(logged_in.get('/today/'))

    assert body.count('quest-row--done') == 2
    assert body.count('quest-row--failed') == 1
    assert 'One-time done' in body
    assert 'One-time failed' in body
    assert 'aria-label="Виконати' not in body
    # The summary counts completions only.
    assert 'виконано 2 з 3' in body


def test_a_one_time_quest_finished_earlier_is_gone(logged_in, user):
    quest = make_quest(user, 'Finished long ago')
    QuestCompletion.objects.create(
        user=user,
        quest=quest,
        status=QuestCompletion.CompletionStatus.COMPLETED,
        completed_at=timezone.now() - timedelta(days=2),
        is_one_time=True,
    )

    assert 'quest-row__title">Finished long ago' not in text(logged_in.get('/today/'))


def test_an_expired_quest_can_only_be_failed(logged_in, user):
    quest = make_quest(user, 'Late', deadline=timezone.now() - timedelta(hours=1))

    body = text(logged_in.get('/today/'))

    assert 'Дедлайн минув' in body
    assert f'/quests/{quest.pk}/complete/' not in body
    assert f'/quests/{quest.pk}/fail/' in body


def hero_html(body):
    return body[body.index('id="character-card"'):body.index('</section>', body.index('id="character-card"'))]


def aside_html(body):
    return body[body.index('id="today-aside"'):body.index('</aside>')]


def header_html(body):
    return body[body.index('<header'):body.index('</header>')]


def coins_shown(body):
    return re.search(r'id="coins-chip".*?Монети: </span><span>(\d+)<', body, re.S).group(1)


def test_coins_are_a_chip_in_the_header_beside_the_avatar(logged_in, user):
    Character.objects.filter(user=user).update(coins=121)

    header = header_html(text(logged_in.get('/today/')))

    assert coins_shown(header) == '121'
    assert '<svg' in header[header.index('id="coins-chip"'):header.index('class="avatar"')]


def test_logout_stays_a_button_in_the_header(logged_in):
    header = header_html(text(logged_in.get('/today/')))

    assert re.search(
        r'<form class="logout" method="post" action="/logout/">.*'
        r'<button class="btn[^"]*" type="submit">Вийти</button>',
        header,
        re.S,
    )


def test_coins_follow_the_user_to_the_other_pages(logged_in, user):
    Character.objects.filter(user=user).update(coins=121)

    assert coins_shown(text(logged_in.get('/skills/'))) == '121'


def test_a_visitor_has_no_coins_chip(client, db):
    assert 'coins-chip' not in text(client.get('/login/'))


def test_the_coins_chip_changes_with_a_completion(logged_in, user):
    Character.objects.filter(user=user).update(coins=121)
    quest = make_quest(user)

    body = text(complete(logged_in, quest))

    assert coins_shown(body) == '171'


def test_the_daily_bonus_is_a_thin_strip_above_the_list(logged_in, user):
    make_quest(user, 'Daily', DAILY)

    body = text(logged_in.get('/today/'))

    assert (
        body.index('id="character-card"')
        < body.index('<h1 class="title">Сьогодні')
        < body.index('id="daily-bonus"')
        < body.index('Щоденні та звички')
    )
    strip = re.search(r'<[a-z]+ class="([^"]*)" id="daily-bonus"', body).group(1)
    assert strip == 'bonus'
    assert '+25 XP' in body[body.index('id="daily-bonus"'):body.index('Щоденні та звички')]


def test_the_section_labels_stay_small_captions(logged_in, user):
    make_quest(user, 'Daily', DAILY)
    make_quest(user, 'Once')

    body = text(logged_in.get('/today/'))

    assert '<h2 class="section__label">Щоденні та звички</h2>' in body
    assert '<h2 class="section__label">Разові</h2>' in body


def test_the_seed_commands_fill_the_catalogs_the_screen_reads(logged_in):
    call_command('seed_skills', verbosity=0)
    call_command('seed_achievements', verbosity=0)

    body = text(logged_in.get('/today/'))

    # What README promises: the catalog's three types, in its order and in Ukrainian.
    hero = hero_html(body)
    assert re.findall(r'tile__label">([^<]+)<', hero) == [
        'Мудрість',
        'Інтелект',
        'Сила',
    ]
    assert not re.search('Wisdom|Intelligence|Strength', hero)
    assert 'achievement-next__name">Переможець босів<' in aside_html(body)


def test_the_seeds_and_the_api_keep_the_stat_types_english_names_and_codes(
    logged_in,
    user,
):
    call_command('seed_skills', verbosity=0)
    wisdom = StatType.objects.get(name='WIS')
    Stat.objects.create(character=user.character, stat_type=wisdom, value=3)

    assert wisdom.display_name == 'Wisdom'
    api = APIClient()
    api.force_authenticate(user)
    assert api.get('/api/character/').json()['stats'] == [
        {'stat_type': 'WIS', 'value': 3},
    ]


def test_an_unknown_stat_type_shows_its_name_from_the_database(logged_in):
    StatType.objects.create(name='LUK', display_name='Luck')

    hero = hero_html(text(logged_in.get('/today/')))

    assert 'tile__label">Luck<' in hero
    assert 'title="Luck"' in hero


def test_a_stat_type_with_no_display_name_falls_back_to_its_code(logged_in):
    StatType.objects.create(name='LUK', display_name='')

    hero = hero_html(text(logged_in.get('/today/')))

    assert 'tile__label">LUK<' in hero


def test_quest_rows_keep_the_stat_codes_as_in_the_reference(logged_in, user):
    wisdom = StatType.objects.create(name='WIS', display_name='Wisdom')
    QuestStatReward.objects.create(quest=make_quest(user), stat_type=wisdom, amount=5)

    body = text(logged_in.get('/today/'))

    assert '+5 WIS' in body
    assert 'tile__label">Мудрість<' in hero_html(body)


def test_the_character_card_holds_the_level_xp_and_stats_only(logged_in, user):
    Character.objects.filter(user=user).update(level=3, xp=300, coins=121)
    Streak.objects.create(
        user=user,
        quest_category=None,
        current_length=6,
        freezes_available=2,
        last_completed_date=timezone.localdate(),
    )
    strength = StatType.objects.create(name='STR', display_name='Сила', icon='dumbbell')
    Stat.objects.create(character=user.character, stat_type=strength, value=42)

    hero = hero_html(text(logged_in.get('/today/')))

    assert 'Lv 3' in hero
    assert 'до рівня 4' in hero
    assert '<progress' in hero
    assert 'tile__value">42<' in hero
    assert 'tile__label">Сила<' in hero
    assert 'Серія' not in hero
    assert '6 днів' not in hero
    assert 'аморо' not in hero
    assert 'онет' not in hero
    assert '121' not in hero


def test_stat_tiles_are_the_catalogs_first_five_in_its_order(logged_in, user):
    names = ['Zeta', 'Alpha', 'Mid', 'Beta', 'Omega', 'Sixth']
    types = [
        StatType.objects.create(name=f'S{index}', display_name=name)
        for index, name in enumerate(names)
    ]
    Stat.objects.create(character=user.character, stat_type=types[1], value=7)

    hero = hero_html(text(logged_in.get('/today/')))

    assert re.findall(r'tile__label">([^<]+)<', hero) == names[:5]
    # A stat of the catalog not earned yet is a real 0.
    assert re.findall(r'tile__value">(\d+)<', hero) == ['0', '7', '0', '0', '0']
    assert 'Sixth' not in hero


def test_an_empty_catalog_shows_no_tiles_and_no_placeholders(logged_in):
    body = text(logged_in.get('/today/'))

    assert not StatType.objects.exists()
    assert 'class="tile' not in body
    assert 'class="tiles' not in body
    assert 'tile__value' not in body


def test_a_stat_icon_follows_the_catalogs_icon_name(logged_in):
    StatType.objects.create(name='A', display_name='Known', icon='heart')
    StatType.objects.create(name='B', display_name='Unknown', icon='no-such-icon')

    hero = hero_html(text(logged_in.get('/today/')))

    assert 'M12 20s-7-4.4' in hero
    assert 'M12 3l2.7 5.6' in hero


def test_the_seeded_stat_types_each_have_a_shape_of_their_own(logged_in):
    call_command('seed_skills', verbosity=0)

    hero = hero_html(text(logged_in.get('/today/')))

    # Wisdom, whose icon is "owl", used to fall through to the star.
    assert 'M4 4l4 2.5h8L20 4' in hero
    assert 'M12 3l2.7 5.6' not in hero


def test_the_side_column_is_exactly_three_cards_in_order(logged_in, user):
    StatType.objects.create(name='STR', display_name='Сила')

    body = text(logged_in.get('/today/'))

    aside = aside_html(body)
    assert re.findall(r'card__label">([^<]+)<', aside) == [
        'Серія',
        'XP за 24 години',
        'Наступна ачівка',
    ]
    assert aside.count('<section') == 3
    assert 'tile' not in aside
    assert 'Останні' not in body
    assert 'Характеристики' not in body


def test_the_streak_card_shows_the_days_and_freezes(logged_in, user):
    Streak.objects.create(
        user=user,
        quest_category=None,
        current_length=6,
        freezes_available=2,
        last_completed_date=timezone.localdate(),
    )

    aside = aside_html(text(logged_in.get('/today/')))

    assert '6 днів' in aside
    assert (
        'Заморозок: 2. Вона рятує серію після пропущеного дня, '
        'нова з’являється кожні 5 рівнів.'
    ) in aside
    # No week of day marks: the flame is the card's only picture. Which day a freeze
    # saved is not stored, so a saved day would look like a missed one.
    streak_card = aside[:aside.index('</section>')]
    assert streak_card.count('<svg') == 1


def test_the_xp_card_shows_what_counts_toward_the_limit(logged_in, user):
    QuestService.complete_quest(make_quest(user, 'Done').pk, user)

    aside = aside_html(text(logged_in.get('/today/')))

    assert '<span>50</span>' in aside
    assert 'з 1000' in aside


def achievement(name, *, hidden=False):
    return Achievement.objects.create(
        name=name,
        description=f'{name} description',
        is_hidden=hidden,
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 100},
    )


def test_the_next_achievement_skips_hidden_and_earned_ones(logged_in, user):
    achievement('Secret', hidden=True)
    earned = achievement('Earned')
    UserAchievement.objects.create(user=user, achievement=earned)
    achievement('Next one')
    achievement('After that')

    body = text(logged_in.get('/today/'))

    assert 'Наступна ачівка' in body
    assert 'achievement-next__name">Next one<' in body
    assert 'Next one description' in body
    assert 'Secret' not in body
    assert 'After that' not in body
    # Evaluators answer only yes or no, so the card has no "17/20" bar.
    assert 'progress--thin' not in aside_html(body)


def test_the_third_card_stays_when_no_achievement_is_next(logged_in, user):
    achievement('Secret', hidden=True)

    aside = aside_html(text(logged_in.get('/today/')))

    assert aside.count('<section') == 3
    assert 'Наступна ачівка' in aside
    assert 'Наразі немає ачівок' in aside
    assert 'Secret' not in aside


def queries_for_today(client):
    with CaptureQueriesContext(connection) as queries:
        assert client.get('/today/').status_code == 200
    return len(queries)


def fill_the_screen(user, prefix, count):
    category = QuestCategory.objects.create(user=user, name=f'{prefix} category')
    stat_type = StatType.objects.create(name=f'{prefix}S', display_name=prefix)
    for index in range(count):
        for quest_type in (DAILY, ONE_TIME):
            quest = make_quest(
                user,
                f'{prefix} {quest_type} {index}',
                quest_type,
                category=category,
            )
            QuestStatReward.objects.create(quest=quest, stat_type=stat_type, amount=1)
    for quest in Quest.objects.filter(user=user, title__startswith=prefix)[:count]:
        QuestService.complete_quest(quest.pk, user)


def test_the_page_takes_as_many_queries_for_many_quests_as_for_few(logged_in, user):
    fill_the_screen(user, 'A', 2)
    few = queries_for_today(logged_in)

    fill_the_screen(user, 'B', 8)
    many = queries_for_today(logged_in)

    assert user.quests.count() == 20
    assert many == few


# -- actions -----------------------------------------------------------------------


def test_completing_answers_with_the_row_and_the_rest_out_of_band(logged_in, user):
    quest = make_quest(user, 'Вчити Python', ONE_TIME)

    response = complete(logged_in, quest)

    assert response.status_code == 200
    body = text(response)
    assert '<html' not in body
    row = row_html(body)
    assert 'quest-row--done' in row
    assert 'Вчити Python' in row
    for element_id in OOB_IDS:
        assert re.search(rf'id="{element_id}"[^>]*hx-swap-oob="true"', body), element_id
    assert 'hx-swap-oob="beforeend:#toasts"' in body
    assert 'toast--xp">+50 XP<' in body
    assert 'toast--coins">+50 монет<' in body
    assert QuestCompletion.objects.filter(quest=quest).count() == 1
    assert Character.objects.get(user=user).xp == 50


def test_out_of_band_blocks_carry_the_new_figures(logged_in, user):
    quest = make_quest(user, 'Вчити Python', DAILY)

    body = text(complete(logged_in, quest))

    assert '25 / 100 XP' in body
    assert 'виконано 1 з 1' in body
    assert 'Виконано 1 з 3' in body
    aside = aside_html(body)
    assert '1 день' in aside
    assert '<span>25</span>' in aside


def test_stats_update_out_of_band_in_the_character_card(logged_in, user):
    wisdom = StatType.objects.create(name='WIS', display_name='Мудрість')
    quest = make_quest(user, 'Читати')
    QuestStatReward.objects.create(quest=quest, stat_type=wisdom, amount=5)

    hero = hero_html(text(complete(logged_in, quest)))

    assert 'tile__value">5<' in hero


def test_completing_takes_three_character_locks(logged_in, user):
    quest = make_quest(user)

    with CaptureQueriesContext(connection) as queries:
        assert complete(logged_in, quest).status_code == 200

    locks = [
        query['sql']
        for query in queries
        if 'FROM "core_character"' in query['sql'] and 'FOR UPDATE' in query['sql']
    ]
    assert len(locks) == 3


def test_the_daily_bonus_comes_with_the_full_set(logged_in, user):
    quests = [make_quest(user, f'Daily {index}', DAILY) for index in range(3)]
    assert 'Виконано 0 з 3' in text(logged_in.get('/today/'))

    complete(logged_in, quests[0])
    second = text(complete(logged_in, quests[1]))
    assert 'Виконано 2 з 3' in second
    assert 'Бонус дня +' not in second

    third = text(complete(logged_in, quests[2]))

    assert 'toast--xp">Бонус дня +25 XP<' in third
    assert 'Отримано сьогодні' in third
    assert Character.objects.get(user=user).xp == 100


def test_too_few_quests_for_the_bonus_is_explained(logged_in, user):
    make_quest(user, 'Daily', DAILY)

    body = text(logged_in.get('/today/'))

    assert 'Виконано 0 з 3' in body
    assert 'щонайменше 3 щоденних' in body


def test_a_failed_quest_rules_the_bonus_out_and_says_so(logged_in, user):
    quests = [make_quest(user, f'Daily {index}', DAILY) for index in range(3)]

    body = text(fail(logged_in, quests[0]))

    assert 'Сьогодні бонус уже недоступний' in body
    assert 'quest-row--failed' in row_html(body)
    assert 'Не вдалось: Daily 0' in body


def test_a_level_up_has_its_toast(logged_in, user):
    Character.objects.filter(user=user).update(xp=90)

    body = text(complete(logged_in, make_quest(user)))

    assert 'toast--level">Новий рівень: 2<' in body
    assert 'Lv 2' in body


def test_an_achievement_has_its_toast_even_when_hidden(logged_in, user):
    Achievement.objects.create(
        name='First quest',
        is_hidden=True,
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 1},
    )

    body = text(complete(logged_in, make_quest(user)))

    assert 'toast--achievement">Ачівка: First quest<' in body


def test_a_boss_step_has_its_toast_and_the_last_one_tells_the_win(logged_in, user):
    boss = BossService.create_boss(user, title='Dragon')
    steps = [make_quest(user, f'Step {index}') for index in range(3)]
    for step in steps:
        BossService.attach_step(user, boss.pk, step.pk)

    first = text(complete(logged_in, steps[0]))
    complete(logged_in, steps[1])
    last = text(complete(logged_in, steps[2]))

    assert 'toast--boss">Бос: −1 HP<' in first
    assert 'Боса переможено' not in first
    assert 'Бос: −1 HP' not in last
    assert 'Боса переможено: +75 XP, +75 монет' in last


@pytest.mark.parametrize(
    ('amount', 'expected'),
    [(1, '+1 монета'), (2, '+2 монети'), (25, '+25 монет'), (21, '+21 монета')],
)
def test_coin_toasts_agree_with_the_number(db, amount, expected):
    toasts = completion_toasts({
        'xp_gained': amount,
        'coins_gained': amount,
        'leveled_up': False,
        'new_level': None,
        'achievements_unlocked': [],
        'daily_bonus': None,
        'boss': None,
    })

    assert [toast.text for toast in toasts] == [f'+{amount} XP', expected]


def assert_row_refusal(response, expected):
    assert response.status_code == 400
    body = text(response)
    assert '<html' not in body
    row = row_html(body)
    assert 'quest-row__alert' in row
    assert expected in row
    assert not re.search(r'\b(Quest|quest) \d+', body)
    assert 'has already' not in body
    assert 'hx-swap-oob' in body


def test_completing_twice_is_refused_next_to_the_row_and_pays_once(logged_in, user):
    quest = make_quest(user, 'Twice', DAILY)
    complete(logged_in, quest)

    response = complete(logged_in, quest)

    assert_row_refusal(response, 'Цей квест уже виконано.')
    assert 'quest-row--done' in row_html(text(response))
    assert QuestCompletion.objects.filter(quest=quest).count() == 1
    assert Character.objects.get(user=user).xp == 25


def test_completing_a_quest_failed_today_is_refused(logged_in, user):
    quest = make_quest(user, 'Failed', DAILY)
    fail(logged_in, quest)

    assert_row_refusal(
        complete(logged_in, quest),
        'Цей квест уже позначено як провалений.',
    )


def test_failing_a_quest_completed_today_is_refused(logged_in, user):
    quest = make_quest(user, 'Done', DAILY)
    complete(logged_in, quest)

    assert_row_refusal(fail(logged_in, quest), 'Цей квест уже виконано.')


def test_a_passed_deadline_is_refused_next_to_the_row(logged_in, user):
    quest = make_quest(user, deadline=timezone.now() - timedelta(minutes=1))

    assert_row_refusal(complete(logged_in, quest), 'Дедлайн цього квесту минув.')


def test_an_inactive_quest_is_refused_next_to_the_row(logged_in, user):
    quest = make_quest(user, is_active=False)

    assert_row_refusal(complete(logged_in, quest), 'Цей квест неактивний.')
    assert_row_refusal(fail(logged_in, quest), 'Цей квест неактивний.')


@override_settings(QUESTIFY_XP_24H_CAP=50)
def test_the_24_hour_limit_is_refused_next_to_the_row(logged_in, user):
    complete(logged_in, make_quest(user, 'First'))

    response = complete(logged_in, make_quest(user, 'Second'))

    assert_row_refusal(response, 'Ліміт XP за останні 24 години вичерпано.')
    assert 'quest-row--done' not in row_html(text(response))


@override_settings(QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER_PER_DAY=1)
def test_the_daily_failure_limit_is_refused_next_to_the_row(logged_in, user):
    fail(logged_in, make_quest(user, 'First'))

    assert_row_refusal(
        fail(logged_in, make_quest(user, 'Second')),
        'Ліміт провалених квестів за день вичерпано.',
    )


@override_settings(QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER=1)
def test_the_lifetime_failure_limit_is_refused_next_to_the_row(logged_in, user):
    fail(logged_in, make_quest(user, 'First'))

    assert_row_refusal(
        fail(logged_in, make_quest(user, 'Second')),
        'Ліміт провалених квестів вичерпано.',
    )


def test_an_error_from_an_event_handler_shows_no_internal_text(logged_in, user):
    from django.core.exceptions import ValidationError

    with patch.object(
        QuestService,
        'complete_for_user',
        side_effect=ValidationError('internal handler text'),
    ):
        response = complete(logged_in, make_quest(user))

    assert response.status_code == 400
    assert GENERIC_ERROR in text(response)
    assert 'internal handler text' not in text(response)


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username='Other', password=PASSWORD)


@pytest.mark.parametrize('action', ['complete', 'fail'])
def test_another_users_quest_is_a_404(logged_in, other_user, action):
    quest = make_quest(other_user, 'Theirs')

    plain = logged_in.post(f'/quests/{quest.pk}/{action}/', '', content_type=FORM)
    fragment = htmx_post(logged_in, f'/quests/{quest.pk}/{action}/')

    assert plain.status_code == 404
    assert fragment.status_code == 404
    assert 'Квест не знайдено.' in text(fragment)
    assert 'Theirs' not in text(fragment)
    assert not QuestCompletion.objects.filter(quest=quest).exists()
    assert Character.objects.get(user=other_user).xp == 0


def test_an_archived_quest_is_a_404_with_its_fragment(logged_in, user):
    quest = make_quest(user)
    QuestService.archive_quest(user, quest.pk)

    response = complete(logged_in, quest)

    assert response.status_code == 404
    assert 'quest-row__alert' in text(response)


@pytest.mark.parametrize('action', ['complete', 'fail'])
def test_actions_only_accept_post(logged_in, user, action):
    quest = make_quest(user)

    assert logged_in.get(f'/quests/{quest.pk}/{action}/').status_code == 405


@pytest.mark.parametrize('action', ['complete', 'fail'])
def test_anonymous_actions_are_sent_to_login(client, user, action):
    quest = make_quest(user)

    plain = client.post(f'/quests/{quest.pk}/{action}/', '', content_type=FORM)
    fragment = client.post(
        f'/quests/{quest.pk}/{action}/',
        '',
        content_type=FORM,
        headers={
            'HX-Request': 'true',
            'HX-Current-URL': 'http://testserver/today/',
        },
    )

    assert plain.status_code == 302
    assert plain['Location'].startswith('/login/?next=')
    assert fragment.status_code == 200
    assert fragment['HX-Redirect'] == '/login/?next=/today/'
    assert not QuestCompletion.objects.filter(quest=quest).exists()


def test_actions_need_the_csrf_token(user):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    quest = make_quest(user)

    response = strict.post(
        f'/quests/{quest.pk}/complete/',
        '',
        content_type=FORM,
        headers={'HX-Request': 'true'},
    )

    assert response.status_code == 403
    assert 'Сесія застаріла' in text(response)
    assert '<html' not in text(response)
    assert not QuestCompletion.objects.filter(quest=quest).exists()


def test_without_javascript_completing_redirects_and_says_what_happened(
    logged_in,
    user,
):
    quest = make_quest(user, 'Plain form')

    response = logged_in.post(f'/quests/{quest.pk}/complete/', '', content_type=FORM)

    assert response.status_code == 302
    assert response['Location'] == '/today/'
    page = text(logged_in.get('/today/'))
    assert '+50 XP' in page
    assert '+50 монет' in page
    assert 'alert--success' in page
    assert 'quest-row--done' in page


def test_without_javascript_a_refusal_redirects_and_shows_the_alert(logged_in, user):
    quest = make_quest(user, deadline=timezone.now() - timedelta(minutes=1))

    response = logged_in.post(f'/quests/{quest.pk}/complete/', '', content_type=FORM)

    assert response.status_code == 302
    page = text(logged_in.get('/today/'))
    assert 'Дедлайн цього квесту минув.' in page
    assert 'alert--error' in page


def test_web_posts_share_the_apis_user_budget_and_reading_is_free(logged_in, user):
    quest = make_quest(user)
    other = make_quest(user, 'Other')
    api = APIClient()
    api.force_authenticate(user=user)

    with patch.object(AtomicUserRateThrottle, 'THROTTLE_RATES', {'user': '1/hour'}):
        for _ in range(3):
            assert logged_in.get('/today/').status_code == 200
        assert complete(logged_in, quest).status_code == 200

        refused = complete(logged_in, other)
        api_refused = api.get('/api/character/')

    assert refused.status_code == 429
    assert refused['Retry-After'].isdigit()
    assert 'Забагато дій. Спробуй знову через' in row_html(text(refused))
    assert not QuestCompletion.objects.filter(quest=other).exists()
    assert api_refused.status_code == 429


def xp_lookalikes(source):
    """Two-letter words reading "XP" that are not plain Latin letters."""
    words = re.findall(r'\b[ХхXx][РрПпPp]\b', source)
    return [word for word in words if not word.isascii()]


def test_xp_lookalikes_are_recognized():
    assert xp_lookalikes('+25 \u0425\u0420') == ['\u0425\u0420']
    assert xp_lookalikes('\u0425P') == ['\u0425P']
    assert xp_lookalikes('X\u0420 X\u041f') == ['X\u0420', 'X\u041f']
    assert xp_lookalikes('+25 XP, xp_gained, \u0445\u0440\u043e\u0431\u0430\u043a') == []


def test_templates_and_texts_spell_xp_in_latin_letters():
    sources = [
        *template_sources(),
        *(path for path in WEB_DIR.glob('*.py') if path.name != 'tests.py'),
        WEB_DIR / 'static/web/js/app.js',
    ]

    for path in sources:
        assert not xp_lookalikes(path.read_text(encoding='utf-8')), path


def test_a_rendered_page_spells_xp_in_latin_letters(logged_in, user):
    make_quest(user)

    body = text(logged_in.get('/today/'))

    assert 'XP' in body
    assert not xp_lookalikes(body)


# -- all quests: the list ----------------------------------------------------------


def titles(body):
    return re.findall(r'quest-row__title">([^<]*)<', body)


def htmx_get(client, url, target='quest-browser'):
    return client.get(url, headers={'HX-Request': 'true', 'HX-Target': target})


def htmx_post_form(client, url, **data):
    return client.post(
        url,
        urlencode(data),
        content_type=FORM,
        headers={'HX-Request': 'true'},
    )


def finish(quest, status=QuestCompletion.CompletionStatus.COMPLETED, **fields):
    return QuestCompletion.objects.create(
        user=quest.user,
        quest=quest,
        status=status,
        is_one_time=quest.quest_type == ONE_TIME
        and status == QuestCompletion.CompletionStatus.COMPLETED,
        **fields,
    )


@pytest.fixture
def stranger(db):
    return User.objects.create_user(username='Stranger', password=PASSWORD)


def test_the_list_shows_the_newest_quests_first(logged_in, user):
    make_quest(user, 'Older')
    make_quest(user, 'Newer', DAILY)

    response = logged_in.get('/quests/')

    assert response.status_code == 200
    body = text(response)
    assert '<h1 class="title">Усі квести</h1>' in body
    assert titles(body) == ['Newer', 'Older']


def test_a_user_with_no_quests_is_told_so(logged_in):
    body = text(logged_in.get('/quests/'))

    assert 'Квестів поки немає' in body
    assert 'Скинути фільтри' not in body


def test_filters_that_match_nothing_offer_to_reset(logged_in, user):
    make_quest(user)

    body = text(logged_in.get('/quests/?type=daily'))

    assert 'За цими фільтрами квестів немає' in body
    assert 'Скинути фільтри' in body


def test_the_status_filter(logged_in, user):
    make_quest(user, 'Open')
    finish(make_quest(user, 'Done'))
    finish(make_quest(user, 'Lost'), QuestCompletion.CompletionStatus.FAILED)
    make_quest(user, 'Hidden', is_active=False, archived_at=timezone.now())
    routine = make_quest(user, 'Routine', DAILY)
    finish(routine, completed_at=timezone.now() - timedelta(days=2))

    assert titles(text(logged_in.get('/quests/'))) == ['Routine', 'Open']
    assert titles(text(logged_in.get('/quests/?status=completed'))) == [
        'Routine',
        'Lost',
        'Done',
    ]
    assert titles(text(logged_in.get('/quests/?status=archive'))) == ['Hidden']


def test_the_type_filter(logged_in, user):
    make_quest(user, 'Once')
    make_quest(user, 'Day', DAILY)
    make_quest(user, 'Habit', HABIT)

    assert titles(text(logged_in.get('/quests/?type=habit'))) == ['Habit']
    assert titles(text(logged_in.get('/quests/?type=one_time'))) == ['Once']


def test_the_category_filter_and_the_uncategorized_chip(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')
    make_quest(user, 'Read', category=study)
    make_quest(user, 'Loose')

    body = text(logged_in.get(f'/quests/?category={study.pk}'))
    assert titles(body) == ['Read']
    assert re.search(
        rf'id="filter-category-{study.pk}" href="[^"]*" aria-current="true"',
        body,
    )
    assert titles(text(logged_in.get('/quests/?category=none'))) == ['Loose']
    assert titles(text(logged_in.get('/quests/'))) == ['Loose', 'Read']


def test_filters_combine(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')
    make_quest(user, 'Match', DAILY, category=study)
    make_quest(user, 'Wrong type', category=study)
    make_quest(user, 'No category', DAILY)
    make_quest(
        user,
        'Archived',
        DAILY,
        category=study,
        is_active=False,
        archived_at=timezone.now(),
    )

    body = text(logged_in.get(f'/quests/?type=daily&category={study.pk}'))

    assert titles(body) == ['Match']


def test_no_category_row_for_a_user_with_no_categories(logged_in, user):
    make_quest(user)

    body = text(logged_in.get('/quests/'))

    assert 'filter-category' not in body
    assert 'filter-type-daily' in body


@pytest.mark.parametrize(
    'query',
    [
        'status=bogus',
        'type=bogus',
        'category=bogus',
        'category=999999999999999999999999999',
        'category=-1',
        'category=',
        'status=active%00',
        'before=bogus',
        'before=999999999999999999999999999',
        'before=-5',
        # Arabic digits pass str.isdigit() and must not reach int().
        'before=%D9%A3',
    ],
)
def test_unknown_filter_values_read_as_no_filter(logged_in, user, query):
    make_quest(user, 'Listed')

    response = logged_in.get(f'/quests/?{query}')

    assert response.status_code == 200
    assert titles(text(response)) == ['Listed']


def test_someone_elses_category_reads_like_one_that_does_not_exist(
    logged_in,
    user,
    stranger,
):
    theirs = QuestCategory.objects.create(user=stranger, name='Secret plans')
    make_quest(stranger, 'Their quest', category=theirs)
    make_quest(user, 'Mine')

    body = text(logged_in.get(f'/quests/?category={theirs.pk}'))
    unknown = text(logged_in.get('/quests/?category=99999999'))

    assert titles(body) == titles(unknown) == ['Mine']
    assert 'Secret plans' not in body
    assert 'Their quest' not in body


def test_filter_links_keep_the_other_filters_and_drop_the_page(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')

    body = text(
        logged_in.get(f'/quests/?status=completed&category={study.pk}&before=500'),
    )

    assert (
        f'id="filter-type-daily" href="/quests/?status=completed&amp;'
        f'type=daily&amp;category={study.pk}"'
    ) in body
    assert f'id="filter-status-archive" href="/quests/?status=archive&amp;category={study.pk}"' in body
    # The default tab and "any" leave the address clean.
    assert f'id="filter-status-active" href="/quests/?category={study.pk}"' in body
    assert 'id="filter-category-any" href="/quests/?status=completed"' in body


def test_a_filter_click_is_a_fragment_that_pushes_the_address(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')
    make_quest(user, 'Read', category=study)
    make_quest(user, 'Loose')
    url = f'/quests/?category={study.pk}'

    fragment = text(htmx_get(logged_in, url))
    page = text(logged_in.get(url))

    assert '<html' not in fragment
    assert 'site-header' not in fragment
    assert fragment.lstrip().startswith(('{#', '<div class="quest-browser"'))
    assert 'id="quest-browser"' in fragment
    # The nav has what htmx needs, and the fragment rebuilds it, chips included.
    assert 'hx-push-url="true"' in fragment
    assert 'hx-boost="true"' in fragment
    assert 'hx-target="#quest-browser"' in fragment
    assert 'aria-current="true"' in fragment
    # A copied link and a reload show exactly what the click showed.
    assert '<html' in page
    assert titles(fragment) == titles(page) == ['Read']


def test_the_address_of_every_filter_opens_with_javascript_off(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')
    make_quest(user, 'Read', category=study)

    page = text(logged_in.get('/quests/'))

    for href in re.findall(r'class="chip chip--link[^>]*href="([^"]+)"', page):
        response = logged_in.get(href.replace('&amp;', '&'))
        assert response.status_code == 200
        assert '<html' in text(response)


def test_the_filter_fragment_of_an_expired_session_redirects_to_this_address(client):
    response = client.get(
        '/quests/?category=3',
        headers={
            'HX-Request': 'true',
            'HX-Current-URL': 'http://testserver/quests/?category=3',
        },
    )

    assert response.status_code == 200
    assert response['HX-Redirect'] == '/login/?next=/quests/%3Fcategory%3D3'


def test_rows_show_the_tags_for_what_they_can_no_longer_do(logged_in, user):
    make_quest(user, 'Off', is_active=False)
    make_quest(user, 'Late', deadline=timezone.now() - timedelta(days=1))
    finish(make_quest(user, 'Lost'), QuestCompletion.CompletionStatus.FAILED)
    finish(make_quest(user, 'Done'))

    active = text(logged_in.get('/quests/'))
    completed = text(logged_in.get('/quests/?status=completed'))

    assert 'Вимкнено' in active
    assert 'Дедлайн минув' in active
    assert 'tag tag--late">Не вдалось' in completed
    assert 'виконано ' in completed


def test_a_row_offers_only_what_the_services_would_accept(logged_in, user):
    quest = make_quest(user, 'Open')
    late = make_quest(user, 'Late', deadline=timezone.now() - timedelta(days=1))
    off = make_quest(user, 'Off', is_active=False)
    done = make_quest(user, 'Done')
    finish(done)

    active = text(logged_in.get('/quests/'))
    completed = text(logged_in.get('/quests/?status=completed'))

    assert f'/quests/{quest.pk}/complete/' in active
    assert f'/quests/{quest.pk}/fail/' in active
    assert f'/quests/{late.pk}/complete/' not in active
    assert f'/quests/{late.pk}/fail/' in active
    assert f'/quests/{off.pk}/complete/' not in active
    assert f'/quests/{off.pk}/fail/' not in active
    assert f'/quests/{off.pk}/edit/' in active
    assert f'/quests/{done.pk}/fail/' not in completed
    assert f'/quests/{done.pk}/edit/' in completed
    assert f'/quests/{done.pk}/archive/' in completed


def test_an_archived_row_offers_nothing(logged_in, user):
    quest = make_quest(
        user,
        'Gone',
        is_active=False,
        archived_at=timezone.now(),
    )

    body = text(logged_in.get('/quests/?status=archive'))

    assert titles(body) == ['Gone']
    assert 'В архіві' in body
    for action in ('complete', 'fail', 'edit', 'archive'):
        assert f'/quests/{quest.pk}/{action}/' not in body
    # The service has no way back, so the screen does not promise one.
    assert 'Відновити' not in body


def test_list_page_takes_the_same_queries_for_any_number_of_rows(logged_in, user):
    stat = StatType.objects.create(name='WIS', display_name='Wisdom')

    def populate(count, offset):
        for index in range(count):
            category = QuestCategory.objects.create(
                user=user,
                name=f'Category {offset + index}',
            )
            quest = make_quest(
                user,
                f'Quest {offset + index}',
                DAILY,
                category=category,
            )
            QuestStatReward.objects.create(quest=quest, stat_type=stat, amount=1)
            finish(quest, completed_at=timezone.now() - timedelta(days=3))

    def queries_for(url, **kwargs):
        with CaptureQueriesContext(connection) as queries:
            response = logged_in.get(url, **kwargs)
        assert response.status_code == 200
        return len(queries)

    populate(2, 0)
    few = queries_for('/quests/')
    few_fragment = queries_for(
        '/quests/',
        headers={'HX-Request': 'true', 'HX-Target': 'quest-browser'},
    )
    populate(30, 100)
    many = queries_for('/quests/')
    many_fragment = queries_for(
        '/quests/',
        headers={'HX-Request': 'true', 'HX-Target': 'quest-browser'},
    )

    # Session, user, the middleware's timezone, categories, the character for the
    # local day, the page, stat rewards; the page adds the header's coins.
    assert few == many == 8
    assert few_fragment == many_fragment == 7


def test_show_more_brings_the_next_rows_and_keeps_the_filters(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')
    quests = [make_quest(user, f'Q{index}', category=study) for index in range(25)]
    newest_first = [quest.title for quest in reversed(quests)]

    first = text(logged_in.get(f'/quests/?category={study.pk}'))

    assert titles(first) == newest_first[:20]
    cursor = quests[5].pk
    more_url = f'/quests/?category={study.pk}&before={cursor}'
    assert f'href="/quests/?category={study.pk}&amp;before={cursor}"' in first
    assert 'hx-target="#quests-more"' in first
    assert 'hx-push-url="false"' in first

    more = text(htmx_get(logged_in, more_url, target='quests-more'))

    assert titles(more) == newest_first[20:]
    assert 'filters' not in more
    assert 'id="quests-more"' not in more
    assert '<html' not in more


def test_show_more_is_absent_on_an_exact_page(logged_in, user):
    for index in range(20):
        make_quest(user, f'Q{index}')

    assert 'quests-more' not in text(logged_in.get('/quests/'))


def test_a_row_leaving_the_tab_does_not_hide_the_next_page(logged_in, user):
    quests = [make_quest(user, f'Q{index}') for index in range(25)]
    first = text(logged_in.get('/quests/'))
    cursor = re.search(r'before=(\d+)', first).group(1)

    QuestService.archive_quest(user, quests[-1].pk)
    more = text(htmx_get(logged_in, f'/quests/?before={cursor}', target='quests-more'))

    # By offset, Q4 would have moved onto the first page and never been shown.
    assert titles(more) == [f'Q{index}' for index in range(4, -1, -1)]


def test_show_more_works_without_javascript(logged_in, user):
    quests = [make_quest(user, f'Q{index}') for index in range(25)]

    body = text(logged_in.get(f'/quests/?before={quests[5].pk}'))

    assert '<html' in body
    assert titles(body) == [f'Q{index}' for index in range(4, -1, -1)]
    assert 'До початку списку' in body


def test_the_list_only_shows_the_users_own_quests(logged_in, user, stranger):
    make_quest(stranger, 'Their quest')
    make_quest(user, 'Mine')

    for url in ('/quests/', '/quests/?status=completed', '/quests/?status=archive'):
        assert 'Their quest' not in text(logged_in.get(url))


def test_the_list_only_answers_reads(logged_in):
    assert post(logged_in, '/quests/').status_code == 405


# -- all quests: creating ----------------------------------------------------------


def in_form(when):
    """A moment as a datetime-local input sends it."""
    return timezone.localtime(when).strftime('%Y-%m-%dT%H:%M')


def future():
    return timezone.now() + timedelta(days=3)


def create(client, **data):
    return post(
        client,
        '/quests/new/',
        **{'title': 'New', 'quest_type': ONE_TIME, 'category': '', 'deadline': '', **data},
    )


def field_error(body, name):
    """The text under one input, or None."""
    found = re.search(rf'id="id_{name}_error">([^<]*)<', body)
    return found and found.group(1)


def test_both_screens_lead_to_the_one_form(logged_in):
    button = r'class="btn btn--primary" href="(/quests/new/[^"]*)"'

    from_today = re.search(button, text(logged_in.get('/today/'))).group(1)
    from_list = re.search(button, text(logged_in.get('/quests/'))).group(1)

    assert from_today.split('?')[0] == from_list.split('?')[0] == '/quests/new/'
    assert 'Новий квест' in text(logged_in.get(from_today))
    assert 'Новий квест' in text(logged_in.get(from_list))


def test_the_form_reads_the_reward_out_and_has_no_field_for_it(logged_in):
    body = text(logged_in.get('/quests/new/'))

    for name in ('title', 'category', 'new_category', 'quest_type', 'deadline'):
        assert f'name="{name}"' in body
    assert 'xp' not in ''.join(re.findall(r'name="([^"]*)"', body)).lower()
    assert re.search(r'Щоденний</span>\s*<span class="tag tag--xp">25 XP<', body)
    assert re.search(r'Звичка</span>\s*<span class="tag tag--xp">25 XP<', body)
    assert re.search(r'Разовий</span>\s*<span class="tag tag--xp">50 XP<', body)
    assert 'is_active' not in body


@pytest.mark.parametrize(
    ('quest_type', 'xp'),
    [(DAILY, 25), (HABIT, 25), (ONE_TIME, 50)],
)
def test_creating_a_quest_sets_the_servers_reward(
    logged_in,
    user,
    quest_type,
    xp,
):
    response = create(logged_in, quest_type=quest_type, xp_reward='999')

    assert response.status_code == 302
    assert response['Location'] == '/quests/'
    assert Quest.objects.get(user=user).xp_reward == xp


@pytest.mark.parametrize('blank', ['', '   '])
def test_a_blank_deadline_and_category_are_none(logged_in, user, blank):
    response = create(logged_in, deadline=blank, category=blank)

    assert response.status_code == 302
    quest = Quest.objects.get(user=user)
    assert quest.deadline is None
    assert quest.category is None


def test_the_form_reads_a_deadline_in_the_characters_timezone(logged_in, user):
    Character.objects.filter(user=user).update(timezone='Asia/Tokyo')

    create(logged_in, deadline='2030-01-15T09:00')

    quest = Quest.objects.get(user=user)
    assert quest.deadline.isoformat() == '2030-01-15T00:00:00+00:00'
    body = text(logged_in.get(f'/quests/{quest.pk}/edit/'))
    assert 'value="2030-01-15T09:00"' in body


def test_creating_returns_where_the_form_was_opened_from(logged_in):
    page = text(logged_in.get('/quests/new/?next=/today/'))

    assert '<input type="hidden" name="next" value="/today/">' in page
    response = create(logged_in, next='/today/')
    assert response['Location'] == '/today/'


@pytest.mark.parametrize(
    'target',
    ['https://evil.example/', '//evil.example/', 'javascript:alert(1)'],
)
def test_the_form_never_leaves_the_site(logged_in, target):
    assert create(logged_in, next=target)['Location'] == '/quests/'
    page = text(logged_in.get(f'/quests/new/?{urlencode({"next": target})}'))
    assert 'evil.example' not in page
    assert 'javascript:' not in page


def test_a_new_quest_is_announced_and_escaped(logged_in, user):
    response = post(
        logged_in,
        '/quests/new/',
        title='<script>alert(1)</script>',
        quest_type=ONE_TIME,
    )

    body = text(logged_in.get(response['Location']))
    assert 'створено' in body
    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;' in body


@pytest.mark.parametrize(
    ('data', 'field', 'expected'),
    [
        ({'title': ''}, 'title', 'Це поле обов’язкове.'),
        ({'title': '   '}, 'title', 'Це поле обов’язкове.'),
        ({'title': 'x' * 256}, 'title', 'Назва задовга (до 255 символів).'),
        ({'deadline': 'nonsense'}, 'deadline', 'Вкажи дату й час дедлайну.'),
        (
            {'deadline': '1999-01-01T10:00'},
            'deadline',
            'Дедлайн має бути між 2000 і 2100 роками.',
        ),
        (
            {'deadline': '2101-01-01T10:00'},
            'deadline',
            'Дедлайн має бути між 2000 і 2100 роками.',
        ),
        (
            {'deadline': '2001-01-01T10:00'},
            'deadline',
            'Дедлайн має бути в майбутньому.',
        ),
        ({'category': '999999'}, 'category', 'Цієї категорії вже не існує.'),
        ({'category': 'abc'}, 'category', 'Обери категорію зі списку.'),
        ({'new_category': 'x' * 101}, 'new_category', 'Назва категорії задовга (до 100 символів).'),
    ],
)
def test_validation_errors_stand_next_to_their_field_in_ukrainian(
    logged_in,
    user,
    data,
    field,
    expected,
):
    response = create(logged_in, **data)

    assert response.status_code == 200
    body = text(response)
    assert field_error(body, field) == expected
    assert 'aria-invalid="true"' in body
    assert not Quest.objects.filter(user=user).exists()
    assert not QuestCategory.objects.filter(user=user).exists()


@pytest.mark.parametrize('value', ['bogus', ''])
def test_a_quest_type_outside_the_list_is_reported_by_its_code(logged_in, value):
    body = text(create(logged_in, quest_type=value))

    assert 'Обери тип квесту зі списку.' in body
    assert 'is not a valid choice' not in body


def test_a_missing_quest_type_is_reported(logged_in):
    body = text(post(logged_in, '/quests/new/', title='New'))

    assert 'Обери тип квесту.' in body


def test_a_time_the_timezone_refuses_has_a_text_of_its_own():
    # DRF's code for it; its text would show the zone's name in English.
    detail = ErrorDetail('Invalid datetime for the timezone "X".', code='make_aware')

    assert field_error_text('deadline', detail) == (
        'Цей час не підходить для твого часового поясу: обери інший.'
    )


def test_a_time_a_clock_change_touches_is_accepted_as_the_api_accepts_it(logged_in, user):
    Character.objects.filter(user=user).update(timezone='Europe/Kyiv')

    # 03:30 does not exist that night; it is the same instant as 04:30.
    response = create(logged_in, deadline='2030-03-31T03:30')

    assert response.status_code == 302
    quest = Quest.objects.get(user=user)
    assert quest.deadline.isoformat() == '2030-03-31T01:30:00+00:00'


def test_someone_elses_category_is_refused_without_showing_its_id(
    logged_in,
    user,
    stranger,
):
    theirs = QuestCategory.objects.create(user=stranger, name='Secret plans')

    body = text(create(logged_in, category=str(theirs.pk)))

    assert field_error(body, 'category') == 'Цієї категорії вже не існує.'
    assert 'Invalid pk' not in body
    assert f'object does not exist' not in body
    assert 'Secret plans' not in body
    assert not Quest.objects.filter(user=user).exists()


def test_a_refused_form_keeps_what_was_typed(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')

    body = text(
        create(
            logged_in,
            title='',
            category=str(study.pk),
            new_category='Fresh',
            quest_type=DAILY,
            deadline='2030-05-05T10:30',
        ),
    )

    assert f'<option value="{study.pk}" selected>Study</option>' in body
    assert 'value="Fresh"' in body
    assert 'value="2030-05-05T10:30"' in body
    assert re.search(r'value="daily" checked', body)
    assert not re.search(r'value="one_time" checked', body)


def test_the_quest_quota_is_the_services_and_says_so(logged_in, user, settings):
    settings.QUESTIFY_MAX_QUESTS_PER_USER = 1
    make_quest(user, 'First', is_active=False, archived_at=timezone.now())

    response = create(logged_in)

    assert response.status_code == 200
    body = text(response)
    assert 'Досягнуто ліміту квестів для цього акаунта.' in body
    assert 'Quest limit' not in body
    assert Quest.objects.filter(user=user).count() == 1
    # Stated right above the button it refused, with what was typed still there.
    assert body.index('Досягнуто ліміту квестів') < body.index('Створити квест')
    assert 'value="New"' in body


def test_the_category_quota_stands_next_to_the_new_category(
    logged_in,
    user,
    settings,
):
    settings.QUESTIFY_MAX_CATEGORIES_PER_USER = 1
    QuestCategory.objects.create(user=user, name='Study')

    body = text(create(logged_in, new_category='Fresh'))

    assert field_error(body, 'new_category') == (
        'Досягнуто ліміту категорій для цього акаунта.'
    )
    assert not Quest.objects.filter(user=user).exists()
    assert QuestCategory.objects.filter(user=user).count() == 1


def test_a_refused_quest_leaves_no_new_category_behind(logged_in, user, settings):
    settings.QUESTIFY_MAX_QUESTS_PER_USER = 1
    make_quest(user, 'First')

    create(logged_in, new_category='Fresh')

    assert not QuestCategory.objects.filter(user=user).exists()


def test_a_new_category_is_created_and_used(logged_in, user):
    existing = QuestCategory.objects.create(user=user, name='Study')

    create(logged_in, new_category='  Fresh  ', category=str(existing.pk))

    quest = Quest.objects.get(user=user)
    # Trimmed by the API's serializer; the typed name wins over the list.
    assert quest.category.name == 'Fresh'
    assert quest.category != existing


@pytest.mark.parametrize('typed', ['Study', ' study ', 'STUDY'])
def test_a_name_the_user_already_has_is_not_a_second_category(logged_in, user, typed):
    study = QuestCategory.objects.create(user=user, name='Study')

    create(logged_in, new_category=typed)

    assert Quest.objects.get(user=user).category == study
    assert QuestCategory.objects.filter(user=user).count() == 1


@pytest.mark.parametrize('typed', ['Здоров’я', 'Здоровʼя', "ЗДОРОВ'Я"])
def test_a_name_differing_only_by_its_apostrophe_is_not_a_second_category(
    logged_in,
    user,
    typed,
):
    health = QuestCategory.objects.create(user=user, name="Здоров'я")

    response = create(logged_in, new_category=typed)

    assert response.status_code == 302
    assert Quest.objects.get(user=user).category == health
    assert QuestCategory.objects.filter(user=user).count() == 1


def test_a_taken_category_name_has_a_ukrainian_text():
    refusal = QuestInputError(
        'You already have a category with this name.',
        code='category_exists',
        field='name',
    )

    assert domain_error_text(refusal) == 'У тебе вже є категорія з такою назвою.'


def test_a_blank_new_category_means_none(logged_in, user):
    create(logged_in, new_category='   ')

    assert Quest.objects.get(user=user).category is None
    assert not QuestCategory.objects.exists()


def test_forms_count_against_the_users_budget(logged_in, user):
    with patch('web.views.user_budget_wait', return_value=42):
        response = create(logged_in, title='Typed')

    assert response.status_code == 429
    assert response['Retry-After'] == '42'
    body = text(response)
    assert 'Забагато дій. Спробуй знову через 42 с.' in body
    assert 'value="Typed"' in body
    assert not Quest.objects.filter(user=user).exists()


# -- all quests: editing -----------------------------------------------------------


def edit(client, quest, pairs=None, **data):
    """Post the edit form; ``pairs`` for a name that comes twice."""
    fields = {
        'title': quest.title,
        'quest_type': quest.quest_type,
        'category': quest.category_id or '',
        'deadline': in_form(quest.deadline) if quest.deadline else '',
        **data,
    }
    return client.post(
        f'/quests/{quest.pk}/edit/',
        urlencode([*fields.items(), *(pairs or [])]),
        content_type=FORM,
    )


def test_the_edit_form_shows_the_quest(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')
    when = future()
    quest = make_quest(user, 'Read', DAILY, category=study, deadline=when)

    body = text(logged_in.get(f'/quests/{quest.pk}/edit/?next=/quests/?type=daily'))

    assert 'Редагування квесту' in body
    assert 'value="Read"' in body
    assert f'<option value="{study.pk}" selected>Study</option>' in body
    assert re.search(r'value="daily" checked', body)
    assert f'value="{in_form(when)}"' in body
    assert 'name="next" value="/quests/?type=daily"' in body
    assert 'href="/quests/?type=daily">Скасувати' in body


def test_editing_saves_through_the_service_and_returns_to_the_list(logged_in, user):
    quest = make_quest(user, 'Old')

    response = edit(logged_in, quest, title='New name', next='/quests/?type=daily')

    assert response.status_code == 302
    assert response['Location'] == '/quests/?type=daily'
    quest.refresh_from_db()
    assert quest.title == 'New name'
    assert 'збережено' in text(logged_in.get(response['Location']))


def test_editing_leaves_what_the_form_does_not_hold(logged_in, user):
    quest = make_quest(user, 'Old', description='Written through the API')

    edit(logged_in, quest, title='New')

    quest.refresh_from_db()
    assert quest.description == 'Written through the API'


def test_the_reward_follows_the_type_and_cannot_be_posted(logged_in, user):
    quest = make_quest(user, 'Old', ONE_TIME)

    edit(logged_in, quest, quest_type=DAILY, xp_reward='999')

    quest.refresh_from_db()
    assert quest.quest_type == DAILY
    assert quest.xp_reward == 25


def test_the_form_reads_out_the_reward_the_quest_actually_pays(logged_in, user):
    quest = make_quest(user, 'Legacy', ONE_TIME)
    Quest.objects.filter(pk=quest.pk).update(xp_reward=10)

    body = text(logged_in.get(f'/quests/{quest.pk}/edit/'))

    assert re.search(r'Разовий</span>\s*<span class="tag tag--xp">10 XP<', body)
    assert re.search(r'Щоденний</span>\s*<span class="tag tag--xp">25 XP<', body)


def test_a_type_with_a_completion_is_fixed_on_the_form_and_in_the_service(
    logged_in,
    user,
):
    quest = make_quest(user, 'Done', DAILY)
    finish(quest)

    page = text(logged_in.get(f'/quests/{quest.pk}/edit/'))
    assert 'name="quest_type"' not in page
    assert 'Тип не можна змінити після першого виконання.' in page

    # Without the input the form still saves; a forged one is refused by the service.
    without = logged_in.post(
        f'/quests/{quest.pk}/edit/',
        urlencode({'title': 'Renamed', 'category': '', 'deadline': ''}),
        content_type=FORM,
    )
    assert without.status_code == 302
    forged = text(edit(logged_in, quest, quest_type=ONE_TIME))
    assert 'Тип квесту не можна змінити після першого виконання.' in forged
    assert 'Quest type cannot' not in forged
    quest.refresh_from_db()
    assert quest.quest_type == DAILY


def test_a_new_category_on_a_refused_edit_goes_back_with_it(logged_in, user):
    quest = make_quest(user, 'Done', DAILY)
    finish(quest)

    edit(logged_in, quest, quest_type=ONE_TIME, new_category='Fresh')

    assert not QuestCategory.objects.filter(user=user).exists()


def test_an_edit_can_move_a_quest_to_a_new_category_or_out_of_one(logged_in, user):
    study = QuestCategory.objects.create(user=user, name='Study')
    quest = make_quest(user, 'Read', category=study)

    edit(logged_in, quest, new_category='Fresh')
    quest.refresh_from_db()
    assert quest.category.name == 'Fresh'

    edit(logged_in, quest, category='')
    quest.refresh_from_db()
    assert quest.category is None


def test_an_untouched_deadline_does_not_reach_the_service(logged_in, user):
    # The input holds minutes; the stored value has seconds an API client set.
    when = (future() + timedelta(minutes=1)).replace(second=37, microsecond=123)
    quest = make_quest(user, 'Read', deadline=when)

    with patch(
        'web.forms.QuestService.update_quest',
        wraps=QuestService.update_quest,
    ) as update:
        response = edit(logged_in, quest, title='Renamed')

    assert response.status_code == 302
    assert 'deadline' not in update.call_args.kwargs
    quest.refresh_from_db()
    assert quest.deadline == when


def test_an_untouched_empty_deadline_does_not_reach_the_service(logged_in, user):
    quest = make_quest(user, 'Read')

    with patch(
        'web.forms.QuestService.update_quest',
        wraps=QuestService.update_quest,
    ) as update:
        edit(logged_in, quest, title='Renamed', deadline='')

    assert 'deadline' not in update.call_args.kwargs


def test_a_changed_deadline_reaches_the_service(logged_in, user):
    quest = make_quest(user, 'Read', deadline=future())
    new = future() + timedelta(days=1)

    with patch(
        'web.forms.QuestService.update_quest',
        wraps=QuestService.update_quest,
    ) as update:
        edit(logged_in, quest, deadline=in_form(new))

    assert 'deadline' in update.call_args.kwargs
    quest.refresh_from_db()
    assert in_form(quest.deadline) == in_form(new)


def test_clearing_a_deadline_sends_none(logged_in, user):
    quest = make_quest(user, 'Read', deadline=future())

    edit(logged_in, quest, deadline='')

    quest.refresh_from_db()
    assert quest.deadline is None


def test_a_quest_past_its_deadline_can_still_be_renamed(logged_in, user):
    when = (timezone.now() - timedelta(days=1)).replace(second=13)
    quest = make_quest(user, 'Late', deadline=when)

    page = text(logged_in.get(f'/quests/{quest.pk}/edit/'))
    assert re.search(r'name="deadline"[^>]*readonly', page)
    assert 'Дедлайн минув, його не можна змінити.' in page

    response = edit(logged_in, quest, title='Renamed')

    assert response.status_code == 302
    quest.refresh_from_db()
    assert quest.title == 'Renamed'
    assert quest.deadline == when


@pytest.mark.parametrize(
    ('deadline', 'expected'),
    [
        ('', 'Дедлайн минув, його не можна змінити.'),
        ('next-week', 'Дедлайн минув, його не можна змінити.'),
        ('2001-01-01T10:00', 'Дедлайн має бути в майбутньому.'),
    ],
)
def test_a_passed_deadline_cannot_be_moved_or_cleared(
    logged_in,
    user,
    deadline,
    expected,
):
    when = timezone.now() - timedelta(days=1)
    quest = make_quest(user, 'Late', deadline=when)
    if deadline == 'next-week':
        deadline = in_form(future())

    body = text(edit(logged_in, quest, title='Renamed', deadline=deadline))

    assert field_error(body, 'deadline') == expected
    assert 'has passed' not in body
    quest.refresh_from_db()
    assert quest.deadline == when
    assert quest.title == 'Late'


def test_the_active_checkbox_comes_after_its_hidden_value(logged_in, user):
    quest = make_quest(user, 'Read')

    body = text(logged_in.get(f'/quests/{quest.pk}/edit/'))

    hidden = body.index('<input type="hidden" name="is_active" value="false">')
    assert hidden < re.search(r'type="checkbox" name="is_active" value="true"', body).start()


def test_an_unchecked_box_turns_the_quest_off(logged_in, user):
    quest = make_quest(user, 'Read')

    edit(logged_in, quest, pairs=[('is_active', 'false')])

    quest.refresh_from_db()
    assert quest.is_active is False


def test_a_checked_box_turns_the_quest_back_on(logged_in, user):
    quest = make_quest(user, 'Read', is_active=False)

    edit(logged_in, quest, pairs=[('is_active', 'false'), ('is_active', 'true')])

    quest.refresh_from_db()
    assert quest.is_active is True


@pytest.mark.parametrize(
    ('pairs', 'checked'),
    [
        ([('is_active', 'false')], False),
        ([('is_active', 'false'), ('is_active', 'true')], True),
    ],
)
def test_a_refused_form_keeps_the_box_as_it_was_left(logged_in, user, pairs, checked):
    quest = make_quest(user, 'Read')

    body = text(edit(logged_in, quest, pairs=pairs, title=''))

    box = re.search(r'<input[^>]*type="checkbox"[^>]*>', body).group(0)
    assert ('checked' in box) is checked


def test_a_turned_off_quest_stays_where_it_can_be_turned_back_on(logged_in, user):
    quest = make_quest(user, 'Read')

    edit(logged_in, quest, pairs=[('is_active', 'false')])

    assert 'Read' in titles(text(logged_in.get('/quests/')))
    assert 'Read' not in text(logged_in.get('/today/'))


def test_edit_and_archive_are_404_for_someone_elses_quest(logged_in, stranger):
    quest = make_quest(stranger, 'Their quest')
    url = f'/quests/{quest.pk}'

    assert logged_in.get(f'{url}/edit/').status_code == 404
    assert edit(logged_in, quest, title='Stolen').status_code == 404
    assert post(logged_in, f'{url}/archive/').status_code == 404
    assert post(logged_in, f'{url}/complete/', list_url='/quests/').status_code == 404
    assert post(logged_in, f'{url}/fail/', list_url='/quests/').status_code == 404
    quest.refresh_from_db()
    assert quest.title == 'Their quest'
    assert quest.archived_at is None
    assert not quest.completions.exists()


@pytest.mark.parametrize('action', ['archive', 'complete', 'fail'])
def test_htmx_is_told_a_foreign_quest_does_not_exist(logged_in, stranger, action):
    quest = make_quest(stranger, 'Their quest')

    response = htmx_post_form(
        logged_in,
        f'/quests/{quest.pk}/{action}/',
        list_url='/quests/',
    )

    assert response.status_code == 404
    body = text(response)
    assert 'Квест не знайдено.' in body
    assert 'Their quest' not in body
    assert str(quest.pk) not in body


def test_an_archived_quest_cannot_be_edited(logged_in, user):
    quest = make_quest(user, 'Gone', is_active=False, archived_at=timezone.now())

    assert logged_in.get(f'/quests/{quest.pk}/edit/').status_code == 404
    assert edit(logged_in, quest, title='Back').status_code == 404


def test_an_edit_form_left_open_while_the_quest_was_archived_is_a_404(logged_in, user):
    quest = make_quest(user, 'Read')

    with patch(
        'web.views.quests_for_user',
        return_value=Quest.objects.filter(pk=quest.pk),
    ):
        QuestService.archive_quest(user, quest.pk)
        response = edit(logged_in, quest, title='Back')

    assert response.status_code == 404


@pytest.mark.parametrize('action', ['edit', 'archive'])
def test_an_id_past_what_the_database_holds_is_a_404(logged_in, action):
    url = f'/quests/{10 ** 30}/{action}/'

    assert post(logged_in, url).status_code == 404


@pytest.mark.parametrize('path', ['/quests/new/', '/quests/1/edit/'])
def test_the_forms_need_a_login(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert response['Location'] == f'/login/?next={path}'


def test_the_forms_need_the_csrf_token(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    quest = make_quest(user)

    assert create(client).status_code == 403
    assert edit(client, quest, title='New').status_code == 403
    assert post(client, f'/quests/{quest.pk}/archive/').status_code == 403
    assert not Quest.objects.filter(title__in=('New',)).exists()


# -- all quests: archiving and the other actions of a row --------------------------


LIST_URL = '/quests/?type=one_time'


def test_archiving_goes_through_a_post_and_keeps_the_row_in_place(logged_in, user):
    quest = make_quest(user, 'Read')

    assert logged_in.get(f'/quests/{quest.pk}/archive/').status_code == 405
    response = htmx_post_form(
        logged_in,
        f'/quests/{quest.pk}/archive/',
        list_url=LIST_URL,
    )

    assert response.status_code == 200
    body = text(response)
    assert titles(body) == ['Read']
    assert 'В архіві' in body
    assert f'/quests/{quest.pk}/' not in body
    assert 'toast' in body
    quest.refresh_from_db()
    assert quest.archived_at is not None
    assert quest.is_active is False
    assert titles(text(logged_in.get('/quests/'))) == []
    assert titles(text(logged_in.get('/quests/?status=archive'))) == ['Read']


def test_archiving_asks_first_since_there_is_no_way_back(logged_in, user):
    make_quest(user, 'Read')

    body = text(logged_in.get('/quests/'))

    assert 'hx-confirm="Перенести квест в архів? Повернути його звідти не можна."' in body


def test_archiving_without_javascript_returns_to_the_same_filters(logged_in, user):
    quest = make_quest(user, 'Read')

    response = post(logged_in, f'/quests/{quest.pk}/archive/', list_url=LIST_URL)

    assert response.status_code == 302
    assert response['Location'] == LIST_URL
    assert 'В архіві: Read' in text(logged_in.get(LIST_URL))


@pytest.mark.parametrize(
    'target',
    [
        'https://evil.example/',
        '//evil.example/',
        '/today/',
        '/admin/',
        '/quests/../admin/',
        '/quests/new/',
    ],
)
def test_a_row_only_ever_returns_to_the_list(logged_in, user, target):
    quest = make_quest(user, 'Read')

    response = post(logged_in, f'/quests/{quest.pk}/archive/', list_url=target)

    assert response['Location'] == '/quests/'


def test_an_archived_quest_still_counts_toward_the_quota(logged_in, user, settings):
    settings.QUESTIFY_MAX_QUESTS_PER_USER = 1
    quest = make_quest(user, 'Read')
    post(logged_in, f'/quests/{quest.pk}/archive/')

    assert 'Досягнуто ліміту квестів' in text(create(logged_in))


def test_archiving_twice_answers_that_the_quest_is_gone(logged_in, user):
    quest = make_quest(user, 'Read')
    url = f'/quests/{quest.pk}/archive/'

    htmx_post_form(logged_in, url, list_url=LIST_URL)
    response = htmx_post_form(logged_in, url, list_url=LIST_URL)

    assert response.status_code == 404
    assert 'Квест не знайдено.' in text(response)


def test_a_row_of_the_list_answers_for_the_list_not_for_today(logged_in, user):
    quest = make_quest(user, 'Read')

    response = htmx_post_form(
        logged_in,
        f'/quests/{quest.pk}/complete/',
        list_url=LIST_URL,
    )

    assert response.status_code == 200
    body = text(response)
    assert 'id="coins-chip"' in body
    for name in OOB_IDS:
        if name != 'coins-chip':
            assert f'id="{name}"' not in body
    assert 'quest-row--done' in body
    assert 'toast--xp' in body
    # The row stays one of the list: it can still go to the archive.
    assert f'/quests/{quest.pk}/archive/' in body
    assert f'name="list_url" value="{LIST_URL}"' in body


def test_a_row_of_today_still_answers_for_today(logged_in, user):
    quest = make_quest(user, 'Read')

    body = text(htmx_post(logged_in, f'/quests/{quest.pk}/complete/'))

    assert 'id="character-card"' in body
    assert f'/quests/{quest.pk}/archive/' not in body


def test_today_rows_have_no_list_actions(logged_in, user):
    quest = make_quest(user, 'Read')

    body = text(logged_in.get('/today/'))

    assert f'/quests/{quest.pk}/fail/' in body
    assert f'/quests/{quest.pk}/edit/' not in body
    assert f'/quests/{quest.pk}/archive/' not in body
    assert 'list_url' not in body


def test_failing_from_the_list_goes_through_the_service(logged_in, user):
    quest = make_quest(user, 'Read')

    response = htmx_post_form(logged_in, f'/quests/{quest.pk}/fail/', list_url=LIST_URL)

    assert response.status_code == 200
    body = text(response)
    assert 'quest-row--failed' in body
    assert 'Не вдалось: Read' in body
    assert f'/quests/{quest.pk}/fail/' not in body
    assert quest.completions.get().status == QuestCompletion.CompletionStatus.FAILED


def test_failing_without_javascript_returns_to_the_list(logged_in, user):
    quest = make_quest(user, 'Read')

    response = post(logged_in, f'/quests/{quest.pk}/fail/', list_url=LIST_URL)

    assert response['Location'] == LIST_URL


def test_a_refusal_in_the_list_is_said_inside_the_row_by_its_code(logged_in, user):
    quest = make_quest(user, 'Read')
    finish(quest)

    response = htmx_post_form(
        logged_in,
        f'/quests/{quest.pk}/complete/',
        list_url=LIST_URL,
    )

    assert response.status_code == 400
    body = text(response)
    assert titles(body) == ['Read']
    row = row_html(body)
    assert f'data-quest-row="{quest.pk}"' in row
    # The whole text, so neither the exception's English nor an id can be in it.
    alert = re.search(r'quest-row__alert" role="alert">([^<]*)<', row)
    assert alert[1] == 'Цей квест уже виконано.'
    assert 'has already been' not in body
    assert 'id="character-card"' not in body


def test_a_refusal_without_javascript_returns_to_the_list(logged_in, user):
    quest = make_quest(user, 'Read')
    finish(quest)

    response = post(logged_in, f'/quests/{quest.pk}/fail/', list_url=LIST_URL)

    assert response['Location'] == LIST_URL
    assert 'Цей квест уже виконано.' in text(logged_in.get(LIST_URL))


def test_row_actions_count_against_the_users_budget(logged_in, user):
    quest = make_quest(user, 'Read')

    with patch('web.views.user_budget_wait', return_value=42):
        response = htmx_post_form(
            logged_in,
            f'/quests/{quest.pk}/archive/',
            list_url=LIST_URL,
        )

    assert response.status_code == 429
    assert response['Retry-After'] == '42'
    assert 'Забагато дій' in text(response)
    quest.refresh_from_db()
    assert quest.archived_at is None


def test_the_api_ignores_the_web_forms_extra_field(user):
    client = APIClient()
    client.force_authenticate(user)

    response = client.post(
        '/api/quests/',
        {'title': 'API', 'quest_type': ONE_TIME, 'new_category': 'Fresh'},
        format='json',
    )

    assert response.status_code == 201
    assert 'new_category' not in response.json()
    assert not QuestCategory.objects.filter(user=user).exists()


@pytest.mark.parametrize(
    ('deadline', 'message'),
    [
        ('2001-01-01T10:00:00Z', 'Deadline must be in the future.'),
        ('2101-01-01T10:00:00Z', 'Deadline must be between the years 2000 and 2100.'),
    ],
)
def test_the_api_keeps_its_english_deadline_errors(user, deadline, message):
    client = APIClient()
    client.force_authenticate(user)

    response = client.post(
        '/api/quests/',
        {'title': 'API', 'quest_type': ONE_TIME, 'deadline': deadline},
        format='json',
    )

    assert response.status_code == 400
    assert response.json() == {'deadline': [message]}


# -- skills ------------------------------------------------------------------------


COUNT = SkillRequirement.RequirementType.QUEST_COUNT
COUNT_BY_CATEGORY = SkillRequirement.RequirementType.QUEST_COUNT_BY_CATEGORY
TOTAL_XP = SkillRequirement.RequirementType.TOTAL_XP


def make_skill(name, *requirements, parent=None, description=''):
    stat_type, _ = StatType.objects.get_or_create(
        name='WIS',
        defaults={'display_name': 'Wisdom', 'icon': 'owl'},
    )
    skill = Skill.objects.create(
        name=name,
        description=description,
        category=stat_type,
        parent=parent,
    )
    for requirement_type, threshold, *category in requirements:
        SkillRequirement.objects.create(
            skill=skill,
            requirement_type=requirement_type,
            threshold=threshold,
            category_name=category[0] if category else '',
        )
    return skill


def record_completions(user, count, category=''):
    QuestCompletion.objects.bulk_create(
        QuestCompletion(
            user=user,
            status=QuestCompletion.CompletionStatus.COMPLETED,
            xp_awarded=25,
            category_name_at_completion=category,
        )
        for _ in range(count)
    )


def unlock(client, skill_id):
    return htmx_post(client, f'/skills/{skill_id}/unlock/')


def plain_unlock(client, skill_id):
    return client.post(f'/skills/{skill_id}/unlock/', '', content_type=FORM)


def node_depths(body):
    """Each skill's name with how many lists it is nested in."""
    depth = 0
    found = []
    for match in re.finditer(r'<(/?)ul\b|skill-node__name"[^>]*>([^<]+)<', body):
        if match[2]:
            found.append((match[2], depth))
        else:
            depth += -1 if match[1] else 1
    return found


def node_html(body, name):
    """One skill's own part of the page, up to where its first child starts."""
    for part in body.split('<li class="skill-node">')[1:]:
        if re.search(rf'skill-node__name"[^>]*>{re.escape(name)}<', part):
            return part
    raise AssertionError(f'{name} is not on the page')


def test_the_tree_is_nested_lists_and_every_node_says_its_status(logged_in, user):
    root = make_skill('Root')
    UserSkill.objects.create(user=user, skill=root)
    child = make_skill('Child', parent=root)
    make_skill('Grandchild', parent=child)
    make_skill('Second root', (COUNT, 5))

    response = logged_in.get('/skills/')

    assert response.status_code == 200
    body = text(response)
    assert '<h1 class="title">Навички</h1>' in body
    assert node_depths(body) == [
        ('Root', 1),
        ('Child', 2),
        ('Grandchild', 3),
        ('Second root', 1),
    ]
    assert 'tag--unlocked">Відкрито<' in node_html(body, 'Root')
    assert 'tag--available">Доступно<' in node_html(body, 'Child')
    assert 'tag--locked">Заблоковано<' in node_html(body, 'Grandchild')
    assert 'Відкрито 1 з 4' in body


def test_the_tree_is_of_this_user_alone(logged_in, stranger):
    root = make_skill('Root')
    UserSkill.objects.create(user=stranger, skill=root)

    body = text(logged_in.get('/skills/'))

    assert 'tag--available">Доступно<' in node_html(body, 'Root')
    assert 'Відкрито 0 з 1' in body


def test_an_empty_catalog_says_so_instead_of_counting_nothing(logged_in):
    body = text(logged_in.get('/skills/'))

    assert 'Поки немає навичок' in body
    assert 'з 0' not in body


def test_requirements_show_their_progress(logged_in, user):
    Character.objects.filter(user=user).update(xp=120)
    record_completions(user, 17, 'Навчання')
    make_skill(
        'Scholar',
        (COUNT_BY_CATEGORY, 20, 'Навчання'),
        (COUNT, 30),
        (TOTAL_XP, 500),
        description='Вчись далі.',
    )

    node = node_html(text(logged_in.get('/skills/')), 'Scholar')

    assert 'Вчись далі.' in node
    assert 'квестів у «Навчання»: 17 з 20' in node
    assert 'квестів: 17 з 30' in node
    assert 'XP: 120 з 500' in node
    assert 'requirement--met' not in node


def test_a_category_is_matched_as_the_service_matches_it(logged_in, user):
    record_completions(user, 2, ' навчання ')
    record_completions(user, 3, 'Спорт')
    make_skill('Scholar', (COUNT_BY_CATEGORY, 20, 'Навчання'))

    node = node_html(text(logged_in.get('/skills/')), 'Scholar')

    assert 'квестів у «Навчання»: 2 з 20' in node


def test_the_seeded_requirements_name_their_categories_in_ukrainian(logged_in, user):
    call_command('seed_skills', verbosity=0)
    record_completions(user, 2, 'навчання')

    body = text(logged_in.get('/skills/'))

    assert 'квестів у «Навчання»: 2 з 5' in node_html(body, 'Дослідник')
    # The apostrophe is escaped on its way to the page, which shows it as typed.
    assert 'квестів у «Здоров&#x27;я»: 0 з 5' in node_html(body, 'Атлет')
    assert 'Study' not in body
    assert 'Health' not in body


def test_a_met_requirement_is_marked_and_stops_at_its_threshold(logged_in, user):
    Character.objects.filter(user=user).update(xp=1200)
    make_skill('Scholar', (TOTAL_XP, 500), (COUNT, 3))

    node = node_html(text(logged_in.get('/skills/')), 'Scholar')

    assert 'XP: 500 з 500' in node
    assert '1200' not in node
    assert node.count('requirement--met') == 1
    assert 'aria-label="Виконано"' in node
    assert 'квестів: 0 з 3' in node


def test_an_unlocked_skill_no_longer_lists_requirements(logged_in, user):
    skill = make_skill('Scholar', (COUNT, 3))
    UserSkill.objects.create(user=user, skill=skill)

    node = node_html(text(logged_in.get('/skills/')), 'Scholar')

    assert 'class="requirement' not in node
    assert 'квестів' not in node


def test_a_requirement_type_removed_from_the_code_reads_as_unmet():
    assert requirement_text(
        {'type': 'gone', 'category': None, 'current': 0, 'required': 5},
    ) == 'умова: 0 з 5'


def test_only_an_available_skill_offers_the_button(logged_in, user):
    done = make_skill('Done')
    UserSkill.objects.create(user=user, skill=done)
    ready = make_skill('Ready', parent=done)
    make_skill('Too early', (COUNT, 5))
    make_skill('Child of locked', parent=make_skill('Locked parent', (COUNT, 5)))

    body = text(logged_in.get('/skills/'))

    assert f'action="/skills/{ready.pk}/unlock/"' in node_html(body, 'Ready')
    assert 'aria-label="Відкрити: Ready"' in node_html(body, 'Ready')
    assert body.count('hx-post="/skills/') == 1


def test_a_locked_child_says_what_it_waits_for(logged_in):
    parent = make_skill('Parent', (COUNT, 5))
    make_skill('Child', parent=parent)

    body = text(logged_in.get('/skills/'))

    assert 'Спершу відкрий «Parent».' in node_html(body, 'Child')
    assert 'Спершу відкрий' not in node_html(body, 'Parent')


def test_a_skill_already_held_waits_for_nothing(logged_in, user):
    # Holding a child without its parent takes a raw edit: the admin refuses it.
    child = make_skill('Child', parent=make_skill('Parent', (COUNT, 5)))
    UserSkill.objects.create(user=user, skill=child)

    node = node_html(text(logged_in.get('/skills/')), 'Child')

    assert 'tag--unlocked">Відкрито<' in node
    assert 'Спершу відкрий' not in node


def test_names_in_the_tree_are_escaped(logged_in):
    make_skill('<b>Name</b>', (COUNT_BY_CATEGORY, 5, '<i>Cat</i>'))

    body = text(logged_in.get('/skills/'))

    assert '<b>Name</b>' not in body
    assert '<i>Cat</i>' not in body
    assert '&lt;b&gt;Name&lt;/b&gt;' in body
    assert '&lt;i&gt;Cat&lt;/i&gt;' in body


def test_unlocking_answers_with_the_tree_and_opens_the_way_for_the_children(
    logged_in,
    user,
):
    root = make_skill('Root')
    make_skill('Child', parent=root)

    response = unlock(logged_in, root.pk)

    assert response.status_code == 200
    assert UserSkill.objects.filter(user=user, skill=root).count() == 1
    body = text(response)
    assert 'id="skill-tree"' in body
    assert '<html' not in body
    assert 'tag--unlocked">Відкрито<' in node_html(body, 'Root')
    assert 'tag--available">Доступно<' in node_html(body, 'Child')
    assert 'Відкрито 1 з 2' in body
    assert 'alert' not in body
    assert 'toast--level">Навичку відкрито: Root<' in body
    # The button pressed is gone, so the focus moves to the node it belonged to.
    assert re.search(r'tabindex="-1" autofocus>Root<', body)
    assert body.count('autofocus') == 1


def test_a_page_holds_no_focus_of_its_own(logged_in):
    make_skill('Root')

    assert 'autofocus' not in text(logged_in.get('/skills/'))


def test_the_form_replaces_the_whole_tree(logged_in):
    make_skill('Root')

    node = node_html(text(logged_in.get('/skills/')), 'Root')

    assert 'hx-target="#skill-tree"' in node
    assert 'hx-swap="outerHTML"' in node
    assert 'hx-sync="#skill-tree:drop"' in node
    assert 'hx-disabled-elt="find button"' in node
    assert 'name="csrfmiddlewaretoken"' in node


def test_unlocking_goes_through_the_service_the_api_uses(logged_in, user):
    skill = make_skill('Root')

    with patch(
        'web.views.SkillService.unlock_skill',
        wraps=SkillService.unlock_skill,
    ) as service:
        assert unlock(logged_in, skill.pk).status_code == 200

    service.assert_called_once_with(user, skill.pk)


def test_an_achievement_earned_by_unlocking_has_its_toast(logged_in, user):
    skill = make_skill('Root')
    earned = Achievement.objects.create(
        name='Перша навичка',
        is_hidden=True,
        condition_type=Achievement.ConditionType.SKILLS_UNLOCKED_COUNT,
        condition_config={'threshold': 1},
    )

    body = text(unlock(logged_in, skill.pk))

    assert 'toast--achievement">Ачівка: Перша навичка<' in body
    assert UserAchievement.objects.filter(user=user, achievement=earned).exists()


def test_unmet_requirements_are_a_400_next_to_the_node(logged_in, user):
    skill = make_skill('Scholar', (COUNT, 5))
    make_skill('Other')

    response = unlock(logged_in, skill.pk)

    assert response.status_code == 400
    assert not UserSkill.objects.filter(user=user).exists()
    body = text(response)
    node = node_html(body, 'Scholar')
    assert 'role="alert">Вимоги навички ще не виконано.<' in node
    assert 'квестів: 0 з 5' in node
    assert 'alert' not in node_html(body, 'Other')
    assert 'Requirements not met' not in body
    assert 'toast' not in body


def test_a_locked_parent_is_a_409_next_to_the_node(logged_in, user):
    parent = make_skill('Parent', (COUNT, 5))
    child = make_skill('Child', parent=parent)

    response = unlock(logged_in, child.pk)

    assert response.status_code == 409
    assert not UserSkill.objects.filter(user=user).exists()
    body = text(response)
    assert (
        'role="alert">Спершу відкрий «Parent», потім «Child».<'
        in node_html(body, 'Child')
    )
    assert 'alert' not in node_html(body, 'Parent')
    assert 'tag--locked">Заблоковано<' in node_html(body, 'Parent')
    assert 'Unlock' not in body


@pytest.mark.parametrize('blocked_by', ['requirements', 'parent'])
def test_a_refusal_shows_no_internal_id(logged_in, blocked_by):
    if blocked_by == 'requirements':
        skill = make_skill('Skill', (COUNT, 5))
    else:
        skill = make_skill('Skill', parent=make_skill('Parent', (COUNT, 5)))

    alert = re.search(r'role="alert">([^<]+)<', text(unlock(logged_in, skill.pk)))[1]

    assert not re.search(r'\d', alert)


def test_unlocking_twice_is_not_an_error_and_changes_nothing(logged_in, user):
    skill = make_skill('Root')
    Achievement.objects.create(
        name='Перша навичка',
        condition_type=Achievement.ConditionType.SKILLS_UNLOCKED_COUNT,
        condition_config={'threshold': 1},
    )
    assert unlock(logged_in, skill.pk).status_code == 200
    first_unlock = UserSkill.objects.get(user=user, skill=skill).unlocked_at

    response = unlock(logged_in, skill.pk)

    assert response.status_code == 200
    body = text(response)
    assert 'tag--unlocked">Відкрито<' in node_html(body, 'Root')
    assert 'alert' not in body
    assert 'toast' not in body
    assert UserSkill.objects.get(user=user, skill=skill).unlocked_at == first_unlock
    assert UserAchievement.objects.filter(user=user).count() == 1


def test_unlocking_twice_without_javascript_says_nothing(logged_in, user):
    skill = make_skill('Root')
    UserSkill.objects.create(user=user, skill=skill)

    response = plain_unlock(logged_in, skill.pk)

    assert response.status_code == 302
    assert 'alert' not in text(logged_in.get(response['Location']))
    assert UserSkill.objects.filter(user=user).count() == 1


def test_without_javascript_unlocking_redirects_and_says_what_happened(logged_in, user):
    skill = make_skill('Root')

    response = plain_unlock(logged_in, skill.pk)

    assert response.status_code == 302
    assert response['Location'] == '/skills/'
    assert UserSkill.objects.filter(user=user, skill=skill).exists()
    assert 'Навичку відкрито: Root' in text(logged_in.get('/skills/'))


def test_without_javascript_a_refusal_redirects_and_shows_the_alert(logged_in):
    skill = make_skill('Scholar', (COUNT, 5))

    response = plain_unlock(logged_in, skill.pk)

    assert response.status_code == 302
    page = text(logged_in.get('/skills/'))
    assert 'Вимоги навички ще не виконано.' in page
    assert 'alert--error' in page


def test_a_skill_deleted_meanwhile_is_a_404_above_the_tree(logged_in):
    make_skill('Other')
    gone = make_skill('Gone')
    gone_id = gone.pk
    gone.delete()

    response = unlock(logged_in, gone_id)

    assert response.status_code == 404
    body = text(response)
    assert 'role="alert">Навичку не знайдено.<' in body
    assert 'Other' in body
    assert 'alert' not in node_html(body, 'Other')


def test_without_javascript_a_missing_skill_is_the_plain_404(logged_in):
    assert plain_unlock(logged_in, 987654).status_code == 404


def test_an_id_past_what_the_database_holds_is_a_404_for_unlocking(logged_in):
    assert unlock(logged_in, 10 ** 30).status_code == 404


def test_unlocking_only_accepts_post(logged_in):
    skill = make_skill('Root')

    assert logged_in.get(f'/skills/{skill.pk}/unlock/').status_code == 405


def test_unlocking_needs_a_login(client, db):
    skill = make_skill('Root')

    response = plain_unlock(client, skill.pk)

    assert response.status_code == 302
    assert response['Location'].startswith('/login/')
    assert not UserSkill.objects.exists()


def test_unlocking_needs_the_csrf_token(user):
    skill = make_skill('Root')
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = plain_unlock(client, skill.pk)

    assert response.status_code == 403
    assert not UserSkill.objects.exists()


def test_unlocking_counts_against_the_users_budget(logged_in):
    skill = make_skill('Root')

    with patch('web.views.user_budget_wait', return_value=42):
        response = unlock(logged_in, skill.pk)

    assert response.status_code == 429
    assert response['Retry-After'] == '42'
    assert 'Забагато дій' in node_html(text(response), 'Root')
    assert not UserSkill.objects.exists()


def test_an_error_from_an_event_handler_shows_no_internal_text_on_the_tree(logged_in):
    skill = make_skill('Root')

    with patch(
        'web.views.SkillService.unlock_skill',
        side_effect=DjangoValidationError('internal 42'),
    ):
        response = unlock(logged_in, skill.pk)

    assert response.status_code == 400
    assert GENERIC_ERROR in node_html(text(response), 'Root')
    assert 'internal 42' not in text(response)


def test_the_skills_page_takes_the_same_queries_for_any_catalog(logged_in, user):
    def populate(prefix, count):
        parent = make_skill(f'{prefix} root', (COUNT, 1), (TOTAL_XP, 1))
        for index in range(count):
            parent = make_skill(
                f'{prefix} {index}',
                (COUNT_BY_CATEGORY, 2, f'{prefix} category'),
                (TOTAL_XP, 1),
                parent=parent,
            )

    def queries_for_page():
        with CaptureQueriesContext(connection) as queries:
            assert logged_in.get('/skills/').status_code == 200
        return len(queries)

    populate('A', 1)
    few = queries_for_page()
    populate('B', 12)
    many = queries_for_page()

    # Session, user, the middleware's timezone, the header's coins, and the tree's
    # XP, unlocks, catalog, requirements and completions.
    assert few == many == 9


# -- achievements ------------------------------------------------------------------


def achievement_card_html(body, name):
    for part in body.split('<li class="achievement-card')[1:]:
        if f'achievement-card__name">{name}<' in part:
            return part
    raise AssertionError(f'{name} is not on the page')


def earn(user, item, when=None):
    row = UserAchievement.objects.create(user=user, achievement=item)
    if when is not None:
        UserAchievement.objects.filter(pk=row.pk).update(unlocked_at=when)
    return row


def test_achievements_are_counted_and_the_bar_shows_the_share(logged_in, user):
    earn(user, achievement('Earned'))
    earn(user, achievement('Hidden and earned', hidden=True))
    achievement('Waiting')
    achievement('Secret', hidden=True)

    response = logged_in.get('/achievements/')

    assert response.status_code == 200
    body = text(response)
    assert '<h1 class="title">Ачівки</h1>' in body
    assert 'Відкрито 2 з 4' in body
    assert re.search(r'<progress[^>]*value="2" max="4"', body)


def test_an_earned_achievement_shows_its_date_in_the_users_timezone(logged_in, user):
    Character.objects.filter(user=user).update(timezone='Europe/Kyiv')
    earn(
        user,
        achievement('Earned'),
        datetime.fromisoformat('2026-03-14T23:30:00+00:00'),
    )

    card = achievement_card_html(text(logged_in.get('/achievements/')), 'Earned')

    assert 'Earned description' in card
    assert '15 березня 2026' in card
    assert 'datetime="2026-03-15T01:30:00+02:00"' in card
    assert 'achievement-card--earned' in card


def test_a_visible_achievement_shows_its_description_and_no_date(logged_in):
    achievement('Waiting')

    card = achievement_card_html(text(logged_in.get('/achievements/')), 'Waiting')

    assert 'Waiting description' in card
    assert 'Отримано' not in card
    assert '<time' not in card
    assert 'achievement-card--earned' not in card


def test_a_hidden_achievement_is_a_question_mark_with_nothing_given_away(logged_in):
    Achievement.objects.create(
        name='Secret name',
        description='Secret text',
        icon='secret-icon',
        is_hidden=True,
        condition_type=Achievement.ConditionType.STREAK_LENGTH,
        condition_config={'threshold': 77},
    )

    body = text(logged_in.get('/achievements/'))

    assert 'achievement-card--masked' in achievement_card_html(body, '???')
    assert 'Secret' not in body
    assert 'secret-icon' not in body
    assert 'streak_length' not in body
    assert '77' not in body
    assert 'Відкрито 0 з 1' in body


def test_a_hidden_achievement_shows_itself_once_earned(logged_in, user):
    earn(user, achievement('Secret', hidden=True))

    body = text(logged_in.get('/achievements/'))

    assert '???' not in body
    card = achievement_card_html(body, 'Secret')
    assert 'Secret description' in card
    assert 'Отримано' in card


def test_achievements_have_no_progress_of_their_own(logged_in, user):
    record_completions(user, 17)
    Achievement.objects.create(
        name='Twenty',
        condition_type=Achievement.ConditionType.QUEST_COUNT,
        condition_config={'threshold': 20},
    )

    body = text(logged_in.get('/achievements/'))

    card = achievement_card_html(body, 'Twenty')
    assert not re.search(r'\d', re.sub(r'<[^>]+>', '', card))
    assert '<progress' not in card
    assert body.count('<progress') == 1


def test_achievement_cards_go_earned_then_visible_then_masked(logged_in, user):
    achievement('Secret', hidden=True)
    achievement('Waiting')
    earn(user, achievement('Earned'))

    body = text(logged_in.get('/achievements/'))

    assert re.findall(r'achievement-card__name">([^<]+)<', body) == [
        'Earned',
        'Waiting',
        '???',
    ]


def test_achievements_are_of_this_user_alone(logged_in, stranger):
    earn(stranger, achievement('Earned by another'))

    body = text(logged_in.get('/achievements/'))

    assert 'Відкрито 0 з 1' in body
    assert 'Отримано' not in body


def test_no_achievements_say_so_and_draw_no_empty_bar(logged_in):
    body = text(logged_in.get('/achievements/'))

    assert 'Поки немає ачівок' in body
    assert '<progress' not in body


def test_the_seeded_achievement_is_shown_in_ukrainian(logged_in):
    call_command('seed_achievements', verbosity=0)

    card = achievement_card_html(
        text(logged_in.get('/achievements/')),
        'Переможець босів',
    )

    assert 'Переможи свого першого боса.' in card


def test_the_achievements_page_only_answers_reads(logged_in):
    assert logged_in.post('/achievements/', '', content_type=FORM).status_code == 405


def test_the_achievements_page_takes_the_same_queries_for_any_number(logged_in, user):
    def queries_for_page():
        with CaptureQueriesContext(connection) as queries:
            assert logged_in.get('/achievements/').status_code == 200
        return len(queries)

    earn(user, achievement('First'))
    few = queries_for_page()
    for index in range(12):
        item = achievement(f'Item {index}', hidden=index % 2 == 0)
        if index % 3 == 0:
            earn(user, item)
    many = queries_for_page()

    # Session, user, the middleware's timezone, the header's coins, the cards.
    assert few == many == 5


# -- rewards -----------------------------------------------------------------------


def give_coins(user, coins):
    Character.objects.filter(user=user).update(coins=coins)


def coins_of(user):
    return Character.objects.get(user=user).coins


def make_reward(user, title='Film night', cost=40, **fields):
    return RewardService.create_reward(user, title=title, cost=cost, **fields)


def card_html(body, title):
    """One reward's card, up to where the next one starts."""
    for part in body.split('<article class="reward-card')[1:]:
        if re.search(rf'reward-card__title"[^>]*>{re.escape(title)}<', part):
            return part
    raise AssertionError(f'{title} is not on the page')


def htmx_get_card(client, url):
    return client.get(url, headers={'HX-Request': 'true'})


def form_token_of(client, reward):
    """The token of the form the confirmation renders."""
    body = text(htmx_get_card(client, f'/rewards/{reward.pk}/redeem/'))
    return re.search(r'name="form_token" value="([^"]+)"', body)[1]


def redeem(client, reward, token=None):
    return htmx_post_form(
        client,
        f'/rewards/{reward.pk}/redeem/',
        form_token=token if token is not None else form_token_of(client, reward),
    )


def plain_redeem(client, reward, token=None):
    return post(
        client,
        f'/rewards/{reward.pk}/redeem/',
        form_token=token if token is not None else form_token_of(client, reward),
    )


def test_rewards_are_in_the_navigation(logged_in):
    body = text(logged_in.get('/rewards/'))

    assert body.count('aria-current="page"') == 1
    assert 'nav__link--active" href="/rewards/" aria-current="page">Нагороди<' in body
    assert 'href="/rewards/"' in text(logged_in.get('/today/'))


@pytest.mark.django_db
@pytest.mark.parametrize('path', ['/rewards/', '/rewards/new/'])
def test_rewards_need_a_login(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert response['Location'] == f'/login/?next={path}'


def test_the_screen_shows_the_balance_and_says_when_there_is_nothing(logged_in, user):
    give_coins(user, 121)

    body = text(logged_in.get('/rewards/'))

    assert '<h1 class="title">Нагороди</h1>' in body
    assert '<span>121</span>' in body
    assert 'Нагород поки немає' in body
    assert 'Операцій поки немає' in body
    assert 'href="/rewards/new/"' in body


def test_a_card_shows_the_title_cost_and_button(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user, description='Без телефона')

    card = card_html(text(logged_in.get('/rewards/')), 'Film night')

    assert '40 монет' in card
    assert 'Без телефона' in card
    assert f'href="/rewards/{reward.pk}/redeem/"' in card
    assert not re.search(r'<button[^>]* disabled', card)
    assert 'Не вистачає' not in card


@pytest.mark.parametrize(
    ('coins', 'hint'),
    [
        (39, 'Не вистачає 1 монети'),
        (38, 'Не вистачає 2 монет'),
        (0, 'Не вистачає 40 монет'),
        (19, 'Не вистачає 21 монети'),
    ],
)
def test_without_enough_coins_the_button_is_disabled_with_a_hint(
    logged_in,
    user,
    coins,
    hint,
):
    give_coins(user, coins)
    reward = make_reward(user)

    card = card_html(text(logged_in.get('/rewards/')), 'Film night')

    assert hint in card
    assert re.search(r'<button[^>]*disabled[^>]*>Отримати<', card)
    assert f'/rewards/{reward.pk}/redeem/' not in card


def test_exactly_enough_coins_is_enough(logged_in, user):
    give_coins(user, 40)
    make_reward(user)

    card = card_html(text(logged_in.get('/rewards/')), 'Film night')

    assert 'Не вистачає' not in card
    assert '>Отримати</a>' in card


def test_a_switched_off_reward_offers_no_button_and_no_hint(logged_in, user):
    make_reward(user, is_active=False)

    card = card_html(text(logged_in.get('/rewards/')), 'Film night')

    assert 'Вимкнено' in card
    assert 'Отримати' not in card
    assert 'Не вистачає' not in card
    assert 'Змінити' in card


def test_only_the_users_own_rewards_are_listed(logged_in, stranger):
    make_reward(stranger, title='Their secret')

    assert 'Their secret' not in text(logged_in.get('/rewards/'))


def test_titles_are_escaped_everywhere_a_reward_shows(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user, title='<b>Bold</b>', description='<i>x</i>')

    page = text(logged_in.get('/rewards/'))
    confirmation = text(htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/'))
    answer = text(redeem(logged_in, reward))

    for body in (page, confirmation, answer):
        assert '<b>Bold</b>' not in body
        assert '&lt;b&gt;Bold&lt;/b&gt;' in body
        assert '<i>x</i>' not in body


# -- rewards: confirmation ---------------------------------------------------------


def test_the_confirmation_names_the_reward_and_its_cost_and_spends_nothing(
    logged_in,
    user,
):
    give_coins(user, 100)
    reward = make_reward(user)

    response = htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/')

    assert response.status_code == 200
    body = text(response)
    assert '<html' not in body
    assert 'Отримати «Film night» за 40 монет?' in body
    assert 'На балансі залишиться 60.' in body
    assert '>Підтвердити<' in body
    assert coins_of(user) == 100
    assert not RewardRedemption.objects.exists()


@pytest.mark.parametrize(
    ('cost', 'words'),
    [(1, 'за 1 монету?'), (2, 'за 2 монети?'), (5, 'за 5 монет?'), (21, 'за 21 монету?')],
)
def test_the_confirmation_agrees_with_the_number(logged_in, user, cost, words):
    give_coins(user, 100)
    reward = make_reward(user, cost=cost)

    assert words in text(htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/'))


def test_the_form_that_spends_is_guarded_against_a_second_click(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    body = text(htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/'))

    form = re.search(r'<form[^>]*>', body)[0]
    assert f'hx-post="/rewards/{reward.pk}/redeem/"' in form
    assert 'hx-sync="this:drop"' in form
    assert 'hx-disabled-elt="find button"' in form
    assert 'name="csrfmiddlewaretoken"' in body


def test_the_list_itself_holds_no_form_that_spends(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    body = text(logged_in.get('/rewards/'))

    assert f'hx-post="/rewards/{reward.pk}/redeem/"' not in body
    assert 'form_token' not in body
    assert 'form_tokens' not in logged_in.session


def test_cancelling_brings_the_card_back(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    response = htmx_get_card(logged_in, f'/rewards/{reward.pk}/card/')

    assert response.status_code == 200
    body = text(response)
    assert '>Отримати</a>' in body
    assert 'form_token' not in body


def test_without_javascript_cancelling_goes_to_the_list(logged_in, user):
    reward = make_reward(user)

    response = logged_in.get(f'/rewards/{reward.pk}/card/')

    assert response.status_code == 302
    assert response['Location'] == '/rewards/'


def test_without_javascript_the_confirmation_is_a_page_of_its_own(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    response = logged_in.get(f'/rewards/{reward.pk}/redeem/')

    assert response.status_code == 200
    body = text(response)
    assert '<html' in body
    assert 'Отримати «Film night» за 40 монет?' in body
    assert 'name="form_token"' in body
    # Its form posts normally: the list it would be swapped into is not on this page.
    assert 'hx-disable' in body
    assert 'href="/rewards/"' in body


def test_asking_to_confirm_what_cannot_be_afforded_says_why(logged_in, user):
    give_coins(user, 3)
    reward = make_reward(user)

    body = text(htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/'))

    assert 'role="alert">Не вистачає монет: є 3, потрібно 40.<' in body
    assert 'form_token' not in body


def test_a_confirmation_asked_for_on_a_stale_page_brings_the_whole_screen_up_to_date(
    logged_in,
    user,
):
    give_coins(user, 100)
    reward = make_reward(user)
    make_reward(user, title='Other', cost=50)
    logged_in.get('/rewards/')
    # Another tab spends the coins, and this one still offers the button.
    give_coins(user, 3)

    response = htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/')

    assert response.status_code == 200
    assert response['HX-Retarget'] == '#reward-browser'
    assert response['HX-Reswap'] == 'outerHTML'
    body = text(response)
    assert 'id="reward-browser"' in body
    assert 'Не вистачає монет: є 3, потрібно 40.' in card_html(body, 'Film night')
    assert 'role="alert"' not in card_html(body, 'Other').split('id="coins-chip"')[0]
    assert 'Не вистачає 47 монет' in card_html(body, 'Other')
    assert re.search(r'id="coins-chip"[^>]*hx-swap-oob="true"', body)
    assert coins_shown(body) == '3'


def test_a_reward_switched_off_on_a_stale_page_is_said_on_the_refreshed_list(
    logged_in,
    user,
):
    give_coins(user, 100)
    reward = make_reward(user)
    RewardService.update_reward(user, reward.pk, is_active=False)

    response = htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/')

    assert response['HX-Retarget'] == '#reward-browser'
    assert 'Ця нагорода неактивна.' in card_html(text(response), 'Film night')
    assert 'form_token' not in text(response)


def test_without_javascript_asking_to_confirm_what_cannot_be_afforded_returns(
    logged_in,
    user,
):
    give_coins(user, 3)
    reward = make_reward(user)

    response = logged_in.get(f'/rewards/{reward.pk}/redeem/')

    assert response.status_code == 302
    assert 'Не вистачає монет: є 3, потрібно 40.' in text(logged_in.get('/rewards/'))


# -- rewards: redeeming ------------------------------------------------------------


def test_redeeming_goes_through_the_service_the_api_uses(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    with patch(
        'web.views.WalletService.redeem_reward',
        wraps=WalletService.redeem_reward,
    ) as service:
        assert redeem(logged_in, reward).status_code == 200

    service.assert_called_once_with(user, reward.pk)


def test_redeeming_spends_the_coins_and_keeps_the_ledger(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    response = redeem(logged_in, reward)

    assert response.status_code == 200
    assert coins_of(user) == 60
    redemption = RewardRedemption.objects.get()
    assert (redemption.title_at_redemption, redemption.cost_at_redemption) == (
        'Film night',
        40,
    )
    transaction = WalletTransaction.objects.get()
    assert (transaction.amount, transaction.balance_after) == (-40, 60)


def test_the_balance_in_the_header_updates_out_of_band(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    body = text(redeem(logged_in, reward))

    chip = re.search(r'<span class="chip chip--coins"[^>]*>.*?</span>\s*</span>', body, re.S)[0]
    assert 'id="coins-chip"' in chip
    assert 'hx-swap-oob="true"' in chip
    assert '<span>60</span>' in chip


def test_redeeming_answers_with_the_list_the_history_and_a_toast(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    body = text(redeem(logged_in, reward))

    assert '<html' not in body
    assert 'id="reward-browser"' in body
    assert '<span>60</span>' in body.split('id="coins-chip"')[0]
    assert re.search(r'id="wallet-history" hx-swap-oob="true"', body)
    assert 'history__title">Обмін: Film night<' in body
    assert 'toast--coins">Отримано: Film night, −40 монет<' in body
    assert 'alert' not in body.split('id="coins-chip"')[0]


def test_the_cards_follow_the_new_balance(logged_in, user):
    give_coins(user, 50)
    first = make_reward(user, title='First', cost=40)
    make_reward(user, title='Second', cost=40)

    body = text(redeem(logged_in, first))

    assert 'Не вистачає 30 монет' in card_html(body, 'First')
    assert 'Не вистачає 30 монет' in card_html(body, 'Second')


def test_the_focus_moves_to_the_card_acted_on(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    make_reward(user, title='Other')

    body = text(redeem(logged_in, reward))

    assert re.search(r'tabindex="-1" autofocus>Film night<', body)
    assert body.count('autofocus') == 1
    assert 'autofocus' not in text(logged_in.get('/rewards/'))


def test_without_javascript_redeeming_redirects_and_says_what_happened(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    response = plain_redeem(logged_in, reward)

    assert response.status_code == 302
    assert response['Location'] == '/rewards/'
    assert coins_of(user) == 60
    page = text(logged_in.get('/rewards/'))
    assert 'Отримано: Film night, −40 монет' in page
    assert 'alert--success' in page


# -- rewards: the one-time token ---------------------------------------------------


def test_two_identical_posts_in_a_row_spend_the_coins_once(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)

    first = redeem(logged_in, reward, token)
    second = redeem(logged_in, reward, token)

    assert first.status_code == 200
    assert 'toast--coins' in text(first)
    assert second.status_code == 200
    body = text(second)
    assert 'role="status">Вже оброблено.' in card_html(body, 'Film night')
    assert 'toast' not in body
    assert '<span>60</span>' in body
    assert coins_of(user) == 60
    assert RewardRedemption.objects.count() == 1
    assert WalletTransaction.objects.count() == 1


def test_without_javascript_a_second_post_spends_nothing_either(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)

    plain_redeem(logged_in, reward, token)
    logged_in.get('/rewards/')
    response = plain_redeem(logged_in, reward, token)

    assert response.status_code == 302
    assert coins_of(user) == 60
    page = text(logged_in.get('/rewards/'))
    assert 'Вже оброблено.' in page
    assert 'alert--info' in page


@pytest.mark.django_db(transaction=True)
def test_two_posts_at_once_with_one_token_spend_the_coins_once(user):
    give_coins(user, 100)
    reward = make_reward(user)
    first = Client()
    first.force_login(user)
    second = Client()
    second.cookies = first.cookies
    token = form_token_of(first, reward)
    barrier = threading.Barrier(2)
    statuses = []

    def post_token(client):
        try:
            barrier.wait(timeout=5)
            statuses.append(redeem(client, reward, token).status_code)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=post_token, args=(client,), daemon=True)
        for client in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert statuses == [200, 200]
    assert coins_of(user) == 60
    assert RewardRedemption.objects.count() == 1


def test_every_confirmation_has_a_token_of_its_own(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    first = form_token_of(logged_in, reward)
    second = form_token_of(logged_in, reward)

    assert first != second
    # Two forms open for real: each may be used once.
    assert 'toast--coins' in text(redeem(logged_in, reward, first))
    assert 'toast--coins' in text(redeem(logged_in, reward, second))
    assert coins_of(user) == 20


@pytest.mark.parametrize('token', ['', 'made-up'])
def test_a_token_never_issued_spends_nothing(logged_in, user, token):
    give_coins(user, 100)
    reward = make_reward(user)

    response = redeem(logged_in, reward, token)

    assert 'Вже оброблено.' in text(response)
    assert coins_of(user) == 100


def test_a_post_with_no_token_at_all_spends_nothing(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)

    response = htmx_post_form(logged_in, f'/rewards/{reward.pk}/redeem/')

    assert 'Вже оброблено.' in text(response)
    assert coins_of(user) == 100


def test_a_token_works_for_its_own_reward_only(logged_in, user):
    give_coins(user, 100)
    cheap = make_reward(user, title='Cheap', cost=1)
    dear = make_reward(user, title='Dear', cost=90)
    token = form_token_of(logged_in, cheap)

    response = redeem(logged_in, dear, token)

    assert 'Вже оброблено.' in text(response)
    assert coins_of(user) == 100
    # Still good for what it was issued for.
    assert 'toast--coins' in text(redeem(logged_in, cheap, token))


def test_a_token_belongs_to_the_session_that_was_given_it(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)
    other_device = Client()
    other_device.force_login(user)

    response = redeem(other_device, reward, token)

    assert 'Вже оброблено.' in text(response)
    assert coins_of(user) == 100


def test_a_refused_post_uses_up_its_token(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)
    give_coins(user, 3)

    assert redeem(logged_in, reward, token).status_code == 400
    give_coins(user, 100)
    response = redeem(logged_in, reward, token)

    assert 'Вже оброблено.' in text(response)
    assert coins_of(user) == 100


def test_the_oldest_tokens_make_way_so_the_session_stays_small(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    oldest = form_token_of(logged_in, reward)

    for _ in range(form_tokens.MAX_TOKENS):
        newest = form_token_of(logged_in, reward)

    assert len(logged_in.session[form_tokens.SESSION_KEY]) == form_tokens.MAX_TOKENS
    assert 'Вже оброблено.' in text(redeem(logged_in, reward, oldest))
    assert 'toast--coins' in text(redeem(logged_in, reward, newest))


def test_tokens_do_not_renew_the_session(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    session_key = logged_in.session.session_key
    expires = timezone.now() + timedelta(days=3)
    Session.objects.filter(pk=session_key).update(expire_date=expires)

    confirmation = htmx_get_card(logged_in, f'/rewards/{reward.pk}/redeem/')
    token = re.search(r'name="form_token" value="([^"]+)"', text(confirmation))[1]
    answer = redeem(logged_in, reward, token)

    # Web sessions last a fixed 30 days from login: no new date, no new cookie.
    assert Session.objects.get(pk=session_key).expire_date == expires
    assert settings.SESSION_COOKIE_NAME not in confirmation.cookies
    assert settings.SESSION_COOKIE_NAME not in answer.cookies
    assert is_logged_in(logged_in)


def test_tokens_rest_on_the_database_session_backend():
    # form_tokens changes the Session row directly.
    assert settings.SESSION_ENGINE == 'django.contrib.sessions.backends.db'


# -- rewards: refusals -------------------------------------------------------------


def test_not_enough_coins_is_said_on_the_card_with_the_numbers(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    make_reward(user, title='Other')
    token = form_token_of(logged_in, reward)
    # Spent in another tab since the form was opened.
    give_coins(user, 3)

    response = redeem(logged_in, reward, token)

    assert response.status_code == 400
    body = text(response)
    card = card_html(body, 'Film night')
    assert 'role="alert">Не вистачає монет: є 3, потрібно 40.<' in card
    assert 'Not enough coins' not in body
    assert 'alert' not in card_html(body, 'Other')
    assert '<span>3</span>' in body
    assert coins_of(user) == 3
    assert not RewardRedemption.objects.exists()


def test_the_redemption_limit_is_said_on_the_card(logged_in, user, settings):
    settings.QUESTIFY_MAX_REDEMPTIONS_PER_USER_PER_24H = 1
    give_coins(user, 100)
    reward = make_reward(user)
    assert redeem(logged_in, reward).status_code == 200

    response = redeem(logged_in, reward)

    assert response.status_code == 400
    assert (
        'role="alert">Ліміт обмінів за останні 24 години вичерпано.<'
        in card_html(text(response), 'Film night')
    )
    assert coins_of(user) == 60
    assert RewardRedemption.objects.count() == 1


def test_a_limit_counts_what_the_api_redeemed_too(logged_in, user, settings):
    settings.QUESTIFY_MAX_REDEMPTIONS_PER_USER_PER_24H = 1
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)
    api = APIClient()
    api.force_authenticate(user)
    assert api.post(f'/api/rewards/{reward.pk}/redeem/').status_code == 201

    response = redeem(logged_in, reward, token)

    assert response.status_code == 400
    assert coins_of(user) == 60


def test_a_reward_switched_off_meanwhile_is_refused_on_its_card(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)
    RewardService.update_reward(user, reward.pk, is_active=False)

    response = redeem(logged_in, reward, token)

    assert response.status_code == 400
    assert 'role="alert">Ця нагорода неактивна.<' in card_html(
        text(response),
        'Film night',
    )
    assert coins_of(user) == 100


def test_a_reward_archived_meanwhile_is_a_404_above_the_list(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    make_reward(user, title='Other')
    token = form_token_of(logged_in, reward)
    RewardService.archive_reward(user, reward.pk)

    response = redeem(logged_in, reward, token)

    assert response.status_code == 404
    body = text(response)
    assert 'role="alert">Нагороду не знайдено: можливо, її вже архівовано.<' in body
    assert 'alert' not in card_html(body, 'Other')
    assert 'Film night' not in body
    assert coins_of(user) == 100


def test_a_reward_archived_between_the_token_and_the_service_is_the_same_404(
    logged_in,
    user,
):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)

    with patch(
        'web.views.WalletService.redeem_reward',
        side_effect=NotFoundError('Not found.', code='reward_not_found'),
    ):
        response = redeem(logged_in, reward, token)

    assert response.status_code == 404
    assert 'Нагороду не знайдено' in text(response)


@pytest.mark.parametrize('blocked', ['coins', 'archived', 'off', 'limit'])
def test_a_refusal_shows_no_internal_id(logged_in, user, settings, blocked):
    give_coins(user, 100)
    # An id that cannot pass for the balance or the cost a text may quote.
    reward = Reward.objects.create(pk=987654, user=user, title='Film night', cost=7)
    token = form_token_of(logged_in, reward)
    if blocked == 'coins':
        give_coins(user, 3)
    elif blocked == 'archived':
        RewardService.archive_reward(user, reward.pk)
    elif blocked == 'off':
        RewardService.update_reward(user, reward.pk, is_active=False)
    else:
        settings.QUESTIFY_MAX_REDEMPTIONS_PER_USER_PER_24H = 0

    alert = re.search(r'role="alert">([^<]+)<', text(redeem(logged_in, reward, token)))[1]

    assert '987654' not in alert


def test_redeeming_counts_against_the_users_budget_and_keeps_the_token(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)

    with patch('web.views.user_budget_wait', return_value=42):
        response = redeem(logged_in, reward, token)

    assert response.status_code == 429
    assert response['Retry-After'] == '42'
    assert 'Забагато дій' in card_html(text(response), 'Film night')
    assert coins_of(user) == 100
    # Nothing was processed, so the same form still works.
    assert 'toast--coins' in text(redeem(logged_in, reward, token))


def test_without_javascript_a_refusal_redirects_and_shows_the_alert(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    token = form_token_of(logged_in, reward)
    give_coins(user, 3)

    response = plain_redeem(logged_in, reward, token)

    assert response.status_code == 302
    page = text(logged_in.get('/rewards/'))
    assert 'Не вистачає монет: є 3, потрібно 40.' in page
    assert 'alert--error' in page


# -- rewards: someone else's -------------------------------------------------------


@pytest.fixture
def their_reward(stranger):
    return make_reward(stranger, title='Their secret')


@pytest.mark.parametrize('action', ['redeem', 'card'])
def test_htmx_is_told_a_foreign_card_does_not_exist(logged_in, their_reward, action):
    response = htmx_get_card(logged_in, f'/rewards/{their_reward.pk}/{action}/')

    assert response.status_code == 404
    body = text(response)
    assert 'Нагороду не знайдено' in body
    assert 'Their secret' not in body
    assert 'form_token' not in body


@pytest.mark.parametrize('action', ['redeem', 'archive'])
def test_acting_on_a_foreign_reward_is_a_404_for_htmx(
    logged_in,
    user,
    stranger,
    their_reward,
    action,
):
    give_coins(stranger, 100)
    give_coins(user, 100)

    response = htmx_post_form(
        logged_in,
        f'/rewards/{their_reward.pk}/{action}/',
        form_token='anything',
    )

    assert response.status_code == 404
    assert 'Their secret' not in text(response)
    assert coins_of(stranger) == 100
    assert coins_of(user) == 100
    their_reward.refresh_from_db()
    assert their_reward.archived_at is None


@pytest.mark.parametrize('action', ['redeem', 'archive', 'edit'])
def test_a_foreign_reward_is_the_plain_404_without_javascript(
    logged_in,
    their_reward,
    action,
):
    response = post(logged_in, f'/rewards/{their_reward.pk}/{action}/', title='Mine', cost=1)

    assert response.status_code == 404
    their_reward.refresh_from_db()
    assert their_reward.title == 'Their secret'
    assert their_reward.archived_at is None


@pytest.mark.parametrize('page', ['redeem', 'edit'])
def test_opening_a_foreign_reward_is_the_plain_404(logged_in, their_reward, page):
    assert logged_in.get(f'/rewards/{their_reward.pk}/{page}/').status_code == 404


@pytest.mark.parametrize('action', ['redeem', 'archive', 'edit'])
def test_an_id_past_what_the_database_holds_is_a_404_for_rewards(logged_in, action):
    assert post(logged_in, f'/rewards/{10 ** 30}/{action}/').status_code == 404


# -- rewards: the protocol ---------------------------------------------------------


@pytest.mark.parametrize('action', ['redeem', 'archive'])
def test_acting_on_a_reward_needs_a_login(client, user, action):
    give_coins(user, 100)
    reward = make_reward(user)

    response = post(client, f'/rewards/{reward.pk}/{action}/')

    assert response.status_code == 302
    assert response['Location'].startswith('/login/')
    assert coins_of(user) == 100
    reward.refresh_from_db()
    assert reward.archived_at is None


@pytest.mark.parametrize('action', ['redeem', 'archive'])
def test_acting_on_a_reward_needs_the_csrf_token(user, action):
    give_coins(user, 100)
    reward = make_reward(user)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = post(client, f'/rewards/{reward.pk}/{action}/')

    assert response.status_code == 403
    assert coins_of(user) == 100


def test_archiving_only_accepts_post(logged_in, user):
    reward = make_reward(user)

    assert logged_in.get(f'/rewards/{reward.pk}/archive/').status_code == 405


def test_the_card_and_the_list_only_answer_reads(logged_in, user):
    reward = make_reward(user)

    assert post(logged_in, f'/rewards/{reward.pk}/card/').status_code == 405
    assert post(logged_in, '/rewards/').status_code == 405


def test_the_api_redeems_as_before_with_no_token(user):
    give_coins(user, 100)
    reward = make_reward(user)
    api = APIClient()
    api.force_authenticate(user)

    response = api.post(f'/api/rewards/{reward.pk}/redeem/')

    assert response.status_code == 201
    assert response.json()['balance'] == 60


# -- rewards: the form -------------------------------------------------------------


def create_reward(client, **data):
    return post(client, '/rewards/new/', **{'title': 'Film night', 'cost': '40', **data})


def test_the_form_starts_empty_and_the_screen_leads_to_it(logged_in):
    assert 'href="/rewards/new/"' in text(logged_in.get('/rewards/'))

    body = text(logged_in.get('/rewards/new/'))

    assert '<h1 class="title">Нова нагорода</h1>' in body
    assert 'name="title"' in body
    assert 'name="description"' in body
    assert 'type="number"' in body
    assert 'min="1"' in body
    assert 'max="100000"' in body
    assert 'name="is_active"' not in body


def test_creating_goes_through_the_service_and_returns_to_the_list(logged_in, user):
    with patch(
        'web.forms.RewardService.create_reward',
        wraps=RewardService.create_reward,
    ) as service:
        response = create_reward(logged_in, description='Без телефона')

    assert response.status_code == 302
    assert response['Location'] == '/rewards/'
    service.assert_called_once_with(
        user,
        title='Film night',
        description='Без телефона',
        cost=40,
    )
    reward = Reward.objects.get(user=user)
    assert (reward.title, reward.cost, reward.is_active) == ('Film night', 40, True)
    assert 'Нагороду «Film night» створено.' in text(logged_in.get('/rewards/'))


@pytest.mark.parametrize(
    ('data', 'field', 'expected'),
    [
        ({'title': ''}, 'title', 'Це поле обов’язкове.'),
        ({'title': 'x' * 256}, 'title', 'Назва задовга (до 255 символів).'),
        ({'cost': ''}, 'cost', 'Вкажи ціну цілим числом.'),
        ({'cost': 'багато'}, 'cost', 'Вкажи ціну цілим числом.'),
        ({'cost': '1.5'}, 'cost', 'Вкажи ціну цілим числом.'),
        ({'cost': '0'}, 'cost', 'Ціна має бути щонайменше 1 монета.'),
        ({'cost': '-5'}, 'cost', 'Ціна має бути щонайменше 1 монета.'),
        ({'cost': '100001'}, 'cost', 'Ціна має бути не більшою за 100 000 монет.'),
        ({'description': 'x' * 10_001}, 'description', 'Опис задовгий.'),
    ],
)
def test_validation_errors_stand_next_to_their_field_in_ukrainian(
    logged_in,
    user,
    data,
    field,
    expected,
):
    response = create_reward(logged_in, **data)

    assert response.status_code == 200
    assert field_error(text(response), field) == expected
    assert not Reward.objects.filter(user=user).exists()


def test_a_refused_form_keeps_what_was_typed(logged_in):
    body = text(create_reward(logged_in, title='Кіно', description='Без телефона', cost='0'))

    assert 'value="Кіно"' in body
    assert '>Без телефона</textarea>' in body
    assert 'value="0"' in body


def test_the_reward_quota_is_the_services_and_says_so(logged_in, user, settings):
    settings.QUESTIFY_MAX_REWARDS_PER_USER = 1
    make_reward(user)

    response = create_reward(logged_in, title='Second')

    assert response.status_code == 200
    assert (
        'role="alert">Досягнуто ліміту нагород для цього акаунта.<' in text(response)
    )
    assert Reward.objects.filter(user=user).count() == 1


def test_a_new_reward_is_announced_and_escaped(logged_in):
    create_reward(logged_in, title='<b>Bold</b>')

    page = text(logged_in.get('/rewards/'))

    assert '<b>Bold</b>' not in page
    assert 'Нагороду «&lt;b&gt;Bold&lt;/b&gt;» створено.' in page


def edit_reward(client, reward, pairs=None, **data):
    fields = {'title': reward.title, 'description': reward.description, 'cost': reward.cost}
    return client.post(
        f'/rewards/{reward.pk}/edit/',
        urlencode(pairs if pairs is not None else {**fields, **data}),
        content_type=FORM,
    )


def test_the_edit_form_shows_the_reward(logged_in, user):
    reward = make_reward(user, description='Без телефона')

    assert f'href="/rewards/{reward.pk}/edit/"' in text(logged_in.get('/rewards/'))
    body = text(logged_in.get(f'/rewards/{reward.pk}/edit/'))

    assert '<h1 class="title">Редагування нагороди</h1>' in body
    assert 'value="Film night"' in body
    assert '>Без телефона</textarea>' in body
    assert 'value="40"' in body
    assert body.index('type="hidden" name="is_active"') < body.index('type="checkbox"')


def test_editing_goes_through_the_service_and_returns_to_the_list(logged_in, user):
    reward = make_reward(user)

    with patch(
        'web.forms.RewardService.update_reward',
        wraps=RewardService.update_reward,
    ) as service:
        response = edit_reward(logged_in, reward, title='Кіно', cost='55')

    assert response.status_code == 302
    assert response['Location'] == '/rewards/'
    assert service.call_args.args == (user, reward.pk)
    reward.refresh_from_db()
    assert (reward.title, reward.cost) == ('Кіно', 55)
    assert 'Нагороду «Кіно» збережено.' in text(logged_in.get('/rewards/'))


def test_a_box_left_unchecked_turns_the_reward_off_and_a_checked_one_on(logged_in, user):
    reward = make_reward(user)
    fields = [('title', 'Film night'), ('description', ''), ('cost', '40')]

    edit_reward(logged_in, reward, pairs=[*fields, ('is_active', 'false')])
    reward.refresh_from_db()
    assert not reward.is_active

    edit_reward(
        logged_in,
        reward,
        pairs=[*fields, ('is_active', 'false'), ('is_active', 'true')],
    )
    reward.refresh_from_db()
    assert reward.is_active


def test_a_new_price_does_not_rewrite_the_history(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    redeem(logged_in, reward)

    edit_reward(logged_in, reward, title='Кіно', cost='5')

    body = text(logged_in.get('/rewards/'))
    assert 'history__title">Обмін: Film night<' in body
    assert 'history__amount">−40<' in body


def test_an_edit_form_left_open_while_the_reward_was_archived_is_a_404(logged_in, user):
    reward = make_reward(user)
    with patch(
        'web.forms.RewardService.update_reward',
        side_effect=NotFoundError('Not found.', code='reward_not_found'),
    ):
        response = edit_reward(logged_in, reward, title='Кіно')

    assert response.status_code == 404


def test_an_archived_reward_cannot_be_opened_for_editing(logged_in, user):
    reward = make_reward(user)
    RewardService.archive_reward(user, reward.pk)

    assert logged_in.get(f'/rewards/{reward.pk}/edit/').status_code == 404


def test_the_forms_count_against_the_users_budget(logged_in, user):
    with patch('web.views.user_budget_wait', return_value=42):
        response = create_reward(logged_in)

    assert response.status_code == 429
    assert response['Retry-After'] == '42'
    assert 'Забагато дій' in text(response)
    assert not Reward.objects.filter(user=user).exists()


def test_the_reward_forms_need_the_csrf_token(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    assert create_reward(client).status_code == 403


# -- rewards: archiving ------------------------------------------------------------


def test_archiving_goes_through_a_post_and_takes_the_card_away(logged_in, user):
    reward = make_reward(user)
    make_reward(user, title='Other')

    response = htmx_post_form(logged_in, f'/rewards/{reward.pk}/archive/')

    assert response.status_code == 200
    body = text(response)
    assert 'id="reward-browser"' in body
    assert 'reward-card__title' in body
    assert 'toast--info">В архіві: Film night<' in body
    assert 'Film night' not in body.split('toast')[0]
    reward.refresh_from_db()
    assert reward.archived_at is not None
    assert not reward.is_active
    assert 'Film night' not in text(logged_in.get('/rewards/'))


def test_archiving_the_last_reward_says_the_list_is_empty(logged_in, user):
    reward = make_reward(user)

    body = text(htmx_post_form(logged_in, f'/rewards/{reward.pk}/archive/'))

    assert 'Нагород поки немає' in body


def test_archiving_asks_first_since_there_is_no_way_back(logged_in, user):
    make_reward(user)

    card = card_html(text(logged_in.get('/rewards/')), 'Film night')

    assert 'hx-confirm="Перенести нагороду в архів? Повернути її звідти не можна."' in card


def test_without_javascript_archiving_redirects_and_says_so(logged_in, user):
    reward = make_reward(user)

    response = post(logged_in, f'/rewards/{reward.pk}/archive/')

    assert response.status_code == 302
    assert response['Location'] == '/rewards/'
    assert 'В архіві: Film night' in text(logged_in.get('/rewards/'))


def test_archiving_twice_answers_that_the_reward_is_gone(logged_in, user):
    reward = make_reward(user)
    assert htmx_post_form(logged_in, f'/rewards/{reward.pk}/archive/').status_code == 200

    response = htmx_post_form(logged_in, f'/rewards/{reward.pk}/archive/')

    assert response.status_code == 404
    assert 'Нагороду не знайдено' in text(response)


def test_an_archived_reward_still_counts_toward_the_quota(logged_in, user, settings):
    settings.QUESTIFY_MAX_REWARDS_PER_USER = 1
    reward = make_reward(user)
    htmx_post_form(logged_in, f'/rewards/{reward.pk}/archive/')

    response = create_reward(logged_in, title='Second')

    assert 'Досягнуто ліміту нагород' in text(response)


def test_archiving_keeps_what_was_redeemed_in_the_history(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    redeem(logged_in, reward)

    body = text(htmx_post_form(logged_in, f'/rewards/{reward.pk}/archive/'))

    assert 'history__title">Обмін: Film night<' in body
    assert coins_of(user) == 60


def test_archiving_counts_against_the_users_budget(logged_in, user):
    reward = make_reward(user)

    with patch('web.views.user_budget_wait', return_value=42):
        response = htmx_post_form(logged_in, f'/rewards/{reward.pk}/archive/')

    assert response.status_code == 429
    assert 'Забагато дій' in card_html(text(response), 'Film night')
    reward.refresh_from_db()
    assert reward.archived_at is None


# -- rewards: the history ----------------------------------------------------------


def add_transaction(user, kind='quest_reward', amount=10, **fields):
    Character.objects.filter(user=user).update(coins=F('coins') + amount)
    return WalletTransaction.objects.create(
        user=user,
        kind=kind,
        amount=amount,
        balance_after=coins_of(user),
        **fields,
    )


def history_titles(body):
    return re.findall(r'history__title">([^<]*)<', body)


def history_more_url(body):
    return re.search(r'<li class="history__more"[^>]*><a[^>]*href="([^"]+)"', body)[1]


@pytest.mark.parametrize(
    ('kind', 'amount', 'label', 'shown'),
    [
        ('quest_reward', 25, 'Виконаний квест', '+25'),
        ('daily_bonus', 25, 'Бонус дня', '+25'),
        ('boss_reward', 75, 'Переможений бос', '+75'),
        ('initial_grant', 300, 'Початковий баланс', '+300'),
    ],
)
def test_each_kind_of_operation_has_its_text_and_sign(
    logged_in,
    user,
    kind,
    amount,
    label,
    shown,
):
    add_transaction(user, kind, amount)

    body = text(logged_in.get('/rewards/'))

    assert history_titles(body) == [label]
    assert f'history__amount history__amount--earned">{shown}<' in body
    assert f'баланс {amount}<' in body


def test_a_redemption_is_a_minus_with_its_title_at_the_time(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    redeem(logged_in, reward)
    RewardService.update_reward(user, reward.pk, title='Renamed')

    body = text(logged_in.get('/rewards/'))

    assert history_titles(body) == ['Обмін: Film night']
    assert 'history__amount">−40<' in body
    assert 'баланс 60<' in body


def test_a_redemption_row_gone_from_the_ledger_still_reads(logged_in, user):
    give_coins(user, 100)
    reward = make_reward(user)
    redeem(logged_in, reward)
    RewardRedemption.objects.all().delete()

    assert history_titles(text(logged_in.get('/rewards/'))) == ['Обмін']


def test_the_history_shows_the_time_in_the_users_timezone(logged_in, user):
    Character.objects.filter(user=user).update(timezone='Europe/Kyiv')
    add_transaction(user, created_at=datetime.fromisoformat('2026-03-14T23:30:00+00:00'))

    assert '15 березня 2026, 01:30' in text(logged_in.get('/rewards/'))


def test_the_history_is_of_this_user_alone(logged_in, stranger):
    add_transaction(stranger, 'daily_bonus')

    assert history_titles(text(logged_in.get('/rewards/'))) == []


def test_the_history_goes_newest_first_and_shows_more_page_by_page(logged_in, user):
    first = timezone.now() - timedelta(hours=100)
    for index in range(TRANSACTION_PAGE_SIZE + 5):
        add_transaction(user, amount=index + 1, created_at=first + timedelta(hours=index))

    page = text(logged_in.get('/rewards/'))

    assert len(history_titles(page)) == TRANSACTION_PAGE_SIZE
    amounts = re.findall(r'history__amount--earned">\+(\d+)<', page)
    assert amounts[0] == str(TRANSACTION_PAGE_SIZE + 5)
    assert '>Показати ще<' in page

    more = logged_in.get(
        history_more_url(page),
        headers={'HX-Request': 'true', 'HX-Target': 'history-more'},
    )

    assert more.status_code == 200
    fragment = text(more)
    assert '<html' not in fragment
    assert 'id="reward-browser"' not in fragment
    assert re.findall(r'history__amount--earned">\+(\d+)<', fragment) == [
        '5', '4', '3', '2', '1',
    ]
    assert '>Показати ще<' not in fragment


def test_show_more_is_absent_on_an_exact_page(logged_in, user):
    for _ in range(TRANSACTION_PAGE_SIZE):
        add_transaction(user)

    assert '>Показати ще<' not in text(logged_in.get('/rewards/'))


def test_show_more_replaces_itself_without_touching_the_address(logged_in, user):
    for _ in range(TRANSACTION_PAGE_SIZE + 1):
        add_transaction(user)

    link = re.search(
        r'<a[^>]*>Показати ще<',
        text(logged_in.get('/rewards/')),
    )[0]

    assert 'hx-boost="true"' in link
    assert 'hx-target="#history-more"' in link
    assert 'hx-push-url="false"' in link
    assert 'hx-sync="this:drop"' in link


def test_coins_earned_meanwhile_do_not_repeat_a_row(logged_in, user):
    start = timezone.now() - timedelta(hours=100)
    for index in range(TRANSACTION_PAGE_SIZE + 2):
        add_transaction(user, amount=index + 1, created_at=start + timedelta(hours=index))
    page = text(logged_in.get('/rewards/'))
    add_transaction(user, amount=999)

    fragment = text(logged_in.get(
        history_more_url(page),
        headers={'HX-Request': 'true', 'HX-Target': 'history-more'},
    ))

    assert re.findall(r'history__amount--earned">\+(\d+)<', fragment) == ['2', '1']


def test_show_more_works_without_javascript(logged_in, user):
    for _ in range(TRANSACTION_PAGE_SIZE + 1):
        add_transaction(user)

    page = text(logged_in.get(history_more_url(text(logged_in.get('/rewards/')))))

    assert '<html' in page
    assert len(history_titles(page)) == 1
    assert '>До початку історії<' in page
    assert 'id="reward-browser"' in page


@pytest.mark.parametrize('cursor', ['abc', '-1', '٣', '9' * 40, ''])
def test_a_malformed_cursor_reads_as_none(logged_in, user, cursor):
    add_transaction(user)

    response = logged_in.get('/rewards/', {'before': cursor})

    assert response.status_code == 200
    assert len(history_titles(text(response))) == 1


def test_someone_elses_row_as_a_cursor_reads_as_none(logged_in, user, stranger):
    add_transaction(user)
    theirs = add_transaction(stranger)

    body = text(logged_in.get('/rewards/', {'before': theirs.pk}))

    assert len(history_titles(body)) == 1


def test_a_page_takes_the_same_queries_for_any_number_of_rows(logged_in, user):
    def queries_for(url='/rewards/', **kwargs):
        with CaptureQueriesContext(connection) as queries:
            response = logged_in.get(url, **kwargs)
        assert response.status_code == 200
        return len(queries)

    def populate(prefix, count):
        give_coins(user, 1000)
        for index in range(count):
            reward = make_reward(user, title=f'{prefix} {index}', cost=1)
            WalletService.redeem_reward(user, reward.pk)

    populate('A', 2)
    few = queries_for()
    populate('B', 25)
    many = queries_for()
    fragment = queries_for(
        history_more_url(text(logged_in.get('/rewards/'))),
        headers={'HX-Request': 'true', 'HX-Target': 'history-more'},
    )

    # Session, user, the middleware's timezone, the balance, the rewards, the history.
    assert few == many == 6
    # No balance and no rewards; the cursor's row and the rows themselves.
    assert fragment == 5


def test_redeeming_takes_a_bounded_number_of_queries(logged_in, user):
    give_coins(user, 1000)
    rewards = [make_reward(user, title=f'R{index}', cost=1) for index in range(12)]
    token = form_token_of(logged_in, rewards[0])
    with CaptureQueriesContext(connection) as few:
        redeem(logged_in, rewards[0], token)

    for index in range(12, 30):
        make_reward(user, title=f'R{index}', cost=1)
    token = form_token_of(logged_in, rewards[1])
    with CaptureQueriesContext(connection) as many:
        redeem(logged_in, rewards[1], token)

    assert len(few) == len(many)


# -- bosses ------------------------------------------------------------------------


def make_boss(user, title='Dragon', steps=0, **fields):
    """A boss and the quests attached to it, through the service."""
    boss = BossService.create_boss(user, title=title, **fields)
    quests = [make_quest(user, f'{title} step {index}') for index in range(steps)]
    for quest in quests:
        BossService.attach_step(user, boss.pk, quest.pk)
    return boss, quests


def beat(user, quests):
    for quest in quests:
        QuestService.complete_quest(quest.pk, user)


def boss_card_html(body, title):
    """One boss's card, up to where the next one starts."""
    for part in body.split('<article class="boss-card')[1:]:
        if re.search(rf'boss-card__title"[^>]*>{re.escape(title)}<', part):
            return part.split('</article>')[0]
    raise AssertionError(f'{title} is not on the page')


def step_html(card, title):
    """One step's row of a card."""
    for part in card.split('<li class="boss-step')[1:]:
        if f'boss-step__title">{title}<' in part:
            return part.split('</li>')[0]
    raise AssertionError(f'{title} is not on the card')


def attach(client, boss_id, quest_id):
    return htmx_post_form(client, f'/bosses/{boss_id}/steps/', quest=quest_id)


def detach(client, boss_id, quest_id):
    return htmx_post_form(client, f'/bosses/{boss_id}/steps/{quest_id}/detach/')


def archive_boss(client, boss_id):
    return htmx_post_form(client, f'/bosses/{boss_id}/archive/')


def new_boss(client, *, quests=(), **data):
    return client.post(
        '/bosses/new/',
        urlencode({'title': 'Dragon', **data, 'quests': list(quests)}, doseq=True),
        content_type=FORM,
    )


def put_step_in_state(user, quest, state):
    if state == 'completed':
        QuestService.complete_quest(quest.pk, user)
    elif state == 'failed':
        QuestService.fail_quest(quest.pk, user)
    elif state == 'expired':
        Quest.objects.filter(pk=quest.pk).update(
            deadline=timezone.now() - timedelta(hours=1),
        )
    elif state == 'inactive':
        Quest.objects.filter(pk=quest.pk).update(is_active=False)


def test_the_navigation_has_six_items_with_bosses_after_the_quests(logged_in):
    body = text(logged_in.get('/bosses/'))

    assert re.findall(r'class="nav__link[^"]*" href="[^"]+"[^>]*>([^<]+)<', body) == [
        'Сьогодні',
        'Усі квести',
        'Боси',
        'Навички',
        'Ачівки',
        'Нагороди',
    ]
    assert body.count('aria-current="page"') == 1
    assert 'nav__link--active" href="/bosses/" aria-current="page">Боси<' in body


@pytest.mark.django_db
@pytest.mark.parametrize('path', ['/bosses/', '/bosses/new/'])
def test_bosses_need_a_login(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert response['Location'] == f'/login/?next={path}'


# -- bosses: the list --------------------------------------------------------------


def test_the_screen_says_when_there_are_no_bosses(logged_in):
    body = text(logged_in.get('/bosses/'))

    assert '<h1 class="title">Боси</h1>' in body
    assert 'Босів поки немає' in body
    assert 'href="/bosses/new/"' in body
    assert 'Переможені' not in body


def test_a_boss_shows_its_hp_bar_and_the_state_of_each_step(logged_in, user):
    _, quests = make_boss(user, steps=3, description='The big one')
    QuestService.complete_quest(quests[0].pk, user)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'The big one' in card
    assert 'HP 2 з 3' in card
    assert re.search(r'<progress[^>]*value="2" max="3"', card)
    assert 'tag--completed">Виконано<' in step_html(card, 'Dragon step 0')
    assert 'tag--open">Відкрито<' in step_html(card, 'Dragon step 1')


@pytest.mark.parametrize(
    ('state', 'tag', 'blocks'),
    [
        ('open', 'tag--open">Відкрито<', False),
        ('completed', 'tag--completed">Виконано<', False),
        ('failed', 'tag--failed">Не вдалось<', True),
        ('expired', 'tag--expired">Дедлайн минув<', True),
        ('inactive', 'tag--inactive">Вимкнено<', True),
    ],
)
def test_every_state_of_a_step_is_named_and_says_if_it_blocks(
    logged_in,
    user,
    state,
    tag,
    blocks,
):
    _, quests = make_boss(user, steps=1)
    put_step_in_state(user, quests[0], state)

    row = step_html(
        boss_card_html(text(logged_in.get('/bosses/')), 'Dragon'),
        'Dragon step 0',
    )

    assert tag in row
    assert ('Блокує перемогу' in row) is blocks


def test_a_failed_step_says_how_to_stop_it_blocking(logged_in, user):
    _, quests = make_boss(user, steps=4)
    put_step_in_state(user, quests[0], 'failed')

    row = step_html(
        boss_card_html(text(logged_in.get('/bosses/')), 'Dragon'),
        'Dragon step 0',
    )

    assert 'Блокує перемогу: відкріпи цей крок.' in row
    assert 'Відкріпити: Dragon step 0' in row


@pytest.mark.parametrize('how', ['archived', 'made daily'])
def test_a_step_that_no_longer_counts_says_so_and_leaves_the_hp(logged_in, user, how):
    _, quests = make_boss(user, steps=3)
    if how == 'archived':
        QuestService.archive_quest(user, quests[0].pk)
    else:
        Quest.objects.filter(pk=quests[0].pk).update(quest_type=DAILY)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'HP 2 з 2' in card
    row = step_html(card, 'Dragon step 0')
    assert row.startswith(' boss-step--off')
    assert 'Не рахується в HP' in row
    assert 'Блокує' not in row


@pytest.mark.parametrize(
    ('steps', 'hint'),
    [
        (0, 'Додай ще 3 кроки, щоб бос міг бути переможений'),
        (1, 'Додай ще 2 кроки, щоб бос міг бути переможений'),
        (2, 'Додай ще 1 крок, щоб бос міг бути переможений'),
    ],
)
def test_a_boss_with_too_few_steps_says_how_many_more_it_needs(
    logged_in,
    user,
    steps,
    hint,
):
    make_boss(user, steps=steps)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert f'boss-card__hint">{hint}<' in card
    assert 'tag--xp' not in card
    assert 'Для перемоги потрібно' not in card


@pytest.mark.parametrize('steps', [3, 4])
def test_a_boss_with_enough_steps_has_no_hint(logged_in, user, steps):
    make_boss(user, steps=steps)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'Додай ще' not in card


def test_the_hint_stands_under_the_hp_bar(logged_in, user):
    make_boss(user, steps=2)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert (
        card.index('<progress')
        < card.index('Додай ще')
        < card.index('class="boss-steps"')
    )


def test_the_hint_counts_only_the_steps_that_count(logged_in, user):
    _, quests = make_boss(user, steps=3)
    QuestService.archive_quest(user, quests[0].pk)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'HP 2 з 2' in card
    assert 'Додай ще 1 крок,' in card


def test_the_hint_follows_the_setting(logged_in, user, settings):
    settings.QUESTIFY_BOSS_MIN_STEPS = 5
    make_boss(user)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'Додай ще 5 кроків,' in card


def test_a_defeated_boss_has_no_hint(logged_in, user, settings):
    _, quests = make_boss(user, steps=3)
    beat(user, quests)
    # Fewer steps than the minimum asks for now, but this one is beaten.
    settings.QUESTIFY_BOSS_MIN_STEPS = 5

    assert 'Додай ще' not in boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')


def test_a_boss_with_enough_steps_reads_out_what_beating_it_pays(logged_in, user):
    make_boss(user, steps=4)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'tag--xp">+100 XP<' in card


def test_a_boss_with_no_steps_has_no_bar(logged_in, user):
    make_boss(user)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert '<progress' not in card
    assert 'Кроків ще немає' in card


def test_a_boss_left_at_zero_hp_says_only_a_completion_beats_it(logged_in, user):
    boss, quests = make_boss(user, steps=4)
    beat(user, quests[:3])
    BossService.detach_step(user, boss.pk, quests[3].pk)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'HP 0 з 3' in card
    assert 'Перемагає лише виконання кроку' in card
    assert not BossDefeat.objects.exists()


def test_a_defeated_boss_is_frozen_on_the_screen(logged_in, user):
    _, quests = make_boss(user, steps=3)
    beat(user, quests)
    make_quest(user, 'Free')

    body = text(logged_in.get('/bosses/'))

    card = boss_card_html(body, 'Dragon')
    assert 'tag--completed">Переможено<' in card
    assert 'HP 0 з 3' in card
    assert '+75 XP, +75 монет' in card
    assert 'name="quest"' not in card
    assert 'Відкріпити' not in card
    assert 'В архів: Dragon' in card


def test_bosses_in_progress_come_before_the_defeated(logged_in, user):
    _, quests = make_boss(user, 'Beaten', steps=3)
    beat(user, quests)
    make_boss(user, 'Fighting')

    body = text(logged_in.get('/bosses/'))

    assert (
        body.index('>Fighting<')
        < body.index('>Переможені<')
        < body.index('>Beaten<')
    )


def test_only_the_users_own_bosses_are_listed(logged_in, stranger):
    make_boss(stranger, 'Their secret', steps=1)

    body = text(logged_in.get('/bosses/'))

    assert 'Their secret' not in body
    assert 'Босів поки немає' in body


def test_an_archived_boss_is_not_listed(logged_in, user):
    boss, _ = make_boss(user)
    BossService.archive_boss(user, boss.pk)

    assert 'Dragon' not in text(logged_in.get('/bosses/'))


def test_titles_are_escaped_everywhere_a_boss_shows(logged_in, user):
    boss, _ = make_boss(user, '<i>boss</i>', description='<i>about</i>')
    quest = make_quest(user, '<i>step</i>')
    BossService.attach_step(user, boss.pk, quest.pk)
    make_quest(user, '<i>free</i>')

    for body in (
        text(logged_in.get('/bosses/')),
        text(logged_in.get('/bosses/new/')),
        text(attach(logged_in, boss.pk, make_quest(user, '<i>more</i>').pk)),
    ):
        assert '<i>' not in body


def test_the_step_list_offers_only_quests_free_to_become_steps(logged_in, user):
    make_boss(user, steps=1)
    free = make_quest(user, 'Free')
    make_quest(user, 'Daily', DAILY)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert re.findall(r'<option value="(\d+)">', card) == [str(free.pk)]


def test_with_no_free_quest_the_card_offers_to_create_one(logged_in, user):
    make_boss(user)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'name="quest"' not in card
    assert 'Вільних разових квестів немає' in card
    assert 'href="/quests/new/?next=/bosses/"' in card


def test_a_boss_at_its_step_limit_offers_no_more(logged_in, user, settings):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2
    make_boss(user, steps=2)
    make_quest(user, 'Free')

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'name="quest"' not in card
    assert 'максимум 2' in card


def test_the_screen_takes_the_same_queries_however_many_bosses(logged_in, user):
    def queries_for_page():
        with CaptureQueriesContext(connection) as queries:
            assert logged_in.get('/bosses/').status_code == 200
        return len(queries)

    make_boss(user, 'First', steps=2)
    make_quest(user, 'Free')
    few = queries_for_page()
    _, quests = make_boss(user, 'Second', steps=3)
    beat(user, quests)
    make_boss(user, 'Third', steps=3)
    make_quest(user, 'Free too')
    many = queries_for_page()

    # Session, user, the middleware's timezone, the header's coins, the bosses, their
    # steps, their defeats, the free quests, the categories of the new-step form.
    assert few == many == 9


# -- bosses: creating --------------------------------------------------------------


def test_the_form_offers_only_quests_free_to_become_steps(logged_in, user, stranger):
    make_boss(user, 'Taken', steps=1)
    make_quest(user, 'Free')
    make_quest(user, 'Daily', DAILY)
    make_quest(user, 'Off', is_active=False)
    make_quest(user, 'Gone', is_active=False, archived_at=timezone.now())
    make_quest(user, 'Late', deadline=timezone.now() - timedelta(hours=1))
    QuestService.complete_quest(make_quest(user, 'Done').pk, user)
    QuestService.fail_quest(make_quest(user, 'Failed').pk, user)
    make_quest(stranger, 'Theirs')

    body = text(logged_in.get('/bosses/new/'))

    assert re.findall(r'choice__label">([^<]+)<', body) == ['Free']


def test_the_form_says_how_many_steps_a_boss_takes(logged_in):
    body = text(logged_in.get('/bosses/new/'))

    assert 'щонайменше 3 кроки' in body
    assert 'до 10' in body


def test_with_no_free_quest_the_form_offers_to_create_one(logged_in):
    body = text(logged_in.get('/bosses/new/'))

    assert 'name="quests"' not in body
    assert 'href="/quests/new/?next=/bosses/new/"' in body


def test_creating_a_boss_attaches_the_quests_ticked(logged_in, user):
    first = make_quest(user, 'First')
    second = make_quest(user, 'Second')
    make_quest(user, 'Left alone')

    response = new_boss(
        logged_in,
        title='Dragon',
        description='Big',
        quests=[first.pk, second.pk],
    )

    assert response.status_code == 302
    assert response['Location'] == '/bosses/'
    boss = Boss.objects.get(user=user)
    assert (boss.title, boss.description) == ('Dragon', 'Big')
    assert set(boss.steps.values_list('quest_id', flat=True)) == {first.pk, second.pk}
    body = text(logged_in.get('/bosses/'))
    assert 'Боса «Dragon» створено.' in body
    assert 'HP 2 з 2' in body


def test_a_boss_can_be_created_with_no_steps(logged_in, user):
    assert new_boss(logged_in).status_code == 302

    assert Boss.objects.get(user=user).steps.count() == 0


def test_creating_goes_through_the_service(logged_in, user):
    quest = make_quest(user)

    with patch(
        'web.forms.BossService.create_boss_with_steps',
        wraps=BossService.create_boss_with_steps,
    ) as service:
        new_boss(logged_in, quests=[quest.pk])

    service.assert_called_once_with(
        user,
        quest_ids=[quest.pk],
        new_steps=[],
        title='Dragon',
    )


def test_a_blank_title_is_refused_and_the_form_keeps_what_was_sent(logged_in, user):
    quest = make_quest(user, 'Ticked')
    make_quest(user, 'Not ticked')

    response = new_boss(logged_in, title='  ', description='Typed', quests=[quest.pk])

    assert response.status_code == 200
    body = text(response)
    assert 'Це поле обов’язкове.' in body
    assert 'Typed' in body
    assert re.search(rf'value="{quest.pk}" checked', body)
    assert body.count(' checked') == 1
    assert not Boss.objects.exists()


def test_a_quest_refused_takes_the_new_boss_back_with_it(logged_in, user):
    fine = make_quest(user, 'Fine')
    done = make_quest(user, 'Done meanwhile')
    QuestService.complete_quest(done.pk, user)

    response = new_boss(logged_in, title='Typed', quests=[fine.pk, done.pk])

    assert response.status_code == 200
    body = text(response)
    assert 'Цей квест уже виконано або провалено.' in body
    assert 'value="Typed"' in body
    assert re.search(rf'value="{fine.pk}" checked', body)
    assert not Boss.objects.exists()
    assert not BossStep.objects.exists()


def test_a_foreign_quest_reads_like_a_missing_one(logged_in, stranger):
    theirs = make_quest(stranger, 'Their secret')

    foreign = text(new_boss(logged_in, quests=[theirs.pk]))
    missing = text(new_boss(logged_in, quests=[987654]))

    for body in (foreign, missing):
        assert 'Квест не знайдено.' in body
        assert 'Their secret' not in body
    assert not Boss.objects.exists()
    assert not BossStep.objects.exists()


@pytest.mark.parametrize('value', ['abc', '0', '-1', '1.5', str(2 ** 63)])
def test_a_malformed_quest_id_in_the_form_is_refused(logged_in, value):
    response = new_boss(logged_in, quests=[value])

    assert response.status_code == 200
    assert 'Обери квест зі списку.' in text(response)
    assert not Boss.objects.exists()


def test_more_quests_than_a_boss_takes_are_refused(logged_in, user, settings):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2
    quests = [make_quest(user, f'Q{index}') for index in range(3)]

    response = new_boss(logged_in, quests=[quest.pk for quest in quests])

    assert 'У боса вже максимум кроків: 2.' in text(response)
    assert not Boss.objects.exists()


@pytest.mark.parametrize(
    ('limit', 'expected'),
    [
        ('QUESTIFY_MAX_ACTIVE_BOSSES', 'Забагато босів у процесі'),
        ('QUESTIFY_MAX_BOSSES_PER_USER', 'Досягнуто ліміту босів'),
    ],
)
def test_the_boss_quotas_are_the_services_and_say_so(
    logged_in,
    user,
    settings,
    limit,
    expected,
):
    setattr(settings, limit, 1)
    make_boss(user, 'First')

    response = new_boss(logged_in, title='Typed')

    assert response.status_code == 200
    body = text(response)
    assert expected in body
    assert 'value="Typed"' in body
    assert Boss.objects.count() == 1


def test_creating_counts_against_the_users_budget(logged_in):
    with patch('web.views.user_budget_wait', return_value=42):
        response = new_boss(logged_in, title='Typed')

    assert response.status_code == 429
    assert response['Retry-After'] == '42'
    body = text(response)
    assert 'Забагато дій. Спробуй знову через 42 с.' in body
    assert 'value="Typed"' in body
    assert not Boss.objects.exists()


def test_the_form_needs_the_csrf_token(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    assert post(client, '/bosses/new/', title='Dragon').status_code == 403
    assert not Boss.objects.exists()


# -- bosses: attaching -------------------------------------------------------------


def test_attaching_answers_with_the_list_and_a_toast(logged_in, user):
    boss, _ = make_boss(user)
    make_boss(user, 'Other')
    quest = make_quest(user, 'Sword')

    response = attach(logged_in, boss.pk, quest.pk)

    assert response.status_code == 200
    body = text(response)
    assert '<html' not in body
    assert 'id="boss-list"' in body
    assert 'toast--boss">Крок додано: Sword<' in body
    card = boss_card_html(body, 'Dragon')
    assert 'HP 1 з 1' in card
    assert step_html(card, 'Sword')
    assert BossStep.objects.get().boss_id == boss.pk
    # Gone from what every boss, this one and the other, can still take.
    assert f'<option value="{quest.pk}"' not in body


def test_attaching_goes_through_the_service(logged_in, user):
    boss, _ = make_boss(user)
    quest = make_quest(user)

    with patch(
        'web.views.BossService.attach_step',
        wraps=BossService.attach_step,
    ) as service:
        assert attach(logged_in, boss.pk, quest.pk).status_code == 200

    service.assert_called_once_with(user, boss.pk, quest.pk)


def test_without_javascript_attaching_redirects_and_says_what_happened(logged_in, user):
    boss, _ = make_boss(user)
    quest = make_quest(user, 'Sword')

    response = post(logged_in, f'/bosses/{boss.pk}/steps/', quest=quest.pk)

    assert response.status_code == 302
    assert response['Location'] == '/bosses/'
    assert 'Крок додано: Sword' in text(logged_in.get('/bosses/'))
    assert BossStep.objects.count() == 1


def test_the_focus_moves_to_the_boss_acted_on(logged_in, user):
    boss, _ = make_boss(user)
    make_boss(user, 'Other')

    body = text(attach(logged_in, boss.pk, make_quest(user).pk))

    assert re.search(r'tabindex="-1" autofocus>Dragon<', body)
    assert body.count('autofocus') == 1
    assert 'autofocus' not in text(logged_in.get('/bosses/'))


@pytest.mark.parametrize(
    ('fields', 'expected'),
    [
        (lambda: {'quest_type': DAILY}, 'Кроком боса може бути лише разовий квест.'),
        (lambda: {'is_active': False}, 'Цей квест неактивний.'),
        (
            lambda: {'is_active': False, 'archived_at': timezone.now()},
            'Квест не знайдено.',
        ),
        (
            lambda: {'deadline': timezone.now() - timedelta(hours=1)},
            'Дедлайн цього квесту минув.',
        ),
    ],
)
def test_a_quest_that_cannot_be_a_step_is_refused_on_the_card(
    logged_in,
    user,
    fields,
    expected,
):
    boss, _ = make_boss(user)
    quest = make_quest(user, 'Bad', **fields())

    response = attach(logged_in, boss.pk, quest.pk)

    assert response.status_code == 400
    assert expected in boss_card_html(text(response), 'Dragon')
    assert not BossStep.objects.exists()


@pytest.mark.parametrize('how', ['completed', 'failed'])
def test_a_quest_already_done_is_refused_on_the_card(logged_in, user, how):
    boss, _ = make_boss(user)
    quest = make_quest(user, 'Bad')
    put_step_in_state(user, quest, how)

    response = attach(logged_in, boss.pk, quest.pk)

    assert response.status_code == 400
    assert 'Цей квест уже виконано або провалено.' in boss_card_html(
        text(response),
        'Dragon',
    )


def test_a_quest_of_another_boss_is_refused_on_the_card(logged_in, user):
    boss, _ = make_boss(user)
    _, quests = make_boss(user, 'Other', steps=1)

    response = attach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 400
    assert 'Цей квест уже є кроком боса.' in boss_card_html(text(response), 'Dragon')
    assert BossStep.objects.get().quest_id == quests[0].pk


def test_a_foreign_quest_is_refused_like_a_missing_one(logged_in, user, stranger):
    boss, _ = make_boss(user)
    theirs = make_quest(stranger, 'Their secret')

    foreign = attach(logged_in, boss.pk, theirs.pk)
    missing = attach(logged_in, boss.pk, 987654)

    for response in (foreign, missing):
        assert response.status_code == 400
        body = text(response)
        assert 'Квест не знайдено.' in boss_card_html(body, 'Dragon')
        assert 'Their secret' not in body
    assert not BossStep.objects.exists()


@pytest.mark.parametrize('value', ['abc', '0', str(2 ** 63)])
def test_a_malformed_quest_id_is_refused_on_the_card(logged_in, user, value):
    boss, _ = make_boss(user)

    response = attach(logged_in, boss.pk, value)

    assert response.status_code == 400
    assert 'Обери квест зі списку.' in boss_card_html(text(response), 'Dragon')


def test_the_step_limit_is_said_with_its_number(logged_in, user, settings):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2
    boss, _ = make_boss(user, steps=2)

    response = attach(logged_in, boss.pk, make_quest(user, 'One too many').pk)

    assert response.status_code == 400
    assert 'У боса вже максимум кроків: 2.' in boss_card_html(text(response), 'Dragon')


def test_a_defeated_boss_takes_no_more_steps_and_says_so(logged_in, user):
    boss, quests = make_boss(user, steps=3)
    beat(user, quests)

    response = attach(logged_in, boss.pk, make_quest(user, 'Late').pk)

    assert response.status_code == 409
    assert 'Боса переможено, його кроки не можна змінити.' in boss_card_html(
        text(response),
        'Dragon',
    )


def test_an_error_shows_no_internal_id(logged_in, user):
    Boss.objects.create(pk=987654, user=user, title='Dragon')
    quest = make_quest(user, 'Daily', DAILY, pk=987655)

    body = text(attach(logged_in, 987654, quest.pk))

    alert = re.search(r'role="alert">([^<]+)<', body)[1]
    assert alert == 'Кроком боса може бути лише разовий квест.'
    assert '98765' not in alert


# -- bosses: detaching -------------------------------------------------------------


def test_detaching_answers_with_the_list_and_gives_the_quest_back(logged_in, user):
    boss, quests = make_boss(user, steps=4)

    response = detach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 200
    body = text(response)
    assert 'toast--boss">Крок відкріплено: Dragon step 0<' in body
    card = boss_card_html(body, 'Dragon')
    assert 'HP 3 з 3' in card
    assert f'<option value="{quests[0].pk}"' in card
    assert sorted(boss.steps.values_list('quest_id', flat=True)) == [
        quest.pk for quest in quests[1:]
    ]


def test_detaching_goes_through_the_service(logged_in, user):
    boss, quests = make_boss(user, steps=4)

    with patch(
        'web.views.BossService.detach_step',
        wraps=BossService.detach_step,
    ) as service:
        assert detach(logged_in, boss.pk, quests[0].pk).status_code == 200

    service.assert_called_once_with(user, boss.pk, quests[0].pk)


def test_detaching_a_failed_step_stops_it_blocking(logged_in, user):
    boss, quests = make_boss(user, steps=4)
    put_step_in_state(user, quests[0], 'failed')
    assert 'Блокує перемогу' in text(logged_in.get('/bosses/'))

    body = text(detach(logged_in, boss.pk, quests[0].pk))

    assert 'Блокує перемогу' not in body
    assert 'HP 3 з 3' in body


def test_a_completed_step_has_no_button_and_is_refused_on_the_card(logged_in, user):
    boss, quests = make_boss(user, steps=2)
    beat(user, quests[:1])
    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')
    assert 'Відкріпити' not in step_html(card, 'Dragon step 0')
    assert 'Відкріпити' in step_html(card, 'Dragon step 1')

    response = detach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 409
    assert 'Виконаний крок не можна відкріпити.' in boss_card_html(
        text(response),
        'Dragon',
    )
    assert boss.steps.count() == 2


def test_a_step_of_a_defeated_boss_is_refused_on_the_card(logged_in, user):
    # Archived before the win, so it stays on the boss without having counted.
    boss, quests = make_boss(user, steps=4)
    QuestService.archive_quest(user, quests[3].pk)
    beat(user, quests[:3])
    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')
    assert 'Не рахувався в перемозі.' in step_html(card, 'Dragon step 3')
    assert 'Відкріпити' not in card

    response = detach(logged_in, boss.pk, quests[3].pk)

    assert response.status_code == 409
    assert 'Боса переможено, його кроки не можна змінити.' in boss_card_html(
        text(response),
        'Dragon',
    )


def test_a_step_detached_meanwhile_is_a_404_on_the_card(logged_in, user):
    boss, quests = make_boss(user, steps=4)
    BossService.detach_step(user, boss.pk, quests[0].pk)

    response = detach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 404
    assert 'Цього кроку вже немає' in boss_card_html(text(response), 'Dragon')


def test_a_step_of_another_boss_cannot_be_detached_through_this_one(logged_in, user):
    boss, _ = make_boss(user)
    other, quests = make_boss(user, 'Other', steps=1)

    response = detach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 404
    assert other.steps.count() == 1


# -- bosses: a new step from the card ----------------------------------------------


BOTH_TEXT = 'Або обери квест, або опиши новий крок, а не все разом.'
NEITHER_TEXT = 'Обери квест або введи назву нового кроку.'


def add_step(client, boss_id, **data):
    return htmx_post_form(client, f'/bosses/{boss_id}/steps/', **data)


def step_field_error(card, name='title'):
    """The error next to one input of the card's form, or None."""
    found = re.search(rf'id="boss-\d+_{name}_error">([^<]+)<', card)
    return found[1] if found else None


def test_a_new_step_is_created_and_attached(logged_in, user):
    boss, _ = make_boss(user)
    make_boss(user, 'Other')

    response = add_step(logged_in, boss.pk, title='Sword')

    assert response.status_code == 200
    body = text(response)
    assert '<html' not in body
    assert 'toast--boss">Крок додано: Sword<' in body
    card = boss_card_html(body, 'Dragon')
    assert 'HP 1 з 1' in card
    assert step_html(card, 'Sword')
    assert 'value="Sword"' not in card
    step = BossStep.objects.get()
    assert step.boss_id == boss.pk
    assert (step.quest.user, step.quest.title) == (user, 'Sword')
    assert (step.quest.quest_type, step.quest.xp_reward) == (ONE_TIME, 50)


def test_a_new_step_goes_through_the_service(logged_in, user):
    boss, _ = make_boss(user)

    with patch(
        'web.views.BossService.add_new_step',
        wraps=BossService.add_new_step,
    ) as service:
        assert add_step(logged_in, boss.pk, title='Sword').status_code == 200

    (who, boss_id, quest_data), _ = service.call_args
    assert (who, boss_id) == (user, boss.pk)
    assert quest_data['title'] == 'Sword'


def test_a_posted_type_or_state_does_not_reach_a_new_step(logged_in, user):
    boss, _ = make_boss(user)

    response = add_step(
        logged_in,
        boss.pk,
        title='Sword',
        quest_type=DAILY,
        is_active='false',
        description='Sneaked in',
    )

    assert response.status_code == 200
    quest = BossStep.objects.get().quest
    assert (quest.quest_type, quest.is_active, quest.description) == (ONE_TIME, True, None)


def test_a_new_step_takes_its_category_and_deadline_as_the_quest_form_does(logged_in, user):
    Character.objects.filter(user=user).update(timezone='Europe/Kyiv')
    boss, _ = make_boss(user)
    category = QuestCategory.objects.create(user=user, name='Study')

    response = add_step(
        logged_in,
        boss.pk,
        title='Sword',
        category=category.pk,
        deadline='2030-01-15T10:00',
    )

    assert response.status_code == 200
    quest = BossStep.objects.get().quest
    assert quest.category == category
    assert quest.deadline == datetime(2030, 1, 15, 8, 0, tzinfo=UTC)


def test_a_new_step_can_bring_a_new_category(logged_in, user):
    boss, _ = make_boss(user)

    assert add_step(
        logged_in,
        boss.pk,
        title='Sword',
        new_category='Study',
    ).status_code == 200

    assert BossStep.objects.get().quest.category.name == 'Study'


def test_the_form_offers_only_the_users_own_categories(logged_in, user, stranger):
    make_boss(user)
    QuestCategory.objects.create(user=user, name='Mine')
    QuestCategory.objects.create(user=stranger, name='Theirs')

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert '>Mine<' in card
    assert '>Theirs<' not in card


def test_every_card_has_the_form_under_ids_of_its_own(logged_in, user):
    make_boss(user)
    make_boss(user, 'Other')
    make_quest(user, 'Free')

    body = text(logged_in.get('/bosses/'))

    ids = re.findall(r'\bid="([^"]+)"', body)
    assert len(ids) == len(set(ids))
    assert 'for="boss-' in body
    assert body.count('>Або новий крок<') == 2


def test_with_no_free_quest_the_card_still_takes_a_new_step(logged_in, user):
    make_boss(user)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'name="quest"' not in card
    assert 'name="title"' in card
    assert '>Новий крок<' in card
    assert 'Стане разовим квестом на 50 XP.' in card


def test_neither_input_is_marked_required_since_either_may_stay_empty(logged_in, user):
    make_boss(user)
    make_quest(user, 'Free')

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    form = card.split('boss-card__attach"')[1].split('</form>')[0]
    assert ' required' not in form


def test_a_defeated_boss_has_no_step_form(logged_in, user):
    _, quests = make_boss(user, steps=3)
    beat(user, quests)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'name="title"' not in card
    assert 'name="new_category"' not in card


def test_a_boss_at_its_limit_has_no_step_form(logged_in, user, settings):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2
    make_boss(user, steps=2)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'name="title"' not in card


@pytest.mark.parametrize(
    'extra',
    [
        {'title': 'Sword'},
        {'new_category': 'Study'},
        {'deadline': '2030-01-15T10:00'},
    ],
)
def test_a_quest_and_a_new_step_together_are_refused_next_to_the_field(
    logged_in,
    user,
    extra,
):
    boss, _ = make_boss(user)
    free = make_quest(user, 'Free')

    response = add_step(logged_in, boss.pk, quest=free.pk, **extra)

    assert response.status_code == 400
    card = boss_card_html(text(response), 'Dragon')
    assert step_field_error(card) == BOTH_TEXT
    # Both stay, so the user clears the one not meant.
    assert f'<option value="{free.pk}" selected>' in card
    assert f'value="{next(iter(extra.values()))}"' in card
    assert not BossStep.objects.exists()
    assert Quest.objects.count() == 1
    assert not QuestCategory.objects.exists()


@pytest.mark.parametrize(
    'data',
    [{}, {'title': '   '}, {'quest': ''}, {'quest': ' ', 'title': ''}],
)
def test_no_quest_and_no_title_are_refused_next_to_the_field(logged_in, user, data):
    boss, _ = make_boss(user)

    response = add_step(logged_in, boss.pk, **data)

    assert response.status_code == 400
    assert step_field_error(boss_card_html(text(response), 'Dragon')) == NEITHER_TEXT
    assert not Quest.objects.exists()


def test_a_deadline_without_a_title_asks_for_the_title(logged_in, user):
    boss, _ = make_boss(user)

    response = add_step(logged_in, boss.pk, deadline='2030-01-15T10:00')

    assert response.status_code == 400
    card = boss_card_html(text(response), 'Dragon')
    assert step_field_error(card) == 'Це поле обов’язкове.'
    assert 'value="2030-01-15T10:00"' in card
    assert 'boss-card__more" open' in card
    assert not Quest.objects.exists()


def test_the_more_fields_stay_closed_until_they_hold_something(logged_in, user):
    make_boss(user)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'boss-card__more">' in card


@pytest.mark.parametrize(
    ('data', 'field', 'expected'),
    [
        ({'title': 'x' * 256}, 'title', 'Назва задовга (до 255 символів).'),
        (
            {'title': 'Sword', 'deadline': '2001-01-01T10:00'},
            'deadline',
            'Дедлайн має бути в майбутньому.',
        ),
        (
            {'title': 'Sword', 'deadline': 'soon'},
            'deadline',
            'Вкажи дату й час дедлайну.',
        ),
        (
            {'title': 'Sword', 'new_category': 'x' * 101},
            'new_category',
            'Назва категорії задовга (до 100 символів).',
        ),
    ],
)
def test_the_serializers_rules_are_said_next_to_their_input(
    logged_in,
    user,
    data,
    field,
    expected,
):
    boss, _ = make_boss(user)

    response = add_step(logged_in, boss.pk, **data)

    assert response.status_code == 400
    assert step_field_error(boss_card_html(text(response), 'Dragon'), field) == expected
    assert not Quest.objects.exists()


@pytest.mark.parametrize('how', ['foreign', 'missing'])
def test_a_category_not_on_offer_is_refused_without_its_id(logged_in, user, stranger, how):
    boss, _ = make_boss(user)
    value = '987654'
    if how == 'foreign':
        QuestCategory.objects.create(pk=int(value), user=stranger, name='Theirs')

    response = add_step(logged_in, boss.pk, title='Sword', category=value)

    assert response.status_code == 400
    card = boss_card_html(text(response), 'Dragon')
    assert step_field_error(card, 'category') == DOMAIN_ERRORS['category_gone']
    assert card.count('987654') == 0
    assert 'Theirs' not in card
    assert not Quest.objects.exists()


def test_past_the_quest_quota_a_new_step_leaves_nothing(logged_in, user, settings):
    settings.QUESTIFY_MAX_QUESTS_PER_USER = 0
    boss, _ = make_boss(user)

    response = add_step(logged_in, boss.pk, title='Sword', new_category='Study')

    assert response.status_code == 400
    card = boss_card_html(text(response), 'Dragon')
    assert f'role="alert">{DOMAIN_ERRORS["quest_limit_reached"]}<' in card
    assert 'value="Sword"' in card
    assert 'value="Study"' in card
    assert not Quest.objects.exists()
    assert not QuestCategory.objects.exists()
    assert not BossStep.objects.exists()


def test_past_the_category_quota_the_error_stands_by_the_new_category(
    logged_in,
    user,
    settings,
):
    settings.QUESTIFY_MAX_CATEGORIES_PER_USER = 0
    boss, _ = make_boss(user)

    response = add_step(logged_in, boss.pk, title='Sword', new_category='Study')

    assert response.status_code == 400
    card = boss_card_html(text(response), 'Dragon')
    assert (
        step_field_error(card, 'new_category')
        == DOMAIN_ERRORS['category_limit_reached']
    )
    assert not Quest.objects.exists()


def test_past_the_step_limit_a_new_step_leaves_nothing(logged_in, user, settings):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2
    boss, _ = make_boss(user, steps=2)

    response = add_step(logged_in, boss.pk, title='Sword', new_category='Study')

    assert response.status_code == 400
    assert 'У боса вже максимум кроків: 2.' in boss_card_html(text(response), 'Dragon')
    assert Quest.objects.count() == 2
    assert not QuestCategory.objects.exists()


@pytest.mark.parametrize('data', [{'title': 'Sword'}, {}])
def test_a_defeated_boss_refuses_a_new_step(logged_in, user, data):
    boss, quests = make_boss(user, steps=3)
    beat(user, quests)

    response = add_step(logged_in, boss.pk, **data)

    assert response.status_code == 409
    assert DOMAIN_ERRORS['boss_defeated'] in boss_card_html(text(response), 'Dragon')
    assert Quest.objects.count() == 3


@pytest.mark.parametrize('data', [{}, {'title': 'x' * 256}])
def test_a_full_boss_says_so_whatever_was_posted(logged_in, user, settings, data):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2
    boss, _ = make_boss(user, steps=2)

    response = add_step(logged_in, boss.pk, **data)

    assert response.status_code == 400
    assert 'У боса вже максимум кроків: 2.' in boss_card_html(text(response), 'Dragon')


@pytest.mark.parametrize(
    'data',
    [{'title': 'Sword'}, {}, {'title': 'x' * 256}, {'quest': '1', 'title': 'Sword'}],
)
@pytest.mark.parametrize('with_htmx', [True, False])
def test_a_foreign_boss_is_a_404_whatever_was_posted(
    logged_in,
    user,
    stranger,
    data,
    with_htmx,
):
    boss, _ = make_boss(stranger, 'Their secret', steps=1)
    quests_before = Quest.objects.count()

    url = f'/bosses/{boss.pk}/steps/'
    if with_htmx:
        response = htmx_post_form(logged_in, url, **data)
    else:
        response = post(logged_in, url, **data)

    assert response.status_code == 404
    assert 'Their secret' not in text(response)
    if with_htmx:
        assert 'Боса не знайдено' in text(response)
    assert Quest.objects.count() == quests_before
    assert not Quest.objects.filter(user=user).exists()


def test_an_archived_boss_refuses_a_new_step_as_a_missing_one(logged_in, user):
    boss, _ = make_boss(user)
    BossService.archive_boss(user, boss.pk)

    response = add_step(logged_in, boss.pk, title='Sword')

    assert response.status_code == 404
    assert not Quest.objects.exists()


def test_a_new_step_counts_against_the_users_budget(logged_in, user):
    boss, _ = make_boss(user)

    with patch('web.views.user_budget_wait', return_value=30):
        response = add_step(logged_in, boss.pk, title='Sword')

    assert response.status_code == 429
    assert response['Retry-After'] == '30'
    assert not Quest.objects.exists()


def test_without_javascript_a_new_step_redirects_and_says_what_happened(logged_in, user):
    boss, _ = make_boss(user)

    response = post(logged_in, f'/bosses/{boss.pk}/steps/', title='Sword')

    assert response.status_code == 302
    assert response['Location'] == '/bosses/'
    assert 'Крок додано: Sword' in text(logged_in.get('/bosses/'))
    assert BossStep.objects.get().quest.title == 'Sword'


def test_without_javascript_the_error_stands_by_the_field_on_the_whole_page(logged_in, user):
    boss, _ = make_boss(user)
    free = make_quest(user, 'Free')

    response = post(logged_in, f'/bosses/{boss.pk}/steps/', quest=free.pk, title='Sword')

    assert response.status_code == 400
    body = text(response)
    assert '<html' in body
    card = boss_card_html(body, 'Dragon')
    assert step_field_error(card) == BOTH_TEXT
    assert 'value="Sword"' in card
    assert not BossStep.objects.exists()


def test_a_new_step_is_on_the_other_screens_as_a_quest(logged_in, user):
    boss, _ = make_boss(user)
    add_step(logged_in, boss.pk, title='Sword')

    assert 'Sword' in text(logged_in.get('/today/'))
    assert 'Sword' in text(logged_in.get('/quests/'))


# -- bosses: new steps in the form for a new boss ------------------------------------


def test_the_form_for_a_new_boss_has_a_box_for_new_steps(logged_in):
    body = text(logged_in.get('/bosses/new/'))

    assert 'name="new_steps"' in body
    assert '>Або нові кроки<' in body
    assert 'разовим квестом на 50 XP' in body


def test_each_line_becomes_a_step_beside_the_quests_ticked(logged_in, user):
    free = make_quest(user, 'Free')

    response = new_boss(logged_in, quests=[free.pk], new_steps='  One \r\n\r\n   \nTwo\n')

    assert response.status_code == 302
    steps = Boss.objects.get(user=user).steps.order_by('pk').select_related('quest')
    assert [step.quest.title for step in steps] == ['Free', 'One', 'Two']
    assert all(step.quest.quest_type == ONE_TIME for step in steps)
    assert all(step.quest.xp_reward == 50 for step in steps)


def test_a_blank_box_adds_no_step(logged_in, user):
    assert new_boss(logged_in, new_steps=' \n\r\n ').status_code == 302

    assert not Boss.objects.get(user=user).steps.exists()
    assert not Quest.objects.exists()


def test_more_lines_than_a_boss_takes_are_refused_unread(logged_in, settings):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2

    with patch('web.forms.QuestSerializer') as serializer:
        response = new_boss(logged_in, new_steps='One\nTwo\nThree')

    serializer.assert_not_called()
    assert response.status_code == 200
    assert 'У боса вже максимум кроків: 2.' in text(response)
    assert not Boss.objects.exists()
    assert not Quest.objects.exists()


def test_quests_and_lines_together_count_toward_the_limit(logged_in, user, settings):
    settings.QUESTIFY_MAX_BOSS_STEPS = 2
    free = make_quest(user, 'Free')

    response = new_boss(logged_in, quests=[free.pk], new_steps='One\nTwo')

    assert 'У боса вже максимум кроків: 2.' in text(response)
    assert not Boss.objects.exists()
    assert Quest.objects.count() == 1


@pytest.mark.parametrize(
    ('line', 'expected'),
    [
        ('x' * 256, 'Рядок 3: Назва задовга (до 255 символів).'),
        ('Bad\x00line', 'Рядок 3: Некоректне значення.'),
    ],
)
def test_a_bad_line_is_named_by_its_number(logged_in, line, expected):
    response = new_boss(logged_in, title='Dragon', new_steps=f'One\n\n{line}')

    assert response.status_code == 200
    body = text(response)
    assert f'id="id_new_steps_error">{expected}<' in body
    assert 'One\n' in body
    assert not Boss.objects.exists()
    assert not Quest.objects.exists()


def test_past_the_quest_quota_no_boss_and_no_quest_is_left(logged_in, user, settings):
    settings.QUESTIFY_MAX_QUESTS_PER_USER = 2
    free = make_quest(user, 'Free')

    response = new_boss(logged_in, quests=[free.pk], new_steps='Fits\nToo many')

    assert response.status_code == 200
    body = text(response)
    assert f'id="id_new_steps_error">{DOMAIN_ERRORS["quest_limit_reached"]}<' in body
    assert 'Fits\nToo many' in body
    assert not Boss.objects.exists()
    assert not BossStep.objects.exists()
    assert list(Quest.objects.values_list('title', flat=True)) == ['Free']


def test_a_boss_refused_by_its_title_keeps_the_lines_and_creates_nothing(logged_in):
    response = new_boss(logged_in, title='', new_steps='One\nTwo')

    assert response.status_code == 200
    assert 'One\nTwo' in text(response)
    assert not Quest.objects.exists()


# -- bosses: the minimum of steps that count ---------------------------------------


MIN_STEPS_TEXT = 'Бос має мати щонайменше 3 кроки'


def test_detaching_what_would_leave_too_few_is_refused_on_the_card(logged_in, user):
    boss, quests = make_boss(user, steps=3)
    make_boss(user, 'Other', steps=3)

    response = detach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 400
    body = text(response)
    assert f'role="alert">{MIN_STEPS_TEXT}<' in boss_card_html(body, 'Dragon')
    assert 'role="alert"' not in boss_card_html(body, 'Other')
    assert 'Крок відкріплено' not in body
    assert boss.steps.count() == 3


def test_without_javascript_the_refusal_redirects_and_says_why(logged_in, user):
    boss, quests = make_boss(user, steps=3)

    response = post(logged_in, f'/bosses/{boss.pk}/steps/{quests[0].pk}/detach/')

    assert response.status_code == 302
    assert MIN_STEPS_TEXT in text(logged_in.get('/bosses/'))
    assert boss.steps.count() == 3


@pytest.mark.parametrize('how', ['archived', 'made daily'])
def test_a_step_that_does_not_count_goes_even_at_the_minimum(logged_in, user, how):
    boss, quests = make_boss(user, steps=4)
    if how == 'archived':
        QuestService.archive_quest(user, quests[3].pk)
    else:
        Quest.objects.filter(pk=quests[3].pk).update(quest_type=DAILY)

    response = detach(logged_in, boss.pk, quests[3].pk)

    assert response.status_code == 200
    assert 'HP 3 з 3' in boss_card_html(text(response), 'Dragon')
    assert boss.steps.count() == 3


def test_an_old_boss_with_one_step_keeps_it_and_says_what_it_needs(logged_in, user):
    boss, quests = make_boss(user, steps=1)

    response = detach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 400
    card = boss_card_html(text(response), 'Dragon')
    assert MIN_STEPS_TEXT in card
    assert 'Додай ще 2 кроки,' in card
    assert boss.steps.count() == 1


def test_a_foreign_boss_at_the_minimum_is_a_404_for_the_web(logged_in, stranger):
    boss, quests = make_boss(stranger, 'Their secret', steps=3)

    response = detach(logged_in, boss.pk, quests[0].pk)

    assert response.status_code == 404
    body = text(response)
    assert 'Боса не знайдено' in body
    assert MIN_STEPS_TEXT not in body
    assert boss.steps.count() == 3


def test_the_api_refuses_in_english_and_the_screen_in_ukrainian(logged_in, user):
    boss, quests = make_boss(user, steps=3)
    api = APIClient()
    api.force_authenticate(user)

    from_api = api.delete(f'/api/bosses/{boss.pk}/steps/{quests[0].pk}/')
    from_web = detach(logged_in, boss.pk, quests[0].pk)

    assert from_api.status_code == from_web.status_code == 400
    assert from_api.json() == {'detail': 'A boss must keep at least 3 steps.'}
    assert re.search(r'role="alert">([^<]+)<', text(from_web))[1] == MIN_STEPS_TEXT


@pytest.mark.parametrize(
    ('minimum', 'expected'),
    [(1, '1 крок'), (3, '3 кроки'), (5, '5 кроків')],
)
def test_the_refusal_is_chosen_by_code_and_agrees_with_the_number(
    user,
    settings,
    minimum,
    expected,
):
    settings.QUESTIFY_BOSS_MIN_STEPS = minimum
    boss, quests = make_boss(user, steps=minimum)

    with pytest.raises(BossStepError) as refusal:
        BossService.detach_step(user, boss.pk, quests[0].pk)

    assert refusal.value.code == 'boss_min_steps'
    assert domain_error_text(refusal.value) == f'Бос має мати щонайменше {expected}'


@pytest.mark.parametrize(
    ('steps', 'note'),
    [
        (3, 'Блокує перемогу: спершу додай ще один крок, потім відкріпи цей.'),
        (4, 'Блокує перемогу: відкріпи цей крок.'),
    ],
)
@pytest.mark.parametrize('state', ['failed', 'expired'])
def test_a_blocking_step_advises_only_what_would_work(logged_in, user, steps, note, state):
    _, quests = make_boss(user, steps=steps)
    put_step_in_state(user, quests[0], state)

    row = step_html(
        boss_card_html(text(logged_in.get('/bosses/')), 'Dragon'),
        'Dragon step 0',
    )

    assert note in row


# -- bosses: archiving -------------------------------------------------------------


def test_archiving_asks_first_and_says_there_is_no_way_back(logged_in, user):
    make_boss(user)

    card = boss_card_html(text(logged_in.get('/bosses/')), 'Dragon')

    assert 'hx-confirm="Перенести боса в архів?' in card
    assert 'повернути боса звідти не можна' in card


def test_archiving_takes_the_card_away_and_frees_the_unfinished_steps(logged_in, user):
    boss, quests = make_boss(user, steps=2)
    beat(user, quests[:1])
    make_boss(user, 'Other')

    response = archive_boss(logged_in, boss.pk)

    assert response.status_code == 200
    body = text(response)
    assert 'toast--info">В архіві: Dragon<' in body
    assert 'boss-card__title' in body
    assert '>Dragon<' not in body
    assert 'autofocus' not in body
    # Free to join another boss, while the completed step stays with this one.
    assert f'<option value="{quests[1].pk}"' in boss_card_html(body, 'Other')
    assert list(boss.steps.values_list('quest_id', flat=True)) == [quests[0].pk]
    boss.refresh_from_db()
    assert boss.archived_at is not None


def test_archiving_goes_through_the_service(logged_in, user):
    boss, _ = make_boss(user)

    with patch(
        'web.views.BossService.archive_boss',
        wraps=BossService.archive_boss,
    ) as service:
        assert archive_boss(logged_in, boss.pk).status_code == 200

    service.assert_called_once_with(user, boss.pk)


def test_a_defeated_boss_can_be_archived(logged_in, user):
    boss, quests = make_boss(user, steps=3)
    beat(user, quests)

    assert archive_boss(logged_in, boss.pk).status_code == 200

    boss.refresh_from_db()
    assert boss.archived_at is not None


def test_without_javascript_archiving_redirects_and_says_what_happened(logged_in, user):
    boss, _ = make_boss(user)

    response = post(logged_in, f'/bosses/{boss.pk}/archive/')

    assert response.status_code == 302
    assert response['Location'] == '/bosses/'
    assert 'В архіві: Dragon' in text(logged_in.get('/bosses/'))


def test_archiving_twice_answers_that_the_boss_is_gone(logged_in, user):
    boss, _ = make_boss(user)
    archive_boss(logged_in, boss.pk)

    response = archive_boss(logged_in, boss.pk)

    assert response.status_code == 404
    assert 'Боса не знайдено' in text(response)


def test_an_archived_boss_still_counts_toward_the_quota(logged_in, user, settings):
    settings.QUESTIFY_MAX_BOSSES_PER_USER = 1
    boss, _ = make_boss(user)
    archive_boss(logged_in, boss.pk)

    assert 'Досягнуто ліміту босів' in text(new_boss(logged_in, title='Next'))


# -- bosses: whose it is and the protocol ------------------------------------------


@pytest.fixture
def their_boss(stranger):
    boss, quests = make_boss(stranger, 'Their secret', steps=1)
    return boss, quests[0]


def boss_action_urls(boss, quest):
    return {
        'attach': f'/bosses/{boss.pk}/steps/',
        'detach': f'/bosses/{boss.pk}/steps/{quest.pk}/detach/',
        'archive': f'/bosses/{boss.pk}/archive/',
    }


BOSS_ACTIONS = ['attach', 'detach', 'archive']


@pytest.mark.parametrize('action', BOSS_ACTIONS)
def test_acting_on_a_foreign_boss_is_a_404_for_htmx(logged_in, user, their_boss, action):
    boss, quest = their_boss
    make_boss(user, 'Mine')
    mine = make_quest(user, 'My quest')

    response = htmx_post_form(
        logged_in,
        boss_action_urls(boss, quest)[action],
        quest=mine.pk,
    )

    assert response.status_code == 404
    body = text(response)
    assert 'Боса не знайдено' in body
    assert 'Their secret' not in body
    assert body.index('role="alert"') < body.index('boss-card__title')
    boss.refresh_from_db()
    assert boss.archived_at is None
    assert boss.steps.count() == 1


@pytest.mark.parametrize('action', BOSS_ACTIONS)
def test_a_foreign_boss_is_the_plain_404_without_javascript(
    logged_in,
    user,
    their_boss,
    action,
):
    boss, quest = their_boss

    response = post(
        logged_in,
        boss_action_urls(boss, quest)[action],
        quest=make_quest(user).pk,
    )

    assert response.status_code == 404
    assert 'Their secret' not in text(response)


@pytest.mark.parametrize(
    'path',
    ['/bosses/{}/steps/', '/bosses/{}/steps/1/detach/', '/bosses/{}/archive/'],
)
def test_an_id_past_what_the_database_holds_is_a_404_for_bosses(logged_in, user, path):
    response = post(logged_in, path.format(10 ** 30), quest=make_quest(user).pk)

    assert response.status_code == 404


def test_a_quest_id_past_what_the_database_holds_is_a_404_for_a_step(logged_in, user):
    boss, _ = make_boss(user)

    response = detach(logged_in, boss.pk, 10 ** 30)

    assert response.status_code == 404
    assert 'Цього кроку вже немає' in boss_card_html(text(response), 'Dragon')


@pytest.mark.parametrize('action', BOSS_ACTIONS)
def test_acting_on_a_boss_needs_a_login(client, user, action):
    boss, quests = make_boss(user, steps=1)

    response = post(client, boss_action_urls(boss, quests[0])[action])

    assert response.status_code == 302
    assert response['Location'].startswith('/login/')
    boss.refresh_from_db()
    assert boss.archived_at is None


@pytest.mark.parametrize('action', BOSS_ACTIONS)
def test_acting_on_a_boss_needs_the_csrf_token(user, action):
    boss, quests = make_boss(user, steps=1)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = post(client, boss_action_urls(boss, quests[0])[action])

    assert response.status_code == 403
    assert boss.steps.count() == 1


@pytest.mark.parametrize('action', BOSS_ACTIONS)
def test_acting_on_a_boss_only_accepts_post(logged_in, user, action):
    boss, quests = make_boss(user, steps=1)

    assert logged_in.get(boss_action_urls(boss, quests[0])[action]).status_code == 405


@pytest.mark.parametrize('action', BOSS_ACTIONS)
def test_acting_on_a_boss_counts_against_the_users_budget(logged_in, user, action):
    boss, quests = make_boss(user, steps=1)

    with patch('web.views.user_budget_wait', return_value=42):
        response = htmx_post_form(
            logged_in,
            boss_action_urls(boss, quests[0])[action],
            quest=make_quest(user).pk,
        )

    assert response.status_code == 429
    assert response['Retry-After'] == '42'
    assert 'Забагато дій' in boss_card_html(text(response), 'Dragon')
    assert boss.steps.count() == 1


def test_without_javascript_a_refusal_redirects_and_says_why(logged_in, user):
    boss, _ = make_boss(user)
    daily = make_quest(user, 'Daily', DAILY)

    response = post(logged_in, f'/bosses/{boss.pk}/steps/', quest=daily.pk)

    assert response.status_code == 302
    assert 'Кроком боса може бути лише разовий квест.' in text(
        logged_in.get('/bosses/'),
    )


def test_the_api_keeps_its_english_and_its_shape(user):
    boss, _ = make_boss(user)
    daily = make_quest(user, 'Daily', DAILY)
    api = APIClient()
    api.force_authenticate(user)

    response = api.post(
        f'/api/bosses/{boss.pk}/steps/',
        {'quest': daily.pk},
        format='json',
    )

    assert response.status_code == 400
    assert response.json() == {'detail': 'Only one-time quests can be boss steps.'}


# -- bosses: the toasts of a completed step ----------------------------------------


def complete_in_list(client, quest):
    return htmx_post_form(
        client,
        f'/quests/{quest.pk}/complete/',
        list_url='/quests/',
    )


@pytest.mark.parametrize('screen', [complete, complete_in_list])
def test_a_step_completed_on_either_screen_tells_the_hit(logged_in, user, screen):
    _, quests = make_boss(user, steps=3)

    body = text(screen(logged_in, quests[0]))

    assert 'toast--boss">Бос: −1 HP<' in body
    assert 'Боса переможено' not in body


@pytest.mark.parametrize('screen', [complete, complete_in_list])
def test_the_last_step_on_either_screen_tells_the_win_instead(logged_in, user, screen):
    _, quests = make_boss(user, steps=3)
    beat(user, quests[:2])

    body = text(screen(logged_in, quests[2]))

    assert 'toast--boss">Боса переможено: +75 XP, +75 монет<' in body
    assert 'Бос: −1 HP' not in body


@pytest.mark.parametrize('screen', [complete, complete_in_list])
def test_a_quest_that_is_no_step_says_nothing_of_bosses(logged_in, user, screen):
    make_boss(user, steps=3)

    body = text(screen(logged_in, make_quest(user, 'Plain')))

    assert 'toast--boss' not in body


def test_without_javascript_a_step_tells_the_hit_as_a_message(logged_in, user):
    _, quests = make_boss(user, steps=3)

    response = post(logged_in, f'/quests/{quests[0].pk}/complete/')

    assert response.status_code == 302
    assert 'Бос: −1 HP' in text(logged_in.get('/today/'))


def test_the_boss_screens_colors_come_after_the_rules_they_override():
    # Equal specificity, so the later rule wins: a modifier before its base does nothing.
    css = (WEB_DIR / 'static/web/css/questify.css').read_text(encoding='utf-8')

    for base, modifier in (
        ('.tag {', '.tag--open {'),
        ('.tag {', '.tag--completed {'),
        ('.tag {', '.tag--failed,'),
        ('.progress {', '.progress--hp {'),
        ('.progress::-webkit-progress-value {', '.progress--hp::-webkit-progress-value {'),
        ('.progress::-moz-progress-bar {', '.progress--hp::-moz-progress-bar {'),
        ('.choices {', '.choices--list {'),
    ):
        assert css.index(base) < css.index(modifier), modifier


def test_the_boss_screen_follows_a_step_completed_on_today(logged_in, user):
    _, quests = make_boss(user, steps=3)

    complete(logged_in, quests[0])

    assert 'HP 2 з 3' in text(logged_in.get('/bosses/'))
