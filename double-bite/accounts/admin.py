from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import DeliveryAddress, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('email', 'role', 'phone', 'is_staff', 'is_active', 'created_at')
    list_filter = ('role', 'is_staff', 'is_active', 'created_at')
    search_fields = ('email', 'phone', 'first_name', 'last_name')
    ordering = ('email',)
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Персональні дані', {'fields': ('first_name', 'last_name', 'phone')}),
        ('Права доступу', {
            'fields': (
                'role',
                'is_active',
                'is_staff',
                'is_superuser',
                'groups',
                'user_permissions',
            )
        }),
        ('Важливі дати', {'fields': ('last_login', 'created_at')}),
    )
    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': ('email', 'password1', 'password2', 'role', 'phone'),
            },
        ),
    )
    readonly_fields = ('created_at',)


@admin.register(DeliveryAddress)
class DeliveryAddressAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'title',
        'city',
        'street',
        'building',
        'apartment',
        'is_default',
        'created_at',
    )
    list_filter = ('city', 'is_default', 'created_at')
    search_fields = ('user__email', 'street', 'city', 'title')
    list_select_related = ('user',)
