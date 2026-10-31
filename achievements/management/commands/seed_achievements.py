from django.core.management.base import BaseCommand
from django.db import transaction

from achievements.models import Achievement


ConditionType = Achievement.ConditionType

# (name, description, icon, condition type, condition config)
ACHIEVEMENTS = [
    (
        'Boss Slayer',
        'Defeat your first boss.',
        'crossed-swords',
        ConditionType.BOSSES_DEFEATED_COUNT,
        {'threshold': 1},
    ),
]


class Command(BaseCommand):
    help = (
        'Create example achievements. Achievements that already exist (by '
        'name) are left unchanged, so running it again is safe.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        created = 0
        for name, description, icon, condition_type, config in ACHIEVEMENTS:
            if Achievement.objects.filter(name=name).exists():
                continue
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

        self.stdout.write(self.style.SUCCESS(
            f'Created {created} achievement(s); '
            f'{Achievement.objects.count()} in total.'
        ))
