from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0004_patient_email_patient_phone_number'),
    ]

    operations = [
        migrations.AddField(
            model_name='scan',
            name='uploaded_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='uploaded_scans',
                to='api.user',
            ),
        ),
        migrations.AddField(
            model_name='scan',
            name='uploaded_by_role',
            field=models.CharField(
                choices=[
                    ('patient', 'Patient'),
                    ('doctor', 'Doctor'),
                    ('admin', 'System Administrator'),
                ],
                default='patient',
                max_length=20,
            ),
        ),
    ]
