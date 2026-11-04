from dataclasses import replace
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)
from django.views.csrf import csrf_failure as default_csrf_failure
from rest_framework.exceptions import ValidationError as DRFValidationError

from bosses.models import Boss, BossStep
from bosses.services import BossConflictError, BossService, BossStepError
from core.auth_serializers import LoginSerializer, UserRegistrationSerializer
from core.errors import DomainError, NotFoundError
from core.services import change_timezone
from core.validators import validate_timezone_name
from quests.selectors import (
    listed_quest,
    prefetch_stat_rewards,
    quests_for_user,
    today_quest,
)
from quests.services import QuestInputError, QuestService
from skills.services import (
    SkillNotFoundError,
    SkillParentLockedError,
    SkillService,
)
from wallet.selectors import rewards_for_user
from wallet.services import RewardService, WalletService

from . import form_tokens
from .forms import (
    errors_from_detail,
    field_errors,
    post_data,
    posted_boss_step,
    posted_boss_values,
    posted_quest_values,
    posted_reward_values,
    posted_step_values,
    quest_refusal_errors,
    save_boss,
    save_quest,
    save_reward,
)
from .middleware import is_web_request
from .screens import (
    achievements_context,
    boss_form_context,
    bosses_context,
    history_context,
    quest_form_context,
    quests_context,
    reward_card,
    reward_form_context,
    rewards_context,
    skills_context,
    today_context,
)
from .texts import (
    ALREADY_PROCESSED,
    DOMAIN_ERRORS,
    GENERIC_ERROR,
    SERVICE_UNAVAILABLE,
    domain_error_text,
)
from .throttling import (
    ThrottleUnavailable,
    describe_wait,
    throttled_for,
    user_budget_wait,
)
from .toasts import (
    archive_toasts,
    boss_step_attached_toasts,
    boss_step_detached_toasts,
    completion_toasts,
    failure_toasts,
    redemption_toasts,
    skill_toasts,
)

FALLBACK_TIMEZONE = 'UTC'
# The element "show more" replaces; any other htmx request gets the whole list.
QUESTS_MORE_TARGET = 'quests-more'
HISTORY_MORE_TARGET = 'history-more'


def safe_next_url(request, candidate):
    """``candidate`` if it stays on this site, else ''.

    Rejects other hosts, protocol-relative ``//host``, backslash tricks and
    ``javascript:`` URLs, and a plain-http target for an https request.
    """
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return ''


def _check_form_throttle(request, scope, template, context, what):
    """The refusal to answer with if the form's budget stops it, else None."""
    try:
        wait = throttled_for(request, scope)
    except ThrottleUnavailable:
        return render(
            request,
            template,
            {**context, 'page_error': SERVICE_UNAVAILABLE},
            status=503,
        )
    if wait is None:
        return None
    response = render(
        request,
        template,
        {
            **context,
            'page_error': (
                f'Забагато спроб {what}. Спробуй знову через '
                f'{describe_wait(wait)}.'
            ),
        },
        status=429,
    )
    response['Retry-After'] = str(wait)
    return response


@require_http_methods(['GET', 'HEAD', 'POST'])
def login_view(request):
    next_url = safe_next_url(
        request,
        request.POST.get('next') or request.GET.get('next'),
    )
    if request.user.is_authenticated:
        return redirect(next_url or settings.LOGIN_REDIRECT_URL)
    context = {'next': next_url, 'form_values': {}, 'errors': {}}
    if request.method != 'POST':
        return render(request, 'web/login.html', context)

    context['form_values'] = {'username': request.POST.get('username', '')}
    refusal = _check_form_throttle(
        request,
        'login',
        'web/login.html',
        context,
        'входу',
    )
    if refusal is not None:
        return refusal

    # The API's own check, so the two doors accept exactly the same logins:
    # case-insensitive names, NFKC, inactive accounts refused.
    serializer = LoginSerializer(
        data=post_data(request),
        context={'request': request},
    )
    if not serializer.is_valid():
        context['errors'] = field_errors(serializer)
        return render(request, 'web/login.html', context)

    # A web login is a session of its own: it neither revokes the account's API
    # token nor, in the other direction, ends when a token is issued.
    login(request, serializer.validated_data['user'])
    return redirect(next_url or settings.LOGIN_REDIRECT_URL)


def browser_timezone(value):
    """The IANA zone the browser reported, or UTC if it is not one."""
    value = (value or '').strip()
    try:
        validate_timezone_name(value)
    except DjangoValidationError:
        return FALLBACK_TIMEZONE
    return value


@require_http_methods(['GET', 'HEAD', 'POST'])
def register_view(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    context = {'form_values': {}, 'errors': {}}
    if request.method != 'POST':
        return render(request, 'web/register.html', context)

    context['form_values'] = {
        'username': request.POST.get('username', ''),
        'timezone': request.POST.get('timezone', ''),
    }
    refusal = _check_form_throttle(
        request,
        'register',
        'web/register.html',
        context,
        'реєстрації',
    )
    if refusal is not None:
        return refusal

    serializer = UserRegistrationSerializer(
        data=post_data(request),
        context={'request': request},
    )
    if not serializer.is_valid():
        context['errors'] = field_errors(serializer)
        return render(request, 'web/register.html', context)

    try:
        user = serializer.save()
    except DRFValidationError as exc:
        # Two registrations raced and this one lost at the database.
        context['errors'] = errors_from_detail(exc.detail)
        return render(request, 'web/register.html', context)
    try:
        change_timezone(user, browser_timezone(request.POST.get('timezone')))
    except (DjangoValidationError, DomainError):
        # A new account keeps UTC rather than fail registering.
        pass
    login(request, user)
    messages.success(request, 'Ласкаво просимо до Questify!')
    return redirect(settings.LOGIN_REDIRECT_URL)


@require_POST
def logout_view(request):
    logout(request)
    return redirect(settings.LOGIN_URL)


@login_required
def today(request):
    return render(request, 'web/today.html', today_context(request.user))


def _is_htmx(request):
    return request.headers.get('HX-Request') == 'true'


def _row_item(user, quest_id):
    """The row of a quest as it stands now, with what it pays; None if gone."""
    item = today_quest(user, quest_id)
    if item is not None:
        prefetch_stat_rewards([item.quest])
    return item


def _posted_list_url(request):
    """The all-quests address a row's form came from; '' for the today screen.

    Rows of the list post it with their forms, so the answer fits that screen and
    a plain form post returns to the same filters.
    """
    if 'list_url' not in request.POST:
        return ''
    url = safe_next_url(request, request.POST['list_url'])
    root = reverse('web:quests')
    return url if urlsplit(url).path == root else root


def _list_row(request, quest_id, list_url, *, toasts=(), row_error=None, status=200):
    """A row of the all-quests list as it stands now, with the coins and toasts."""
    item = listed_quest(request.user, quest_id)
    if item is None:
        return render(
            request,
            'web/partials/quest_row_gone.html',
            {'row_error': row_error or DOMAIN_ERRORS['quest_not_found']},
            status=status,
        )
    prefetch_stat_rewards([item.quest])
    return render(
        request,
        'web/partials/quest_list_action.html',
        {
            'item': item,
            'list_mode': True,
            'list_url': list_url,
            'toasts': toasts,
            'row_error': row_error,
        },
        status=status,
    )


def _quest_refusal(request, quest_id, text, status, list_url=''):
    """Say why next to the row, or above the list without JS.

    On the today screen the whole screen is sent along: a refusal often means
    another tab or device got there first, and what it did belongs on this
    screen too.
    """
    if not _is_htmx(request):
        messages.error(request, text)
        return redirect(list_url or 'web:today')
    if list_url:
        return _list_row(
            request,
            quest_id,
            list_url,
            row_error=text,
            status=status,
        )
    item = _row_item(request.user, quest_id)
    if item is None:
        return render(
            request,
            'web/partials/quest_row_gone.html',
            {'row_error': text},
            status=status,
        )
    return render(
        request,
        'web/partials/quest_action.html',
        {**today_context(request.user), 'item': item, 'row_error': text},
        status=status,
    )


def _quest_action(request, quest_id, action, *, list_url=''):
    """Run ``action``, which returns toasts, and answer htmx or a plain form post."""
    wait = user_budget_wait(request)
    if wait is not None:
        response = _quest_refusal(
            request,
            quest_id,
            f'Забагато дій. Спробуй знову через {describe_wait(wait)}.',
            429,
            list_url,
        )
        response['Retry-After'] = str(wait)
        return response
    try:
        toasts = action()
    except NotFoundError as exc:
        if not _is_htmx(request):
            raise Http404 from None
        return render(
            request,
            'web/partials/quest_row_gone.html',
            {'row_error': domain_error_text(exc)},
            status=404,
        )
    except DomainError as exc:
        return _quest_refusal(
            request,
            quest_id,
            domain_error_text(exc),
            400,
            list_url,
        )
    except DjangoValidationError:
        # From an event handler; its text is not written for the user.
        return _quest_refusal(request, quest_id, GENERIC_ERROR, 400, list_url)

    if not _is_htmx(request):
        for toast in toasts:
            messages.success(request, toast.text)
        return redirect(list_url or 'web:today')
    if list_url:
        return _list_row(request, quest_id, list_url, toasts=toasts)
    return render(
        request,
        'web/partials/quest_action.html',
        {
            **today_context(request.user),
            'item': _row_item(request.user, quest_id),
            'toasts': toasts,
        },
    )


@require_POST
@login_required
def quest_complete(request, quest_id):
    return _quest_action(
        request,
        quest_id,
        lambda: completion_toasts(
            QuestService.complete_for_user(request.user, quest_id),
        ),
        list_url=_posted_list_url(request),
    )


@require_POST
@login_required
def quest_fail(request, quest_id):
    return _quest_action(
        request,
        quest_id,
        lambda: failure_toasts(
            QuestService.fail_for_user(request.user, quest_id).quest.title,
        ),
        list_url=_posted_list_url(request),
    )


@require_POST
@login_required
def quest_archive(request, quest_id):
    """The web's DELETE: it hides the quest, and there is no way back from it."""
    return _quest_action(
        request,
        quest_id,
        lambda: archive_toasts(
            QuestService.archive_quest(request.user, quest_id).title,
        ),
        # Only the list has this button, so without a filter it still answers there.
        list_url=_posted_list_url(request) or reverse('web:quests'),
    )


@require_safe
@login_required
def quests(request):
    context = quests_context(request.user, request.GET)
    if not _is_htmx(request):
        return render(request, 'web/quests.html', context)
    if request.headers.get('HX-Target') == QUESTS_MORE_TARGET:
        return render(request, 'web/partials/quest_page.html', context)
    return render(request, 'web/partials/quest_browser.html', context)


def _quest_form(request, quest):
    """The same form for a new quest and for one being edited."""
    next_url = safe_next_url(
        request,
        request.POST.get('next') or request.GET.get('next'),
    )
    if request.method != 'POST':
        return render(
            request,
            'web/quest_form.html',
            quest_form_context(request.user, quest, next_url=next_url),
        )

    def refused(*, errors=None, page_error=None, status=200):
        return render(
            request,
            'web/quest_form.html',
            quest_form_context(
                request.user,
                quest,
                values=posted_quest_values(request.POST, quest),
                errors=errors,
                page_error=page_error,
                next_url=next_url,
            ),
            status=status,
        )

    wait = user_budget_wait(request)
    if wait is not None:
        response = refused(
            page_error=f'Забагато дій. Спробуй знову через {describe_wait(wait)}.',
            status=429,
        )
        response['Retry-After'] = str(wait)
        return response
    try:
        saved, errors = save_quest(request, quest)
    except NotFoundError:
        # Archived in another tab since the form was opened.
        raise Http404 from None
    if errors:
        return refused(errors=errors)
    messages.success(
        request,
        f'Квест «{saved.title}» {"збережено" if quest else "створено"}.',
    )
    return redirect(next_url or 'web:quests')


@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def quest_new(request):
    return _quest_form(request, None)


@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def quest_edit(request, quest_id):
    quest = quests_for_user(request.user).filter(pk=quest_id).first()
    if quest is None:
        raise Http404
    return _quest_form(request, quest)


@require_safe
@login_required
def skills(request):
    return render(request, 'web/skills.html', skills_context(request.user))


def _skill_refusal(request, skill_id, text, status):
    """Say why next to the node, or above the tree without JS.

    The whole tree comes along: a refusal means this page was out of date, and a
    parent reported as locked must not stay "open" a few lines above the error.
    """
    if not _is_htmx(request):
        messages.error(request, text)
        return redirect('web:skills')
    return render(
        request,
        'web/partials/skill_tree.html',
        skills_context(request.user, acted_on=skill_id, error=text),
        status=status,
    )


@require_POST
@login_required
def skill_unlock(request, skill_id):
    wait = user_budget_wait(request)
    if wait is not None:
        response = _skill_refusal(
            request,
            skill_id,
            f'Забагато дій. Спробуй знову через {describe_wait(wait)}.',
            429,
        )
        response['Retry-After'] = str(wait)
        return response
    try:
        unlock = SkillService.unlock_skill(request.user, skill_id)
    except SkillNotFoundError as exc:
        if not _is_htmx(request):
            raise Http404 from None
        return _skill_refusal(request, skill_id, domain_error_text(exc), 404)
    except SkillParentLockedError as exc:
        return _skill_refusal(request, skill_id, domain_error_text(exc), 409)
    except DomainError as exc:
        return _skill_refusal(request, skill_id, domain_error_text(exc), 400)
    except DjangoValidationError:
        # From an event handler; its text is not written for the user.
        return _skill_refusal(request, skill_id, GENERIC_ERROR, 400)

    toasts = skill_toasts(unlock)
    if not _is_htmx(request):
        for toast in toasts:
            messages.success(request, toast.text)
        return redirect('web:skills')
    return render(
        request,
        'web/partials/skill_action.html',
        {**skills_context(request.user, acted_on=skill_id), 'toasts': toasts},
    )


@require_safe
@login_required
def achievements(request):
    return render(
        request,
        'web/achievements.html',
        achievements_context(request.user),
    )


@require_safe
@login_required
def rewards(request):
    if (
        _is_htmx(request)
        and request.headers.get('HX-Target') == HISTORY_MORE_TARGET
    ):
        return render(
            request,
            'web/partials/transaction_page.html',
            history_context(request.user, request.GET),
        )
    return render(
        request,
        'web/rewards.html',
        rewards_context(request.user, request.GET),
    )


def _rewards_answer(
    request,
    *,
    acted_on=None,
    error=None,
    notice=None,
    toasts=(),
    status=200,
):
    """The list with the coins and the history out of band.

    The whole list, because spending coins changes what every other card can offer,
    and a refusal often means another tab got there first.
    """
    return render(
        request,
        'web/partials/reward_action.html',
        {
            **rewards_context(
                request.user,
                acted_on=acted_on,
                error=error,
                notice=notice,
            ),
            'toasts': toasts,
        },
        status=status,
    )


def _reward_refusal(request, reward_id, text, status):
    """Say why on the card, or above the list without JS."""
    if not _is_htmx(request):
        if status == 404:
            raise Http404
        messages.error(request, text)
        return redirect('web:rewards')
    return _rewards_answer(request, acted_on=reward_id, error=text, status=status)


def _reward_budget_refusal(request, reward_id):
    """The refusal to answer with if the user's budget is spent, else None."""
    wait = user_budget_wait(request)
    if wait is None:
        return None
    response = _reward_refusal(
        request,
        reward_id,
        f'Забагато дій. Спробуй знову через {describe_wait(wait)}.',
        429,
    )
    response['Retry-After'] = str(wait)
    return response


def _redeem_scope(reward_id):
    return f'redeem:{reward_id}'


def _reward_gone(request):
    """A card asked for that is not the user's, or no longer there."""
    if not _is_htmx(request):
        raise Http404
    return render(
        request,
        'web/partials/reward_card_gone.html',
        {'error': DOMAIN_ERRORS['reward_not_found']},
        status=404,
    )


def _redeem_blocked_text(card):
    """Why this card cannot be redeemed now, in the service's words; else None."""
    if not card.reward.is_active:
        return DOMAIN_ERRORS['reward_inactive']
    if card.shortfall:
        return DOMAIN_ERRORS['not_enough_coins'].format(
            balance=card.balance,
            cost=card.reward.cost,
        )
    return None


def _reward_confirm(request, reward_id):
    """Ask before spending: on the card, or on a page of its own without JS.

    The form that spends exists only here, so a token is issued for what is really
    about to be confirmed, not for every card of every page shown.
    """
    card = reward_card(request.user, reward_id)
    if card is None:
        return _reward_gone(request)
    blocked = _redeem_blocked_text(card)
    if blocked:
        if not _is_htmx(request):
            messages.error(request, blocked)
            return redirect('web:rewards')
        # The button was on offer, so the page is out of date: another tab spent the
        # coins or switched the reward off. The whole list rather than this card, or
        # the header would go on showing a balance this text contradicts.
        response = _rewards_answer(request, acted_on=reward_id, error=blocked)
        response['HX-Retarget'] = '#reward-browser'
        response['HX-Reswap'] = 'outerHTML'
        return response
    card = replace(
        card,
        token=form_tokens.issue(request, _redeem_scope(reward_id)),
    )
    return render(
        request,
        'web/partials/reward_card.html' if _is_htmx(request) else 'web/reward_confirm.html',
        {'card': card, 'balance': card.balance},
    )


def _reward_redeem(request, reward_id):
    refusal = _reward_budget_refusal(request, reward_id)
    if refusal is not None:
        return refusal
    # Before the token: another user's reward is a 404 whatever was posted with it.
    if not rewards_for_user(request.user).filter(pk=reward_id).exists():
        return _reward_refusal(
            request,
            reward_id,
            DOMAIN_ERRORS['reward_not_found'],
            404,
        )
    if not form_tokens.claim(
        request,
        _redeem_scope(reward_id),
        request.POST.get('form_token', ''),
    ):
        if not _is_htmx(request):
            messages.info(request, ALREADY_PROCESSED)
            return redirect('web:rewards')
        return _rewards_answer(request, acted_on=reward_id, notice=ALREADY_PROCESSED)
    try:
        result = WalletService.redeem_reward(request.user, reward_id)
    except NotFoundError as exc:
        return _reward_refusal(request, reward_id, domain_error_text(exc), 404)
    except DomainError as exc:
        return _reward_refusal(request, reward_id, domain_error_text(exc), 400)
    toasts = redemption_toasts(result.redemption)
    if not _is_htmx(request):
        for toast in toasts:
            messages.success(request, toast.text)
        return redirect('web:rewards')
    return _rewards_answer(request, acted_on=reward_id, toasts=toasts)


@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def reward_redeem(request, reward_id):
    if request.method == 'POST':
        return _reward_redeem(request, reward_id)
    return _reward_confirm(request, reward_id)


@require_safe
@login_required
def reward_card_view(request, reward_id):
    """The card back from its confirmation; only "cancel" under htmx leads here."""
    if not _is_htmx(request):
        return redirect('web:rewards')
    card = reward_card(request.user, reward_id)
    if card is None:
        return _reward_gone(request)
    return render(request, 'web/partials/reward_card.html', {'card': card})


def _reward_form(request, reward):
    """The same form for a new reward and for one being edited."""
    if request.method != 'POST':
        return render(request, 'web/reward_form.html', reward_form_context(reward))

    def refused(*, errors=None, page_error=None, status=200):
        return render(
            request,
            'web/reward_form.html',
            reward_form_context(
                reward,
                values=posted_reward_values(request.POST, reward),
                errors=errors,
                page_error=page_error,
            ),
            status=status,
        )

    wait = user_budget_wait(request)
    if wait is not None:
        response = refused(
            page_error=f'Забагато дій. Спробуй знову через {describe_wait(wait)}.',
            status=429,
        )
        response['Retry-After'] = str(wait)
        return response
    try:
        saved, errors = save_reward(request, reward)
    except NotFoundError:
        # Archived in another tab since the form was opened.
        raise Http404 from None
    if errors:
        return refused(errors=errors)
    messages.success(
        request,
        f'Нагороду «{saved.title}» {"збережено" if reward else "створено"}.',
    )
    return redirect('web:rewards')


@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def reward_new(request):
    return _reward_form(request, None)


@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def reward_edit(request, reward_id):
    reward = rewards_for_user(request.user).filter(pk=reward_id).first()
    if reward is None:
        raise Http404
    return _reward_form(request, reward)


@require_POST
@login_required
def reward_archive(request, reward_id):
    """The web's DELETE: it hides the reward, and there is no way back from it."""
    refusal = _reward_budget_refusal(request, reward_id)
    if refusal is not None:
        return refusal
    try:
        archived = RewardService.archive_reward(request.user, reward_id)
    except NotFoundError as exc:
        return _reward_refusal(request, reward_id, domain_error_text(exc), 404)
    toasts = archive_toasts(archived.title)
    if not _is_htmx(request):
        for toast in toasts:
            messages.success(request, toast.text)
        return redirect('web:rewards')
    return _rewards_answer(request, toasts=toasts)


@require_safe
@login_required
def bosses(request):
    return render(request, 'web/bosses.html', bosses_context(request.user))


def _bosses_answer(request, *, acted_on=None, error=None, toasts=(), status=200):
    """The whole list: a step attached or detached changes what every boss can take."""
    return render(
        request,
        'web/partials/boss_action.html',
        {
            **bosses_context(request.user, acted_on=acted_on, error=error),
            'toasts': toasts,
        },
        status=status,
    )


def _boss_refusal(request, boss_id, text, status):
    """Say why on the card, or above the list without JS."""
    if not _is_htmx(request):
        if status == 404:
            raise Http404
        messages.error(request, text)
        return redirect('web:bosses')
    return _bosses_answer(request, acted_on=boss_id, error=text, status=status)


def _boss_budget_refusal(request, boss_id):
    """The refusal to answer with if the user's budget is spent, else None."""
    wait = user_budget_wait(request)
    if wait is None:
        return None
    response = _boss_refusal(
        request,
        boss_id,
        f'Забагато дій. Спробуй знову через {describe_wait(wait)}.',
        429,
    )
    response['Retry-After'] = str(wait)
    return response


def _boss_action(request, boss_id, action):
    """Run ``action``, which returns toasts, and answer htmx or a plain form post.

    The services report a missing row with ``DoesNotExist``, so each is caught by
    name; a foreign boss reads as a missing one.
    """
    try:
        toasts = action()
    except Boss.DoesNotExist:
        return _boss_refusal(request, boss_id, DOMAIN_ERRORS['boss_not_found'], 404)
    except BossStep.DoesNotExist:
        return _boss_refusal(request, boss_id, DOMAIN_ERRORS['step_not_found'], 404)
    except BossConflictError as exc:
        return _boss_refusal(request, boss_id, domain_error_text(exc), 409)
    except BossStepError as exc:
        return _boss_refusal(request, boss_id, domain_error_text(exc), 400)
    if not _is_htmx(request):
        for toast in toasts:
            messages.success(request, toast.text)
        return redirect('web:bosses')
    return _bosses_answer(request, acted_on=boss_id, toasts=toasts)


def _boss_step_form_refusal(request, boss_id, errors):
    """Say why next to the input, keeping what was typed, also without JS.

    That takes the form, so a boss that has none answers for itself whatever was
    posted: a missing or foreign one with a 404, a defeated or full one as the
    service would.
    """
    context = bosses_context(
        request.user,
        acted_on=boss_id,
        step_values=posted_step_values(request.POST),
        step_errors=errors,
    )
    card = context['acted_on_card']
    if card is None:
        return _boss_refusal(request, boss_id, DOMAIN_ERRORS['boss_not_found'], 404)
    if card.defeat is not None:
        return _boss_refusal(request, boss_id, DOMAIN_ERRORS['boss_defeated'], 409)
    if not card.can_attach:
        text = DOMAIN_ERRORS['boss_steps_full'].format(limit=context['max_steps'])
        return _boss_refusal(request, boss_id, text, 400)
    template = 'web/partials/boss_action.html' if _is_htmx(request) else 'web/bosses.html'
    return render(request, template, {**context, 'toasts': ()}, status=400)


@require_POST
@login_required
def boss_attach(request, boss_id):
    """The card's form: a quest from the list or a new step, one of the two."""
    refusal = _boss_budget_refusal(request, boss_id)
    if refusal is not None:
        return refusal
    quest_id, quest_data, errors = posted_boss_step(request)
    if errors:
        return _boss_step_form_refusal(request, boss_id, errors)

    def add_step():
        if quest_id is not None:
            return BossService.attach_step(request.user, boss_id, quest_id)
        return BossService.add_new_step(request.user, boss_id, quest_data)

    try:
        return _boss_action(
            request,
            boss_id,
            lambda: boss_step_attached_toasts(add_step()),
        )
    except QuestInputError as exc:
        return _boss_step_form_refusal(request, boss_id, quest_refusal_errors(exc))


@require_POST
@login_required
def boss_detach(request, boss_id, quest_id):
    refusal = _boss_budget_refusal(request, boss_id)
    if refusal is not None:
        return refusal
    return _boss_action(
        request,
        boss_id,
        lambda: boss_step_detached_toasts(
            BossService.detach_step(request.user, boss_id, quest_id),
        ),
    )


@require_POST
@login_required
def boss_archive(request, boss_id):
    """The web's DELETE: it hides the boss, and there is no way back from it."""
    refusal = _boss_budget_refusal(request, boss_id)
    if refusal is not None:
        return refusal
    return _boss_action(
        request,
        boss_id,
        lambda: archive_toasts(
            BossService.archive_boss(request.user, boss_id).title,
        ),
    )


@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def boss_new(request):
    if request.method != 'POST':
        return render(request, 'web/boss_form.html', boss_form_context(request.user))

    def refused(*, errors=None, page_error=None, status=200):
        return render(
            request,
            'web/boss_form.html',
            boss_form_context(
                request.user,
                values=posted_boss_values(request.POST),
                errors=errors,
                page_error=page_error,
            ),
            status=status,
        )

    wait = user_budget_wait(request)
    if wait is not None:
        response = refused(
            page_error=f'Забагато дій. Спробуй знову через {describe_wait(wait)}.',
            status=429,
        )
        response['Retry-After'] = str(wait)
        return response
    boss, errors = save_boss(request)
    if errors:
        return refused(errors=errors)
    messages.success(request, f'Боса «{boss.title}» створено.')
    return redirect('web:bosses')


def csrf_failure(request, reason=''):
    """A readable page (or, for htmx, fragment) when the CSRF check fails."""
    if not is_web_request(request):
        return default_csrf_failure(request, reason=reason)
    if request.headers.get('HX-Request') == 'true':
        return render(request, 'web/partials/csrf_failure.html', status=403)
    return render(request, 'web/csrf_failure.html', status=403)
