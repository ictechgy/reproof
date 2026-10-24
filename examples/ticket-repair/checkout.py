def checkout_total(subtotal_cents):
    if subtotal_cents > 5000:
        return subtotal_cents
    return subtotal_cents + 500
