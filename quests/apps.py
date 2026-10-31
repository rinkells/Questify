from django.apps import AppConfig


class QuestsConfig(AppConfig):
    name = 'quests'

    def ready(self):
        from .xp_cap import completed_quest_xp, daily_bonus_xp, register_xp_source

        # Another app's XP joins the cap by registering in its own ready().
        register_xp_source(completed_quest_xp)
        register_xp_source(daily_bonus_xp)
