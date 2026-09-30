import pytest

from voice_agent.costs import PriceBook


@pytest.fixture(scope="session")
def book() -> PriceBook:
    return PriceBook.load()
