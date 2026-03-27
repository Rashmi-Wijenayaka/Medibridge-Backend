from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0005_scan_uploaded_by_and_role'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='visit_count',
            field=models.PositiveIntegerField(default=1),
        ),
    ]
