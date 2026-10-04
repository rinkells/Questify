from django.conf import settings
from django.db import models


class Achievement(models.Model):
    class ConditionType(models.TextChoices):
        TOTAL_XP = 'total_xp', 'Total XP'
        QUEST_COUNT = 'quest_count', 'Quest count'
        QUEST_COUNT_BY_CATEGORY = (
            'quest_count_by_category',
            'Quest count by category',
        )
        STREAK_LENGTH = 'streak_length', 'Streak length'
        LEVEL_REACHED = 'level_reached', 'Level reached'

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, null=True)
    icon = models.CharField(max_length=100, blank=True, null=True)
    is_hidden = models.BooleanField(default=False)
    condition_type = models.CharField(
        max_length=40,
        choices=ConditionType.choices,
    )
    condition_config = models.JSONField(default=dict)

    def __str__(self):
        return self.name


class UserAchievement(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='user_achievements',
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name='user_achievements',
    )
    unlocked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'achievement'),
                name='unique_user_achievement',
            ),
        ]
        ordering = ('-unlocked_at',)

    def __str__(self):
        return f'{self.user}: {self.achievement}'
