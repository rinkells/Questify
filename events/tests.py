from datetime import datetime, timezone as datetime_timezone
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import Character, Stat, StatType
from quests.models import Quest, QuestCompletion, QuestStatReward, Streak
from quests.services import QuestService
from achievements.models import Achievement, UserAchievement

from .dispatcher import EventDispatcher
from .event_types import Event, EventResult, LevelUpEvent, QuestCompletedEvent
from .handlers import (
    LevelCheckHandler,
    LoggingHandler,
    StreakUpdateHandler,
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

    def test_dispatch_logs_and_propagates_handler_errors(self):
        dispatcher = EventDispatcher()
        successful_handler = Mock()
        dispatcher.register(Event, Mock(side_effect=RuntimeError('expected')))
        dispatcher.register(Event, successful_handler)

        with self.assertRaises(RuntimeError):
            dispatcher.dispatch(Event())

        successful_handler.assert_not_called()


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

    def test_stat_update_handler_query_count_does_not_grow_with_rewards(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        stat_types = [
            StatType.objects.create(name=f'S{i}', display_name=f'Stat {i}')
            for i in range(6)
        ]

        def run(count):
            # A fresh user each time, so every run takes the same code path.
            user = User.objects.create_user(username=f'rewards-{count}')
            quest = Quest.objects.create(
                user=user,
                title=f'Rewards {count}',
                quest_type=Quest.QuestType.ONE_TIME,
                xp_reward=50,
            )
            for stat_type in stat_types[:count]:
                QuestStatReward.objects.create(quest=quest, stat_type=stat_type, amount=1)
            event = QuestCompletedEvent(
                quest_id=quest.id,
                user_id=user.id,
                completed_at=timezone.now(),
                result=EventResult(),
            )
            with CaptureQueriesContext(connection) as queries:
                StatUpdateHandler().handle(event)
            return len(queries), event.result.stats_updated

        few, _ = run(1)
        many, updated = run(6)

        self.assertEqual(many, few)
        self.assertEqual(updated, {f'S{i}': 1 for i in range(6)})

    def test_stat_update_handler_sums_duplicate_rewards_for_one_stat(self):
        strength = StatType.objects.create(name='STR', display_name='Strength')
        QuestStatReward.objects.create(quest=self.quest, stat_type=strength, amount=3)
        QuestStatReward.objects.create(quest=self.quest, stat_type=strength, amount=4)
        Stat.objects.create(character=self.character, stat_type=strength, value=10)
        event = QuestCompletedEvent(
            quest_id=self.quest.id,
            user_id=self.user.id,
            completed_at=timezone.now(),
            result=EventResult(),
        )

        StatUpdateHandler().handle(event)

        self.assertEqual(Stat.objects.get(character=self.character, stat_type=strength).value, 17)
        self.assertEqual(event.result.stats_updated, {'STR': 17})

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

    def test_streak_update_handler_uses_event_date_and_updates_result(self):
        event = QuestCompletedEvent(
            quest_id=self.quest.id,
            user_id=self.user.id,
            completed_at=datetime(
                2026,
                10,
                12,
                tzinfo=datetime_timezone.utc,
            ),
        )

        StreakUpdateHandler().handle(event)

        streak = Streak.objects.get(user=self.user, quest_category=None)
        self.assertEqual(streak.last_completed_date.isoformat(), '2026-10-12')

    def test_streak_update_handler_updates_event_result(self):
        event = QuestCompletedEvent(
            quest_id=self.quest.id,
            user_id=self.user.id,
            completed_at=datetime(
                2026,
                10,
                12,
                tzinfo=datetime_timezone.utc,
            ),
            result=EventResult(),
        )

        StreakUpdateHandler().handle(event)

        self.assertEqual(event.result.streak_length, 1)

    @patch('events.handlers.StreakService.grant_freeze')
    def test_streak_freeze_handler_grants_freeze_every_five_levels(
        self,
        grant_freeze,
    ):
        StreakFreezeHandler().handle(LevelUpEvent(self.user.id, 4, 5))
        StreakFreezeHandler().handle(LevelUpEvent(self.user.id, 5, 6))

        grant_freeze.assert_called_once_with(self.user.id, 1)

    def test_level_check_batches_large_xp_without_per_level_dispatch(self):
        self.character.xp = 1_000_000
        self.character.save(update_fields=('xp',))

        with patch('events.handlers.dispatcher.dispatch') as dispatch:
            LevelCheckHandler().handle(self.event)

        self.character.refresh_from_db()
        self.assertGreater(self.character.level, 2_000)
        self.assertEqual(
            self.character.xp_to_next_level,
            round(100 * self.character.level**1.2),
        )
        dispatch.assert_called_once()
        level_event = dispatch.call_args.args[0]
        self.assertEqual(level_event.old_level, 1)
        self.assertEqual(level_event.new_level, self.character.level)

    @patch(
        'events.handlers.StreakService.record_completion',
        side_effect=RuntimeError('streak failed'),
    )
    def test_failed_handler_rolls_back_quest_completion_side_effects(
        self,
        record_completion,
    ):
        stat_type = StatType.objects.create(
            name='HEA',
            display_name='Health',
            icon='heart',
        )
        QuestStatReward.objects.create(
            quest=self.quest,
            stat_type=stat_type,
            amount=4,
        )
        achievement = Achievement.objects.create(
            name='XP achievement',
            condition_type='total_xp',
            condition_config={'threshold': 1},
        )

        with self.assertRaises(RuntimeError):
            QuestService.complete_quest(self.quest.id, self.user)

        self.assertFalse(
            QuestCompletion.objects.filter(quest=self.quest).exists()
        )
        self.character.refresh_from_db()
        self.assertEqual(self.character.xp, 0)
        self.assertFalse(
            Stat.objects.filter(
                character=self.character,
                stat_type=stat_type,
            ).exists()
        )
        self.assertFalse(
            Streak.objects.filter(user=self.user).exists()
        )
        self.assertFalse(
            UserAchievement.objects.filter(
                user=self.user,
                achievement=achievement,
            ).exists()
        )
        record_completion.assert_called_once()


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
