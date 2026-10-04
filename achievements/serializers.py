from rest_framework import serializers

from .models import Achievement, UserAchievement


class AchievementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Achievement
        fields = (
            'id',
            'name',
            'description',
            'icon',
            'is_hidden',
            'condition_type',
        )
        read_only_fields = fields

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        user = self.context['request'].user
        is_unlocked = UserAchievement.objects.filter(
            user=user,
            achievement=instance,
        ).exists()
        if instance.is_hidden and not is_unlocked:
            representation['name'] = '???'
            representation['description'] = None
        return representation
