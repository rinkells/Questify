from django import template

from core.selectors import get_coins

from ..plurals import uk_plural
from ..texts import (
    OTHER_TRANSACTION_LABEL,
    QUEST_TYPE_LABELS,
    STAT_LABELS,
    TRANSACTION_LABELS,
)

register = template.Library()

NAV_ITEMS = (
    ('Сьогодні', 'web:today', '/today/'),
    ('Усі квести', 'web:quests', '/quests/'),
    ('Боси', 'web:bosses', '/bosses/'),
    ('Навички', 'web:skills', '/skills/'),
    ('Ачівки', 'web:achievements', '/achievements/'),
    ('Нагороди', 'web:rewards', '/rewards/'),
)
# As many as the design has category colors (.dot--c0 and on).
CATEGORY_COLORS = 4


@register.simple_tag(takes_context=True)
def nav_items(context):
    """The navigation entries, the one for the current URL marked active."""
    from django.urls import reverse

    path = context['request'].path_info
    items = []
    for label, url_name, prefix in NAV_ITEMS:
        items.append({
            'label': label,
            'url': reverse(url_name),
            'active': path.startswith(prefix),
        })
    return items


@register.simple_tag(takes_context=True)
def coin_balance(context):
    """The header's balance: the screen's own figure if it has one, so they agree."""
    overview = context.get('overview')
    if overview is not None:
        return overview.coins
    balance = context.get('balance')
    if balance is not None:
        return balance
    return get_coins(context['user'])


@register.filter
def initials(user):
    """Up to two capital letters for an avatar, from the name or username."""
    name = (user.get_full_name() or user.get_username() or '').strip()
    parts = [part for part in name.replace('_', ' ').replace('-', ' ').split() if part]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper() or '?'


@register.filter
def display_name(user):
    return user.get_short_name() or user.get_username()


@register.filter
def quest_type_label(quest_type):
    return QUEST_TYPE_LABELS.get(quest_type, '')


@register.filter
def stat_label(stat):
    """A stat's name in Ukrainian by its code, else as the database has it."""
    return STAT_LABELS.get(stat.name) or stat.display_name or stat.name


@register.filter
def transaction_label(transaction):
    """What a wallet transaction was, by its kind; never the stored English."""
    label = TRANSACTION_LABELS.get(transaction.kind, OTHER_TRANSACTION_LABEL)
    if transaction.redemption is not None:
        return f'{label}: {transaction.redemption.title_at_redemption}'
    return label


@register.filter
def signed_amount(amount):
    return f'+{amount}' if amount > 0 else f'−{abs(amount)}'


@register.filter
def category_color(category):
    """The color class of a category: stable, since it follows the id."""
    return f'c{category.pk % CATEGORY_COLORS}'


# ``{{ n|uk_plural:"день,дні,днів" }}``
register.filter('uk_plural', uk_plural)
