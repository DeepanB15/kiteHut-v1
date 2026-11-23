"""Order workflow."""

from shop.models import Order
from shop.pricing import calculate_total


class OrderService:
    def create(self, items):
        return Order(items)

    def checkout(self, items, discount_percent=0):
        order = self.create(items)
        return calculate_total(order, discount_percent)
