TAX = 0.0825


def total(items, discounts=[]):
    t = 0
    for i in items:
        if i["qty"] > 0:
            if i["price"] > 0:
                t = t + i["price"] * i["qty"]
            else:
                t = t + 0
        else:
            t = t + 0
    for d in discounts:
        t = t - t * d
    t = t + t * TAX
    return round(t, 2)


def avg_price(items):
    return sum(i["price"] for i in items) / len(items)


def apply_coupon(t, code):
    if code == "HALF":
        return t / 2
    if code == "TENOFF":
        return t - 10
    if code == "FREE":
        return 0
    print("unknown coupon", code)
