import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from rest_framework.test import APITestCase

from .management.commands.seed_catalog import RETURN_GIFT_PRODUCTS
from .models import CartItem, Category, DeliveryChargeSettings, Order, Payment, Product, Quotation, RazorpayCheckout, User
from .views import DeliveryLocationNotFound, _nominatim_geocode


class StoreApiTests(APITestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Wedding Return Gifts", slug="wedding-return-gifts")
        self.product = Product.objects.create(
            category=self.category,
            name="Artisan Return Hamper",
            slug="artisan-return-hamper",
            description="A carefully curated hamper for a special celebration.",
            price="3500.00",
            stock=4,
            is_customizable=True,
        )
        self.customer = User.objects.create_user(email="customer@example.com", password="StrongPassword2026!")
        self.admin = User.objects.create_superuser(email="admin@example.com", password="StrongPassword2026!")

    def test_health_and_return_gift_catalog_are_database_backed(self):
        health = self.client.get("/api/health")
        catalog = self.client.get("/api/products", {"category": self.category.slug})
        search = self.client.get("/api/products", {"search": "wedding return gifts"})

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.data["database"], "connected")
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual([item["name"] for item in catalog.data["products"]], [self.product.name])
        self.assertEqual(search.status_code, 200)
        self.assertEqual([item["name"] for item in search.data["products"]], [self.product.name])

    def test_quote_inquiry_is_validated_and_persisted(self):
        response = self.client.post(
            "/api/quotations",
            {
                "fullName": "A Boutique Customer",
                "email": "event@example.com",
                "phone": "+91 9876543210",
                "eventType": "wedding",
                "estimatedQuantity": "50-100",
                "budgetRange": "₹2,000–₹4,000 per guest",
                "eventDate": "2027-01-15",
                "deliveryLocation": "Chennai",
                "additionalDetails": "Gold packaging, please.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Quotation.objects.count(), 1)
        self.assertEqual(Quotation.objects.get().full_name, "A Boutique Customer")

    def test_seed_catalog_adds_five_illustrated_products_to_each_return_gift_category_idempotently(self):
        return_gift_names = {product[0] for product in RETURN_GIFT_PRODUCTS}
        original_product = {
            "name": self.product.name,
            "description": self.product.description,
            "price": Decimal(self.product.price),
            "stock": self.product.stock,
            "category_id": self.product.category_id,
        }

        call_command("seed_catalog", verbosity=0)
        seeded_products = Product.objects.filter(category__slug__in=[
            "baby-shower-return-gifts",
            "birthday-return-gifts",
            "customized-return-gifts",
            "housewarming-return-gifts",
            "traditional-return-gifts",
            "wedding-return-gifts",
        ]).filter(name__in=return_gift_names)
        first_run_count = seeded_products.count()

        self.assertEqual(first_run_count, 30)
        for slug in (
            "baby-shower-return-gifts",
            "birthday-return-gifts",
            "customized-return-gifts",
            "housewarming-return-gifts",
            "traditional-return-gifts",
            "wedding-return-gifts",
        ):
            category_products = Product.objects.filter(category__slug=slug, name__in=return_gift_names)
            self.assertEqual(category_products.count(), 5, slug)
            self.assertTrue(all(product.images.exists() for product in category_products), slug)

        self.product.refresh_from_db()
        self.assertEqual(
            {
                "name": self.product.name,
                "description": self.product.description,
                "price": self.product.price,
                "stock": self.product.stock,
                "category_id": self.product.category_id,
            },
            original_product,
        )
        self.assertEqual(Category.objects.filter(slug="wedding-return-gifts").count(), 1)

        call_command("seed_catalog", verbosity=0)
        self.assertEqual(seeded_products.count(), first_run_count)

    def test_checkout_decrements_stock_and_records_customization_and_payment(self):
        self.client.force_authenticate(self.customer)
        self.client.post("/api/cart/items", {"productId": self.product.id, "quantity": 2}, format="json")
        response = self.client.post(
            "/api/orders/checkout",
            {
                "shippingAddress": {
                    "fullName": "A Boutique Customer",
                    "phone": "+91 9876543210",
                    "addressLine1": "12 Market Road",
                    "city": "Chennai",
                    "state": "Tamil Nadu",
                    "postalCode": "600001",
                    "country": "India",
                },
                "customizations": [{"productId": self.product.id, "giftMessage": "With thanks", "printedName": "Vedha"}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(user=self.customer)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 2)
        self.assertEqual(order.items.get().gift_message, "With thanks")
        self.assertEqual(order.items.get().printed_name, "Vedha")
        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.amount, order.total)
        self.assertEqual(payment.method, "cash_on_delivery")
        self.assertEqual(order.delivery_charge, Decimal("0.00"))
        self.assertIsNone(order.delivery_distance_km)
        self.assertEqual(self.client.get("/api/orders").data["orders"][0]["id"], order.id)

    def test_product_offer_is_editable_displayed_and_applied_to_checkout(self):
        self.client.force_authenticate(self.admin)
        updated = self.client.patch(
            f"/api/admin/products/{self.product.id}",
            {"offerPercentage": 20},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["product"]["offer_percentage"], 20)
        self.assertEqual(updated.data["product"]["final_price"], "2800.00")

        self.client.force_authenticate(self.customer)
        catalog_product = self.client.get("/api/products").data["products"][0]
        self.assertEqual(catalog_product["offer_percentage"], 20)
        self.assertEqual(catalog_product["final_price"], "2800.00")
        self.client.post("/api/cart/items", {"productId": self.product.id, "quantity": 2}, format="json")
        cart_item = self.client.get("/api/cart").data["items"][0]
        self.assertEqual(cart_item["price"], "2800.00")
        self.assertEqual(cart_item["original_price"], "3500.00")

        response = self.client.post(
            "/api/orders/checkout",
            {
                "shippingAddress": {
                    "fullName": "A Boutique Customer",
                    "phone": "+91 9876543210",
                    "addressLine1": "12 Market Road",
                    "city": "Chennai",
                    "state": "Tamil Nadu",
                    "postalCode": "600001",
                    "country": "India",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(user=self.customer)
        self.assertEqual(order.subtotal, Decimal("5600.00"))
        self.assertEqual(order.items.get().unit_price, Decimal("2800.00"))

    def test_product_offer_percentage_must_be_between_zero_and_one_hundred(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            f"/api/admin/products/{self.product.id}",
            {"offerPercentage": 101},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.offer_percentage, 0)

    def test_customer_can_cancel_pending_order_once_and_stock_is_restored(self):
        self.client.force_authenticate(self.customer)
        self.client.post("/api/cart/items", {"productId": self.product.id, "quantity": 2}, format="json")
        checkout = self.client.post(
            "/api/orders/checkout",
            {
                "shippingAddress": {
                    "fullName": "A Boutique Customer",
                    "phone": "+91 9876543210",
                    "addressLine1": "12 Market Road",
                    "city": "Chennai",
                    "state": "Tamil Nadu",
                    "postalCode": "600001",
                    "country": "India",
                },
            },
            format="json",
        )
        self.assertEqual(checkout.status_code, 201, checkout.data)
        order_id = checkout.data["order"]["id"]
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 2)

        cancelled = self.client.post(f"/api/orders/{order_id}/cancel")

        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["order"]["status"], Order.Status.CANCELLED)
        self.assertEqual(cancelled.data["order"]["payment_status"], Order.PaymentStatus.FAILED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 4)
        self.assertEqual(Payment.objects.get(order_id=order_id).status, Payment.Status.FAILED)

        repeated = self.client.post(f"/api/orders/{order_id}/cancel")
        self.assertEqual(repeated.status_code, 409)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 4)

    def test_customer_cannot_cancel_another_customers_order(self):
        other_customer = User.objects.create_user(email="other@example.com", password="OtherCustomerPass2026!")
        order = Order.objects.create(
            user=other_customer,
            subtotal="3500.00",
            shipping_address={"fullName": "Other Customer"},
        )
        self.client.force_authenticate(self.customer)

        response = self.client.post(f"/api/orders/{order.id}/cancel")

        self.assertEqual(response.status_code, 404)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_delivery_settings_default_to_one_rupee_and_are_admin_only(self):
        denied = self.client.get("/api/admin/delivery-charge-settings")
        self.assertEqual(denied.status_code, 401)

        self.client.force_authenticate(self.admin)
        default_response = self.client.get("/api/admin/delivery-charge-settings")
        self.assertEqual(default_response.status_code, 200)
        self.assertFalse(default_response.data["enabled"])
        self.assertEqual(default_response.data["rate_per_km"], "1.00")
        self.assertEqual(default_response.data["distance_provider"], "OpenStreetMap + OSRM")
        self.assertTrue(default_response.data["distance_service_configured"])

        saved = self.client.put(
            "/api/admin/delivery-charge-settings",
            {"enabled": True, "rate_per_km": "2.50"},
            format="json",
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.data["enabled"])
        self.assertEqual(saved.data["rate_per_km"], "2.50")
        self.assertEqual(DeliveryChargeSettings.get_solo().rate_per_km, Decimal("2.50"))

    @patch("store.views._driving_distance_meters", return_value=15421)
    def test_delivery_quote_calculates_distance_and_charge_from_admin_rate(self, get_distance):
        self.client.force_authenticate(self.customer)
        DeliveryChargeSettings.objects.create(pk=1, enabled=True, rate_per_km=Decimal("1.25"))

        response = self.client.post(
            "/api/delivery/quote",
            {
                "shippingAddress": {
                    "fullName": "A Boutique Customer",
                    "phone": "+91 9876543210",
                    "addressLine1": "12 Market Road",
                    "city": "Chennai",
                    "state": "Tamil Nadu",
                    "postalCode": "600001",
                    "country": "India",
                },
                "distance_km": 1,
                "cod_charge": 0,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["distance_km"], Decimal("15.42"))
        self.assertEqual(response.data["rate_per_km"], Decimal("1.25"))
        self.assertEqual(response.data["cod_charge"], Decimal("19.28"))
        get_distance.assert_called_once_with(
            "12 Market Road, Chennai, Tamil Nadu, 600001, India",
            "600001, Chennai, Tamil Nadu, India",
        )

    @patch("store.views._driving_distance_meters", return_value=15421)
    def test_checkout_saves_server_calculated_distance_charge_and_payment_total(self, get_distance):
        self.client.force_authenticate(self.customer)
        self.client.post("/api/cart/items", {"productId": self.product.id, "quantity": 1}, format="json")
        self.client.force_authenticate(self.admin)
        settings_response = self.client.put(
            "/api/admin/delivery-charge-settings",
            {"enabled": True, "rate_per_km": "1.25"},
            format="json",
        )
        self.assertEqual(settings_response.status_code, 200, settings_response.data)
        self.client.force_authenticate(self.customer)
        address = {
            "fullName": "A Boutique Customer",
            "phone": "+91 9876543210",
            "addressLine1": "12 Market Road",
            "city": "Chennai",
            "state": "Tamil Nadu",
            "postalCode": "600001",
            "country": "India",
        }
        quote_response = self.client.post("/api/delivery/quote", {"shippingAddress": address}, format="json")
        self.assertEqual(quote_response.status_code, 200, quote_response.data)
        self.assertEqual(quote_response.data["cod_charge"], Decimal("19.28"))

        response = self.client.post(
            "/api/orders/checkout",
            {"shippingAddress": address},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(user=self.customer)
        self.assertEqual(order.delivery_distance_km, Decimal("15.42"))
        self.assertEqual(order.delivery_charge, Decimal("19.28"))
        self.assertEqual(response.data["order"]["delivery_distance_km"], "15.42")
        self.assertEqual(Payment.objects.get(order=order).amount, Decimal("3519.28"))
        self.assertEqual(get_distance.call_count, 2)

    @patch("store.views._nominatim_lookup", side_effect=[DeliveryLocationNotFound(), (10.4, 77.9)])
    def test_geocoding_falls_back_to_the_postal_locality_when_full_address_is_unlisted(self, lookup):
        location = _nominatim_geocode("unlisted house address", "624005, Dindigul, Tamil Nadu, India")

        self.assertEqual(location, (10.4, 77.9))
        lookup.assert_any_call("unlisted house address")
        lookup.assert_any_call("624005, Dindigul, Tamil Nadu, India")

    def test_admin_routes_require_staff_and_allow_staff_catalog_management(self):
        denied = self.client.get("/api/admin/overview")
        self.assertEqual(denied.status_code, 401)

        self.client.force_authenticate(self.admin)
        allowed = self.client.get("/api/admin/overview")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data["metrics"]["active_products"], 1)

    def test_admin_can_delete_product_image_and_primary_is_reassigned(self):
        primary = self.product.images.create(image_url="https://example.com/primary.jpg", is_primary=True)
        secondary = self.product.images.create(image_url="https://example.com/secondary.jpg")
        self.client.force_authenticate(self.admin)

        response = self.client.delete(f"/api/admin/products/{self.product.id}/images/{primary.id}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.product.images.filter(pk=primary.id).exists())
        self.assertEqual(response.data["images"][0]["id"], secondary.id)
        secondary.refresh_from_db()
        self.assertTrue(secondary.is_primary)

    def test_customer_cannot_delete_product_image(self):
        image = self.product.images.create(image_url="https://example.com/primary.jpg", is_primary=True)
        self.client.force_authenticate(self.customer)

        response = self.client.delete(f"/api/admin/products/{self.product.id}/images/{image.id}")

        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.product.images.filter(pk=image.id).exists())

    def test_registration_creates_customer_and_returns_tokens(self):
        response = self.client.post(
            "/api/auth/register",
            {
                "name": "New Customer",
                "email": "new@example.com",
                "phone": "+91 9876543210",
                "password": "NewCustomerPass2026!",
                "address": {
                    "fullName": "New Customer",
                    "phone": "+91 9876543210",
                    "addressLine1": "12 Market Road",
                    "addressLine2": "Town Centre",
                    "city": "Chennai",
                    "state": "Tamil Nadu",
                    "postalCode": "600001",
                    "country": "India",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("token", response.data)
        self.assertTrue(User.objects.get(email="new@example.com").check_password("NewCustomerPass2026!"))
        self.assertEqual(
            User.objects.get(email="new@example.com").profile.address["addressLine2"],
            "Town Centre",
        )

    def test_domain_settings_support_production_frontend_and_backend_domains(self):
        self.assertIn("api.vedhikareturngift.com", settings.ALLOWED_HOSTS)
        self.assertIn("https://vedhikareturngift.com", settings.CORS_ALLOWED_ORIGINS)
        self.assertIn("https://www.vedhikareturngift.com", settings.CORS_ALLOWED_ORIGINS)
        self.assertIn("https://api.vedhikareturngift.com", settings.CSRF_TRUSTED_ORIGINS)
        self.assertIn("https://vedhikareturngift.com", settings.CSRF_TRUSTED_ORIGINS)
        self.assertEqual(settings.FRONTEND_URL, "https://vedhikareturngift.com")
        self.assertEqual(settings.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_forgot_password_email_uses_production_frontend_domain(self):
        mail.outbox.clear()
        response = self.client.post(
            "/api/auth/forgot-password",
            {"email": self.customer.email},
            format="json",
            HTTP_HOST="api.vedhikareturngift.com",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("https://vedhikareturngift.com/reset-password/", mail.outbox[0].body)

    def test_products_endpoint_returns_cors_headers_and_https_media_urls_behind_proxy(self):
        self.product.images.create(image="products/sample.jpg", is_primary=True)
        response = self.client.get(
            "/api/products",
            HTTP_HOST="api.vedhikareturngift.com",
            HTTP_ORIGIN="https://vedhikareturngift.com",
            HTTP_X_FORWARDED_PROTO="https",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "https://vedhikareturngift.com")
        self.assertEqual(
            response.data["products"][0]["image_url"],
            "https://api.vedhikareturngift.com/media/products/sample.jpg",
        )

    @override_settings(RAZORPAY_KEY_ID="rzp_test_123", RAZORPAY_KEY_SECRET="secret_test_123")
    def test_payment_config_exposes_key_id_only_without_secret(self):
        response = self.client.get("/api/payments/config")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["razorpay_enabled"])
        self.assertEqual(response.data["razorpay_key_id"], "rzp_test_123")
        self.assertNotIn("secret_test_123", str(response.data))

    @override_settings(RAZORPAY_KEY_ID="rzp_test_123", RAZORPAY_KEY_SECRET="secret_test_123")
    @patch("store.views._razorpay_create_remote_order", return_value={"id": "order_rzp_1001", "currency": "INR"})
    def test_razorpay_checkout_verify_and_cancel_refund_flow(self, mock_create_remote_order):
        self.client.force_authenticate(self.customer)
        self.client.post("/api/cart/items", {"productId": self.product.id, "quantity": 2}, format="json")
        address = {
            "fullName": "A Boutique Customer",
            "phone": "+91 9876543210",
            "addressLine1": "12 Market Road",
            "city": "Chennai",
            "state": "Tamil Nadu",
            "postalCode": "600001",
            "country": "India",
        }

        create_response = self.client.post(
            "/api/payments/razorpay/order",
            {
                "shippingAddress": address,
                "customizations": [{"productId": self.product.id, "giftMessage": "Warm wishes", "printedName": "Vedha"}],
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, 201, create_response.data)
        self.assertEqual(create_response.data["orderId"], "order_rzp_1001")
        self.assertEqual(create_response.data["amount"], 700000)
        mock_create_remote_order.assert_called_once()
        # Stock and cart remain untouched until payment verification succeeds.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 4)
        self.assertEqual(CartItem.objects.filter(cart__user=self.customer).count(), 1)

        bad_verify = self.client.post(
            "/api/payments/razorpay/verify",
            {
                "razorpay_order_id": "order_rzp_1001",
                "razorpay_payment_id": "pay_rzp_9001",
                "razorpay_signature": "invalid_signature",
            },
            format="json",
        )
        self.assertEqual(bad_verify.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

        signature = hmac.new(
            b"secret_test_123",
            b"order_rzp_1001|pay_rzp_9001",
            hashlib.sha256,
        ).hexdigest()
        verify_response = self.client.post(
            "/api/payments/razorpay/verify",
            {
                "razorpay_order_id": "order_rzp_1001",
                "razorpay_payment_id": "pay_rzp_9001",
                "razorpay_signature": signature,
            },
            format="json",
        )
        self.assertEqual(verify_response.status_code, 201, verify_response.data)
        order = Order.objects.get(pk=verify_response.data["order"]["id"])
        self.assertEqual(order.payment_method, "razorpay")
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertEqual(Payment.objects.get(order=order).provider_reference, "pay_rzp_9001")
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 2)
        self.assertEqual(CartItem.objects.filter(cart__user=self.customer).count(), 0)

        # Verification replay is idempotent and does not deduct stock twice.
        replay_response = self.client.post(
            "/api/payments/razorpay/verify",
            {
                "razorpay_order_id": "order_rzp_1001",
                "razorpay_payment_id": "pay_rzp_9001",
                "razorpay_signature": signature,
            },
            format="json",
        )
        self.assertEqual(replay_response.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 2)

        cancel_response = self.client.post(f"/api/orders/{order.id}/cancel")
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancel_response.data["order"]["payment_status"], Order.PaymentStatus.REFUNDED)
        self.assertEqual(Payment.objects.get(order=order).status, Payment.Status.REFUNDED)

    @override_settings(
        RAZORPAY_KEY_ID="rzp_test_123",
        RAZORPAY_KEY_SECRET="secret_test_123",
        RAZORPAY_WEBHOOK_SECRET="whsec_test_123",
    )
    def test_razorpay_webhook_finalizes_captured_payment(self):
        checkout = RazorpayCheckout.objects.create(
            user=self.customer,
            razorpay_order_id="order_rzp_wh_1",
            subtotal=Decimal("3500.00"),
            delivery_charge=Decimal("0.00"),
            amount=Decimal("3500.00"),
            shipping_address={
                "fullName": "A Boutique Customer",
                "phone": "+91 9876543210",
                "addressLine1": "12 Market Road",
                "city": "Chennai",
                "state": "Tamil Nadu",
                "postalCode": "600001",
                "country": "India",
            },
            items_snapshot=[
                {
                    "product_id": self.product.id,
                    "product_name": self.product.name,
                    "unit_price": "3500.00",
                    "quantity": 1,
                    "gift_message": "",
                    "printed_name": "",
                }
            ],
        )
        body = json.dumps(
            {
                "event": "payment.captured",
                "payload": {"payment": {"entity": {"id": "pay_rzp_wh_1", "order_id": "order_rzp_wh_1"}}},
            }
        ).encode("utf-8")
        webhook_sig = hmac.new(b"whsec_test_123", body, hashlib.sha256).hexdigest()

        response = self.client.post(
            "/api/payments/razorpay/webhook",
            data=body,
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE=webhook_sig,
        )

        self.assertEqual(response.status_code, 200)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, RazorpayCheckout.Status.PAID)
        self.assertIsNotNone(checkout.order)
        self.assertEqual(checkout.order.payment_status, Order.PaymentStatus.PAID)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
