from django.db import transaction
from django.utils import timezone

from events.dispatcher import dispatcher
from events.event_types import EventResult, QuestCompletedEvent

from .models import Quest, QuestCompletion


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
        return Quest.objects.create(user=user, **data)
