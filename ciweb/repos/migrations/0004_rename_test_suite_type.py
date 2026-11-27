from django.db import migrations

# The test suite type that was called "indianlaw" (after a repository) is now called "comparison" (after what it does).
OLD, NEW = 'indianlaw', 'comparison'


def forwards(apps, schema_editor):
    apps.get_model('repos', 'RepoProfile').objects.filter(adapter = OLD).update(adapter = NEW)


def backwards(apps, schema_editor):
    apps.get_model('repos', 'RepoProfile').objects.filter(adapter = NEW).update(adapter = OLD)


class Migration(migrations.Migration):
    dependencies = [('repos', '0003_full_words_in_help_texts')]
    operations = [migrations.RunPython(forwards, backwards)]
