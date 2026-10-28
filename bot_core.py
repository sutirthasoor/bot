# bot_core.py
import asyncio, datetime as dt, re, secrets, zoneinfo
from typing import Callable, Iterable
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from playwright_turnstile import solve_turnstile  # <-- new (only used in AMT password-change)

CDP_URL = "http://localhost:9222"
TZ      = zoneinfo.ZoneInfo("Asia/Kolkata")
CODE_RE = re.compile(r"🆔\s*(?P<user>\S+?)\s*🔑\s*(?P<pw>\S+)")


# ============================================================
# VENDOR CONFIG — one entry per inventory that has a known vendor site.
# ============================================================
VENDOR_CONFIGS = {
    # ---- Android Multi Tool ----
    "Android Multi Tool[AMT]__RENT": {
        "kind":         "amt",
        "login_url":    "https://androidmultitool.com/controller/login",
        "account_url":  "https://androidmultitool.com/controller/account/",
    },
    "Android Multi Tool[AMT]__RENTDEMO": {
        "kind":         "amt",
        "login_url":    "https://androidmultitool.com/controller/login",
        "account_url":  "https://androidmultitool.com/controller/account/",
    },

    # ---- Galaxy Multi Tool ----
    "Galaxy Multi Tool": {
        "kind":         "galaxy",
        "login_url":    "https://galaxyunlock.in/login",
        "account_url":  "https://galaxyunlock.in/account",
    },
    "Galaxy Multi ToolDEMO": {
        "kind":         "galaxy",
        "login_url":    "https://galaxyunlock.in/login",
        "account_url":  "https://galaxyunlock.in/account",
    },

    # ---- UNLOCK Tool ----
    "UNLOCK___TOOL__RENT": {
        "kind":         "unlocktool",
        "login_url":    "https://unlocktool.net/post-in/",
        "account_url":  "https://unlocktool.net/password-change/",
    },
    "UNLOCK___TOOL__RENTDEMO": {
        "kind":         "unlocktool",
        "login_url":    "https://unlocktool.net/post-in/",
        "account_url":  "https://unlocktool.net/password-change/",
    },

    # ---- TFM Tool ----
    "TFM--TOOL": {
        "kind":        "tfm",
        "login_url":   "https://beta.tfmtool.com/auth/login",
        "account_url": "https://beta.tfmtool.com/dashboard/profile",
    },

    # Others — added as their vendor HTML comes in
}


# ============================================================
# REMOTE SERVICE MAPPING
# ============================================================
REMOTE_SERVICE_MAP = {
    "Android Multi Tool[AMT]__RENT":     "AMT ( Android Multi Tool ) 2 Hours ❇️ Fast (24x7)",
    "Android Multi Tool[AMT]__RENTDEMO": "AMT ( Android Multi Tool ) 2 Hours ❇️ Fast (24x7)",
    "Galaxy Multi Tool":                 "Galaxy Multi Tool-{RENT 2 Hours}",
    "Galaxy Multi ToolDEMO":             "Galaxy Multi Tool-{RENT 2 Hours}",
    "UNLOCK___TOOL__RENT":               "UNLOCKTOOL LOGIN - 6hrs",
    "UNLOCK___TOOL__RENTDEMO":           "UNLOCKTOOL LOGIN - 6hrs",
    "EFT-PRO":                           "EFT-PRO__RENT{30-45MNT}",
    "TFM--TOOL":                         "TFM Tool Pro 2 Hours ❇️ Fast (24x7",
}


def gen_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def gen_password_tfm() -> str:
    """
    TFM-specific password generator.
    TFM policy: at least 8 chars, with uppercase, lowercase and a digit.
    Uses secrets for cryptographic randomness and avoids ambiguous chars.
    Guarantees at least one of each required class, then shuffles.
    """
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghjkmnpqrstuvwxyz"
    digit = "23456789"
    pool  = upper + lower + digit
    chars = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digit),
    ] + [secrets.choice(pool) for _ in range(5)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s.strip(), "%d-%m-%Y %H:%M").replace(tzinfo=TZ)


def parse_code(code: str):
    m = CODE_RE.search(code)
    return (m.group("user"), m.group("pw")) if m else (None, None)


class Rotator:
    def __init__(self, log: Callable[[str], None]):
        self.log = log
        self.stop_flag = False
        self.current_user = None

    # ============================================================
    # pinfrp admin panel side
    # ============================================================

    async def _close_modal(self, page):
        m = page.locator("#viewInventory.show")
        if await m.count() and await m.first.is_visible():
            try:
                await page.click('#viewInventoryContent button[data-bs-dismiss="modal"]')
                await page.wait_for_selector("#viewInventory.show", state="detached", timeout=5000)
            except Exception:
                pass

    async def _ensure_on_inventory_page(self, page):
        await self._close_modal(page)
        if await page.locator("#inventory_table tbody tr").count() == 0:
            self.log("[state] navigating to Inventory page")
            await page.click('.pc-link[data-dom="load-inventory"]')
            await page.wait_for_selector("#inventory_table tbody tr", timeout=15000)

    async def _list_all_inventories(self, page) -> list:
        rows = page.locator("#inventory_table tbody tr")
        n = await rows.count()
        names = []
        for i in range(n):
            name = (await rows.nth(i).locator("td").nth(0).inner_text()).strip()
            if name:
                names.append(name)
        self.log(f"[inventory-list] found {len(names)} inventories")
        for nm in names:
            self.log(f"[inventory-list]   - {nm}")
        return names

    async def _open_inventory(self, page, name: str):
        await self._close_modal(page)
        self.log(f"opening '{name}'")
        row = page.locator(
            "#inventory_table tbody tr",
            has=page.locator(f"td:text-is('{name}')"),
        ).first
        if await row.count() == 0:
            raise RuntimeError(f"inventory row not found: {name!r}")
        await row.scroll_into_view_if_needed()
        await row.click()
        await page.wait_for_selector("#viewInventory.show", timeout=15000)

    async def _open_sold_out_tab(self, page):
        await page.click('#viewInventoryContent button[data-section="soldout"]')
        await page.wait_for_selector("#soldOutCodeTable tbody tr", timeout=10000)

    async def _read_rows(self, page, inv: str):
        rows = page.locator("#soldOutCodeTable tbody tr")
        n = await rows.count()
        out = []
        for i in range(n):
            r = rows.nth(i)
            cells = r.locator("td")
            cn = await cells.count()
            if cn < 2:
                continue
            try:
                code = (await cells.nth(0).inner_text()).strip()
                ts   = (await cells.nth(1).inner_text()).strip()
            except Exception:
                continue
            id_ = await r.locator("button#editCode").get_attribute("data-id")
            if not code or not ts or not id_:
                continue
            out.append({"id": id_, "code": code, "sold_out_at": ts})
        return out

    async def _write_new_code_in_admin(self, admin_page, inv_name: str, code_id: str,
                                       new_code: str, mark_available: bool):
        """
        Refresh the admin panel page, navigate to Inventory, find the row, and update it.
        """
        self.log(f"[admin] hard-refreshing panel to clear state before updating id={code_id}")
        
        # 1. Reload the page
        await admin_page.reload(wait_until="domcontentloaded", timeout=30000)
        
        # 2. WAIT FOR SIDEBAR: This ensures the site's JS is fully loaded before we click
        self.log("[admin] waiting for sidebar to render...")
        await admin_page.wait_for_selector(".pc-sidebar", timeout=15000)
        await asyncio.sleep(2) # Extra buffer for event listeners
        
        # 3. Navigate to Inventory by clicking the sidebar link directly
        self.log("[state] navigating to Inventory page via sidebar")
        await admin_page.click('.pc-link[data-dom="load-inventory"]')
        
        # 4. Wait for the inventory table to fully load
        await admin_page.wait_for_selector("#inventory_table tbody tr", timeout=15000)
        await asyncio.sleep(1.5) # Let DataTables finish rendering
        
        # 5. Open the specific inventory modal
        await self._open_inventory(admin_page, inv_name)
        
        # 6. Go to Sold Out tab
        await self._open_sold_out_tab(admin_page)
        await asyncio.sleep(1)
        
        # 7. Find the row and click edit
        max_attempts = 3
        last_exc = None

        for attempt in range(1, max_attempts + 1):
            try:
                btn = admin_page.locator(
                    f'#soldOutCodeTable button#editCode[data-id="{code_id}"]'
                )
                if await btn.count() == 0:
                    self.log(f"[admin-retry] row id={code_id} not found — refreshing table")
                    await asyncio.sleep(2)
                    if await btn.count() == 0:
                        raise RuntimeError(f"row id={code_id} not present in Sold Out tab")

                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=5000)

                # Wait for the modal to appear
                await admin_page.wait_for_selector("#viewCode.show", timeout=8000)
                await asyncio.sleep(1)

                form = admin_page.locator("#viewCodeContent #updateCodeForm")
                await form.locator('input[name="code"]').fill(new_code)
                if mark_available:
                    await form.locator('select[name="markas"]').select_option("Available")

                self.log(f"[admin] waiting 2s before clicking submit for id={code_id}")
                await asyncio.sleep(2)

                await form.locator('button[type="submit"]').click()
                await admin_page.wait_for_selector("#viewCode.show", state="detached", timeout=10000)
                
                self.log(f"[admin] Successfully updated id={code_id}")
                return  # success

            except Exception as e:
                last_exc = e
                self.log(f"[admin-retry] id={code_id} attempt {attempt}/{max_attempts} failed: {e!r}")
                await asyncio.sleep(2)

        raise RuntimeError(
            f"admin panel update failed after {max_attempts} attempts "
            f"for id={code_id}: {last_exc!r}"
        )

    # ============================================================
    # Remote service re-connect logic
    # ============================================================

    async def _ensure_remote_connected(self, admin_page, inv: str) -> bool:
        """
        Connects the remote service to this inventory.
        (This is only called if the inventory status is 'Active')
        """
        if self.stop_flag:
            return False

        self.log(f"[connect] [{inv}] connecting remote service...")

        remote_title = REMOTE_SERVICE_MAP.get(inv)
        if not remote_title:
            self.log(f"[connect] [{inv}] no remote service mapping — skipping")
            return False

        # 3a. Navigate to Services List → Remote
        self.log("[connect] navigating to Services List -> Remote")
        try:
            await admin_page.click('.pc-link:has-text("Services List")', timeout=5000)
        except Exception:
            pass
        await admin_page.click('.pc-link[data-dom="load-remote-service"]')
        await admin_page.wait_for_selector("#service_list_table tbody tr", timeout=15000)

        # 3b. Click the <td> containing the remote service name
        self.log(f"[connect] clicking remote service name cell: {remote_title!r}")
        td = admin_page.locator(
            f'#service_list_table tbody td.text-left:text-is("{remote_title}")'
        ).first
        if await td.count() == 0:
            short = remote_title.split("❇")[0].strip()
            self.log(f"[connect] exact match failed — trying contains: {short!r}")
            td = admin_page.locator(
                f'#service_list_table tbody td.text-left:has-text("{short}")'
            ).first
        if await td.count() == 0:
            raise RuntimeError(f"remote service row not found for {remote_title!r}")

        await td.scroll_into_view_if_needed()
        await td.click()

        await admin_page.wait_for_selector('button[data-section="connect"]', timeout=15000)
        self.log("[connect] offcanvas opened")

        # 3c. Click "Connect" section button
        self.log("[connect] clicking Connect section button")
        await admin_page.click('button[data-section="connect"]', timeout=10000)

        # 3d. Ensure "Connect Inventory" tab is active
        self.log("[connect] opening Connect Inventory tab")
        try:
            await admin_page.click('button#inventory-tab', timeout=5000)
        except Exception:
            pass

        # 3e. Choose inventory in dropdown
        self.log(f"[connect] choosing inventory in dropdown: {inv}")
        try:
            await admin_page.click('#select2-inventory-id-container', timeout=8000)
        except Exception:
            await admin_page.click(
                'span.select2-selection__rendered:has-text("Choose Inventory")',
                timeout=5000,
            )
        await admin_page.wait_for_selector('input.select2-search__field', timeout=5000)
        await admin_page.fill('input.select2-search__field', inv)
        await asyncio.sleep(0.5)
        await admin_page.press('input.select2-search__field', 'Enter')
        self.log(f"[connect] inventory '{inv}' chosen")

        # 3f. Click final Connect button
        self.log("[connect] clicking final Connect button")
        await admin_page.click('button#add-custom-price-button', timeout=10000)
        self.log(f"[connect] remote service re-connected for [{inv}]")

        # 3g. Close the offcanvas
        self.log("[connect] closing offcanvas")
        try:
            await admin_page.click(
                'button.btn-close[data-bs-dismiss="offcanvas"]',
                timeout=8000,
            )
            await asyncio.sleep(0.5)
            self.log("[connect] offcanvas closed")
        except Exception as e:
            self.log(f"[connect] close button click failed (non-fatal): {e!r}")
            try:
                await admin_page.keyboard.press("Escape")
            except Exception:
                pass

        # 3h. Navigate back to Inventory
        self.log("[connect] navigating back to Inventory")
        await admin_page.click('.pc-link[data-dom="load-inventory"]')
        await admin_page.wait_for_selector("#inventory_table tbody tr", timeout=15000)
        return True

    # ============================================================
    # Vendor dispatcher
    # ============================================================

    async def _vendor_login(self, ctx, cfg: dict, email: str, password: str):
        kind = cfg["kind"]
        self.log(f"[DIAG] vendor login dispatch: kind={kind!r}")
        if kind == "amt":
            return await self._vendor_login_amt(ctx, cfg, email, password)
        if kind == "galaxy":
            return await self._vendor_login_galaxy(ctx, cfg, email, password)
        if kind == "unlocktool":
            return await self._vendor_login_unlocktool(ctx, cfg, email, password)
        if kind == "tfm":
            return await self._vendor_login_tfm(ctx, cfg, email, password)
        raise RuntimeError(f"unknown vendor kind: {kind!r}")

    async def _vendor_change_password(self, page, cfg: dict, old_pw: str, new_pw: str):
        kind = cfg["kind"]
        if kind == "amt":
            return await self._vendor_change_amt(page, old_pw, new_pw)
        if kind == "galaxy":
            return await self._vendor_change_galaxy(page, cfg, old_pw, new_pw)
        if kind == "unlocktool":
            return await self._vendor_change_unlocktool(page, cfg, old_pw, new_pw)
        if kind == "tfm":
            return await self._vendor_change_tfm(page, cfg, old_pw, new_pw)
        raise RuntimeError(f"unknown vendor kind: {kind!r}")

    # ---------------- Android Multi Tool ----------------

    async def _vendor_login_amt(self, ctx, cfg, email, password):
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            self.log(f"[vendor] goto {cfg['login_url']}")
            await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)

            # ---- If already logged in (redirected to dashboard), log out first ----
            if "/dashboard" in (page.url or ""):
                self.log(f"[vendor] already logged in — redirect detected at {page.url!r}. Logging out.")
                try:
                    logout_btn = page.locator(
                        'button[onclick*="controller/logout"]'
                    ).first
                    if await logout_btn.count() == 0:
                        logout_btn = page.locator(
                            'button:has-text("Logout")'
                        ).first
                    await logout_btn.scroll_into_view_if_needed()
                    await logout_btn.click(timeout=8000)
                except Exception as e:
                    self.log(f"[vendor] logout click failed ({e!r}) — navigating to logout URL directly")
                    try:
                        await page.goto(
                            "http://androidmultitool.com/controller/logout",
                            wait_until="domcontentloaded",
                            timeout=20000,
                        )
                    except Exception:
                        pass

                # Wait for session to clear, then force back onto the login page
                await asyncio.sleep(2)
                try:
                    await page.wait_for_function(
                        "() => !location.href.includes('/dashboard')",
                        timeout=10000,
                    )
                except PWTimeout:
                    pass

                self.log("[vendor] re-navigating to login page after logout")
                await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)
                # If logout silently failed and we bounced back to dashboard, abort loudly
                if "/dashboard" in (page.url or ""):
                    raise RuntimeError(
                        f"AMT still on dashboard after logout attempt — url={page.url!r}"
                    )

            try:
                await page.wait_for_function(
                    """() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 20;
                    }""",
                    timeout=15000,
                )
                self.log("[vendor] Turnstile OK")
            except PWTimeout:
                self.log("[vendor] Turnstile not detected — continuing")

            await page.fill("#email", email)
            await page.fill("#password", password)

            try:
                await page.evaluate(
                    "typeof process_login === 'function' ? process_login() : null"
                )
            except Exception:
                await page.click('button:has-text("Login")')

            await page.wait_for_selector(
                "a[href='https://androidmultitool.com/controller/account/']",
                timeout=20000,
            )
            self.log(f"[vendor] login OK: {email}")
            return page
        except Exception:
            try:
                await page.screenshot(path=f"vendor_login_fail_{email}.png")
            except Exception:
                pass
            try:
                await page.close()
            except Exception:
                pass
            raise

    async def _vendor_change_amt(self, page, old_pw, new_pw):
        try:
            await page.click("a[href='https://androidmultitool.com/controller/account/']")
        except Exception:
            await page.goto("https://androidmultitool.com/controller/account/",
                            wait_until="domcontentloaded")

        await page.wait_for_selector("#password", timeout=20000)
        await page.wait_for_selector("#password_confirm", timeout=20000)

        await page.fill("#password", new_pw)
        await page.fill("#password_confirm", new_pw)

        # Wait for Turnstile widget, solve it
        self.log("[vendor] waiting for Turnstile widget to render...")
        try:
            await page.wait_for_selector('input[name="cf-turnstile-response"]', timeout=10000)
        except PWTimeout:
            self.log("[vendor] cf-turnstile-response input not found — proceeding")

        self.log("[vendor] solving account-page Turnstile via Peak...")
        try:
            await solve_turnstile(
                page,
                api_key="pk_YOUR_PEAK_KEY",
                sitekey="0x4AAAAAAA7_wuwG9YEeNZkY",
            )
            self.log("[vendor] account-page Turnstile token injected")
        except Exception as e:
            self.log(f"[vendor] Turnstile solve failed: {e!r} — continuing")

        # ---- Invoke change_details() directly (bypasses click interception) ----
        self.log("[vendor] invoking change_details() directly")
        try:
            try:
                await page.evaluate("$('#loading').hide()")
            except Exception:
                pass

            has_fn = await page.evaluate("typeof change_details === 'function'")
            self.log(f"[vendor] change_details defined = {has_fn}")
            if not has_fn:
                raise RuntimeError("change_details() is not defined")

            token_val = await page.evaluate(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return el ? el.value.slice(0, 40) : '<no input>';
                }"""
            )
            self.log(f"[vendor] cf-turnstile token prefix = {token_val!r}")

            await page.evaluate("change_details()")
            self.log("[vendor] change_details() invoked")
        except Exception as e:
            self.log(f"[vendor] JS invoke failed ({e!r}) — trying forced click")
            try:
                await page.locator('button[onclick*="change_details"]').click(force=True, timeout=5000)
                self.log("[vendor] forced click succeeded")
            except Exception as e2:
                self.log(f"[vendor] forced click also failed: {e2!r}")
                raise

        # ---- Wait for outcome ----
        self.log("[vendor] waiting for server response...")
        change_ok = False
        try:
            await page.wait_for_function(
                """() => {
                    const loading = document.querySelector('#loading');
                    if (loading && loading.style.display !== 'none') return false;
                    const swal = document.querySelector('.swal2-popup');
                    if (swal && swal.offsetParent !== null) return true;
                    const p = document.querySelector('#password');
                    if (p && p.value === '') return true;
                    return false;
                }""",
                timeout=15000,
            )

            popup_text = ""
            try:
                popup = page.locator(".swal2-popup").first
                if await popup.count() and await popup.is_visible():
                    popup_text = (await popup.inner_text(timeout=2000)).lower()
                    self.log(f"[vendor] AMT popup: {popup_text!r}")
            except Exception:
                pass

            if any(w in popup_text for w in ("success", "updated", "changed", "saved")):
                change_ok = True
            elif any(w in popup_text for w in ("error", "fail", "invalid", "captcha", "turnstile", "verification")):
                change_ok = False
            else:
                try:
                    pw_val = await page.locator("#password").input_value()
                    if pw_val == "":
                        change_ok = True
                except Exception:
                    pass

            try:
                await page.click('.swal2-confirm', timeout=3000)
            except Exception:
                pass
        except PWTimeout:
            self.log("[vendor] no confirmation signal within 15s")

        if not change_ok:
            await page.screenshot(path=f"amt_change_unconfirmed_{new_pw}.png")
            raise RuntimeError(
                "AMT password change could not be confirmed — check "
                f"amt_change_unconfirmed_{new_pw}.png. Likely Turnstile rejection."
            )
        self.log("[vendor] AMT password change confirmed")

        try:
            await page.screenshot(path=f"vendor_after_save_{new_pw}.png")
        except Exception:
            pass

        # ---- Log out of AMT ----
        self.log("[vendor] logging out of AMT")
        try:
            clicked_logout = False
            for sel in [
                'button[onclick*="controller/logout"]',
                'button:has-text("Logout")',
                'button.btn.btn-primary:has(i.fa-right-from-bracket)',
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() == 0:
                        continue
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=5000)
                    clicked_logout = True
                    self.log(f"[vendor] AMT Logout clicked via {sel!r}")
                    break
                except Exception:
                    continue

            if not clicked_logout:
                self.log("[vendor] Logout button not found — going to logout URL")
                try:
                    await page.goto(
                        "http://androidmultitool.com/controller/logout",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                except Exception:
                    pass

            try:
                await page.wait_for_selector("#password", timeout=8000)
                self.log("[vendor] AMT logout OK — login form present")
            except PWTimeout:
                self.log(f"[vendor] AMT logout — current url: {page.url} (could not confirm)")
        except Exception as e:
            self.log(f"[vendor] AMT logout failed (non-fatal): {e!r}")

    # ---------------- Galaxy Multi Tool ----------------

    async def _vendor_login_galaxy(self, ctx, cfg, email, password):
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            self.log(f"[vendor] goto {cfg['login_url']}")
            await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)

            # Make sure we're actually ON the login form before submitting
            await page.wait_for_selector("#email", timeout=15000)
            await page.wait_for_selector("#password", timeout=15000)

            await page.fill("#email", email)
            await page.fill("#password", password)

            # Log what the submit button actually says (helps diagnose text mismatches)
            btn = page.locator('form button[type="submit"]').first
            if await btn.count() == 0:
                btn = page.locator(
                    'button:has-text("Login"), button:has-text("Sign in"), button:has-text("Log in")'
                ).first
            try:
                btn_text = (await btn.inner_text()).strip()
            except Exception:
                btn_text = "<unknown>"
            self.log(f"[vendor] submit button text = {btn_text!r}")

            await btn.click()

            # Wait until we actually leave the login form (URL changes AND #password is gone)
            try:
                await page.wait_for_function(
                    "() => location.pathname !== '/login' && !document.querySelector('#password')",
                    timeout=20000,
                )
            except PWTimeout:
                await page.screenshot(path=f"galaxy_login_still_on_form_{email}.png")
                body_snip = ""
                try:
                    body_snip = (await page.locator("body").inner_text(timeout=2000))[:300]
                except Exception:
                    pass
                raise RuntimeError(
                    f"Galaxy login did not leave the form. url={page.url!r} "
                    f"body[:300]={body_snip!r}"
                )

            self.log(f"[vendor] left login page, now at {page.url}")

            # ---- REAL AUTH CHECK: go to /account and confirm Security tab renders ----
            self.log(f"[vendor] navigating to account page: {cfg['account_url']}")
            await page.goto(cfg["account_url"], wait_until="domcontentloaded", timeout=30000)

            if "/login" in (page.url or ""):
                await page.screenshot(path=f"galaxy_account_bounce_{email}.png")
                raise RuntimeError(
                    f"Galaxy bounced /account -> /login: session not established. url={page.url!r}"
                )

            try:
                await page.wait_for_selector(
                    'button[role="tab"]:has-text("Security")', timeout=15000
                )
            except PWTimeout:
                await page.screenshot(path=f"galaxy_no_security_tab_{email}.png")
                body_snip = ""
                try:
                    body_snip = (await page.locator("body").inner_text(timeout=2000))[:400]
                except Exception:
                    pass
                raise RuntimeError(
                    f"Galaxy /account did not show Security tab — login silently rejected. "
                    f"url={page.url!r} body[:400]={body_snip!r}"
                )

            self.log(f"[vendor] login OK: {email}")
            return page
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            raise

    async def _vendor_change_galaxy(self, page, cfg, old_pw, new_pw):
        # _vendor_login_galaxy already navigated to /account and verified the Security tab
        self.log(f"[vendor] on account page: {page.url}")

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        self.log("[vendor] clicking Security tab")
        await page.click('button[role="tab"]:has-text("Security")')
        await page.wait_for_timeout(500)  # let the tab content mount

        # Log what's on the Security tab so future failures are diagnosable
        try:
            body_snip = (await page.locator("body").inner_text(timeout=2000))[:500]
            self.log(f"[vendor] security tab body[:500] = {body_snip!r}")
        except Exception:
            pass

        self.log("[vendor] clicking Change Password")
        try:
            await page.wait_for_selector('button:has-text("Change Password")', timeout=8000)
            await page.click('button:has-text("Change Password")')
        except PWTimeout:
            # Fallback: maybe it's a link, or different casing
            try:
                await page.click(
                    'button:has-text("Change password"), a:has-text("Change Password")',
                    timeout=5000,
                )
            except Exception:
                await page.screenshot(path="galaxy_change_pw_missing.png")
                raise RuntimeError(
                    "Change Password button not found on Security tab — "
                    "check galaxy_change_pw_missing.png"
                )

        self.log("[vendor] filling form")
        await page.wait_for_selector("#current-pass", timeout=10000)
        await page.wait_for_selector("#new-pass",     timeout=10000)
        await page.wait_for_selector("#confirm-pass", timeout=10000)

        await page.fill("#current-pass", old_pw)
        await page.fill("#new-pass",     new_pw)
        await page.fill("#confirm-pass", new_pw)

        self.log("[vendor] waiting for Update Password to enable")
        try:
            await page.wait_for_selector(
                'button[type="submit"]:has-text("Update Password"):not([disabled])',
                timeout=8000,
            )
        except PWTimeout:
            for sel, val in [("#current-pass", old_pw),
                             ("#new-pass", new_pw),
                             ("#confirm-pass", new_pw)]:
                await page.evaluate(
                    """([sel, val]) => {
                        const el = document.querySelector(sel);
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(el, val);
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                    }""",
                    [sel, val],
                )
            await page.wait_for_selector(
                'button[type="submit"]:has-text("Update Password"):not([disabled])',
                timeout=8000,
            )

        self.log("[vendor] submitting password change")
        await page.click('button[type="submit"]:has-text("Update Password")')
        await asyncio.sleep(4)
        try:
            await page.screenshot(path=f"galaxy_after_save_{new_pw}.png")
        except Exception:
            pass

        # ---- Log out of the Galaxy vendor site before returning to admin ----
        self.log("[vendor] logging out of Galaxy vendor")
        try:
            # 1) Open the avatar dropdown.
            #    The avatar is the span with classes
            #    "flex h-full w-full items-center justify-center rounded-full bg-muted".
            #    It lives inside a trigger button, but the span itself is the visible node.
            avatar_selectors = [
                'button:has(span.rounded-full.bg-muted)',
                'button:has(span:has-text("P"))',
                'header button[aria-haspopup="menu"]',
                'button[aria-haspopup="menu"]',
                'span.flex.h-full.w-full.items-center.justify-center.rounded-full.bg-muted',
                'span.flex.h-full.w-full.items-center.justify-center.rounded-full',
            ]
            opened = False
            for sel in avatar_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=5000)
                    opened = True
                    self.log(f"[vendor] avatar clicked via {sel!r}")
                    break
                except Exception:
                    continue

            if not opened:
                try:
                    await page.screenshot(path="galaxy_logout_avatar_fail.png")
                except Exception:
                    pass
                raise RuntimeError("could not open avatar menu for logout")

            # 2) Click the "Log out" menu item.
            #    Radix renders <div role="menuitem"> with a <span>Log out</span>.
            #    Match on the span text and click the menuitem that contains it.
            logout_selectors = [
                'div[role="menuitem"]:has(span:text-is("Log out"))',
                'div[role="menuitem"]:has-text("Log out")',
                '[role="menuitem"]:has-text("Log out")',
                'text="Log out"',
            ]
            clicked = False
            for sel in logout_selectors:
                try:
                    item = page.locator(sel).first
                    await item.wait_for(state="visible", timeout=6000)
                    await item.scroll_into_view_if_needed()
                    await item.click(timeout=5000)
                    clicked = True
                    self.log(f"[vendor] Log out clicked via {sel!r}")
                    break
                except Exception:
                    continue

            if not clicked:
                try:
                    await page.screenshot(path="galaxy_logout_menuitem_fail.png")
                except Exception:
                    pass
                raise RuntimeError("could not click 'Log out' menu item")

            # 3) Confirm we're actually logged out.
            try:
                await page.wait_for_url("**/login**", timeout=10000)
                self.log("[vendor] logout OK — on login page")
            except PWTimeout:
                # Fallback: a login form appearing also counts as logged out
                try:
                    await page.wait_for_selector("#password", timeout=5000)
                    self.log(f"[vendor] logout OK — login form present. url={page.url}")
                except PWTimeout:
                    self.log(f"[vendor] logout — current url: {page.url} (could not confirm)")
        except Exception as e:
            self.log(f"[vendor] logout step failed (non-fatal): {e!r}")

    # ---------------- UNLOCK Tool ----------------

    async def _vendor_login_unlocktool(self, ctx, cfg, email, password):
        page = await ctx.new_page()
        page.set_default_timeout(30000)
        try:
            self.log(f"[vendor] goto {cfg['login_url']}")
            await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)

            self.log("=" * 60)
            self.log("[vendor] ⚠️  ACTION REQUIRED")
            self.log("[vendor] Cloudflare is showing a verification challenge.")
            self.log("[vendor] Please switch to the Chrome Bot window and:")
            self.log("[vendor]   1. Wait for the 'Verify you are human' checkbox")
            self.log("[vendor]   2. Click it if it doesn't auto-solve")
            self.log("[vendor]   3. Wait for it to pass (green checkmark)")
            self.log("[vendor] The bot is waiting — 2 minute limit.")
            self.log("=" * 60)

            waited = 0
            token_present = False
            # MAX 120 seconds wait
            while waited < 120 and not self.stop_flag:
                try:
                    token_present = await page.evaluate(
                        """() => {
                            const el = document.querySelector('input[name="cf-turnstile-response"]');
                            return el && el.value && el.value.length > 20;
                        }"""
                    )
                except Exception:
                    token_present = False

                if token_present:
                    self.log(f"[vendor] ✓ Turnstile passed after {waited}s")
                    break

                if waited % 30 == 0 and waited > 0:
                    self.log(f"[vendor] still waiting for Turnstile... ({waited}s / 120s)")

                await asyncio.sleep(2)
                waited += 2

            if not token_present:
                self.log("[vendor] Turnstile wait timed out (2 minutes). Skipping this tool.")
                raise RuntimeError("UNLOCK Tool Cloudflare timed out after 2 minutes")

            if self.stop_flag:
                self.log("[vendor] stop requested during Cloudflare wait — aborting")
                raise RuntimeError("Stopped during Cloudflare wait")

            await page.fill('input[name="username"]', email)
            await page.fill('input[name="password"]', password)

            self.log("[vendor] clicking Login")
            await page.click('button[type="submit"]:has-text("Login")')

            self.log("[vendor] waiting for dashboard (navbar user menu)")
            try:
                await page.wait_for_selector(
                    'a#navbarDropdown, a.nav-link.dropdown-toggle',
                    timeout=25000,
                )
                self.log(f"[vendor] login OK: {email}")
                return page
            except PWTimeout:
                await page.screenshot(path=f"vendor_login_fail_{email}.png")
                raise RuntimeError(f"UNLOCK Tool login failed. url={page.url!r}")
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            raise

    async def _vendor_change_unlocktool(self, page, cfg, old_pw, new_pw):
        self.log("[vendor] opening user dropdown")
        try:
            await page.click('a#navbarDropdown', timeout=10000)
            await page.wait_for_selector('a.dropdown-item[href="/password-change/"]', timeout=5000)
            self.log("[vendor] clicking Change password link")
            await page.click('a.dropdown-item[href="/password-change/"]')
        except Exception as e:
            self.log(f"[vendor] dropdown click failed ({e}) — navigating directly")
            await page.goto(cfg["account_url"], wait_until="domcontentloaded", timeout=30000)

        self.log("[vendor] waiting for password form")
        await page.wait_for_selector('input[name="old_password"]',    timeout=20000)
        await page.wait_for_selector('input[name="new_password1"]',   timeout=20000)
        await page.wait_for_selector('input[name="new_password2"]',   timeout=20000)

        self.log("[vendor] filling form")
        await page.fill('input[name="old_password"]',  old_pw)
        await page.fill('input[name="new_password1"]', new_pw)
        await page.fill('input[name="new_password2"]', new_pw)

        try:
            await page.wait_for_function(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return !el || (el.value && el.value.length > 20);
                }""",
                timeout=10000,
            )
        except PWTimeout:
            pass

        self.log("[vendor] clicking Change password button")
        clicked = False
        for sel in [
            'button[type="submit"]:has-text("Change password")',
            'form button[type="submit"]',
        ]:
            try:
                btn = page.locator(sel).first
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=5000)
                clicked = True
                self.log(f"[vendor] submit clicked via {sel!r}")
                break
            except Exception:
                continue

        if not clicked:
            raise RuntimeError("UNLOCK Tool change-password button not found")

        await asyncio.sleep(4)
        try:
            await page.screenshot(path=f"unlocktool_after_save_{new_pw}.png")
        except Exception:
            pass

        try:
            body = (await page.locator("body").inner_text(timeout=3000)).lower()
            if any(w in body for w in ("password changed", "successfully",
                                        "updated", "password has been")):
                self.log("[vendor] page confirms password change")
            elif any(w in body for w in ("error", "does not match",
                                          "too similar", "too short", "incorrect")):
                self.log("[vendor] page mentions error — check screenshot")
        except Exception:
            pass

        # ---- Log out of UNLOCK Tool before returning to admin ----
        self.log("[vendor] logging out of UNLOCK Tool")
        try:
            try:
                await page.goto("https://unlocktool.net/",
                                wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass

            await page.wait_for_selector('a#navbarDropdown', timeout=8000)
            await page.click('a#navbarDropdown')
            self.log("[vendor] dropdown clicked")

            await page.wait_for_selector('a.dropdown-item[href="/logout/"]', timeout=8000)
            await page.click('a.dropdown-item[href="/logout/"]')
            self.log("[vendor] Log out clicked")

            try:
                await page.wait_for_selector('a[href="/post-in/"]', timeout=10000)
                self.log("[vendor] logout OK — login link present")
            except PWTimeout:
                self.log(f"[vendor] logout — current url: {page.url}")
        except Exception as e:
            self.log(f"[vendor] logout step failed (non-fatal): {e!r}")

    # ---------------- TFM Tool ----------------

    async def _vendor_login_tfm(self, ctx, cfg, email, password):
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            self.log(f"[vendor] goto {cfg['login_url']}")
            await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)

            # Wait for the login form to render (Nuxt SPA hydration)
            await page.wait_for_selector('input[name="email"]', timeout=20000)
            await page.wait_for_selector('input[name="password"]', timeout=20000)

            # TFM's Turnstile auto-solves on load. Wait for the hidden token to be populated.
            self.log("[vendor] waiting for Turnstile token...")
            try:
                await page.wait_for_function(
                    """() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 20;
                    }""",
                    timeout=20000,
                )
                self.log("[vendor] TFM Turnstile OK")
            except PWTimeout:
                self.log("[vendor] TFM Turnstile not detected — continuing anyway")

            # Fill credentials (values may be pre-populated in the SSR payload
            # but playwright .fill() replaces them cleanly).
            await page.fill('input[name="email"]', email)
            await page.fill('input[name="password"]', password)

            # Find the submit button — it's the big primary "Log in" button
            self.log("[vendor] submitting login")
            btn = page.locator('form button[type="submit"]').first
            if await btn.count() == 0:
                btn = page.locator('button[type="submit"]:has-text("Log in")').first
            await btn.scroll_into_view_if_needed()
            await btn.click()

            # Wait until we actually leave /auth/login AND the login form is gone.
            try:
                await page.wait_for_function(
                    """() => !location.pathname.startsWith('/auth/login')
                            && !document.querySelector('input[name="password"]')""",
                    timeout=25000,
                )
            except PWTimeout:
                await page.screenshot(path=f"tfm_login_still_on_form_{email}.png")
                body = ""
                try:
                    body = (await page.locator("body").inner_text(timeout=2000))[:400]
                except Exception:
                    pass
                raise RuntimeError(
                    f"TFM login did not leave /auth/login. url={page.url!r} body[:400]={body!r}"
                )

            self.log(f"[vendor] TFM left login page, now at {page.url}")

            # ---- REAL AUTH CHECK: go to profile and confirm avatar menu button exists ----
            self.log(f"[vendor] navigating to profile: {cfg['account_url']}")
            await page.goto(cfg["account_url"], wait_until="domcontentloaded", timeout=30000)

            if "/auth/login" in (page.url or ""):
                await page.screenshot(path=f"tfm_profile_bounce_{email}.png")
                raise RuntimeError(
                    f"TFM bounced /dashboard/profile -> /auth/login: session not established. "
                    f"url={page.url!r}"
                )

            # The avatar button in the header is a strong "logged in" signal.
            try:
                await page.wait_for_selector(
                    'button:has(span.rounded-full) svg, '
                    'button[type="button"]:has(span.rounded-full.border-border-subtle)',
                    timeout=15000,
                )
            except PWTimeout:
                await page.screenshot(path=f"tfm_no_avatar_{email}.png")
                body = ""
                try:
                    body = (await page.locator("body").inner_text(timeout=2000))[:500]
                except Exception:
                    pass
                raise RuntimeError(
                    f"TFM /dashboard/profile did not show avatar menu — login silently rejected. "
                    f"url={page.url!r} body[:500]={body!r}"
                )

            self.log(f"[vendor] login OK: {email}")
            return page
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            raise

    async def _vendor_change_tfm(self, page, cfg, old_pw, new_pw):
        # _vendor_login_tfm already navigated to /dashboard/profile
        self.log(f"[vendor] on profile page: {page.url}")

        try:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass

            self.log("[vendor] clicking Change password button")
            try:
                await page.wait_for_selector(
                    'button:has-text("Change password")', timeout=10000
                )
                await page.click('button:has-text("Change password")')
            except PWTimeout:
                await page.screenshot(path="tfm_change_pw_missing.png")
                raise RuntimeError(
                    "Change password button not found on TFM profile — "
                    "check tfm_change_pw_missing.png"
                )

            self.log("[vendor] waiting for Change password modal")
            await page.wait_for_selector("#modal-current-password", timeout=15000)
            await page.wait_for_selector("#modal-new-password",     timeout=15000)
            await page.wait_for_selector("#modal-confirm-password", timeout=15000)

            self.log("[vendor] filling form")
            await page.fill("#modal-current-password", old_pw)
            await page.fill("#modal-new-password",     new_pw)
            await page.fill("#modal-confirm-password", new_pw)

            self.log("[vendor] clicking Update password")
            submit_sel = 'button:has-text("Update password")'
            try:
                await page.wait_for_selector(submit_sel, timeout=8000)
                await page.click(submit_sel)
            except Exception as e:
                self.log(f"[vendor] Update password click failed ({e!r}) — forcing")
                await page.locator(submit_sel).first.click(force=True, timeout=5000)

            # Wait for the modal to close — TFM's success behaviour. If it
            # doesn't close, just log and move on; the admin-panel update is
            # what matters, and the finally below always logs out.
            self.log("[vendor] waiting for modal to close...")
            try:
                await page.wait_for_function(
                    """() => !document.querySelector('#modal-current-password')""",
                    timeout=25000,
                )
                self.log("[vendor] TFM password change confirmed (modal closed)")
            except PWTimeout:
                self.log("[vendor] modal did not close within 25s — "
                         "proceeding to admin update anyway")
        finally:
            # ---- Always log out of TFM before returning to admin ----
            await self._tfm_logout(page, cfg)

    async def _tfm_logout(self, page, cfg):
        """Robust TFM logout. Called from the finally of _vendor_change_tfm
        so it runs whether the password change succeeded or not."""
        self.log("[vendor] logging out of TFM")
        try:
            # 1) Make sure we're on the profile page (SPA may have navigated)
            if "/dashboard/profile" not in (page.url or ""):
                self.log(f"[vendor] re-navigating to profile for logout: {page.url}")
                try:
                    await page.goto(cfg["account_url"],
                                    wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass

            # 2) Click the exact avatar button in the header
            self.log("[vendor] clicking TFM avatar button")
            avatar_btn = page.locator(
                'header button[type="button"]:has(span.rounded-full)'
            ).first
            if await avatar_btn.count() == 0:
                avatar_btn = page.locator(
                    'button[type="button"]:has(span.rounded-full)'
                ).first
            if await avatar_btn.count() == 0:
                try:
                    await page.screenshot(path="tfm_logout_avatar_fail.png")
                except Exception:
                    pass
                raise RuntimeError("TFM avatar button not found")

            await avatar_btn.scroll_into_view_if_needed()
            await avatar_btn.click(timeout=6000)

            # 3) Wait for the "Log out" row to render, then click it
            self.log("[vendor] waiting for Log out menu row to appear")
            logout_candidates = [
                'div.cursor-pointer:has-text("Log out")',
                'div.cursor-pointer:has(svg):has-text("Log out")',
                'div:has(svg):has-text("Log out")',
                'div[role="menuitem"]:has-text("Log out")',
                '[role="menuitem"]:has-text("Log out")',
            ]
            clicked = False
            for sel in logout_candidates:
                try:
                    item = page.locator(sel).first
                    await item.wait_for(state="visible", timeout=8000)
                    await item.scroll_into_view_if_needed()
                    await item.click(timeout=5000)
                    clicked = True
                    self.log(f"[vendor] TFM Log out clicked via {sel!r}")
                    break
                except Exception as e:
                    self.log(f"[vendor] logout selector {sel!r} failed: {e!r}")
                    continue

            if not clicked:
                try:
                    await page.screenshot(path="tfm_logout_menuitem_fail.png")
                except Exception:
                    pass
                raise RuntimeError("could not click TFM 'Log out' menu item")

            # 4) Confirm logout
            self.log("[vendor] waiting for login form after logout")
            try:
                await page.wait_for_function(
                    """() => location.pathname.startsWith('/auth/login')
                            || !!document.querySelector('input[name="password"]')""",
                    timeout=15000,
                )
                self.log(f"[vendor] TFM logout OK — url={page.url}")
                return
            except PWTimeout:
                pass

            # 5) Force-navigate to login page as last resort
            try:
                await page.goto(cfg["login_url"],
                                wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_selector('input[name="password"]', timeout=8000)
                self.log("[vendor] TFM logout OK (forced re-nav) — login form present")
                return
            except Exception as e:
                self.log(f"[vendor] TFM logout — forced re-nav failed: {e!r}")

            try:
                await page.screenshot(path="tfm_logout_unconfirmed.png")
            except Exception:
                pass
            self.log(f"[vendor] TFM logout — current url: {page.url} (could not confirm)")
        except Exception as e:
            self.log(f"[vendor] TFM logout step failed (non-fatal): {e!r}")

    # ============================================================
    # Orchestration
    # ============================================================

    async def _rotate_row(self, admin_page, vendor_ctx, row, hold: dt.timedelta,
                          inv: str, mark_available: bool) -> bool:
        if self.stop_flag:
            self.log("[stop] aborting before rotate")
            return False

        user, old_pw = parse_code(row["code"])
        if not user:
            self.log(f"[skip] unparseable: {row['code']!r}")
            return False
        try:
            sold = parse_ts(row["sold_out_at"])
        except Exception as e:
            self.log(f"[skip] bad ts {row['sold_out_at']!r}: {e}")
            return False

        age = dt.datetime.now(TZ) - sold
        if age < hold:
            self.log(f"[wait] [{inv}] {user} — sold {age} ago")
            return False

        cfg = VENDOR_CONFIGS.get(inv)
        is_tfm = bool(cfg and cfg.get("kind") == "tfm")

        # TFM has its own stricter password policy. Other vendors keep the
        # existing generator unchanged.
        new_pw = gen_password_tfm() if is_tfm else gen_password()
        new_code = f"🆔 {user} 🔑 {new_pw}"

        self.current_user = user
        self.log(f"[rotating] [{inv}] {user}  old password = {old_pw}  "
                 f"->  new password = {new_pw}  (age={age})")

        if self.stop_flag:
            self.log(f"[stop] aborting rotation of {user} — before vendor change")
            self.current_user = None
            return False

        if cfg:
            vendor_page = None
            try:
                vendor_page = await self._vendor_login(vendor_ctx, cfg, user, old_pw)

                if self.stop_flag:
                    self.log(f"[stop] aborting rotation of {user} — after vendor login")
                    try:
                        await vendor_page.close()
                    except Exception:
                        pass
                    self.current_user = None
                    return False

                await self._vendor_change_password(vendor_page, cfg, old_pw, new_pw)
                self.log(f"[vendor-pass] [{inv}] {user}  ->  "
                         f"VENDOR password set to: {new_pw}")
            except Exception as e:
                self.log(f"[vendor-fail] [{inv}] {user}: {e!r}")
                return False
            finally:
                if vendor_page:
                    try:
                        await vendor_page.close()
                    except Exception:
                        pass
        else:
            self.log(f"[vendor-skip] [{inv}] {user} — admin panel only")

        if self.stop_flag:
            self.log(f"[stop] aborting rotation of {user} — before admin update")
            self.current_user = None
            return False

        # ---- Admin panel update (with retry + loud sync-fail logging) ----
        try:
            await self._write_new_code_in_admin(admin_page, inv, row["id"], new_code, mark_available)
        except Exception as e:
            self.log(f"[SYNC-FAIL] [{inv}] {user} — vendor password is {new_pw} "
                     f"but ADMIN panel was NOT updated")
            self.log(f"[SYNC-FAIL] [{inv}] {user} — reason: {e!r}")
            self.log(f"[SYNC-FAIL] [{inv}] {user} — MANUAL FIX REQUIRED: update admin "
                     f"code to 🆔 {user} 🔑 {new_pw}")
            self.current_user = None
            return False

        self.log(f"[admin-pass] [{inv}] {user}  ->  "
                 f"ADMIN code set to: {new_code}")
        self.log(f"[ok] [{inv}] {user} rotated (mark_available={mark_available})")
        self.current_user = None
        return True

    async def _process_inventory(self, admin_page, vendor_ctx, inv: str,
                                 hold: dt.timedelta, limit: int,
                                 mark_available: bool) -> int:
        rotated = 0

        # ---- 1. ROTATE ----
        await self._open_inventory(admin_page, inv)
        try:
            await self._open_sold_out_tab(admin_page)
            rows = await self._read_rows(admin_page, inv)
            limit_str = str(limit) if limit > 0 else "unlimited"
            self.log(f"[scan] [{inv}] {len(rows)} sold-out row(s) "
                     f"(limit={limit_str}, mark_available={mark_available})")
            for row in rows:
                if self.stop_flag:
                    self.log("[stop] break requested")
                    break
                if limit > 0 and rotated >= limit:
                    self.log(f"[limit] [{inv}] reached {limit} rotation(s)")
                    break
                try:
                    if await self._rotate_row(admin_page, vendor_ctx, row, hold,
                                              inv, mark_available):
                        rotated += 1
                except Exception as e:
                    self.log(f"[fail] [{inv}] id={row['id']}: {e!r}")
                await asyncio.sleep(1.5)
        finally:
            await self._close_modal(admin_page)

        # NOTE: Inventory connection is NO LONGER done here. It is done at the very end of run_once.
        return rotated

    async def run_once(self, targets: Iterable) -> int:
        self.stop_flag = False
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            ctx     = browser.contexts[0]

            admin_page = ctx.pages[0]
            self.log(f"[DIAG] admin tab url = {admin_page.url}")
            await self._ensure_on_inventory_page(admin_page)

            expanded = []
            for t in targets:
                if t["name"] == "*":
                    all_invs = await self._list_all_inventories(admin_page)
                    for nm in all_invs:
                        expanded.append({
                            "name": nm,
                            "hours": t["hours"],
                            "limit": t.get("limit", 0),
                            "mark_available": t.get("mark_available", True),
                        })
                else:
                    expanded.append(t)

            self.log(f"[plan] {len(expanded)} inventory/inventories to process")

            total = 0
            for t in expanded:
                if self.stop_flag:
                    break
                inv            = t["name"]
                hold           = dt.timedelta(hours=float(t["hours"]))
                limit          = int(t.get("limit") or 0)
                mark_available = bool(t.get("mark_available", True))
                try:
                    n = await self._process_inventory(
                        admin_page, ctx, inv, hold, limit, mark_available
                    )
                    total += n
                    self.log(f"[done] [{inv}] rotated {n}")
                except Exception as e:
                    self.log(f"[inventory-fail] {inv!r}: {e!r}")

            # ---- 2. FINAL STEP: CHECK STATUS & RECONNECT ACTIVE INVENTORIES ----
            self.log(f"[connect] checking status and connecting active inventories...")
            
            # Ensure we are on the main inventory table before reading statuses
            await self._ensure_on_inventory_page(admin_page)
            await admin_page.wait_for_selector("#inventory_table tbody tr", timeout=15000)
            
            for t in expanded:
                inv_to_connect = t["name"]
                if self.stop_flag:
                    break
                
                # Find the table row for this inventory
                row_locator = admin_page.locator(
                    "#inventory_table tbody tr",
                    has=admin_page.locator(f"td:text-is('{inv_to_connect}')")
                ).first
                
                if await row_locator.count() == 0:
                    self.log(f"[connect] [{inv_to_connect}] row not found on main page — skipping")
                    continue
                    
                # Extract the status text (e.g., "Active" or "Empty")
                try:
                    # The status is in the last <td> as a badge
                    status_text = await row_locator.locator("td").last.inner_text()
                    status_text = status_text.strip()
                except Exception as e:
                    self.log(f"[connect] [{inv_to_connect}] could not read status: {e}")
                    continue
                
                if "Active" in status_text:
                    self.log(f"[connect] [{inv_to_connect}] is Active — proceeding to connect")
                    try:
                        reconnected = await self._ensure_remote_connected(admin_page, inv_to_connect)
                        if reconnected:
                            self.log(f"[connect] [{inv_to_connect}] re-connected — waiting 10s for codes to refresh")
                            await asyncio.sleep(10)
                    except Exception as e:
                        self.log(f"[connect-fail] [{inv_to_connect}] {e!r}")
                else:
                    self.log(f"[connect] [{inv_to_connect}] is Empty — skipping connection")

            self.log(f"[all done] total {total}")
            return total