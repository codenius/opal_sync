import pytest
import httpx
from opal_sync.auth.shibboleth import (
    extract_login_error,
    extract_form_details,
    login_shibboleth,
    ShibbolethAuthError,
)


def test_extract_login_error_from_idp_feedback_html():
    html = """
    <html><body>
      <form>
        <div class="feedbackPanelERROR">
          Die Anmeldung ist fehlgeschlagen. Bitte prüfen Sie Benutzername und Passwort.
        </div>
      </form>
    </body></html>
    """

    assert extract_login_error(html) == (
        "Die Anmeldung ist fehlgeschlagen. Bitte prüfen Sie Benutzername und Passwort."
    )


def test_extract_login_error_from_plain_idp_message():
    html = "<html><body><p>Anmeldung fehlgeschlagen. Das Passwort ist falsch.</p></body></html>"

    assert extract_login_error(html) == "Anmeldung fehlgeschlagen."


def test_extract_form_details(idp_creds_html):
    action, inputs = extract_form_details(idp_creds_html)
    assert action == "/idp/profile/SAML2/Redirect/SSO?execution=e1s2"
    assert "csrf_token" in inputs
    assert inputs["csrf_token"] == "test-csrf-token"
    assert "j_username" in inputs
    assert "j_password" in inputs

@pytest.mark.asyncio
async def test_login_shibboleth_success(shiblogin_html, idp_login_html, idp_creds_html):
    saml_assertion_html = """
    <html><body>
    <form action="https://bildungsportal.sachsen.de/dfn/Shibboleth.sso/SAML2/POST" method="post">
        <input type="hidden" name="SAMLResponse" value="PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0iVVRGLTgiPz4=" />
        <input type="hidden" name="RelayState" value="cookie:123456" />
    </form>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "shiblogin" in url and request.method == "GET":
            return httpx.Response(200, text=shiblogin_html, request=request)
        if "shiblogin" in url and request.method == "POST":
            return httpx.Response(
                302,
                headers={"Location": "https://bildungsportal.sachsen.de/dfn/Shibboleth.sso/Login?entityID=htwk"},
                request=request,
            )
        if "Shibboleth.sso/Login" in url and request.method == "GET":
            return httpx.Response(
                302,
                headers={"Location": "https://shib1.rz.htwk-leipzig.de/idp/profile/SAML2/Redirect/SSO?execution=e1s1"},
                request=request,
            )
        if "execution=e1s1" in url and request.method == "GET":
            return httpx.Response(200, text=idp_login_html, request=request)
        if "execution=e1s1" in url and request.method == "POST":
            return httpx.Response(200, text=idp_creds_html, request=request)
        if "execution=e1s2" in url and request.method == "POST":
            return httpx.Response(200, text=saml_assertion_html, request=request)
        if "SAML2/POST" in url and request.method == "POST":
            return httpx.Response(
                302,
                headers={"Location": "https://bildungsportal.sachsen.de/opal/auth/home"},
                request=request,
            )
        if "/opal/auth/" in url:
            return httpx.Response(200, text="<html><body>Meine Kurse</body></html>", request=request)
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        success = await login_shibboleth(
            client=client,
            username="s12345",
            password="secretpassword",
            wayf_value=7,
        )
        assert success is True

@pytest.mark.asyncio
async def test_login_shibboleth_bad_password(shiblogin_html, idp_login_html, idp_creds_html):
    bad_creds_html = """
    <html><body>
      <form action="/idp/profile/SAML2/Redirect/SSO?execution=e1s2">
        <input name="j_username" />
        <input name="j_password" />
        <div class="feedbackPanelERROR">Das Passwort ist falsch.</div>
      </form>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "shiblogin" in url and request.method == "GET":
            return httpx.Response(200, text=shiblogin_html, request=request)
        if "shiblogin" in url and request.method == "POST":
            return httpx.Response(
                302,
                headers={"Location": "https://bildungsportal.sachsen.de/dfn/Shibboleth.sso/Login?entityID=htwk"},
                request=request,
            )
        if "Shibboleth.sso/Login" in url:
            return httpx.Response(
                302,
                headers={"Location": "https://shib1.rz.htwk-leipzig.de/idp/profile/SAML2/Redirect/SSO?execution=e1s1"},
                request=request,
            )
        if "execution=e1s1" in url and request.method == "GET":
            return httpx.Response(200, text=idp_login_html, request=request)
        if "execution=e1s1" in url and request.method == "POST":
            return httpx.Response(200, text=idp_creds_html, request=request)
        if "execution=e1s2" in url and request.method == "POST":
            return httpx.Response(200, text=bad_creds_html, request=request)
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ShibbolethAuthError, match="Invalid username or password"):
            await login_shibboleth(client, "s12345", "wrongpass", 7)
