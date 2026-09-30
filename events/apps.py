from django.apps import AppConfig


class EventsConfig(AppConfig):
    name = 'events'

    def ready(self):
        from .handlers import (
            LevelCheckHandler,
            LoggingHandler,
            StreakFreezeHandler,
            StatUpdateHandler,
            XPRewardHandler,
        )
        from .dispatcher import dispatcher
        from .event_types import Event, LevelUpEvent, QuestCompletedEvent

        dispatcher.register(QuestCompletedEvent, XPRewardHandler().handle)
        dispatcher.register(QuestCompletedEvent, StatUpdateHandler().handle)
        dispatcher.register(QuestCompletedEvent, LevelCheckHandler().handle)
        dispatcher.register(LevelUpEvent, StreakFreezeHandler().handle)
        dispatcher.register(Event, LoggingHandler().handle)
