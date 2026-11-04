"""What a completed quest tells the user, from its ``EventResult``."""
from dataclasses import dataclass

from achievements.selectors import achievement_names

from .templatetags.web_extras import uk_plural


@dataclass(frozen=True)
class Toast:
    text: str
    # Selects the look: 'xp', 'coins', 'level', 'achievement', 'boss' or 'info'.
    kind: str = 'info'


def _coins(amount):
    return f'+{amount} {uk_plural(amount, "монета,монети,монет")}'


def completion_toasts(result):
    """The toasts for ``QuestService.complete_quest``'s result; at most one query.

    Achievements arrive as ids, so their names take one query, and only when
    this completion unlocked any.
    """
    toasts = []
    if result['xp_gained']:
        toasts.append(Toast(f"+{result['xp_gained']} XP", 'xp'))
    if result['coins_gained']:
        toasts.append(Toast(_coins(result['coins_gained']), 'coins'))
    if result['daily_bonus']:
        toasts.append(Toast(f"Бонус дня +{result['daily_bonus']['xp']} XP", 'xp'))
    if result['leveled_up']:
        toasts.append(Toast(f"Новий рівень: {result['new_level']}", 'level'))
    toasts.extend(
        Toast(f'Ачівка: {name}', 'achievement')
        for name in achievement_names(result['achievements_unlocked'])
    )
    boss = result['boss']
    if boss and boss['defeated']:
        xp, coins = boss['defeated']['xp'], boss['defeated']['coins']
        toasts.append(Toast(f'Боса переможено: +{xp} XP, {_coins(coins)}', 'boss'))
    elif boss:
        toasts.append(Toast('Бос: −1 HP', 'boss'))
    return toasts


def skill_toasts(unlock):
    """The toasts for ``SkillService.unlock_skill``'s result; at most one query.

    A repeated unlock changed nothing, so it says nothing.
    """
    if not unlock.created:
        return []
    return [
        Toast(f'Навичку відкрито: {unlock.user_skill.skill.name}', 'level'),
        *(
            Toast(f'Ачівка: {name}', 'achievement')
            for name in achievement_names(unlock.achievements_unlocked)
        ),
    ]


def redemption_toasts(redemption):
    """What a redemption tells the user, from its own snapshot of title and cost."""
    cost = redemption.cost_at_redemption
    coins = f'{cost} {uk_plural(cost, "монета,монети,монет")}'
    return [Toast(f'Отримано: {redemption.title_at_redemption}, −{coins}', 'coins')]


def boss_step_attached_toasts(step):
    return [Toast(f'Крок додано: {step.quest.title}', 'boss')]


def boss_step_detached_toasts(quest):
    return [Toast(f'Крок відкріплено: {quest.title}', 'boss')]


def failure_toasts(quest_title):
    return [Toast(f'Не вдалось: {quest_title}')]


def archive_toasts(quest_title):
    return [Toast(f'В архіві: {quest_title}')]
