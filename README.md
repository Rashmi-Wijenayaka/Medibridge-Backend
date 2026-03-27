# Django Backend for ChatBot

This directory contains a Django project that serves as the backend for the React frontend located in `frontend/`.

## Setup

1. Create and activate a Python virtual environment (recommended):
   ```bash
   cd backend
   python -m venv venv
   venv\Scripts\activate    # Windows
   # or source venv/bin/activate on macOS/Linux
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Apply the migrations and create a superuser if needed:
   ```bash
   python manage.py makemigrations
   python manage.py migrate
   python manage.py createsuperuser  # optional, for accessing admin
   ```

4. Run the development server:
   ```bash
   python manage.py runserver
   ```

   The API will be available at `http://127.0.0.1:8000/api/`.

## API Endpoints

- `GET /api/` – returns a simple welcome message (used by `Home.jsx`).
- `POST /api/patients/` – create a new patient record (fields correspond to the diagnosis form). The response will include the saved object with its `id`.
- `GET /api/patients/` – list patient records.
- `POST /api/chat/` – send a chat message and receive a response from the model. You should supply `{ message: "...", patient_id: <id> }`; both the question and reply are logged on the server and associated with the patient.
- `GET /api/messages/?patient=<id>` – fetch the conversation history for a given patient.

The frontend now uses these endpoints to avoid hard‑coded chat text and to display real patient data. The chat is fully dynamic; messages are processed by the trained model at runtime and saved to the database.

## Integrating with Frontend

- The `Home` component can fetch `/api/` to confirm the backend is running before transitioning.
- Form submission in `DiagnosisForm` should send a POST request to `/api/patients/` with the form data.
- Chat messages (from `DiagnosticChat`) can be forwarded to `/api/chat/` to get AI responses.

## Notes

- Existing `chatbot.py` script has been reused inside `ChatbotAPIView` for inference. Make sure `Data.json` and trained model objects are present.
- The database is SQLite by default; you can configure `DATABASES` in `backend_project/settings.py` for other engines.

## Notifications (Free Method)

The backend sends **email notifications** to patients when they receive doctor messages or when diagnosis results are ready.

### Setup Free Email Notifications

1. **Choose a sender email account**
   - Recommended: Gmail account with App Password enabled.

2. **Set environment variables**
   Create a `.env` file in the `backend/` directory using `.env.example`:
   ```
   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
   EMAIL_HOST=smtp.gmail.com
   EMAIL_PORT=587
   EMAIL_USE_TLS=True
   EMAIL_HOST_USER=your_email@gmail.com
   EMAIL_HOST_PASSWORD=your_app_password_here
   DEFAULT_FROM_EMAIL=your_email@gmail.com
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **How it works**
   - When a doctor sends a message to a patient, an email is sent to the patient's email.
   - When an admin uploads diagnosis notes, an email notification is sent.
   - Notifications are sent only when the patient has an email saved.

5. **Customization**
   Edit message templates in `api/sms_utils.py`:
   - `notify_patient_new_message()`
   - `notify_patient_diagnosis_update()`
