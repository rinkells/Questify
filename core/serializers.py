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
