from django.urls import path

from . import views
from django.contrib.auth import views as auth_views

urlpatterns = [
    path("", views.latest_data_table, name="latest_data_table"),
    path("events/<str:document_id>/", views.event_detail, name="event_detail"),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
]
