from rest_framework import serializers

from .models import Character, Stat


class StatSerializer(serializers.ModelSerializer):
    stat_type = serializers.CharField(source='stat_type.name', read_only=True)

    class Meta:
        model = Stat
        fields = ('stat_type', 'value')


class CharacterSerializer(serializers.ModelSerializer):
    stats = StatSerializer(many=True, read_only=True)

    class Meta:
        model = Character
        fields = ('id', 'level', 'xp', 'xp_to_next_level', 'stats')
