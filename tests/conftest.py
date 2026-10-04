import pytest

pytest_plugins = ["pytester"]

# The fixture org every test runs as: the scripts take their org from GITRUNNER_ORG and have no
# default, so a stranger's checkout never builds for someone else's org.
TEST_ORG = "example-org"


@pytest.fixture(autouse=True)
def _fixture_org(monkeypatch):
    monkeypatch.setenv("GITRUNNER_ORG", TEST_ORG)
