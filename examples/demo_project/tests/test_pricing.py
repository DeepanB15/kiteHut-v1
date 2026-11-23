from shop.models import Item, Order
from shop.pricing import calculate_total


def test_total():
    assert calculate_total(Order([Item("A", 100)]), 10) == 90
