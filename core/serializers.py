from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .errors import DomainError
from .models import Character, Stat
from .selectors import get_current_streak
from .services import check_timezone_change, realign_streak


class DetailSerializer(serializers.Serializer):
    """The ``{'detail': '...'}`` body of every client-facing API error."""

    detail = serializers.CharField()


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
        return get_current_streak(character)

    def validate_timezone(self, value):
        if self.instance is None:
            return value
        try:
            check_timezone_change(self.instance, value)
        except DomainError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def update(self, instance, validated_data):
        old_timezone = instance.timezone
        instance = super().update(instance, validated_data)
        if instance.timezone != old_timezone:
            realign_streak(instance)
        return instance

    class Meta:
        model = Character
        fields = (
            'id',
            'level',
            'xp',
            'xp_to_next_level',
            'coins',
            'timezone',
            'current_streak',
            'stats',
        )
        read_only_fields = (
            'id',
            'level',
            'xp',
            'xp_to_next_level',
            'coins',
            'current_streak',
            'stats',
        )
