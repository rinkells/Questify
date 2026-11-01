from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from core.errors import DomainError, NotFoundError
from core.models import Character
from events.dispatcher import dispatcher
from events.event_types import EventResult, QuestCompletedEvent

from .models import (
	DailyBonus,
	Quest,
	QuestCategory,
	QuestCompletion,
	Streak,
)
from .xp_cap import xp_left_in_24h


DAILY_BONUS_QUEST_TYPES = (Quest.QuestType.DAILY, Quest.QuestType.HABIT)


# Every refusal below carries a ``code`` (see core.errors); str(exc) is the
# English text the API sends. The code of a message with an id in it does not
# carry the id, so a client can show its own text without one.


class QuestAlreadyCompletedError(DomainError):
    code = 'quest_already_completed'


class QuestAlreadyFailedError(DomainError):
    code = 'quest_already_failed'


class QuestRuleError(DomainError):
    """A completion or failure the rules refuse (400).

    Codes: ``quest_inactive``, ``quest_deadline_passed``,
    ``xp_limit_reached``, ``daily_fail_limit_reached`` and
    ``lifetime_fail_limit_reached``.
    """


class QuestInputError(DomainError):
    """A create, update or delete the rules refuse (400).

    ``field`` names the input, or is None for the whole request. Codes:
    ``quest_limit_reached``, ``category_limit_reached``, ``category_gone``,
    ``deadline_locked`` and ``quest_type_locked``.
    """


# The XP a quest pays when completed, by type: the server's policy, not the
# client's choice.
XP_REWARD_BY_TYPE = {
    Quest.QuestType.DAILY: 25,
    Quest.QuestType.HABIT: 25,
    Quest.QuestType.ONE_TIME: 50,
}
QUEST_FIELDS = frozenset((
    'category', 'title', 'description', 'quest_type', 'is_active', 'deadline',
))
CATEGORY_FIELDS = frozenset(('name', 'icon'))


def _check_fields(fields, allowed):
    unknown = set(fields) - allowed
    if unknown:
        raise TypeError(f'Unexpected fields: {sorted(unknown)}')


def _get_locked(queryset, pk, code):
    """``queryset.get(pk=pk)`` FOR UPDATE, or ``NotFoundError``.

    A malformed id is a missing row, as in DRF's get_object_or_404.
    """
    try:
        return queryset.select_for_update().get(pk=pk)
    except (
        queryset.model.DoesNotExist,
        TypeError,
        ValueError,
        DjangoValidationError,
    ):
        raise NotFoundError('Not found.', code=code) from None


def local_day(now, timezone_name):
    """Return the local date of ``now`` and the aware bounds of that day."""
    user_timezone = ZoneInfo(timezone_name)
    today = timezone.localtime(now, user_timezone).date()
    day_start = timezone.make_aware(
        datetime.combine(today, time.min),
        user_timezone,
    )
    next_day_start = timezone.make_aware(
        datetime.combine(today + timedelta(days=1), time.min),
        user_timezone,
    )
    return today, day_start, next_day_start


@dataclass(frozen=True)
class DailyBonusProgress:
    total: int
    completed: int

    @property
    def required(self):
        """Completions the bonus needs: every quest, and at least the minimum.

        Below the minimum this exceeds ``total``, so the bonus stays out of
        reach until more quests are added.
        """
        return max(self.total, settings.QUESTIFY_DAILY_BONUS_MIN_QUESTS)

    @property
    def earned(self):
        return self.completed >= self.required


def daily_bonus_progress(user_id, day_start, next_day_start):
    """Count the day's bonus quests and those completed within the day.

    The set is every active daily and habit quest except those whose
    deadline is at or before the start of the day and that have no
    completion in it. A deadline missed during the day keeps its quest in
    the set, uncompleted, as a failure does, so the outcome does not depend
    on whether it passed before or after the other completions.

    A deadline exactly at the start of the day counts as before it, although
    complete_quest (which refuses only ``deadline < now``) would still accept
    that single instant; a completion made then keeps the quest in the set
    through ``completed_today``.
    """
    completed_in_day = QuestCompletion.objects.filter(
        quest=OuterRef('pk'),
        status=QuestCompletion.CompletionStatus.COMPLETED,
        completed_at__gte=day_start,
        completed_at__lt=next_day_start,
    )
    counts = Quest.objects.filter(
        user_id=user_id,
        quest_type__in=DAILY_BONUS_QUEST_TYPES,
        is_active=True,
        archived_at__isnull=True,
    ).annotate(
        completed_today=Exists(completed_in_day),
    ).filter(
        Q(deadline__isnull=True)
        | Q(deadline__gt=day_start)
        | Q(completed_today=True),
    ).aggregate(
        total=Count('pk'),
        completed=Count('pk', filter=Q(completed_today=True)),
    )
    return DailyBonusProgress(
        total=counts['total'],
        completed=counts['completed'],
    )


@dataclass(frozen=True)
class DailyBonusStatus:
    date: date
    required: int
    completed: int
    total: int
    awarded: bool
    # The XP paid, or the XP on offer while the bonus is not yet paid.
    xp: int


class QuestService:
    @staticmethod
    @transaction.atomic
    def complete_quest(quest_id, user=None, *, locked_quest=None):
        if locked_quest is not None:
            quest = locked_quest
            if user is None:
                user = quest.user
            elif quest.user_id != user.pk:
                raise Quest.DoesNotExist
        else:
            quest_queryset = Quest.objects.select_for_update()
            if user is None:
                quest = quest_queryset.get(pk=quest_id)
                user = quest.user
            else:
                quest = quest_queryset.get(pk=quest_id, user=user)
        character = Character.locked_for_user_id(user.id)
        # Take the timestamp only after the per-user lock is held. A timestamp
        # read before the lock can be older than rows committed by a request
        # that locked first, hiding their XP from the 24h cap below.
        now = timezone.now()
        category_name = ''
        if quest.category_id:
            category_name = QuestCategory.objects.select_for_update().get(
                pk=quest.category_id,
            ).name
        if quest.archived_at is not None or not quest.is_active:
            raise QuestRuleError(
                'This quest is inactive and cannot be completed.',
                code='quest_inactive',
            )
        if quest.deadline is not None and quest.deadline < now:
            raise QuestRuleError(
                'This quest deadline has passed.',
                code='quest_deadline_passed',
            )

        today, day_start, next_day_start = local_day(now, character.timezone)
        if quest.quest_type in (Quest.QuestType.DAILY, Quest.QuestType.HABIT):
            existing_completion = QuestCompletion.objects.filter(
                quest=quest,
                completed_at__gte=day_start,
                completed_at__lt=next_day_start,
            ).order_by('-completed_at').first()
            if existing_completion:
                if existing_completion.status == QuestCompletion.CompletionStatus.FAILED:
                    raise QuestAlreadyFailedError(
                        'This daily or habit quest has already been failed today.'
                    )
                raise QuestAlreadyCompletedError(
                    'This daily or habit quest has already been completed today.'
                )
        elif quest.quest_type == Quest.QuestType.ONE_TIME:
            existing_completion = quest.completions.first()
            if existing_completion:
                if existing_completion.status == QuestCompletion.CompletionStatus.FAILED:
                    raise QuestAlreadyFailedError(
                        f'Quest {quest_id} has already been failed.'
                    )
                raise QuestAlreadyCompletedError(
                    f'Quest {quest_id} has already been completed.'
                )
        # Every registered source: completions and daily bonuses. Boss
        # rewards register none on purpose; they are outside the cap.
        xp_left = xp_left_in_24h(user.id, now)
        xp_awarded = min(quest.xp_reward, xp_left)
        if xp_awarded <= 0:
            raise QuestRuleError(
                'The XP limit for the last 24 hours has been reached.',
                code='xp_limit_reached',
            )

        completion = QuestCompletion.objects.create(
            user=user,
            quest=quest,
            category_name_at_completion=category_name,
            status=QuestCompletion.CompletionStatus.COMPLETED,
            completed_at=now,
            xp_awarded=xp_awarded,
            is_one_time=quest.quest_type == Quest.QuestType.ONE_TIME,
        )
        daily_bonus = None
        if quest.quest_type in DAILY_BONUS_QUEST_TYPES:
            daily_bonus = QuestService._award_daily_bonus(
                completion,
                today,
                day_start,
                next_day_start,
                xp_left=xp_left - xp_awarded,
            )
        result = EventResult()
        # The bonus rides on this event rather than a handler of its own, so
        # the XP handler adds it before the level check and the wallet pays
        # it, without another lock or a second reading of the cap.
        dispatcher.dispatch(
            QuestCompletedEvent(
                quest_id=quest.id,
                user_id=user.id,
                completed_at=completion.completed_at,
                result=result,
                xp_awarded=xp_awarded,
                completion_id=completion.id,
                is_one_time=completion.is_one_time,
                bonus_xp=daily_bonus.xp_awarded if daily_bonus else 0,
                daily_bonus_id=daily_bonus.id if daily_bonus else None,
            )
        )
        return result.as_dict()

    @staticmethod
    def _award_daily_bonus(completion, today, day_start, next_day_start, *, xp_left):
        """Record the day's bonus if ``completion`` finished every bonus quest.

        Runs inside ``complete_quest`` under the character lock, so the check
        and the insert cannot race. The bonus counts toward the 24-hour cap:
        it is cut to the XP left, and nothing is recorded when none is left.
        """
        bonus_xp = min(settings.QUESTIFY_DAILY_BONUS_XP, xp_left)
        if bonus_xp <= 0:
            return None
        # Checked by local date, the key of the unique constraint, so a date
        # repeated by a timezone change ends here instead of in an error.
        if DailyBonus.objects.filter(
            user_id=completion.user_id,
            local_date=today,
        ).exists():
            return None
        progress = daily_bonus_progress(
            completion.user_id,
            day_start,
            next_day_start,
        )
        if not progress.earned:
            return None
        return DailyBonus.objects.create(
            user_id=completion.user_id,
            local_date=today,
            awarded_at=completion.completed_at,
            xp_awarded=bonus_xp,
            quest_completion=completion,
        )

    @staticmethod
    def daily_bonus_status(user):
        """Today's progress toward the daily bonus, in the user's timezone."""
        # A missing character is recreated with the default zone, UTC.
        timezone_name = Character.objects.filter(user=user).values_list(
            'timezone',
            flat=True,
        ).first() or 'UTC'
        now = timezone.now()
        today, day_start, next_day_start = local_day(now, timezone_name)
        progress = daily_bonus_progress(user.pk, day_start, next_day_start)
        bonus = DailyBonus.objects.filter(user=user, local_date=today).first()
        return DailyBonusStatus(
            date=today,
            required=progress.required,
            completed=progress.completed,
            total=progress.total,
            awarded=bonus is not None,
            xp=bonus.xp_awarded if bonus else settings.QUESTIFY_DAILY_BONUS_XP,
        )

    @staticmethod
    @transaction.atomic
    def fail_quest(quest_id, user, *, locked_quest=None):
        if locked_quest is None:
            quest = Quest.objects.select_for_update().get(pk=quest_id, user=user)
        else:
            quest = locked_quest
            if quest.user_id != user.pk:
                raise Quest.DoesNotExist
        character = Character.locked_for_user_id(user.id)
        now = timezone.now()
        if quest.archived_at is not None or not quest.is_active:
            raise QuestRuleError(
                'This quest is inactive and cannot be failed.',
                code='quest_inactive',
            )
        # No deadline check: a missed deadline is exactly what a failure
        # records. Completing and moving the deadline stay closed after it,
        # so refusing this too would leave archiving as the only way out.

        category_name = ''
        if quest.category_id:
            category_name = QuestCategory.objects.select_for_update().get(
                pk=quest.category_id,
            ).name
        _, day_start, next_day_start = local_day(now, character.timezone)
        if quest.quest_type in (Quest.QuestType.DAILY, Quest.QuestType.HABIT):
            existing_completion = QuestCompletion.objects.filter(
                quest=quest,
                completed_at__gte=day_start,
                completed_at__lt=next_day_start,
            ).order_by('-completed_at').first()
        else:
            existing_completion = quest.completions.first()

        if existing_completion:
            if existing_completion.status == QuestCompletion.CompletionStatus.COMPLETED:
                raise QuestAlreadyCompletedError(
                    f'Quest {quest_id} has already been completed.'
                )
            raise QuestAlreadyFailedError(f'Quest {quest_id} has already been failed.')

        failed_completions = QuestCompletion.objects.filter(
            user=user,
            status=QuestCompletion.CompletionStatus.FAILED,
        )
        if failed_completions.filter(
            completed_at__gte=day_start,
            completed_at__lt=next_day_start,
        ).count() >= settings.QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER_PER_DAY:
            raise QuestRuleError(
                'The daily failed-quest limit has been reached.',
                code='daily_fail_limit_reached',
            )
        if failed_completions.count() >= (
            settings.QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER
        ):
            raise QuestRuleError(
                'The lifetime failed-quest limit has been reached.',
                code='lifetime_fail_limit_reached',
            )

        return QuestCompletion.objects.create(
            user=user,
            quest=quest,
            category_name_at_completion=category_name,
            completed_at=now,
            xp_awarded=0,
            status=QuestCompletion.CompletionStatus.FAILED,
        )

    @staticmethod
    @transaction.atomic
    def complete_for_user(user, quest_id):
        """Lock the user's quest and complete it; returns the result dict.

        Raises ``NotFoundError`` for an id that is not one of the user's
        unarchived quests.
        """
        quest = _get_locked(_live_quests(user), quest_id, 'quest_not_found')
        return QuestService.complete_quest(quest.pk, user, locked_quest=quest)

    @staticmethod
    @transaction.atomic
    def fail_for_user(user, quest_id):
        """Lock the user's quest and fail it; returns the completion row."""
        quest = _get_locked(_live_quests(user), quest_id, 'quest_not_found')
        return QuestService.fail_quest(quest.pk, user, locked_quest=quest)

    @staticmethod
    def _lock_category(user, category):
        """Lock the chosen category so a concurrent DELETE cannot orphan the FK.

        The caller validated the category before this transaction began;
        without the lock a category deleted in between surfaces as a 500.
        """
        if category is None:
            return
        locked = QuestCategory.objects.select_for_update().filter(
            pk=category.pk,
            user=user,
        ).first()
        if locked is None:
            raise QuestInputError(
                'This category no longer exists.',
                code='category_gone',
                field='category',
            )

    @staticmethod
    @transaction.atomic
    def create_quest(user, **fields):
        """Create a quest with the server's reward, within the user's quota.

        ``fields`` are the validated inputs (``QUEST_FIELDS``; ``title`` and
        ``quest_type`` are required) and ``category`` is a category of the
        user's. Archived quests count toward the quota.
        """
        _check_fields(fields, QUEST_FIELDS)
        Character.locked_for_user_id(user.pk)
        if Quest.objects.filter(user=user).count() >= (
            settings.QUESTIFY_MAX_QUESTS_PER_USER
        ):
            raise QuestInputError(
                'Quest limit reached for this account.',
                code='quest_limit_reached',
            )
        QuestService._lock_category(user, fields.get('category'))
        return Quest.objects.create(
            user=user,
            xp_reward=XP_REWARD_BY_TYPE[fields['quest_type']],
            **fields,
        )

    @staticmethod
    @transaction.atomic
    def update_quest(user, quest_id, **fields):
        """Change an unarchived quest of the user's; returns it.

        The reward follows a change of type, which is refused once the quest
        has a completion. A deadline that has passed cannot be changed.
        Raises ``NotFoundError`` for an archived or foreign quest.
        """
        _check_fields(fields, QUEST_FIELDS)
        quest = _get_locked(_live_quests(user), quest_id, 'quest_not_found')
        if (
            'deadline' in fields
            and quest.deadline is not None
            and quest.deadline < timezone.now()
            and fields['deadline'] != quest.deadline
        ):
            raise QuestInputError(
                'The deadline has passed and cannot be changed.',
                code='deadline_locked',
                field='deadline',
            )
        QuestService._lock_category(user, fields.get('category'))
        new_type = fields.get('quest_type', quest.quest_type)
        type_changed = new_type != quest.quest_type
        if type_changed and QuestCompletion.objects.filter(
            quest=quest,
        ).exists():
            raise QuestInputError(
                'Quest type cannot change after it has a completion.',
                code='quest_type_locked',
                field='quest_type',
            )
        for name, value in fields.items():
            setattr(quest, name, value)
        if type_changed:
            quest.xp_reward = XP_REWARD_BY_TYPE[new_type]
        quest.save()
        return quest

    @staticmethod
    @transaction.atomic
    def archive_quest(user, quest_id):
        """Hide the quest and deactivate it.

        It stays in the history and counts toward the quota.
        """
        quest = _get_locked(_live_quests(user), quest_id, 'quest_not_found')
        quest.is_active = False
        quest.archived_at = timezone.now()
        quest.save(update_fields=('is_active', 'archived_at', 'updated_at'))
        return quest


def _live_quests(user):
    return Quest.objects.filter(user=user, archived_at__isnull=True)


class CategoryService:
    @staticmethod
    @transaction.atomic
    def create_category(user, **fields):
        _check_fields(fields, CATEGORY_FIELDS)
        Character.locked_for_user_id(user.pk)
        if QuestCategory.objects.filter(user=user).count() >= (
            settings.QUESTIFY_MAX_CATEGORIES_PER_USER
        ):
            raise QuestInputError(
                'Category limit reached for this account.',
                code='category_limit_reached',
            )
        return QuestCategory.objects.create(user=user, **fields)

    @staticmethod
    @transaction.atomic
    def update_category(user, category_id, **fields):
        """Raises ``NotFoundError`` after a concurrent DELETE."""
        _check_fields(fields, CATEGORY_FIELDS)
        category = _get_locked(
            QuestCategory.objects.filter(user=user),
            category_id,
            'category_not_found',
        )
        for name, value in fields.items():
            setattr(category, name, value)
        category.save()
        return category

    @staticmethod
    @transaction.atomic
    def delete_category(user, category_id):
        """Delete a category; its quests keep existing without one.

        Serializes with quest create/update, which lock the category row too.
        The lock order must match complete/fail (quest rows, then category):
        taking the category first deadlocks against a concurrent completion.
        """
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            raise NotFoundError('Not found.', code='category_not_found') from None
        list(
            Quest.objects.select_for_update().filter(
                category_id=category_id,
                user=user,
            ).order_by('pk').values_list('pk', flat=True)
        )
        category = _get_locked(
            QuestCategory.objects.filter(user=user),
            category_id,
            'category_not_found',
        )
        category.delete()


class StreakService:
    @staticmethod
    def _get_general_streak(user):
        user_id = getattr(user, 'pk', user)
        # Serialize creation and updates for NULL-category streaks per user.
        Character.objects.select_for_update().get(user_id=user_id)
        streak, _ = Streak.objects.get_or_create(
            user_id=user_id,
            quest_category=None,
        )
        return streak

    @staticmethod
    @transaction.atomic
    def record_completion(user, completed_at: date | datetime):
        user_id = getattr(user, 'pk', user)
        character = Character.objects.select_for_update().get(user_id=user_id)
        if isinstance(completed_at, datetime):
            if timezone.is_naive(completed_at):
                raise TypeError('completed_at must be timezone-aware')
            completion_date = timezone.localtime(
                completed_at,
                ZoneInfo(character.timezone),
            ).date()
        elif isinstance(completed_at, date):
            completion_date = completed_at
        else:
            raise TypeError('completed_at must be a date or aware datetime')

        streak, _ = Streak.objects.get_or_create(
            user_id=user_id,
            quest_category=None,
        )
        if streak.last_completed_date is None:
            streak.current_length = 1
        else:
            days_since_completion = (
                completion_date - streak.last_completed_date
            ).days
            if days_since_completion <= 0:
                return streak
            if days_since_completion == 1:
                streak.current_length += 1
            elif days_since_completion == 2 and streak.freezes_available:
                streak.freezes_available -= 1
                streak.current_length += 1
            else:
                streak.current_length = 1

        streak.last_completed_date = completion_date
        streak.longest_length = max(
            streak.longest_length,
            streak.current_length,
        )
        streak.save()
        return streak

    @staticmethod
    @transaction.atomic
    def grant_freeze(user, amount=1):
        if amount <= 0:
            return StreakService._get_general_streak(user)
        streak = StreakService._get_general_streak(user)
        streak.freezes_available += amount
        streak.save(update_fields=('freezes_available',))
        return streak
