# School operations modules

Each module below is a Django app under `backend/eduflow/<module>`. Its `models.py` docstring is the
authoritative statement of its rules; this page is the map. Every module:

- keeps its rows in the school's tenant (RLS on every table, same-school composite foreign keys);
- reads through data scopes (`policies.py`; shared rules in `people/scoping.py`);
- writes through services that validate, run in a transaction and write an audit event;
- has tests for its flows, permissions, cross-school isolation (`*_MATRIX`) and RLS.

| Module | Records | Key rules | Permissions |
|---|---|---|---|
| `admissions` | Application, StageChange, ApplicationDocument | Forward-only stages; offers approved in the queue; enrolment creates the student, guardian and enrollment through `people` | `admission.read/manage/approve` |
| `homework` | Homework, Submission | Set for a section and subject the teacher teaches; late after the due date (school time); replaceable until reviewed; no row = pending | `homework.read/manage/submit/review` |
| `conduct` | Remark, Incident | Teachers write about students they teach; families see only what is marked visible | `remark.*`, `behaviour.*` |
| `assessment` | Exam, MarkSheet, Mark, MarkCorrection, GradeBand | draft -> submitted -> approved -> published; families see published marks only; corrections after approval go through the queue; bands are the school's | `exam.*`, `assessment.read/create/update/approve` |
| `fees` | FeePlan, Instalment, StudentFee, Payment, Refund | Scholarship %; sequential receipts; idempotent payments; refunds approved in the queue; overdue = due to date - net paid | `fee.read/update/manage/approve` |
| `hr` | StaffAttendance, LeaveType, LeaveRequest, SalaryStructure, PayrollRun, Payslip, JobOpening, Candidate | Late after the school's time (default 08:00); leave in the queue against the school's leave types; payroll applies entered amounts and freezes payslips | `staff_attendance.*`, `leave.*`, `payroll.*`, `recruitment.*` |
| `library` | Book, Copy, Loan, LibrarySettings | One open loan per copy; loan period, fine rate and limit are settings; daily overdue reminders | `library.read/manage` |
| `hostel` | Hostel, Room, Allocation, Outpass, RollCall | Beds never overbooked; outpasses requested by families, approved in the queue, checked at the gate | `hostel.read/manage/outpass/approve` |
| `transport` | Vehicle, Route, Stop, Rider, Trip, Position, Maintenance | Capacity; drivers run their own routes; delays of 10+ minutes notify families; positions only as reported | `transport.read/manage/operate` |
| `inventory` | Item, StockMovement, Asset, Vendor, Requisition, PurchaseOrder, OrderLine, GoodsReceipt, ReceiptLine, VendorInvoice | Stock never negative; requisitions approved in the queue (`expense`); goods received add stock; no depreciation | `inventory.*`, `procurement.*` |
| `visitors` | Visit | Hosts pre-register, security approves; random single-use QR pass, stored as a hash; valid on the visit's day | `visitor.read/register/manage` |
| `alumni` | Alumnus, Event, Registration, Campaign, Donation | Consent to contact; event capacity; sequential donation receipts | `alumni.read/manage` |
| `communication` | Announcement, Acknowledgement, AnnouncementResponse, Thread, Message, Complaint | Audience addressing; acknowledgements; staff responses; reply tracking; reporter-chosen sentiment | `announcement.*`, `message.*`, `complaint.*` |
| `lms` | Lesson, Progress, Quiz, Question, Attempt, LearningPath, PathStep, LiveClass, LiveAttendance | Families see published content; progress only grows; keys hidden until a first attempt; AI drafts only with a provider | `lms.read/manage/learn` |
| `monitoring` | MonitoringSettings, Alert | See ADR-031 | `monitoring.read/manage/ask` |
| `reports` | (none) | JSON or CSV; scoped; audited; CSV formula neutralising | `report.read` + the data's permission |

Shared services: `documents` (files and document audiences), `notifications` (in-app, email, SMS),
`approvals` (the central queue) and `tenancy.jobs` (scheduled per-school jobs).
