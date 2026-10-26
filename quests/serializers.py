from django.conf import settings
from rest_framework import serializers

from .models import Quest, QuestCategory


class QuestCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = QuestCategory
        fields = ('id', 'name', 'icon')


class QuestSerializer(serializers.ModelSerializer):
    category = serializers.PrimaryKeyRelatedField(
        queryset=QuestCategory.objects.none(),
        required=False,
        allow_null=True,
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=settings.QUESTIFY_MAX_DESCRIPTION_LENGTH,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        user = self.context.get('user', user)
        if user is not None and getattr(user, 'is_authenticated', True):
            self.fields['category'].queryset = QuestCategory.objects.filter(
                user=user,
            )

    def validate_deadline(self, value):
        if value is not None and not 2000 <= value.year <= 2100:
            raise serializers.ValidationError(
                'Deadline must be between the years 2000 and 2100.'
            )
        return value

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
        read_only_fields = ('id', 'xp_reward', 'created_at', 'updated_at')


class QuestCompleteResponseSerializer(serializers.Serializer):
    xp_gained = serializers.IntegerField()
    leveled_up = serializers.BooleanField()
    new_level = serializers.IntegerField(allow_null=True)
    stats_updated = serializers.DictField()
    achievements_unlocked = serializers.ListField(
        child=serializers.IntegerField(),
    )
    streak_length = serializers.IntegerField()
