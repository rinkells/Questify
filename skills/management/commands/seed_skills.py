from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import StatType
from skills.models import Skill, SkillRequirement


RequirementType = SkillRequirement.RequirementType

STAT_TYPES = {
    'WIS': ('Wisdom', 'owl'),
    'INT': ('Intelligence', 'brain'),
    'STR': ('Strength', 'sword'),
}

# (name, stat type, description, requirements, children); a requirement is
# (type, threshold) or (type, threshold, category name).
SKILL_TREE = [
    (
        'First Steps', 'WIS', 'Complete your first quest.',
        [(RequirementType.QUEST_COUNT, 1)],
        [
            (
                'Routine Builder', 'WIS', 'Keep showing up: 20 quests and 500 XP.',
                [
                    (RequirementType.QUEST_COUNT, 20),
                    (RequirementType.TOTAL_XP, 500),
                ],
                [
                    (
                        'Unstoppable', 'WIS', '100 quests and 2500 XP.',
                        [
                            (RequirementType.QUEST_COUNT, 100),
                            (RequirementType.TOTAL_XP, 2500),
                        ],
                        [],
                    ),
                ],
            ),
            (
                'Scholar', 'INT', 'Complete 5 quests in a "Study" category.',
                [(RequirementType.QUEST_COUNT_BY_CATEGORY, 5, 'Study')],
                [
                    (
                        'Deep Focus', 'INT', '25 "Study" quests and 1000 XP.',
                        [
                            (RequirementType.QUEST_COUNT_BY_CATEGORY, 25, 'Study'),
                            (RequirementType.TOTAL_XP, 1000),
                        ],
                        [],
                    ),
                ],
            ),
            (
                'Athlete', 'STR', 'Complete 5 quests in a "Health" category.',
                [(RequirementType.QUEST_COUNT_BY_CATEGORY, 5, 'Health')],
                [
                    (
                        'Endurance', 'STR', 'Complete 25 "Health" quests.',
                        [(RequirementType.QUEST_COUNT_BY_CATEGORY, 25, 'Health')],
                        [],
                    ),
                ],
            ),
        ],
    ),
]


class Command(BaseCommand):
    help = (
        'Create an example skill tree. Skills that already exist (by name) '
        'are left unchanged, so running it again is safe.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        stat_types = {}
        for name, (display_name, icon) in STAT_TYPES.items():
            stat_types[name], _ = StatType.objects.get_or_create(
                name=name,
                defaults={'display_name': display_name, 'icon': icon},
            )

        created = 0
        pending = [(None, order, node) for order, node in enumerate(SKILL_TREE)]
        while pending:
            parent, order, node = pending.pop(0)
            name, stat_type, description, requirements, children = node
            skill = Skill.objects.filter(name=name).first()
            if skill is None:
                skill = Skill(
                    name=name,
                    description=description,
                    category=stat_types[stat_type],
                    parent=parent,
                    order=order,
                )
                skill.full_clean()
                skill.save()
                for requirement_type, threshold, *category in requirements:
                    requirement = SkillRequirement(
                        skill=skill,
                        requirement_type=requirement_type,
                        threshold=threshold,
                        category_name=category[0] if category else '',
                    )
                    requirement.full_clean()
                    requirement.save()
                created += 1
            pending.extend(
                (skill, child_order, child)
                for child_order, child in enumerate(children)
            )

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} skill(s); {Skill.objects.count()} in the catalog.'
        ))
