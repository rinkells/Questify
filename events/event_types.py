from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BossProgress:
    """The boss a completion hit by completing one of its steps."""

    id: int
    title: str
    # Live steps (see bosses.models.live_steps) still to complete, and in all.
    hp: int
    max_hp: int
    defeated: bool = False
    xp_gained: int = 0
    coins_gained: int = 0

    def as_dict(self):
        defeated = None
        if self.defeated:
            defeated = {'xp': self.xp_gained, 'coins': self.coins_gained}
        return {
            'id': self.id,
            'title': self.title,
            'hp': self.hp,
            'max_hp': self.max_hp,
            'defeated': defeated,
        }


@dataclass
class EventResult:
    xp_gained: int = 0
    coins_gained: int = 0
    leveled_up: bool = False
    new_level: int | None = None
    stats_updated: dict[str, int] = field(default_factory=dict)
    achievements_unlocked: list[int] = field(default_factory=list)
    streak_length: int = 0
    # The daily bonus, kept apart from the quest's own xp_gained/coins_gained.
    bonus_xp_gained: int = 0
    bonus_coins_gained: int = 0
    # Kept apart from xp_gained/coins_gained too, like the daily bonus.
    boss: BossProgress | None = None

    def as_dict(self):
        daily_bonus = None
        if self.bonus_xp_gained or self.bonus_coins_gained:
            daily_bonus = {
                'xp': self.bonus_xp_gained,
                'coins': self.bonus_coins_gained,
            }
        return {
            'xp_gained': self.xp_gained,
            'coins_gained': self.coins_gained,
            'leveled_up': self.leveled_up,
            'new_level': self.new_level,
            'stats_updated': self.stats_updated,
            'achievements_unlocked': self.achievements_unlocked,
            'streak_length': self.streak_length,
            'daily_bonus': daily_bonus,
            'boss': self.boss.as_dict() if self.boss else None,
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
    # The QuestCompletion row, so a handler can tie what it writes to it.
    completion_id: int | None = None
    # Whether the quest was one-time, so a handler that only cares about
    # one-time quests (bosses' steps) can skip its queries for the rest.
    is_one_time: bool = False
    # XP of the daily bonus this completion earned (already capped), paid
    # with the quest's XP, and its quests.DailyBonus row.
    bonus_xp: int = 0
    daily_bonus_id: int | None = None


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


@dataclass(frozen=True)
class SkillUnlockedEvent(Event):
    user_id: int
    skill_id: int
    result: EventResult | None = None


@dataclass(frozen=True)
class BossDefeatedEvent(Event):
    """A boss beaten by a quest completion, dispatched only inside one.

    It has no level check of its own: bosses' completion handler dispatches
    it before XPRewardHandler and LevelCheckHandler run (bosses precede
    events in INSTALLED_APPS), so the completion's level check sees its XP.
    Dispatching it anywhere else (a claim, an admin action) needs a level
    check first.
    """

    user_id: int
    boss_id: int
    boss_defeat_id: int
    # Paid in full: the one XP outside the 24-hour cap. Always positive.
    xp_awarded: int
    result: EventResult | None = None
