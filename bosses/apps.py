from django.apps import AppConfig


class BossesConfig(AppConfig):
    name = 'bosses'

    def ready(self):
        from events.dispatcher import dispatcher
        from events.event_types import QuestCompletedEvent

        from .handlers import BossProgressHandler

        # Registered before the events app's handlers (see INSTALLED_APPS):
        # the boss's XP must be added before the completion's level check.
        # Boss rewards register no XP source: they are deliberately outside
        # the 24-hour cap (see BossProgressHandler).
        dispatcher.register(QuestCompletedEvent, BossProgressHandler().handle)
