from typing import Optional
import keyring
from keyring.errors import KeyringError
from opal_sync.utils.logging import get_logger

logger = get_logger()
KEYRING_SERVICE_NAME = "opal-sync"


def store_credentials(username: str, password: str, service: str = KEYRING_SERVICE_NAME) -> bool:
    """Store the user password securely in OS credential manager."""
    try:
        keyring.set_password(service, username, password)
        logger.debug(f"Stored credentials in keyring for username: {username}")
        return True
    except Exception as e:
        logger.error(f"Failed to store password in keyring: {e}")
        return False


def get_credentials(username: str, service: str = KEYRING_SERVICE_NAME) -> Optional[str]:
    """Retrieve the password from OS credential manager."""
    try:
        return keyring.get_password(service, username)
    except Exception as e:
        logger.error(f"Failed to retrieve password from keyring: {e}")
        return None


def delete_credentials(username: str, service: str = KEYRING_SERVICE_NAME) -> bool:
    """Delete the password from OS credential manager."""
    try:
        keyring.delete_password(service, username)
        return True
    except Exception:
        return False
