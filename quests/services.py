from django.db import transaction
from django.utils import timezone

from events.dispatcher import dispatcher
from events.event_types import QuestCompletedEvent

from .models import Quest, QuestCompletion


class QuestService:
    @staticmethod
    @transaction.atomic
    def complete_quest(quest_id):
        quest = Quest.objects.select_for_update().get(pk=quest_id)
        completion = QuestCompletion.objects.create(
            quest=quest,
            status=QuestCompletion.CompletionStatus.COMPLETED,
        )
        dispatcher.dispatch(
            QuestCompletedEvent(
                quest_id=quest.id,
                user_id=quest.user_id,
                completed_at=completion.completed_at or timezone.now(),
            )
        )
        return completion
