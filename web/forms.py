"""Validation of web forms with the API's own DRF serializers.

A form is posted as ``application/x-www-form-urlencoded``; ``post_data`` turns
it into the dict a serializer takes, and ``field_errors`` turns a failed
serializer into Ukrainian texts chosen by error code.
"""
from django.conf import settings
from django.utils import timezone

from bosses.serializers import BossSerializer, BossStepCreateSerializer
from bosses.services import BossLimitError, BossService, BossStepError
from quests.models import Quest
from quests.serializers import QuestCategorySerializer, QuestSerializer
from quests.services import QuestInputError, QuestService
from wallet.serializers import RewardSerializer
from wallet.services import RewardLimitError, RewardService

from .texts import (
    BOSS_STEP_BOTH_ERROR,
    BOSS_STEP_NEITHER_ERROR,
    DOMAIN_ERRORS,
    QUEST_CHOICE_ERROR,
    boss_step_line_error,
    domain_error_text,
    field_error_text,
)

NON_FIELD_ERRORS = 'non_field_errors'
# Refusals the service makes about the account, which this form can still show
# next to the input that ran into them.
QUEST_ERROR_FIELDS = {'category_limit_reached': 'new_category'}
# The inputs that describe a new step on a boss's card.
NEW_STEP_FIELDS = ('title', 'category', 'new_category', 'deadline')


def post_data(request, *, blank_to_none=()):
    """``request.POST`` as a dict, with the named blank fields as None.

    A blank optional input (a cleared date) must reach the serializer as
    None, not as '' which it would reject. A name posted twice keeps its last
    value, which is what makes a hidden input before a checkbox work: unchecked
    posts only the hidden value, checked posts the checkbox's after it.
    """
    data = request.POST.dict()
    for name in blank_to_none:
        if name in data and not data[name].strip():
            data[name] = None
    return data


def errors_from_detail(detail):
    """``{field: [text, ...]}`` for the ``detail`` of a DRF validation error."""
    errors = {}
    for field, details in detail.items():
        if not isinstance(details, (list, tuple)):
            details = [details]
        texts = []
        for item in details:
            if isinstance(item, (list, tuple, dict)):
                # A nested or list field: one text for the whole field.
                texts.append(field_error_text(field, None))
            else:
                texts.append(field_error_text(field, item))
        errors[field] = list(dict.fromkeys(texts))
    return errors


def field_errors(serializer):
    """``{field: [text, ...]}`` for a serializer whose ``is_valid()`` failed."""
    return errors_from_detail(serializer.errors)


# -- the quest form ----------------------------------------------------------------


def deadline_input_value(deadline):
    """A stored deadline as a datetime-local input shows it: local, to the minute."""
    if deadline is None:
        return ''
    return timezone.localtime(deadline).strftime('%Y-%m-%dT%H:%M')


def initial_quest_values(quest):
    """What the form shows before anything is posted."""
    if quest is None:
        return {
            'title': '',
            'category': '',
            'new_category': '',
            'quest_type': Quest.QuestType.ONE_TIME.value,
            'deadline': '',
            'is_active': True,
        }
    return {
        'title': quest.title,
        'category': str(quest.category_id or ''),
        'new_category': '',
        'quest_type': quest.quest_type,
        'deadline': deadline_input_value(quest.deadline),
        'is_active': quest.is_active,
    }


def posted_quest_values(post, quest):
    """What the form shows again after a refusal: what the user sent."""
    values = initial_quest_values(quest)
    for name in ('title', 'category', 'new_category', 'quest_type', 'deadline'):
        if name in post:
            values[name] = post[name]
    if 'is_active' in post:
        values['is_active'] = post['is_active'] == 'true'
    return values


def quest_form_data(request, quest=None):
    """The posted form as the serializer takes it.

    The deadline goes along only if it really changed. The input holds minutes
    in the character's timezone, so sending an untouched one back would drop the
    stored seconds and count as a change, and a quest past its deadline, which
    refuses any change to it, could then not even be renamed.
    """
    data = post_data(request, blank_to_none=('category', 'deadline'))
    if quest is not None and 'deadline' in data:
        posted = (data['deadline'] or '').strip()
        if posted == deadline_input_value(quest.deadline):
            del data['deadline']
    return data


def _new_category_name(request):
    """The validated name typed for a new category, or None, and its errors."""
    typed = request.POST.get('new_category', '')
    if not typed.strip():
        return None, {}
    serializer = QuestCategorySerializer(data={'name': typed})
    if serializer.is_valid():
        return serializer.validated_data['name'], {}
    return None, errors_from_detail({'new_category': serializer.errors['name']})


def quest_refusal_errors(exc):
    """A ``QuestInputError`` as the errors of the form that ran into it."""
    field = QUEST_ERROR_FIELDS.get(exc.code, exc.field or NON_FIELD_ERRORS)
    return {field: [domain_error_text(exc)]}


def save_quest(request, quest=None):
    """Validate and save the form as the API would; returns (quest, errors).

    The serializer and the service are the API's, so quotas, the server's reward
    and the rules on deadlines and types live there alone. A partial update
    leaves what the form does not hold, such as the description, as it is.
    ``NotFoundError`` reaches the caller.
    """
    serializer = QuestSerializer(
        instance=quest,
        data=quest_form_data(request, quest),
        partial=quest is not None,
        context={'user': request.user},
    )
    errors = {} if serializer.is_valid() else field_errors(serializer)
    new_category, category_errors = _new_category_name(request)
    errors.update(category_errors)
    if errors:
        return None, errors
    fields = {'new_category': new_category, **serializer.validated_data}
    try:
        if quest is None:
            return QuestService.create_quest(request.user, **fields), {}
        return QuestService.update_quest(request.user, quest.pk, **fields), {}
    except QuestInputError as exc:
        return None, quest_refusal_errors(exc)


# -- the reward form ---------------------------------------------------------------


def initial_reward_values(reward):
    """What the form shows before anything is posted."""
    if reward is None:
        return {'title': '', 'description': '', 'cost': '', 'is_active': True}
    return {
        'title': reward.title,
        'description': reward.description,
        'cost': reward.cost,
        'is_active': reward.is_active,
    }


def posted_reward_values(post, reward):
    """What the form shows again after a refusal: what the user sent."""
    values = initial_reward_values(reward)
    for name in ('title', 'description', 'cost'):
        if name in post:
            values[name] = post[name]
    if 'is_active' in post:
        values['is_active'] = post['is_active'] == 'true'
    return values


def save_reward(request, reward=None):
    """Validate and save the form as the API would; returns (reward, errors).

    The serializer and the service are the API's. ``NotFoundError`` reaches the
    caller.
    """
    serializer = RewardSerializer(
        instance=reward,
        data=post_data(request),
        partial=reward is not None,
    )
    if not serializer.is_valid():
        return None, field_errors(serializer)
    fields = serializer.validated_data
    try:
        if reward is None:
            return RewardService.create_reward(request.user, **fields), {}
        return RewardService.update_reward(request.user, reward.pk, **fields), {}
    except RewardLimitError as exc:
        return None, {NON_FIELD_ERRORS: [domain_error_text(exc)]}


# -- the boss form -----------------------------------------------------------------


def initial_boss_values():
    """What the form shows before anything is posted."""
    return {'title': '', 'description': '', 'quests': [], 'new_steps': ''}


def posted_boss_values(post):
    """What the form shows again after a refusal: what the user sent."""
    return {
        'title': post.get('title', ''),
        'description': post.get('description', ''),
        'quests': post.getlist('quests'),
        'new_steps': post.get('new_steps', ''),
    }


def _posted_step_ids(request):
    """The ids of the quests ticked in the form, and the errors of those that are not."""
    serializer = BossStepCreateSerializer(
        data=[{'quest': value} for value in request.POST.getlist('quests')],
        many=True,
    )
    if not serializer.is_valid():
        return [], {'quests': [QUEST_CHOICE_ERROR]}
    return [item['quest'] for item in serializer.validated_data], {}


def _new_step_serializer(request, data):
    """The API's rules for a quest, for a step that is one-time whatever was posted."""
    return QuestSerializer(
        data={**data, 'quest_type': Quest.QuestType.ONE_TIME.value},
        context={'user': request.user},
    )


def _posted_new_steps(request):
    """The ``quest_data`` per line of the new steps' box, and the errors of those that fail.

    A line is a step and a blank line is nothing. Lines beyond what a boss can
    take are refused unread: the box is free text, and a body can hold thousands.
    """
    lines = [
        (number, line)
        for number, line in enumerate(
            request.POST.get('new_steps', '').splitlines(),
            start=1,
        )
        if line.strip()
    ]
    limit = settings.QUESTIFY_MAX_BOSS_STEPS
    if len(lines) > limit:
        return [], {'new_steps': [DOMAIN_ERRORS['boss_steps_full'].format(limit=limit)]}
    new_steps = []
    errors = []
    for number, line in lines:
        serializer = _new_step_serializer(request, {'title': line})
        if serializer.is_valid():
            new_steps.append(serializer.validated_data)
        else:
            errors.extend(
                boss_step_line_error(number, message)
                for message in field_errors(serializer)['title']
            )
    return new_steps, {'new_steps': errors} if errors else {}


def save_boss(request):
    """Validate and create the boss as the API would; returns (boss, errors).

    The serializer's and the service's rules are the API's, so quotas and what a
    step may be live there alone. A quest or new step refused takes the new boss
    and every quest made for it back with it.
    """
    serializer = BossSerializer(data=post_data(request))
    errors = {} if serializer.is_valid() else field_errors(serializer)
    quest_ids, step_errors = _posted_step_ids(request)
    errors.update(step_errors)
    new_steps, new_step_errors = _posted_new_steps(request)
    errors.update(new_step_errors)
    if errors:
        return None, errors
    try:
        return BossService.create_boss_with_steps(
            request.user,
            quest_ids=quest_ids,
            new_steps=new_steps,
            **serializer.validated_data,
        ), {}
    except BossLimitError as exc:
        return None, {NON_FIELD_ERRORS: [domain_error_text(exc)]}
    except BossStepError as exc:
        return None, {'quests': [domain_error_text(exc)]}
    except QuestInputError as exc:
        return None, {'new_steps': [domain_error_text(exc)]}


# -- a step from a boss's card ------------------------------------------------------


def posted_step_values(post):
    """What the card's form shows again after a refusal: what the user sent."""
    return {name: post.get(name, '') for name in ('quest', *NEW_STEP_FIELDS)}


def _new_step_data(request):
    """The described step as ``add_new_step`` takes it, or None, and its errors."""
    data = post_data(request, blank_to_none=('category', 'deadline'))
    serializer = _new_step_serializer(
        request,
        {name: data[name] for name in ('title', 'category', 'deadline') if name in data},
    )
    errors = {} if serializer.is_valid() else field_errors(serializer)
    new_category, category_errors = _new_category_name(request)
    errors.update(category_errors)
    if errors:
        return None, errors
    return {'new_category': new_category, **serializer.validated_data}, {}


def posted_boss_step(request):
    """What the card's form asks for: (quest id, quest data, errors).

    A quest from the list or a new step, and any input of a new step counts: a
    deadline sent along with a quest would otherwise be dropped without a word.
    """
    quest = request.POST.get('quest', '').strip()
    described = any(request.POST.get(name, '').strip() for name in NEW_STEP_FIELDS)
    if quest and described:
        return None, None, {'title': [BOSS_STEP_BOTH_ERROR]}
    if not quest and not described:
        return None, None, {'title': [BOSS_STEP_NEITHER_ERROR]}
    if described:
        return None, *_new_step_data(request)
    body = BossStepCreateSerializer(data={'quest': quest})
    if not body.is_valid():
        return None, None, {NON_FIELD_ERRORS: [QUEST_CHOICE_ERROR]}
    return body.validated_data['quest'], None, {}
