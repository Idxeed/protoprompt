from tenacity.attempts import count_attempts

class FakeState:
    def __init__(self, attempt_number):
        self.attempt_number = attempt_number

def test_counts_attempt_number():
    assert count_attempts(FakeState(7)) == 7

def test_missing_attribute_gives_zero():
    class Empty: pass
    assert count_attempts(Empty()) == 0
