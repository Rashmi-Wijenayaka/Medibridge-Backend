#!/usr/bin/env python
"""
Test script to verify email notifications are working.
Usage: python test_email_notifications.py
"""
import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend_project.settings')
sys.path.insert(0, os.path.dirname(__file__))
django.setup()

from api.models import Patient
from api.sms_utils import notify_patient_new_message, notify_patient_diagnosis_update
from django.conf import settings

def test_email_config():
    """Verify email configuration"""
    print("=" * 60)
    print("EMAIL CONFIGURATION CHECK")
    print("=" * 60)
    print(f"Email Backend: {settings.EMAIL_BACKEND}")
    print(f"Email Host: {settings.EMAIL_HOST}")
    print(f"Email Port: {settings.EMAIL_PORT}")
    print(f"Email Use TLS: {settings.EMAIL_USE_TLS}")
    print(f"From Email: {settings.DEFAULT_FROM_EMAIL}")
    print(f"Notifications Enabled: {settings.EMAIL_NOTIFICATIONS_ENABLED}")
    print()
    
    if not settings.EMAIL_NOTIFICATIONS_ENABLED:
        print("❌ ERROR: Email notifications are DISABLED!")
        print("   Make sure EMAIL_HOST_USER and EMAIL_HOST_PASSWORD are set in .env")
        return False
    
    print("✓ Email configuration is valid")
    return True

def test_patient_with_email():
    """Test sending notification to a patient with email"""
    print("\n" + "=" * 60)
    print("TESTING EMAIL NOTIFICATIONS")
    print("=" * 60)
    
    # Create a test patient
    print("\nCreating test patient with email...")
    patient, created = Patient.objects.get_or_create(
        full_name='Test Patient',
        defaults={
            'email': 'test@example.com',
            'phone_number': '555-0123',
            'age': 30,
            'queue_number': 'TEST001'
        }
    )
    
    if created:
        print(f"✓ Created test patient: {patient.full_name}")
    else:
        print(f"✓ Using existing patient: {patient.full_name}")
    
    print(f"  Email: {patient.email}")
    
    # Test new message notification
    print("\nTesting doctor message notification...")
    result = notify_patient_new_message(
        patient, 
        "Dr. Smith",
        "Please review your latest test results..."
    )
    
    if result:
        print("✓ Doctor message notification sent successfully")
    else:
        print("❌ Failed to send doctor message notification")
        print("   Check backend logs for SMTP errors")
    
    # Test diagnosis notification
    print("\nTesting diagnosis update notification...")
    result = notify_patient_diagnosis_update(
        patient,
        "Your preliminary diagnosis is ready for review."
    )
    
    if result:
        print("✓ Diagnosis notification sent successfully")
    else:
        print("❌ Failed to send diagnosis notification")
        print("   Check backend logs for SMTP errors")
    
    print("\n" + "=" * 60)
    print("Testing complete!")
    print("=" * 60)

if __name__ == '__main__':
    if test_email_config():
        test_patient_with_email()
