import os
import shutil
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Backup DB and purge patient-related data for given emails (keeps User accounts).'

    def add_arguments(self, parser):
        parser.add_argument('--emails', type=str, required=True,
                            help='Comma-separated list of emails to target')
        parser.add_argument('--delete-media', action='store_true', dest='delete_media',
                            help='Also delete related media files (scans and summary_reports)')

    def handle(self, *args, **options):
        from api.models import Patient, Message, Diagnosis, DoctorMessage, Scan, PasswordResetOTP, User

        emails = [e.strip().lower() for e in options['emails'].split(',') if e.strip()]
        delete_media = options.get('delete_media', False)

        if not emails:
            raise CommandError('No emails provided')

        self.stdout.write(self.style.WARNING(f'Starting purge for emails: {emails}'))

        ts = timezone.now().strftime('%Y%m%d%H%M%S')
        backups_dir = os.path.abspath(os.path.join(os.getcwd(), 'db_backups'))
        os.makedirs(backups_dir, exist_ok=True)

        db_path = os.path.abspath(os.path.join(os.getcwd(), 'backend', 'db.sqlite3'))
        if os.path.exists(db_path):
            backup_db = os.path.join(backups_dir, f'db.sqlite3.{ts}')
            shutil.copy2(db_path, backup_db)
            self.stdout.write(self.style.SUCCESS(f'Backed up DB to {backup_db}'))
        else:
            self.stdout.write(self.style.WARNING(f'DB not found at {db_path}, skipping DB backup'))

        log_path = os.path.join(backups_dir, f'purge-log-{ts}.txt')
        log_lines = []

        # Find patients by Patient.email or patient.user.email
        patients_qs = Patient.objects.filter(email__in=emails) | Patient.objects.filter(user__email__in=emails)
        patients = list(patients_qs)
        if not patients:
            self.stdout.write(self.style.WARNING('No matching Patient records found for given emails.'))
            return

        patient_ids = [p.id for p in patients]
        log_lines.append(f'Found patients: {patient_ids}\n')

        # Messages
        msg_qs = Message.objects.filter(patient_id__in=patient_ids)
        msg_count = msg_qs.count()
        log_lines.append(f'Messages to delete: {msg_count}\n')
        msg_qs.delete()

        # DoctorMessage
        doc_qs = DoctorMessage.objects.filter(patient_id__in=patient_ids)
        doc_count = doc_qs.count()
        log_lines.append(f'DoctorMessages to delete: {doc_count}\n')
        doc_qs.delete()

        # Diagnosis
        diag_qs = Diagnosis.objects.filter(patient_id__in=patient_ids)
        diag_count = diag_qs.count()
        # collect summary_pdf files to delete if requested
        diag_files = [d.summary_pdf.path for d in diag_qs if d.summary_pdf and hasattr(d.summary_pdf, 'path')]
        log_lines.append(f'Diagnoses to delete: {diag_count}\n')
        diag_qs.delete()

        # Scans (delete files separately)
        scan_qs = Scan.objects.filter(patient_id__in=patient_ids)
        scan_count = scan_qs.count()
        scan_files = [s.file.path for s in scan_qs if s.file and hasattr(s.file, 'path')]
        log_lines.append(f'Scans to delete: {scan_count}\n')
        # delete scan DB rows (we'll delete files below if requested)
        scan_qs_ids = list(scan_qs.values_list('id', flat=True))
        scan_qs.delete()

        # PasswordResetOTP for linked users
        user_ids = [p.user_id for p in patients if p.user_id]
        otp_qs = PasswordResetOTP.objects.filter(user_id__in=user_ids)
        otp_count = otp_qs.count()
        log_lines.append(f'PasswordResetOTP to delete: {otp_count}\n')
        otp_qs.delete()

        # Finally delete Patient rows
        patient_delete_count = Patient.objects.filter(id__in=patient_ids).delete()
        log_lines.append(f'Patient delete result: {patient_delete_count}\n')

        # Optionally delete media files
        media_deleted = []
        if delete_media:
            # scan files
            for fpath in scan_files:
                try:
                    if os.path.exists(fpath):
                        os.remove(fpath)
                        media_deleted.append(fpath)
                except Exception as e:
                    log_lines.append(f'Failed to delete scan file {fpath}: {e}\n')

            # diagnosis summary PDFs
            for fpath in diag_files:
                try:
                    if os.path.exists(fpath):
                        os.remove(fpath)
                        media_deleted.append(fpath)
                except Exception as e:
                    log_lines.append(f'Failed to delete diag file {fpath}: {e}\n')

        # Write media list snapshot
        media_list_path = os.path.join(backups_dir, f'media-list-{ts}.txt')
        with open(media_list_path, 'w', encoding='utf-8') as mf:
            for p in scan_files + diag_files:
                mf.write(f'{p}\n')

        log_lines.append(f'Media files recorded to {media_list_path}\n')
        if media_deleted:
            log_lines.append(f'Media files deleted: {len(media_deleted)}\n')

        # Write purge log
        with open(log_path, 'w', encoding='utf-8') as lf:
            lf.writelines(log_lines)

        self.stdout.write(self.style.SUCCESS('Purge completed. Summary:'))
        for line in log_lines:
            self.stdout.write(line.strip())
        self.stdout.write(self.style.SUCCESS(f'Purge log written to {log_path}'))
