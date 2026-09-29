from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class EventResult:
    xp_gained: int = 0
    leveled_up: bool = False
    new_level: int | None = None
    stats_updated: dict[str, int] | dict[Any, int] = None
    achievements_unlocked: list[int] = None

    def __post_init__(self):
        if self.stats_updated is None:
            self.stats_updated = {}
        if self.achievements_unlocked is None:
            self.achievements_unlocked = []

    def as_dict(self):
        return {
            'xp_gained': self.xp_gained,
            'leveled_up': self.leveled_up,
            'new_level': self.new_level,
            'stats_updated': self.stats_updated,
            'achievements_unlocked': self.achievements_unlocked,
        }


@dataclass(frozen=True)
class Event:
    pass


@dataclass(frozen=True)
class QuestCompletedEvent(Event):
    quest_id: int
    user_id: int
    completed_at: datetime
    result: EventResult | None = None


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
