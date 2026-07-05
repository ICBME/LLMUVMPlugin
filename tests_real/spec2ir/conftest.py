from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--spec2ir-real-data-limit",
        action="store",
        type=int,
        default=10,
        help="Limit the number of Spec2IR real-data cases for aggregate tests.",
    )


@pytest.fixture
def spec2ir_real_data_limit(request: pytest.FixtureRequest) -> int:
    return int(request.config.getoption("--spec2ir-real-data-limit"))

