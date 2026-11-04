from django.urls import path
from django.views.generic import RedirectView

from . import views

app_name = 'web'

urlpatterns = [
    path('', RedirectView.as_view(pattern_name='web:today'), name='home'),
    path('today/', views.today, name='today'),
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),
    path('quests/', views.quests, name='quests'),
    path('quests/new/', views.quest_new, name='quest-new'),
    path('quests/<int:quest_id>/edit/', views.quest_edit, name='quest-edit'),
    path(
        'quests/<int:quest_id>/archive/',
        views.quest_archive,
        name='quest-archive',
    ),
    path(
        'quests/<int:quest_id>/complete/',
        views.quest_complete,
        name='quest-complete',
    ),
    path('quests/<int:quest_id>/fail/', views.quest_fail, name='quest-fail'),
    path('bosses/', views.bosses, name='bosses'),
    path('bosses/new/', views.boss_new, name='boss-new'),
    path('bosses/<int:boss_id>/steps/', views.boss_attach, name='boss-attach'),
    path(
        'bosses/<int:boss_id>/steps/<int:quest_id>/detach/',
        views.boss_detach,
        name='boss-detach',
    ),
    path('bosses/<int:boss_id>/archive/', views.boss_archive, name='boss-archive'),
    path('skills/', views.skills, name='skills'),
    path(
        'skills/<int:skill_id>/unlock/',
        views.skill_unlock,
        name='skill-unlock',
    ),
    path('achievements/', views.achievements, name='achievements'),
    path('rewards/', views.rewards, name='rewards'),
    path('rewards/new/', views.reward_new, name='reward-new'),
    path('rewards/<int:reward_id>/edit/', views.reward_edit, name='reward-edit'),
    path(
        'rewards/<int:reward_id>/archive/',
        views.reward_archive,
        name='reward-archive',
    ),
    path(
        'rewards/<int:reward_id>/redeem/',
        views.reward_redeem,
        name='reward-redeem',
    ),
    path(
        'rewards/<int:reward_id>/card/',
        views.reward_card_view,
        name='reward-card',
    ),
]
