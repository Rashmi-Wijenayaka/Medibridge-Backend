import json

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from .models import DoctorMessage, Patient, User


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.patient_id = self.scope['url_route']['kwargs']['patient_id']
        self.room_group_name = f'chat_{self.patient_id}'

        scope_user = self.scope.get('user')
        if not scope_user or isinstance(scope_user, AnonymousUser) or not getattr(scope_user, 'is_authenticated', False):
            await self.close(code=4401)
            return

        if not await self.patient_exists(self.patient_id):
            await self.close(code=4404)
            return

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    # Receive message from WebSocket
    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        message = text_data_json['message']
        sender_id = text_data_json['sender_id']

        # Save message to database
        doctor_message = await self.save_message(sender_id, message)

        # Send message to room group
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': message,
                'sender': doctor_message.sender.username,
                'timestamp': doctor_message.timestamp.isoformat(),
            }
        )

    # Receive message from room group
    async def chat_message(self, event):
        message = event['message']
        sender = event['sender']
        timestamp = event['timestamp']

        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'message': message,
            'sender': sender,
            'timestamp': timestamp,
        }))

    @database_sync_to_async
    def save_message(self, sender_id, message):
        sender = User.objects.get(id=sender_id)
        patient = Patient.objects.get(id=self.patient_id)
        # Ensure visit_count is at least 1 when first doctor message is sent
        if (getattr(patient, 'visit_count', 0) or 0) < 1:
            patient.visit_count = 1
            patient.save(update_fields=['visit_count'])
        return DoctorMessage.objects.create(
            patient=patient,
            sender=sender,
            text=message
        )

    @database_sync_to_async
    def patient_exists(self, patient_id):
        return Patient.objects.filter(id=patient_id).exists()