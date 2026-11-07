import pytest

from app.utils import own_domain


@pytest.mark.parametrize("domain", [
    "facebook.com", "m.facebook.com", "web.facebook.com", "business.facebook.com",
    "api.whatsapp.com", "sites.google.com", "maps.app.goo.gl", "joes.wixsite.com", None, "",
])
def test_shared_platforms_are_not_the_companys_own_domain(domain):
    assert own_domain(domain) is None


@pytest.mark.parametrize("domain", ["smiledental.com", "notfacebook.com", "shop.acme.co.uk"])
def test_own_domains(domain):
    assert own_domain(domain) == domain
