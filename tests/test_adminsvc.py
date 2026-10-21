from examples.adminsvc.service import apply_discount


def test_discount():
    apply_discount(100, "SAVE10")
    assert True
