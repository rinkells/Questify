from django.db import transaction
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from events.dispatcher import dispatcher
from events.event_types import EventResult, QuestCompletedEvent

from .models import Quest, QuestCompletion, Streak


User = get_user_model()


class QuestAlreadyCompletedError(ValueError):
    pass


class QuestService:
    @staticmethod
    @transaction.atomic
    def complete_quest(quest_id, user=None):
        quest_queryset = Quest.objects.select_for_update()
        if user is None:
            quest = quest_queryset.get(pk=quest_id)
            user = quest.user
        else:
            quest = quest_queryset.get(pk=quest_id, user=user)
        now = timezone.now()
        if not quest.is_active:
            raise ValidationError('This quest is inactive and cannot be completed.')
        if quest.deadline is not None and quest.deadline < now:
            raise ValidationError('This quest deadline has passed.')
        if quest.quest_type in (Quest.QuestType.DAILY, Quest.QuestType.HABIT):
            today = timezone.localtime(now).date()
            day_start = timezone.make_aware(datetime.combine(today, time.min))
            next_day_start = timezone.make_aware(
                datetime.combine(today + timedelta(days=1), time.min)
            )
            if QuestCompletion.objects.filter(
                quest=quest,
                status=QuestCompletion.CompletionStatus.COMPLETED,
                completed_at__gte=day_start,
                completed_at__lt=next_day_start,
            ).exists():
                raise ValidationError(
                    'This daily or habit quest has already been completed today.'
                )
        if (
            quest.quest_type == Quest.QuestType.ONE_TIME
            and quest.completions.filter(
                status=QuestCompletion.CompletionStatus.COMPLETED,
            ).exists()
        ):
            raise QuestAlreadyCompletedError(
                f'Quest {quest_id} has already been completed.'
            )
        completion = QuestCompletion.objects.create(
            quest=quest,
            status=QuestCompletion.CompletionStatus.COMPLETED,
        )
        result = EventResult()
        dispatcher.dispatch(
            QuestCompletedEvent(
                quest_id=quest.id,
                user_id=user.id,
                completed_at=completion.completed_at or timezone.now(),
                result=result,
            )
        )
        return result.as_dict()

    @staticmethod
    @transaction.atomic
    def fail_quest(quest_id, user):
        quest = Quest.objects.select_for_update().get(pk=quest_id, user=user)
        return QuestCompletion.objects.create(
            quest=quest,
            status=QuestCompletion.CompletionStatus.FAILED,
        )

    @staticmethod
    def create_quest(user, **data):
        category = data.get('category')
        if category is not None and category.user_id != user.id:
            raise ValueError('Category must belong to the current user.')
        return Quest.objects.create(user=user, **data)


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
    def record_completion(user, completion_date: date):
        if not isinstance(completion_date, date):
            raise TypeError('completion_date must be a date')

        streak = StreakService._get_general_streak(user)
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
    def grant_freeze(user):
        streak = StreakService._get_general_streak(user)
        streak.freezes_available += 1
        streak.save(update_fields=('freezes_available',))
        return streak
