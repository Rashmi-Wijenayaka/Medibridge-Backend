from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.permissions import AllowAny
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.contrib.auth import authenticate
from django.core.exceptions import ObjectDoesNotExist

from .models import Patient, Message, Diagnosis, DoctorMessage, Scan, User, PasswordResetOTP
from .serializers import PatientSerializer, MessageSerializer, DiagnosisSerializer, DoctorMessageSerializer, ScanSerializer, UserSerializer
from .sms_utils import (
    send_sms_notification,
    notify_patient_new_message,
    notify_patient_diagnosis_update,
    notify_doctor_new_patient_message,
)

from django.shortcuts import get_object_or_404
import random
import json
import os
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.utils import ImageReader
from django.http import HttpResponse
from django.core.mail import send_mail
from django.conf import settings
import re
from io import BytesIO
from django.core.files.base import ContentFile
from django.utils import timezone
from datetime import timedelta
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
import logging
import threading

logger = logging.getLogger(__name__)


def _run_in_background(task, *args, **kwargs):
    """Run non-critical side effects without delaying API responses."""
    worker = threading.Thread(target=task, args=args, kwargs=kwargs, daemon=True)
    worker.start()


def _clean_clue_summary_text(summary_text):
    """Normalize clue summary wording for patient-facing messages."""
    text = (summary_text or '').strip()
    if not text:
        return ''

    text = re.sub(r'\bAI\b\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\(?\s*\d+(?:\.\d+)?\s*%\s*confidence\s*\)?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text)

    cleaned_lines = []
    for line in text.splitlines():
        compact = re.sub(r'\s{2,}', ' ', line).strip()
        compact = re.sub(r'\s+([,.;:])', r'\1', compact)
        if compact:
            cleaned_lines.append(compact)
    return '\n'.join(cleaned_lines)

class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get('unique_id') or request.data.get('username') or '').strip().upper()
        password = request.data.get('password') or ''
        if not username or not password:
            return Response({'error': 'unique_id and password are required'}, status=status.HTTP_400_BAD_REQUEST)
        user = User.objects.filter(username=username).order_by('-id').first()
        if user and user.check_password(password):
            token, created = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user': UserSerializer(user).data
            })
        return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)


class SignupView(APIView):
    permission_classes = [AllowAny]

    @staticmethod
    def _is_valid_unique_id(unique_id, role):
        if role == 'doctor':
            return bool(re.fullmatch(r'DOC-[0-9]{4,}', unique_id))
        if role == 'admin':
            return bool(re.fullmatch(r'ADM-[0-9]{4,}', unique_id))
        return False

    @staticmethod
    def _validate_password_rules(password):
        if len(password) < 8:
            return 'Password must be at least 8 characters long.'
        if not re.search(r'[A-Z]', password):
            return 'Password must include at least one uppercase letter.'
        if not re.search(r'[a-z]', password):
            return 'Password must include at least one lowercase letter.'
        if not re.search(r'[0-9]', password):
            return 'Password must include at least one number.'
        if not re.search(r'[^A-Za-z0-9]', password):
            return 'Password must include at least one special character.'
        return None

    @staticmethod
    def _split_full_name(full_name):
        normalized = re.sub(r'\s+', ' ', (full_name or '')).strip()
        if not normalized:
            return '', ''
        parts = normalized.split(' ', 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ''
        return first_name, last_name

    def post(self, request):
        unique_id = (request.data.get('unique_id') or '').strip().upper()
        password = request.data.get('password') or ''
        email = (request.data.get('email') or '').strip()
        full_name = re.sub(r'\s+', ' ', (request.data.get('full_name') or '')).strip()
        role = (request.data.get('role') or '').strip()

        if not unique_id or not password or not full_name or role not in ['doctor', 'admin']:
            return Response(
                {'error': 'unique_id, full_name, password, and valid role (doctor/admin) are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not re.fullmatch(r'[A-Za-z\s]+', full_name):
            return Response({'error': 'full_name can only contain English letters and spaces.'}, status=status.HTTP_400_BAD_REQUEST)

        if email:
            try:
                validate_email(email)
            except ValidationError:
                return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)
        elif role == 'doctor':
            # Allow doctor sign-up without manual email entry.
            email = f"{unique_id.lower()}@doctor.local"
        elif role == 'admin':
            # Allow admin sign-up without manual email entry.
            email = f"{unique_id.lower()}@admin.local"

        if not self._is_valid_unique_id(unique_id, role):
            if role == 'doctor':
                return Response({'error': 'Doctor unique ID must match DOC-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Admin unique ID must match ADM-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)

        password_error = self._validate_password_rules(password)
        if password_error:
            return Response({'error': password_error}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(username=unique_id).exists():
            return Response({'error': 'Unique ID already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        if email and User.objects.filter(email__iexact=email).exists():
            return Response({'error': 'Email already in use.'}, status=status.HTTP_400_BAD_REQUEST)

        first_name, last_name = self._split_full_name(full_name)

        user = User.objects.create_user(
            username=unique_id,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            role=role
        )
        token, _ = Token.objects.get_or_create(user=user)

        return Response(
            {
                'message': 'Account created successfully.',
                'token': token.key,
                'user': UserSerializer(user).data
            },
            status=status.HTTP_201_CREATED
        )


class PatientSignupView(APIView):
    permission_classes = [AllowAny]

    @staticmethod
    def _normalize_full_name(value):
        return re.sub(r'\s+', ' ', (value or '')).strip()

    @staticmethod
    def _generate_patient_username():
        while True:
            candidate = f"PAT-{random.randint(0, 999999):06d}"
            if not User.objects.filter(username=candidate).exists():
                return candidate

    @staticmethod
    def _validate_password_rules(password):
        if len(password) < 8:
            return 'Password must be at least 8 characters long.'
        if not re.search(r'[A-Z]', password):
            return 'Password must include at least one uppercase letter.'
        if not re.search(r'[a-z]', password):
            return 'Password must include at least one lowercase letter.'
        if not re.search(r'[0-9]', password):
            return 'Password must include at least one number.'
        if not re.search(r'[^A-Za-z0-9]', password):
            return 'Password must include at least one special character.'
        return None

    def post(self, request):
        full_name = self._normalize_full_name(request.data.get('full_name', ''))
        email = (request.data.get('email') or '').strip().lower()
        phone_number = (request.data.get('phone_number') or '').strip()
        password = request.data.get('password') or ''

        if not full_name or not email or not password:
            return Response({'error': 'full_name, email, and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.fullmatch(r'[A-Za-z\s]+', full_name):
            return Response({'error': 'full_name can only contain English letters and spaces.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        password_error = self._validate_password_rules(password)
        if password_error:
            return Response({'error': password_error}, status=status.HTTP_400_BAD_REQUEST)

        existing_user = User.objects.filter(email__iexact=email).order_by('-id').first()
        if existing_user and existing_user.role != 'patient':
            return Response({'error': 'Email already in use.'}, status=status.HTTP_400_BAD_REQUEST)

        # Reuse stale/orphan patient users when available to avoid false
        # "email already in use" blocks after partial cleanup or interrupted flows.
        user = existing_user if existing_user and existing_user.role == 'patient' else None
        if user:
            linked_patient_exists = Patient.objects.filter(user=user).exists()
            sent_message_exists = DoctorMessage.objects.filter(sender=user).exists()
            if linked_patient_exists or sent_message_exists:
                return Response({'error': 'Email already in use. Please log in instead.'}, status=status.HTTP_400_BAD_REQUEST)

            user.username = self._generate_patient_username()
            user.email = email
            user.role = 'patient'
            user.set_password(password)
            user.save(update_fields=['username', 'email', 'role', 'password'])
        else:
            username = self._generate_patient_username()
            user = User(
                username=username,
                email=email,
                role='patient'
            )
            # Store password securely using Django's built-in password hasher.
            user.set_password(password)
            user.save()

        # Reuse existing intake profile by email when available so historical
        # doctor/admin messages remain visible right after patient signup.
        patient_profile = (
            Patient.objects
            .filter(email__iexact=email)
            .order_by('-created_at')
            .first()
        )

        if patient_profile:
            patient_profile.user = user
            patient_profile.full_name = full_name or patient_profile.full_name
            patient_profile.phone_number = phone_number or patient_profile.phone_number
            patient_profile.email = email
            patient_profile.save(update_fields=['user', 'full_name', 'phone_number', 'email'])
        else:
            patient_profile = Patient.objects.create(
                user=user,
                full_name=full_name,
                email=email,
                phone_number=phone_number,
                queue_number='',
                area_of_concern=''
            )
        token, _ = Token.objects.get_or_create(user=user)

        return Response(
            {
                'message': 'Patient account created successfully.',
                'token': token.key,
                'user': UserSerializer(user).data,
                'patient': {
                    'id': patient_profile.id,
                    'full_name': patient_profile.full_name,
                    'queue_number': patient_profile.queue_number,
                }
            },
            status=status.HTTP_201_CREATED
        )


class PatientLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        password = request.data.get('password') or ''

        if not email or not password:
            return Response({'error': 'email and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Choose newest patient account for this email to stay consistent
        # with signup behavior and avoid stale-account password mismatches.
        user = User.objects.filter(email__iexact=email, role='patient').order_by('-id').first()
        if not user:
            return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)

        authenticated_user = authenticate(username=user.username, password=password)
        if not authenticated_user:
            return Response({'error': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)

        token, _ = Token.objects.get_or_create(user=authenticated_user)
        patient_profile = Patient.objects.filter(user=authenticated_user).first()
        email_profile = (
            Patient.objects
            .filter(email__iexact=email)
            .order_by('-created_at')
            .first()
        )

        # Legacy compatibility: if the login account points to a different
        # patient row than the intake row (same email), relink to email row so
        # prior doctor/admin updates are visible.
        if email_profile and (not patient_profile or patient_profile.id != email_profile.id):
            if patient_profile and patient_profile.id != email_profile.id:
                patient_profile.user = None
                patient_profile.save(update_fields=['user'])

            if not email_profile.user or email_profile.user == authenticated_user:
                email_profile.user = authenticated_user
                email_profile.save(update_fields=['user'])
                patient_profile = email_profile

        if not patient_profile:
            patient_profile = email_profile

        return Response(
            {
                'token': token.key,
                'user': UserSerializer(authenticated_user).data,
                'patient': {
                    'id': patient_profile.id,
                    'full_name': patient_profile.full_name,
                    'queue_number': patient_profile.queue_number,
                } if patient_profile else None
            }
        )


class PatientRequestPasswordResetOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        if not email:
            return Response({'error': 'email is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email, role='patient').order_by('-id').first()
        if not user:
            return Response({'error': 'No patient account found for this email.'}, status=status.HTTP_404_NOT_FOUND)

        PasswordResetOTP.objects.filter(user=user, used_at__isnull=True, expires_at__gt=timezone.now()).update(
            expires_at=timezone.now()
        )

        otp_code = f"{random.randint(0, 999999):06d}"
        expires_at = timezone.now() + timedelta(minutes=10)
        PasswordResetOTP.objects.create(user=user, otp_code=otp_code, expires_at=expires_at)

        send_mail(
            'Your Patient Password Reset OTP',
            (
                f'Hello {user.username},\n\n'
                f'Your one-time password (OTP) is: {otp_code}\n'
                f'This OTP will expire at {expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")}.\n\n'
                'If you did not request this reset, you can ignore this email.'
            ),
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )

        return Response({'message': 'OTP sent to your email. It is valid for 10 minutes.'})


class PatientVerifyPasswordResetOTPView(APIView):
    permission_classes = [AllowAny]

    @staticmethod
    def _validate_password_rules(password):
        if len(password) < 8:
            return 'Password must be at least 8 characters long.'
        if not re.search(r'[A-Z]', password):
            return 'Password must include at least one uppercase letter.'
        if not re.search(r'[a-z]', password):
            return 'Password must include at least one lowercase letter.'
        if not re.search(r'[0-9]', password):
            return 'Password must include at least one number.'
        if not re.search(r'[^A-Za-z0-9]', password):
            return 'Password must include at least one special character.'
        return None

    def post(self, request):
        email = (request.data.get('email') or '').strip().lower()
        otp_code = (request.data.get('otp_code') or '').strip()
        new_password = request.data.get('new_password') or ''

        if not email or not otp_code or not new_password:
            return Response(
                {'error': 'email, otp_code, and new_password are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        password_error = self._validate_password_rules(new_password)
        if password_error:
            return Response({'error': password_error}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email, role='patient').order_by('-id').first()
        if not user:
            return Response({'error': 'No patient account found for this email.'}, status=status.HTTP_404_NOT_FOUND)

        otp_record = PasswordResetOTP.objects.filter(
            user=user,
            otp_code=otp_code,
            used_at__isnull=True,
            expires_at__gt=timezone.now()
        ).order_by('-created_at').first()

        if not otp_record:
            return Response({'error': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save(update_fields=['password'])
        otp_record.used_at = timezone.now()
        otp_record.save(update_fields=['used_at'])
        Token.objects.filter(user=user).delete()

        return Response({'message': 'Password reset successful. Please log in with your new password.'})


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    @staticmethod
    def _is_valid_unique_id(unique_id, role):
        if role == 'doctor':
            return bool(re.fullmatch(r'DOC-[0-9]{4,}', unique_id))
        if role == 'admin':
            return bool(re.fullmatch(r'ADM-[0-9]{4,}', unique_id))
        return False

    @staticmethod
    def _validate_password_rules(password):
        if len(password) < 8:
            return 'Password must be at least 8 characters long.'
        if not re.search(r'[A-Z]', password):
            return 'Password must include at least one uppercase letter.'
        if not re.search(r'[a-z]', password):
            return 'Password must include at least one lowercase letter.'
        if not re.search(r'[0-9]', password):
            return 'Password must include at least one number.'
        if not re.search(r'[^A-Za-z0-9]', password):
            return 'Password must include at least one special character.'
        return None

    def post(self, request):
        unique_id = (request.data.get('unique_id') or '').strip().upper()
        email = (request.data.get('email') or '').strip()
        new_password = request.data.get('new_password') or ''
        role = (request.data.get('role') or '').strip()

        if not unique_id or not email or not new_password or role not in ['doctor', 'admin']:
            return Response(
                {'error': 'unique_id, email, new_password, and valid role (doctor/admin) are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not self._is_valid_unique_id(unique_id, role):
            if role == 'doctor':
                return Response({'error': 'Doctor unique ID must match DOC-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Admin unique ID must match ADM-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        password_error = self._validate_password_rules(new_password)
        if password_error:
            return Response({'error': password_error}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(username=unique_id, role=role).first()
        if not user:
            return Response({'error': 'Account not found for the provided role and unique ID.'}, status=status.HTTP_404_NOT_FOUND)

        if (user.email or '').strip().lower() != email.lower():
            return Response({'error': 'Email does not match this account.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save(update_fields=['password'])
        Token.objects.filter(user=user).delete()

        return Response({'message': 'Password reset successful. Please log in with your new password.'})


class RequestPasswordResetOTPView(APIView):
    permission_classes = [AllowAny]

    @staticmethod
    def _is_valid_unique_id(unique_id, role):
        if role == 'doctor':
            return bool(re.fullmatch(r'DOC-[0-9]{4,}', unique_id))
        if role == 'admin':
            return bool(re.fullmatch(r'ADM-[0-9]{4,}', unique_id))
        return False

    def post(self, request):
        unique_id = (request.data.get('unique_id') or '').strip().upper()
        email = (request.data.get('email') or '').strip()
        role = (request.data.get('role') or '').strip()

        if not unique_id or not email or role not in ['doctor', 'admin']:
            return Response(
                {'error': 'unique_id, email, and valid role (doctor/admin) are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not self._is_valid_unique_id(unique_id, role):
            if role == 'doctor':
                return Response({'error': 'Doctor unique ID must match DOC-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Admin unique ID must match ADM-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(username=unique_id, role=role).first()
        if not user:
            return Response({'error': 'Account not found for the provided role and unique ID.'}, status=status.HTTP_404_NOT_FOUND)

        if (user.email or '').strip().lower() != email.lower():
            return Response({'error': 'Email does not match this account.'}, status=status.HTTP_400_BAD_REQUEST)

        PasswordResetOTP.objects.filter(user=user, used_at__isnull=True, expires_at__gt=timezone.now()).update(
            expires_at=timezone.now()
        )

        otp_code = f"{random.randint(0, 999999):06d}"
        expires_at = timezone.now() + timedelta(minutes=10)
        PasswordResetOTP.objects.create(user=user, otp_code=otp_code, expires_at=expires_at)

        send_mail(
            'Your Password Reset OTP',
            (
                f'Hello {user.username},\n\n'
                f'Your one-time password (OTP) is: {otp_code}\n'
                f'This OTP will expire at {expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")}.\n\n'
                'If you did not request this reset, you can ignore this email.'
            ),
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )

        return Response({'message': 'OTP sent to your email. It is valid for 10 minutes.'})


class VerifyPasswordResetOTPView(APIView):
    permission_classes = [AllowAny]

    @staticmethod
    def _is_valid_unique_id(unique_id, role):
        if role == 'doctor':
            return bool(re.fullmatch(r'DOC-[0-9]{4,}', unique_id))
        if role == 'admin':
            return bool(re.fullmatch(r'ADM-[0-9]{4,}', unique_id))
        return False

    @staticmethod
    def _validate_password_rules(password):
        if len(password) < 8:
            return 'Password must be at least 8 characters long.'
        if not re.search(r'[A-Z]', password):
            return 'Password must include at least one uppercase letter.'
        if not re.search(r'[a-z]', password):
            return 'Password must include at least one lowercase letter.'
        if not re.search(r'[0-9]', password):
            return 'Password must include at least one number.'
        if not re.search(r'[^A-Za-z0-9]', password):
            return 'Password must include at least one special character.'
        return None

    def post(self, request):
        unique_id = (request.data.get('unique_id') or '').strip().upper()
        email = (request.data.get('email') or '').strip()
        role = (request.data.get('role') or '').strip()
        otp_code = (request.data.get('otp_code') or '').strip()
        new_password = request.data.get('new_password') or ''

        if not unique_id or not email or not otp_code or not new_password or role not in ['doctor', 'admin']:
            return Response(
                {'error': 'unique_id, email, otp_code, new_password, and valid role (doctor/admin) are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not self._is_valid_unique_id(unique_id, role):
            if role == 'doctor':
                return Response({'error': 'Doctor unique ID must match DOC-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)
            return Response({'error': 'Admin unique ID must match ADM-1234 format.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_email(email)
        except ValidationError:
            return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        password_error = self._validate_password_rules(new_password)
        if password_error:
            return Response({'error': password_error}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(username=unique_id, role=role).first()
        if not user:
            return Response({'error': 'Account not found for the provided role and unique ID.'}, status=status.HTTP_404_NOT_FOUND)

        if (user.email or '').strip().lower() != email.lower():
            return Response({'error': 'Email does not match this account.'}, status=status.HTTP_400_BAD_REQUEST)

        otp_record = PasswordResetOTP.objects.filter(
            user=user,
            otp_code=otp_code,
            used_at__isnull=True,
            expires_at__gt=timezone.now()
        ).order_by('-created_at').first()

        if not otp_record:
            return Response({'error': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save(update_fields=['password'])
        otp_record.used_at = timezone.now()
        otp_record.save(update_fields=['used_at'])
        Token.objects.filter(user=user).delete()

        return Response({'message': 'Password reset successful. Please log in with your new password.'})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.auth_token.delete()
        return Response({'message': 'Logged out successfully'})


class TokenVerifyView(APIView):
    """Lightweight endpoint used by the frontend to validate stored tokens."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'valid': True})


class CheckPatientMessagesView(APIView):
    """
    Check for unread messages without login using phone number or email.
    Query params: phone or email
    """
    permission_classes = [AllowAny]

    def get(self, request):
        phone = request.query_params.get('phone', '').strip()
        email = request.query_params.get('email', '').strip()

        if not phone and not email:
            return Response(
                {'error': 'Please provide either phone number or email'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Find patient by phone or email
            patient = None
            if phone:
                patient = Patient.objects.filter(phone_number=phone).first()
            elif email:
                patient = Patient.objects.filter(email=email).first()

            if not patient:
                return Response(
                    {'error': 'No patient found with provided phone or email'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Get doctor messages
            doctor_messages = DoctorMessage.objects.filter(patient=patient).order_by('-timestamp')[:10]

            # Get diagnosis/admin messages
            diagnoses = Diagnosis.objects.filter(patient=patient).order_by('-created_at')[:5]

            # Prepare response
            response_data = {
                'patient_name': patient.full_name,
                'patient_id': patient.id,
                'doctor_messages': DoctorMessageSerializer(doctor_messages, many=True).data,
                'admin_messages': DiagnosisSerializer(diagnoses, many=True).data,
                'doctor_message_count': doctor_messages.count(),
                'admin_message_count': diagnoses.count(),
                'has_messages': doctor_messages.count() > 0 or diagnoses.count() > 0
            }

            return Response(response_data)

        except Exception as e:
            logger.error(f'Error checking patient messages: {str(e)}')
            return Response(
                {'error': 'An error occurred while checking for messages'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class GeneratePDFView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, diagnosis_id):
        diagnosis = get_object_or_404(Diagnosis, id=diagnosis_id)
        if request.user.role != 'admin':
            return Response({'error': 'Only admins can generate PDFs'}, status=status.HTTP_403_FORBIDDEN)
        
        # Generate PDF
        response = HttpResponse(content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="diagnosis_{diagnosis.patient.full_name}.pdf"'
        
        doc = SimpleDocTemplate(response, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        
        story.append(Paragraph("Medical Diagnosis Report", styles['Title']))
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Patient: {diagnosis.patient.full_name}", styles['Normal']))
        story.append(Paragraph(f"Diagnosis Date: {diagnosis.created_at.strftime('%Y-%m-%d')}", styles['Normal']))
        story.append(Paragraph(f"Admin: {diagnosis.admin.username}", styles['Normal']))
        story.append(Spacer(1, 12))
        story.append(Paragraph("Diagnosis Notes:", styles['Heading2']))
        story.append(Paragraph(diagnosis.admin_notes, styles['Normal']))
        
        doc.build(story)
        diagnosis.pdf_generated = True
        diagnosis.save()
        return response


class SendEmailView(APIView):
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _normalize_name(value):
        return re.sub(r'\s+', ' ', (value or '')).strip().lower()

    @staticmethod
    def _normalize_phone(value):
        return re.sub(r'\s+', '', (value or ''))

    def _resolve_recipient_email(self, patient):
        """
        Pick the best recipient email across potentially duplicated patient rows.
        Priority:
        1) Any linked user email from this/related rows
        2) Latest non-empty patient.email from related rows
        3) Current patient email fallback
        """
        related_qs = Patient.objects.none()

        if getattr(patient, 'user_id', None):
            related_qs = Patient.objects.filter(user_id=patient.user_id).order_by('-created_at')
        else:
            normalized_name = self._normalize_name(patient.full_name)
            normalized_phone = self._normalize_phone(patient.phone_number)
            if normalized_name:
                related_qs = Patient.objects.filter(full_name__iexact=patient.full_name).order_by('-created_at')
                if normalized_phone:
                    related_qs = related_qs.filter(phone_number__icontains=normalized_phone)

        for p in related_qs:
            if getattr(p, 'user', None) and (p.user.email or '').strip():
                return p.user.email.strip(), p

        for p in related_qs:
            if (p.email or '').strip():
                return p.email.strip(), p

        if getattr(patient, 'user', None) and (patient.user.email or '').strip():
            return patient.user.email.strip(), patient

        return (patient.email or '').strip(), patient

    def post(self, request, diagnosis_id):
        diagnosis = get_object_or_404(Diagnosis, id=diagnosis_id)
        if request.user.role != 'admin':
            return Response({'error': 'Only admins can send emails'}, status=status.HTTP_403_FORBIDDEN)

        patient = diagnosis.patient
        recipient_email, source_patient = self._resolve_recipient_email(patient)

        if not recipient_email:
            logger.warning('SendEmailView skipped for diagnosis %s: no patient/user email', diagnosis_id)
            return Response({'error': 'Patient email is not available for this diagnosis.'}, status=status.HTTP_400_BAD_REQUEST)

        # Keep diagnosis patient row in sync for future sends and UI consistency.
        if (patient.email or '').strip().lower() != recipient_email.lower():
            patient.email = recipient_email
            patient.save(update_fields=['email'])

        admin_name = request.user.first_name or request.user.username
        summary_text = _clean_clue_summary_text(diagnosis.admin_notes)
        message_text = (
            f"Your diagnosis clue summary from {admin_name} is ready.\n\n"
            f"Clue Summary:\n{summary_text or 'Your doctor has prepared your diagnosis summary.'}\n\n"
            f"Log in to your medical chat to view your full results."
        )

        logger.info('Attempting diagnosis email for diagnosis %s to %s', diagnosis_id, recipient_email)
        sent = notify_patient_diagnosis_update(patient, message_text)
        if not sent:
            logger.error('Failed diagnosis email send for diagnosis %s to %s', diagnosis_id, recipient_email)
            return Response(
                {
                    'message': 'Diagnosis saved, but email could not be delivered right now.',
                    'email_sent': False,
                    'recipient_email': recipient_email,
                    'recipient_patient_id': source_patient.id,
                },
                status=status.HTTP_200_OK,
            )

        logger.info('Diagnosis email sent for diagnosis %s to %s', diagnosis_id, recipient_email)
        return Response({
            'message': 'Patient email notification sent successfully',
            'email_sent': True,
            'recipient_email': recipient_email,
            'recipient_patient_id': source_patient.id,
        })


class GenerateSummaryPDFView(APIView):
    permission_classes = [IsAuthenticated]

    IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.heif')

    @staticmethod
    def _sender_label(sender):
        sender_value = (sender or '').strip().lower()
        if sender_value in ['user', 'patient']:
            return 'Patient'
        if sender_value in ['bot', 'doctor']:
            return 'Assistant'
        return sender_value.capitalize() if sender_value else 'Unknown'

    @staticmethod
    def _patient_qa_pairs(patient):
        area_mapping = {
            'Head': 'Head.json',
            'Breast': 'Breasts.json',
            'Breasts': 'Breasts.json',
            'Pelvis': 'Pelvic.json',
            'Urinary System': 'UrinarySystem.json',
            'Skin': 'Skin.json',
            'Hormonal': 'Hormone.json'
        }

        dataset_filename = area_mapping.get(patient.area_of_concern)
        if not dataset_filename:
            return []

        dataset_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'Datasets',
            dataset_filename
        )

        try:
            with open(dataset_path, 'r', encoding='utf-8') as f:
                dataset = json.load(f)
        except Exception:
            return []

        intents = dataset.get('ourIntents', [])
        answer_messages = list(
            Message.objects.filter(patient=patient, sender__in=['user', 'patient']).order_by('timestamp')
        )

        qa_pairs = []
        for idx, intent in enumerate(intents):
            question = intent.get('patterns', [f'Question {idx + 1}'])[0]
            answer_obj = answer_messages[idx] if idx < len(answer_messages) else None
            qa_pairs.append(
                {
                    'index': idx + 1,
                    'question': question,
                    'answer': answer_obj.text if answer_obj else 'Not answered yet'
                }
            )
        return qa_pairs

    @staticmethod
    def _scan_based_conclusion(scans):
        if not scans:
            return (
                'No uploaded scans/documents were available at the time of report generation. '
                'Clinical conclusion should rely on Q/A findings and direct medical examination.'
            )

        total = len(scans)
        names = [((scan.file.name or '').split('/')[-1]).lower() for scan in scans if getattr(scan, 'file', None)]
        image_count = sum(1 for name in names if name.endswith(GenerateSummaryPDFView.IMAGE_EXTENSIONS))
        doc_count = total - image_count

        keyword_map = {
            'xray': 'X-ray related upload(s)',
            'x-ray': 'X-ray related upload(s)',
            'ct': 'CT related upload(s)',
            'mri': 'MRI related upload(s)',
            'ultrasound': 'Ultrasound related upload(s)',
            'echo': 'Echography/echo related upload(s)',
            'report': 'Clinical report document(s)',
            'lab': 'Lab-related document(s)',
        }

        detected_tags = []
        for key, label in keyword_map.items():
            if any(key in name for name in names):
                detected_tags.append(label)

        tag_text = ', '.join(detected_tags) if detected_tags else 'No modality keywords detected in file names'

        return (
            f'Uploaded file review: {total} file(s) detected ({image_count} image file(s), {doc_count} document file(s)). '
            f'Identified categories: {tag_text}. '
            'These uploaded images/documents should be clinically correlated with symptom Q/A and direct examination '
            'before final diagnosis confirmation.'
        )


    def post(self, request, patient_id):
        if request.user.role != 'admin':
            return Response({'error': 'Only admins can generate summary PDFs'}, status=status.HTTP_403_FORBIDDEN)

        patient = get_object_or_404(Patient, id=patient_id)
        qa_pairs = self._patient_qa_pairs(patient)
        if not qa_pairs:
            return Response({'error': 'No patient questions/answers found to summarize.'}, status=status.HTTP_400_BAD_REQUEST)

        patient_scans = list(Scan.objects.filter(patient=patient).order_by('uploaded_at'))
        scan_conclusion = self._scan_based_conclusion(patient_scans)

        diagnosis = Diagnosis.objects.filter(patient=patient, admin=request.user).order_by('-created_at').first()
        if not diagnosis:
            diagnosis = Diagnosis.objects.create(
                patient=patient,
                admin=request.user,
                admin_notes='Auto-generated Q/A summary report.'
            )

        buffer = BytesIO()
        try:
            pdf = canvas.Canvas(buffer, pagesize=letter)
            width, height = letter
            y = height - 50

            def add_line(text, font='Helvetica', size=11, gap=16, max_chars=115):
                nonlocal y
                safe = (text or '').replace('\t', ' ').replace('\r', ' ')
                for raw_line in safe.split('\n'):
                    words = raw_line.strip().split()
                    if not words:
                        continue
                    current = ''
                    for word in words:
                        candidate = f"{current} {word}".strip()
                        if len(candidate) > max_chars and current:
                            if y < 60:
                                pdf.showPage()
                                y = height - 50
                            pdf.setFont(font, size)
                            pdf.drawString(50, y, current)
                            y -= gap
                            current = word
                        else:
                            current = candidate
                    if current:
                        if y < 60:
                            pdf.showPage()
                            y = height - 50
                        pdf.setFont(font, size)
                        pdf.drawString(50, y, current)
                        y -= gap

            add_line('Patient Q/A Summary Report', font='Helvetica-Bold', size=14, gap=20)
            add_line(f'Patient: {patient.full_name}')
            add_line(f'Queue Number: {patient.queue_number or "N/A"}')
            add_line(f'Area of Concern: {patient.area_of_concern or "N/A"}')
            add_line(f'Generated At: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}')
            y -= 8
            add_line('Questions and Answers', font='Helvetica-Bold', size=12, gap=18)

            for pair in qa_pairs:
                add_line(f"Q{pair['index']}: {pair['question']}", font='Helvetica-Bold', size=10, gap=14)
                add_line(f"A{pair['index']}: {pair['answer']}", font='Helvetica', size=10, gap=16)
                y -= 4

            y -= 6
            add_line('Uploaded Medical Scans / Documents', font='Helvetica-Bold', size=12, gap=18)

            if not patient_scans:
                add_line('No uploaded files found for this patient.', size=10, gap=14)
            else:
                for idx, scan in enumerate(patient_scans, start=1):
                    file_name = (scan.file.name or '').split('/')[-1]
                    add_line(f'{idx}. {file_name}', size=10, gap=14)

                    lower_name = file_name.lower()
                    is_image = lower_name.endswith(self.IMAGE_EXTENSIONS)
                    if is_image:
                        try:
                            img_reader = ImageReader(scan.file.path)
                            iw, ih = img_reader.getSize()
                            max_width = width - 100
                            max_height = 220
                            scale = min(max_width / max(iw, 1), max_height / max(ih, 1), 1.0)
                            draw_w = iw * scale
                            draw_h = ih * scale

                            if y - draw_h < 60:
                                pdf.showPage()
                                y = height - 50

                            pdf.drawImage(
                                img_reader,
                                50,
                                y - draw_h,
                                width=draw_w,
                                height=draw_h,
                                preserveAspectRatio=True,
                                mask='auto'
                            )
                            y -= draw_h + 14
                        except Exception:
                            add_line('   [Image preview unavailable in PDF, file attached on server]', size=9, gap=12)
                    else:
                        add_line('   [Document file - open from patient uploads for full content]', size=9, gap=12)

            y -= 4
            add_line('Preliminary Scan-Based Conclusion', font='Helvetica-Bold', size=12, gap=18)
            add_line(scan_conclusion, size=10, gap=14)

            pdf.save()

            filename = f'summary_patient_{patient.id}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.pdf'
            try:
                diagnosis.summary_pdf.save(filename, ContentFile(buffer.getvalue()), save=False)
            except Exception:
                logger.exception('Failed to save summary PDF to diagnosis.summary_pdf')
                # continue: we'll still attempt to persist metadata

            # Keep diagnosis notes aligned with the generated scan-based summary when notes are still auto-generated.
            if not (diagnosis.admin_notes or '').strip() or diagnosis.admin_notes.startswith('Auto-generated'):
                diagnosis.admin_notes = scan_conclusion

            diagnosis.pdf_generated = True
            diagnosis.sent_to_doctor = False
            diagnosis.save()

            return Response(
                {
                    'message': 'Summary PDF generated successfully.',
                    'diagnosis_id': diagnosis.id,
                    'summary_pdf': diagnosis.summary_pdf.url if diagnosis.summary_pdf else None
                },
                status=status.HTTP_201_CREATED
            )
        except Exception as exc:
            logger.exception('Error generating summary PDF for patient %s: %s', patient_id, exc)
            return Response({'error': 'Internal error generating summary PDF. See server logs.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        finally:
            try:
                buffer.close()
            except Exception:
                pass


class SendSummaryToDoctorView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, diagnosis_id):
        if request.user.role != 'admin':
            return Response({'error': 'Only admins can send summary PDFs to doctors'}, status=status.HTTP_403_FORBIDDEN)

        diagnosis = get_object_or_404(Diagnosis, id=diagnosis_id)
        if not diagnosis.summary_pdf:
            return Response({'error': 'Generate the summary PDF before sending it to doctors.'}, status=status.HTTP_400_BAD_REQUEST)

        diagnosis.sent_to_doctor = True
        diagnosis.save(update_fields=['sent_to_doctor'])

        return Response({'message': 'Summary PDF sent to doctor section successfully.'})


# simple view to return welcome message for Home.jsx
class HomeView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"message": "Welcome to the MediBridge backend!"})


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all().order_by('-created_at')
    serializer_class = PatientSerializer
    permission_classes = [AllowAny]
    VISIT_INCREMENT_FIELDS = {
        'full_name',
        'email',
        'phone_number',
        'age',
        'weight',
        'height',
        'queue_number',
    }

    @staticmethod
    def _normalize_full_name(value):
        return re.sub(r'\s+', ' ', (value or '')).strip()

    @staticmethod
    def _normalized_identity_key(patient):
        name = re.sub(r'\s+', ' ', (patient.full_name or '')).strip().lower()
        email = (patient.email or '').strip().lower()
        phone = re.sub(r'\s+', '', (patient.phone_number or ''))
        contact = email or phone
        # Prefer contact-aware grouping when available, fallback to name.
        return f'{name}::{contact}' if contact else name

    @staticmethod
    def _record_priority(patient):
        has_queue = 1 if (patient.queue_number or '').strip() else 0
        has_area = 1 if (patient.area_of_concern or '').strip() else 0
        has_user = 1 if getattr(patient, 'user_id', None) else 0
        visit_count = int(getattr(patient, 'visit_count', 1) or 1)
        # prioritize queue presence first, then area/user linkage.
        return (has_queue, has_area, has_user, visit_count)

    def _should_increment_visit_count(self, incoming_data):
        return any(field in incoming_data for field in self.VISIT_INCREMENT_FIELDS)

    def get_queryset(self):
        return Patient.objects.all().order_by('-created_at')

    def create(self, request, *args, **kwargs):
        full_name = self._normalize_full_name(request.data.get('full_name', ''))
        if not full_name:
            return Response({'error': 'full_name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not re.fullmatch(r'[A-Za-z\s]+', full_name):
            return Response({'error': 'full_name can only contain English letters and spaces.'}, status=status.HTTP_400_BAD_REQUEST)

        mutable_data = request.data.copy()
        mutable_data['full_name'] = full_name
        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()

        mutable_data = request.data.copy()
        if 'full_name' in mutable_data:
            full_name = self._normalize_full_name(mutable_data.get('full_name', ''))
            if not full_name:
                return Response({'error': 'full_name is required.'}, status=status.HTTP_400_BAD_REQUEST)
            if not re.fullmatch(r'[A-Za-z\s]+', full_name):
                return Response({'error': 'full_name can only contain English letters and spaces.'}, status=status.HTTP_400_BAD_REQUEST)
            mutable_data['full_name'] = full_name

        serializer = self.get_serializer(instance, data=mutable_data, partial=partial)
        serializer.is_valid(raise_exception=True)

        # Do NOT auto-increment `visit_count` on simple profile updates.
        # Visit numbering is derived from completed diagnostic sessions
        # or patient-initiated doctor messages. Persist incoming fields
        # as-is and manage `visit_count` elsewhere (chat/doctor message handlers).
        serializer.save()

        return Response(serializer.data)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        # Dedupe for doctor/admin dashboards where intake + linked patient rows can overlap.
        # Keep the best record for each identity key while preserving deterministic ordering.
        if request.user.is_authenticated and request.user.role in ['doctor', 'admin']:
            selected_by_key = {}
            selected_order = []

            for patient in queryset:
                key = self._normalized_identity_key(patient)
                if key not in selected_by_key:
                    selected_by_key[key] = patient
                    selected_order.append(key)
                    continue

                existing = selected_by_key[key]
                if self._record_priority(patient) > self._record_priority(existing):
                    selected_by_key[key] = patient

            deduped = [selected_by_key[k] for k in selected_order]
            serializer = self.get_serializer(deduped, many=True)
            return Response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class MessageViewSet(viewsets.ModelViewSet):
    queryset = Message.objects.all().order_by('timestamp')
    serializer_class = MessageSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = Message.objects.all().order_by('timestamp')
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            qs = qs.filter(patient__id=patient_id)
        return qs


class DiagnosisViewSet(viewsets.ModelViewSet):
    queryset = Diagnosis.objects.all().order_by('-created_at')
    serializer_class = DiagnosisSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = Diagnosis.objects.all().order_by('-created_at')
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            qs = qs.filter(patient__id=patient_id)
        return qs

    def perform_create(self, serializer):
        diagnosis = serializer.save(admin=self.request.user)

        # Ensure visit_count is at least 1 when diagnosis is created
        try:
            patient = diagnosis.patient
            if (getattr(patient, 'visit_count', 0) or 0) < 1:
                patient.visit_count = 1
                patient.save(update_fields=['visit_count'])
        except Exception:
            logger.exception('Failed to set visit_count=1 on diagnosis creation')

        # Send email notification to patient about diagnosis update without
        # blocking the main request path.
        admin_name = self.request.user.first_name or self.request.user.username

        def _notify_create():
            try:
                notify_patient_diagnosis_update(
                    patient,
                    f"Your diagnosis summary from {admin_name} is ready. Log in to your medical chat to view your results."
                )
            except Exception as e:
                logger.warning(f"Failed to send diagnosis email notification: {str(e)}")

        _run_in_background(_notify_create)

    def perform_update(self, serializer):
        diagnosis = serializer.save()

        # Also notify patient when an existing conclusion is edited by admin,
        # without blocking the API response.
        editor = self.request.user if self.request.user.is_authenticated else None
        editor_name = (editor.first_name or editor.username) if editor else 'admin'

        def _notify_update():
            try:
                notify_patient_diagnosis_update(
                    diagnosis.patient,
                    f"Your diagnosis summary from {editor_name} has been updated. Log in to your medical chat to view the latest results."
                )
            except Exception as e:
                logger.warning(f"Failed to send diagnosis update email notification: {str(e)}")

        _run_in_background(_notify_update)


class DoctorMessageViewSet(viewsets.ModelViewSet):
    queryset = DoctorMessage.objects.all().order_by('timestamp')
    serializer_class = DoctorMessageSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = DoctorMessage.objects.all().order_by('timestamp')
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            qs = qs.filter(patient__id=patient_id)
        return qs

    def perform_create(self, serializer):
        patient = serializer.validated_data.get('patient')
        sender_user = self.request.user if self.request.user.is_authenticated else None

        if sender_user is None and patient and patient.user:
            sender_user = patient.user

        # If the request is anonymous (first-time patient path), still persist
        # the message as patient-origin by attaching/creating a patient user.
        if sender_user is None and patient:
            fallback_username = f"patient_{patient.id}"
            sender_user, _ = User.objects.get_or_create(
                username=fallback_username,
                defaults={
                    'role': 'patient',
                    'email': patient.email or '',
                },
            )
            if sender_user.role != 'patient':
                sender_user.role = 'patient'
                sender_user.save(update_fields=['role'])
            if not patient.user_id:
                patient.user = sender_user
                patient.save(update_fields=['user'])

        if sender_user is None:
            raise DRFValidationError('No valid sender user is available to create this message.')

        doctor_message = serializer.save(sender=sender_user)

        # Send email notification to the opposite side based on sender role.
        try:
            patient = doctor_message.patient
            sender_name = sender_user.first_name or sender_user.username
            message_preview = doctor_message.text[:100] if doctor_message.text else ""
            if sender_user.role == 'patient':
                notify_doctor_new_patient_message(patient, sender_name, message_preview)
            else:
                notify_patient_new_message(patient, sender_name, message_preview)
        except Exception as e:
            logger.warning(f"Failed to send doctor message SMS notification: {str(e)}")
        
        # If the sender is the patient, treat this as a visit action. Use the
        # count of prior patient-originated doctor messages to determine visit
        # numbering so the first direct question is Visit 1.
        try:
            if sender_user.role == 'patient':
                prior_patient_msgs = DoctorMessage.objects.filter(patient=patient, sender__role='patient').exclude(id=doctor_message.id).count() or 0
                new_visit_num = prior_patient_msgs + 1
                if (getattr(patient, 'visit_count', 0) or 0) < new_visit_num:
                    patient.visit_count = new_visit_num
                    patient.save(update_fields=['visit_count'])
        except Exception:
            logger.exception('Failed to update patient visit_count on patient doctor-message')


class ScanViewSet(viewsets.ModelViewSet):
    queryset = Scan.objects.all().order_by('-uploaded_at')
    serializer_class = ScanSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        mutable_data = request.data.copy()
        if request.user.is_authenticated:
            mutable_data['uploaded_by'] = request.user.id
            mutable_data['uploaded_by_role'] = request.user.role
        else:
            # Public upload path is used by patients before/without login.
            mutable_data['uploaded_by_role'] = 'patient'

        serializer = self.get_serializer(data=mutable_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    # allow filtering by patient
    def get_queryset(self):
        qs = Scan.objects.all().order_by('-uploaded_at')
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            qs = qs.filter(patient__id=patient_id)
        return qs


# optional endpoint to generate bot response using existing logic
class ChatbotAPIView(APIView):
    """POST a JSON object {"message": "...", "patient_id": <id>} and receive a reply from the trained model."""
    
    permission_classes = [AllowAny]

    AREA_MAPPING = {
        'Head': 'Head.json',
        'Breast': 'Breasts.json',
        'Breasts': 'Breasts.json',
        'Pelvis': 'Pelvic.json',
        'Urinary System': 'UrinarySystem.json',
        'Skin': 'Skin.json',
        'Hormonal': 'Hormone.json'
    }

    def _build_completion_response(self):
        return {
            "reply": (
                "Thank you for completing all the diagnostic questions. "
                "A healthcare professional will review your responses."
            ),
            "has_next_question": False
        }

    def _validate_mandatory_answer(self, current_intent, user_text, current_question_index):
        allowed_responses = current_intent.get('responses', [])
        if allowed_responses and user_text not in allowed_responses:
            return {
                "error": "Please answer using one of the provided options.",
                "question_index": current_question_index,
                "expected_responses": allowed_responses,
                "current_question": current_intent.get('patterns', [f"Question {current_question_index + 1}"])[0]
            }
        return None

    def _load_patient_intents(self, area_of_concern):
        dataset_filename = self.AREA_MAPPING.get(area_of_concern)
        if not dataset_filename:
            raise ValueError(
                f"Unknown area of concern: {area_of_concern}. "
                f"Available areas: {', '.join(self.AREA_MAPPING.keys())}"
            )

        dataset_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'Datasets',
            dataset_filename
        )

        with open(dataset_path, 'r', encoding='utf-8') as f:
            dataset = json.load(f)

        intents = dataset.get('ourIntents', [])
        if not intents:
            raise ValueError(f"Dataset {dataset_filename} does not contain any intents.")

        return intents

    def post(self, request):
        text = request.data.get('message', '')
        text = text.strip() if isinstance(text, str) else ''
        patient_id = request.data.get('patient_id')
        raw_question_index = request.data.get('question_index')
        patient = None
        
        if not text:
            return Response({"error": "No message provided."}, status=status.HTTP_400_BAD_REQUEST)
        
        # patient_id is required for conversational flow
        if not patient_id:
            return Response({"error": "patient_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient = Patient.objects.get(id=patient_id)
        except Patient.DoesNotExist:
            return Response({"error": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            intents = self._load_patient_intents(patient.area_of_concern)

            conversation = Message.objects.filter(patient=patient).order_by('timestamp')
            user_messages = [msg for msg in conversation if msg.sender == 'user']
            current_question_index = len(user_messages)  # 0-based index of next unanswered question

            if raw_question_index in [None, '']:
                target_question_index = current_question_index
            else:
                try:
                    target_question_index = int(raw_question_index)
                except (TypeError, ValueError):
                    return Response({"error": "question_index must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

            if target_question_index < 0 or target_question_index >= len(intents):
                return Response({"error": "question_index is out of range."}, status=status.HTTP_400_BAD_REQUEST)

            editing_existing_answer = target_question_index < len(user_messages)

            # If all questions are already answered, only edits to existing answers are allowed.
            if current_question_index >= len(intents) and not editing_existing_answer:
                return Response(self._build_completion_response())

            current_intent = intents[target_question_index]

            # Mandatory answer validation: patient must answer every dataset question.
            validation_error = self._validate_mandatory_answer(
                current_intent=current_intent,
                user_text=text,
                current_question_index=target_question_index
            )
            if validation_error:
                return Response(validation_error, status=status.HTTP_400_BAD_REQUEST)

            if editing_existing_answer:
                existing_message = user_messages[target_question_index]
                existing_message.text = text
                existing_message.save(update_fields=['text'])
                
                # Even when editing, check if there's a next question
                next_index = target_question_index + 1
                has_next_question = next_index < len(intents)
                
                response_data = {
                    "reply": "Answer updated successfully.",
                    "edited": True,
                    "question_index": target_question_index,
                    "has_next_question": has_next_question
                }
                
                # Include next question if available
                if has_next_question:
                    next_intent = intents[next_index]
                    next_question = next_intent.get('patterns', [f"Question {next_index + 1}"])[0]
                    next_responses = next_intent.get('responses', [])
                    response_data.update({
                        "next_question": next_question,
                        "next_responses": next_responses,
                        "next_intent": next_intent.get('tag', ''),
                        "question_index": next_index
                    })
                
                return Response(response_data)

            # Save valid user response.
            # Ensure visit_count is at least 1 when first message is sent
            if (getattr(patient, 'visit_count', 0) or 0) < 1:
                patient.visit_count = 1
                patient.save(update_fields=['visit_count'])
            Message.objects.create(sender='user', text=text, patient=patient)

            next_index = current_question_index + 1
            has_next_question = next_index < len(intents)

            if has_next_question:
                reply_text = f"Thank you for your response: '{text}'"
                next_intent = intents[next_index]
                next_question = next_intent.get('patterns', [f"Question {next_index + 1}"])[0]
                next_responses = next_intent.get('responses', [])
            else:
                reply_text = self._build_completion_response()["reply"]
                Message.objects.create(sender='bot', text=reply_text, patient=patient)

                # Mark this as a completed visit. Count prior completion messages
                # Mark this as a completed visit. Count prior completion messages
                # and existing diagnoses so both bot-driven completions and
                # admin-created conclusions contribute to visit numbering.
                try:
                    from django.utils import timezone
                    from api.models import Diagnosis

                    completion_ts = timezone.now()

                    prior_bot_completions = Message.objects.filter(
                        patient=patient,
                        sender='bot',
                        text__contains='Thank you for completing all the diagnostic questions'
                    ).count() or 0

                    prior_diagnoses = Diagnosis.objects.filter(patient=patient).count() or 0

                    # Treat previous diagnoses as prior completed visits when inferring visit number.
                    new_visit_num = prior_bot_completions + prior_diagnoses + 1
                    if (getattr(patient, 'visit_count', 0) or 0) < new_visit_num:
                        patient.visit_count = new_visit_num
                        patient.save(update_fields=['visit_count'])

                    # Notify doctor if there's no up-to-date diagnosis for this completion.
                    latest_diag = Diagnosis.objects.filter(patient=patient).order_by('-created_at').first()
                    should_notify = False
                    if not latest_diag:
                        should_notify = True
                    else:
                        # Notify if latest diagnosis was not explicitly sent to doctor
                        # or if it was created before this completion (stale).
                        try:
                            if not latest_diag.sent_to_doctor:
                                should_notify = True
                            else:
                                if latest_diag.created_at and latest_diag.created_at < completion_ts:
                                    should_notify = True
                        except Exception:
                            should_notify = True

                    if should_notify:
                        from .sms_utils import notify_doctor_new_patient_message
                        notify_doctor_new_patient_message(
                            patient,
                            sender_name=patient.full_name or f"Patient {patient.id}",
                            message_preview=f"Conclusion Needed for Visit #{new_visit_num}"
                        )
                except Exception:
                    logger.exception('Failed to update patient visit_count or notify doctor on completion')
            
            # Prepare response data
            response_data = {
                "reply": reply_text,
                "has_next_question": has_next_question
            }
            
            # If there's a next question, include it
            if has_next_question:
                response_data.update({
                    "next_question": next_question,
                    "next_responses": next_responses,
                    "next_intent": next_intent.get('tag', ''),
                    "question_index": next_index
                })
            
            return Response(response_data)
            
        except ValueError as e:
            return Response(
                {"error": str(e)}, 
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except Exception as e:
            return Response(
                {"error": f"Chatbot error: {str(e)}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DatasetView(APIView):
    """Retrieve dataset for a specific body area."""

    permission_classes = [AllowAny]

    # Mapping of area names from frontend to dataset filenames
    AREA_MAPPING = {
        'Head': 'Head.json',
        'Breast': 'Breasts.json',
        'Breasts': 'Breasts.json',
        'Pelvis': 'Pelvic.json',
        'Urinary System': 'UrinarySystem.json',
        'Skin': 'Skin.json',
        'Hormonal': 'Hormone.json'
    }

    def get(self, request):
        area = request.query_params.get('area', '')
        
        if not area:
            return Response(
                {"error": "Area parameter is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get the dataset filename from mapping
        dataset_filename = self.AREA_MAPPING.get(area)
        
        if not dataset_filename:
            return Response(
                {"error": f"Unknown area: {area}. Available areas: {', '.join(self.AREA_MAPPING.keys())}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Construct the path to the dataset file
        dataset_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'Datasets',
            dataset_filename
        )

        try:
            with open(dataset_path, 'r', encoding='utf-8') as f:
                dataset = json.load(f)
            return Response(dataset)
        except FileNotFoundError:
            return Response(
                {"error": f"Dataset file not found: {dataset_filename}"},
                status=status.HTTP_404_NOT_FOUND
            )
        except json.JSONDecodeError:
            return Response(
                {"error": f"Invalid JSON in dataset file: {dataset_filename}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PatientQAView(APIView):
    """Return reconstructed diagnostic questions with corresponding patient answers."""

    permission_classes = [IsAuthenticated]

    AREA_MAPPING = {
        'Head': 'Head.json',
        'Breast': 'Breasts.json',
        'Breasts': 'Breasts.json',
        'Pelvis': 'Pelvic.json',
        'Urinary System': 'UrinarySystem.json',
        'Skin': 'Skin.json',
        'Hormonal': 'Hormone.json'
    }

    def get(self, request, patient_id):
        if request.user.role != 'admin':
            return Response({'error': 'Only admins can view patient Q/A details.'}, status=status.HTTP_403_FORBIDDEN)

        patient = get_object_or_404(Patient, id=patient_id)
        dataset_filename = self.AREA_MAPPING.get(patient.area_of_concern)
        if not dataset_filename:
            return Response({'error': f'Unknown area of concern: {patient.area_of_concern}'}, status=status.HTTP_400_BAD_REQUEST)

        dataset_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'Datasets',
            dataset_filename
        )

        try:
            with open(dataset_path, 'r', encoding='utf-8') as f:
                dataset = json.load(f)
        except FileNotFoundError:
            return Response({'error': f'Dataset file not found: {dataset_filename}'}, status=status.HTTP_404_NOT_FOUND)
        except json.JSONDecodeError:
            return Response({'error': f'Invalid JSON in dataset file: {dataset_filename}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        intents = dataset.get('ourIntents', [])
        answer_messages = list(
            Message.objects.filter(patient=patient, sender__in=['user', 'patient']).order_by('timestamp')
        )

        qa_pairs = []
        for idx, intent in enumerate(intents):
            question = intent.get('patterns', [f'Question {idx + 1}'])[0]
            answer_obj = answer_messages[idx] if idx < len(answer_messages) else None
            answer_text = answer_obj.text if answer_obj else None
            qa_pairs.append(
                {
                    'index': idx + 1,
                    'question': question,
                    'answer': answer_text,
                    'answered_at': answer_obj.timestamp if answer_obj else None
                }
            )

        return Response(
            {
                'patient': {
                    'id': patient.id,
                    'full_name': patient.full_name,
                    'area_of_concern': patient.area_of_concern
                },
                'qa_pairs': qa_pairs
            }
        )


class LGBMDiagnosisView(APIView):
    """
    GET /api/lgbm-diagnose/<patient_id>/
    Runs a LightGBM classifier on the patient's recorded Q/A answers and
    returns a structured diagnosis conclusion that the admin can review and
    use as the starting point for the final diagnosis note.
    """

    permission_classes = [IsAuthenticated]

    AREA_MAPPING = {
        'Head':           'Head.json',
        'Breast':         'Breasts.json',
        'Breasts':        'Breasts.json',
        'Pelvis':         'Pelvic.json',
        'Urinary System': 'UrinarySystem.json',
        'Skin':           'Skin.json',
        'Hormonal':       'Hormone.json',
    }

    AREA_ALIASES = {
        'head': 'Head',
        'breast': 'Breast',
        'breasts': 'Breast',
        'pelvis': 'Pelvis',
        'urinary system': 'Urinary System',
        'skin': 'Skin',
        'hormonal': 'Hormonal',
    }

    @classmethod
    def _normalize_area(cls, area):
        key = (area or '').strip().lower()
        return cls.AREA_ALIASES.get(key, (area or '').strip())

    def get(self, request, patient_id):
        if request.user.role != 'admin':
            return Response(
                {'error': 'Only admins can run AI diagnosis.'},
                status=status.HTTP_403_FORBIDDEN
            )

        patient = get_object_or_404(Patient, id=patient_id)
        normalized_area = self._normalize_area(patient.area_of_concern)

        if not normalized_area or normalized_area not in self.AREA_MAPPING:
            return Response(
                {'error': f"Unknown area of concern: '{patient.area_of_concern}'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        dataset_filename = self.AREA_MAPPING[normalized_area]
        dataset_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'Datasets', dataset_filename
        )

        try:
            with open(dataset_path, 'r', encoding='utf-8') as f:
                dataset = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            return Response(
                {'error': f'Dataset error: {exc}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        intents = dataset.get('ourIntents', [])
        answer_messages = list(
            Message.objects.filter(
                patient=patient, sender__in=['user', 'patient']
            ).order_by('timestamp')
        )

        qa_pairs = []
        for idx, intent in enumerate(intents):
            question   = intent.get('patterns', [f'Question {idx + 1}'])[0]
            answer_obj = answer_messages[idx] if idx < len(answer_messages) else None
            qa_pairs.append({
                'index':    idx + 1,
                'question': question,
                'answer':   answer_obj.text if answer_obj else '',
            })

        scan_files = []
        for scan in Scan.objects.filter(patient=patient).order_by('-uploaded_at'):
            file_name = os.path.basename(scan.file.name) if scan.file else ''
            if file_name:
                file_path = ''
                try:
                    file_path = scan.file.path
                except Exception:
                    file_path = ''
                scan_files.append({
                    'name': file_name,
                    'path': file_path,
                    'uploaded_at': scan.uploaded_at.isoformat() if scan.uploaded_at else None,
                })

        try:
            from .lgbm_diagnosis import run_lgbm_diagnosis
            result = run_lgbm_diagnosis(qa_pairs, normalized_area, scan_files)
        except Exception as exc:
            logger.exception('Diagnosis inference error for patient %s: %s', patient_id, exc)
            return Response(
                {'error': 'Unable to generate a suggested diagnosis. Please try again or complete the diagnosis manually.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response(result)
