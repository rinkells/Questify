from rest_framework import serializers

from .models import Achievement
from .selectors import MASKED_NAME, is_masked


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
        if is_masked(instance):
            representation['name'] = MASKED_NAME
            representation['description'] = None
            representation['condition_type'] = None
            representation.pop('id', None)
            representation.pop('icon', None)
            representation.pop('is_hidden', None)
        return representation
