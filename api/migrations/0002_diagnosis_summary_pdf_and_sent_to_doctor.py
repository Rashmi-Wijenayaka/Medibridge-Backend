from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='diagnosis',
            name='sent_to_doctor',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='diagnosis',
            name='summary_pdf',
            field=models.FileField(blank=True, null=True, upload_to='summary_reports/'),
        ),
    ]
