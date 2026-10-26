from zoneinfo import ZoneInfo
from datetime import timedelta

from drf_spectacular.utils import extend_schema_field
from django.utils import timezone
from rest_framework import serializers

from .models import Character, Stat


class StatSerializer(serializers.ModelSerializer):
    stat_type = serializers.CharField(source='stat_type.name', read_only=True)

    class Meta:
        model = Stat
        fields = ('stat_type', 'value')


class CharacterSerializer(serializers.ModelSerializer):
    stats = StatSerializer(many=True, read_only=True)
    current_streak = serializers.SerializerMethodField()

    @extend_schema_field(serializers.IntegerField)
    def get_current_streak(self, character) -> int:
        prefetched_streaks = getattr(
            character.user,
            'prefetched_streaks',
            None,
        )
        if prefetched_streaks is None:
            streak = character.user.streaks.filter(
                quest_category=None,
            ).first()
        else:
            streak = next(
                (
                    item
                    for item in prefetched_streaks
                    if item.quest_category_id is None
                ),
                None,
            )
        if streak is None or streak.last_completed_date is None:
            return 0

        today = timezone.localdate(timezone=ZoneInfo(character.timezone))
        days_since_completion = (today - streak.last_completed_date).days
        streak_is_current = 0 <= days_since_completion <= 1 or (
            days_since_completion == 2 and streak.freezes_available > 0
        )
        return streak.current_length if streak_is_current else 0

    def validate_timezone(self, value):
        if self.instance is None or value == self.instance.timezone:
            return value

        completed_recently = self.instance.user.quest_completions.filter(
            status='completed',
            completed_at__gte=timezone.now() - timedelta(hours=24),
        ).exists()
        if completed_recently:
            raise serializers.ValidationError(
                'Timezone can be changed 24 hours after the last quest completion.'
            )
        return value

    def update(self, instance, validated_data):
        old_timezone = instance.timezone
        instance = super().update(instance, validated_data)
        if instance.timezone != old_timezone:
            self._realign_streak(instance)
        return instance

    @staticmethod
    def _realign_streak(character):
        """Re-express the streak's last day in the character's new timezone.

        Streak gaps are measured in the current timezone. Leaving the stored
        date in the old zone would let a timezone switch shrink a real gap
        (up to ~26 hours) and keep a streak without spending a freeze.
        """
        from quests.models import Streak

        streak = Streak.objects.select_for_update().filter(
            user_id=character.user_id,
            quest_category=None,
        ).first()
        if streak is None or streak.last_completed_date is None:
            return
        last_completion = character.user.quest_completions.filter(
            status='completed',
        ).order_by('-completed_at').first()
        if last_completion is None:
            return
        streak.last_completed_date = timezone.localtime(
            last_completion.completed_at,
            ZoneInfo(character.timezone),
        ).date()
        streak.save(update_fields=('last_completed_date',))
        # The prefetched copy loaded by the view is now stale.
        if hasattr(character.user, 'prefetched_streaks'):
            del character.user.prefetched_streaks

    class Meta:
        model = Character
        fields = (
            'id',
            'level',
            'xp',
            'xp_to_next_level',
            'timezone',
            'current_streak',
            'stats',
        )
        read_only_fields = (
            'id',
            'level',
            'xp',
            'xp_to_next_level',
            'current_streak',
            'stats',
        )
