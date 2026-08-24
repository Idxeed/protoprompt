def count_attempts(retry_state):
    return getattr(retry_state, 'attempt_number', 0)