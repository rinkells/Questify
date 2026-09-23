from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Character


@receiver(post_save, sender=get_user_model())
def create_character_for_user(sender, instance, created, **kwargs):
    if created:
        Character.objects.create(user=instance)
