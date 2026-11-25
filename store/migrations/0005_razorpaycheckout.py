import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0004_product_offer_percentage"),
    ]

    operations = [
        migrations.CreateModel(
            name="RazorpayCheckout",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("razorpay_order_id", models.CharField(max_length=100, unique=True)),
                ("razorpay_payment_id", models.CharField(blank=True, max_length=100)),
                ("razorpay_signature", models.CharField(blank=True, max_length=255)),
                ("subtotal", models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(0)])),
                ("delivery_charge", models.DecimalField(decimal_places=2, default=0, max_digits=10, validators=[django.core.validators.MinValueValidator(0)])),
                ("delivery_distance_km", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(0)])),
                ("currency", models.CharField(default="INR", max_length=10)),
                ("shipping_address", models.JSONField()),
                ("notes", models.TextField(blank=True)),
                ("items_snapshot", models.JSONField(default=list)),
                ("status", models.CharField(choices=[("created", "Created"), ("paid", "Paid"), ("failed", "Failed"), ("cancelled", "Cancelled")], default="created", max_length=20)),
                ("failure_reason", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("order", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="razorpay_checkout", to="store.order")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="razorpay_checkouts", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]

