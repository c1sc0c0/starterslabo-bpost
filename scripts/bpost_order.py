#!/usr/bin/env python3
"""
Create bpost (rebelchalk.com) parcel orders as a guest and hand back a payment link.

Never pays anything: the money step happens on the MultiSafepay page the link opens.

Examples
--------
Price a Shopify order without creating anything:

    python bpost_order.py --shopify 1002 --quote

Dry run (creates the real draft, prints bpost's total, deletes it):

    python bpost_order.py --shopify 1002 --dry-run

Turn it into a payable order and print the checkout link:

    python bpost_order.py --shopify 1002

Explicit recipient, home delivery:

    python bpost_order.py --name "Jane Doe" --street Kammenstraat --number 51 \
        --zip 2000 --city Antwerpen --country BE --weight-kg 3 --delivery address

Batch from JSON:

    python bpost_order.py --json shipments.json --dry-run

See reference.md for the API map and the weight / destination codes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = "https://parcel.bpost.be"
AUTOCOMPLETE = "https://api.mailops.bpost.cloud/roa-info/externalMailingAddressProofingRest/autocomplete"
PAYMENT_DESCRIPTION = "Online aanmaak verzendetiket(ten)"
BASKET_URL = "https://parcel.bpost.be/nl/basket"
BASKET_HINT = "seed sessionStorage.orderId with the order id, then open " + BASKET_URL

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
CONFIG_FILE = Path(os.environ.get("BPOST_CONFIG") or SKILL_DIR / "config.yaml")


def _load_flat_yaml(text: str) -> dict:
    """Flat `key: value` reader (dotted keys allowed) so no YAML dependency is needed."""
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        pass
    data: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip("'\"")
    return data


CFG: dict = _load_flat_yaml(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
SENDER_FILE = Path(
    os.environ.get("BPOST_SENDER_FILE") or CFG.get("sender_file") or SKILL_DIR / "sender.yaml"
)
SESSION_FILE = Path(os.environ.get("BPOST_SESSION") or SKILL_DIR / ".bpost-session")
KEYCHAIN_SERVICE = os.environ.get("BPOST_KEYCHAIN_SERVICE") or CFG.get("keychain_service") or "scout_bpost"
LOGIN_HOST = "https://login.bpost.be"
OUTPUT_DIR = Path(os.environ.get("BPOST_OUTPUT_DIR") or CFG.get("output_dir") or SKILL_DIR / "records")
SHOPIFY_SHOP = (os.environ.get("SHOPIFY_SHOP") or CFG.get("shopify.shop") or "").strip()
SHOPIFY_KEYCHAIN = (
    os.environ.get("SHOPIFY_KEYCHAIN_SERVICE") or CFG.get("shopify.keychain_service") or "scout_shopify"
)

WEIGHT_BANDS = [
    (2.0, "0_2"),
    (5.0, "2_5"),
    (10.0, "5_10"),
    (20.0, "10_20"),
    (30.0, "20_30"),
]
DESTINATIONS = {"pickup": "PICKUP_POINT", "address": "ADDRESS", "locker": "LOCKER"}
DEST_NAME = {"PICKUP_POINT": "pickup point", "ADDRESS": "home address", "LOCKER": "locker"}


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
class BpostError(RuntimeError):
    pass


class BpostGuestClient:
    """Minimal guest client for parcel.bpost.be (no account, no cookies needed)."""

    def __init__(self, lang: str = "nl", timeout: int = 30, cookie: str | None = None):
        self.lang = lang
        self.timeout = timeout
        self.cookie = cookie or None
        self._countries: dict[str, str] | None = None

    def _request(self, method: str, path: str, payload: dict | None = None):
        url = path if path.startswith("http") else BASE + path
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Accept-Language", self.lang)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
            xsrf = _cookie_value(self.cookie, "XSRF-TOKEN")
            if xsrf:
                req.add_header("X-XSRF-TOKEN", xsrf)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:600]
            raise BpostError(f"bpost {method} {path} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise BpostError(f"bpost {method} {path} -> network error: {e.reason}") from e

    def whoami(self) -> dict | None:
        try:
            return self._request("GET", "/customer/user/me")
        except BpostError:
            return None

    # -- catalog helpers ---------------------------------------------------- #
    def catalog(self, country: str, destination: str, lines: list[str] | None = None) -> dict:
        return (
            self._request(
                "POST",
                "/customer/catalog/search",
                {
                    "lang": self.lang.upper(),
                    "country": country,
                    "destination": destination,
                    "useProductLines": lines or ["STANDARD", "LOCKER_TO_LOCKER", "ECONOMY"],
                },
            )
            or {}
        )

    def pickup_points(
        self,
        country: str,
        postal_code: str,
        street: str | None = None,
        destination: str = "PICKUP_POINT",
    ):
        query = {"lang": self.lang, "country": country, "destination": destination, "postalCode": postal_code}
        if street:
            query["streetName"] = street
        payload = self._request("GET", "/customer/location/delivery-points?" + urllib.parse.urlencode(query))
        points = []
        for entry in (payload or {}).get("deliveryPointsList", []):
            sp = entry.get("servicePoint") or {}
            points.append(
                {
                    "id": sp.get("id"),
                    "type": sp.get("type"),
                    "name": sp.get("name"),
                    "street": sp.get("street"),
                    "number": sp.get("number"),
                    "zip": sp.get("zip"),
                    "city": sp.get("city"),
                    "lockerType": sp.get("lockerType"),
                }
            )
        return points

    def normalize_address(self, zip_code: str, city: str, street: str, number: str) -> dict:
        """Best-effort canonicalisation against bpost's address proofing API."""
        out = {"zip": zip_code, "city": city, "street": street, "number": number}
        try:
            q = urllib.parse.urlencode({"id": 1, "q": zip_code, "maxNumberOfSuggestions": 5})
            payload = self._request("GET", f"{AUTOCOMPLETE}/locality?{q}")
            hit = _match(payload, city) or (payload or [None])[0]
            if hit:
                out["zip"] = str(hit.get("postalCode") or zip_code)
                out["city"] = (hit.get("locality") or city).upper()
            q = urllib.parse.urlencode(
                {
                    "id": 3,
                    "q": street,
                    "postalCode": out["zip"],
                    "locality": out["city"],
                    "maxNumberOfSuggestions": 5,
                }
            )
            payload = self._request("GET", f"{AUTOCOMPLETE}/street?{q}")
            hit = _match(payload, street, key="streetName") or (payload or [None])[0]
            if hit:
                out["street"] = (hit.get("streetName") or street).upper()
        except BpostError:
            pass
        return out

    # -- order mutation ----------------------------------------------------- #
    def new_ids(self) -> tuple[str, str]:
        return f"orderId-{uuid.uuid4()}", f"parcelId-{uuid.uuid4()}"

    def add_parcel(self, order_id: str, parcel_id: str, parcel: dict, recipient: dict, sender: dict):
        surcharge = _surcharge_block() if parcel["country_code"] == "BE" else None
        body = {
            "parcelId": parcel_id,
            "orderId": order_id,
            "surcharge": surcharge,
            "parcelDetails": {
                "countryCode": parcel["country_code"],
                "destinationCode": parcel["destination_code"],
                "weightRangeCode": parcel["weight_range_code"],
                "senderReference": parcel.get("reference"),
                "productTypeCode": None,
                "productLineCode": parcel.get("product_line") or "STANDARD",
                "selectedOptions": parcel.get("selected_options"),
            },
        }
        if parcel["country_code"] != "BE":
            body["international"] = {
                "nonDeliveryInstructions": "RETURN_TO_SENDER_NON_PRIORITY",
                "contentDescription": parcel.get("contents") or None,
                "shipmentType": None,
                "parcelValue": parcel.get("value_eur"),
            }
        self._request("POST", "/customer/parcel/product", body)
        payload = {**body, "recipient": recipient}
        self._request("POST", "/customer/parcel/recipient", payload)
        self._request("POST", "/customer/parcel/sender", {**payload, "sender": sender})

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/customer/orders/{order_id}?lang={self.lang}") or {}

    def delete_order(self, order_id: str) -> None:
        self._request("DELETE", f"/customer/orders/{order_id}?lang={self.lang}")

    def payment_link(self, order_id: str) -> str:
        payload = {
            "gateway": "MOLLIE",
            "language": self.lang,
            "orderId": order_id,
            "description": PAYMENT_DESCRIPTION,
        }
        out = self._request("POST", "/customer/payment/orders", payload) or {}
        url = out.get("paymentUrl")
        if not url:
            raise BpostError(f"payment link missing in response: {out}")
        return url


def _match(payload, needle: str, key: str = "locality"):
    needle = (needle or "").lower()
    for item in payload or []:
        if needle and needle in str(item.get(key, "")).lower():
            return item
    return None


def _surcharge_block() -> dict:
    # End-of-year domestic surcharge, as the site sends it (€1, 27/11–24/12).
    return {
        "status": "WARNING",
        "surchargeWarningStartDate": "2026-10-26",
        "surchargeActualStartDate": "2026-11-27",
        "surchargeActualEndDate": "2026-12-24",
        "priceDetails": {"priceExcludingVat": 1, "vat": 0, "priceIncludingVat": 1},
        "vat": 0,
        "price": 1,
    }


# --------------------------------------------------------------------------- #
# shipment shaping
# --------------------------------------------------------------------------- #
POSTAL_RE = re.compile(r"^(?:[A-Z]{1,2}[-\s]?)?(\d{4,5})\s+(.+)$")
STREET_RE = re.compile(
    r"^(?P<street>.*?)[\s,]+(?P<num>\d+[A-Za-z]?(?:\s*[-/]\s*\d+[A-Za-z]?)?)"
    r"(?:\s*(?:bus|box|bte|bis|apt|app|unit)\s*(?P<box>[\w/-]+))?$",
    re.I,
)
COUNTRY_FALLBACK = {
    "belgium": "BE", "belgië": "BE", "belgie": "BE", "belgique": "BE", "belgien": "BE",
    "netherlands": "NL", "nederland": "NL", "pays-bas": "NL", "niederlande": "NL",
    "germany": "DE", "duitsland": "DE", "allemagne": "DE", "deutschland": "DE",
    "france": "FR", "frankrijk": "FR", "frankreich": "FR",
    "luxembourg": "LU", "luxemburg": "LU",
    "united kingdom": "GB", "england": "GB", "great britain": "GB",
    "spain": "ES", "spanje": "ES", "italy": "IT", "italië": "IT",
    "austria": "AT", "oostenrijk": "AT", "portugal": "PT", "poland": "PL", "polen": "PL",
    "denmark": "DK", "denemarken": "DK", "sweden": "SE", "zweden": "SE",
    "ireland": "IE", "ierland": "IE", "finland": "FI", "finnland": "FI",
    "czechia": "CZ", "tsjechië": "CZ", "hungary": "HU", "hongarije": "HU",
}


def _country_map(client: BpostGuestClient) -> dict[str, str]:
    """Country names -> ISO code, from bpost's catalog (cached per client)."""
    if client._countries is not None:
        return client._countries
    mapping = dict(COUNTRY_FALLBACK)
    for lang in ("nl", "en"):
        try:
            rows = client._request("GET", f"/customer/catalog/countries?lang={lang}&includeInactive=true") or []
        except BpostError:
            continue
        for row in rows:
            code = (row.get("code") or "").upper()
            name = (row.get("name") or "").strip().lower()
            if code and name:
                mapping.setdefault(name, code)
    client._countries = mapping
    return mapping


def parse_address(text: str, client: BpostGuestClient | None = None) -> dict:
    """Turn a pasted address into shipment fields.

    Handles the shapes people actually paste:

        Felix Riexinger
        Glockenstr 56
        67655 Kaiserslautern
        Germany

        Jan Peeters, Kammenstraat 51 bus 3, 2000 Antwerpen, België
    """
    parts = [
        p.strip()
        for p in re.split(r"[\n,]+", text or "")
        if p.strip() and not re.fullmatch(r"\d+\s*[º°]", p.strip())
    ]
    if len(parts) < 2:
        raise SystemExit(f"could not read an address from {text!r}")

    countries = _country_map(client) if client else dict(COUNTRY_FALLBACK)
    country = None
    if len(parts) > 2:
        probe = parts[-1].strip().lower()
        if probe in countries:
            country = countries[probe]
            parts = parts[:-1]
        elif len(probe) <= 3 and probe.upper() in countries.values():
            country = probe.upper()
            parts = parts[:-1]

    zip_code = city = None
    for i, part in enumerate(parts):
        match = POSTAL_RE.match(part)
        if match:
            zip_code, city = match.group(1), match.group(2).strip()
            del parts[i]
            break
    if not zip_code:
        raise SystemExit(f"no postcode found in {text!r} (expected something like '2000 Antwerpen')")

    street = number = box = None
    for i, part in enumerate(parts):
        match = STREET_RE.match(part)
        if match and any(ch.isdigit() for ch in part):
            street = match.group("street").strip()
            number = re.sub(r"\s+", "", match.group("num"))
            box = (match.group("box") or "").strip()
            del parts[i]
            break
    if not street:
        raise SystemExit(f"no street + house number found in {text!r}")

    country = country or "BE"
    if len(parts) >= 2:
        first, last = parts[0], " ".join(parts[1:])
    elif parts:
        bits = parts[0].split()
        first, last = bits[0], " ".join(bits[1:]) or bits[0]
    else:
        first, last = "Ontvanger", "Ontvanger"

    return {
        "first_name": first,
        "last_name": last,
        "company": None,
        "street": street,
        "number": number,
        "box": box or "",
        "zip": zip_code,
        "city": city,
        "country_code": country,
    }


def weight_range_code(weight_kg: float, country: str) -> str:
    prefix = "DOM" if country.upper() == "BE" else "INT"
    for limit, band in WEIGHT_BANDS:
        if weight_kg <= limit:
            return f"{prefix}_{band}"
    raise BpostError(f"{weight_kg} kg is above the 30 kg parcel limit")


def split_street(address: str) -> tuple[str, str]:
    """'Glockenstr 56' -> ('Glockenstr', '56'); falls back to ('Glockenstr 56', '')."""
    address = (address or "").strip()
    m = re.match(r"^(.*?)[\s,]+(\d+[A-Za-z]?(?:\s*[-/]\s*\d+[A-Za-z]?)?)$", address)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return address, ""


def _keychain(account: str, service: str | None = None) -> str:
    service = service or KEYCHAIN_SERVICE
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.rstrip("\n")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _form(html: str) -> tuple[str, dict[str, str]]:
    """First <form> on the page: its action and its inputs."""
    match = re.search(r"<form[^>]*action=\"([^\"]+)\"[^>]*>(.*?)</form>", html, re.S | re.I)
    if not match:
        raise BpostError("no login form found on the bpost sign-in page")
    action, inner = match.group(1), match.group(2)
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", inner, re.I):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        kind = re.search(r'type="([^"]+)"', tag)
        if kind and kind.group(1).lower() in ("checkbox", "submit", "button"):
            continue
        fields[name.group(1)] = value.group(1) if value else ""
    return action, fields


def _error_text(html: str) -> str:
    for pattern in (r'class="[^"]*error[^"]*"[^>]*>\s*([^<]{4,200})', r'id="error[^"]*"[^>]*>\s*([^<]{4,200})'):
        hits = [h.strip() for h in re.findall(pattern, html) if h.strip()]
        if hits:
            return hits[0]
    return ""


def login_from_keychain(email: str | None = None, password: str | None = None, interactive: bool = True) -> str:
    """Sign in to bpost (Ping) with Keychain credentials and return the session cookie string."""
    email = email or _keychain("email")
    password = password or _keychain("password")
    if not email or not password:
        raise SystemExit(
            f"no bpost credentials in Keychain service {KEYCHAIN_SERVICE!r}.\n"
            "Add them once:\n"
            "  security add-generic-password -s scout_bpost -a email -w 'you@example.com' -U\n"
            "  read -s -p 'bpost password: ' P && security add-generic-password "
            "-s scout_bpost -a password -w \"$P\" -U && unset P"
        )

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    resp = opener.open(BASE + "/customer/oauth2/authorization/bpostping", timeout=30)
    html = resp.read().decode("utf-8", errors="replace")
    page_url = resp.url

    for attempt in range(3):
        action, fields = _form(html)
        target = urllib.parse.urljoin(page_url, action)
        lowered = {k.lower() for k in fields}
        if "pf.username" in fields:
            fields["pf.username"] = email
            fields["pf.pass"] = password
            fields.setdefault("pf.ok", "Sign In")
        elif any("otp" in k or "code" in k or "passcode" in k for k in lowered):
            if not interactive:
                raise BpostError("bpost asked for a one-time code; run `login` in a terminal")
            try:
                code = input("bpost one-time code: ").strip()
            except EOFError:
                raise SystemExit(
                    "bpost wants a one-time code and there is no terminal to type it in — "
                    "run `login` from your own shell."
                )
            for key in list(fields):
                if "otp" in key.lower() or "code" in key.lower():
                    fields[key] = code
        else:
            break
        body = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(target, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Referer", page_url)
        resp = opener.open(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
        page_url = resp.url
        if "parcel.bpost.be" in page_url and "pf.pass" not in html:
            break
        error = _error_text(html)
        if error:
            raise SystemExit(f"bpost login failed: {error}")

    cookies = [f"{c.name}={c.value}" for c in jar if "bpost.be" in c.domain]
    if not cookies:
        raise BpostError("login finished without any parcel.bpost.be cookie")
    cookie = "; ".join(cookies)
    client = BpostGuestClient(lang="nl", cookie=cookie)
    if not client.whoami():
        raise SystemExit("login seemed to work but /customer/user/me is not authenticated")
    return cookie


def save_session(cookie: str) -> Path:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(cookie + "\n", encoding="utf-8")
    try:
        SESSION_FILE.chmod(0o600)
    except OSError:
        pass
    return SESSION_FILE


def _cookie_value(cookie: str, name: str) -> str:
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return urllib.parse.unquote(value)
    return ""


def load_session(explicit: str | None = None) -> str | None:
    """Logged-in bpost session cookie, if one is configured.

    Stored in `.cursor/skills/bpost-label/.bpost-session` (gitignored) or passed with
    `--cookie` / `BPOST_SESSION`. With a session the basket lives on the account, so
    https://parcel.bpost.be/nl/basket becomes a real, shareable link.
    """
    raw = explicit
    if not raw and SESSION_FILE.exists():
        raw = SESSION_FILE.read_text(encoding="utf-8")
    if not raw:
        return None
    cleaned = " ".join(raw.replace("\n", " ").split())
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()
    return cleaned or None


def ensure_private_registration(client: BpostGuestClient, me: dict | None) -> dict | None:
    """bpost nags a half-registered account with a 'Voor mezelf / Voor mijn bedrijf' modal.

    Finishing it as private (the default for this shop) stops the modal blocking the basket.
    """
    if not me or me.get("registrationComplete") or (me.get("customerType") or "").lower() != "private":
        return me
    client._request("POST", "/customer/user/registration/residential", {"language": client.lang})
    print("  account registered as private (bpost chooser modal disabled)", file=sys.stderr)
    return client.whoami() or me


def _make_client(args, require_session: bool = False) -> tuple[BpostGuestClient, str | None]:
    cookie = None if getattr(args, "guest", False) else load_session(getattr(args, "cookie", None))
    client = BpostGuestClient(lang=args.lang, cookie=cookie)
    if cookie:
        me = client.whoami()
        if not me:
            if require_session:
                raise SystemExit(
                    "bpost session cookie is not accepted (expired?). Log in again and refresh "
                    f"{SESSION_FILE}."
                )
            print("  note: bpost session cookie rejected, continuing as guest", file=sys.stderr)
            client.cookie = None
            return client, None
        me = ensure_private_registration(client, me)
        name = me.get("firstName") or me.get("email") or me.get("userId") or "account"
        print(f"  signed in as {name}", file=sys.stderr)
    return client, cookie


def _validate_vat(vat: str) -> str:
    """bpost rejects sender VAT numbers that fail the Belgian checksum."""
    raw = vat.replace(" ", "").upper()
    if not raw:
        return vat
    if not raw.startswith("BE") or len(raw) != 12 or not raw[2:].isdigit():
        raise SystemExit(f"sender VAT {vat!r} is not a Belgian VAT number (BE + 10 digits)")
    base, check = int(raw[2:10]), int(raw[10:12])
    if (97 - base % 97) % 97 != check:
        raise SystemExit(f"sender VAT {vat!r} fails the Belgian checksum; bpost will reject the sender")
    return raw


def sender_from_config() -> dict:
    if not SENDER_FILE.exists():
        raise SystemExit(
            f"Missing sender config: {SENDER_FILE}\n"
            "Copy sender.example.yaml to sender.yaml and fill in the Chalk Rebels return address."
        )
    data = _load_flat_yaml(SENDER_FILE.read_text(encoding="utf-8"))
    required = ["first_name", "last_name", "street", "number", "zip", "city", "email"]
    missing = [k for k in required if not str(data.get(k, "")).strip()]
    if missing:
        raise SystemExit(f"{SENDER_FILE} is missing: {', '.join(missing)}")
    return {
        "contactType": "COMPANY" if data.get("company") else "INDIVIDUAL",
        "title": data.get("title", "") or "",
        "firstName": data["first_name"],
        "lastName": data["last_name"],
        "emailAddress": data["email"],
        "company": data.get("company") or None,
        "vatNumber": _validate_vat(data.get("vat_number") or "") if data.get("vat_number") else None,
        "phoneNumber": data.get("phone") or None,
        "addressCityCode": str(data["zip"]),
        "addressStreet": str(data["street"]).upper(),
        "addressStreetNo": str(data["number"]),
        "addressBoxNo": str(data.get("box") or ""),
        "address": {
            "countryCode": data.get("country", "BE"),
            "postalCode": str(data["zip"]),
            "city": str(data["city"]).upper(),
            "street": str(data["street"]).upper(),
            "streetNumber": str(data["number"]),
            "boxNumber": str(data.get("box") or ""),
        },
        "phoneNumberToDisplay": "",
    }


def build_recipient(shipment: dict, point: dict | None) -> dict:
    if point:
        pickup = point
        block = "locker" if shipment["destination_code"] == "LOCKER" else "pickupPoint"
        payload = {
            "contactType": "INDIVIDUAL",
            "firstName": shipment["first_name"],
            "lastName": shipment["last_name"],
            "company": shipment.get("company") or None,
            "notificationLang": shipment.get("lang", "nl"),
            "phoneNumber": shipment.get("phone") or None,
            "postalCode": pickup["zip"],
            "addressStreet": pickup["street"],
            "addressStreetNo": pickup["number"],
            "notificationType": "EMAIL",
            "emailAddress": shipment["email"],
            "phoneNumberToDisplay": "",
            block: {
                "address": {
                    "countryCode": shipment["country_code"],
                    "postalCode": pickup["zip"],
                    "city": pickup["city"],
                    "street": pickup["street"],
                    "streetNumber": pickup["number"],
                },
                "locationId": pickup["id"],
                "locationName": pickup["name"],
                "notificationLanguage": shipment.get("lang", "nl"),
                "notificationType": "EMAIL",
            },
        }
        if block == "locker":
            payload[block]["lockerType"] = pickup.get("lockerType")
        return payload
    return {
        "contactType": "COMPANY" if shipment.get("company") else "INDIVIDUAL",
        "firstName": shipment["first_name"],
        "lastName": shipment["last_name"],
        "company": shipment.get("company") or None,
        "notificationLang": shipment.get("lang", "nl"),
        "phoneNumber": shipment.get("phone") or None,
        "postalCode": shipment["zip"],
        "addressStreet": shipment["street"].upper(),
        "addressStreetNo": shipment["number"],
        "notificationType": "EMAIL",
        "emailAddress": shipment["email"],
        "phoneNumberToDisplay": "",
        "address": {
            "countryCode": shipment["country_code"],
            "postalCode": shipment["zip"],
            "city": shipment["city"].upper(),
            "street": shipment["street"],
            "streetNumber": shipment["number"],
            "boxNumber": shipment.get("box") or "",
        },
    }


# --------------------------------------------------------------------------- #
# Shopify
# --------------------------------------------------------------------------- #
def _shopify_token(shop: str) -> str:
    """Admin token from env, Keychain, or a client_credentials exchange."""
    env = (os.environ.get("SHOPIFY_ADMIN_TOKEN") or "").strip()
    if env.startswith(("shpat_", "shpca_")):
        return env
    admin = _keychain("api", SHOPIFY_KEYCHAIN)
    if admin.startswith(("shpat_", "shpca_")):
        return admin
    client_id = _keychain("client_id", SHOPIFY_KEYCHAIN)
    secret = _keychain("client_secret", SHOPIFY_KEYCHAIN) or (
        admin if admin.startswith("shpss_") else ""
    )
    if not client_id or not secret:
        raise SystemExit(
            "no Shopify admin token: set SHOPIFY_ADMIN_TOKEN, or store one in Keychain "
            f"service {SHOPIFY_KEYCHAIN!r} (accounts api, or client_id + client_secret)"
        )
    data = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
    ).encode()
    req = urllib.request.Request(f"https://{shop}/admin/oauth/access_token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise BpostError(f"Shopify token exchange failed: HTTP {e.code}") from e
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise BpostError("Shopify token exchange returned no access_token")
    return token


def shopify_order(number: str) -> dict:
    """Fetch one order from the configured Shopify store."""
    shop = SHOPIFY_SHOP
    if not shop:
        raise SystemExit(
            "no Shopify shop configured: set SHOPIFY_SHOP (e.g. your-shop.myshopify.com) "
            "or shopify.shop in config.yaml"
        )
    token = _shopify_token(shop)
    name = number if str(number).startswith("#") else f"#{number}"
    url = (
        f"https://{shop}/admin/api/2024-10/orders.json?status=any"
        f"&name={urllib.parse.quote(name)}&fields=id,name,email,total_price,total_weight,"
        "shipping_address,billing_address,line_items,note,created_at,financial_status"
    )
    req = urllib.request.Request(url)
    req.add_header("X-Shopify-Access-Token", token)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise BpostError(f"Shopify order lookup failed: HTTP {e.code}") from e
    orders = payload.get("orders") or []
    if not orders:
        raise SystemExit(f"Shopify order {name} not found on {shop}")
    return orders[0]


def shipments_from_shopify(order: dict, split: str, delivery: str, lang: str) -> list[dict]:
    addr = order.get("shipping_address") or {}
    street, number = split_street(addr.get("address1"))
    country = (addr.get("country_code") or "BE").upper()
    base = {
        "first_name": addr.get("first_name") or "",
        "last_name": addr.get("last_name") or "",
        "company": addr.get("company"),
        "street": street,
        "number": number,
        "box": addr.get("address2") or "",
        "zip": addr.get("zip") or "",
        "city": addr.get("city") or "",
        "country_code": country,
        "email": order.get("email") or "",
        "phone": addr.get("phone") or order.get("phone"),
        "lang": lang,
        "delivery": delivery,
        "reference": order.get("name", "").lstrip("#"),
    }
    items = order.get("line_items") or []
    if split == "single" or not items:
        grams = int(order.get("total_weight") or 0)
        return [{**base, "weight_kg": max(grams, 1) / 1000.0, "contents": order.get("name", "")}]
    out = []
    for item in items:
        qty = int(item.get("quantity") or 1)
        grams = int(item.get("grams") or 0) or 1
        out.append(
            {
                **base,
                "weight_kg": grams * qty / 1000.0,
                "contents": f"{item.get('title')} x{qty}",
                "reference": f"{base['reference']} {item.get('sku') or ''}".strip(),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def clear_stale_order(client: BpostGuestClient) -> None:
    """A cart the human emptied leaves an empty in-progress order that shadows new ones."""
    try:
        oip = client._request("GET", "/customer/user/order-in-progress")
    except BpostError:
        return
    if oip and not oip.get("numberOfItems"):
        client.delete_order(oip["orderId"])
        print(f"  cleared stale empty bpost order {oip['orderId']}", file=sys.stderr)


def resolve_pickup(client: BpostGuestClient, shipment: dict, wanted: str | None) -> dict:
    points = client.pickup_points(
        shipment["country_code"],
        shipment["zip"],
        destination=shipment.get("destination_code", "PICKUP_POINT"),
    )
    if not points:
        raise BpostError(f"no pickup points found near {shipment['zip']} {shipment['city']}")
    if wanted:
        needle = wanted.lower()
        for point in points:
            hay = f"{point['id']} {point['name']} {point['street']}".lower()
            if needle in hay:
                return point
        raise BpostError(f"no pickup point matched {wanted!r} near {shipment['zip']}")
    return points[0]


def run_create(args) -> int:
    client, cookie = _make_client(args)
    if args.dry_run and cookie:
        # a dry run must not touch the account basket pointer
        print("  dry run: using a guest order so the account cart stays as it is", file=sys.stderr)
        client, cookie = BpostGuestClient(lang=args.lang), None
    sender = sender_from_config()
    shipments, source = collect_shipments(args, sender, client)

    for shipment in shipments:
        shipment.setdefault("delivery", args.delivery)
        shipment.setdefault("lang", args.lang)
        shipment["country_code"] = shipment["country_code"].upper()
        shipment["email"] = shipment.get("email") or sender["emailAddress"]
        if shipment["country_code"] != "BE":
            # bpost only offers pickup points domestically.
            shipment["delivery"] = "address"
        if not shipment.get("weight_kg"):
            raise SystemExit(f"shipment for {shipment.get('first_name')} has no weight")
        if shipment["weight_kg"] > 30:
            raise SystemExit(f"{shipment['weight_kg']} kg exceeds the 30 kg bpost limit")
        if shipment["country_code"] == "BE" and args.normalize:
            fixed = client.normalize_address(shipment["zip"], shipment["city"], shipment["street"], shipment["number"])
            shipment.update(fixed)

    # price every parcel from the catalog so we know the product line up front
    noted_uninsured = set()
    for shipment in shipments:
        shipment["destination_code"] = DESTINATIONS[shipment["delivery"]]
        shipment["band"] = weight_range_code(shipment["weight_kg"], shipment["country_code"])
        shipment["product_line"], shipment["quoted_price"] = choose_product_line(
            client, shipment, args.product_line
        )
        shipment["pickup"] = (
            resolve_pickup(client, shipment, args.pickup)
            if shipment["destination_code"] in ("PICKUP_POINT", "LOCKER")
            else None
        )
        # WARRANTY: default on for Belgian parcels, unavailable (and skipped) abroad
        if args.insurance is None and shipment["country_code"] != "BE":
            if shipment["country_code"] not in noted_uninsured:
                noted_uninsured.add(shipment["country_code"])
                print(
                    f"  note: bpost offers no insurance for {shipment['country_code']}, "
                    "shipping uninsured",
                    file=sys.stderr,
                )
        wants_insurance = args.insurance is not False and (
            shipment["country_code"] == "BE" or args.insurance is True
        )
        if wants_insurance:
            options = client.catalog(shipment["country_code"], shipment["destination_code"]).get("options") or []
            warranty = next((o for o in options if o.get("code") == "WARRANTY"), None)
            if warranty:
                shipment["insurance"] = True
                shipment["insurance_price"] = float(warranty.get("price") or 0)
                shipment["quoted_price"] = round(
                    shipment["quoted_price"] + shipment["insurance_price"], 2
                )
            elif args.insurance is True:
                raise BpostError(
                    f"bpost does not offer insurance (WARRANTY) for {shipment['country_code']} "
                    f"{shipment['destination_code']} — use --no-insurance"
                )
            else:
                print(
                    f"  note: no bpost insurance available for {shipment['country_code']} "
                    f"{shipment['destination_code']}, shipping uninsured",
                    file=sys.stderr,
                )

    print(f"\nbpost plan for {source}")
    _print_plan(shipments)
    quoted_total = round(sum(s["quoted_price"] for s in shipments), 2)
    print(f"\n  QUOTE TOTAL: {quoted_total} EUR")

    if args.quote:
        print("\n  quote only: nothing created.")
        return 0

    clear_stale_order(client)
    order_id, _ = client.new_ids()
    created = []
    for shipment in shipments:
        parcel_id = f"parcelId-{uuid.uuid4()}"
        parcel = {
            "country_code": shipment["country_code"],
            "destination_code": shipment["destination_code"],
            "weight_range_code": shipment["band"],
            "reference": shipment.get("reference"),
            "product_line": shipment["product_line"],
            "contents": shipment.get("contents"),
            "value_eur": shipment.get("value_eur") or args.value_eur,
            "selected_options": (
                {"warranty": {"code": "WARRANTY"}} if shipment.get("insurance") else None
            ),
        }
        pickup = shipment["pickup"]
        recipient = build_recipient(shipment, pickup)
        client.add_parcel(order_id, parcel_id, parcel, recipient, sender)
        created.append({"parcelId": parcel_id, "shipment": shipment, "pickup": pickup})

    order = client.get_order(order_id)
    if not (order.get("parcelsWithSummaries") or []):
        raise BpostError("bpost accepted the calls but the order stayed empty; nothing to pay")

    record = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "orderId": order_id,
        "source": source,
        "total_eur": (order.get("orderSummary") or {}).get("total"),
        "sender": sender,
        "parcels": created,
        "order": order,
        "payment_url": None,
        "account": bool(cookie),
    }

    if args.dry_run:
        _print_order(order)
        client.delete_order(order_id)
        print("\n  dry run: draft order deleted, nothing to pay (no record kept).")
        return 0

    _save(record)
    _print_order(order, sender)
    print(f"  record: {_record_path(order_id)}")
    if args.pay_link:
        return _mint_payment_link(client, order_id, record)
    if cookie:
        print(f"\n  BASKET  {BASKET_URL}")
        print("          (signed in, so the order is on your account: this URL works anywhere)")
        print("  Pay:    click Betaal there, or mint a PSP link with `pay " + order_id + "`")
    else:
        print("\n  Nothing is payable yet. Review it in bpost before any money moves:")
        print("    - in this app: seed sessionStorage.orderId with the id above, open /nl/basket")
        print("    - own browser, save once as a bookmark:")
        print(f"      {bookmarklet(order_id)}")
        print("  Pay: click Betaal in the bpost basket, or mint a portable link with:")
        print(f"    python3 .cursor/skills/bpost-label/scripts/bpost_order.py pay {order_id}")
    return 0


def _mint_payment_link(client: BpostGuestClient, order_id: str, record: dict | None) -> int:
    order = client.get_order(order_id)
    total = (order.get("orderSummary") or {}).get("total")
    if not (order.get("parcelsWithSummaries") or []):
        raise BpostError(f"{order_id} has no parcels (empty or already cleaned up)")
    url = client.payment_link(order_id)
    if record is None:
        record = _load_record(order_id) or {}
    record.update(
        {
            "orderId": order_id,
            "total_eur": total,
            "order": order,
            "payment_url": url,
            "payment_link_created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _save(record)
    print(f"\n  orderId: {order_id}")
    print(f"  TOTAL:   {total} EUR")
    print("\n  Pay here (the only step the skill will not do):")
    print(f"  {url}\n")
    return 0


def _print_order(order: dict, sender: dict | None = None) -> None:
    """Human review block: exactly what bpost has, before anything is payable."""
    summary = order.get("orderSummary") or {}
    created = str(order.get("creationDate") or "")[:19].replace("T", " ")
    print(f"\n  REVIEW  {order.get('orderId')}   created {created}")
    parcels = order.get("parcelsWithSummaries") or []
    for idx, entry in enumerate(parcels, start=1):
        parcel = entry.get("parcel") or {}
        s = entry.get("summary") or {}
        rec = parcel.get("recipient") or {}
        det = parcel.get("parcelDetails") or {}
        intl = parcel.get("international") or {}
        name = " ".join(x for x in (rec.get("firstName"), rec.get("lastName")) if x)
        if rec.get("company"):
            name = f"{name} ({rec['company']})" if name else rec["company"]
        dest = DEST_NAME.get(det.get("destinationCode"), det.get("destinationCode"))
        weight = (s.get("weightRange") or {}).get("name")
        price = (s.get("parcelPrice") or {}).get("price")
        print(f"\n  {idx}. {name or '?'}   [{det.get('countryCode')}]  {dest}")
        print(
            f"     {weight} · {det.get('productLineCode')} · {s.get('productCode')}"
            f" · EUR {price}   email: {rec.get('emailAddress')}"
        )
        if rec.get("pickupPoint"):
            pp = rec["pickupPoint"]
            addr = pp.get("address") or {}
            print(
                f"     pickup: {pp.get('locationName')} (id {pp.get('locationId')})"
                f" — {addr.get('street')} {addr.get('streetNumber')}, {addr.get('postalCode')} {addr.get('city')}"
            )
        elif rec.get("locker"):
            lk = rec["locker"]
            addr = lk.get("address") or {}
            print(
                f"     locker: {lk.get('locationName')} ({lk.get('lockerType')}, id {lk.get('locationId')})"
                f" — {addr.get('street')} {addr.get('streetNumber')}, {addr.get('postalCode')} {addr.get('city')}"
            )
        else:
            a = rec.get("address") or {}
            box = f" bus {a['boxNumber']}" if a.get("boxNumber") else ""
            print(
                f"     to:     {a.get('street')} {a.get('streetNumber')}{box},"
                f" {a.get('postalCode')} {a.get('city')}, {a.get('countryCode')}"
            )
        if det.get("senderReference"):
            print(f"     ref:    {det['senderReference']}")
        if intl.get("contentDescription"):
            print(f"     customs: {intl['contentDescription']} — value {intl.get('parcelValue')}")
        for opt in s.get("selectedOptions") or []:
            if opt.get("code"):
                print(f"     option:  {opt.get('code')} ({opt.get('name')}) +EUR {opt.get('price')}")
    snd = (parcels[0].get("parcel") or {}).get("sender") if parcels else None
    if snd:
        a = snd.get("address") or {}
        box = f" bus {a['boxNumber']}" if a.get("boxNumber") else ""
        who = snd.get("company") or f"{snd.get('firstName')} {snd.get('lastName')}"
        print(
            f"\n  FROM    {who} — {a.get('street')} {a.get('streetNumber')}{box},"
            f" {a.get('postalCode')} {a.get('city')}, {a.get('countryCode')}"
            f"   VAT {snd.get('vatNumber') or '-'}   {snd.get('emailAddress')}"
        )
    print(f"\n  TOTAL   EUR {summary.get('total')}")
    print(f"  STATUS  {order.get('paymentStatus') or 'not payable yet (no checkout link minted)'}")



def collect_shipments(args, sender: dict, client: BpostGuestClient) -> tuple[list[dict], str]:
    if args.shopify:
        order = shopify_order(args.shopify)
        shipments = shipments_from_shopify(order, args.split, args.delivery, args.lang)
        return shipments, f"Shopify {order.get('name')} ({order.get('email')})"
    if args.json:
        payload = json.loads(Path(args.json).read_text(encoding="utf-8"))
        shipments = payload["shipments"] if isinstance(payload, dict) else payload
        return shipments, f"{args.json} ({len(shipments)} shipments)"
    if args.to:
        parsed = parse_address(args.to, client)
        return (
            [
                {
                    **parsed,
                    "email": args.email or sender["emailAddress"],
                    "phone": args.phone,
                    "lang": args.lang,
                    "delivery": args.delivery,
                    "weight_kg": args.weight_kg,
                    "reference": args.reference,
                    "contents": args.contents or "",
                }
            ],
            f"{parsed['first_name']} {parsed['last_name']}, {parsed['city']}",
        )
    if not (args.name and args.street and args.zip and args.city):
        raise SystemExit("Provide --shopify <order>, --to <pasted address>, --json <file>, or --name/--street/--zip/--city")
    street, number = split_street(args.street)
    parts = args.name.split()
    return (
        [
            {
                "first_name": parts[0],
                "last_name": " ".join(parts[1:]) or parts[0],
                "company": None,
                "street": street,
                "number": args.number or number,
                "box": "",
                "zip": args.zip,
                "city": args.city,
                "country_code": args.country.upper(),
                "email": args.email or sender["emailAddress"],
                "phone": args.phone,
                "lang": args.lang,
                "delivery": args.delivery,
                "weight_kg": args.weight_kg,
                "reference": args.reference,
                "contents": "",
            }
        ],
        f"{args.name}, {args.city}",
    )


def choose_product_line(client: BpostGuestClient, shipment: dict, forced: str | None) -> tuple[str, float]:
    """Cheapest product line that actually supports this country/destination/weight."""
    catalog = client.catalog(shipment["country_code"], shipment["destination_code"])
    candidates = [
        (float(line.get("price") or 0), line["code"])
        for line in catalog.get("productLines") or []
        if line.get("state") in ("SELECTED", "ENABLED")
    ]
    if not candidates:
        raise BpostError(f"no product line for {shipment['country_code']} {shipment['destination_code']}")
    if forced:
        codes = {c for _, c in candidates}
        if forced not in codes:
            raise BpostError(
                f"{forced} is not offered for {shipment['country_code']} {shipment['destination_code']} "
                f"(available: {', '.join(sorted(codes))})"
            )
        candidates = [(p, c) for p, c in candidates if c == forced]

    rejected = []
    for base_price, code in sorted(candidates):
        line_catalog = client.catalog(
            shipment["country_code"], shipment["destination_code"], lines=[code]
        )
        ranges = {w["code"]: w for w in line_catalog.get("weightRanges") or []}
        band = ranges.get(shipment["band"])
        if band is None or band.get("state") == "DISABLED":
            rejected.append(code)
            continue
        return code, round(base_price + float(band.get("price") or 0), 2)
    raise BpostError(
        f"{shipment['weight_kg']:.2f} kg ({shipment['band']}) is not offered for "
        f"{shipment['country_code']} {shipment['destination_code']} by {', '.join(rejected) or 'any product line'}"
    )


def _print_plan(shipments: list[dict]) -> None:
    for idx, sh in enumerate(shipments, start=1):
        print(
            f"  {idx}. {sh['first_name']} {sh['last_name']:<20} "
            f"{sh['weight_kg']:>5.2f} kg  {sh['delivery']:<8} {sh['product_line']:<10} "
            f"{sh['quoted_price']:>6.2f} EUR  {sh['city']} [{sh['country_code']}]"
        )
        if sh.get("pickup"):
            p = sh["pickup"]
            kind = "locker" if sh["destination_code"] == "LOCKER" else "pickup"
            print(f"     {kind}: {p['name']} — {p['street']} {p['number']}, {p['zip']} {p['city']} (id {p['id']})")
        else:
            box = f" bus {sh['box']}" if sh.get("box") else ""
            print(f"     to:     {sh['street']} {sh['number']}{box}, {sh['zip']} {sh['city']}, {sh['country_code']}")
        if sh.get("insurance"):
            print(f"     insurance: WARRANTY +EUR {sh.get('insurance_price', 0):.2f} (cover up to EUR 500)")
        if sh.get("contents"):
            print(f"     contents: {sh['contents']}")


def _record_path(order_id: str) -> Path:
    return OUTPUT_DIR / f"{order_id}.json"


def _save(record: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _record_path(record["orderId"]).write_text(json.dumps(record, indent=2), encoding="utf-8")


def _load_record(order_id: str) -> dict | None:
    path = _record_path(order_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def run_review(args) -> int:
    """Re-print a created order from bpost (works days later, no writes)."""
    client, _ = _make_client(args)
    order_id = _normalise_order_id(args.order)
    order = client.get_order(order_id)
    if not (order.get("parcelsWithSummaries") or []):
        raise BpostError(f"{order_id} is empty, paid, or cleaned up")
    _print_order(order)
    record = _load_record(order_id)
    if record and record.get("payment_url"):
        print(f"  link    {record['payment_url']}")
    if record.get("account"):
        print(f"\n  BASKET  {BASKET_URL}   (signed in: works anywhere)")
    else:
        print(f"  basket in this app: {BASKET_HINT}")
        print(f"  own browser: {bookmarklet(order_id)}")
    return 0


def run_open(args) -> int:
    """Write a self-contained review sheet you can open anywhere (bpost has no such URL)."""
    client, _ = _make_client(args)
    order_id = _normalise_order_id(args.order)
    order = client.get_order(order_id)
    if not (order.get("parcelsWithSummaries") or []):
        raise BpostError(f"{order_id} is empty, paid, or cleaned up")
    record = _load_record(order_id) or {}
    path = review_sheet(order, order_id, record.get("payment_url"))
    _print_order(order)
    print(f"\n  sheet   {path}")
    if record.get("payment_url"):
        print(f"  link    {record['payment_url']}")
    if record.get("account"):
        print(f"  BASKET  {BASKET_URL}   (signed in: works anywhere)")
    else:
        print(f"  basket in this app: {BASKET_HINT}")
        print(f"  own browser: {bookmarklet(order_id)}")
    return 0


def review_sheet(order: dict, order_id: str, payment_url: str | None) -> Path:
    """Standalone HTML mirror of the bpost basket, so the order can be checked anywhere."""
    summary = order.get("orderSummary") or {}
    parcels = order.get("parcelsWithSummaries") or []
    sender = ((parcels[0].get("parcel") or {}).get("sender") if parcels else None) or {}
    rows = []
    for idx, entry in enumerate(parcels, start=1):
        parcel = entry.get("parcel") or {}
        s = entry.get("summary") or {}
        rec = parcel.get("recipient") or {}
        det = parcel.get("parcelDetails") or {}
        intl = parcel.get("international") or {}
        name = " ".join(x for x in (rec.get("firstName"), rec.get("lastName")) if x)
        if rec.get("company"):
            name = f"{name} ({rec['company']})" if name else rec["company"]
        where = ""
        if rec.get("pickupPoint"):
            pp = rec["pickupPoint"]
            a = pp.get("address") or {}
            where = f"pickup: {_esc(pp.get('locationName'))}<br>{_esc(a.get('street'))} {_esc(a.get('streetNumber'))}, {_esc(a.get('postalCode'))} {_esc(a.get('city'))}"
        elif rec.get("locker"):
            lk = rec["locker"]
            a = lk.get("address") or {}
            where = f"locker ({_esc(lk.get('lockerType'))}): {_esc(lk.get('locationName'))}<br>{_esc(a.get('street'))} {_esc(a.get('streetNumber'))}, {_esc(a.get('postalCode'))} {_esc(a.get('city'))}"
        else:
            a = rec.get("address") or {}
            box = f" bus {_esc(a.get('boxNumber'))}" if a.get("boxNumber") else ""
            where = f"{_esc(a.get('street'))} {_esc(a.get('streetNumber'))}{box}<br>{_esc(a.get('postalCode'))} {_esc(a.get('city'))}, {_esc(a.get('countryCode'))}"
        price = (s.get("parcelPrice") or {}).get("price")
        rows.append(
            f"""<tr>
      <td>{idx}</td>
      <td><strong>{_esc(name)}</strong><br><span class=mut>{_esc(rec.get('emailAddress'))}</span></td>
      <td>{where}</td>
      <td>{(s.get('weightRange') or {}).get('name','')}<br><span class=mut>{_esc(det.get('productLineCode'))} · {_esc(s.get('productCode'))}</span>{''.join(f"<br><span class=mut>+ {_esc(o.get('name'))}</span>" for o in (s.get('selectedOptions') or []) if o.get('code'))}</td>
      <td>{_esc(det.get('senderReference') or '')}<br><span class=mut>{_esc(intl.get('contentDescription') or '')}</span></td>
      <td class=num>€&nbsp;{price}</td>
    </tr>"""
        )
    a = sender.get("address") or {}
    box = f" bus {_esc(a.get('boxNumber'))}" if a.get("boxNumber") else ""
    pay_block = (
        f'<a class=btn href="{_esc(payment_url)}">Betaal op de betaalpagina</a>'
        if payment_url
        else '<a class=btn href="https://parcel.bpost.be/nl/basket">Open bpost (seed the order id first)</a>'
    )
    html = f"""<!doctype html>
<html lang="nl"><meta charset="utf-8">
<title>bpost {_esc(order_id)}</title>
<style>
 :root {{ --red:#e30613; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; padding:32px; font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif; color:#222; background:#f6f6f6; }}
 main {{ max-width:960px; margin:0 auto; background:#fff; border-radius:10px; padding:28px 32px; box-shadow:0 1px 4px #0001; }}
 h1 {{ font-size:20px; margin:0 0 4px; }}
 .mut {{ color:#777; font-size:12.5px; }}
 .head {{ display:flex; justify-content:space-between; align-items:flex-start; border-bottom:3px solid var(--red); padding-bottom:14px; margin-bottom:18px; }}
 .tag {{ background:#fdecec; color:var(--red); border-radius:999px; padding:4px 12px; font-size:12px; font-weight:600; }}
 .from {{ background:#fafafa; border:1px solid #eee; border-radius:8px; padding:12px 14px; margin-bottom:20px; font-size:14px; }}
 table {{ width:100%; border-collapse:collapse; }}
 th {{ text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:#888; border-bottom:1px solid #e5e5e5; padding:8px 10px; }}
 td {{ padding:12px 10px; border-bottom:1px solid #f0f0f0; vertical-align:top; font-size:14px; }}
 td.num {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
 .total {{ display:flex; justify-content:space-between; align-items:center; margin-top:18px; padding-top:16px; border-top:2px solid #222; font-size:19px; font-weight:700; }}
 .btn {{ display:inline-block; margin-top:18px; background:var(--red); color:#fff; text-decoration:none; padding:12px 20px; border-radius:6px; font-weight:600; }}
 .foot {{ margin-top:22px; font-size:12px; color:#888; }}
 @media print {{ body {{ background:#fff; padding:0; }} main {{ box-shadow:none; }} .btn {{ display:none; }} }}
</style>
<main>
  <div class=head>
    <div>
      <h1>bpost zending</h1>
      <div class=mut>{_esc(order_id)} · aangemaakt {_esc(str(order.get('creationDate') or '')[:19].replace('T',' '))}</div>
    </div>
    <div class=tag>{_esc(order.get('paymentStatus') or 'niet betaald')}</div>
  </div>
  <div class=from><strong>Van</strong> — {_esc(sender.get('company') or (sender.get('firstName','') + ' ' + sender.get('lastName','')))},
    {_esc(a.get('street'))} {_esc(a.get('streetNumber'))}{box}, {_esc(a.get('postalCode'))} {_esc(a.get('city'))}, {_esc(a.get('countryCode'))}
    · BTW {_esc(sender.get('vatNumber') or '-')} · {_esc(sender.get('emailAddress'))}</div>
  <table>
    <thead><tr><th>#</th><th>Ontvanger</th><th>Adres</th><th>Gewicht / product</th><th>Referentie</th><th class=num>Prijs</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <div class=total><span>Totaal</span><span>€&nbsp;{summary.get('total')}</span></div>
  {pay_block}
  <div class=foot>Spiegel van de bpost-bestelling op het moment van openen. bpost zelf heeft geen deelbare
  winkelmandje-URL: de order leeft in <code>sessionStorage</code>. Deze pagina is de leesbare kopie.</div>
</main>
</html>
"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"review-{order_id}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _esc(value) -> str:
    import html as _html

    return _html.escape(str(value if value is not None else ""))


def run_login(args) -> int:
    global KEYCHAIN_SERVICE
    if getattr(args, "keychain_service", None):
        KEYCHAIN_SERVICE = args.keychain_service
    print(f"signing in with Keychain service {KEYCHAIN_SERVICE!r} …")
    cookie = login_from_keychain(interactive=True)
    path = save_session(cookie)
    client = BpostGuestClient(lang=args.lang, cookie=cookie)
    me = ensure_private_registration(client, client.whoami()) or {}
    print(f"signed in as {me.get('firstName') or ''} {me.get('lastName') or ''}".strip())
    print(f"  email: {me.get('email')}")
    print(f"  saved: {path} (mode 600, gitignored)")
    print(f"  basket: {BASKET_URL}")
    return 0


def run_whoami(args) -> int:
    cookie = load_session(args.cookie)
    if not cookie:
        print(f"no session configured ({SESSION_FILE} missing); orders would be guest orders")
        return 1
    client = BpostGuestClient(lang=args.lang, cookie=cookie)
    me = client.whoami()
    if not me:
        print("session rejected (expired?) — log in again and refresh the cookie")
        return 1
    print(f"signed in as {me.get('firstName') or ''} {me.get('lastName') or ''}".strip())
    print(f"  email: {me.get('email')}")
    print(f"  type:  {me.get('customerType')}")
    print("  basket link works: https://parcel.bpost.be/nl/basket")
    return 0


def run_pay(args) -> int:
    client, _ = _make_client(args)
    order_id = _normalise_order_id(args.order)
    print(f"\nbpost order {order_id}")
    return _mint_payment_link(client, order_id, _load_record(order_id))


def _normalise_order_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise SystemExit("pass an orderId, e.g. pay orderId-…")
    if raw.isdigit() or raw.startswith("#"):
        raise SystemExit("that looks like a Shopify number; use `create --shopify <n>` first")
    return raw if raw.startswith("orderId-") else f"orderId-{raw}"


def bookmarklet(order_id: str) -> str:
    """One-line bookmark for the user's own browser: seeds the order id, opens the basket.

    bpost has no URL that carries a guest basket, so this is the portable equivalent.
    Save it once as a bookmark, then click it while on parcel.bpost.be.
    """
    js = (
        "javascript:(()=>{var id=prompt('bpost orderId','%s');if(!id)return;"
        "sessionStorage.setItem('orderId',id.trim());sessionStorage.setItem('lang','nl');"
        "location.href='/nl/basket'})()" % order_id
    )
    return js


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("pay", "review", "create", "open", "whoami", "login"):
        cmd, argv = argv[0], argv[1:]
    else:
        cmd = "create"

    if cmd in ("pay", "review", "open", "whoami", "login"):
        p = argparse.ArgumentParser(prog=f"bpost_order.py {cmd}", description={
            "pay": "Mint the MultiSafepay checkout link for an order that already exists.",
            "review": "Print what bpost has for an order (read-only).",
            "open": "Write a standalone review sheet (HTML) for an order.",
            "whoami": "Check whether the stored bpost session cookie still works.",
            "login": "Sign in to bpost with the Keychain credentials and store the session.",
        }[cmd])
        p.add_argument("order", nargs="?", help="orderId-… returned by create (not needed for whoami)")
        p.add_argument("--lang", default="nl", choices=["nl", "fr", "en", "de"])
        p.add_argument("--cookie", help="Cookie header value from a logged-in parcel.bpost.be session")
        p.add_argument("--keychain-service", default=None, help="Keychain service holding email/password (default: scout_bpost)")
        p.add_argument("--guest", action="store_true", help="ignore the stored session and act as a guest")
        args = p.parse_args(argv)
        try:
            if cmd == "login":
                return run_login(args)
            if cmd == "whoami":
                return run_whoami(args)
            if cmd == "pay":
                return run_pay(args)
            if cmd == "open":
                return run_open(args)
            return run_review(args)
        except BpostError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    p = argparse.ArgumentParser(description="Create a bpost parcel order (no payment link until you ask).")
    src = p.add_argument_group("shipment source")
    src.add_argument("--shopify", metavar="ORDER", help="Shopify order number on rebelchalk, e.g. 1002 or #1002")
    src.add_argument("--to", metavar="ADDRESS", help="pasted address: 'Name, Street 12, 2000 City, Country' or the same in newlines")
    src.add_argument("--json", metavar="FILE", help="JSON file with a list of shipments")
    src.add_argument("--name", help="recipient full name")
    src.add_argument("--street", help="street (may include the number)")
    src.add_argument("--number", help="street number, if not part of --street")
    src.add_argument("--zip")
    src.add_argument("--city")
    src.add_argument("--country", default="BE")
    src.add_argument("--email", help="recipient email (defaults to the sender email)")
    src.add_argument("--phone")
    src.add_argument("--weight-kg", "--weight", dest="weight_kg", type=float, help="parcel weight in kg")
    src.add_argument("--contents", help="customs description for non-EU (what is in the box)")
    src.add_argument("--reference", help="sender reference printed on the label")

    opt = p.add_argument_group("options")
    opt.add_argument("--split", choices=["line", "single"], default="line", help="Shopify: one parcel per line item (default) or one parcel total")
    opt.add_argument("--delivery", choices=sorted(DESTINATIONS), default="pickup", help="default: pickup (pickup point)")
    opt.add_argument("--pickup", help="substring of the pickup point id/name/street")
    opt.add_argument("--product-line", choices=["STANDARD", "ECONOMY", "LOCKER_TO_LOCKER"], help="default: STANDARD domestic, ECONOMY international")
    opt.add_argument("--value-eur", type=float, help="declared value for customs (non-EU)")
    opt.add_argument("--insurance", dest="insurance", action="store_true", default=None,
                     help="force WARRANTY cover (default: on for Belgian parcels, +EUR 1.55/parcel)")
    opt.add_argument("--no-insurance", dest="insurance", action="store_false",
                     help="ship without WARRANTY cover")
    opt.add_argument("--lang", default="nl", choices=["nl", "fr", "en", "de"])
    opt.add_argument("--normalize", dest="normalize", action="store_true", default=True, help="canonicalise BE addresses via bpost proofing (default: on)")
    opt.add_argument("--no-normalize", dest="normalize", action="store_false")
    opt.add_argument("--dry-run", action="store_true", help="build the order, print prices, then delete the draft")
    opt.add_argument("--quote", action="store_true", help="price only: no order is created")
    opt.add_argument("--pay-link", action="store_true", help="also mint the checkout link (skip the review pause)")
    opt.add_argument("--cookie", help="Cookie header value from a logged-in parcel.bpost.be session")
    opt.add_argument("--guest", action="store_true", help="ignore the stored session and act as a guest")
    args = p.parse_args(argv)

    if not args.shopify and not args.json and args.weight_kg is None:
        p.error("--weight is required unless the weight comes from a Shopify order or the JSON file")

    try:
        return run_create(args)
    except BpostError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
