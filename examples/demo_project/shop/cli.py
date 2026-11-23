"""Command-line entry point."""

from shop.models import Item
from shop.orders import OrderService


def main():
    service = OrderService()
    return service.checkout([Item("A1", 1000), Item("B2", 250)], 10)
