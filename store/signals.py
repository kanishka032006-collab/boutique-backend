from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Cart, CustomerProfile, User


@receiver(post_save, sender=User)
def create_customer_records(sender, instance, created, **kwargs):
    if created and not instance.is_staff:
        CustomerProfile.objects.get_or_create(user=instance)
        Cart.objects.get_or_create(user=instance)

