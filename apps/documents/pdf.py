"""Printable PDFs generated on request: report cards and fee receipts (A4, reportlab)."""

import io
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

INK = colors.HexColor("#1C1B22")
MUTED = colors.HexColor("#6B6875")
LINE = colors.HexColor("#DDD7CB")
BRAND = colors.HexColor("#3446C8")


def _rupees(value) -> str:
    amount = Decimal(str(value)).quantize(Decimal("1"))
    s = f"{int(amount):d}"
    # Indian digit grouping: 1,19,000
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "Rs. " + ",".join(groups + [tail]) if groups else "Rs. " + tail


def _header(c: canvas.Canvas, school_name: str, title: str, subtitle: str) -> float:
    w, h = A4
    c.setFillColor(BRAND)
    c.roundRect(20 * mm, h - 32 * mm, 12 * mm, 12 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(36 * mm, h - 25 * mm, school_name)
    c.setFont("Helvetica", 9.5)
    c.setFillColor(MUTED)
    c.drawString(36 * mm, h - 30.5 * mm, subtitle)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(w - 20 * mm, h - 25 * mm, title)
    c.setStrokeColor(LINE)
    c.line(20 * mm, h - 38 * mm, w - 20 * mm, h - 38 * mm)
    return h - 48 * mm


def report_card_pdf(results: dict, exam_id: str) -> bytes:
    exam = next(e for e in results["exams"] if e["id"] == exam_id)
    previous = next((e for e in results["exams"] if e["held_on"] < exam["held_on"]), None)
    prev_by_subject = {s["subject"]: s for s in (previous["subjects"] if previous else [])}
    student = results["student"]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{student['name']} - {exam['name']} report card")
    w, _h = A4
    y = _header(c, results["school"]["name"], "Progress report", f"{exam['name']} · held {exam['held_on']}")

    c.setFont("Helvetica", 10)
    c.setFillColor(INK)
    c.drawString(20 * mm, y, f"{student['name']}  ·  Class {student['class']}  ·  Roll {student['roll_no']}  ·  Adm. No. {student['admission_no']}")
    y -= 12 * mm

    cols = [20 * mm, 95 * mm, 125 * mm, 145 * mm, 165 * mm]
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(MUTED)
    for x, label in zip(cols, ["SUBJECT (TEACHER)", previous["name"].upper() if previous else "", exam["name"].upper(), "GRADE", "CLASS AVG"]):
        c.drawString(x, y, label)
    y -= 4 * mm
    c.setStrokeColor(LINE)
    c.line(20 * mm, y, w - 20 * mm, y)
    y -= 7 * mm
    for s in exam["subjects"]:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(cols[0], y, s["subject"])
        if s.get("teacher"):
            c.setFont("Helvetica", 8.5)
            c.setFillColor(MUTED)
            c.drawString(cols[0], y - 4 * mm, s["teacher"])
        c.setFont("Helvetica", 10.5)
        c.setFillColor(MUTED)
        prev = prev_by_subject.get(s["subject"])
        c.drawString(cols[1], y, f"{prev['percent']:.0f}" if prev else "–")
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(cols[2], y, f"{s['percent']:.0f}")
        c.drawString(cols[3], y, s["grade"])
        c.setFont("Helvetica", 10.5)
        c.drawString(cols[4], y, f"{s['class_average']:.0f}" if s["class_average"] is not None else "–")
        y -= 11 * mm
    c.line(20 * mm, y + 5 * mm, w - 20 * mm, y + 5 * mm)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(cols[0], y, "Overall")
    if previous:
        c.drawString(cols[1], y, f"{previous['percent']:.0f}%")
    c.drawString(cols[2], y, f"{exam['percent']:.0f}%")
    c.drawString(cols[3], y, exam["grade"])
    if exam.get("class_average") is not None:
        c.drawString(cols[4], y, f"{exam['class_average']:.0f}%")
    y -= 16 * mm

    note = exam.get("note")
    if note:
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(MUTED)
        c.drawString(20 * mm, y, "CLASS TEACHER'S NOTE")
        y -= 6 * mm
        c.setFont("Helvetica", 10.5)
        c.setFillColor(INK)
        for line in _wrap(note["body"], 95):
            c.drawString(20 * mm, y, line)
            y -= 5.5 * mm
        y -= 6 * mm

    y = max(y, 45 * mm)
    c.setStrokeColor(LINE)
    for x, who, role in ((20 * mm, results.get("class_teacher"), "Class teacher"), (120 * mm, results["school"].get("principal"), "Principal")):
        c.line(x, y, x + 60 * mm, y)
        c.setFont("Helvetica-Bold", 9.5)
        c.setFillColor(INK)
        c.drawString(x, y - 5 * mm, who or "")
        c.setFont("Helvetica", 8.5)
        c.setFillColor(MUTED)
        c.drawString(x, y - 9.5 * mm, role)
    c.showPage()
    c.save()
    return buf.getvalue()


def receipt_pdf(*, school_name: str, receipt_no: str, paid_at, student: str, class_label: str, title: str, items: list[tuple[str, Decimal]], amount, method: str, paid_by: str | None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Receipt {receipt_no}")
    w, _h = A4
    y = _header(c, school_name, "Fee receipt", f"Receipt {receipt_no} · {paid_at:%d %b %Y, %I:%M %p}")
    c.setFont("Helvetica", 10.5)
    c.setFillColor(INK)
    c.drawString(20 * mm, y, f"Received for {student} (Class {class_label})")
    y -= 6 * mm
    if paid_by:
        c.setFillColor(MUTED)
        c.drawString(20 * mm, y, f"Paid by {paid_by} · {method.upper() if method else 'Online'}")
        y -= 6 * mm
    y -= 6 * mm
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(INK)
    c.drawString(20 * mm, y, title)
    y -= 8 * mm
    c.setFont("Helvetica", 10.5)
    for head, value in items:
        c.drawString(24 * mm, y, head)
        c.drawRightString(w - 20 * mm, y, _rupees(value))
        y -= 6.5 * mm
    c.setStrokeColor(LINE)
    c.line(20 * mm, y + 2 * mm, w - 20 * mm, y + 2 * mm)
    y -= 5 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(24 * mm, y, "Total paid")
    c.drawRightString(w - 20 * mm, y, _rupees(amount))
    y -= 20 * mm
    c.setFillColor(colors.HexColor("#1F7A4D"))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, y, "PAID")
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawString(20 * mm, 20 * mm, "Tuition and related fees of a recognised school are exempt from GST. This receipt is computer generated.")
    c.showPage()
    c.save()
    return buf.getvalue()


def _wrap(text: str, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


def invoice_pdf(*, school_name: str, title: str, student: str, class_label: str, due_date, items: list[tuple[str, Decimal]], amount, balance) -> bytes:
    """A fee invoice (challan) listing each fee head, as families get on paper."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{title} - {student}")
    w, _h = A4
    y = _header(c, school_name, "Fee invoice", f"{title} · due {due_date:%d %b %Y}")
    c.setFont("Helvetica", 10.5)
    c.setFillColor(INK)
    c.drawString(20 * mm, y, f"{student} (Class {class_label})")
    y -= 14 * mm
    for head, value in items:
        c.drawString(24 * mm, y, head)
        c.drawRightString(w - 20 * mm, y, _rupees(value))
        y -= 6.5 * mm
    c.setStrokeColor(LINE)
    c.line(20 * mm, y + 2 * mm, w - 20 * mm, y + 2 * mm)
    y -= 5 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(24 * mm, y, "Total")
    c.drawRightString(w - 20 * mm, y, _rupees(amount))
    y -= 8 * mm
    c.setFont("Helvetica", 10.5)
    c.drawString(24 * mm, y, "Still to pay")
    c.drawRightString(w - 20 * mm, y, _rupees(balance))
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.5)
    c.drawString(20 * mm, 20 * mm, "Pay in the EduFlow app or at the school office. This invoice is computer generated.")
    c.showPage()
    c.save()
    return buf.getvalue()


def datesheet_pdf(*, school_name: str, exam: str, class_label: str, papers: list, report_by=None) -> bytes:
    """An exam date sheet: one row per paper with time, room and syllabus."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{exam} date sheet - {class_label}")
    w, _h = A4
    subtitle = f"Class {class_label}" + (f" · be seated by {report_by:%I:%M %p}" if report_by else "")
    y = _header(c, school_name, f"{exam} · date sheet", subtitle)
    for date, subject, starts, ends, room, syllabus in papers:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20 * mm, y, f"{date:%a %d %b}")
        c.drawString(55 * mm, y, subject)
        c.setFont("Helvetica", 10)
        c.drawRightString(w - 20 * mm, y, f"{starts:%I:%M}–{ends:%I:%M %p} · {room}")
        y -= 5.5 * mm
        if syllabus:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 9)
            c.drawString(55 * mm, y, " · ".join(syllabus))
            y -= 5 * mm
        c.setStrokeColor(LINE)
        c.line(20 * mm, y + 1.5 * mm, w - 20 * mm, y + 1.5 * mm)
        y -= 7 * mm
    c.showPage()
    c.save()
    return buf.getvalue()


def admit_card_pdf(*, school_name: str, exam: str, student: str, class_label: str, roll_no, admission_no: str, papers: list, report_by=None) -> bytes:
    """The admit card: who may sit the exam, and where and when each paper is."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{exam} admit card - {student}")
    w, _h = A4
    y = _header(c, school_name, f"{exam} · admit card", f"Class {class_label}")
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, y, student)
    y -= 6.5 * mm
    c.setFont("Helvetica", 10)
    c.setFillColor(MUTED)
    c.drawString(20 * mm, y, f"Class {class_label} · Roll {roll_no} · Adm. no. {admission_no}")
    y -= 5.5 * mm
    if report_by:
        c.drawString(20 * mm, y, f"Be in your seat by {report_by:%I:%M %p} with this card.")
        y -= 5.5 * mm
    c.setStrokeColor(LINE)
    c.line(20 * mm, y, w - 20 * mm, y)
    y -= 9 * mm
    for date, subject, starts, ends, room, _syllabus in papers:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20 * mm, y, f"{date:%a %d %b}")
        c.drawString(55 * mm, y, subject)
        c.setFont("Helvetica", 10)
        c.drawRightString(w - 20 * mm, y, f"{starts:%I:%M}–{ends:%I:%M %p} · {room}")
        y -= 8 * mm
    y -= 12 * mm
    c.setStrokeColor(LINE)
    c.line(w - 80 * mm, y, w - 20 * mm, y)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 20 * mm, y - 5 * mm, "Invigilator's signature, each paper")
    c.showPage()
    c.save()
    return buf.getvalue()
