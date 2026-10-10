"""The catalogue is not personal data: anyone with ``library.read`` sees every book and copy. Loans belong to
the borrower: with ``self`` scope a member sees their own, with ``child`` scope a parent their children's."""

from __future__ import annotations

from django.db.models import Q

from eduflow.authz.catalog import DataScope
from eduflow.authz.scopes import ScopedResource
from eduflow.people.scoping import children, own_student

from .models import Book, Copy, Loan

books: ScopedResource[Book] = ScopedResource("book", Book)
copies: ScopedResource[Copy] = ScopedResource("book_copy", Copy)
loans: ScopedResource[Loan] = ScopedResource("loan", Loan)

EVERYTHING = Q(pk__isnull=False)
for _scope in (DataScope.SELF, DataScope.CHILD, DataScope.SECTION, DataScope.ASSIGNED):
    books.rule(_scope)(lambda actor: EVERYTHING)
    copies.rule(_scope)(lambda actor: EVERYTHING)

loans.rule(DataScope.SELF)(lambda actor: Q(member=actor.membership) | own_student("student__", actor))
loans.rule(DataScope.CHILD)(lambda actor: children("student__", actor))
