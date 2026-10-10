# Requirements traceability

How each product requirement maps to backend code, endpoints and tests, and what is blocked and why.

## Sources

The four product analysis reports named in the brief were not available in the repository or the
workspace. The requirements below come from the sources that were available, in this order of authority:

| Source | What it gives |
|---|---|
| EduFlow screen documentation (`EDUflow.docx`, "Full screen documentation") | Modules per role, the role matrix, action types (view, create, edit, archive, approve, export, download, publish), the central approvals queue |
| EduFlow enterprise high-level design (HLD) | Module list, Monitoring Intelligence Layer, Ask EduFlow, LMS, integrations, multi-tenancy |
| Prototype (`eduflow-app`, React/TypeScript): `data/types.ts`, `engine/monitoring.ts`, `engine/ask.ts`, `store/school.ts`, README, DESIGN | Field-level domain model, the alert rule set and thresholds, owners and escalations, risk weights, the scorecard formula, the pulse KPIs, the Ask intents |
| Repository docs (`CURRENT_STATE.md`, ADR-001 to ADR-029) | The client contract and the decisions already taken |

Where a source states a value (75% attendance, 12-point marks drop, 24-hour reply, 08:15 registers,
08:00 late check-in, the risk weights, the scorecard weights), the backend uses it as the default and makes
it a school setting. Where no source states a rule, the backend does not invent one; see
[Blocked or deferred](#blocked-or-deferred).

Status: **Existing** (before this work), **Built** (this work), **Partial**, **Blocked**.

## Core ERP

| Requirement | Source | Status | Module and endpoints | Tests |
|---|---|---|---|---|
| Admissions pipeline: enquiry, visit, assessment, offer, enrolled, dropped; sources | Prototype `Admission`; screens "Admissions" | Built | `admissions`: `/admissions`, `/move`, `/enrol`, documents | `admissions/tests` |
| Offer approval in the central queue; enrolment creates student, guardian, enrollment | Screens: approvals "admissions" | Built | approvals kind `admission`; `people` services | same |
| Online application (public) | HLD; prototype source `Website` | Built | `POST /admissions/apply` (rate-limited, enquiry only) | same |
| Student lifecycle: enrol, withdraw, complete, transfer | Phase 3 | Existing | `people` | `people/tests` |
| Homework and assignments: set, attach, submit, late, review | Prototype `Homework`; screens "Homework" | Built | `homework`: `/homework`, `/submit`, `/submissions`, `/review` | `homework/tests` |
| Remarks (praise, concern, note; visible to parent) | Prototype `Remark` | Built | `conduct`: `/remarks` | `conduct/tests` |
| Behaviour incidents | Prototype `behaviourIncidents`; rule "Repeated behaviour notes" | Built | `conduct`: `/incidents`, `/resolve` | same |
| Exams, mark sheets, deadline, submit, approve, publish | Prototype `Exam`; screens "Exams" | Built | `assessment`: `/exams`, `/mark-sheets`, approvals kind `mark_sheet` | `assessment/tests` |
| Marks corrections through approvals | Prototype approval kind `marks-correction` | Built | `/marks/{id}/corrections`, approvals kind `marks_correction` | same |
| Results, ranks, report cards, grading | Screens "Report cards" | Built (grade bands are the school's own) | `/exams/{id}/results`, `/students/{id}/report-card`, `/grade-bands` | same |
| Fee plans, instalments, scholarships | Prototype `feeTotal`, `scholarshipPct` | Built | `fees`: `/fee-plans`, `/student-fees` | `fees/tests` |
| Payments with receipts (UPI, card, cash, bank transfer, cheque), idempotent | Prototype `FeePayment` | Built (recorded by staff) | `/fee-payments` | same |
| Refunds through approvals | Prototype approval kind `refund` | Built | `/fee-payments/{id}/refunds`, approvals kind `refund` | same |
| Statements, defaulters, collections | Screens "Fees", "Defaulters" | Built | `/students/{id}/fees`, `/fees/defaulters`, `/fees/collections` | same |
| Online payments | HLD integrations | **Blocked** | No payment gateway is integrated | - |
| Staff attendance: check-in, late after 08:00 | Prototype `StaffDay`; rule "Frequent late arrivals" | Built (time is a setting) | `hr`: `/staff-attendance`, `/check-in`, `/check-out`, `/hr/settings` | `hr/tests` |
| Leave requests through approvals, balances | Prototype approval kind `leave`, `leaveBalance` | Built (leave types are the school's) | `/leave-types`, `/leave-requests`, `/leave-balances`, approvals kind `leave` | same |
| Payroll runs (draft, processed, paid), payslips | Prototype `PayrollRun`, `Salary` | Built | `/staff/{id}/salary`, `/payroll-runs`, `/payslips` | same |
| PF, ESI, TDS | Prototype `Salary` fields | Partial: entered amounts only | same | same |
| Recruitment: openings, candidates | HLD | Partial: stages are provisional | `/job-openings`, `/candidates` | same |
| Library: catalogue, copies, issue, return, renew, fines | Screens "Library"; role Librarian | Built (loan period and fine rate are settings) | `library`: `/library/books`, `/library/loans`, `/library/settings` | `library/tests` |
| Hostel: rooms, allocation, outpass approval, roll call | Screens "Hostel"; role Hostel Manager | Built | `hostel`: `/hostels`, `/hostel/allocations`, `/hostel/outpasses`, approvals kind `outpass` | `hostel/tests` |
| Transport: vehicles, routes, stops, riders, trips, delays | Prototype `Bus`; rule "Bus delayed" | Built | `transport`: `/transport/...` | `transport/tests` |
| Live GPS | HLD | Partial: reported fixes only | `POST /transport/trips/{id}/positions` | same |
| Vehicle maintenance and compliance dates | HLD | Built | `/transport/maintenance` | same |
| Inventory: items, stock movements, reorder level | HLD | Built | `inventory`: `/inventory/items`, `/inventory/movements` | `inventory/tests` |
| Assets | HLD | Built (no depreciation) | `/inventory/assets` | same |
| Vendors and procurement: requisition, PO, goods received, invoice | HLD; prototype approval kind `expense` | Built | `/procurement/...`, approvals kind `expense` | same |
| Visitor management: QR passes, security approval | HLD; prototype README roadmap | Built | `visitors`: `/visits`, `/decision`, `/pass`, `/scan` | `visitors/tests` |
| Alumni: directory, events, campaigns, donations | HLD; prototype README roadmap | Built | `alumni`: `/alumni`, `/alumni-events`, `/alumni-campaigns` | `alumni/tests` |
| Documents with audiences | Screens "Documents" | Built | `documents`: `/documents` | `documents/tests` |
| Central approvals queue | Screens | Built | `approvals`: `/approvals`, `/approvals/{kind}/{id}/decision` | `approvals/tests` and each provider |

## Communication

| Requirement | Source | Status | Module and endpoints |
|---|---|---|---|
| Announcements to staff, parents, students; acknowledgement; staff responses | Prototype `Announcement` | Built | `communication`: `/announcements`, `/acknowledge`, `/responses`, `/acknowledgements`, `/announcements/pending` |
| Parent and class-teacher / subject-teacher threads; student-teacher | Prototype `Thread` kinds | Built | `/threads`, `/threads/{id}/messages` |
| Pending-reply tracking | Rule "Parent waiting 24h+" | Built | `awaiting_reply_since`, `?awaiting_reply=true` |
| Complaints and feedback with sentiment | Prototype `Complaint` | Built (sentiment chosen by the reporter) | `/complaints`, `/complaints/sentiment` |
| In-app notifications and preferences; email and SMS delivery | Prototype `Notification` | Built | `notifications`: `/notifications`, `/notifications/preferences` |
| WhatsApp, push | HLD, prototype roadmap | **Blocked** | No provider integrated |

## Monitoring Intelligence Layer

| Requirement | Source | Status | Where |
|---|---|---|---|
| Rule engine over all modules | HLD; prototype `computeAlerts` | Built | `monitoring/rules.py` (14 rules) |
| Deduplicated alerts with lifecycle open, acknowledged, resolved, auto-resolve | HLD | Built | `monitoring/engine.py`, `/monitoring/alerts` |
| Explanation ("why"), owner, escalation | Prototype alert fields; README table | Built | alert fields; escalation notifies the principal once |
| Role-scoped views | HLD | Built | school scope sees all; a teacher sees alerts with their rows, trimmed to them |
| School pulse KPIs and morning brief | Prototype `schoolPulse`, `morningBrief` | Built | `/monitoring/pulse` |
| Student risk score | Prototype `studentRisk` | Built (same weights) | `/monitoring/risk`, `/students/{id}/risk` |
| Teacher scorecards | Prototype `teacherScorecard` | Built (same formula) | `/monitoring/scorecards` |
| Ask EduFlow over authorised data | Prototype `ask.ts` | Built (deterministic intents) | `/monitoring/ask` |
| Parent sentiment | Prototype complaints sentiment | Built | `/complaints/sentiment`, rule `negative_feedback` |
| Thresholds per school | Prototype `RULES` | Built | `/monitoring/settings` |
| Scheduled evaluation | HLD | Built | beat `tenancy.school_jobs_frequent` -> per-school tenant task |
| "Class without a teacher" | Prototype `staff-cover` | **Blocked** | Substitutions are not recorded |

## LMS

| Requirement | Source | Status | Where |
|---|---|---|---|
| Content repository (notes, files) | Prototype `Lesson` kind notes | Built | `/lms/lessons` |
| Recorded video lessons | Prototype `Lesson` kind video | Partial: link to the school's video host | `video_url` (https only) |
| Learning progress | Prototype `progress` | Built | `/lms/lessons/{id}/progress` |
| Quizzes (MCQ, explanations, best score) | Prototype `Quiz` | Built | `/lms/quizzes`, `/attempts` |
| Learning paths | HLD | Built | `/lms/paths`, `/progress` |
| Live classes | HLD; prototype roadmap | Built (provider-neutral link) | `/lms/live-classes`, `/join` |
| AI-generated assessments | HLD; prototype roadmap | Partial: only with a configured provider | `POST /lms/quizzes/generate` answers 503 without one |
| Learning analytics | HLD | Built | `/lms/analytics` |

## Reports

| Requirement | Status | Where |
|---|---|---|
| Attendance, fee dues, exam results, staff attendance, admissions funnel; JSON or CSV (`?export=csv`) export; audited | Built | `reports`: `/reports/...` |

## Blocked or deferred

| Item | Why | What exists instead |
|---|---|---|
| Online fee payments | No payment gateway or merchant account | Staff record payments with receipts; idempotent |
| Statutory PF, ESI, TDS calculation | Rules depend on registration, state and tax regime; no source defines them | Entered amounts, applied as given |
| Asset depreciation | No method, rate or useful life in any source | Cost and purchase date are kept |
| Library loan period and fine rate | Not in any source | School settings; no fine without a rate |
| Recruitment stages | Not in any source | Provisional stages: applied, screening, interview, offer, hired, rejected |
| Video hosting | No hosting provider | Lessons link to where the school hosts video |
| Live-class provider integration | No provider chosen | Any provider's join link; attendance on join |
| AI question generation | No provider and no credentials | Provider interface; 503 until configured; output validated and returned as a draft |
| Live GPS feed | No tracking provider or device | Reported fixes only; never estimated |
| WhatsApp, push notifications | No provider | In-app, email and SMS channels |
| "Class without a teacher" alert | Substitutions are not recorded | - |
| Holiday calendar | Not built | School days are inferred from the published timetable and from registers taken |
| Event outbox (ADR-010) | Not built | Scheduled per-school evaluation |
