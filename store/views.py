import io
import json
import logging
import threading
import time
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    Cart,
    CartItem,
    Category,
    ContactMessage,
    CustomerProfile,
    DeliveryChargeSettings,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductImage,
    Quotation,
    User,
    Wishlist,
)
from .serializers import (
    CartItemSerializer,
    CategorySerializer,
    ContactMessageSerializer,
    CustomerRegistrationSerializer,
    DeliveryChargeSettingsSerializer,
    OrderCreateSerializer,
    OrderSerializer,
    ProductImageSerializer,
    ProductSerializer,
    QuotationSerializer,
    ShippingAddressSerializer,
    UserSummarySerializer,
    WishlistSerializer,
)

logger = logging.getLogger(__name__)


class DeliveryDistanceError(Exception):
    pass


class DeliveryLocationNotFound(DeliveryDistanceError):
    pass


_nominatim_lock = threading.Lock()
_last_nominatim_request = 0.0


def _google_maps_json(url, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Content-Type": "application/json"} if data else {}
    request_headers.update(headers or {})
    request = Request(url, data=data, headers=request_headers)
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise DeliveryDistanceError("Google Maps could not calculate this delivery distance.") from error
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryDistanceError("Google Maps is temporarily unavailable. Please try again.") from error


def _google_geocode(address, api_key):
    query = urlencode({"address": address, "key": api_key})
    result = _google_maps_json(f"https://maps.googleapis.com/maps/api/geocode/json?{query}")
    if result.get("status") != "OK" or not result.get("results"):
        logger.warning("Google geocoding returned status %s.", result.get("status", "UNKNOWN"))
        raise DeliveryDistanceError("Google Maps could not find a route for this address. Check the delivery address.")
    try:
        location = result["results"][0]["geometry"]["location"]
        latitude, longitude = location["lat"], location["lng"]
    except (IndexError, KeyError, TypeError) as error:
        raise DeliveryDistanceError("Google Maps returned an incomplete location for this address.") from error
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise DeliveryDistanceError("Google Maps returned an invalid location for this address.")
    return {"latitude": latitude, "longitude": longitude}


def _google_driving_distance_meters(destination_address):
    api_key = settings.GOOGLE_MAPS_API_KEY
    if not api_key:
        raise DeliveryDistanceError("Distance-based COD is enabled, but the Google Maps API key is not configured.")
    origin = _google_geocode(settings.DELIVERY_STORE_ADDRESS, api_key)
    destination = _google_geocode(destination_address, api_key)
    result = _google_maps_json(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        {
            "origin": {"location": {"latLng": {"latitude": origin["latitude"], "longitude": origin["longitude"]}}},
            "destination": {"location": {"latLng": {"latitude": destination["latitude"], "longitude": destination["longitude"]}}},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_UNAWARE",
            "languageCode": "en-IN",
            "units": "METRIC",
        },
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.distanceMeters",
        },
    )
    routes = result.get("routes", [])
    if not routes or "distanceMeters" not in routes[0]:
        raise DeliveryDistanceError("Google Maps could not calculate this delivery distance. Check the delivery address.")
    return int(routes[0]["distanceMeters"])


@lru_cache(maxsize=256)
def _nominatim_lookup(address):
    global _last_nominatim_request
    with _nominatim_lock:
        delay = 1.1 - (time.monotonic() - _last_nominatim_request)
        if delay > 0:
            time.sleep(delay)
        query = urlencode({
            "q": address,
            "format": "jsonv2",
            "limit": 1,
            "countrycodes": "in",
            "addressdetails": 1,
        })
        _last_nominatim_request = time.monotonic()
    request = Request(
        f"https://nominatim.openstreetmap.org/search?{query}",
        headers={"User-Agent": "VedhikaBoutique/1.0 (delivery distance quotes)"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            results = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise DeliveryDistanceError("OpenStreetMap could not find this delivery location. Please try again.") from error
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryDistanceError("The distance service is temporarily unavailable. Please try again.") from error
    if not results:
        raise DeliveryLocationNotFound("We could not find this delivery address.")
    try:
        latitude = float(results[0]["lat"])
        longitude = float(results[0]["lon"])
        country_code = results[0]["address"]["country_code"]
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise DeliveryDistanceError("The distance service returned an incomplete location.") from error
    if country_code != "in" or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise DeliveryDistanceError("The distance service returned an invalid delivery location.")
    return latitude, longitude


def _nominatim_geocode(address, fallback_address=None):
    try:
        return _nominatim_lookup(address)
    except DeliveryLocationNotFound:
        if not fallback_address or fallback_address.strip() == address.strip():
            raise DeliveryDistanceError("We could not find this delivery address. Please check the address and PIN code.")
        logger.info("Using a broader locality lookup for an address not indexed by OpenStreetMap.")
        try:
            return _nominatim_lookup(fallback_address)
        except DeliveryLocationNotFound as error:
            raise DeliveryDistanceError("We could not find this delivery address. Please check the address and PIN code.") from error


def _osm_driving_distance_meters(destination_address, destination_fallback_address=None):
    origin = _nominatim_geocode(
        settings.DELIVERY_STORE_ADDRESS,
        settings.DELIVERY_STORE_FALLBACK_ADDRESS,
    )
    destination = _nominatim_geocode(
        destination_address,
        destination_fallback_address,
    )
    coordinates = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
    request = Request(
        f"https://router.project-osrm.org/route/v1/driving/{coordinates}?overview=false&steps=false",
        headers={"User-Agent": "VedhikaBoutique/1.0 (delivery distance quotes)"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise DeliveryDistanceError("The driving-distance service could not calculate this route.") from error
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryDistanceError("The driving-distance service is temporarily unavailable. Please try again.") from error
    routes = result.get("routes", [])
    if result.get("code") != "Ok" or not routes or "distance" not in routes[0]:
        raise DeliveryDistanceError("No driving route was found for this delivery address. Check the address and PIN code.")
    return int(routes[0]["distance"])


def _driving_distance_meters(destination_address, destination_fallback_address=None):
    if settings.GOOGLE_MAPS_API_KEY:
        return _google_driving_distance_meters(destination_address)
    return _osm_driving_distance_meters(destination_address, destination_fallback_address)


def _delivery_quote(address, charge_settings=None):
    charge_settings = charge_settings or DeliveryChargeSettings.get_solo()
    if not charge_settings.enabled:
        return {
            "enabled": False,
            "rate_per_km": charge_settings.rate_per_km,
            "distance_km": None,
            "cod_charge": Decimal("0.00"),
        }
    destination = ", ".join(
        part
        for part in (
            address["addressLine1"],
            address.get("addressLine2", ""),
            address["city"],
            address["state"],
            address["postalCode"],
            address.get("country", "India"),
        )
        if part
    )
    destination_fallback = ", ".join(
        part
        for part in (
            address["postalCode"],
            address["city"],
            address["state"],
            address.get("country", "India"),
        )
        if part
    )
    meters = _driving_distance_meters(destination, destination_fallback)
    distance_km = (Decimal(meters) / Decimal("1000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cod_charge = (distance_km * charge_settings.rate_per_km).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "enabled": True,
        "rate_per_km": charge_settings.rate_per_km,
        "distance_km": distance_km,
        "cod_charge": cod_charge,
    }


def _session_response(user, http_status=status.HTTP_200_OK):
    refresh = RefreshToken.for_user(user)
    if user.is_staff:
        refresh["role"] = "admin"
    else:
        refresh["role"] = "customer"
    return Response(
        {
            "token": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserSummarySerializer(user).data,
        },
        status=http_status,
    )


def _product_queryset():
    return Product.objects.filter(is_active=True).select_related("category").prefetch_related("images")


def _admin_product_queryset():
    return Product.objects.select_related("category").prefetch_related("images").order_by("-created_at")


def _categories_queryset():
    return Category.objects.annotate(product_count=Count("products", filter=Q(products__is_active=True))).order_by("name")


def _serialize_product(product, request):
    return ProductSerializer(product, context={"request": request}).data


def _cart_items(user):
    cart, _ = Cart.objects.get_or_create(user=user)
    return cart.items.select_related("product", "product__category").prefetch_related("product__images").order_by("id")


def _admin_only(request):
    if not request.user.is_staff:
        return Response({"error": "Admin access is required."}, status=status.HTTP_403_FORBIDDEN)
    return None


def _xlsx_response(filename, title, headers, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title[:31]
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _pdf_response(filename, title, lines):
    output = io.BytesIO()
    page = canvas.Canvas(output, pagesize=A4)
    page.setTitle(title)
    width, height = A4
    y = height - 56
    page.setFont("Helvetica-Bold", 18)
    page.setFillColorRGB(0.85, 0.68, 0.0)
    page.drawString(48, y, title)
    y -= 35
    page.setFont("Helvetica", 9)
    page.setFillColorRGB(0.15, 0.15, 0.15)
    for line in lines:
        text = str(line).encode("latin-1", "replace").decode("latin-1")
        for offset in range(0, max(1, len(text)), 105):
            if y < 55:
                page.showPage()
                y = height - 56
                page.setFont("Helvetica", 9)
            page.drawString(48, y, text[offset : offset + 105])
            y -= 14
        y -= 7
    page.save()
    response = HttpResponse(output.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return Response({"status": "ok", "database": "connected"})


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    serializer = CustomerRegistrationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if User.objects.filter(email__iexact=serializer.validated_data["email"]).exists():
        return Response({"error": "An account with this email already exists."}, status=status.HTTP_409_CONFLICT)
    try:
        user = serializer.save()
    except IntegrityError:
        return Response({"error": "An account with this email already exists."}, status=status.HTTP_409_CONFLICT)
    return _session_response(user, status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    email = str(request.data.get("email", "")).strip().lower()
    password = request.data.get("password", "")
    if not email or not isinstance(password, str) or not password:
        return Response({"error": "Email and password are required."}, status=status.HTTP_400_BAD_REQUEST)
    user = authenticate(request, email=email, password=password)
    is_admin_login = request.path.endswith("/admin/login")
    if not user or not user.is_active or (is_admin_login != user.is_staff):
        return Response({"error": "Email or password is incorrect."}, status=status.HTTP_401_UNAUTHORIZED)
    return _session_response(user)


@api_view(["POST"])
@permission_classes([AllowAny])
def forgot_password(request):
    email = str(request.data.get("email", "")).strip()
    if not email:
        return Response({"error": "Email address is required."}, status=status.HTTP_400_BAD_REQUEST)
    form = PasswordResetForm({"email": email})
    if not form.is_valid():
        return Response({"error": "Enter a valid email address."}, status=status.HTTP_400_BAD_REQUEST)
    form.save(
        request=request,
        use_https=urlparse(settings.FRONTEND_URL).scheme == "https",
        domain_override=urlparse(settings.FRONTEND_URL).netloc,
        email_template_name="registration/password_reset_email.html",
        subject_template_name="registration/password_reset_subject.txt",
    )
    return Response({"message": "If an account exists for that address, password reset instructions have been sent."})


@api_view(["POST"])
@permission_classes([AllowAny])
def reset_password(request):
    uid = request.data.get("uid")
    token = request.data.get("token")
    password = request.data.get("newPassword")
    if not uid or not token or not isinstance(password, str):
        return Response({"error": "A valid reset link and new password are required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        user_id = urlsafe_base64_decode(uid).decode()
        user = User.objects.get(pk=user_id, is_active=True)
    except (ValueError, TypeError, UnicodeDecodeError, User.DoesNotExist):
        return Response({"error": "This password reset link is invalid or has expired."}, status=status.HTTP_400_BAD_REQUEST)
    if not default_token_generator.check_token(user, token):
        return Response({"error": "This password reset link is invalid or has expired."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_password(password, user=user)
    except DjangoValidationError as error:
        return Response({"error": "Please choose a stronger password.", "details": error.messages}, status=status.HTTP_400_BAD_REQUEST)
    user.set_password(password)
    user.save(update_fields=["password"])
    return Response({"message": "Your password has been updated. Please sign in."})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def profile(request):
    user = request.user
    profile, _ = CustomerProfile.objects.get_or_create(user=user)
    if request.method == "PATCH":
        name = request.data.get("name")
        phone = request.data.get("phone")
        if name is not None:
            if not isinstance(name, str) or not 2 <= len(name.strip()) <= 120:
                return Response({"error": "Name must be between 2 and 120 characters."}, status=status.HTTP_400_BAD_REQUEST)
            user.first_name = name.strip()
            user.last_name = ""
            user.save(update_fields=["first_name", "last_name"])
        if phone is not None:
            if not isinstance(phone, str) or len(phone) > 30:
                return Response({"error": "Phone number must be 30 characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)
            profile.phone = phone.strip()
            profile.save(update_fields=["phone"])
        address = request.data.get("address")
        if address is not None:
            if not isinstance(address, dict):
                return Response({"error": "Address must be an object."}, status=status.HTTP_400_BAD_REQUEST)
            profile.address = address
            profile.save(update_fields=["address"])
    return Response({"user": UserSummarySerializer(user).data, "address": profile.address})


@api_view(["POST"])
@permission_classes([AllowAny])
def contact(request):
    serializer = ContactMessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save(user=request.user if request.user.is_authenticated else None)
    return Response({"message": "Your message has been received. Thank you."}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def quotation(request):
    serializer = QuotationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save(user=request.user if request.user.is_authenticated else None)
    return Response(
        {"message": "Your VEDHIKA BRAND inquiry has been received. Our gifting team will be in touch.", "inquiry": serializer.data},
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def categories(request):
    return Response({"categories": CategorySerializer(_categories_queryset(), many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def products(request):
    queryset = _product_queryset()
    search = request.query_params.get("search", "").strip()
    category_slug = request.query_params.get("category", "").strip()
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(description__icontains=search)
            | Q(category__name__icontains=search)
        )
    if category_slug:
        queryset = queryset.filter(category__slug=category_slug)
    try:
        min_price = request.query_params.get("minPrice")
        max_price = request.query_params.get("maxPrice")
        if min_price not in (None, ""):
            min_price = Decimal(min_price)
            if not min_price.is_finite() or min_price < 0:
                raise InvalidOperation
            queryset = queryset.filter(price__gte=min_price)
        if max_price not in (None, ""):
            max_price = Decimal(max_price)
            if not max_price.is_finite() or max_price < 0:
                raise InvalidOperation
            queryset = queryset.filter(price__lte=max_price)
    except (InvalidOperation, TypeError, ValueError):
        return Response({"error": "Price filters must be valid non-negative numbers."}, status=status.HTTP_400_BAD_REQUEST)
    sort = request.query_params.get("sort", "name")
    ordering = {"price_asc": "price", "price_desc": "-price", "newest": "-created_at", "name": "name"}.get(sort)
    if not ordering:
        return Response({"error": "Unsupported sort order."}, status=status.HTTP_400_BAD_REQUEST)
    queryset = queryset.order_by(ordering)
    try:
        limit = min(100, max(1, int(request.query_params.get("limit", 48))))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        return Response({"error": "Limit and offset must be whole numbers."}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"products": ProductSerializer(queryset[offset : offset + limit], many=True, context={"request": request}).data})


@api_view(["GET"])
@permission_classes([AllowAny])
def product_detail(request, slug):
    product = get_object_or_404(_product_queryset(), slug=slug)
    return Response({"product": _serialize_product(product, request)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cart(request):
    items = _cart_items(request.user)
    serialized = CartItemSerializer(items, many=True, context={"request": request}).data
    return Response({"items": serialized})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cart_add(request):
    try:
        product_id = int(request.data.get("productId"))
        quantity = int(request.data.get("quantity", 1))
    except (TypeError, ValueError):
        return Response({"error": "A valid product and quantity are required."}, status=status.HTTP_400_BAD_REQUEST)
    if quantity < 1 or quantity > 1000:
        return Response({"error": "Quantity must be between 1 and 1000."}, status=status.HTTP_400_BAD_REQUEST)
    product = get_object_or_404(Product, pk=product_id, is_active=True)
    cart_object, _ = Cart.objects.get_or_create(user=request.user)
    item, created = CartItem.objects.get_or_create(cart=cart_object, product=product, defaults={"quantity": quantity})
    if not created:
        item.quantity += quantity
    if item.quantity > product.stock:
        if created:
            item.delete()
        return Response({"error": f"Only {product.stock} item(s) are currently available."}, status=status.HTTP_409_CONFLICT)
    if not created:
        item.save(update_fields=["quantity"])
    return Response(
        {"items": CartItemSerializer(_cart_items(request.user), many=True, context={"request": request}).data},
        status=status.HTTP_201_CREATED,
    )


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def cart_item(request, item_id):
    item = get_object_or_404(CartItem.objects.select_related("product"), pk=item_id, cart__user=request.user)
    if request.method == "DELETE":
        item.delete()
        return Response({"items": CartItemSerializer(_cart_items(request.user), many=True, context={"request": request}).data})
    try:
        quantity = int(request.data.get("quantity"))
    except (TypeError, ValueError):
        return Response({"error": "A valid quantity is required."}, status=status.HTTP_400_BAD_REQUEST)
    if quantity < 1 or quantity > item.product.stock:
        return Response({"error": "Quantity must be at least one and cannot exceed available stock."}, status=status.HTTP_400_BAD_REQUEST)
    item.quantity = quantity
    item.save(update_fields=["quantity"])
    return Response({"items": CartItemSerializer(_cart_items(request.user), many=True, context={"request": request}).data})


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def wishlist(request, product_id=None):
    if request.method == "GET":
        saved = Wishlist.objects.filter(user=request.user, product__is_active=True).select_related("product", "product__category").prefetch_related("product__images")
        return Response({"items": WishlistSerializer(saved, many=True, context={"request": request}).data})
    if request.method == "POST":
        try:
            product_id = int(request.data.get("productId"))
        except (TypeError, ValueError):
            return Response({"error": "A valid product is required."}, status=status.HTTP_400_BAD_REQUEST)
        product = get_object_or_404(Product, pk=product_id, is_active=True)
        Wishlist.objects.get_or_create(user=request.user, product=product)
        saved = Wishlist.objects.filter(user=request.user, product__is_active=True).select_related("product", "product__category").prefetch_related("product__images")
        return Response({"items": WishlistSerializer(saved, many=True, context={"request": request}).data}, status=status.HTTP_201_CREATED)
    get_object_or_404(Wishlist, user=request.user, product_id=product_id).delete()
    saved = Wishlist.objects.filter(user=request.user, product__is_active=True).select_related("product", "product__category").prefetch_related("product__images")
    return Response({"items": WishlistSerializer(saved, many=True, context={"request": request}).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_order(request):
    serializer = OrderCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not CartItem.objects.filter(cart__user=request.user).exists():
        return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        delivery_quote = _delivery_quote(serializer.validated_data["shippingAddress"])
    except DeliveryDistanceError as error:
        return Response({"error": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    customizations = {item["productId"]: item for item in serializer.validated_data.get("customizations", [])}
    try:
        with transaction.atomic():
            Cart.objects.get_or_create(user=request.user)
            cart_object = Cart.objects.select_for_update().get(user=request.user)
            items = list(
                CartItem.objects.select_for_update()
                .filter(cart=cart_object)
                .select_related("product")
                .order_by("id")
            )
            if not items:
                return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)
            if any(not item.product.is_active for item in items):
                return Response({"error": "A product in your cart is no longer available. Remove it before checkout."}, status=status.HTTP_409_CONFLICT)
            cart_product_ids = {item.product_id for item in items}
            if any(product_id not in cart_product_ids for product_id in customizations):
                return Response({"error": "Customization was submitted for a product not in your cart."}, status=status.HTTP_400_BAD_REQUEST)
            subtotal = 0
            for item in items:
                product = Product.objects.select_for_update().get(pk=item.product_id)
                if item.quantity > product.stock:
                    return Response({"error": f"{product.name} no longer has enough stock."}, status=status.HTTP_409_CONFLICT)
                customization = customizations.get(item.product_id)
                if customization and (customization["giftMessage"] or customization["printedName"]) and not product.is_customizable:
                    return Response({"error": f"{product.name} does not support customization."}, status=status.HTTP_400_BAD_REQUEST)
                subtotal += product.price * item.quantity
            order = Order.objects.create(
                user=request.user,
                subtotal=subtotal,
                delivery_charge=delivery_quote["cod_charge"],
                delivery_distance_km=delivery_quote["distance_km"],
                shipping_address=serializer.validated_data["shippingAddress"],
                notes=serializer.validated_data.get("notes", ""),
            )
            Payment.objects.create(order=order, amount=order.total)
            for item in items:
                product = Product.objects.select_for_update().get(pk=item.product_id)
                customization = customizations.get(item.product_id, {})
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    product_name=product.name,
                    unit_price=product.price,
                    quantity=item.quantity,
                    gift_message=customization.get("giftMessage", ""),
                    printed_name=customization.get("printedName", ""),
                )
                product.stock -= item.quantity
                product.save(update_fields=["stock", "updated_at"])
            cart_object.items.all().delete()
    except Cart.DoesNotExist:
        return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)
    order.refresh_from_db()
    return Response({"order": OrderSerializer(order).data}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def delivery_quote(request):
    serializer = ShippingAddressSerializer(data=request.data.get("shippingAddress", {}))
    serializer.is_valid(raise_exception=True)
    try:
        quote = _delivery_quote(serializer.validated_data)
    except DeliveryDistanceError as error:
        return Response({"error": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(quote)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def orders(request):
    queryset = Order.objects.filter(user=request.user).prefetch_related("items")
    return Response({"orders": OrderSerializer(queryset, many=True).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_order(request, order_id):
    with transaction.atomic():
        order = get_object_or_404(
            Order.objects.select_for_update().prefetch_related("items__product"),
            pk=order_id,
            user=request.user,
        )
        if order.status not in (Order.Status.PENDING, Order.Status.CONFIRMED):
            return Response(
                {"error": "Only pending or confirmed orders can be cancelled, and each order can be cancelled only once."},
                status=status.HTTP_409_CONFLICT,
            )
        for item in order.items.all():
            if item.product_id:
                Product.objects.filter(pk=item.product_id).update(stock=F("stock") + item.quantity)
        order.status = Order.Status.CANCELLED
        order.payment_status = Order.PaymentStatus.FAILED
        order.save(update_fields=["status", "payment_status", "updated_at"])
        Payment.objects.filter(order=order, status=Payment.Status.PENDING).update(status=Payment.Status.FAILED)
    order.refresh_from_db()
    return Response({"order": OrderSerializer(order).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def order_payment(request, order_id):
    order = get_object_or_404(Order, pk=order_id, user=request.user)
    payment, _ = Payment.objects.get_or_create(
        order=order,
        defaults={"amount": order.total, "method": order.payment_method, "status": order.payment_status},
    )
    return Response(
        {
            "payment": {
                "method": payment.method,
                "status": payment.status,
                "amount": str(payment.amount),
                "reference": payment.provider_reference,
            }
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_overview(request):
    denied = _admin_only(request)
    if denied:
        return denied
    total_sales = Order.objects.exclude(status=Order.Status.CANCELLED).aggregate(
        amount=Coalesce(Sum(F("subtotal") + F("delivery_charge")), Value(0), output_field=DecimalField(max_digits=12, decimal_places=2))
    )["amount"]
    metrics = {
        "total_orders": Order.objects.count(),
        "total_customers": User.objects.filter(is_staff=False, is_superuser=False).count(),
        "active_products": Product.objects.filter(is_active=True).count(),
        "total_sales": total_sales,
        "open_orders": Order.objects.filter(status__in=[Order.Status.PENDING, Order.Status.CONFIRMED, Order.Status.PROCESSING]).count(),
        "low_stock": Product.objects.filter(is_active=True, stock__lte=5).count(),
        "new_inquiries": Quotation.objects.filter(status=Quotation.Status.NEW).count(),
    }
    recent = Order.objects.select_related("user").order_by("-created_at")[:8]
    return Response(
        {
            "metrics": metrics,
            "recentOrders": [
                {
                    "id": order.id,
                    "status": order.status,
                    "subtotal": str(order.total),
                    "created_at": order.created_at,
                    "customer_name": order.user.name,
                    "customer_email": order.user.email,
                }
                for order in recent
            ],
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def admin_products(request):
    denied = _admin_only(request)
    if denied:
        return denied
    if request.method == "GET":
        return Response({"products": ProductSerializer(_admin_product_queryset(), many=True, context={"request": request}).data})
    serializer = ProductSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    product = serializer.save()
    return Response({"product": _serialize_product(product, request)}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def admin_product_detail(request, product_id):
    denied = _admin_only(request)
    if denied:
        return denied
    product = get_object_or_404(Product.objects.all(), pk=product_id)
    if request.method == "DELETE":
        product.is_active = False
        product.save(update_fields=["is_active", "updated_at"])
        return Response({"message": "Product removed from the storefront."})
    serializer = ProductSerializer(product, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"product": _serialize_product(product, request)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def admin_product_images(request, product_id):
    denied = _admin_only(request)
    if denied:
        return denied
    product = get_object_or_404(Product, pk=product_id)
    files = request.FILES.getlist("images")
    if not files or len(files) > 5:
        return Response({"error": "Choose between one and five product images."}, status=status.HTTP_400_BAD_REQUEST)
    from PIL import Image, UnidentifiedImageError

    for image_file in files:
        if image_file.size > 5 * 1024 * 1024:
            return Response({"error": "Each image must be 5 MB or smaller."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        try:
            with Image.open(image_file) as image:
                image.verify()
        except (UnidentifiedImageError, OSError):
            return Response({"error": "An uploaded file is not a valid image."}, status=status.HTTP_400_BAD_REQUEST)
    is_first = not product.images.exists()
    for index, image_file in enumerate(files):
        ProductImage.objects.create(
            product=product,
            image=image_file,
            alt_text=image_file.name[:200],
            is_primary=is_first and index == 0,
        )
    return Response({"message": "Product images uploaded."}, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def admin_product_image_detail(request, product_id, image_id):
    denied = _admin_only(request)
    if denied:
        return denied
    image = get_object_or_404(ProductImage, pk=image_id, product_id=product_id)
    storage = image.image.storage if image.image else None
    image_name = image.image.name if image.image else ""
    image.delete()

    remaining_images = ProductImage.objects.filter(product_id=product_id)
    if not remaining_images.filter(is_primary=True).exists():
        next_image = remaining_images.order_by("id").first()
        if next_image:
            next_image.is_primary = True
            next_image.save(update_fields=["is_primary"])

    warning = ""
    if storage and image_name and not ProductImage.objects.filter(image=image_name).exists():
        try:
            storage.delete(image_name)
        except OSError:
            logger.exception("Could not remove deleted product image file %s", image_name)
            warning = "The image was removed from the product, but its stored file could not be deleted."

    images = ProductImage.objects.filter(product_id=product_id).order_by("-is_primary", "id")
    return Response({
        "images": ProductImageSerializer(images, many=True, context={"request": request}).data,
        "warning": warning,
    })


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def admin_delivery_charge_settings(request):
    denied = _admin_only(request)
    if denied:
        return denied
    charge_settings = DeliveryChargeSettings.get_solo()
    if request.method == "PUT":
        serializer = DeliveryChargeSettingsSerializer(charge_settings, data=request.data)
        serializer.is_valid(raise_exception=True)
        charge_settings = serializer.save()
    return Response({
        **DeliveryChargeSettingsSerializer(charge_settings).data,
        "distance_service_configured": True,
        "distance_provider": "Google Maps" if settings.GOOGLE_MAPS_API_KEY else "OpenStreetMap + OSRM",
    })


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def admin_categories(request):
    denied = _admin_only(request)
    if denied:
        return denied
    if request.method == "GET":
        return Response({"categories": CategorySerializer(_categories_queryset(), many=True).data})
    serializer = CategorySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    category = serializer.save()
    return Response({"category": CategorySerializer(category).data}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def admin_category_detail(request, category_id):
    denied = _admin_only(request)
    if denied:
        return denied
    category = get_object_or_404(Category, pk=category_id)
    if request.method == "DELETE":
        category.delete()
        return Response({"message": "Category deleted."})
    serializer = CategorySerializer(category, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({"category": CategorySerializer(category).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_customers(request):
    denied = _admin_only(request)
    if denied:
        return denied
    customers = User.objects.filter(is_staff=False, is_superuser=False).prefetch_related("orders", "profile").order_by("-date_joined")
    result = []
    for customer in customers:
        customer_orders = customer.orders.exclude(status=Order.Status.CANCELLED)
        total_spent = customer_orders.aggregate(
            amount=Coalesce(Sum(F("subtotal") + F("delivery_charge")), Value(0), output_field=DecimalField(max_digits=12, decimal_places=2))
        )["amount"]
        result.append(
            {
                "id": customer.id,
                "name": customer.name,
                "email": customer.email,
                "phone": getattr(getattr(customer, "profile", None), "phone", ""),
                "created_at": customer.date_joined,
                "order_count": customer.orders.count(),
                "total_spent": total_spent,
            }
        )
    return Response({"customers": result})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_messages(request):
    denied = _admin_only(request)
    if denied:
        return denied
    return Response({"messages": ContactMessageSerializer(ContactMessage.objects.all(), many=True).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_quotations(request):
    denied = _admin_only(request)
    if denied:
        return denied
    quotations = Quotation.objects.all()
    return Response({"quotations": QuotationSerializer(quotations, many=True).data})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_quotation_status(request, quotation_id):
    denied = _admin_only(request)
    if denied:
        return denied
    quotation = get_object_or_404(Quotation, pk=quotation_id)
    new_status = request.data.get("status")
    if new_status not in Quotation.Status.values:
        return Response({"error": "Choose a valid inquiry status."}, status=status.HTTP_400_BAD_REQUEST)
    quotation.status = new_status
    quotation.save(update_fields=["status"])
    return Response({"quotation": QuotationSerializer(quotation).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_orders(request):
    denied = _admin_only(request)
    if denied:
        return denied
    result = []
    for order in Order.objects.select_related("user").prefetch_related("items").all():
        row = OrderSerializer(order).data
        row.update(
            {
                "customer_name": order.user.name,
                "customer_email": order.user.email,
                "customer_phone": getattr(getattr(order.user, "profile", None), "phone", ""),
            }
        )
        result.append(row)
    return Response({"orders": result})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_order_status(request, order_id):
    denied = _admin_only(request)
    if denied:
        return denied
    new_status = request.data.get("status")
    if new_status not in Order.Status.values:
        return Response({"error": "Choose a valid order status."}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update().prefetch_related("items__product"), pk=order_id)
        if order.status == Order.Status.CANCELLED and new_status != Order.Status.CANCELLED:
            return Response({"error": "A cancelled order cannot be reopened."}, status=status.HTTP_409_CONFLICT)
        if order.status == Order.Status.DELIVERED and new_status == Order.Status.CANCELLED:
            return Response({"error": "A delivered order cannot be cancelled."}, status=status.HTTP_409_CONFLICT)
        if new_status == Order.Status.CANCELLED and order.status != Order.Status.CANCELLED:
            for item in order.items.all():
                if item.product_id:
                    Product.objects.filter(pk=item.product_id).update(stock=F("stock") + item.quantity)
        order.status = new_status
        if new_status == Order.Status.DELIVERED and order.payment_method == "cash_on_delivery":
            order.payment_status = Order.PaymentStatus.PAID
            order.save(update_fields=["status", "payment_status", "updated_at"])
            Payment.objects.filter(order=order).update(status=Payment.Status.PAID)
        elif new_status == Order.Status.CANCELLED:
            order.payment_status = Order.PaymentStatus.FAILED
            order.save(update_fields=["status", "payment_status", "updated_at"])
            Payment.objects.filter(order=order, status=Payment.Status.PENDING).update(status=Payment.Status.FAILED)
        else:
            order.save(update_fields=["status", "updated_at"])
            if new_status == Order.Status.CANCELLED:
                Payment.objects.filter(order=order, status=Payment.Status.PENDING).update(status=Payment.Status.FAILED)
    return Response({"order": {"id": order.id, "status": order.status}})


def _date_filters(request):
    from_date = request.query_params.get("from")
    to_date = request.query_params.get("to")
    try:
        parsed_from = date.fromisoformat(from_date) if from_date else None
        parsed_to = date.fromisoformat(to_date) if to_date else None
    except ValueError:
        return None, None, Response({"error": "Report dates must use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
    if parsed_from and parsed_to and parsed_from > parsed_to:
        return None, None, Response({"error": "The report start date cannot be after its end date."}, status=status.HTTP_400_BAD_REQUEST)
    queryset = Order.objects.exclude(status=Order.Status.CANCELLED)
    if parsed_from:
        queryset = queryset.filter(created_at__date__gte=parsed_from)
    if parsed_to:
        queryset = queryset.filter(created_at__date__lte=parsed_to)
    return queryset, (parsed_from, parsed_to), None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_sales_report(request):
    denied = _admin_only(request)
    if denied:
        return denied
    queryset, dates, error = _date_filters(request)
    if error:
        return error
    result = queryset.aggregate(
        order_count=Count("id"),
        sales=Coalesce(Sum(F("subtotal") + F("delivery_charge")), Value(0), output_field=DecimalField(max_digits=12, decimal_places=2)),
    )
    result["average_order_value"] = result["sales"] / result["order_count"] if result["order_count"] else 0
    return Response({"report": result, "from": dates[0], "to": dates[1]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_orders_export(request, export_format):
    denied = _admin_only(request)
    if denied:
        return denied
    orders_data = Order.objects.select_related("user").order_by("-created_at")
    rows = [
        [
            order.id,
            order.status,
            float(order.total),
            order.created_at.strftime("%Y-%m-%d %H:%M"),
            order.user.name,
            order.user.email,
            getattr(getattr(order.user, "profile", None), "phone", ""),
            str(order.shipping_address),
        ]
        for order in orders_data
    ]
    headers = ["Order ID", "Status", "Total", "Date", "Customer", "Email", "Phone", "Delivery address"]
    if export_format == "xlsx":
        return _xlsx_response("vedhika-orders.xlsx", "Orders", headers, rows)
    lines = [" | ".join(map(str, row)) for row in rows]
    return _pdf_response("vedhika-orders.pdf", "VEDHIKA BRAND Order Details", lines)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_customers_export(request, export_format):
    denied = _admin_only(request)
    if denied:
        return denied
    customers = User.objects.filter(is_staff=False, is_superuser=False).prefetch_related("orders", "profile").order_by("-date_joined")
    headers = ["Customer", "Email", "Phone", "Joined", "Orders", "Total spent"]
    rows = []
    for customer in customers:
        total = customer.orders.exclude(status=Order.Status.CANCELLED).aggregate(
            value=Coalesce(Sum(F("subtotal") + F("delivery_charge")), Value(0), output_field=DecimalField(max_digits=12, decimal_places=2))
        )["value"]
        rows.append(
            [
                customer.name,
                customer.email,
                getattr(getattr(customer, "profile", None), "phone", ""),
                customer.date_joined.strftime("%Y-%m-%d"),
                customer.orders.count(),
                float(total),
            ]
        )
    if export_format == "xlsx":
        return _xlsx_response("vedhika-customers.xlsx", "Customers", headers, rows)
    return _pdf_response("vedhika-customers.pdf", "VEDHIKA BRAND Customer Details", [" | ".join(map(str, row)) for row in rows])


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_sales_pdf(request):
    denied = _admin_only(request)
    if denied:
        return denied
    queryset, dates, error = _date_filters(request)
    if error:
        return error
    result = queryset.aggregate(
        order_count=Count("id"),
        sales=Coalesce(Sum(F("subtotal") + F("delivery_charge")), Value(0), output_field=DecimalField(max_digits=12, decimal_places=2)),
    )
    average = result["sales"] / result["order_count"] if result["order_count"] else 0
    lines = [
        f"Period: {dates[0] or 'All time'} to {dates[1] or timezone.localdate()}",
        f"Orders: {result['order_count']}",
        f"Sales: INR {result['sales']}",
        f"Average order value: INR {average}",
    ]
    return _pdf_response("vedhika-sales-report.pdf", "VEDHIKA BRAND Sales Report", lines)
