"""Ukrainian texts of the web UI that depend on what the services report.

Services raise ``core.errors.DomainError`` with a machine ``code`` and
``params``; the API sends the English ``str(exc)``, the web shows the text
written here for the code. Nothing here is built from ``str(exc)``, and no
internal id is ever part of a text.
"""
from rest_framework.exceptions import ErrorDetail

from .plurals import uk_plural

GENERIC_ERROR = 'Щось пішло не так. Спробуй ще раз.'
SERVICE_UNAVAILABLE = 'Сервіс тимчасово недоступний, спробуй пізніше'
# A form token that is spent, or that was never issued: the two cannot be told apart.
ALREADY_PROCESSED = 'Вже оброблено. Якщо нагороди немає в історії, спробуй ще раз.'

QUEST_TYPE_LABELS = {
    'daily': 'Щоденний',
    'habit': 'Звичка',
    'one_time': 'Разовий',
}

# StatType.name -> text. The model has no ``code`` field: the unique ``name`` (WIS)
# plays that part, and ``display_name`` (Wisdom) is free text. A type not listed
# here shows what the database has.
STAT_LABELS = {
    'WIS': 'Мудрість',
    'INT': 'Інтелект',
    'STR': 'Сила',
}

# SkillEvaluationContext.status -> text.
SKILL_STATUS_LABELS = {
    'unlocked': 'Відкрито',
    'available': 'Доступно',
    'locked': 'Заблоковано',
}

# RequirementProgress.type -> what is counted; a type removed from the code still
# reads as something to meet, since such a skill cannot be unlocked.
REQUIREMENT_LABELS = {
    'total_xp': 'XP',
    'quest_count': 'квестів',
    'quest_count_by_category': 'квестів у «{category}»',
}
OTHER_REQUIREMENT_LABEL = 'умова'

# WalletTransaction.kind -> text; a redemption adds the reward's title.
TRANSACTION_LABELS = {
    'quest_reward': 'Виконаний квест',
    'daily_bonus': 'Бонус дня',
    'boss_reward': 'Переможений бос',
    'redemption': 'Обмін',
    'initial_grant': 'Початковий баланс',
}
OTHER_TRANSACTION_LABEL = 'Операція'

# bosses.selectors.step_status -> text.
BOSS_STEP_STATUS_LABELS = {
    'open': 'Відкрито',
    'completed': 'Виконано',
    'failed': 'Не вдалось',
    'expired': 'Дедлайн минув',
    'inactive': 'Вимкнено',
}
# Why a step that counts stands in the boss's way; these are the states it cannot
# be completed from, so its HP can never reach 0.
BOSS_STEP_BLOCKS = {
    'failed': 'Блокує перемогу: відкріпи цей крок.',
    'expired': 'Блокує перемогу: відкріпи цей крок.',
    'inactive': 'Блокує перемогу, доки квест вимкнено.',
}
# The same steps on a boss with no step to spare: detaching would be refused.
BOSS_STEP_BLOCKS_AT_MINIMUM = {
    'failed': 'Блокує перемогу: спершу додай ще один крок, потім відкріпи цей.',
    'expired': 'Блокує перемогу: спершу додай ще один крок, потім відкріпи цей.',
}
BOSS_STEP_NOT_COUNTED = 'Не рахується в HP: квест в архіві або вже не разовий.'
BOSS_STEP_NOT_IN_WIN = 'Не рахувався в перемозі.'
# Any value of the step's input that is not a quest's id. Only a hand-made request
# sends one: the inputs list real quests.
QUEST_CHOICE_ERROR = 'Обери квест зі списку.'
# The card's form takes a quest from the list or a new step, one of the two.
BOSS_STEP_BOTH_ERROR = 'Або обери квест, або опиши новий крок, а не все разом.'
BOSS_STEP_NEITHER_ERROR = 'Обери квест або введи назву нового кроку.'

# DomainError.code -> text; "{name}" is filled from DomainError.params.
DOMAIN_ERRORS = {
    # quests
    'quest_not_found': 'Квест не знайдено.',
    'category_not_found': 'Категорію не знайдено.',
    'quest_already_completed': 'Цей квест уже виконано.',
    'quest_already_failed': 'Цей квест уже позначено як провалений.',
    'quest_inactive': 'Цей квест неактивний.',
    'quest_deadline_passed': 'Дедлайн цього квесту минув.',
    'xp_limit_reached': 'Ліміт XP за останні 24 години вичерпано.',
    'daily_fail_limit_reached': 'Ліміт провалених квестів за день вичерпано.',
    'lifetime_fail_limit_reached': 'Ліміт провалених квестів вичерпано.',
    'quest_limit_reached': 'Досягнуто ліміту квестів для цього акаунта.',
    'category_limit_reached': 'Досягнуто ліміту категорій для цього акаунта.',
    'category_exists': 'У тебе вже є категорія з такою назвою.',
    'category_gone': 'Цієї категорії вже не існує.',
    'deadline_locked': 'Дедлайн минув, його не можна змінити.',
    'quest_type_locked': 'Тип квесту не можна змінити після першого виконання.',
    # wallet
    'reward_not_found': 'Нагороду не знайдено: можливо, її вже архівовано.',
    'reward_limit_reached': 'Досягнуто ліміту нагород для цього акаунта.',
    'reward_inactive': 'Ця нагорода неактивна.',
    'redemption_limit_reached': 'Ліміт обмінів за останні 24 години вичерпано.',
    'not_enough_coins': 'Не вистачає монет: є {balance}, потрібно {cost}.',
    # bosses
    'boss_not_found': 'Боса не знайдено: можливо, його вже архівовано.',
    'step_not_found': 'Цього кроку вже немає: можливо, його вже відкріплено.',
    'quest_not_one_time': 'Кроком боса може бути лише разовий квест.',
    'quest_already_done': 'Цей квест уже виконано або провалено.',
    'quest_already_step': 'Цей квест уже є кроком боса.',
    'boss_steps_full': 'У боса вже максимум кроків: {limit}.',
    'boss_min_steps': 'Бос має мати щонайменше {minimum} {steps}',
    'boss_defeated': 'Боса переможено, його кроки не можна змінити.',
    'step_completed': 'Виконаний крок не можна відкріпити.',
    'boss_limit_reached': 'Досягнуто ліміту босів для цього акаунта.',
    'active_boss_limit_reached': (
        'Забагато босів у процесі: переможи або архівуй одного з них.'
    ),
    # skills
    'skill_not_found': 'Навичку не знайдено.',
    'skill_parent_locked': 'Спершу відкрий «{parent}», потім «{skill}».',
    'skill_requirements_not_met': 'Вимоги навички ще не виконано.',
    # character
    'timezone_change_locked': (
        'Часовий пояс можна змінити через 24 години після останнього '
        'виконаного чи проваленого квесту.'
    ),
}

# DRF / Django validation code -> text, for a field that has no text of its own.
FIELD_ERRORS = {
    'required': 'Це поле обов’язкове.',
    'blank': 'Це поле обов’язкове.',
    'null': 'Це поле обов’язкове.',
    'max_length': 'Забагато символів.',
    'min_length': 'Замало символів.',
    'invalid': 'Некоректне значення.',
    'unique': 'Таке значення вже зайняте.',
    'username_taken': 'Це ім’я користувача вже зайняте.',
    'password_too_short': 'Пароль закороткий: потрібно щонайменше 8 символів.',
    'password_too_common': 'Цей пароль надто поширений.',
    'password_entirely_numeric': 'Пароль не може складатися лише з цифр.',
    'password_too_similar': 'Пароль надто схожий на ім’я користувача.',
    'authorization': 'Невірне ім’я користувача або пароль.',
}

# (field, code) -> text, tried before FIELD_ERRORS.
SPECIFIC_FIELD_ERRORS = {
    ('username', 'invalid'): (
        'Ім’я користувача може містити лише літери, цифри та знаки @ . + - _'
    ),
    ('username', 'max_length'): 'Ім’я користувача задовге (до 150 символів).',
    ('password', 'max_length'): 'Пароль задовгий.',
    ('title', 'max_length'): 'Назва задовга (до 255 символів).',
    ('quest_type', 'required'): 'Обери тип квесту.',
    ('quest_type', 'null'): 'Обери тип квесту.',
    ('quest_type', 'invalid_choice'): 'Обери тип квесту зі списку.',
    # DRF's own text for these two would show the id it was given.
    ('category', 'does_not_exist'): DOMAIN_ERRORS['category_gone'],
    ('category', 'incorrect_type'): 'Обери категорію зі списку.',
    ('category', 'invalid'): 'Обери категорію зі списку.',
    ('new_category', 'max_length'): 'Назва категорії задовга (до 100 символів).',
    ('deadline', 'invalid'): 'Вкажи дату й час дедлайну.',
    ('deadline', 'date'): 'Вкажи дату й час дедлайну.',
    ('deadline', 'make_aware'): (
        'Цей час не підходить для твого часового поясу: обери інший.'
    ),
    ('deadline', 'overflow'): 'Дедлайн має бути між 2000 і 2100 роками.',
    ('deadline', 'deadline_out_of_range'): 'Дедлайн має бути між 2000 і 2100 роками.',
    ('deadline', 'deadline_in_past'): 'Дедлайн має бути в майбутньому.',
    ('description', 'max_length'): 'Опис задовгий.',
    ('cost', 'required'): 'Вкажи ціну.',
    ('cost', 'null'): 'Вкажи ціну.',
    ('cost', 'invalid'): 'Вкажи ціну цілим числом.',
    ('cost', 'max_string_length'): 'Вкажи ціну цілим числом.',
    ('cost', 'min_value'): 'Ціна має бути щонайменше 1 монета.',
    ('cost', 'max_value'): 'Ціна має бути не більшою за 100 000 монет.',
}


def boss_steps_hint(missing):
    """What a boss with too few steps that count still needs."""
    steps = uk_plural(missing, 'крок,кроки,кроків')
    return f'Додай ще {missing} {steps}, щоб бос міг бути переможений'


def boss_new_step_hint(xp):
    """What a new step becomes; the reward is the server's, never a field."""
    return f'Стане разовим квестом на {xp} XP.'


def boss_step_line_error(number, text):
    """An error of one line of the new boss's steps; the line is the user's own count."""
    return f'Рядок {number}: {text}'


def _plural_words(exc):
    """The words a text needs to agree with a number; a service reports only that."""
    if exc.code == 'boss_min_steps':
        return {'steps': uk_plural(exc.params['minimum'], 'крок,кроки,кроків')}
    return {}


def domain_error_text(exc):
    """The text for a DomainError, chosen by its code; never ``str(exc)``."""
    template = DOMAIN_ERRORS.get(exc.code)
    if template is None:
        return GENERIC_ERROR
    try:
        return template.format(**exc.params, **_plural_words(exc))
    except (KeyError, IndexError):
        return GENERIC_ERROR


def requirement_text(requirement):
    """One line of a skill's progress: ``квестів у «Навчання»: 17 з 20``.

    ``requirement`` is a node's dict from ``SkillService.get_tree``. Past the
    threshold the count stops at it: "1200 з 500" would read like a mistake.
    """
    label = REQUIREMENT_LABELS.get(requirement['type'], OTHER_REQUIREMENT_LABEL)
    required = requirement['required']
    current = min(requirement['current'], required)
    return f"{label.format(category=requirement['category'])}: {current} з {required}"


def field_error_text(field, detail):
    """The text for one DRF validation error of ``field``, chosen by its code."""
    code = getattr(detail, 'code', None) if isinstance(detail, ErrorDetail) else None
    return (
        SPECIFIC_FIELD_ERRORS.get((field, code))
        or FIELD_ERRORS.get(code)
        or FIELD_ERRORS['invalid']
    )
