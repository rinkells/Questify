"""The level curve: XP thresholds and progress within a level.

Level ``n`` is reached at ``xp_threshold(n - 1)`` XP, so the XP a character
needs for the next level is ``xp_threshold(level)``
(``Character.xp_to_next_level``).
"""
from dataclasses import dataclass


def xp_threshold(level):
    """Total XP at which a character moves past ``level``; 0 for level 0."""
    return round(100 * level**1.2)


def level_for_xp(xp):
    """The level a total of ``xp`` reaches."""
    low = 0
    high = 1
    while xp_threshold(high) <= xp:
        low = high
        high *= 2

    while low + 1 < high:
        middle = (low + high) // 2
        if xp_threshold(middle) <= xp:
            low = middle
        else:
            high = middle
    return low + 1


@dataclass(frozen=True)
class XPProgress:
    level: int
    xp: int
    # Total XP at which this level began and at which the next one begins.
    level_start_xp: int
    next_level_xp: int

    @property
    def xp_in_level(self):
        return max(self.xp - self.level_start_xp, 0)

    @property
    def xp_for_level(self):
        """XP between the two thresholds."""
        return self.next_level_xp - self.level_start_xp

    @property
    def xp_to_next_level(self):
        return max(self.next_level_xp - self.xp, 0)

    @property
    def percent(self):
        """Whole percent of the level done, 0-100."""
        if self.xp_for_level <= 0:
            return 0
        return min(100, self.xp_in_level * 100 // self.xp_for_level)


def xp_progress(level, xp):
    """Progress of a character at ``level`` with ``xp`` total XP.

    The stored level is used rather than ``level_for_xp(xp)``: the level
    never goes down, so it may be above what the XP alone gives.
    """
    level = max(level, 1)
    return XPProgress(
        level=level,
        xp=xp,
        level_start_xp=xp_threshold(level - 1),
        next_level_xp=xp_threshold(level),
    )
