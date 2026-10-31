from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .selectors import format_month


class OffsetDateTimeField(serializers.DateTimeField):
    """A datetime in its own offset (the user's zone), not converted to UTC."""

    def enforce_timezone(self, value):
        return value


class MonthlyXPSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    quests = serializers.IntegerField()
    daily_bonus = serializers.IntegerField()
    bosses = serializers.IntegerField()


class MonthlyUnlocksSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    # In the order they were unlocked.
    names = serializers.ListField(child=serializers.CharField())


class MonthlySummarySerializer(serializers.Serializer):
    """A ``selectors.MonthlySummary``."""

    month = serializers.SerializerMethodField()
    timezone = serializers.CharField()
    # Local midnight of the month's first day and of the next month's.
    starts_at = OffsetDateTimeField()
    ends_at = OffsetDateTimeField()
    # The month starts before the wallet itemizes the user's earnings, so XP
    # and coins earned earlier in it are missing.
    partial = serializers.BooleanField()
    quests_completed = serializers.IntegerField()
    quests_failed = serializers.IntegerField()
    # Completions whose recorded XP is an estimate (older than the record of
    # awarded XP); their XP is not in xp, which comes from the wallet.
    estimated_completions = serializers.IntegerField()
    xp = MonthlyXPSerializer()
    daily_bonuses = serializers.IntegerField()
    # Every credit but the initial grant; equal to xp.total while each coin
    # earned pays for one XP.
    coins_earned = serializers.IntegerField()
    coins_spent = serializers.IntegerField()
    # The balance at the end of the month; for the current month, now.
    balance_end = serializers.IntegerField()
    bosses_defeated = serializers.IntegerField()
    achievements_unlocked = MonthlyUnlocksSerializer()
    skills_unlocked = MonthlyUnlocksSerializer()
    current_level = serializers.IntegerField()

    @extend_schema_field(serializers.CharField)
    def get_month(self, summary):
        return format_month(summary.month)
