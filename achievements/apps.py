from django.apps import AppConfig


class AchievementsConfig(AppConfig):
    name = 'achievements'

    def ready(self):
        from events.dispatcher import dispatcher
        from events.event_types import LevelUpEvent, QuestCompletedEvent

        from .handlers import AchievementCheckHandler

        handler = AchievementCheckHandler().handle
        dispatcher.register(QuestCompletedEvent, handler)
        dispatcher.register(LevelUpEvent, handler)
