#!/usr/bin/env python3
import os
import django
import sys

# Configure Django settings for standalone script
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend_project.settings')
try:
    django.setup()
except Exception as e:
    print('Failed to setup Django:', e)
    raise

from api.models import Patient, Message, DoctorMessage

COMPLETION_MARKER = 'Thank you for completing all the diagnostic questions'

print('Recomputing visit_count for all patients...')
count = 0
for patient in Patient.objects.all():
    try:
        completions = Message.objects.filter(patient=patient, sender='bot', text__contains=COMPLETION_MARKER).count()
        patient_doctor_msgs = DoctorMessage.objects.filter(patient=patient, sender__role='patient').count()

        # If patient has any direct doctor messages, treat that as at least one visit
        inferred_visits = max(completions, 1 if patient_doctor_msgs > 0 else 0)
        inferred_visits = max(1, inferred_visits)  # ensure at least 1

        if (patient.visit_count or 0) != inferred_visits:
            print(f'Patient {patient.id} ({patient.full_name}): visit_count {patient.visit_count} -> {inferred_visits}')
            patient.visit_count = inferred_visits
            patient.save(update_fields=['visit_count'])
            count += 1
    except Exception as e:
        print('Error processing patient', patient.id, e)

print('Done. Updated', count, 'patients.')
