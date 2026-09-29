from rest_framework import serializers

from .models import Quest, QuestCategory


class QuestCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = QuestCategory
        fields = ('id', 'name', 'icon')


class QuestSerializer(serializers.ModelSerializer):
    class Meta:
        model = Quest
        fields = (
            'id',
            'category',
            'title',
            'description',
            'quest_type',
            'xp_reward',
            'is_active',
            'deadline',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class QuestCompleteResponseSerializer(serializers.Serializer):
    xp_gained = serializers.IntegerField()
    leveled_up = serializers.BooleanField()
    new_level = serializers.IntegerField(allow_null=True)
    stats_updated = serializers.DictField()
    achievements_unlocked = serializers.ListField(
        child=serializers.IntegerField(),
    )
