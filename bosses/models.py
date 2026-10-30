from django.conf import settings
from django.db import models
from django.db.models import Count, Exists, ExpressionWrapper, OuterRef, Q
from django.utils import timezone

from quests.models import MAX_XP_REWARD, Quest, QuestCompletion


def live_steps(*, step='', boss='boss__'):
    """The steps that count toward HP and the reward, as a filter.

    ``step`` and ``boss`` are the lookup paths to the step and to its boss
    from the queried model. Until the defeat a step counts while its quest is
    unarchived and still one-time: quests lets the type change before the
    first completion and knows nothing of bosses, so a step turned daily or
    habit drops out the way an archived one does. A defeated boss is frozen:
    only the steps marked ``counted`` when it was beaten count, whatever
    becomes of their quests.
    """
    return Q(**{
        f'{boss}defeat__isnull': True,
        f'{step}quest__archived_at__isnull': True,
        f'{step}quest__quest_type': Quest.QuestType.ONE_TIME,
    }) | Q(**{f'{step}counted': True})


class BossQuerySet(models.QuerySet):
    def with_progress(self):
        """Annotate ``max_hp`` (live steps) and ``completed_steps``.

        HP is never stored: it is the live steps without a completed row, so
        0 for a defeated boss, whose live steps are those that beat it.
        Counting is distinct, because the join through completions repeats a
        step for each of its rows. The GROUP BY this adds rules out
        ``select_for_update()``, so locks go through a plain queryset.
        """
        live = live_steps(step='steps__', boss='')
        return self.annotate(
            max_hp=Count('steps', filter=live, distinct=True),
            completed_steps=Count(
                'steps',
                filter=live & Q(
                    steps__quest__completions__status=(
                        QuestCompletion.CompletionStatus.COMPLETED
                    ),
                ),
                distinct=True,
            ),
        )


class Boss(models.Model):
    """A goal beaten by completing one-time quests, its steps."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='bosses',
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    archived_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = BossQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=('user', 'archived_at')),
        ]

    @property
    def hp(self):
        """Live steps still to complete; needs ``with_progress()``."""
        return self.max_hp - self.completed_steps

    def __str__(self):
        return self.title


class BossStepQuerySet(models.QuerySet):
    def with_status(self):
        """Annotate ``live`` and whether the quest was completed or failed."""
        Status = QuestCompletion.CompletionStatus
        completions = QuestCompletion.objects.filter(quest=OuterRef('quest'))
        return self.annotate(
            live=ExpressionWrapper(
                live_steps(),
                output_field=models.BooleanField(),
            ),
            completed=Exists(completions.filter(status=Status.COMPLETED)),
            failed=Exists(completions.filter(status=Status.FAILED)),
        )


class BossStep(models.Model):
    boss = models.ForeignKey(
        Boss,
        on_delete=models.CASCADE,
        related_name='steps',
    )
    # One-to-one, so a quest is a step of one boss at most.
    quest = models.OneToOneField(
        Quest,
        on_delete=models.CASCADE,
        related_name='boss_step',
    )
    added_at = models.DateTimeField(default=timezone.now, editable=False)
    # Set at the defeat on the steps that beat the boss, which from then on
    # count instead of the live rule (see live_steps).
    counted = models.BooleanField(default=False, editable=False)

    objects = BossStepQuerySet.as_manager()

    def __str__(self):
        return f'{self.boss}: {self.quest}'


class BossDefeat(models.Model):
    """The completion that beat a boss and the XP it paid in full.

    The reward is the one XP outside the 24-hour cap, so it is never cut.
    """

    # One-to-one, so the database pays a boss once.
    boss = models.OneToOneField(
        Boss,
        on_delete=models.CASCADE,
        related_name='defeat',
        editable=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='boss_defeats',
        editable=False,
    )
    defeated_at = models.DateTimeField(default=timezone.now, editable=False)
    xp_awarded = models.PositiveIntegerField(editable=False)
    quest_completion = models.OneToOneField(
        QuestCompletion,
        on_delete=models.SET_NULL,
        related_name='boss_defeat',
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=('user', 'defeated_at'),
                name='boss_defeat_user_date_idx',
            ),
        ]
        constraints = [
            # Positive, like DailyBonus: the minimum number of steps makes
            # every reward at least one share, and nothing cuts it.
            models.CheckConstraint(
                condition=Q(xp_awarded__gt=0, xp_awarded__lte=MAX_XP_REWARD),
                name='boss_defeat_xp_awarded_between_1_and_1000',
            ),
        ]

    def __str__(self):
        return f'{self.boss} (+{self.xp_awarded})'
