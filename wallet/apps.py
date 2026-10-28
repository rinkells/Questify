from django.apps import AppConfig


class WalletConfig(AppConfig):
    name = 'wallet'

    def ready(self):
        from events.dispatcher import dispatcher
        from events.event_types import QuestCompletedEvent

        from .handlers import QuestRewardCoinsHandler

        # Registered after the events app's handlers and before the
        # achievement check (see INSTALLED_APPS).
        dispatcher.register(QuestCompletedEvent, QuestRewardCoinsHandler().handle)
