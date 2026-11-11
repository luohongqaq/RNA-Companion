"""Business exception hierarchy."""


class BusinessError(Exception):
    code = "business_error"
    status_code = 409


class NotFoundError(BusinessError):
    code = "not_found"
    status_code = 404


class InvalidStateError(BusinessError):
    code = "invalid_state"


class InvalidInputError(BusinessError):
    code = "invalid_input"
    status_code = 422


class IdempotencyConflictError(BusinessError):
    code = "idempotency_conflict"

