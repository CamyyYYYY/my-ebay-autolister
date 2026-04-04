import os
import re
import json
import base64
import statistics
import requests
import time
import tempfile
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

from dotenv import load_dotenv
from PIL import Image
from openai import OpenAI

load_dotenv()

# Try to activate AVIF plugin if present
try:
    import pillow_avif  # type: ignore
except Exception:
    try:
        from pillow_avif import AvifImagePlugin  # type: ignore  # noqa: F401
    except Exception:
        pass


EBAY_CONFIG = {
    "app_id": os.getenv("EBAY_APP_ID", "").strip(),
    "dev_id": os.getenv("EBAY_DEV_ID", "").strip(),
    "cert_id": os.getenv("EBAY_CERT_ID", "").strip(),
    "user_token": os.getenv("EBAY_USER_TOKEN", "").strip(),
}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".avif")

COMMON_COLORS = [
    "Black", "Blue", "Brown", "Gray", "Grey", "Green", "Beige", "White", "Red",
    "Pink", "Purple", "Yellow", "Orange", "Tan", "Navy", "Olive", "Burgundy",
    "Gold", "Silver", "Cream", "Ivory", "Khaki", "Teal", "Charcoal",
    "Turquoise", "Multicolor"
]

SHELL_FROM_TITLE = {
    "denim": "Denim",
    "leather": "Leather",
    "wool": "Wool",
    "polyester": "Polyester",
    "nylon": "Nylon",
    "suede": "Suede",
    "cotton": "Cotton",
    "corduroy": "Corduroy",
    "canvas": "Canvas",
    "down": "Down",
    "fleece": "Fleece",
    "shell": "Polyester",
}

SIZE_PAT = re.compile(
    r"\b(?:size|sz|tagged|marked)\s*[:\-]?\s*([XSML]{1,3}|XXL|XXXL|[0-9]{2,3})\b",
    re.I
)

CLOTHING_CATEGORY_IDS = {"57988", "11484", "1059"}


def require_ebay_config():
    missing = [k for k, v in EBAY_CONFIG.items() if not v]
    if missing:
        raise RuntimeError(
            f"Missing eBay credentials in .env: {', '.join(missing)}"
        )


def require_openai():
    if client is None:
        raise RuntimeError("Missing OPENAI_API_KEY in .env")


def median(vals):
    try:
        nums = [float(v) for v in vals if v is not None]
        return statistics.median(nums) if nums else None
    except Exception:
        return None


def norm_space(s: str) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    s = s.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", s).strip()


def b64_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _http_body_snippet(content: bytes, limit: int = 600) -> str:
    try:
        raw = (content or b"")[:limit]
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _parse_ebay_xml(content: bytes):
    data = content or b""
    if not data.strip():
        raise ET.ParseError("Empty response body")
    return ET.fromstring(data)


def x(v) -> str:
    return _xml_escape(str(v or ""), {'"': "&quot;", "'": "&apos;"})


def normalize_image_path(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()

    if ext not in SUPPORTED_EXTS or ext == ".avif":
        try:
            im = Image.open(path)
            im.load()
            im = im.convert("RGB")
            tmpdir = os.path.join(tempfile.gettempdir(), "ebay_autolister_norm")
            os.makedirs(tmpdir, exist_ok=True)
            out = os.path.join(tmpdir, f"norm_{int(time.time() * 1000)}.jpg")
            im.save(out, "JPEG", quality=92)
            return out
        except Exception:
            return path

    return path


def ebay_get_oauth_token():
    require_ebay_config()

    url = "https://api.ebay.com/identity/v1/oauth2/token"
    basic = base64.b64encode(
        f"{EBAY_CONFIG['app_id']}:{EBAY_CONFIG['cert_id']}".encode()
    ).decode()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic}",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }

    r = requests.post(url, headers=headers, data=data, timeout=25)
    r.raise_for_status()
    return r.json()["access_token"]


def ebay_search_by_image(image_path, limit=50):
    token = ebay_get_oauth_token()

    with open(image_path, "rb") as f:
        b64img = base64.b64encode(f.read()).decode()

    url = (
        "https://api.ebay.com/buy/browse/v1/item_summary/"
        f"search_by_image?limit={limit}&fieldgroups=FULL"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    r = requests.post(
        url,
        headers=headers,
        data=json.dumps({"image": b64img}),
        timeout=35,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"eBay image search error {r.status_code}: {r.text[:400]}")

    return r.json().get("itemSummaries") or []


def browse_specifics_to_map(item):
    out = {}
    for spec in (item.get("itemSpecifics") or []):
        name = norm_space(spec.get("name"))
        vals = [
            norm_space(v)
            for v in (spec.get("values") or [])
            if norm_space(v)
        ]
        if name and vals:
            out[name] = vals
    return out


def merge_specifics(*dicts):
    merged = {}
    for d in dicts:
        for k, v in (d or {}).items():
            vals = v if isinstance(v, list) else [v]
            merged.setdefault(k, [])
            for val in vals:
                if val not in merged[k]:
                    merged[k].append(val)
    return merged


def specifics_xml(d):
    blocks = []
    for k, vals in (d or {}).items():
        vals = vals if isinstance(vals, list) else [vals]
        vxml = "".join(f"<Value>{x(val)[:400]}</Value>" for val in vals if val)
        blocks.append(
            f"<NameValueList><Name>{x(k)[:65]}</Name>{vxml}</NameValueList>"
        )
    return "".join(blocks)


def canonical_aspect_name(raw: str) -> str:
    t = norm_space(raw)
    m = re.search(r"the item specific\s+(.+?)\s+is missing", t, re.I)
    if not m:
        m = re.search(r"the item specific name\s+(.+?)\s+is too long", t, re.I)

    name = norm_space(m.group(1) if m else t)
    name = re.sub(r"(add .*|enter .*|then try again.*)$", "", name, flags=re.I).strip(" .:-")
    low = name.lower()

    mapping = {
        "form factor": "Form Factor",
        "brand": "Brand",
        "model": "Model",
        "compatible brand": "Compatible Brand",
        "type": "Type",
        "microphone type": "Microphone Type",
        "size type": "Size Type",
        "outer shell material": "Outer Shell Material",
        "department": "Department",
        "size": "Size",
        "color": "Color",
        "style": "Style",
    }

    for k, v in mapping.items():
        if k in low:
            return v

    return " ".join(w.capitalize() for w in name.split())[:65]


def default_value_for_aspect(name: str, title_hint: str) -> str:
    t = (title_hint or "").lower()

    if name == "Form Factor":
        if "lavalier" in t or "lapel" in t:
            return "Lavalier/Lapel"
        if "headset" in t:
            return "Headset"
        if "handheld" in t or "stand" in t:
            return "Handheld/Stand-Held"
        if "shotgun" in t:
            return "Shotgun"
        return "Does Not Apply"

    if name in ("Type", "Microphone Type"):
        if "wireless" in t:
            return "Wireless"
        if "condenser" in t:
            return "Condenser"
        return "Does Not Apply"

    if name == "Brand":
        return "Unbranded"

    if name == "Size Type":
        return "Regular"

    if name == "Department":
        if any(w in t for w in ["women", "womens", "woman", "ladies"]):
            return "Women"
        if any(w in t for w in ["men", "mens", "man"]):
            return "Men"
        if any(w in t for w in ["boys", "kid", "youth", "girls"]):
            return "Unisex Kids"
        return "Unisex Adults"

    if name == "Type":
        if "vest" in t:
            return "Vest"
        if "coat" in t or "parka" in t or "overcoat" in t:
            return "Coat"
        return "Jacket"

    if name == "Color":
        for c in COMMON_COLORS:
            if re.search(rf"\b{re.escape(c.lower())}\b", t):
                return c
        return "Multicolor"

    if name == "Outer Shell Material":
        for k, v in SHELL_FROM_TITLE.items():
            if k in t:
                return v
        return "Does Not Apply"

    return "Does Not Apply"


def is_clothing_category(cat_hint: dict | None) -> bool:
    if not cat_hint:
        return False

    cid = str(cat_hint.get("id", ""))
    if cid in CLOTHING_CATEGORY_IDS:
        return True

    name = (cat_hint.get("name", "") or "").lower()
    return any(
        w in name
        for w in [
            "coat", "jacket", "vest", "clothing", "apparel",
            "t-shirt", "shirt", "tee", "hoodie", "sweatshirt"
        ]
    )


def infer_apparel_from_title(title: str) -> dict:
    t = title.lower()
    out = {}

    if "petite" in t:
        out["Size Type"] = ["Petite"]
    elif any(w in t for w in ["tall", "big & tall", "big and tall"]):
        out["Size Type"] = ["Big & Tall"]
    elif any(w in t for w in ["junior", "juniors"]):
        out["Size Type"] = ["Juniors"]
    else:
        out["Size Type"] = ["Regular"]

    if any(w in t for w in ["women", "womens", "woman", "ladies"]):
        out["Department"] = ["Women"]
    elif any(w in t for w in ["men", "mens", "man's", "male"]):
        out["Department"] = ["Men"]
    elif any(w in t for w in ["girl", "girls"]):
        out["Department"] = ["Girls"]
    elif any(w in t for w in ["boy", "boys", "youth", "kids", "kid"]):
        out["Department"] = ["Boys"]
    else:
        out["Department"] = ["Unisex Adults"]

    if "vest" in t:
        out["Type"] = ["Vest"]
    elif any(w in t for w in ["coat", "parka", "overcoat", "over coat", "over-coat"]):
        out["Type"] = ["Coat"]
    else:
        out["Type"] = ["Jacket"]

    if "denim" in t or "trucker" in t:
        out["Style"] = ["Trucker"]
    elif "bomber" in t:
        out["Style"] = ["Bomber"]
    elif "puffer" in t or "down" in t:
        out["Style"] = ["Puffer"]
    elif "leather" in t:
        out["Style"] = ["Motorcycle"]
    elif "western" in t:
        out["Style"] = ["Western"]

    for c in COMMON_COLORS:
        if re.search(rf"\b{re.escape(c.lower())}\b", t):
            out["Color"] = [c]
            break
    if "Color" not in out:
        out["Color"] = ["Multicolor"]

    for k, v in SHELL_FROM_TITLE.items():
        if k in t:
            out["Outer Shell Material"] = [v]
            break

    m = SIZE_PAT.search(title)
    if m:
        out["Size"] = [m.group(1).upper()]

    return out


def make_description(title, ref=""):
    if client is None:
        return (
            f"<h2>{title}</h2>"
            "<ul><li>Quality build</li><li>Fast shipping</li>"
            "<li>30-day returns</li></ul>"
        )

    try:
        prompt = f"""Create clean HTML (no markdown). Title: {title}
Ref: {ref[:600]}
Include: short intro, <ul><li> highlights, shipping & returns. Concise, professional."""
        r = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Return only valid e-commerce HTML."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=600,
        )
        html = (
            r.choices[0]
            .message
            .content
            .strip()
            .replace("```html", "")
            .replace("```", "")
            .strip()
        )
        return html.strip('"').strip("'")
    except Exception:
        return (
            f"<h2>{title}</h2>"
            "<ul><li>Quality build</li><li>Fast shipping</li>"
            "<li>30-day returns</li></ul>"
        )


def ai_guess_from_image(path: str) -> dict:
    if client is None:
        return {
            "title": "3D Printed Item",
            "description_html": (
                "<h2>3D Printed Item</h2>"
                "<p>High quality. 30-day returns.</p>"
            ),
            "price_suggestion": 9.99,
            "dimensions_in": {"length": 4, "width": 3, "height": 2},
            "weight_oz": 4.0,
            "category_hint": "General",
            "specifics": {},
        }

    try:
        img_b64 = b64_image(path)
        prompt = (
            "You are an e-commerce lister. Look at the image and produce a STRICT JSON object with keys:\n"
            "title (<=80 chars), description_html (short HTML), price_suggestion (number),\n"
            "dimensions_in (object with length,width,height in inches; numbers only),\n"
            "weight_oz (number), category_hint (few words),\n"
            "specifics (object of name -> [values]). If unsure, make a conservative guess.\n"
            "Return JSON only, no extra text."
        )

        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Return valid JSON only."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                        },
                    ],
                },
            ],
            temperature=0.3,
            max_tokens=500,
        )

        raw = r.choices[0].message.content.strip()
        data = json.loads(raw)

        data.setdefault("title", "Untitled")
        data.setdefault("description_html", f"<h2>{data['title']}</h2>")
        data.setdefault("price_suggestion", 9.99)
        data.setdefault("dimensions_in", {"length": 4, "width": 3, "height": 2})
        data.setdefault("weight_oz", 4.0)
        data.setdefault("category_hint", "General")
        data.setdefault("specifics", {})
        return data

    except Exception:
        return {
            "title": "3D Printed Item",
            "description_html": (
                "<h2>3D Printed Item</h2>"
                "<p>High quality. 30-day returns.</p>"
            ),
            "price_suggestion": 9.99,
            "dimensions_in": {"length": 4, "width": 3, "height": 2},
            "weight_oz": 4.0,
            "category_hint": "General",
            "specifics": {},
        }


def upload_site_hosted_pictures(image_paths, log=None, max_photos=24):
    require_ebay_config()

    def say(msg):
        if log:
            log(msg)

    if not image_paths:
        return []

    paths = []
    for p in image_paths:
        if p and os.path.isfile(p):
            p2 = normalize_image_path(p)
            if p2 not in paths:
                paths.append(p2)
        if len(paths) >= max_photos:
            break

    url = "https://api.ebay.com/ws/api.dll"
    headers = {
        "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-DEV-NAME": EBAY_CONFIG["dev_id"],
        "X-EBAY-API-APP-NAME": EBAY_CONFIG["app_id"],
        "X-EBAY-API-CERT-NAME": EBAY_CONFIG["cert_id"],
        "X-EBAY-API-SITEID": "0",
    }

    eps_urls = []
    for path in paths:
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{EBAY_CONFIG['user_token']}</eBayAuthToken></RequesterCredentials>
  <WarningLevel>High</WarningLevel>
</UploadSiteHostedPicturesRequest>"""

        data = {"XML Payload": xml}
        say(f"Uploading image to eBay: {os.path.basename(path)}")

        try:
            with open(path, "rb") as f:
                files = {
                    "file": (os.path.basename(path), f, "application/octet-stream")
                }
                r = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=60,
                )
        except Exception as e:
            say(f"Image upload failed (request error): {e}")
            continue

        try:
            root = _parse_ebay_xml(r.content)
        except ET.ParseError as e:
            snippet = _http_body_snippet(r.content)
            say(
                "Image upload failed (invalid eBay XML response): "
                f"{e}; HTTP {getattr(r, 'status_code', '?')}; "
                f"Body: {snippet or '(empty)'}"
            )
            continue

        ack = root.find(".//{urn:ebay:apis:eBLBaseComponents}Ack")
        if ack is not None and ack.text in ("Success", "Warning"):
            full = root.find(".//{urn:ebay:apis:eBLBaseComponents}FullURL")
            if full is not None and full.text:
                eps_urls.append(full.text)
                say("Image uploaded ✓")
            else:
                say("Upload succeeded but FullURL missing.")
        else:
            err = root.find(".//{urn:ebay:apis:eBLBaseComponents}LongMessage")
            msg = err.text if err is not None else r.text[:300]
            say(f"Image upload failed: {msg}")

    return eps_urls


def parse_trading_errors(xml_bytes):
    msgs, missing = [], []

    try:
        root = _parse_ebay_xml(xml_bytes)
    except ET.ParseError as e:
        snippet = _http_body_snippet(xml_bytes)
        msgs.append(f"Invalid eBay XML response: {e}. Body: {snippet or '(empty)'}")
        return False, msgs, missing

    ack = root.find(".//{urn:ebay:apis:eBLBaseComponents}Ack")
    success = ack is not None and ack.text in ("Success", "Warning")

    for node in root.findall(".//{urn:ebay:apis:eBLBaseComponents}Errors"):
        sm = node.find("{urn:ebay:apis:eBLBaseComponents}ShortMessage")
        lm = node.find("{urn:ebay:apis:eBLBaseComponents}LongMessage")
        text = norm_space((lm.text or sm.text or ""))
        if text:
            msgs.append(text)
            for pat in (
                r"the item specific\s+(.+?)\s+is missing",
                r"the item specific name\s+(.+?)\s+is too long",
            ):
                m = re.search(pat, text, re.I)
                if m:
                    missing.append(canonical_aspect_name(m.group(1)))

    missing = list(dict.fromkeys(missing))
    return success, msgs, missing


def addfixedpriceitem_xml(
    title,
    price,
    description,
    category,
    specifics,
    picture_urls=None,
    quantity=1,
    condition_id="1000",
    condition_desc=None,
    best_offer=True,
):
    pics = "".join(f"<PictureURL>{x(u)}</PictureURL>" for u in (picture_urls or []))
    if not pics:
        pics = (
            "<PictureURL>"
            "https://via.placeholder.com/1200x1200.png?text=Photo+coming+soon"
            "</PictureURL>"
        )

    cond_desc_xml = (
        f"<ConditionDescription><![CDATA[{(condition_desc or '')[:999]}]]></ConditionDescription>"
        if condition_desc and condition_id != "1000"
        else ""
    )

    best_offer_xml = (
        "<BestOfferDetails><BestOfferEnabled>"
        f"{str(bool(best_offer)).lower()}"
        "</BestOfferEnabled></BestOfferDetails>"
    )

    return f"""<?xml version="1.0" encoding="utf-8"?>
<AddFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{x(EBAY_CONFIG['user_token'])}</eBayAuthToken></RequesterCredentials>
  <Item>
    <Title>{x(title[:80])}</Title>
    <Description><![CDATA[{description}]]></Description>
    <PrimaryCategory><CategoryID>{x(category['id'])}</CategoryID></PrimaryCategory>
    <StartPrice>{x(price)}</StartPrice>
    <Country>US</Country><Currency>USD</Currency>
    <DispatchTimeMax>3</DispatchTimeMax>
    <ListingDuration>GTC</ListingDuration>
    <ListingType>FixedPriceItem</ListingType>
    <Quantity>{x(quantity)}</Quantity>
    <ConditionID>{x(condition_id)}</ConditionID>
    {cond_desc_xml}
    <PostalCode>90210</PostalCode>
    <ItemSpecifics>{specifics_xml(specifics)}</ItemSpecifics>
    <ReturnPolicy>
      <ReturnsAcceptedOption>ReturnsAccepted</ReturnsAcceptedOption>
      <RefundOption>MoneyBack</RefundOption>
      <ReturnsWithinOption>Days_30</ReturnsWithinOption>
      <ShippingCostPaidByOption>Buyer</ShippingCostPaidByOption>
    </ReturnPolicy>
    <ShippingDetails>
      <ShippingType>Flat</ShippingType>
      <ShippingServiceOptions>
        <ShippingServicePriority>1</ShippingServicePriority>
        <ShippingService>USPSMedia</ShippingService>
        <ShippingServiceCost>4.99</ShippingServiceCost>
      </ShippingServiceOptions>
    </ShippingDetails>
    {best_offer_xml}
    <PictureDetails>{pics}</PictureDetails>
  </Item>
</AddFixedPriceItemRequest>"""


def create_ebay_listing(
    title,
    price,
    description,
    category_hint=None,
    inferred_specifics=None,
    log=None,
    local_images=None,
    quantity=1,
    condition_id="1000",
    condition_desc=None,
    best_offer=True,
):
    require_ebay_config()

    def say(msg):
        if log:
            log(msg)

    if category_hint and category_hint.get("id"):
        category = {
            "id": str(category_hint["id"]),
            "name": category_hint.get("name", "Suggested"),
            "condition": condition_id,
        }
    else:
        category = {
            "id": "42428",
            "name": "Tools",
            "condition": condition_id,
        }

    specifics = merge_specifics({"Brand": ["Unbranded"]}, inferred_specifics or {})

    if category_hint and "microphone" in (category_hint.get("name", "").lower()):
        if "Form Factor" not in specifics:
            specifics["Form Factor"] = [default_value_for_aspect("Form Factor", title)]

    if is_clothing_category(category_hint):
        must = ["Size Type", "Department", "Type", "Color", "Size"]
        inferred = infer_apparel_from_title(title)
        specifics = merge_specifics(specifics, inferred)

        if "Outer Shell Material" not in specifics:
            specifics["Outer Shell Material"] = [
                default_value_for_aspect("Outer Shell Material", title)
            ]

        if "Style" not in specifics:
            guess_style = infer_apparel_from_title(title).get("Style")
            if guess_style:
                specifics["Style"] = guess_style

        still_missing = [m for m in must if m not in specifics or not specifics[m]]
        if still_missing:
            raise RuntimeError("APPAREL_MISSING:" + ",".join(still_missing))

    picture_urls = upload_site_hosted_pictures(local_images or [], log=log, max_photos=24)

    headers = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
        "X-EBAY-API-DEV-NAME": EBAY_CONFIG["dev_id"],
        "X-EBAY-API-APP-NAME": EBAY_CONFIG["app_id"],
        "X-EBAY-API-CERT-NAME": EBAY_CONFIG["cert_id"],
        "X-EBAY-API-CALL-NAME": "AddFixedPriceItem",
        "X-EBAY-API-SITEID": "0",
        "Content-Type": "text/xml",
    }
    url = "https://api.ebay.com/ws/api.dll"

    for attempt in range(1, 4):
        xml = addfixedpriceitem_xml(
            title=title,
            price=price,
            description=description,
            category=category,
            specifics=specifics,
            picture_urls=picture_urls,
            quantity=quantity,
            condition_id=condition_id,
            condition_desc=condition_desc,
            best_offer=best_offer,
        )

        r = requests.post(url, headers=headers, data=xml, timeout=35)
        success, messages, missing = parse_trading_errors(r.content)

        if success:
            try:
                root = _parse_ebay_xml(r.content)
            except ET.ParseError as e:
                say(
                    "Listing succeeded but response was not valid XML: "
                    f"{e}. Body: {_http_body_snippet(r.content) or '(empty)'}"
                )
                return None, None

            node = root.find(".//{urn:ebay:apis:eBLBaseComponents}ItemID")
            if node is not None:
                return node.text, category["name"]

            say("Listing succeeded but ItemID missing.")
            return None, None

        for m in messages:
            if "funds from your sales may be unavailable" not in m.lower():
                say(f"eBay Error: {m}")

        if not missing:
            break

        chosen = []
        for raw in missing:
            name = canonical_aspect_name(raw)
            if name not in specifics or not specifics[name]:
                val = default_value_for_aspect(name, title)
                specifics[name] = [val]
                chosen.append(f"{name}={val}")

        if chosen:
            say("Missing specifics added: " + ", ".join(chosen) + f" (retry {attempt}/3)")

    return None, None