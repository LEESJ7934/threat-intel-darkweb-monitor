from django.apps import AppConfig


class MongodbconnectConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mongoDbConnect'

    def ready(self):
        from governance.django_controls import connect_signals
        connect_signals()
