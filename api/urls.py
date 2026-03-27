from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import HomeView, PatientViewSet, MessageViewSet, ChatbotAPIView, DatasetView, DiagnosisViewSet, DoctorMessageViewSet, ScanViewSet, LoginView, SignupView, PatientSignupView, PatientLoginView, PatientRequestPasswordResetOTPView, PatientVerifyPasswordResetOTPView, ResetPasswordView, RequestPasswordResetOTPView, VerifyPasswordResetOTPView, LogoutView, TokenVerifyView, CheckPatientMessagesView, GeneratePDFView, SendEmailView, GenerateSummaryPDFView, SendSummaryToDoctorView, PatientQAView, LGBMDiagnosisView

router = DefaultRouter()
router.register(r'patients', PatientViewSet, basename='patient')
router.register(r'messages', MessageViewSet, basename='message')
router.register(r'diagnoses', DiagnosisViewSet, basename='diagnosis')
router.register(r'doctormessages', DoctorMessageViewSet, basename='doctormessage')
router.register(r'scans', ScanViewSet, basename='scan')

urlpatterns = [
    path('', HomeView.as_view(), name='home'),
    path('chat/', ChatbotAPIView.as_view(), name='chatbot'),
    path('dataset/', DatasetView.as_view(), name='dataset'),
    path('login/', LoginView.as_view(), name='login'),
    path('signup/', SignupView.as_view(), name='signup'),
    path('patient-signup/', PatientSignupView.as_view(), name='patient_signup'),
    path('patient-login/', PatientLoginView.as_view(), name='patient_login'),
    path('patient-request-reset-otp/', PatientRequestPasswordResetOTPView.as_view(), name='patient_request_reset_otp'),
    path('patient-verify-reset-otp/', PatientVerifyPasswordResetOTPView.as_view(), name='patient_verify_reset_otp'),
    path('reset-password/', ResetPasswordView.as_view(), name='reset_password'),
    path('request-reset-otp/', RequestPasswordResetOTPView.as_view(), name='request_reset_otp'),
    path('verify-reset-otp/', VerifyPasswordResetOTPView.as_view(), name='verify_reset_otp'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('auth-check/', TokenVerifyView.as_view(), name='auth_check'),
    path('check-messages/', CheckPatientMessagesView.as_view(), name='check_messages'),
    path('generate-pdf/<int:diagnosis_id>/', GeneratePDFView.as_view(), name='generate_pdf'),
    path('send-email/<int:diagnosis_id>/', SendEmailView.as_view(), name='send_email'),
    path('generate-summary-pdf/<int:patient_id>/', GenerateSummaryPDFView.as_view(), name='generate_summary_pdf'),
    path('send-summary-to-doctor/<int:diagnosis_id>/', SendSummaryToDoctorView.as_view(), name='send_summary_to_doctor'),
    path('patient-qa/<int:patient_id>/', PatientQAView.as_view(), name='patient_qa'),
    path('lgbm-diagnose/<int:patient_id>/', LGBMDiagnosisView.as_view(), name='lgbm_diagnose'),
    path('', include(router.urls)),
]
