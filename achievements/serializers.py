from rest_framework import serializers

from .models import Achievement


class AchievementSerializer(serializers.ModelSerializer):
    condition_type = serializers.CharField(read_only=True, allow_null=True)

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
        is_unlocked = getattr(instance, 'is_unlocked', False)
        if instance.is_hidden and not is_unlocked:
            representation['name'] = '???'
            representation['description'] = None
            representation['condition_type'] = None
            representation.pop('id', None)
            representation.pop('icon', None)
            representation.pop('is_hidden', None)
        return representation
