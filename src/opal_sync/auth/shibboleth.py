import re
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import httpx

from opal_sync.utils.logging import get_logger

logger = get_logger()
OPAL_LOGIN_URL = "https://bildungsportal.sachsen.de/opal/shiblogin"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class ShibbolethAuthError(Exception):
    """Raised when authentication against Shibboleth / OPAL fails."""
    pass


def extract_form_details(html: str, form_selector: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """Extract action URL and hidden/default input values from an HTML form."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one(form_selector) if form_selector else soup.find("form")
    if not form:
        raise ShibbolethAuthError(f"No HTML form found matching selector '{form_selector}'")

    action = form.get("action", "")
    inputs: Dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            inputs[name] = inp.get("value", "")
    return action, inputs


async def login_shibboleth(
    client: httpx.AsyncClient,
    username: str,
    password: str,
    wayf_value: int = 7
) -> bool:
    """Execute the full 6-step Shibboleth SAML flow to authenticate against OPAL."""
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    client.cookies.clear()

    # Step 1: Initial visit to OPAL shiblogin page
    logger.info("Step 1/6: Initiating OPAL session...")
    resp = await client.get(OPAL_LOGIN_URL, headers=headers, follow_redirects=True)
    if resp.status_code != 200:
        raise ShibbolethAuthError(f"Failed to access OPAL login page (HTTP {resp.status_code})")

    # Locate the WAYF form
    soup = BeautifulSoup(resp.text, "html.parser")
    shib_form = soup.find("form", id=re.compile(r".*shibAuthForm.*|id11|.*login.*", re.I))
    if not shib_form:
        # Fallback to any form containing wayfselection
        for f in soup.find_all("form"):
            if f.find(attrs={"name": "wayfselection"}):
                shib_form = f
                break

    if not shib_form:
        raise ShibbolethAuthError("Could not locate WAYF institution selection form on OPAL login page")

    wayf_action = urljoin(str(resp.url), shib_form.get("action", ""))

    # Step 2: Post WAYF selection (HTWK Leipzig = 7)
    logger.info(f"Step 2/6: Selecting home institution (WAYF value={wayf_value})...")
    wayf_data = {
        "wayfselection": str(wayf_value),
        "shibLogin": "Login mit Hochschul-Account",
    }
    resp = await client.post(
        wayf_action,
        data=wayf_data,
        headers={**headers, "Referer": str(resp.url)},
        follow_redirects=False
    )

    if resp.status_code not in (301, 302, 303, 307):
        raise ShibbolethAuthError(f"WAYF POST failed to redirect (HTTP {resp.status_code})")

    sp_login_url = urljoin(str(resp.url), resp.headers["Location"])

    # Step 3: Follow Service Provider (SP) redirect to HTWK IdP
    logger.info("Step 3/6: Contacting Shibboleth SP and redirecting to IdP...")
    resp = await client.get(sp_login_url, headers=headers, follow_redirects=False)
    if resp.status_code not in (301, 302, 303, 307):
        raise ShibbolethAuthError(f"SP redirect failed (HTTP {resp.status_code})")

    idp_redirect_url = urljoin(str(resp.url), resp.headers["Location"])

    # Step 4: Contact HTWK IdP (Local Storage check step)
    logger.info("Step 4/6: Processing IdP local storage verification...")
    resp = await client.get(idp_redirect_url, headers=headers, follow_redirects=True)
    if resp.status_code != 200:
        raise ShibbolethAuthError(f"Failed to reach IdP login flow (HTTP {resp.status_code})")

    # The IdP may present an execution=e1s1 form (local storage check)
    if "execution=e1s1" in str(resp.url) or "shib_idp_ls_supported" in resp.text:
        action, form_inputs = extract_form_details(resp.text)
        action_url = urljoin(str(resp.url), action)

        ls_payload = dict(form_inputs)
        ls_payload.update({
            "shib_idp_ls_exception.shib_idp_session_ss": "",
            "shib_idp_ls_success.shib_idp_session_ss": "false",
            "shib_idp_ls_value.shib_idp_session_ss": "",
            "shib_idp_ls_exception.shib_idp_persistent_ss": "",
            "shib_idp_ls_success.shib_idp_persistent_ss": "false",
            "shib_idp_ls_value.shib_idp_persistent_ss": "",
            "shib_idp_ls_supported": "",
            "_eventId_proceed": "",
        })

        resp = await client.post(
            action_url,
            data=ls_payload,
            headers={**headers, "Referer": str(resp.url)},
            follow_redirects=True
        )

    # Step 5: Submit credentials at IdP (usually execution=e1s2)
    logger.info("Step 5/6: Submitting credentials to HTWK IdP...")
    action, cred_inputs = extract_form_details(resp.text)
    action_url = urljoin(str(resp.url), action)

    csrf_token = cred_inputs.get("csrf_token", "")
    cred_payload = {
        "csrf_token": csrf_token,
        "j_username": username,
        "j_password": password,
        "_eventId_proceed": "",
    }

    resp = await client.post(
        action_url,
        data=cred_payload,
        headers={**headers, "Referer": str(resp.url)},
        follow_redirects=True
    )

    if resp.status_code != 200:
        raise ShibbolethAuthError(f"Credential submission failed (HTTP {resp.status_code})")

    # Check for authentication errors on IdP page
    if "Passwort ist falsch" in resp.text or "Benutzername oder Passwort" in resp.text or "Anmeldung fehlgeschlagen" in resp.text:
        raise ShibbolethAuthError("Invalid username or password for HTWK login.")

    # Check for SAMLResponse in form
    soup = BeautifulSoup(resp.text, "html.parser")
    saml_input = soup.find("input", attrs={"name": "SAMLResponse"})
    relay_state_input = soup.find("input", attrs={"name": "RelayState"})
    saml_form = soup.find("form")

    if not saml_input or not saml_form:
        raise ShibbolethAuthError(
            "IdP did not return a SAMLResponse assertion. Check your credentials or HTWK account state."
        )

    saml_post_url = urljoin(str(resp.url), saml_form.get("action", ""))
    saml_payload = {
        "SAMLResponse": saml_input.get("value", ""),
        "RelayState": relay_state_input.get("value", "") if relay_state_input else "",
    }

    # Step 6: Post SAMLResponse back to Shibboleth SP
    logger.info("Step 6/6: Submitting SAML assertion to OPAL SP...")
    resp = await client.post(
        saml_post_url,
        data=saml_payload,
        headers={**headers, "Referer": str(resp.url)},
        follow_redirects=True
    )

    # After posting SAML, we should land on an authenticated OPAL page (e.g. /opal/auth/...)
    final_url = str(resp.url)
    if "/opal/auth/" in final_url or "dmz" not in final_url and resp.status_code == 200:
        logger.info("[green]Authentication successful![/green]")
        return True

    # Check if we can reach resource/courses
    test_resp = await client.get(f"{OPAL_LOGIN_URL.rsplit('/', 1)[0]}/auth/resource/courses", headers=headers)
    if test_resp.status_code == 200 and "shiblogin" not in test_resp.text.lower():
        logger.info("[green]Authentication confirmed via resource/courses access![/green]")
        return True

    raise ShibbolethAuthError(f"Post-SAML navigation ended at unexpected location: {final_url}")
