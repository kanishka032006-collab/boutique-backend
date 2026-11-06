from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Cart, CartItem, Category, ContactMessage, CustomerProfile, Order, OrderItem, Payment, Product, ProductImage, Quotation, User, Wishlist


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2", "is_staff", "is_superuser")}),)


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price", "stock", "is_active", "is_customizable")
    list_filter = ("category", "is_active", "is_customizable")
    search_fields = ("name", "description", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [ProductImageInline]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "subtotal", "delivery_charge", "payment_status", "created_at")
    list_filter = ("status", "payment_status", "created_at")
    search_fields = ("id", "user__email")
    inlines = []


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "event_type", "estimated_quantity", "status", "created_at")
    list_filter = ("status", "event_type", "created_at")
    search_fields = ("full_name", "email", "phone", "delivery_location")


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "is_read", "created_at")
    list_filter = ("is_read", "created_at")
    search_fields = ("name", "email", "message")


admin.site.register(CustomerProfile)
admin.site.register(Cart)
admin.site.register(CartItem)
admin.site.register(OrderItem)
admin.site.register(Payment)
admin.site.register(Wishlist)
admin.site.site_header = "VEDHIKA BRAND Administration"
admin.site.site_title = "VEDHIKA BRAND Admin"
admin.site.index_title = "Boutique management"
