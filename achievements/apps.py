from django.apps import AppConfig


class AchievementsConfig(AppConfig):
    name = 'achievements'

    def ready(self):
        import achievements.checks  # noqa: F401
        from events.dispatcher import dispatcher
        from events.event_types import QuestCompletedEvent, SkillUnlockedEvent

        from .handlers import AchievementCheckHandler

        handler = AchievementCheckHandler().handle
        # Not LevelUpEvent: it is only dispatched inside a quest completion,
        # whose own check runs later and covers level_reached as well.
        dispatcher.register(QuestCompletedEvent, handler)
        dispatcher.register(SkillUnlockedEvent, handler)
