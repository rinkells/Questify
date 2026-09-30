from datetime import datetime
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import Character, Stat, StatType
from quests.models import Quest, QuestStatReward
from quests.services import QuestService

from .dispatcher import EventDispatcher
from .event_types import Event, LevelUpEvent, QuestCompletedEvent
from .handlers import (
    LevelCheckHandler,
    LoggingHandler,
    StreakFreezeHandler,
    StatUpdateHandler,
    XPRewardHandler,
)


User = get_user_model()


class EventDispatcherTests(TestCase):
    def test_dispatch_calls_matching_handlers_in_registration_order(self):
        dispatcher = EventDispatcher()
        calls = []
        first = Mock(side_effect=lambda event: calls.append('first'))
        second = Mock(side_effect=lambda event: calls.append('second'))

        dispatcher.register(Event, first)
        dispatcher.register(QuestCompletedEvent, second)
        dispatcher.dispatch(QuestCompletedEvent(1, 2, datetime.now()))

        self.assertEqual(calls, ['first', 'second'])

    def test_dispatch_isolates_handler_errors(self):
        dispatcher = EventDispatcher()
        successful_handler = Mock()
        dispatcher.register(Event, Mock(side_effect=RuntimeError('expected')))
        dispatcher.register(Event, successful_handler)

        dispatcher.dispatch(Event())

        successful_handler.assert_called_once()


class HandlerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='handler-user')
        self.character = self.user.character
        self.quest = Quest.objects.create(
            user=self.user,
            title='Test quest',
            quest_type=Quest.QuestType.ONE_TIME,
            xp_reward=25,
        )
        self.event = QuestCompletedEvent(
            quest_id=self.quest.id,
            user_id=self.user.id,
            completed_at=timezone.now(),
        )

    def test_xp_reward_handler_adds_quest_xp(self):
        XPRewardHandler().handle(self.event)

        self.character.refresh_from_db()
        self.assertEqual(self.character.xp, 25)

    def test_stat_update_handler_adds_all_stat_rewards(self):
        strength = StatType.objects.create(
            name='STR', display_name='Strength', icon='sword'
        )
        creativity = StatType.objects.create(
            name='CRE', display_name='Creativity', icon='palette'
        )
        QuestStatReward.objects.create(quest=self.quest, stat_type=strength, amount=3)
        QuestStatReward.objects.create(quest=self.quest, stat_type=creativity, amount=2)

        StatUpdateHandler().handle(self.event)

        self.assertEqual(Stat.objects.get(character=self.character, stat_type=strength).value, 3)
        self.assertEqual(Stat.objects.get(character=self.character, stat_type=creativity).value, 2)

    def test_level_check_handler_levels_up_and_publishes_event(self):
        self.character.xp = 100
        self.character.save(update_fields=('xp',))

        with patch('events.handlers.dispatcher.dispatch') as dispatch:
            LevelCheckHandler().handle(self.event)

        self.character.refresh_from_db()
        self.assertEqual(self.character.level, 2)
        self.assertEqual(self.character.xp_to_next_level, round(100 * 2**1.2))
        dispatch.assert_called_once_with(LevelUpEvent(self.user.id, 1, 2))

    @patch('events.handlers.logger')
    def test_logging_handler_logs_event(self, logger):
        LoggingHandler().handle(self.event)

        logger.info.assert_called_once()

    @patch('events.handlers.StreakService.grant_freeze')
    def test_streak_freeze_handler_grants_freeze_every_five_levels(
        self,
        grant_freeze,
    ):
        StreakFreezeHandler().handle(LevelUpEvent(self.user.id, 4, 5))
        StreakFreezeHandler().handle(LevelUpEvent(self.user.id, 5, 6))

        grant_freeze.assert_called_once_with(self.user.id)


class QuestServiceTests(TestCase):
    def test_complete_quest_dispatches_event_and_updates_progress(self):
        user = User.objects.create_user(username='service-user')
        quest = Quest.objects.create(
            user=user,
            title='Complete me',
            quest_type=Quest.QuestType.ONE_TIME,
            xp_reward=40,
        )

        completion = QuestService.complete_quest(quest.id)

        self.assertEqual(completion['xp_gained'], 40)
        user.character.refresh_from_db()
        self.assertEqual(user.character.xp, 40)
