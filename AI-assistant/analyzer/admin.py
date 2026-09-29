from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import UserProfile


class ProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name = "Профиль"
    verbose_name_plural = "Профиль"
    fields = ("role", "full_name")


class UserAdmin(BaseUserAdmin):
    inlines = (ProfileInline,)
    list_display  = ("username", "email", "get_role", "is_staff", "date_joined")
    list_filter   = ("profile__role", "is_staff")

    def get_role(self, obj):
        try: return obj.profile.get_role_display()
        except: return "—"
    get_role.short_description = "Роль"


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
admin.site.register(UserProfile)
