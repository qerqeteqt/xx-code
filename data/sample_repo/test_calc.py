from calc import add, div


def test_add():
    assert add(2, 3) == 5


def test_div_by_zero():
    assert div(1, 0) is None
