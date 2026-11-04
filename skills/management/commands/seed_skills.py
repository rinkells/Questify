from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import StatType
from quests.selectors import category_key
from skills.models import Skill, SkillRequirement


RequirementType = SkillRequirement.RequirementType

# Codes and English names stay: the web shows them in Ukrainian by code
# (web.texts.STAT_LABELS).
STAT_TYPES = {
    'WIS': ('Wisdom', 'owl'),
    'INT': ('Intelligence', 'brain'),
    'STR': ('Strength', 'sword'),
}

# (category name, earlier name). Completions are matched against the name through
# category_key, which takes any apostrophe people type for the one written here.
STUDY = ('Навчання', 'Study')
HEALTH = ("Здоров'я", 'Health')

# (name, earlier name, stat type, description, requirements, children); a
# requirement is (type, threshold) or (type, threshold, category name, earlier
# category name). The earlier names are what find the rows seeded before the
# texts were translated.
SKILL_TREE = [
    (
        'Перші кроки', 'First Steps', 'WIS', 'Виконай свій перший квест.',
        [(RequirementType.QUEST_COUNT, 1)],
        [
            (
                'Майстер рутини', 'Routine Builder', 'WIS',
                'Не зупиняйся: 20 квестів і 500 XP.',
                [
                    (RequirementType.QUEST_COUNT, 20),
                    (RequirementType.TOTAL_XP, 500),
                ],
                [
                    (
                        'Незупинний', 'Unstoppable', 'WIS', '100 квестів і 2500 XP.',
                        [
                            (RequirementType.QUEST_COUNT, 100),
                            (RequirementType.TOTAL_XP, 2500),
                        ],
                        [],
                    ),
                ],
            ),
            (
                'Дослідник', 'Scholar', 'INT',
                'Виконай 5 квестів у категорії «Навчання».',
                [(RequirementType.QUEST_COUNT_BY_CATEGORY, 5, *STUDY)],
                [
                    (
                        'Глибокий фокус', 'Deep Focus', 'INT',
                        '25 квестів у категорії «Навчання» і 1000 XP.',
                        [
                            (RequirementType.QUEST_COUNT_BY_CATEGORY, 25, *STUDY),
                            (RequirementType.TOTAL_XP, 1000),
                        ],
                        [],
                    ),
                ],
            ),
            (
                'Атлет', 'Athlete', 'STR',
                "Виконай 5 квестів у категорії «Здоров'я».",
                [(RequirementType.QUEST_COUNT_BY_CATEGORY, 5, *HEALTH)],
                [
                    (
                        'Витривалість', 'Endurance', 'STR',
                        "Виконай 25 квестів у категорії «Здоров'я».",
                        [(RequirementType.QUEST_COUNT_BY_CATEGORY, 25, *HEALTH)],
                        [],
                    ),
                ],
            ),
        ],
    ),
]


def move_to_current_category(skill, requirement_type, category, earlier_category):
    """Give a requirement created under the earlier category name the current one.

    The skill, the type and the stored key identify it, so the row keeps its id and
    threshold, and going through save() rewrites category_key with the name. A
    requirement already under the current name stays, since a second one would break
    the unique constraint, and so does one an admin pointed at a third category.
    Returns whether it moved.
    """
    requirements = SkillRequirement.objects.filter(
        skill=skill,
        requirement_type=requirement_type,
    )
    earlier_key = category_key(earlier_category)
    if (
        requirements.filter(category_key=category_key(category)).exists()
        or not requirements.filter(category_key=earlier_key).exists()
    ):
        return False
    SkillRequirement.objects.update_or_create(
        skill=skill,
        requirement_type=requirement_type,
        category_key=earlier_key,
        defaults={'category_name': category},
    )
    return True


class Command(BaseCommand):
    help = (
        'Create an example skill tree, or bring the names and descriptions of '
        'the skills it already created, and the category names of their '
        'requirements, up to date. Thresholds, parents and order are left '
        'unchanged, so running it again is safe.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        stat_types = {}
        for name, (display_name, icon) in STAT_TYPES.items():
            stat_types[name], _ = StatType.objects.get_or_create(
                name=name,
                defaults={'display_name': display_name, 'icon': icon},
            )

        created = updated = moved = 0
        existing = []
        pending = [(None, order, node) for order, node in enumerate(SKILL_TREE)]
        while pending:
            parent, order, node = pending.pop(0)
            name, earlier_name, stat_type, description, requirements, children = node
            # The current name first: renaming the earlier row onto a name that
            # is taken would break the unique constraint.
            skill = (
                Skill.objects.filter(name=name).first()
                or Skill.objects.filter(name=earlier_name).first()
            )
            is_new = skill is None
            if is_new:
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
            elif (skill.name, skill.description) != (name, description):
                skill.name = name
                skill.description = description
                # Only the texts: unlocks and the tree stay as they are.
                skill.save(update_fields=('name', 'description'))
                updated += 1
            if not is_new:
                existing.append((skill, requirements))
            pending.extend(
                (skill, child_order, child)
                for child_order, child in enumerate(children)
            )

        # After every skill: a requirement written before a later skill's row
        # could deadlock with an unlock (see the lock modes in skills/models.py).
        for skill, requirements in existing:
            for requirement_type, _threshold, *category in requirements:
                if category and move_to_current_category(
                    skill,
                    requirement_type,
                    *category,
                ):
                    moved += 1

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} skill(s), updated {updated}, moved {moved} '
            f'requirement(s) to the current category name; '
            f'{Skill.objects.count()} in the catalog.'
        ))
