"""
Notification utilities.

The project keeps the existing function names for backward compatibility,
but delivery is done through email (free method) instead of Twilio SMS.
"""

import logging
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_sms_notification(phone_number, message_text):
    """
    Compatibility shim retained for older call sites.

    This no longer sends SMS. It logs and returns False.
    Use patient-based helpers below which deliver by email.
    """
    logger.info("SMS delivery is disabled. Free email notifications are active instead.")
    logger.debug("Suppressed SMS target=%s message=%s", phone_number, message_text)
    return False


def _send_email_notification(recipient_email, subject, message_text):
    if not recipient_email:
        logger.info("Skipping notification: no patient email provided")
        return False

    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
    if not from_email:
        logger.warning("DEFAULT_FROM_EMAIL is not configured")
        return False

    try:
        send_mail(
            subject=subject,
            message=message_text,
            from_email=from_email,
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        logger.info("Email notification sent to %s", recipient_email)
        return True
    except Exception as exc:
        logger.error("Failed to send email notification to %s: %s", recipient_email, str(exc))
        return False


def notify_patient_new_message(patient, sender_name, message_preview=None):
    """
    Send email notification to patient when they receive a new message from doctor.
    
    Args:
        patient (Patient): Patient object with phone_number
        sender_name (str): Name of the person sending the message (e.g., "Dr. Smith")
        message_preview (str, optional): Short preview of the message content
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    
    if not patient.email:
        logger.info("No email for patient %s", patient.full_name)
        return False

    # Create message text with optional preview
    if message_preview and len(message_preview) > 50:
        message_preview = message_preview[:50] + "..."

    if message_preview:
        email_text = f"Hi {patient.full_name}, you have a new message from {sender_name}: {message_preview}"
    else:
        email_text = f"Hi {patient.full_name}, you have a new message from {sender_name}. Log in to your medical chat to view."

    return _send_email_notification(
        patient.email,
        "New message from your care team",
        email_text,
    )


def notify_patient_diagnosis_update(patient, message_text="Your diagnosis summary is ready. Log in to view your results."):
    """
    Send email notification to patient when admin uploads diagnosis/notes.
    
    Args:
        patient (Patient): Patient object with phone_number
        message_text (str): Custom message to send
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    
    if not patient.email:
        logger.info("No email for patient %s", patient.full_name)
        return False

    email_text = f"Hi {patient.full_name}, {message_text}"
    return _send_email_notification(
        patient.email,
        "Diagnosis update available",
        email_text,
    )


def notify_doctor_new_patient_message(patient, sender_name, message_preview=None):
    """
    Send email notifications to doctors when a patient sends a new message.

    Args:
        patient (Patient): Patient profile associated with the message
        sender_name (str): Sender display name
        message_preview (str, optional): Short preview of the message content

    Returns:
        bool: True if at least one notification email was sent successfully
    """
    from .models import User

    recipient_emails = list(
        User.objects
        .filter(role='doctor')
        .exclude(email__isnull=True)
        .exclude(email='')
        .values_list('email', flat=True)
        .distinct()
    )

    if not recipient_emails:
        logger.info("Skipping doctor notification: no doctor emails configured")
        return False

    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
    if not from_email:
        logger.warning("DEFAULT_FROM_EMAIL is not configured")
        return False

    if message_preview and len(message_preview) > 120:
        message_preview = message_preview[:120] + "..."

    queue_label = (patient.queue_number or 'N/A').strip() or 'N/A'
    area_label = (patient.area_of_concern or 'N/A').strip() or 'N/A'

    subject = f"New patient message received (Queue {queue_label})"
    if message_preview:
        body = (
            f"A new patient message has been received.\n\n"
            f"Queue: {queue_label}\n"
            f"Area of concern: {area_label}\n"
            f"From: {sender_name}\n"
            f"Message preview: {message_preview}\n\n"
            f"Please log in to MediBridge Doctor Chat to reply."
        )
    else:
        body = (
            f"A new patient message has been received.\n\n"
            f"Queue: {queue_label}\n"
            f"Area of concern: {area_label}\n"
            f"From: {sender_name}\n\n"
            f"Please log in to MediBridge Doctor Chat to reply."
        )

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=from_email,
            recipient_list=recipient_emails,
            fail_silently=False,
        )
        logger.info("Doctor notification email sent to %d recipient(s)", len(recipient_emails))
        return True
    except Exception as exc:
        logger.error("Failed to send doctor notification email: %s", str(exc))
        return False
