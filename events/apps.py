from django.apps import AppConfig


class EventsConfig(AppConfig):
    name = 'events'

    def ready(self):
        from .handlers import (
            BossRewardXPHandler,
            LevelCheckHandler,
            LoggingHandler,
            StreakFreezeHandler,
            StreakUpdateHandler,
            StatUpdateHandler,
            XPRewardHandler,
        )
        from .dispatcher import dispatcher
        from .event_types import (
            BossDefeatedEvent,
            Event,
            LevelUpEvent,
            QuestCompletedEvent,
        )

        dispatcher.register(QuestCompletedEvent, XPRewardHandler().handle)
        dispatcher.register(QuestCompletedEvent, StatUpdateHandler().handle)
        dispatcher.register(QuestCompletedEvent, LevelCheckHandler().handle)
        dispatcher.register(QuestCompletedEvent, StreakUpdateHandler().handle)
        # No level check: the completion's own LevelCheckHandler runs after it.
        dispatcher.register(BossDefeatedEvent, BossRewardXPHandler().handle)
        dispatcher.register(LevelUpEvent, StreakFreezeHandler().handle)
        dispatcher.register(Event, LoggingHandler().handle)
