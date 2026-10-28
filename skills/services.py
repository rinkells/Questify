from dataclasses import asdict, dataclass
from functools import cached_property

from django.db import transaction

from core.models import Character
from events.dispatcher import dispatcher
from events.event_types import EventResult, SkillUnlockedEvent
from quests import selectors

from .models import (
    CATALOG_READ_LOCK,
    Skill,
    SkillRequirement,
    UserSkill,
    lock_skill_catalog,
)


RequirementType = SkillRequirement.RequirementType


class SkillNotFoundError(LookupError):
    pass


class SkillUnlockError(ValueError):
    pass


class SkillRequirementsNotMetError(SkillUnlockError):
    pass


class SkillParentLockedError(SkillUnlockError):
    pass


@dataclass(frozen=True)
class RequirementProgress:
    type: str
    category: str | None
    current: int
    required: int
    met: bool

    def describe(self):
        if self.type == RequirementType.TOTAL_XP:
            label = 'XP'
        elif self.type == RequirementType.QUEST_COUNT:
            label = 'completed quests'
        elif self.type == RequirementType.QUEST_COUNT_BY_CATEGORY:
            label = f'completed quests in "{self.category}"'
        else:
            label = self.type
        return f'{label} {self.current}/{self.required}'


@dataclass(frozen=True)
class SkillEvaluationContext:
    """Progress shared by every skill evaluated in one request.

    Built from at most three queries however many skills and requirements
    are checked: the character's XP (skipped when the caller passes it), the
    unlocked skill ids, and one aggregate over the user's completed
    ``QuestCompletion`` rows (see ``quests.selectors.completion_stats``),
    run only once a quest-count requirement is evaluated.

    ``total_xp`` is ``Character.xp``, the same total that levels and the
    ``total_xp`` achievement use. Completions of archived or deleted quests
    still count, and categories are matched by the name recorded at
    completion, case-insensitively.
    """

    user_id: int
    total_xp: int
    unlocked_skill_ids: frozenset[int]

    @classmethod
    def for_user(cls, user_id, total_xp=None):
        if total_xp is None:
            total_xp = Character.objects.filter(user_id=user_id).values_list(
                'xp',
                flat=True,
            ).first() or 0
        return cls(
            user_id=user_id,
            total_xp=total_xp,
            unlocked_skill_ids=frozenset(
                UserSkill.objects.filter(user_id=user_id).values_list(
                    'skill_id',
                    flat=True,
                )
            ),
        )

    @cached_property
    def _completion_stats(self):
        return selectors.completion_stats(self.user_id)

    @property
    def completed_count(self):
        return self._completion_stats.completed_count

    @property
    def completed_by_category(self):
        return self._completion_stats.counts_by_category

    def progress(self, requirement):
        evaluator = REQUIREMENT_EVALUATORS.get(requirement.requirement_type)
        current = 0 if evaluator is None else evaluator(self, requirement)
        return RequirementProgress(
            type=requirement.requirement_type,
            category=requirement.category_name or None,
            current=current,
            required=requirement.threshold,
            # A type removed from the code must not leave its skills unlockable.
            met=evaluator is not None and current >= requirement.threshold,
        )

    def parent_locked(self, skill):
        return (
            skill.parent_id is not None
            and skill.parent_id not in self.unlocked_skill_ids
        )

    def status(self, skill, progress):
        """The one availability rule, used by both the tree and unlocking."""
        if skill.id in self.unlocked_skill_ids:
            return 'unlocked'
        if self.parent_locked(skill):
            return 'locked'
        if all(item.met for item in progress):
            return 'available'
        return 'locked'


def _total_xp(context, requirement):
    return context.total_xp


def _quest_count(context, requirement):
    return context.completed_count


def _quest_count_by_category(context, requirement):
    # The stored key, which clean() and save() derive from category_name with
    # selectors.category_key and which requirements are unique on.
    return context.completed_by_category.get(requirement.category_key, 0)


REQUIREMENT_EVALUATORS = {
    RequirementType.TOTAL_XP: _total_xp,
    RequirementType.QUEST_COUNT: _quest_count,
    RequirementType.QUEST_COUNT_BY_CATEGORY: _quest_count_by_category,
}


@dataclass(frozen=True)
class SkillUnlock:
    user_skill: UserSkill
    achievements_unlocked: list[int]


class SkillService:
    @staticmethod
    def get_tree(user):
        context = SkillEvaluationContext.for_user(user.pk)
        skills = list(
            Skill.objects.select_related('category').prefetch_related(
                'requirements',
            )
        )
        nodes = {}
        for skill in skills:
            progress = [
                context.progress(requirement)
                for requirement in skill.requirements.all()
            ]
            nodes[skill.id] = {
                'id': skill.id,
                'name': skill.name,
                'description': skill.description,
                'category': skill.category.name,
                'parent_id': skill.parent_id,
                'status': context.status(skill, progress),
                'requirements': [asdict(item) for item in progress],
                'children': [],
            }

        # Built from the roots down, so a node caught in a parent cycle that
        # bypassed validation is left out instead of looping forever.
        roots = []
        for skill in skills:
            if skill.parent_id is None:
                roots.append(nodes[skill.id])
            elif skill.parent_id in nodes:
                nodes[skill.parent_id]['children'].append(nodes[skill.id])
        return roots

    @staticmethod
    @transaction.atomic
    def unlock_skill(user, skill_id):
        """Unlock a skill for the user and publish ``SkillUnlockedEvent``.

        A shared lock on the catalog comes first, so an admin edit in
        progress finishes before the skill and its requirements are read and
        is what gets checked. The character row is locked only after it: an
        unlock waiting for a catalog edit must not hold that row, or the
        user's quest completions and profile updates would wait as well.
        Requests then serialize on the character row, so two parallel unlocks
        cannot both pass. Unlocking a skill the user already has returns the
        existing row, publishes nothing and reports no achievements. A
        failing event handler propagates and rolls the unlock back.
        """
        lock_skill_catalog(CATALOG_READ_LOCK)
        character = Character.locked_for_user_id(user.pk)
        try:
            skill = Skill.objects.select_related('parent').prefetch_related(
                'requirements',
            ).get(pk=skill_id)
        except Skill.DoesNotExist:
            # A dedicated error, so a DoesNotExist from an event handler
            # below is not mistaken for an unknown skill.
            raise SkillNotFoundError(skill_id) from None

        context = SkillEvaluationContext.for_user(
            user.pk,
            total_xp=character.xp,
        )
        # status() reports 'unlocked' before looking at progress; checking it
        # first spares a repeated unlock the completion aggregate.
        if skill.id in context.unlocked_skill_ids:
            return SkillUnlock(
                user_skill=UserSkill.objects.get(user=user, skill=skill),
                achievements_unlocked=[],
            )
        progress = [
            context.progress(requirement)
            for requirement in skill.requirements.all()
        ]
        if context.status(skill, progress) != 'available':
            if context.parent_locked(skill):
                raise SkillParentLockedError(
                    f'Unlock "{skill.parent.name}" before "{skill.name}".'
                )
            raise SkillRequirementsNotMetError(
                'Requirements not met: '
                + '; '.join(item.describe() for item in progress if not item.met)
                + '.'
            )

        user_skill = UserSkill.objects.create(user=user, skill=skill)
        result = EventResult()
        dispatcher.dispatch(
            SkillUnlockedEvent(
                user_id=user.pk,
                skill_id=skill.pk,
                result=result,
            ),
        )
        return SkillUnlock(
            user_skill=user_skill,
            achievements_unlocked=result.achievements_unlocked,
        )
