class InvalidStatusTransitionError(Exception):
    pass


class CartEmptyError(Exception):
    pass


class OrderMinimumAmountError(Exception):
    pass


class DishUnavailableError(Exception):
    pass


class CartLimitExceededError(Exception):
    pass
