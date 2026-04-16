"""Browser-based authentication manager using Playwright."""
import asyncio
import logging
import time
import uuid
from typing import Dict, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

_LOGGER = logging.getLogger(__name__)

# Chromium flags shared between interactive auth and headless refresh
_CHROMIUM_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-gpu-compositing',
    '--disable-gpu-sandbox',
    '--disable-software-rasterizer',
    '--disable-accelerated-2d-canvas',
    '--disable-accelerated-video-decode',
    '--disable-accelerated-video-encode',
    '--disable-skia-runtime-opts',
    '--disable-partial-raster',
    '--disable-zero-copy',
    '--disable-lcd-text',
    '--disable-font-subpixel-positioning',
    '--disable-features=VizDisplayCompositor,dbus,IsolateOrigins,site-per-process,UseSkiaRenderer,TranslateUI',
    '--disable-breakpad',
    '--disable-component-update',
    '--disable-blink-features=AutomationControlled',
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-extensions',
    '--disable-sync',
    '--no-first-run',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-background-timer-throttling',
    '--memory-pressure-off',
    '--disable-low-res-tiling',
    '--ozone-platform=x11',
]


class SessionKeepAlive:
    """Periodic session refresh using saved browser state.

    Launches a headless browser with saved Playwright storage state,
    navigates to families.google.com to exercise the session, extracts
    fresh cookies, and saves both cookies and updated state back to disk.
    """

    # Normal interval between refreshes
    REFRESH_INTERVAL = 4 * 3600  # 4 hours
    # Retry backoff schedule after failures (seconds)
    RETRY_BACKOFF = [3600, 7200, 14400]  # 1h, 2h, 4h
    # How many consecutive failures before we stop retrying
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, storage, language: str = "en-US", timezone: str = "Europe/Paris"):
        self._storage = storage
        self._language = language
        self._timezone = timezone
        self._task: Optional[asyncio.Task] = None
        self._consecutive_failures = 0
        self._last_refresh: Optional[float] = None
        self._last_error: Optional[str] = None
        self._refresh_count = 0
        self._running = False

    @property
    def status(self) -> dict:
        """Return keepalive status for health endpoint."""
        return {
            "running": self._running,
            "last_refresh": self._last_refresh,
            "last_refresh_ago": round(time.time() - self._last_refresh) if self._last_refresh else None,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "total_refreshes": self._refresh_count,
        }

    def start(self):
        """Start the keepalive background loop."""
        if self._task and not self._task.done():
            _LOGGER.warning("SessionKeepAlive already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_done)
        _LOGGER.info("SessionKeepAlive started (interval=%ds)", self.REFRESH_INTERVAL)

    def stop(self):
        """Stop the keepalive background loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        _LOGGER.info("SessionKeepAlive stopped")

    async def trigger_refresh(self):
        """Manually trigger a single refresh cycle."""
        await self._do_refresh()

    def reset_after_auth(self):
        """Reset failure tracking after a successful interactive authentication."""
        self._consecutive_failures = 0
        self._last_refresh = time.time()

    def _on_done(self, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            _LOGGER.error("SessionKeepAlive loop crashed: %s", exc)

    async def _loop(self):
        """Main keepalive loop. Runs an initial refresh on startup, then periodic."""
        # Initial refresh on startup (if state exists)
        if await self._storage.browser_state_exists():
            _LOGGER.info("Browser state found on startup, running initial refresh")
            await self._do_refresh()
        else:
            _LOGGER.info("No browser state found, waiting for first authentication")

        while self._running:
            delay = self._next_delay()
            _LOGGER.info("Next session refresh in %d minutes", delay // 60)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            await self._do_refresh()

    def _next_delay(self) -> int:
        """Calculate delay until next refresh, accounting for backoff on failures."""
        if self._consecutive_failures == 0:
            return self.REFRESH_INTERVAL
        idx = min(self._consecutive_failures - 1, len(self.RETRY_BACKOFF) - 1)
        return self.RETRY_BACKOFF[idx]

    async def _do_refresh(self):
        """Execute one refresh cycle: launch browser, navigate, extract cookies."""
        state = await self._storage.load_browser_state()
        if state is None:
            _LOGGER.warning("No browser state available for refresh")
            return

        playwright = None
        browser = None
        context = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=True,
                args=_CHROMIUM_ARGS,
            )
            context = await browser.new_context(
                storage_state=state,
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 800},
                locale=self._language,
                timezone_id=self._timezone,
            )
            page = await context.new_page()

            # Navigate to families.google.com — this exercises the session
            _LOGGER.info("Refresh: navigating to families.google.com...")
            resp = await page.goto('https://families.google.com/families/', wait_until='load', timeout=30000)

            # Check if we got redirected to a login page
            current_url = page.url
            if 'accounts.google.com' in current_url:
                raise RuntimeError(f"Session expired — redirected to login: {current_url}")

            if resp and resp.status >= 400:
                raise RuntimeError(f"Bad response from families.google.com: {resp.status}")

            _LOGGER.info("Refresh: page loaded at %s", current_url)
            # Let any JS settle and Set-Cookie headers apply
            await asyncio.sleep(3)

            # Also visit an API-like page to trigger more cookie refreshes
            try:
                await page.goto('https://families.google.com/families/', wait_until='load', timeout=15000)
                await asyncio.sleep(2)
            except Exception:
                pass  # Non-critical

            # Extract fresh cookies
            cookies = await context.cookies()
            google_cookies = [
                c for c in cookies
                if any(d in c.get('domain', '') for d in ['google.com', 'families.google.com', 'accounts.google.com'])
            ]

            if not google_cookies:
                raise RuntimeError("No Google cookies found after navigation")

            # Save fresh cookies
            await self._storage.save_cookies(google_cookies)

            # Save updated browser state (includes refreshed localStorage etc.)
            new_state = await context.storage_state()
            await self._storage.save_browser_state(new_state)

            self._consecutive_failures = 0
            self._last_refresh = time.time()
            self._last_error = None
            self._refresh_count += 1
            _LOGGER.info(
                "Session refresh #%d successful: %d cookies saved",
                self._refresh_count, len(google_cookies),
            )

        except Exception as e:
            self._consecutive_failures += 1
            self._last_error = str(e)
            _LOGGER.error(
                "Session refresh failed (attempt %d/%d): %s",
                self._consecutive_failures, self.MAX_CONSECUTIVE_FAILURES, e,
            )
            if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                _LOGGER.error(
                    "Session refresh has failed %d times consecutively. "
                    "Manual re-authentication is likely required via noVNC.",
                    self._consecutive_failures,
                )

        finally:
            try:
                if context:
                    await context.close()
                if browser:
                    await browser.close()
                if playwright:
                    await playwright.stop()
            except Exception as cleanup_err:
                _LOGGER.warning("Refresh cleanup error: %s", cleanup_err)


class BrowserAuthManager:
    """Manages browser-based authentication sessions."""

    MAX_CONCURRENT_SESSIONS = 1

    def __init__(self, auth_timeout: int = 300, language: str = "en-US", timezone: str = "Europe/Paris", storage=None):
        """Initialize browser auth manager."""
        self._sessions: Dict[str, Dict] = {}
        self._monitor_tasks: Dict[str, asyncio.Task] = {}
        self._playwright = None
        self._auth_timeout = auth_timeout
        self._language = language
        self._timezone = timezone
        self._storage = storage  # Injected SharedStorage instance
        self._keepalive: Optional[SessionKeepAlive] = None

    async def initialize(self):
        """Initialize Playwright and start session keepalive."""
        try:
            self._playwright = await async_playwright().start()
            _LOGGER.info("Playwright initialized successfully")
        except Exception as e:
            _LOGGER.error(f"Failed to initialize Playwright: {e}")
            raise

        # Start keepalive scheduler
        if self._storage:
            self._keepalive = SessionKeepAlive(
                self._storage,
                language=self._language,
                timezone=self._timezone,
            )
            self._keepalive.start()

    async def start_auth_session(self) -> str:
        """Start a new authentication session."""
        # Prune old completed sessions (prevent memory leak)
        self._prune_old_sessions()

        # Prevent concurrent sessions (memory protection, especially on RPi)
        active = [s for s in self._sessions.values() if s.get('status') == 'authenticating']
        if len(active) >= self.MAX_CONCURRENT_SESSIONS:
            raise RuntimeError("An authentication session is already in progress. Please wait or cancel it first.")

        session_id = str(uuid.uuid4())
        _LOGGER.info(f"Starting authentication session: {session_id}")

        browser = None
        context = None
        page = None
        try:
            # Launch browser (non-headless so user can interact)
            # Extensive flags for virtualized/nested VM environments (VirtualBox, VMware, etc.)
            # These prevent crashes caused by GPU acceleration and missing system services
            browser = await self._playwright.chromium.launch(
                headless=False,
                args=_CHROMIUM_ARGS,
            )

            # Create context with realistic user agent
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 800},
                locale=self._language,
                timezone_id=self._timezone
            )

            # Create page
            page = await context.new_page()

            # Store session
            self._sessions[session_id] = {
                'browser': browser,
                'context': context,
                'page': page,
                'status': 'authenticating',
                'cookies': None,
                'error': None,
                'created_at': time.time(),
            }

            # Listen for new tabs/popups
            def on_page(new_page):
                _LOGGER.info(f"New tab detected, switching monitoring to new page")
                self._sessions[session_id]['page'] = new_page

            context.on("page", on_page)

            # Navigate to Google Family Link
            # Using 'load' instead of 'networkidle' for better reliability
            # 'networkidle' can timeout on pages with continuous background requests
            _LOGGER.info("Navigating to Google Family Link...")
            await page.goto('https://families.google.com', wait_until='load', timeout=30000)

            # Start monitoring in background with proper error handling
            task = asyncio.create_task(self._monitor_authentication(session_id))
            task.add_done_callback(lambda t: self._on_monitor_done(session_id, t))
            self._monitor_tasks[session_id] = task

            return session_id

        except Exception as e:
            _LOGGER.error(f"Failed to start auth session: {e}")
            # Cleanup browser resources on failure to prevent leaks
            try:
                if page:
                    await page.close()
                if context:
                    await context.close()
                if browser:
                    await browser.close()
            except Exception as cleanup_err:
                _LOGGER.warning(f"Cleanup after failed session start: {cleanup_err}")
            raise

    async def _monitor_authentication(self, session_id: str):
        """Monitor authentication progress."""
        session = self._sessions.get(session_id)
        if not session:
            return

        context: BrowserContext = session['context']

        try:
            _LOGGER.info(f"Monitoring authentication for session {session_id}")

            # Wait for successful login - multiple possible indicators
            # We'll wait for URL change or specific elements that indicate success
            max_wait_time = self._auth_timeout * 1000  # Convert to milliseconds

            # Wait for URL to contain "families.google.com" and not be on login page
            await asyncio.sleep(5)  # Give initial page time to load

            # Poll for authentication completion
            start_time = asyncio.get_event_loop().time()
            authenticated = False
            last_url = None
            GOOGLE_AUTH_COOKIE_NAMES = {'SID', 'HSID', 'SSID', 'APISID', 'SAPISID'}

            while (asyncio.get_event_loop().time() - start_time) < self._auth_timeout:
                # Get the current page (might have changed if new tab opened)
                page: Page = session['page']
                current_url = page.url

                # Log URL changes at INFO, repeated polls at DEBUG
                if current_url != last_url:
                    _LOGGER.info(f"URL changed to: {current_url}")
                    last_url = current_url
                else:
                    _LOGGER.debug(f"Polling - URL unchanged")

                # Method 1: URL-based detection
                # Check if we're past the login page
                if 'accounts.google.com' not in current_url:
                    if any(domain in current_url for domain in [
                        'families.google.com',
                        'myaccount.google.com',
                    ]):
                        _LOGGER.info(f"Authentication detected via URL: {current_url}")

                        # Navigate to families.google.com to ensure cookies are properly configured
                        _LOGGER.info("Navigating to families.google.com to finalize cookie configuration...")
                        try:
                            await page.goto('https://families.google.com/families/', wait_until='load', timeout=15000)
                            _LOGGER.info("Successfully navigated to families.google.com")
                            await asyncio.sleep(2)
                        except Exception as e:
                            _LOGGER.warning(f"Failed to navigate to families.google.com: {e}")

                        authenticated = True
                        break

                # Method 2: Cookie-based detection (fallback)
                # Google sets auth cookies (SID, HSID, etc.) after successful login
                # even before the URL redirect completes
                try:
                    cookies = await context.cookies()
                    google_auth_cookies = [
                        c for c in cookies
                        if c.get('name') in GOOGLE_AUTH_COOKIE_NAMES
                        and '.google.com' in c.get('domain', '')
                    ]
                    if len(google_auth_cookies) >= 3:
                        _LOGGER.info(
                            f"Authentication detected via cookies "
                            f"({len(google_auth_cookies)} auth cookies found: "
                            f"{[c['name'] for c in google_auth_cookies]})"
                        )

                        # Navigate to families.google.com to finalize cookies
                        _LOGGER.info("Navigating to families.google.com to finalize cookie configuration...")
                        try:
                            await page.goto('https://families.google.com/families/', wait_until='load', timeout=15000)
                            _LOGGER.info("Successfully navigated to families.google.com")
                            await asyncio.sleep(2)
                        except Exception as e:
                            _LOGGER.warning(f"Failed to navigate to families.google.com: {e}")

                        authenticated = True
                        break
                except Exception as e:
                    _LOGGER.debug(f"Cookie check failed: {e}")

                await asyncio.sleep(2)  # Check every 2 seconds

            if not authenticated:
                raise asyncio.TimeoutError("Authentication timeout")

            # Extract cookies
            _LOGGER.info("Authentication detected, extracting cookies...")
            cookies = await context.cookies()

            # Filter relevant Google cookies
            google_cookies = [
                c for c in cookies
                if any(domain in c.get('domain', '') for domain in [
                    'google.com', 'families.google.com', 'accounts.google.com'
                ])
            ]

            if not google_cookies:
                raise Exception("No valid Google cookies found")

            _LOGGER.info(f"Extracted {len(google_cookies)} Google cookies")

            # Save to shared storage (use injected instance to avoid config mismatch)
            if self._storage:
                await self._storage.save_cookies(google_cookies)
            else:
                from app.storage.file_storage import SharedStorage
                storage = SharedStorage()
                await storage.save_cookies(google_cookies)

            # Save full browser state for session keepalive
            try:
                browser_state = await context.storage_state()
                if self._storage:
                    await self._storage.save_browser_state(browser_state)
                    _LOGGER.info("Browser state saved for session keepalive")
                    # Reset keepalive failure counter since we have fresh state
                    if self._keepalive:
                        self._keepalive.reset_after_auth()
            except Exception as state_err:
                _LOGGER.warning(f"Failed to save browser state (non-critical): {state_err}")

            # Update session
            session['status'] = 'completed'
            session['cookies'] = google_cookies

            _LOGGER.info(f"Authentication completed successfully for session {session_id}")

            # Close browser after a short delay
            await asyncio.sleep(2)
            await self._cleanup_session(session_id)

        except (asyncio.TimeoutError, PlaywrightTimeoutError):
            session['status'] = 'timeout'
            session['error'] = 'Authentication timeout - user did not complete login in time'
            _LOGGER.error(f"Authentication timeout for session {session_id}")
            await self._cleanup_session(session_id)

        except Exception as e:
            session['status'] = 'error'
            session['error'] = str(e)
            _LOGGER.error(f"Authentication error for session {session_id}: {e}")
            await self._cleanup_session(session_id)

    def _on_monitor_done(self, session_id: str, task: asyncio.Task):
        """Handle monitor task completion, log unhandled errors."""
        self._monitor_tasks.pop(session_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            _LOGGER.error(f"Monitor task for session {session_id} failed: {exc}")

    def _prune_old_sessions(self, max_age: int = 3600):
        """Remove completed/errored sessions older than max_age seconds."""
        now = time.time()
        to_delete = [
            sid for sid, session in self._sessions.items()
            if session.get('status') in ('completed', 'timeout', 'error', 'cleaned_up')
            and now - session.get('created_at', 0) > max_age
        ]
        for sid in to_delete:
            del self._sessions[sid]
        if to_delete:
            _LOGGER.debug(f"Pruned {len(to_delete)} old sessions")

    async def get_session_status(self, session_id: str) -> Dict:
        """Get status of authentication session."""
        session = self._sessions.get(session_id)
        if not session:
            return {'status': 'not_found'}

        cookies = session.get('cookies')
        return {
            'status': session['status'],
            'has_cookies': cookies is not None,
            'error': session.get('error'),
            'cookie_count': len(cookies) if cookies else 0
        }

    async def _cleanup_session(self, session_id: str):
        """Clean up session resources."""
        session = self._sessions.get(session_id)
        if session:
            try:
                if session.get('page'):
                    await session['page'].close()
                if session.get('context'):
                    await session['context'].close()
                if session.get('browser'):
                    await session['browser'].close()
                _LOGGER.info(f"Cleaned up session {session_id}")
            except Exception as e:
                _LOGGER.warning(f"Cleanup error for session {session_id}: {e}")
            finally:
                # Retain only minimal metadata, discard heavy objects
                self._sessions[session_id] = {
                    'status': session.get('status', 'cleaned_up'),
                    'created_at': session.get('created_at'),
                }

    @property
    def keepalive_status(self) -> Optional[dict]:
        """Return keepalive status dict, or None if keepalive is not active."""
        if self._keepalive is None:
            return None
        return self._keepalive.status

    async def trigger_keepalive_refresh(self):
        """Manually trigger a keepalive refresh cycle.

        Raises RuntimeError if keepalive is not initialized.
        """
        if self._keepalive is None:
            raise RuntimeError("Keepalive not initialized")
        await self._keepalive.trigger_refresh()

    async def cleanup(self):
        """Cleanup all resources."""
        _LOGGER.info("Cleaning up all sessions...")
        if self._keepalive:
            self._keepalive.stop()
        for session_id in list(self._sessions.keys()):
            await self._cleanup_session(session_id)

        if self._playwright:
            await self._playwright.stop()
            _LOGGER.info("Playwright stopped")
