from django.apps import AppConfig


class WalletConfig(AppConfig):
    name = 'wallet'

    def ready(self):
        from events.dispatcher import dispatcher
        from events.event_types import BossDefeatedEvent, QuestCompletedEvent

        from .handlers import BossRewardCoinsHandler, QuestRewardCoinsHandler

        # Registered after the events app's handlers and before the
        # achievement check (see INSTALLED_APPS).
        dispatcher.register(QuestCompletedEvent, QuestRewardCoinsHandler().handle)
        dispatcher.register(BossDefeatedEvent, BossRewardCoinsHandler().handle)
