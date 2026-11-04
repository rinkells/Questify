"""What a screen shows, gathered once for the page and for its fragments.

The figures come from the services and selectors; this only puts them together,
so a fragment sent after an action can never disagree with a reloaded page.
"""
from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from achievements.selectors import achievement_cards, next_achievement
from bosses.models import Boss, BossDefeat
from bosses.selectors import attachable_quests, list_bosses, step_status
from bosses.services import boss_reward
from core.selectors import character_overview, get_coins
from quests.models import Quest
from quests.selectors import (
    QUEST_LIST_ACTIVE,
    QUEST_LIST_ARCHIVE,
    QUEST_LIST_COMPLETED,
    UNCATEGORIZED,
    categories_for_user,
    finished_one_time_today,
    has_completions,
    prefetch_stat_rewards,
    quest_list,
    today_quests,
    xp_usage_24h,
)
from quests.services import XP_REWARD_BY_TYPE, QuestService
from skills.services import SkillService
from wallet.models import MAX_REWARD_COST, Reward
from wallet.selectors import rewards_for_user, transaction_page

from .forms import initial_boss_values, initial_quest_values, initial_reward_values
from .texts import (
    BOSS_STEP_BLOCKS,
    BOSS_STEP_BLOCKS_AT_MINIMUM,
    BOSS_STEP_NOT_COUNTED,
    BOSS_STEP_NOT_IN_WIN,
    BOSS_STEP_STATUS_LABELS,
    QUEST_TYPE_LABELS,
    SKILL_STATUS_LABELS,
    boss_new_step_hint,
    boss_steps_hint,
    requirement_text,
)

COMPLETED = 'completed'
# As many stat tiles as the character card holds.
CHARACTER_CARD_STATS = 5


def bonus_hint(bonus, routines):
    """Why today's bonus cannot be earned as things stand: a key, or None.

    'too_few' means fewer quests than the minimum. 'lost' means the quests still
    possible cannot make up the rest: one was failed or missed its deadline today.
    """
    if bonus.awarded:
        return None
    if bonus.total < bonus.required:
        return 'too_few'
    possible = sum(item.can_complete for item in routines)
    if bonus.completed + possible < bonus.required:
        return 'lost'
    return None


def today_context(user, *, now=None):
    now = now or timezone.now()
    quests = today_quests(user, now=now)
    finished = finished_one_time_today(user, now=now)
    prefetch_stat_rewards(item.quest for item in [*quests.items, *finished])
    bonus = QuestService.daily_bonus_status(user)
    overview = character_overview(user)
    return {
        'overview': overview,
        'now': timezone.localtime(now),
        'quests': quests,
        'finished': finished,
        'bonus': bonus,
        'bonus_hint': bonus_hint(bonus, quests.routines),
        'summary_completed': sum(
            item.state == COMPLETED for item in [*quests.routines, *finished]
        ),
        'summary_total': len(quests.routines) + quests.total + len(finished),
        # In the catalog's order, as character_overview gives them.
        'stats': overview.stats[:CHARACTER_CARD_STATS],
        'xp_usage': xp_usage_24h(user, now=now),
        'next_achievement': next_achievement(user),
    }


# -- all quests --------------------------------------------------------------------


STATUS_LABELS = {
    QUEST_LIST_ACTIVE: 'Активні',
    QUEST_LIST_COMPLETED: 'Виконані',
    QUEST_LIST_ARCHIVE: 'Архів',
}
# Longer than any id PostgreSQL holds, and short enough to parse without a thought.
MAX_CURSOR_DIGITS = 18


@dataclass(frozen=True)
class QuestFilters:
    status: str = QUEST_LIST_ACTIVE
    quest_type: str | None = None
    # A category's id as text, ``UNCATEGORIZED`` or None for any.
    category: str | None = None


def quest_filters(query, categories):
    """The filters a query string asks for.

    Each value is only ever compared with the user's own options, so anything
    else, a category of someone else's among it, reads as no filter at all and
    says nothing about what exists.
    """
    status = query.get('status')
    quest_type = query.get('type')
    category = query.get('category')
    own_categories = {str(item.pk) for item in categories} | {UNCATEGORIZED}
    return QuestFilters(
        status=status if status in STATUS_LABELS else QUEST_LIST_ACTIVE,
        quest_type=quest_type if quest_type in QUEST_TYPE_LABELS else None,
        category=category if category in own_categories else None,
    )


def quest_list_url(filters, *, before=None):
    """The list's address for these filters: what can be copied and reloaded."""
    params = {}
    if filters.status != QUEST_LIST_ACTIVE:
        params['status'] = filters.status
    if filters.quest_type:
        params['type'] = filters.quest_type
    if filters.category:
        params['category'] = filters.category
    if before is not None:
        params['before'] = before
    url = reverse('web:quests')
    return f'{url}?{urlencode(params)}' if params else url


def _cursor(query):
    """The ``before`` of the query string as a number, or None if it is not one."""
    value = query.get('before', '')
    if value.isascii() and value.isdigit() and len(value) <= MAX_CURSOR_DIGITS:
        return int(value)
    return None


def _chip(key, label, filters, *, dot_of=None, **change):
    """A filter link: these filters with one changed, the current one marked."""
    target = replace(filters, **change)
    return {
        # Stable across swaps, so htmx can put the focus back on it.
        'id': f'filter-{key}',
        'label': label,
        'url': quest_list_url(target),
        'active': target == filters,
        # The category whose color the chip shows, if any.
        'dot_of': dot_of,
    }


def _filter_chips(filters, categories):
    status_chips = [
        _chip(f'status-{value}', label, filters, status=value)
        for value, label in STATUS_LABELS.items()
    ]
    type_chips = [
        _chip('type-any', 'Усі', filters, quest_type=None),
        *(
            _chip(f'type-{value}', label, filters, quest_type=value)
            for value, label in QUEST_TYPE_LABELS.items()
        ),
    ]
    category_chips = []
    if categories:
        category_chips = [
            _chip('category-any', 'Усі', filters, category=None),
            *(
                _chip(
                    f'category-{item.pk}',
                    item.name,
                    filters,
                    dot_of=item,
                    category=str(item.pk),
                )
                for item in categories
            ),
            _chip(
                f'category-{UNCATEGORIZED}',
                'Без категорії',
                filters,
                category=UNCATEGORIZED,
            ),
        ]
    return status_chips, type_chips, category_chips


def quests_context(user, query, *, now=None):
    """The list for a query string. Four queries whatever the rows' number."""
    categories = list(categories_for_user(user))
    filters = quest_filters(query, categories)
    before = _cursor(query)
    page = quest_list(
        user,
        status=filters.status,
        quest_type=filters.quest_type,
        category=filters.category,
        before=before,
        now=now,
    )
    prefetch_stat_rewards(item.quest for item in page.items)
    status_chips, type_chips, category_chips = _filter_chips(filters, categories)
    return {
        'status_chips': status_chips,
        'type_chips': type_chips,
        'category_chips': category_chips,
        'page': page,
        'list_mode': True,
        'list_url': quest_list_url(filters),
        'more_url': (
            quest_list_url(filters, before=page.next_before)
            if page.next_before is not None
            else ''
        ),
        'later_page': before is not None,
        'filtered': filters != QuestFilters(),
    }


def _skill_node(node, parent, acted_on, error):
    """A node of ``SkillService.get_tree`` with what the screen says about it."""
    is_acted_on = node['id'] == acted_on
    return {
        'id': node['id'],
        'name': node['name'],
        'description': node['description'],
        'status': node['status'],
        'status_label': SKILL_STATUS_LABELS[node['status']],
        # Once unlocked they are history, and an admin may have raised them since.
        'requirements': [] if node['status'] == 'unlocked' else [
            {'text': requirement_text(item), 'met': item['met']}
            for item in node['requirements']
        ],
        # With every requirement met, this is all that still stands in the way.
        'waits_for': (
            parent['name']
            if node['status'] != 'unlocked'
            and parent is not None
            and parent['status'] != 'unlocked'
            else None
        ),
        'acted_on': is_acted_on,
        'error': error if is_acted_on else None,
        'children': [
            _skill_node(child, node, acted_on, error)
            for child in node['children']
        ],
    }


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node['children'])


def skills_context(user, *, acted_on=None, error=None):
    """The skill tree. Five queries whatever the size of the catalog.

    ``error`` belongs to the skill ``acted_on``. A skill the tree no longer has
    (deleted meanwhile) has no node to stand next to, so it goes above the tree.
    """
    tree = [
        _skill_node(node, None, acted_on, error)
        for node in SkillService.get_tree(user)
    ]
    nodes = list(_walk(tree))
    placed = any(node['acted_on'] for node in nodes)
    return {
        'tree': tree,
        'skills_total': len(nodes),
        'skills_unlocked': sum(node['status'] == 'unlocked' for node in nodes),
        'tree_error': None if placed else error,
    }


def achievements_context(user):
    """The achievement cards: earned, then visible, then masked; one query.

    Within each group they keep the catalog's order. There is no progress per
    achievement, because evaluators answer only yes or no.
    """
    cards = sorted(
        achievement_cards(user),
        key=lambda card: (not card.unlocked, card.masked),
    )
    return {
        'cards': cards,
        'achievements_total': len(cards),
        'achievements_unlocked': sum(card.unlocked for card in cards),
    }


# -- rewards -------------------------------------------------------------------------


@dataclass(frozen=True)
class RewardCard:
    reward: Reward
    balance: int
    # The card the last action was about, which gets the focus and any message.
    acted_on: bool = False
    error: str | None = None
    notice: str | None = None
    # Set while the card asks for confirmation: the form's one-time token.
    token: str | None = None

    @property
    def shortfall(self):
        return max(0, self.reward.cost - self.balance)

    @property
    def can_redeem(self):
        """What the screen offers. The service still decides on the post."""
        return self.reward.is_active and self.shortfall == 0

    @property
    def balance_after(self):
        return self.balance - self.reward.cost


def reward_card(user, reward_id):
    """One card of the user's as it stands now, or None. Two queries."""
    reward = rewards_for_user(user).filter(pk=reward_id).first()
    return None if reward is None else RewardCard(reward, get_coins(user))


def history_context(user, query):
    """One page of the coin history for a query string; one query, two with a cursor."""
    before = _cursor(query)
    history = transaction_page(user, before=before)
    return {
        'history': history,
        'later_page': before is not None,
        'more_url': (
            f"{reverse('web:rewards')}?{urlencode({'before': history.next_before})}"
            if history.next_before is not None
            else ''
        ),
    }


def rewards_context(user, query=None, *, acted_on=None, error=None, notice=None):
    """The rewards screen. Three queries, four with a cursor in the history.

    ``error`` and ``notice`` belong to the reward ``acted_on``. One the list no longer
    has (archived meanwhile) has no card to stand on, so its error goes above the list.
    """
    balance = get_coins(user)
    cards = [
        RewardCard(
            reward,
            balance,
            acted_on=reward.pk == acted_on,
            error=error if reward.pk == acted_on else None,
            notice=notice if reward.pk == acted_on else None,
        )
        for reward in rewards_for_user(user)
    ]
    return {
        'balance': balance,
        'cards': cards,
        'list_error': None if any(card.acted_on for card in cards) else error,
        **history_context(user, query or {}),
    }


def reward_form_context(reward=None, *, values=None, errors=None, page_error=None):
    """The create or edit form."""
    return {
        'reward': reward,
        'form_title': 'Редагування нагороди' if reward else 'Нова нагорода',
        'values': values or initial_reward_values(reward),
        'errors': errors or {},
        'page_error': page_error,
        'max_cost': MAX_REWARD_COST,
        'max_description': settings.QUESTIFY_MAX_DESCRIPTION_LENGTH,
    }


# -- bosses --------------------------------------------------------------------------


@dataclass(frozen=True)
class BossStepRow:
    quest_id: int
    title: str
    status: str
    label: str
    # Whether it counts toward HP; a row that does not is shown muted.
    live: bool
    note: str
    can_detach: bool


@dataclass(frozen=True)
class BossCard:
    boss: Boss
    steps: list[BossStepRow]
    defeat: BossDefeat | None
    # What beating it would pay now; None while it cannot be beaten by these steps.
    reward: int | None
    # How many more steps that count a win needs, in words; '' if it has enough.
    steps_hint: str
    # Every step is done and it stands: only completing a step beats a boss, and
    # the last open one was detached or archived.
    needs_new_step: bool
    can_attach: bool
    steps_full: bool
    # The card the last action was about, which gets the focus and any error.
    acted_on: bool = False
    error: str | None = None
    # What its step form holds again after a refusal, and why next to each input.
    step_values: dict = field(default_factory=dict)
    step_errors: dict = field(default_factory=dict)

    @property
    def field_prefix(self):
        """Every card holds the same inputs, and their ids must differ."""
        return f'boss-{self.boss.pk}'


def _boss_step_note(step, status, defeated, at_minimum):
    if not step.live:
        return BOSS_STEP_NOT_IN_WIN if defeated else BOSS_STEP_NOT_COUNTED
    if defeated:
        return ''
    if at_minimum and status in BOSS_STEP_BLOCKS_AT_MINIMUM:
        return BOSS_STEP_BLOCKS_AT_MINIMUM[status]
    return BOSS_STEP_BLOCKS.get(status, '')


def _boss_card(boss, now, acted_on, error, step_values, step_errors):
    defeat = getattr(boss, 'defeat', None)
    defeated = defeat is not None
    minimum = settings.QUESTIFY_BOSS_MIN_STEPS
    # BossService.detach_step would refuse a step that counts.
    at_minimum = boss.max_hp <= minimum
    rows = []
    for step in boss.steps.all():
        status = step_status(step, now)
        rows.append(BossStepRow(
            quest_id=step.quest_id,
            title=step.quest.title,
            status=status,
            label=BOSS_STEP_STATUS_LABELS[status],
            live=step.live,
            note=_boss_step_note(step, status, defeated, at_minimum),
            can_detach=not defeated and not step.completed,
        ))
    enough_steps = boss.max_hp >= minimum
    is_acted_on = boss.pk == acted_on
    return BossCard(
        boss=boss,
        steps=rows,
        defeat=defeat,
        reward=boss_reward(boss.max_hp) if enough_steps and not defeated else None,
        steps_hint=(
            boss_steps_hint(minimum - boss.max_hp)
            if not defeated and not enough_steps
            else ''
        ),
        needs_new_step=not defeated and enough_steps and boss.hp == 0,
        can_attach=not defeated and len(rows) < settings.QUESTIFY_MAX_BOSS_STEPS,
        steps_full=not defeated and len(rows) >= settings.QUESTIFY_MAX_BOSS_STEPS,
        acted_on=is_acted_on,
        error=error if is_acted_on else None,
        step_values=step_values if is_acted_on else {},
        step_errors=step_errors if is_acted_on else {},
    )


def bosses_context(
    user,
    *,
    acted_on=None,
    error=None,
    step_values=None,
    step_errors=None,
    now=None,
):
    """The bosses screen, those in progress first. Five queries.

    ``error``, ``step_values`` and ``step_errors`` belong to the boss ``acted_on``.
    One the list no longer has (archived meanwhile, or never the user's) has no card
    to stand on, so its error goes above the list and says nothing about what exists.
    """
    now = now or timezone.now()
    cards = [
        _boss_card(boss, now, acted_on, error, step_values or {}, step_errors or {})
        for boss in list_bosses(user)
    ]
    acted_on_card = next((card for card in cards if card.acted_on), None)
    return {
        'active_cards': [card for card in cards if card.defeat is None],
        'defeated_cards': [card for card in cards if card.defeat is not None],
        'acted_on_card': acted_on_card,
        'attachable': list(attachable_quests(user, now=now)),
        'category_options': list(categories_for_user(user)),
        'new_step_hint': boss_new_step_hint(
            XP_REWARD_BY_TYPE[Quest.QuestType.ONE_TIME],
        ),
        'list_error': None if acted_on_card else error,
        'max_steps': settings.QUESTIFY_MAX_BOSS_STEPS,
    }


def boss_form_context(user, *, values=None, errors=None, page_error=None):
    """The form for a new boss: its fields and the quests free to become its steps."""
    values = values or initial_boss_values()
    return {
        'values': values,
        'errors': errors or {},
        'page_error': page_error,
        'quest_options': [
            {
                'value': quest.pk,
                'label': quest.title,
                'deadline': quest.deadline,
                'checked': str(quest.pk) in values['quests'],
            }
            for quest in attachable_quests(user)
        ],
        'max_description': settings.QUESTIFY_MAX_DESCRIPTION_LENGTH,
        'min_steps': settings.QUESTIFY_BOSS_MIN_STEPS,
        'max_steps': settings.QUESTIFY_MAX_BOSS_STEPS,
        'new_step_xp': XP_REWARD_BY_TYPE[Quest.QuestType.ONE_TIME],
    }


def quest_form_context(
    user,
    quest=None,
    *,
    values=None,
    errors=None,
    next_url='',
    page_error=None,
):
    """The create or edit form. The reward is only ever read out here: never a field."""
    values = values or initial_quest_values(quest)
    return {
        'quest': quest,
        'form_title': 'Редагування квесту' if quest else 'Новий квест',
        'values': values,
        'errors': errors or {},
        'page_error': page_error,
        'next_url': next_url,
        'back_url': next_url or reverse('web:quests'),
        'category_options': [
            {
                'value': str(item.pk),
                'label': item.name,
                'selected': str(item.pk) == values['category'],
            }
            for item in categories_for_user(user)
        ],
        'type_options': [
            {
                'value': value,
                'label': label,
                # A quest keeps the reward it was given until its type changes.
                'xp': (
                    quest.xp_reward
                    if quest is not None and quest.quest_type == value
                    else XP_REWARD_BY_TYPE[value]
                ),
                'checked': value == values['quest_type'],
            }
            for value, label in QUEST_TYPE_LABELS.items()
        ],
        'type_locked': quest is not None and has_completions(quest),
        'deadline_locked': (
            quest is not None
            and quest.deadline is not None
            and quest.deadline < timezone.now()
        ),
    }
