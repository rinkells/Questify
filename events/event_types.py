from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Event:
    pass


@dataclass(frozen=True)
class QuestCompletedEvent(Event):
    quest_id: int
    user_id: int
    completed_at: datetime


@dataclass(frozen=True)
class LevelUpEvent(Event):
    user_id: int
    old_level: int
    new_level: int


@dataclass(frozen=True)
class AchievementUnlockedEvent(Event):
    user_id: int
    achievement_id: int


@dataclass(frozen=True)
class BossDamagedEvent(Event):
    boss_id: int
    damage: int
    remaining_hp: int


@dataclass(frozen=True)
class BossDefeatedEvent(Event):
    boss_id: int
    user_id: int
