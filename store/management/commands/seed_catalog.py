from django.core.management.base import BaseCommand
from django.utils.text import slugify

from store.models import Category, Product, ProductImage


COLLECTIONS = [
    ("Gift Hampers", "gift-hampers", "Artisan gift hampers"),
    ("Wedding Return Gifts", "wedding-return-gifts", "A thoughtful thank-you for wedding guests"),
    ("Birthday Return Gifts", "birthday-return-gifts", "Memorable return gifts for birthday celebrations"),
    ("Baby Shower Return Gifts", "baby-shower-return-gifts", "Thoughtful keepsakes for welcoming a little one"),
    ("Housewarming Return Gifts", "housewarming-return-gifts", "Warm, considered gifts for a new home"),
    ("Traditional Return Gifts", "traditional-return-gifts", "Heritage-inspired gifts for meaningful occasions"),
    ("Heritage Silk Gifts", "heritage-silk-gifts", "Heritage silk products and keepsakes"),
    ("Corporate Gifting", "corporate-gifting", "Premium corporate gifting collections"),
    ("Kanchipuram Sarees", "kanchipuram-sarees", "Authentic handloom Kanchipuram sarees"),
    ("Customized Return Gifts", "customized-return-gifts", "Personalized return gifts for every occasion"),
]

PRODUCTS = [
    ("Artisan Gift Hamper", 5500, "A generous artisan-curated hamper, arranged with heritage treats and a personal finishing touch.", "gift-hampers", "photo-1549465220-1a8b9238cd48"),
    ("Wedding Return Gift", 3500, "A considered wedding keepsake, beautifully presented for the people who made your celebration complete.", "wedding-return-gifts", "photo-1607344645866-009c320b63e0"),
    ("Luxury Silk Gift Box", 7500, "A refined silk gift box celebrating South Indian craft, heirloom textures and thoughtful presentation.", "heritage-silk-gifts", "photo-1513201099705-a9746e1e201f"),
    ("Corporate Gift Collection", 4500, "A polished, customizable gifting collection for teams, clients and distinguished corporate occasions.", "corporate-gifting", "photo-1544816155-12df9643f363"),
    ("Premium Kanchipuram Saree", 12000, "An elegant handloom Kanchipuram-inspired saree collection, selected for its enduring artistry and occasion-ready finish.", "kanchipuram-sarees", "photo-1610030469983-98e550d6193c"),
    ("Customized Celebration Hamper", 6000, "A bespoke celebration hamper with thoughtful personalization and premium artisan details.", "customized-return-gifts", "photo-1549465220-1a8b9238cd48"),
]

RETURN_GIFT_PRODUCTS = [
    ("Manjal Kumkum Box", 299, "A charming brass-finish box for manjal and kumkum, a thoughtful blessing for baby shower guests.", "baby-shower-return-gifts", "photo-1603006905003-be475563bc59"),
    ("Bangles Gift Set", 499, "A colourful pair of traditional glass bangles presented in a keepsake gift box.", "baby-shower-return-gifts", "photo-1611652022419-a9419f74343d"),
    ("Brass Kuthu Vilakku", 1299, "A petite brass kuthu vilakku with classic Tamil detailing, made to brighten a guest's home.", "baby-shower-return-gifts", "photo-1603006905003-be475563bc59"),
    ("Thamboolam Set", 399, "A festive thamboolam set with useful traditional essentials, arranged for a warm and auspicious welcome.", "baby-shower-return-gifts", "photo-1607344645866-009c320b63e0"),
    ("Traditional Jute Gift Bags", 249, "Reusable jute gift bags with a neat traditional finish, ideal for sweets and small keepsakes.", "baby-shower-return-gifts", "photo-1590874103328-eac38a683ce7"),

    ("Customized Name Bottles", 349, "Reusable water bottles personalized with each little guest's name for a practical birthday keepsake.", "birthday-return-gifts", "photo-1602143407151-7111542de6e8"),
    ("Chocolate Hampers", 799, "A cheerful assortment of chocolates, wrapped and ready to make every birthday guest smile.", "birthday-return-gifts", "photo-1606313564200-e75d5e30476c"),
    ("Stationery Kits", 399, "A delightful set of writing and drawing essentials packed for young artists and curious minds.", "birthday-return-gifts", "photo-1455390582262-044cdead277a"),
    ("Customized Keychains", 249, "A charming personalized keychain favour with a name or birthday detail to remember the celebration.", "birthday-return-gifts", "photo-1513364776144-60967b0f800f"),
    ("Kids Activity Kits", 599, "A screen-free activity kit with fun creative projects to keep little party guests happily engaged.", "birthday-return-gifts", "photo-1513475382585-d06e58bcb0e0"),

    ("Personalized Name Plates", 1499, "A welcoming name plate personalized for a family, crafted as a memorable housewarming keepsake.", "customized-return-gifts", "photo-1600210492486-724fe5c67fb0"),
    ("Wooden Gifts", 899, "A thoughtfully finished wooden keepsake with optional personalization and a warm artisan character.", "customized-return-gifts", "photo-1531058020387-3be344556be6"),
    ("Name-Printed Bags", 299, "Reusable cotton gift bags printed with a name or event detail for a personal, practical favour.", "customized-return-gifts", "photo-1590874103328-eac38a683ce7"),
    ("Customized Gift Hampers", 1799, "A made-to-order hamper with a personal message and a considered mix of boutique treats.", "customized-return-gifts", "photo-1549465220-1a8b9238cd48"),
    ("Photo Frames", 699, "A graceful tabletop photo frame personalized to preserve a favourite celebration memory.", "customized-return-gifts", "photo-1513519245088-0e12902e5a38"),

    ("Brass Diya", 599, "A finely finished brass diya that brings a traditional glow to pooja rooms and festive evenings.", "housewarming-return-gifts", "photo-1603006905003-be475563bc59"),
    ("Ceramic Pots", 499, "Hand-finished decorative ceramic pots to add a gentle artisan accent to a new home.", "housewarming-return-gifts", "photo-1578749556568-bc2c40e68b61"),
    ("Traditional Pooja Sets", 1199, "A useful pooja set with traditional essentials, presented as an auspicious new-home gift.", "housewarming-return-gifts", "photo-1607344645866-009c320b63e0"),
    ("Decorative Urli", 999, "A decorative urli inspired by South Indian homes, perfect for flowers and welcoming displays.", "housewarming-return-gifts", "photo-1600210492486-724fe5c67fb0"),
    ("Lakshmi Coin Gift Box", 449, "A Lakshmi coin gift presented in an elegant box as a symbol of prosperity and good wishes.", "housewarming-return-gifts", "photo-1607344645866-009c320b63e0"),

    ("Manjal Kumkum Set", 349, "A graceful manjal and kumkum set arranged with traditional details for an auspicious guest favour.", "traditional-return-gifts", "photo-1603006905003-be475563bc59"),
    ("Brass Kuthu Vilakku", 1299, "A traditional brass kuthu vilakku with graceful detailing, gifted as a lasting symbol of light and prosperity.", "traditional-return-gifts", "photo-1603006905003-be475563bc59"),
    ("Thamboolam Bags", 299, "Reusable festive thamboolam bags with room for betel leaves, fruits, sweets and small gifts.", "traditional-return-gifts", "photo-1590874103328-eac38a683ce7"),
    ("Tanjore Art Gifts", 1499, "A richly detailed Tanjore-inspired art keepsake celebrating Tamil Nadu's beloved craft heritage.", "traditional-return-gifts", "photo-1577083552431-6e5fd01aa342"),
    ("Terracotta Items", 399, "Earthy terracotta home accents with a handmade character, thoughtfully chosen for meaningful gifting.", "traditional-return-gifts", "photo-1578749556568-bc2c40e68b61"),

    ("Premium Thamboolam Bags", 599, "Premium fabric thamboolam bags with refined South Indian detailing for your wedding guests.", "wedding-return-gifts", "photo-1590874103328-eac38a683ce7"),
    ("Brass Diya Sets", 1499, "A matching set of brass diyas, beautifully finished as a lasting and auspicious wedding favour.", "wedding-return-gifts", "photo-1603006905003-be475563bc59"),
    ("Wedding Sweet Boxes", 499, "An elegant wedding sweet box presentation, ready to share a little sweetness with every guest.", "wedding-return-gifts", "photo-1606313564200-e75d5e30476c"),
    ("Kumkum Boxes", 399, "Decorative kumkum boxes with a traditional finish, curated as a lovely keepsake for wedding guests.", "wedding-return-gifts", "photo-1607344645866-009c320b63e0"),
    ("Premium Gift Hampers", 2499, "A premium wedding hamper of artisan-selected treats and keepsakes, finished with a personal note.", "wedding-return-gifts", "photo-1549465220-1a8b9238cd48"),
]

CUSTOMIZABLE_RETURN_GIFT_PRODUCTS = {
    "Customized Name Bottles",
    "Customized Keychains",
    "Personalized Name Plates",
    "Name-Printed Bags",
    "Customized Gift Hampers",
    "Photo Frames",
}


class Command(BaseCommand):
    help = "Create VEDHIKA BRAND collections and starter catalog without overwriting existing products."

    def handle(self, *args, **options):
        for name, slug, description in COLLECTIONS:
            Category.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "description": description},
            )
        created_count = 0
        catalog_products = [*PRODUCTS, *RETURN_GIFT_PRODUCTS]
        name_counts = {}
        for name, _, _, _, _ in catalog_products:
            name_counts[name] = name_counts.get(name, 0) + 1
        for name, price, description, category_slug, photo_id in catalog_products:
            category = Category.objects.get(slug=category_slug)
            product_slug = slugify(f"{name}-{category_slug}") if name_counts[name] > 1 else slugify(name)
            product, created = Product.objects.get_or_create(
                slug=product_slug,
                defaults={
                    "name": name,
                    "description": description,
                    "price": price,
                    "stock": 25,
                    "category": category,
                    "is_customizable": (
                        category_slug in {"wedding-return-gifts", "corporate-gifting", "customized-return-gifts"}
                        or name in CUSTOMIZABLE_RETURN_GIFT_PRODUCTS
                    ),
                },
            )
            if created:
                ProductImage.objects.create(
                    product=product,
                    image_url=f"https://images.unsplash.com/{photo_id}?auto=format&fit=crop&w=1100&q=85",
                    alt_text=name,
                    is_primary=True,
                )
                created_count += 1
        self.stdout.write(self.style.SUCCESS(f"VEDHIKA BRAND catalog ready. Added {created_count} new products."))
