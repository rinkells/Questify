from django.core.management.base import BaseCommand
from django.db import transaction

from achievements.models import Achievement


ConditionType = Achievement.ConditionType

# (name, earlier name, description, icon, condition type, condition config). The
# earlier name is what finds a row seeded before the texts were translated.
ACHIEVEMENTS = [
    (
        'Переможець босів',
        'Boss Slayer',
        'Переможи свого першого боса.',
        'crossed-swords',
        ConditionType.BOSSES_DEFEATED_COUNT,
        {'threshold': 1},
    ),
]


class Command(BaseCommand):
    help = (
        'Create example achievements, or bring the names and descriptions of '
        'the ones already created up to date. Their conditions are left '
        'unchanged, so running it again is safe.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        created = updated = 0
        for name, earlier_name, description, icon, condition_type, config in ACHIEVEMENTS:
            # Names are not unique here, so update_or_create could find two rows.
            achievement = (
                Achievement.objects.filter(name=name).first()
                or Achievement.objects.filter(name=earlier_name).first()
            )
            if achievement is None:
                achievement = Achievement(
                    name=name,
                    description=description,
                    icon=icon,
                    condition_type=condition_type,
                    condition_config=config,
                )
                achievement.full_clean()
                achievement.save()
                created += 1
            elif (achievement.name, achievement.description) != (name, description):
                achievement.name = name
                achievement.description = description
                # Only the texts: who has it and what earns it stay as they are.
                achievement.save(update_fields=('name', 'description'))
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} achievement(s), updated {updated}; '
            f'{Achievement.objects.count()} in total.'
        ))
