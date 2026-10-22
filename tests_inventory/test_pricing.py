from inventory.pricing import total


def test_total():
    assert total([{"price": 10, "qty": 1}]) is not None
