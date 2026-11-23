"""Plain data models."""


class Item:
    """A purchasable item."""

    def __init__(self, sku, price_cents):
        self.sku = sku
        self.price_cents = price_cents


class Order:
    """A collection of items."""

    def __init__(self, items):
        self.items = list(items)
