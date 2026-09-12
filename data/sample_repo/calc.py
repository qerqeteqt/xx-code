"""一个故意写错的小模块，用来试跑 agent。"""


def add(a, b):
    """返回两数之和。"""
    return a - b  # 故意的 bug：应该是 a + b


def div(a, b):
    """返回 a 除以 b；b 为 0 时返回 None。"""
    return a / b  # 故意的 bug：没有处理除零
