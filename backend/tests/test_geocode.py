import httpx
import pytest

from app.sources.base import SourceError  # (first: app.sources imports app.geocode)
from app.geocode import geocode  # noqa: E402


@pytest.mark.parametrize("response, words", [
    (httpx.Response(429), "busy"),
    (httpx.Response(503), "didn't answer"),
    (httpx.Response(200, text="<html>not json</html>"), "didn't answer"),
])
def test_location_service_problems_are_explained(response, words):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(SourceError, match=words):
        geocode(client, "Karachi")


def test_network_error_is_explained():
    def fail(request):
        raise httpx.ConnectError("no internet")
    with pytest.raises(SourceError):
        geocode(httpx.Client(transport=httpx.MockTransport(fail)), "Karachi")
