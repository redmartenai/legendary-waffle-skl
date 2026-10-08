"""Create (or promote) an EduFlow platform administrator. Operator tool; runs as the database owner.

    python manage.py create_platform_admin --email ops@example.com --full-name "Ops"

The password is read from ``EDUFLOW_ADMIN_PASSWORD`` or prompted for; it is never a command-line argument
(which would land in shell history and process listings).
"""

from __future__ import annotations

import getpass
import os
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from eduflow.audit import services as audit
from eduflow.identity.models import User
from eduflow.identity.phone import InvalidPhone, normalize_phone


class Command(BaseCommand):
    help = "Create or promote a platform administrator."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", required=True)
        parser.add_argument("--full-name", required=True)
        parser.add_argument("--phone")

    def handle(self, *args: Any, **options: Any) -> None:
        email = options["email"].strip().lower()
        try:
            phone = normalize_phone(options["phone"]) if options.get("phone") else None
        except InvalidPhone as exc:
            raise CommandError(str(exc)) from None

        user = User.objects.filter(email=email).first()
        password = os.environ.get("EDUFLOW_ADMIN_PASSWORD") or None
        if user is None and password is None:
            password = getpass.getpass("Password: ")
        if password is not None:
            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from None

        with transaction.atomic():
            if user is None:
                now = timezone.now()
                user = User.objects.create_user(
                    email=email,
                    phone=phone,
                    password=password,
                    full_name=options["full_name"],
                    email_verified_at=now,
                    phone_verified_at=now if phone else None,
                )
            elif password is not None:
                user.set_password(password)
            user.is_platform_admin = True
            user.save()
            audit.record(
                "identity.platform_admin.granted",
                actor_id=None,
                school_id=None,
                target_type="user",
                target_id=user.pk,
            )
        self.stdout.write(self.style.SUCCESS(f"Platform administrator ready: {user.pk}"))
