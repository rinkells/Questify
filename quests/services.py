from django.db import transaction
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from events.dispatcher import dispatcher
from events.event_types import EventResult, QuestCompletedEvent
from core.models import Character

from .models import (
	MAX_XP_REWARD,
	Quest,
	QuestCategory,
	QuestCompletion,
	Streak,
)


class QuestAlreadyCompletedError(ValueError):
    pass


class QuestAlreadyFailedError(ValueError):
    pass


def get_xp_24h_cap():
    # Read at call time so the cap follows the active settings.
    return getattr(settings, 'QUESTIFY_XP_24H_CAP', 1_000)


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
        now = timezone.now()
        character = Character.locked_for_user_id(user.id)
        category_name = ''
        if quest.category_id:
            category_name = QuestCategory.objects.select_for_update().get(
                pk=quest.category_id,
            ).name
        if quest.archived_at is not None or not quest.is_active:
            raise ValidationError('This quest is inactive and cannot be completed.')
        if quest.deadline is not None and quest.deadline < now:
            raise ValidationError('This quest deadline has passed.')

        user_timezone = ZoneInfo(character.timezone)
        today = timezone.localtime(now, user_timezone).date()
        day_start = timezone.make_aware(
            datetime.combine(today, time.min),
            user_timezone,
        )
        next_day_start = timezone.make_aware(
            datetime.combine(today + timedelta(days=1), time.min),
            user_timezone,
        )
        if quest.quest_type in (Quest.QuestType.DAILY, Quest.QuestType.HABIT):
            existing_completion = QuestCompletion.objects.filter(
                quest=quest,
                completed_at__gte=day_start,
                completed_at__lt=next_day_start,
            ).first()
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
        recent_completions = QuestCompletion.objects.filter(
            user=user,
            status=QuestCompletion.CompletionStatus.COMPLETED,
            completed_at__gte=now - timedelta(hours=24),
            completed_at__lt=now,
        )
        xp_earned_in_24h = (
            recent_completions.filter(
                xp_awarded_is_estimate=False,
            ).aggregate(total=Sum('xp_awarded'))['total'] or 0
        )
        xp_earned_in_24h += (
            recent_completions.filter(xp_awarded_is_estimate=True).count()
            * MAX_XP_REWARD
        )
        xp_awarded = min(
            quest.xp_reward,
            max(0, get_xp_24h_cap() - xp_earned_in_24h),
        )
        if xp_awarded <= 0:
            raise ValidationError('The XP limit for the last 24 hours has been reached.')

        completion = QuestCompletion.objects.create(
            user=user,
            quest=quest,
            category_name_at_completion=category_name,
            status=QuestCompletion.CompletionStatus.COMPLETED,
            completed_at=now,
            xp_awarded=xp_awarded,
        )
        result = EventResult()
        dispatcher.dispatch(
            QuestCompletedEvent(
                quest_id=quest.id,
                user_id=user.id,
                completed_at=completion.completed_at,
                result=result,
                xp_awarded=xp_awarded,
            )
        )
        return result.as_dict()

    @staticmethod
    @transaction.atomic
    def fail_quest(quest_id, user, *, locked_quest=None):
        if locked_quest is None:
            quest = Quest.objects.select_for_update().get(pk=quest_id, user=user)
        else:
            quest = locked_quest
            if quest.user_id != user.pk:
                raise Quest.DoesNotExist
        now = timezone.now()
        if quest.archived_at is not None or not quest.is_active:
            raise ValidationError('This quest is inactive and cannot be failed.')
        if quest.deadline is not None and quest.deadline < now:
            raise ValidationError('This quest deadline has passed and cannot be failed.')

        character = Character.locked_for_user_id(user.id)
        category_name = ''
        if quest.category_id:
            category_name = QuestCategory.objects.select_for_update().get(
                pk=quest.category_id,
            ).name
        user_timezone = ZoneInfo(character.timezone)
        today = timezone.localtime(now, user_timezone).date()
        day_start = timezone.make_aware(
            datetime.combine(today, time.min),
            user_timezone,
        )
        next_day_start = timezone.make_aware(
            datetime.combine(today + timedelta(days=1), time.min),
            user_timezone,
        )
        if quest.quest_type in (Quest.QuestType.DAILY, Quest.QuestType.HABIT):
            existing_completion = QuestCompletion.objects.filter(
                quest=quest,
                completed_at__gte=day_start,
                completed_at__lt=next_day_start,
            ).first()
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
            raise ValidationError('The daily failed-quest limit has been reached.')
        if failed_completions.count() >= (
            settings.QUESTIFY_MAX_FAILED_COMPLETIONS_PER_USER
        ):
            raise ValidationError('The lifetime failed-quest limit has been reached.')

        return QuestCompletion.objects.create(
            user=user,
            quest=quest,
            category_name_at_completion=category_name,
            completed_at=now,
            xp_awarded=0,
            status=QuestCompletion.CompletionStatus.FAILED,
        )

class StreakService:
    @staticmethod
    def _get_general_streak(user):
        user_id = getattr(user, 'pk', user)
        # Serialize creation and updates for NULL-category streaks per user.
        from core.models import Character
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
