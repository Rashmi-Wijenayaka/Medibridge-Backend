from rest_framework import serializers
from .models import Patient, Message, Diagnosis, DoctorMessage, Scan, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'role']


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = '__all__'


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = '__all__'


class DiagnosisSerializer(serializers.ModelSerializer):
    admin = UserSerializer(read_only=True)
    patient_details = PatientSerializer(source='patient', read_only=True)

    class Meta:
        model = Diagnosis
        fields = '__all__'


class DoctorMessageSerializer(serializers.ModelSerializer):
    sender = UserSerializer(read_only=True)

    class Meta:
        model = DoctorMessage
        fields = '__all__'


class ScanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Scan
        fields = '__all__'
