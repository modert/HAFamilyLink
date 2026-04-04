"""File-based storage for cookies with encryption."""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from cryptography.fernet import Fernet, InvalidToken
import logging

_LOGGER = logging.getLogger(__name__)


class SharedStorage:
    """Manages cookie storage in Home Assistant shared directory."""

    def __init__(self, share_dir: str = "/share/familylink"):
        """Initialize storage manager."""
        self.share_dir = Path(share_dir)
        self.storage_path = self.share_dir / "cookies.enc"
        self.key_file = self.share_dir / ".key"
        self._encryption_key = self._get_encryption_key()

        # Ensure directory exists — HA add-ons share /share via mapped volume
        self.share_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.share_dir, 0o700)

    def _get_encryption_key(self) -> bytes:
        """Get or create encryption key."""
        if self.key_file.exists():
            with open(self.key_file, "rb") as f:
                return f.read()

        # Generate new key
        key = Fernet.generate_key()
        self.key_file.write_bytes(key)
        os.chmod(self.key_file, 0o600)

        _LOGGER.info("Generated new encryption key")
        return key

    async def save_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        """Save cookies to encrypted file."""
        try:
            # Prepare data
            data = {
                "cookies": cookies,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0"
            }

            # Encrypt
            fernet = Fernet(self._encryption_key)
            json_data = json.dumps(data, indent=2)
            encrypted = fernet.encrypt(json_data.encode())

            # Write atomically
            temp_file = self.storage_path.with_suffix('.tmp')
            temp_file.write_bytes(encrypted)
            temp_file.rename(self.storage_path)

            os.chmod(self.storage_path, 0o600)

            _LOGGER.info(f"Saved {len(cookies)} cookies to shared storage")

        except Exception as e:
            _LOGGER.error(f"Failed to save cookies: {e}")
            raise

    async def load_cookies(self) -> List[Dict[str, Any]]:
        """Load cookies from encrypted file."""
        if not self.storage_path.exists():
            raise FileNotFoundError("No cookies found")

        try:
            # Read and decrypt
            encrypted = self.storage_path.read_bytes()
            fernet = Fernet(self._encryption_key)
            decrypted = fernet.decrypt(encrypted)

            # Parse
            data = json.loads(decrypted.decode())
            cookies = data.get("cookies", [])

            _LOGGER.info(f"Loaded {len(cookies)} cookies from shared storage")
            return cookies

        except InvalidToken:
            _LOGGER.error(
                "Cookie file is corrupted or encryption key has changed. "
                "Deleting corrupted file — please re-authenticate."
            )
            self.storage_path.unlink(missing_ok=True)
            raise FileNotFoundError("Cookies were corrupted and have been deleted. Please re-authenticate.")

        except Exception as e:
            _LOGGER.error(f"Failed to load cookies: {e}")
            raise

    async def save_browser_state(self, state: Dict[str, Any]) -> None:
        """Save Playwright browser storage state (cookies + localStorage + sessionStorage)."""
        try:
            state_path = self.share_dir / "browser_state.enc"
            data = {
                "state": state,
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0"
            }
            fernet = Fernet(self._encryption_key)
            encrypted = fernet.encrypt(json.dumps(data).encode())

            temp_file = state_path.with_suffix('.tmp')
            temp_file.write_bytes(encrypted)
            temp_file.rename(state_path)
            os.chmod(state_path, 0o600)

            _LOGGER.info("Saved browser state to shared storage")
        except Exception as e:
            _LOGGER.error(f"Failed to save browser state: {e}")
            raise

    async def load_browser_state(self) -> Dict[str, Any] | None:
        """Load Playwright browser storage state."""
        state_path = self.share_dir / "browser_state.enc"
        if not state_path.exists():
            return None

        try:
            encrypted = state_path.read_bytes()
            fernet = Fernet(self._encryption_key)
            decrypted = fernet.decrypt(encrypted)
            data = json.loads(decrypted.decode())
            _LOGGER.info("Loaded browser state from shared storage")
            return data.get("state")
        except (InvalidToken, Exception) as e:
            _LOGGER.warning(f"Failed to load browser state: {e}")
            return None

    async def browser_state_exists(self) -> bool:
        """Check if browser state file exists."""
        return (self.share_dir / "browser_state.enc").exists()

    async def clear_cookies(self) -> None:
        """Remove stored cookies and browser state."""
        if self.storage_path.exists():
            self.storage_path.unlink()
        state_path = self.share_dir / "browser_state.enc"
        if state_path.exists():
            state_path.unlink()
        _LOGGER.info("Cleared stored cookies and browser state")

    async def check_exists(self) -> bool:
        """Check if cookies exist."""
        return self.storage_path.exists()
