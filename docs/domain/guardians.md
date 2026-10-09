# Guardians

A **guardian** is a parent or guardian as the school knows them: `full_name`, `phone` (normalised to E.164), `email`, `occupation`, and an optional `membership` when they sign in. The membership is read-only in the API: it is set only when the guardian accepts a guardian invitation (ADR-025), which is what gives a parent their children. Contact details live here because many guardians never have an account.

A **student-guardian link** (`/student-guardians`) connects them:

| Field | Notes |
|---|---|
| `student`, `guardian` | Same school (composite FKs); each pair at most once |
| `relationship` | `mother`, `father`, `guardian`, `grandparent`, `sibling`, `other` |
| `is_primary` | At most one per student. Setting it moves the flag from the previous primary guardian. |
| `is_emergency_contact`, `contact_preference` (`phone`, `sms`, `email`, `app`) | |

- A student may have many guardians, and a guardian many students.
- A parent's access to their children comes from these links: the `child` data scope.
- Filters: guardians by `student_id`; links by `student_id` and `guardian_id`.
- Data scopes:
  - `self`: the guardian's own record, and a student's own guardians
  - `section`: guardians of students the teacher teaches
- Permissions: `guardian.read`, `guardian.manage`.
- Audit: `people.guardian.created|updated|linked|link_updated|unlinked|account_linked`.
- Communication with guardians is a later phase.
