from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    ROLE_STUDENT  = 'student'
    ROLE_TEACHER  = 'teacher'
    ROLE_CHOICES  = [(ROLE_STUDENT, 'Студент'), (ROLE_TEACHER, 'Преподаватель')]

    user      = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role      = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_STUDENT)
    full_name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_teacher(self):
        return self.role == self.ROLE_TEACHER

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"
