"""OTP challenges serve sign-in and invitation verification, by phone or email (docs/security/otp.md).

The address digest column becomes ``address_hash`` (it now covers email addresses too), challenges gain a
``subject_id`` (the invitation a code verifies), and sessions can record an invitation sign-in.
"""

from django.db import migrations, models
from django.utils import timezone


def invalidate_open_challenges(apps, schema_editor):  # type: ignore[no-untyped-def]
    """The address digest changes with this migration, so earlier open codes could no longer be matched for
    cooldowns and invalidation. They are closed (they live for minutes anyway)."""
    OtpChallenge = apps.get_model("identity", "OtpChallenge")
    OtpChallenge.objects.filter(consumed_at__isnull=True, invalidated_at__isnull=True).update(
        invalidated_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [("identity", "0002_identifier_verification")]

    operations = [
        migrations.RunPython(invalidate_open_challenges, migrations.RunPython.noop),
        migrations.RemoveIndex("otpchallenge", name="identity_otp_phone_idx"),
        migrations.RenameField("otpchallenge", "phone_hash", "address_hash"),
        migrations.AddIndex(
            "otpchallenge",
            models.Index(fields=["address_hash", "created_at"], name="identity_otp_address_idx"),
        ),
        migrations.AddField(
            "otpchallenge",
            "subject_id",
            models.UUIDField(blank=True, help_text="The invitation an invitation code verifies.", null=True),
        ),
        migrations.AlterField(
            "otpchallenge",
            "purpose",
            models.CharField(
                choices=[("login", "Sign in"), ("invitation", "Accept an invitation")], default="login", max_length=16
            ),
        ),
        migrations.AlterField(
            "authsession",
            "auth_method",
            models.CharField(
                choices=[
                    ("password", "Password"),
                    ("otp", "One-time code"),
                    ("invitation", "Accepted an invitation (one-time code)"),
                ],
                max_length=16,
            ),
        ),
    ]
