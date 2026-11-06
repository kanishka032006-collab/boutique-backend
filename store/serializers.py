from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import (
    CartItem,
    Category,
    ContactMessage,
    CustomerProfile,
    DeliveryChargeSettings,
    Order,
    OrderItem,
    Product,
    ProductImage,
    Quotation,
    User,
    Wishlist,
)


class UserSummarySerializer(serializers.ModelSerializer):
    name = serializers.CharField(read_only=True)
    phone = serializers.CharField(source="profile.phone", read_only=True, default="")

    class Meta:
        model = User
        fields = ["id", "name", "email", "phone"]


class ShippingAddressSerializer(serializers.Serializer):
    fullName = serializers.CharField(min_length=2, max_length=120)
    phone = serializers.CharField(min_length=7, max_length=30)
    addressLine1 = serializers.CharField(min_length=4, max_length=200)
    addressLine2 = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    city = serializers.CharField(min_length=2, max_length=100)
    state = serializers.CharField(min_length=2, max_length=100)
    postalCode = serializers.CharField(min_length=3, max_length=20)
    country = serializers.CharField(min_length=2, max_length=100, default="India")


class CustomerRegistrationSerializer(serializers.Serializer):
    name = serializers.CharField(min_length=2, max_length=120, trim_whitespace=True)
    email = serializers.EmailField()
    password = serializers.CharField(min_length=10, max_length=128, write_only=True)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    address = ShippingAddressSerializer(required=False)

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        name = validated_data.pop("name")
        phone = validated_data.pop("phone", "")
        address = validated_data.pop("address", {})
        user = User.objects.create_user(
            email=validated_data["email"].lower(),
            password=validated_data["password"],
            first_name=name,
        )
        profile, _ = CustomerProfile.objects.get_or_create(user=user)
        profile.phone = phone
        profile.address = address
        profile.save(update_fields=["phone", "address"])
        return user


class CategorySerializer(serializers.ModelSerializer):
    product_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "description", "product_count"]
        read_only_fields = ["slug", "product_count"]


class ProductImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    primary = serializers.BooleanField(source="is_primary", read_only=True)

    class Meta:
        model = ProductImage
        fields = ["id", "url", "alt_text", "primary"]

    def get_url(self, obj):
        url = obj.url
        if not url or url.startswith(("http://", "https://")):
            return url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default="")
    category_slug = serializers.CharField(source="category.slug", read_only=True, default="")
    category_id = serializers.IntegerField(read_only=True, allow_null=True)
    image_url = serializers.SerializerMethodField()
    is_customizable = serializers.BooleanField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    isCustomizable = serializers.BooleanField(source="is_customizable", write_only=True, required=False)
    isActive = serializers.BooleanField(source="is_active", write_only=True, required=False)
    categoryId = serializers.PrimaryKeyRelatedField(
        source="category", queryset=Category.objects.all(), write_only=True, required=False, allow_null=True
    )
    images = ProductImageSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "name", "slug", "description", "price", "stock", "is_customizable", "is_active",
            "category_id", "categoryId", "category_name", "category_slug", "image_url", "images",
            "isCustomizable", "isActive", "created_at",
        ]
        read_only_fields = ["id", "slug", "created_at"]

    def get_image_url(self, obj):
        image = obj.images.order_by("-is_primary", "id").first()
        if not image:
            return ""
        url = image.url
        if url.startswith(("http://", "https://")):
            return url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class CartItemSerializer(serializers.ModelSerializer):
    product_id = serializers.IntegerField(source="product.id", read_only=True)
    name = serializers.CharField(source="product.name", read_only=True)
    slug = serializers.CharField(source="product.slug", read_only=True)
    price = serializers.DecimalField(source="product.price", max_digits=10, decimal_places=2, read_only=True)
    stock = serializers.IntegerField(source="product.stock", read_only=True)
    is_customizable = serializers.BooleanField(source="product.is_customizable", read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = ["id", "product_id", "name", "slug", "price", "stock", "quantity", "is_customizable", "image_url"]

    def get_image_url(self, obj):
        image = obj.product.images.order_by("-is_primary", "id").first()
        if not image:
            return ""
        url = image.url
        if url.startswith(("http://", "https://")):
            return url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class OrderCustomizationSerializer(serializers.Serializer):
    productId = serializers.IntegerField(min_value=1)
    giftMessage = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    printedName = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")


class OrderCreateSerializer(serializers.Serializer):
    shippingAddress = ShippingAddressSerializer()
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    customizations = OrderCustomizationSerializer(many=True, required=False, default=list)


class DeliveryChargeSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryChargeSettings
        fields = ["enabled", "rate_per_km"]


class OrderItemSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="product_name", read_only=True)
    price = serializers.DecimalField(source="unit_price", max_digits=10, decimal_places=2, read_only=True)
    giftMessage = serializers.CharField(source="gift_message", read_only=True)
    printedName = serializers.CharField(source="printed_name", read_only=True)

    class Meta:
        model = OrderItem
        fields = ["name", "price", "quantity", "giftMessage", "printedName"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    payment_method = serializers.CharField(read_only=True)
    payment_status = serializers.CharField(read_only=True)
    total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    delivery_distance_km = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, allow_null=True)

    class Meta:
        model = Order
        fields = [
            "id", "status", "subtotal", "delivery_charge", "delivery_distance_km", "total", "shipping_address", "notes",
            "payment_method", "payment_status", "created_at", "items",
        ]


class QuotationSerializer(serializers.ModelSerializer):
    fullName = serializers.CharField(source="full_name", min_length=2, max_length=120)
    eventType = serializers.ChoiceField(source="event_type", choices=Quotation.EventType.choices)
    estimatedQuantity = serializers.ChoiceField(source="estimated_quantity", choices=Quotation.QuantityRange.choices)
    budgetRange = serializers.CharField(source="budget_range", max_length=120, required=False, allow_blank=True, default="")
    eventDate = serializers.DateField(source="event_date", required=False, allow_null=True)
    deliveryLocation = serializers.CharField(source="delivery_location", min_length=2, max_length=250)
    additionalDetails = serializers.CharField(source="additional_details", max_length=3000, required=False, allow_blank=True, default="")

    class Meta:
        model = Quotation
        fields = [
            "id", "fullName", "email", "phone", "eventType", "estimatedQuantity", "budgetRange",
            "eventDate", "deliveryLocation", "additionalDetails", "status", "created_at",
        ]
        read_only_fields = ["id", "status", "created_at"]


class ContactMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactMessage
        fields = ["id", "name", "email", "phone", "message", "is_read", "created_at"]
        read_only_fields = ["id", "is_read", "created_at"]


class WishlistSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)

    class Meta:
        model = Wishlist
        fields = ["id", "product", "created_at"]
