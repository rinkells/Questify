from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import serializers

from .models import Boss, BossStep


def bosses_for_display(queryset):
    """What BossSerializer reads: progress, steps and the defeat."""
    return queryset.with_progress().prefetch_related(
        Prefetch(
            'steps',
            queryset=BossStep.objects.with_status().select_related(
                'quest',
            ).order_by('pk'),
        ),
        'defeat',
    )


class BossStepSerializer(serializers.ModelSerializer):
    quest = serializers.IntegerField(source='quest_id', read_only=True)
    title = serializers.CharField(source='quest.title', read_only=True)
    # 'completed', 'failed', 'expired' (deadline passed), 'inactive' (an
    # archived quest too) or 'open'. A live step that is failed or expired
    # blocks the boss until detached or archived; an inactive one, until
    # its quest is active again.
    status = serializers.SerializerMethodField()
    # Whether the step counts toward HP and the reward (see live_steps):
    # false once its quest is archived or no longer one-time, and on a
    # defeated boss true only for the steps that beat it.
    live = serializers.BooleanField(read_only=True)

    class Meta:
        model = BossStep
        fields = ('quest', 'title', 'status', 'live', 'added_at')
        read_only_fields = fields

    def get_status(self, step) -> str:
        if step.completed:
            return 'completed'
        if step.failed:
            return 'failed'
        # The rules complete_quest applies.
        quest = step.quest
        if quest.deadline is not None and quest.deadline < timezone.now():
            return 'expired'
        if not quest.is_active:
            return 'inactive'
        return 'open'


class BossDefeatSerializer(serializers.Serializer):
    xp = serializers.IntegerField(source='xp_awarded')
    # One coin per XP, as for quests.
    coins = serializers.IntegerField(source='xp_awarded')
    defeated_at = serializers.DateTimeField()


class BossSerializer(serializers.ModelSerializer):
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=settings.QUESTIFY_MAX_DESCRIPTION_LENGTH,
    )
    hp = serializers.IntegerField(read_only=True)
    max_hp = serializers.IntegerField(read_only=True)
    steps = BossStepSerializer(many=True, read_only=True)
    defeated = BossDefeatSerializer(source='defeat', read_only=True, allow_null=True)

    class Meta:
        model = Boss
        fields = (
            'id',
            'title',
            'description',
            'hp',
            'max_hp',
            'steps',
            'defeated',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class BossStepCreateSerializer(serializers.Serializer):
    # Bounded, so an id beyond bigint is a 400 rather than a database error.
    quest = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
