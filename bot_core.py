# bot_core.py
import asyncio, datetime as dt, re, secrets, zoneinfo
from typing import Callable, Iterable
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

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

    # Others — added as their vendor HTML comes in
}


def gen_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


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

    async def _write_new_code_in_admin(self, admin_page, code_id: str,
                                       new_code: str, mark_available: bool):
        btn = admin_page.locator(f'#soldOutCodeTable button#editCode[data-id="{code_id}"]')
        await btn.scroll_into_view_if_needed()
        await btn.click()
        await admin_page.wait_for_selector("#viewCode.show", timeout=10000)

        form = admin_page.locator("#viewCodeContent #updateCodeForm")
        await form.locator('input[name="code"]').fill(new_code)
        if mark_available:
            await form.locator('select[name="markas"]').select_option("Available")

        await form.locator('button[type="submit"]').click()
        await admin_page.wait_for_selector("#viewCode.show", state="detached", timeout=10000)

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
        raise RuntimeError(f"unknown vendor kind: {kind!r}")

    async def _vendor_change_password(self, page, cfg: dict, old_pw: str, new_pw: str):
        kind = cfg["kind"]
        if kind == "amt":
            return await self._vendor_change_amt(page, old_pw, new_pw)
        if kind == "galaxy":
            return await self._vendor_change_galaxy(page, cfg, old_pw, new_pw)
        if kind == "unlocktool":
            return await self._vendor_change_unlocktool(page, cfg, old_pw, new_pw)
        raise RuntimeError(f"unknown vendor kind: {kind!r}")

    # ---------------- Android Multi Tool ----------------

    async def _vendor_login_amt(self, ctx, cfg, email, password):
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            self.log(f"[vendor] goto {cfg['login_url']}")
            await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)

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

        self.log("[vendor] waiting for account-page Turnstile token")
        try:
            await page.wait_for_function(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return el && el.value && el.value.length > 20;
                }""",
                timeout=15000,
            )
            self.log("[vendor] account-page Turnstile OK")
        except PWTimeout:
            self.log("[vendor] account-page Turnstile not solved — clicking anyway")

        self.log("[vendor] clicking Save Changes button")
        clicked = False
        for sel in [
            'button.btn.btn-icon.btn-success:has-text("Save Changes")',
            'button[onclick*="change_details"]',
            'button:has-text("Save Changes")',
        ]:
            try:
                btn = page.locator(sel).first
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=5000)
                clicked = True
                self.log(f"[vendor] Save clicked via {sel!r}")
                break
            except Exception:
                continue

        if not clicked:
            self.log("[vendor] button click failed — invoking change_details() as fallback")
            await page.evaluate(
                "typeof change_details === 'function' ? change_details() : null"
            )

        await asyncio.sleep(5)
        try:
            await page.screenshot(path=f"vendor_after_save_{new_pw}.png")
        except Exception:
            pass

    # ---------------- Galaxy Multi Tool ----------------

    async def _vendor_login_galaxy(self, ctx, cfg, email, password):
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            self.log(f"[vendor] goto {cfg['login_url']}")
            await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)
            await page.fill("#email", email)
            await page.fill("#password", password)
            await page.click('form button[type="submit"]:has-text("Login")')

            try:
                await page.wait_for_selector(
                    'button[aria-haspopup="menu"], a[href="/account"]',
                    timeout=25000,
                )
                self.log(f"[vendor] login OK: {email}")
                return page
            except PWTimeout:
                await page.screenshot(path=f"vendor_login_fail_{email}.png")
                raise RuntimeError(f"Galaxy login failed. url={page.url!r}")
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
            raise

    async def _vendor_change_galaxy(self, page, cfg, old_pw, new_pw):
        self.log("[vendor] navigating to account page")
        await page.goto(cfg["account_url"], wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        self.log("[vendor] waiting for Security tab")
        try:
            await page.wait_for_selector(
                'button[role="tab"]:has-text("Security")', timeout=10000
            )
        except PWTimeout:
            self.log("[vendor] Security tab not visible — burger fallback")
            try:
                await page.screenshot(path="galaxy_pre_burger.png")
            except Exception:
                pass

            opened = False
            for sel in [
                'button[aria-controls*="radix"]:has(svg.lucide-menu)',
                'button:has(span.sr-only:text("Toggle Menu"))',
                'button:has(svg.lucide-menu)',
                'header button[aria-haspopup="dialog"]',
                'header button[aria-haspopup="menu"]',
            ]:
                try:
                    await page.click(sel, timeout=3000)
                    opened = True
                    self.log(f"[vendor] burger clicked via {sel!r}")
                    break
                except Exception:
                    continue
            if not opened:
                try:
                    await page.screenshot(path="galaxy_burger_fail.png")
                except Exception:
                    pass
                raise RuntimeError("could not open burger menu")

            await page.wait_for_selector("a[href='/account']", timeout=8000)
            await page.click("a[href='/account']")
            await page.wait_for_selector(
                'button[role="tab"]:has-text("Security")', timeout=15000
            )

        self.log("[vendor] clicking Security tab")
        await page.click('button[role="tab"]:has-text("Security")')

        self.log("[vendor] clicking Change Password")
        await page.click('button:has-text("Change Password")', timeout=10000)

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

        self.log("[vendor] submitting")
        await page.click('button[type="submit"]:has-text("Update Password")')
        await asyncio.sleep(4)
        try:
            await page.screenshot(path=f"galaxy_after_save_{new_pw}.png")
        except Exception:
            pass

    # ---------------- UNLOCK Tool ----------------

    async def _vendor_login_unlocktool(self, ctx, cfg, email, password):
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        try:
            self.log(f"[vendor] goto {cfg['login_url']}")
            await page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)

            # Wait for Cloudflare Turnstile token
            self.log("[vendor] waiting for Turnstile token")
            try:
                await page.wait_for_function(
                    """() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el && el.value && el.value.length > 20;
                    }""",
                    timeout=20000,
                )
                self.log("[vendor] Turnstile OK")
            except PWTimeout:
                self.log("[vendor] Turnstile not detected — continuing")

            # UNLOCK Tool login uses name= attributes, not id=
            await page.fill('input[name="username"]', email)
            await page.fill('input[name="password"]', password)

            self.log("[vendor] clicking Login")
            await page.click('button[type="submit"]:has-text("Login")')

            # Success = the navbar user dropdown appears
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
        # Click the navbar dropdown, then the Change password link
        self.log("[vendor] opening user dropdown")
        try:
            await page.click('a#navbarDropdown', timeout=10000)
            await page.wait_for_selector('a.dropdown-item[href="/password-change/"]', timeout=5000)
            self.log("[vendor] clicking Change password link")
            await page.click('a.dropdown-item[href="/password-change/"]')
        except Exception as e:
            self.log(f"[vendor] dropdown click failed ({e}) — navigating directly")
            await page.goto(cfg["account_url"], wait_until="domcontentloaded", timeout=30000)

        # Wait for the Django password form fields
        self.log("[vendor] waiting for password form")
        await page.wait_for_selector('input[name="old_password"]',    timeout=20000)
        await page.wait_for_selector('input[name="new_password1"]',   timeout=20000)
        await page.wait_for_selector('input[name="new_password2"]',   timeout=20000)

        self.log("[vendor] filling form")
        await page.fill('input[name="old_password"]',  old_pw)
        await page.fill('input[name="new_password1"]', new_pw)
        await page.fill('input[name="new_password2"]', new_pw)

        # Wait for Turnstile on the change page, if present
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

        # Look for Django messages
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

    # ============================================================
    # Orchestration
    # ============================================================

    async def _rotate_row(self, admin_page, vendor_ctx, row, hold: dt.timedelta,
                          inv: str, mark_available: bool) -> bool:
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

        new_pw   = gen_password()
        new_code = f"🆔 {user} 🔑 {new_pw}"
        self.current_user = user
        self.log(f"[rotating] [{inv}] {user}  old password = {old_pw}  "
                 f"->  new password = {new_pw}  (age={age})")

        cfg = VENDOR_CONFIGS.get(inv)

        if cfg:
            vendor_page = None
            try:
                vendor_page = await self._vendor_login(vendor_ctx, cfg, user, old_pw)
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

        await self._write_new_code_in_admin(admin_page, row["id"], new_code, mark_available)
        self.log(f"[admin-pass] [{inv}] {user}  ->  "
                 f"ADMIN code set to: {new_code}")
        self.log(f"[ok] [{inv}] {user} rotated (mark_available={mark_available})")
        self.current_user = None
        return True

    async def _process_inventory(self, admin_page, vendor_ctx, inv: str,
                                 hold: dt.timedelta, limit: int,
                                 mark_available: bool) -> int:
        rotated = 0
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
            self.log(f"[all done] total {total}")
            return total