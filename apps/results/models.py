from django.db import models

from apps.core.models import SchoolScopedModel


class Exam(SchoolScopedModel):
    class_group = models.ForeignKey("academics.ClassGroup", on_delete=models.CASCADE, related_name="exams")
    name = models.CharField(max_length=60)
    held_on = models.DateField()
    is_published = models.BooleanField(default=False)

    class Meta:
        ordering = ["held_on"]


class ExamMark(SchoolScopedModel):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="marks")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    subject = models.ForeignKey("academics.Subject", on_delete=models.RESTRICT, related_name="+")
    marks = models.DecimalField(max_digits=5, decimal_places=1)
    max_marks = models.DecimalField(max_digits=5, decimal_places=1, default=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exam", "student", "subject"], name="uniq_exam_mark")]
