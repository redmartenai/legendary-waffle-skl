# API changelog

## 2026-10-10: school operations, communication, monitoring, LMS, reports

Additive: no existing path, field or enum name changed. Every path below is tenant-scoped (needs
`X-School-Id`), permission-checked and documented in [openapi.yaml](openapi.yaml), except
`POST /api/v1/admissions/apply` (public, rate-limited). New enums have explicit names
(`ENUM_NAME_OVERRIDES`); `CategoryEnum` and `ChannelEnum` keep their earlier meaning.

New paths by module:

### notifications

- `/api/v1/notifications`
- `/api/v1/notifications/read`
- `/api/v1/notifications/preferences`

### approvals

- `/api/v1/approvals`
- `/api/v1/approvals/{kind}/{id}/decision`

### documents

- `/api/v1/documents`
- `/api/v1/documents/{id}`
- `/api/v1/documents/{id}/download`

### admissions

- `/api/v1/admissions/apply`
- `/api/v1/admissions`
- `/api/v1/admissions/{id}`
- `/api/v1/admissions/{id}/move`
- `/api/v1/admissions/{id}/enrol`
- `/api/v1/admissions/{id}/documents`
- `/api/v1/admissions/{id}/documents/{id}`

### homework

- `/api/v1/homework`
- `/api/v1/homework/{id}`
- `/api/v1/homework/{id}/submissions`
- `/api/v1/homework/{id}/submit`
- `/api/v1/homework/{id}/attachment`
- `/api/v1/homework/submissions/{id}/review`
- `/api/v1/homework/submissions/{id}/file`

### conduct

- `/api/v1/remarks`
- `/api/v1/remarks/{id}`
- `/api/v1/incidents`
- `/api/v1/incidents/{id}`
- `/api/v1/incidents/{id}/resolve`

### assessment

- `/api/v1/exams`
- `/api/v1/exams/{id}`
- `/api/v1/exams/{id}/sheets`
- `/api/v1/exams/{id}/sheets/generate`
- `/api/v1/exams/{id}/publish`
- `/api/v1/exams/{id}/results`
- `/api/v1/mark-sheets/{id}`
- `/api/v1/mark-sheets/{id}/marks`
- `/api/v1/mark-sheets/{id}/submit`
- `/api/v1/marks/{id}/corrections`
- `/api/v1/students/{id}/report-card`
- `/api/v1/grade-bands`
- `/api/v1/grade-bands/{id}`

### fees

- `/api/v1/fee-plans`
- `/api/v1/fee-plans/{id}`
- `/api/v1/fee-plans/{id}/assign`
- `/api/v1/student-fees`
- `/api/v1/student-fees/{id}`
- `/api/v1/fee-payments`
- `/api/v1/fee-payments/{id}`
- `/api/v1/fee-payments/{id}/refunds`
- `/api/v1/students/{id}/fees`
- `/api/v1/fees/defaulters`
- `/api/v1/fees/collections`

### hr

- `/api/v1/hr/settings`
- `/api/v1/staff-attendance`
- `/api/v1/staff-attendance/check-in`
- `/api/v1/staff-attendance/check-out`
- `/api/v1/leave-types`
- `/api/v1/leave-types/{id}`
- `/api/v1/leave-requests`
- `/api/v1/leave-requests/{id}`
- `/api/v1/leave-requests/{id}/cancel`
- `/api/v1/leave-balances`
- `/api/v1/staff/{id}/salary`
- `/api/v1/payroll-runs`
- `/api/v1/payroll-runs/{id}`
- `/api/v1/payroll-runs/{id}/process`
- `/api/v1/payroll-runs/{id}/paid`
- `/api/v1/payslips`
- `/api/v1/payslips/{id}`
- `/api/v1/job-openings`
- `/api/v1/job-openings/{id}`
- `/api/v1/candidates`
- `/api/v1/candidates/{id}`
- `/api/v1/candidates/{id}/resume`

### library

- `/api/v1/library/settings`
- `/api/v1/library/books`
- `/api/v1/library/books/{id}`
- `/api/v1/library/books/{id}/copies`
- `/api/v1/library/copies/{id}/status`
- `/api/v1/library/loans`
- `/api/v1/library/loans/{id}`
- `/api/v1/library/loans/{id}/return`
- `/api/v1/library/loans/{id}/renew`
- `/api/v1/library/loans/{id}/fine-paid`

### hostel

- `/api/v1/hostels`
- `/api/v1/hostels/{id}`
- `/api/v1/hostels/{id}/rooms`
- `/api/v1/hostels/{id}/roll-call`
- `/api/v1/hostel/allocations`
- `/api/v1/hostel/allocations/{id}`
- `/api/v1/hostel/outpasses`
- `/api/v1/hostel/outpasses/{id}`
- `/api/v1/hostel/outpasses/{id}/gate`
- `/api/v1/hostel/outpasses/{id}/cancel`
- `/api/v1/hostel/roll-call`

### transport

- `/api/v1/transport/vehicles`
- `/api/v1/transport/vehicles/{id}`
- `/api/v1/transport/routes`
- `/api/v1/transport/routes/{id}`
- `/api/v1/transport/routes/{id}/trips`
- `/api/v1/transport/riders`
- `/api/v1/transport/riders/{id}`
- `/api/v1/transport/trips`
- `/api/v1/transport/trips/{id}`
- `/api/v1/transport/trips/{id}/arrive`
- `/api/v1/transport/trips/{id}/delay`
- `/api/v1/transport/trips/{id}/positions`
- `/api/v1/transport/maintenance`
- `/api/v1/transport/maintenance/{id}`

### inventory

- `/api/v1/inventory/items`
- `/api/v1/inventory/items/{id}`
- `/api/v1/inventory/movements`
- `/api/v1/inventory/assets`
- `/api/v1/inventory/assets/{id}`
- `/api/v1/procurement/vendors`
- `/api/v1/procurement/vendors/{id}`
- `/api/v1/procurement/requisitions`
- `/api/v1/procurement/requisitions/{id}`
- `/api/v1/procurement/orders`
- `/api/v1/procurement/orders/{id}`
- `/api/v1/procurement/orders/{id}/status`
- `/api/v1/procurement/orders/{id}/receive`
- `/api/v1/procurement/orders/{id}/invoices`
- `/api/v1/procurement/invoices`
- `/api/v1/procurement/invoices/{id}/pay`

### visitors

- `/api/v1/visits`
- `/api/v1/visits/scan`
- `/api/v1/visits/inside`
- `/api/v1/visits/{id}`
- `/api/v1/visits/{id}/decision`
- `/api/v1/visits/{id}/pass`
- `/api/v1/visits/{id}/cancel`

### alumni

- `/api/v1/alumni`
- `/api/v1/alumni/{id}`
- `/api/v1/alumni-events`
- `/api/v1/alumni-events/{id}`
- `/api/v1/alumni-events/{id}/registrations`
- `/api/v1/alumni-campaigns`
- `/api/v1/alumni-campaigns/{id}`
- `/api/v1/alumni-campaigns/{id}/donations`

### communication

- `/api/v1/announcements`
- `/api/v1/announcements/pending`
- `/api/v1/announcements/{id}`
- `/api/v1/announcements/{id}/acknowledge`
- `/api/v1/announcements/{id}/responses`
- `/api/v1/announcements/{id}/acknowledgements`
- `/api/v1/threads`
- `/api/v1/threads/{id}`
- `/api/v1/threads/{id}/messages`
- `/api/v1/complaints`
- `/api/v1/complaints/sentiment`
- `/api/v1/complaints/{id}`

### lms

- `/api/v1/lms/lessons`
- `/api/v1/lms/lessons/{id}`
- `/api/v1/lms/lessons/{id}/file`
- `/api/v1/lms/lessons/{id}/progress`
- `/api/v1/lms/quizzes`
- `/api/v1/lms/quizzes/generate`
- `/api/v1/lms/quizzes/{id}`
- `/api/v1/lms/quizzes/{id}/attempts`
- `/api/v1/lms/paths`
- `/api/v1/lms/paths/{id}`
- `/api/v1/lms/paths/{id}/progress`
- `/api/v1/lms/live-classes`
- `/api/v1/lms/live-classes/{id}`
- `/api/v1/lms/live-classes/{id}/join`
- `/api/v1/lms/analytics`

### monitoring

- `/api/v1/monitoring/alerts`
- `/api/v1/monitoring/alerts/{id}`
- `/api/v1/monitoring/alerts/{id}/acknowledge`
- `/api/v1/monitoring/alerts/{id}/resolve`
- `/api/v1/monitoring/evaluate`
- `/api/v1/monitoring/rules`
- `/api/v1/monitoring/settings`
- `/api/v1/monitoring/pulse`
- `/api/v1/monitoring/risk`
- `/api/v1/students/{id}/risk`
- `/api/v1/monitoring/scorecards`
- `/api/v1/monitoring/ask`

### reports

- `/api/v1/reports/attendance`
- `/api/v1/reports/fee-dues`
- `/api/v1/reports/exam-results`
- `/api/v1/reports/staff-attendance`
- `/api/v1/reports/admissions`
