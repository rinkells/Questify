from rest_framework import serializers


class SkillRequirementProgressSerializer(serializers.Serializer):
    type = serializers.CharField()
    category = serializers.CharField(allow_null=True)
    current = serializers.IntegerField()
    required = serializers.IntegerField()
    met = serializers.BooleanField()


class SkillTreeNodeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    description = serializers.CharField()
    category = serializers.CharField()
    parent_id = serializers.IntegerField(allow_null=True)
    status = serializers.ChoiceField(choices=('unlocked', 'available', 'locked'))
    requirements = SkillRequirementProgressSerializer(many=True)
    # Nested nodes of the same shape; a recursive field would only complicate
    # the generated schema.
    children = serializers.ListField(child=serializers.DictField())


class SkillUnlockResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()
    unlocked_at = serializers.DateTimeField()
    # Achievements this unlock earned; empty when the skill was already
    # unlocked.
    achievements_unlocked = serializers.ListField(
        child=serializers.IntegerField(),
    )
