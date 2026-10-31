from django.conf import settings

from events.dispatcher import dispatcher
from events.event_types import (
    BossDefeatedEvent,
    BossProgress,
    Event,
    QuestCompletedEvent,
)

from .models import Boss, BossDefeat, BossStep
from .services import boss_reward


class BossProgressHandler:
    """Report the boss a completed step hit, and defeat it when it is beaten.

    Registered first for QuestCompletedEvent (bosses precede events in
    INSTALLED_APPS), so the BossDefeatedEvent it dispatches adds its XP
    before the completion's XPRewardHandler and LevelCheckHandler run, and
    that level check covers it.

    It takes no lock. complete_quest holds the completed quest and the
    character row, which attaching, detaching and archiving (every change to
    which steps a boss has, and to whether it is archived) take too. Other
    writes can still land meanwhile: archiving another step's quest or
    changing its type (whether that step is live) takes only the quest row,
    and editing the title only the boss row. The decision therefore rests on
    a single query of the boss's steps, which sees one committed state, so
    any interleaving acts like some serial order.

    A boss is beaten when its last live step is completed and at least
    QUESTIFY_BOSS_MIN_STEPS live steps are completed. Archiving or detaching
    the last open step completes nothing, so the boss stays open until a new
    step is added and completed. A defeated boss is frozen: the steps that
    beat it are marked counted, and its other steps no longer hit it.

    The reward is the one XP outside the 24-hour cap: it pays for long work,
    its steps fall under the cap themselves, and it is bounded by the step
    limit (QUESTIFY_MAX_BOSS_STEPS x QUESTIFY_BOSS_XP_PER_STEP) and the
    boss quotas.
    """

    def handle(self, event: Event):
        if not isinstance(event, QuestCompletedEvent):
            return
        if not event.is_one_time:
            # Not a live step (see live_steps), so no query is needed.
            return
        boss = Boss.objects.filter(
            steps__quest_id=event.quest_id,
            archived_at__isnull=True,
            defeat__isnull=True,
        ).values('id', 'title').first()
        if boss is None:
            return
        # The completed quest is one of them: complete_quest refuses an
        # archived quest, and it is one-time.
        steps = dict(
            BossStep.objects.with_status().filter(
                boss_id=boss['id'],
                live=True,
            ).values_list('pk', 'completed')
        )
        completed = [pk for pk, done in steps.items() if done]
        progress = BossProgress(
            id=boss['id'],
            title=boss['title'],
            hp=len(steps) - len(completed),
            max_hp=len(steps),
        )
        if event.result is not None:
            event.result.boss = progress
        if progress.hp or len(completed) < settings.QUESTIFY_BOSS_MIN_STEPS:
            return

        # From here on these steps are the boss's HP, whatever becomes of
        # their quests (see live_steps).
        BossStep.objects.filter(pk__in=completed).update(counted=True)
        defeat = BossDefeat.objects.create(
            boss_id=boss['id'],
            user_id=event.user_id,
            defeated_at=event.completed_at,
            xp_awarded=boss_reward(len(completed)),
            quest_completion_id=event.completion_id,
        )
        progress.defeated = True
        dispatcher.dispatch(
            BossDefeatedEvent(
                user_id=event.user_id,
                boss_id=boss['id'],
                boss_defeat_id=defeat.id,
                xp_awarded=defeat.xp_awarded,
                result=event.result,
            )
        )
