from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class EventResult:
    xp_gained: int = 0
    leveled_up: bool = False
    new_level: int | None = None
    stats_updated: dict[str, int] = field(default_factory=dict)
    achievements_unlocked: list[int] = field(default_factory=list)
    streak_length: int = 0

    def as_dict(self):
        return {
            'xp_gained': self.xp_gained,
            'leveled_up': self.leveled_up,
            'new_level': self.new_level,
            'stats_updated': self.stats_updated,
            'achievements_unlocked': self.achievements_unlocked,
            'streak_length': self.streak_length,
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
    xp_awarded: int | None = None


@dataclass(frozen=True)
class LevelUpEvent(Event):
    user_id: int
    old_level: int
    new_level: int
    result: EventResult | None = None


@dataclass(frozen=True)
class AchievementUnlockedEvent(Event):
    user_id: int
    achievement_id: int
