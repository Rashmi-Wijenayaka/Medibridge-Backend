from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class User(AbstractUser):
    ROLE_CHOICES = (
        ('patient', 'Patient'),
        ('doctor', 'Doctor'),
        ('admin', 'System Administrator'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='patient')

    def __str__(self):
        return f"{self.username} ({self.role})"


class Patient(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='patient_profile', null=True, blank=True)
    full_name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    age = models.IntegerField(null=True, blank=True)
    weight = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    height = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    queue_number = models.CharField(max_length=50, blank=True)
    area_of_concern = models.CharField(max_length=100, blank=True)
    visit_count = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.full_name} ({self.queue_number})"


# optional model for storing chat messages if needed
class Message(models.Model):
    sender = models.CharField(max_length=50)
    text = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    # associate messages with a patient when possible so we can replay a conversation
    patient = models.ForeignKey(
        Patient,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='messages'
    )

    def __str__(self):
        return f"{self.sender}: {self.text[:20]}"


class Diagnosis(models.Model):
    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        related_name='diagnoses'
    )
    admin = models.ForeignKey(User, on_delete=models.CASCADE, related_name='admin_diagnoses', null=True, blank=True)
    admin_notes = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    pdf_generated = models.BooleanField(default=False)
    summary_pdf = models.FileField(upload_to='summary_reports/', null=True, blank=True)
    sent_to_doctor = models.BooleanField(default=False)

    def __str__(self):
        return f"Diagnosis for {self.patient.full_name} at {self.created_at.strftime('%Y-%m-%d %H:%M')}"


class DoctorMessage(models.Model):
    SENDER_CHOICES = (
        ('doctor', 'Doctor'),
        ('patient', 'Patient'),
    )

    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        related_name='doctor_messages'
    )
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    text = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.sender.username} -> {self.patient.full_name}: {self.text[:20]}"


class Scan(models.Model):
    UPLOADER_ROLE_CHOICES = (
        ('patient', 'Patient'),
        ('doctor', 'Doctor'),
        ('admin', 'System Administrator'),
    )

    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        related_name='scans'
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_scans'
    )
    uploaded_by_role = models.CharField(
        max_length=20,
        choices=UPLOADER_ROLE_CHOICES,
        default='patient'
    )
    file = models.FileField(upload_to='scans/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Scan for {self.patient.full_name} [{self.uploaded_by_role}] ({self.file.name})"


class PasswordResetOTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='password_reset_otps')
    otp_code = models.CharField(max_length=6)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_active(self):
        return self.used_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"OTP for {self.user.username} ({'active' if self.is_active() else 'inactive'})"
