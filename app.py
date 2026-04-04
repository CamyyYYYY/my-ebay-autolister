import os
import json
import tempfile
import uuid
from pathlib import Path

from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

from ebay_core import (
    normalize_image_path,
    ebay_search_by_image,
    browse_specifics_to_map,
    merge_specifics,
    infer_apparel_from_title,
    make_description,
    ai_guess_from_image,
    create_ebay_listing,
    is_clothing_category,
    default_value_for_aspect,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB total upload size


def save_uploaded_files(files):
    saved_paths = []

    for f in files:
        if not f or not f.filename:
            continue

        ext = Path(f.filename).suffix.lower()
        safe_ext = ext if ext else ".jpg"
        unique_name = f"{uuid.uuid4().hex}{safe_ext}"
        dest = UPLOAD_DIR / unique_name
        f.save(dest)
        saved_paths.append(str(dest))

    return saved_paths


def extract_price(item):
    try:
        return float((item.get("price") or {}).get("value"))
    except Exception:
        return None


def build_compare_payload_from_ebay(items):
    top = items[0]
    title = (top.get("title") or "Untitled")[:80]
    short = top.get("shortDescription") or ""

    cats = top.get("categories") or []
    if cats:
        category = {
            "id": str(cats[0].get("categoryId", "42428")),
            "name": cats[0].get("categoryName", "Suggested"),
        }
    else:
        category = {"id": "42428", "name": "Suggested"}

    specifics = browse_specifics_to_map(top)
    description = make_description(title, short)
    apparel = infer_apparel_from_title(title)

    matches = []
    for it in items[:10]:
        matches.append(
            {
                "title": it.get("title", ""),
                "price": extract_price(it),
                "category": ((it.get("categories") or [{}])[0]).get("categoryName", "—"),
            }
        )

    top_price = extract_price(top)

    return {
        "ok": True,
        "source": "ebay",
        "title": title,
        "price": top_price,
        "category": category,
        "specifics": specifics,
        "description": description,
        "apparel": apparel,
        "matches": matches,
    }


def build_compare_payload_from_ai(image_path):
    data = ai_guess_from_image(image_path)
    title = (data.get("title") or "Untitled")[:80]
    specifics = data.get("specifics") or {}
    apparel = infer_apparel_from_title(title)

    category = {
        "id": "42428",
        "name": data.get("category_hint", "General"),
    }

    description = data.get("description_html") or make_description(title, title)

    dims = data.get("dimensions_in") or {}
    weight_oz = data.get("weight_oz")

    if dims or weight_oz:
        parts = []
        if dims:
            parts.append(
                f'{dims.get("length", "?")}" L × {dims.get("width", "?")}" W × {dims.get("height", "?")}" H'
            )
        if weight_oz:
            parts.append(f"{weight_oz} oz")
        description += "<p><strong>Approx. size</strong>: " + " · ".join(parts) + "</p>"

    return {
        "ok": True,
        "source": "ai",
        "title": title,
        "price": float(data.get("price_suggestion", 9.99)),
        "category": category,
        "specifics": specifics,
        "description": description,
        "apparel": apparel,
        "matches": [],
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/compare", methods=["POST"])
def api_compare():
    files = request.files.getlist("images")
    if not files:
        return jsonify({"ok": False, "error": "Please upload at least one image."}), 400

    image_paths = save_uploaded_files(files)
    if not image_paths:
        return jsonify({"ok": False, "error": "No valid images were uploaded."}), 400

    first_image = normalize_image_path(image_paths[0])

    try:
        items = ebay_search_by_image(first_image, limit=10)
        if items:
            payload = build_compare_payload_from_ebay(items)
        else:
            payload = build_compare_payload_from_ai(first_image)
    except Exception as ebay_error:
        try:
            payload = build_compare_payload_from_ai(first_image)
            payload["warning"] = f"eBay image search failed, used AI fallback: {ebay_error}"
        except Exception as ai_error:
            return jsonify(
                {
                    "ok": False,
                    "error": f"eBay compare failed: {ebay_error}. AI fallback failed: {ai_error}",
                }
            ), 500

    payload["uploaded_images"] = image_paths
    return jsonify(payload)


@app.route("/api/create", methods=["POST"])
def api_create():
    title = request.form.get("title", "").strip()
    price = request.form.get("price", "").strip()
    description = request.form.get("description", "").strip()

    if not title:
        return jsonify({"ok": False, "error": "Title is required."}), 400
    if not price:
        return jsonify({"ok": False, "error": "Price is required."}), 400
    if not description:
        return jsonify({"ok": False, "error": "Description is required."}), 400

    category_id = request.form.get("category_id", "").strip() or "42428"
    category_name = request.form.get("category_name", "").strip() or "Custom"

    quantity_raw = request.form.get("quantity", "1").strip()
    try:
        quantity = max(1, int(quantity_raw))
    except ValueError:
        return jsonify({"ok": False, "error": "Quantity must be a whole number."}), 400

    condition_id = request.form.get("condition_id", "1000").strip() or "1000"
    condition_desc = request.form.get("condition_desc", "").strip() or None
    best_offer = request.form.get("best_offer", "true").lower() == "true"

    files = request.files.getlist("images")
    if not files:
        return jsonify({"ok": False, "error": "Please upload at least one image."}), 400

    image_paths = save_uploaded_files(files)
    if not image_paths:
        return jsonify({"ok": False, "error": "No valid images were uploaded."}), 400

    category_hint = {"id": category_id, "name": category_name}

    inferred_specifics = {}
    specifics_json = request.form.get("specifics_json", "").strip()
    if specifics_json:
        try:
            parsed = json.loads(specifics_json)
            if isinstance(parsed, dict):
                inferred_specifics = merge_specifics(inferred_specifics, parsed)
        except Exception:
            return jsonify({"ok": False, "error": "specifics_json is not valid JSON."}), 400

    size = request.form.get("size", "").strip()
    size_type = request.form.get("size_type", "").strip()

    if size:
        inferred_specifics["Size"] = [size]
    if size_type:
        inferred_specifics["Size Type"] = [size_type]

    title_guesses = infer_apparel_from_title(title)
    inferred_specifics = merge_specifics(inferred_specifics, title_guesses)

    if is_clothing_category(category_hint):
        if "Outer Shell Material" not in inferred_specifics:
            inferred_specifics["Outer Shell Material"] = [
                default_value_for_aspect("Outer Shell Material", title)
            ]
        if "Style" not in inferred_specifics and title_guesses.get("Style"):
            inferred_specifics["Style"] = title_guesses["Style"]

    logs = []

    def logger(msg):
        logs.append(str(msg))

    try:
        item_id, used_category_name = create_ebay_listing(
            title=title,
            price=price,
            description=description,
            category_hint=category_hint,
            inferred_specifics=inferred_specifics,
            log=logger,
            local_images=image_paths,
            quantity=quantity,
            condition_id=condition_id,
            condition_desc=condition_desc,
            best_offer=best_offer,
        )
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e), "logs": logs}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "logs": logs}), 500

    if not item_id:
        return jsonify(
            {
                "ok": False,
                "error": "eBay listing creation failed.",
                "logs": logs,
            }
        ), 500

    return jsonify(
        {
            "ok": True,
            "item_id": item_id,
            "category_name": used_category_name,
            "listing_url": f"https://www.ebay.com/itm/{item_id}",
            "logs": logs,
        }
    )


if __name__ == "__main__":
    app.run(debug=True)