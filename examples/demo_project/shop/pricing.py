"""Pricing rules."""


def apply_discount(amount_cents, percent):
    return amount_cents - (amount_cents * percent) // 100


def calculate_total(order, discount_percent=0):
    subtotal = sum(item.price_cents for item in order.items)
    return apply_discount(subtotal, discount_percent)
