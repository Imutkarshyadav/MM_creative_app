from flask import Flask, jsonify, request, send_from_directory
import urllib.request
import urllib.error
import json
import gzip
import time
import base64
import hmac
import io
import hashlib
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
import os
import random
import re
import secrets
import threading
from urllib.parse import urlparse

from python_calamine import CalamineWorkbook

app = Flask(__name__)

SHEET_ID = "128O1hciqdQ2eClfQ6O0m_klXecO6d8hsNvXSCMoXQMI"
URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
MISSING_STOCK_SHEET_ID = "108x7r9upB9CzexlHu91GkIBuN4x3Y4snrUV3GXrXY7c"
MISSING_STOCK_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{MISSING_STOCK_SHEET_ID}/edit"
HISTORY_FILE = '/tmp/creative_history.json'

cache = {
    "timestamp": 0,
    "data": None,
    "last_raw_excel": None,
    "local_date": None
}
CACHE_DURATION = 15
MISSING_STOCK_CACHE_DURATION = 300
missing_stock_cache = {"timestamp": 0, "tabs": [], "entries": {}}
SHEET_GID_PATTERN = re.compile(
    r'\[(\d+),0,\\"(\d+)\\",\[\{\\"1\\":\[\[0,0,\\"([^\\"]+)\\"'
)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_SHEET_NAME = "Daily Report"
PUSH_SUBSCRIPTIONS_KEY = "creative-report:push:subscriptions"
PUSH_SNAPSHOT_KEY = "creative-report:push:last-snapshot"
PUSH_CHECK_INTERVAL_SECONDS = 30
push_check_cache = {"checked_at": 0}
ADMIN_SETTINGS_KEY = "creative-report:admin:settings"
ADMIN_COOKIE_NAME = "creative_admin_session"
ADMIN_SESSION_SECONDS = 4 * 60 * 60
ADMIN_PASSWORD_HASH_ITERATIONS = 600_000
ADMIN_PASSWORD_MIN_LENGTH = 12
admin_login_attempts = {}
admin_login_lock = threading.Lock()


@app.after_request
def compress_sync_response(response):
    if (
        request.path != "/api/sync"
        or response.status_code != 200
        or response.headers.get("Content-Encoding")
        or request.accept_encodings.quality("gzip") <= 0
    ):
        return response

    response_body = response.get_data()
    if len(response_body) <= 1024:
        return response

    response.set_data(gzip.compress(response_body, compresslevel=5, mtime=0))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    return response


@app.route("/api/push/config", methods=["GET"])
def get_push_config():
    return jsonify(push_configuration())


@app.route("/api/admin/config", methods=["GET", "POST", "PUT"])
def admin_config():
    if request.method != "GET" and not request.is_json:
        response = jsonify({"success": False, "error": "Expected a JSON request."})
        response.headers["Cache-Control"] = "no-store"
        return response, 400
    payload = request.get_json(silent=True) if request.method != "GET" else {}
    if not isinstance(payload, dict):
        response = jsonify({"success": False, "error": "Expected a JSON object."})
        response.headers["Cache-Control"] = "no-store"
        return response, 400

    try:
        status, result, token = admin_api_request(
            request.method,
            payload,
            request.cookies.get(ADMIN_COOKIE_NAME, ""),
            request.remote_addr or ""
        )
    except RuntimeError as error:
        print(f"Admin settings request failed: {error}")
        response = jsonify({"success": False, "error": str(error)})
        response.headers["Cache-Control"] = "no-store"
        return response, 503

    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    if token == "clear":
        response.delete_cookie(ADMIN_COOKIE_NAME, path="/", httponly=True, samesite="Lax")
    elif token:
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        response.set_cookie(
            ADMIN_COOKIE_NAME,
            token,
            max_age=ADMIN_SESSION_SECONDS,
            httponly=True,
            secure=request.is_secure or forwarded_proto == "https",
            samesite="Lax",
            path="/"
        )
    return response, status


def get_daily_report_events(report_date):
    endpoint_url = os.environ.get("AUDIT_WEB_APP_URL", "").strip()
    api_token = os.environ.get("AUDIT_API_TOKEN", "").strip()
    if not endpoint_url or not api_token:
        raise RuntimeError(
            "Private audit access is not configured. Set AUDIT_WEB_APP_URL and AUDIT_API_TOKEN on the app server."
        )

    payload = json.dumps({
        "action": "get_daily_report",
        "date": report_date,
        "token": api_token
    }).encode("utf-8")
    endpoint_request = urllib.request.Request(
        endpoint_url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(endpoint_request, timeout=8) as response:
            response_body = response.read(1024 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise RuntimeError("Could not connect to the private audit endpoint.") from error
    if len(response_body) > 1024 * 1024:
        raise RuntimeError("The private audit endpoint returned too much data.")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("The private audit endpoint returned invalid JSON.") from error
    if not isinstance(result, dict) or result.get("success") is not True:
        message = result.get("error") if isinstance(result, dict) else None
        raise RuntimeError(message or "The private audit endpoint rejected the request.")
    events = result.get("events")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise RuntimeError("The private audit endpoint returned an invalid event list.")
    return {
        "events": events,
        "tracking_enabled": result.get("tracking_enabled") is True
    }


@app.route("/api/admin/daily-report", methods=["GET"])
def admin_daily_report():
    response_headers = {"Cache-Control": "no-store"}
    try:
        settings = get_admin_settings()
        session_token = request.cookies.get(ADMIN_COOKIE_NAME, "")
        if not is_valid_admin_session(session_token, settings):
            response = jsonify({"success": False, "error": "Admin sign-in is required."})
            response.headers.update(response_headers)
            return response, 401
        report_date = request.args.get("date", "")
        try:
            parsed_date = datetime.strptime(report_date, "%Y-%m-%d").date()
        except ValueError:
            response = jsonify({"success": False, "error": "A valid report date is required."})
            response.headers.update(response_headers)
            return response, 400
        if parsed_date.isoformat() != report_date:
            response = jsonify({"success": False, "error": "A valid report date is required."})
            response.headers.update(response_headers)
            return response, 400

        report = get_daily_report_events(report_date)
        response = jsonify({"success": True, **report})
        response.headers.update(response_headers)
        return response
    except RuntimeError as error:
        print(f"Daily Report request failed: {error}")
        response = jsonify({"success": False, "error": str(error)})
        response.headers.update(response_headers)
        return response, 503


def get_admin_settings():
    if os.environ.get("UPSTASH_REDIS_REST_URL") and os.environ.get("UPSTASH_REDIS_REST_TOKEN"):
        stored = upstash_command("GET", ADMIN_SETTINGS_KEY)
        if stored:
            try:
                settings = json.loads(stored)
            except (TypeError, ValueError) as error:
                raise RuntimeError("Stored admin settings are invalid.") from error
            if not isinstance(settings, dict) or not isinstance(settings.get("color_settings_enabled"), bool):
                raise RuntimeError("Stored admin settings have an invalid format.")
            return settings
        return {"color_settings_enabled": True}

    if os.environ.get("VERCEL"):
        raise RuntimeError("Persistent admin settings require UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN.")

    settings_path = os.environ.get(
        "ADMIN_SETTINGS_FILE",
        os.path.join(ROOT_DIR, "admin_settings.json")
    )
    if not os.path.exists(settings_path):
        return {"color_settings_enabled": True}
    try:
        with open(settings_path, "r", encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
    except (OSError, ValueError) as error:
        raise RuntimeError("Could not read the saved admin settings.") from error
    if not isinstance(settings, dict) or not isinstance(settings.get("color_settings_enabled"), bool):
        raise RuntimeError("Stored admin settings have an invalid format.")
    return settings


def save_admin_settings(settings):
    if os.environ.get("UPSTASH_REDIS_REST_URL") and os.environ.get("UPSTASH_REDIS_REST_TOKEN"):
        upstash_command("SET", ADMIN_SETTINGS_KEY, json.dumps(settings))
        return
    if os.environ.get("VERCEL"):
        raise RuntimeError("Persistent admin settings require UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN.")

    settings_path = os.environ.get(
        "ADMIN_SETTINGS_FILE",
        os.path.join(ROOT_DIR, "admin_settings.json")
    )
    try:
        with open(settings_path, "w", encoding="utf-8") as settings_file:
            json.dump(settings, settings_file, indent=2)
    except OSError as error:
        raise RuntimeError("Could not save the admin settings.") from error


def get_admin_session_secret(settings):
    password_hash = settings.get("admin_password_hash")
    password_salt = settings.get("admin_password_salt")
    if password_hash is not None or password_salt is not None:
        if (
            not isinstance(password_hash, str)
            or not isinstance(password_salt, str)
            or not re.fullmatch(r"[0-9a-f]{64}", password_hash)
            or not re.fullmatch(r"[0-9a-f]{32}", password_salt)
        ):
            raise RuntimeError("Stored admin credentials have an invalid format.")
        return bytes.fromhex(password_hash)
    password = os.environ.get("ADMIN_PASSWORD", "")
    return password.encode("utf-8") if password else b""


def verify_admin_password(password, settings):
    password_hash = settings.get("admin_password_hash")
    password_salt = settings.get("admin_password_salt")
    if password_hash is not None or password_salt is not None:
        if (
            not isinstance(password_hash, str)
            or not isinstance(password_salt, str)
            or not re.fullmatch(r"[0-9a-f]{64}", password_hash)
            or not re.fullmatch(r"[0-9a-f]{32}", password_salt)
        ):
            raise RuntimeError("Stored admin credentials have an invalid format.")
        derived_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(password_salt),
            ADMIN_PASSWORD_HASH_ITERATIONS
        )
        return hmac.compare_digest(derived_hash, bytes.fromhex(password_hash))
    configured_password = os.environ.get("ADMIN_PASSWORD", "")
    return bool(configured_password) and hmac.compare_digest(
        password.encode("utf-8"),
        configured_password.encode("utf-8")
    )


def hash_admin_password(password):
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        ADMIN_PASSWORD_HASH_ITERATIONS
    )
    return {
        "admin_password_salt": salt.hex(),
        "admin_password_hash": password_hash.hex()
    }


def create_admin_session_token(session_secret):
    expires_at = int(time.time()) + ADMIN_SESSION_SECONDS
    payload = str(expires_at)
    signature = hmac.new(session_secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def is_valid_admin_session(token, settings):
    session_secret = get_admin_session_secret(settings)
    if not session_secret or not token:
        return False
    try:
        expires_text, signature = token.split(".", 1)
        expires_at = int(expires_text)
    except (ValueError, TypeError):
        return False
    if expires_at <= time.time() or expires_at > time.time() + ADMIN_SESSION_SECONDS:
        return False
    expected_signature = hmac.new(session_secret, expires_text.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected_signature)


def admin_api_request(method, payload=None, session_token="", remote_address=""):
    if method == "GET":
        settings = get_admin_settings()
        session_secret = get_admin_session_secret(settings)
        return 200, {
            "color_settings_enabled": settings["color_settings_enabled"],
            "authenticated": is_valid_admin_session(session_token, settings),
            "password_configured": bool(session_secret)
        }, None

    if method == "POST":
        if payload.get("action") == "logout":
            return 200, {"success": True}, "clear"
        settings = get_admin_settings()
        if payload.get("action") == "change_password":
            if not is_valid_admin_session(session_token, settings):
                return 401, {"success": False, "error": "Admin sign-in is required."}, None
            new_password = payload.get("new_password")
            confirm_password = payload.get("confirm_password")
            if not isinstance(new_password, str) or not isinstance(confirm_password, str):
                return 400, {"success": False, "error": "Enter and confirm the new password."}, None
            if len(new_password) < ADMIN_PASSWORD_MIN_LENGTH:
                return 400, {
                    "success": False,
                    "error": f"New password must be at least {ADMIN_PASSWORD_MIN_LENGTH} characters."
                }, None
            if len(new_password) > 1024:
                return 400, {"success": False, "error": "New password is too long."}, None
            if not hmac.compare_digest(new_password.encode("utf-8"), confirm_password.encode("utf-8")):
                return 400, {"success": False, "error": "New passwords do not match."}, None
            settings.update(hash_admin_password(new_password))
            save_admin_settings(settings)
            session_secret = get_admin_session_secret(settings)
            return 200, {"success": True}, create_admin_session_token(session_secret)

        supplied_password = payload.get("password")
        if not get_admin_session_secret(settings):
            return 503, {"success": False, "error": "Admin access is not configured. Set the ADMIN_PASSWORD environment variable."}, None
        if not isinstance(supplied_password, str):
            return 400, {"success": False, "error": "Enter the admin password."}, None

        now = time.time()
        with admin_login_lock:
            attempts = [attempt for attempt in admin_login_attempts.get(remote_address, []) if now - attempt < 300]
            if len(attempts) >= 5:
                admin_login_attempts[remote_address] = attempts
                return 429, {"success": False, "error": "Too many password attempts. Wait five minutes and try again."}, None
            if not verify_admin_password(supplied_password, settings):
                attempts.append(now)
                admin_login_attempts[remote_address] = attempts
                return 401, {"success": False, "error": "Incorrect admin password."}, None
            admin_login_attempts.pop(remote_address, None)
        session_secret = get_admin_session_secret(settings)
        return 200, {
            "success": True,
            "color_settings_enabled": settings["color_settings_enabled"],
            "authenticated": True
        }, create_admin_session_token(session_secret)

    if method == "PUT":
        settings = get_admin_settings()
        if not is_valid_admin_session(session_token, settings):
            return 401, {"success": False, "error": "Admin sign-in is required."}, None
        enabled = payload.get("color_settings_enabled")
        if not isinstance(enabled, bool):
            return 400, {"success": False, "error": "color_settings_enabled must be true or false."}, None
        settings["color_settings_enabled"] = enabled
        save_admin_settings(settings)
        return 200, {"success": True, "color_settings_enabled": enabled}, None

    return 405, {"success": False, "error": "Method not allowed."}, None


def spreadsheet_column_name(column_index):
    column = column_index + 1
    name = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        name = chr(65 + remainder) + name
    return name


def cell_text(value):
    return str(value).strip() if value is not None else ""


def is_completed_status(status):
    normalized_status = status.strip().upper()
    return "DONE" in normalized_status or "IGNORE" in normalized_status or normalized_status == "Y"


def is_stock_creative(value):
    if value is None or isinstance(value, (date, datetime)):
        return False
    text = cell_text(value)
    if not text or re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{4}", text):
        return False
    if re.search(r"\((?:[^)]*(?:JSG|MET|STACK)\s*\d*[^)]*)\)\s*$", text, re.IGNORECASE):
        return False
    return True


def parse_stock_sheet(sheet_name, rows, gid):
    entries = []
    campaign_aliases = {}
    header_row_index = 0
    campaign_name_column = None

    for row_index, row in enumerate(rows):
        marker_column = next(
            (index for index, value in enumerate(row) if cell_text(value).casefold() == "campaign name"),
            None
        )
        if marker_column is not None:
            header_row_index = row_index + 1
            campaign_name_column = marker_column
            break

    if campaign_name_column is not None:
        for row in rows[header_row_index:]:
            if campaign_name_column >= len(row):
                continue
            campaign_name = cell_text(row[campaign_name_column])
            match = re.search(r"\(([^()]+)\)\s*$", campaign_name)
            if campaign_name and match:
                campaign_aliases[match.group(1).strip().casefold()] = campaign_name

    if sheet_name.strip().casefold() == "campaigns list":
        if not rows:
            return entries
        headers = [cell_text(value).casefold() for value in rows[0]]
        campaign_index = next((i for i, value in enumerate(headers) if value == "campaign name"), None)
        creative_index = next((i for i, value in enumerate(headers) if "creative" in value or "code" in value), None)
        if campaign_index is None or creative_index is None:
            raise ValueError("The CAMPAIGNS LIST tab is missing campaign or creative columns.")
        for row_index, row in enumerate(rows[1:], start=1):
            if campaign_index >= len(row) or creative_index >= len(row):
                continue
            campaign = cell_text(row[campaign_index])
            creative_value = row[creative_index]
            creative = cell_text(creative_value)
            if not campaign or not is_stock_creative(creative_value):
                continue
            cell = f"{spreadsheet_column_name(creative_index)}{row_index + 1}"
            entries.append({
                "campaign": campaign,
                "campaign_code": "",
                "creative": creative,
                "row": row_index + 1,
                "column": creative_index,
                "cell": cell,
                "gid": gid
            })
        return entries

    if header_row_index >= len(rows):
        return entries

    headers = rows[header_row_index]
    end_column = campaign_name_column if campaign_name_column is not None else len(headers)
    for column_index, header_value in enumerate(headers[:end_column]):
        campaign = cell_text(header_value)
        if not campaign:
            continue
        code_match = re.match(r"^([^\s(]+)", campaign)
        campaign_code = code_match.group(1).strip() if code_match else campaign
        aliases = [
            campaign,
            campaign_code,
            campaign_aliases.get(campaign_code.casefold(), "")
        ]
        for row_index in range(header_row_index + 1, len(rows)):
            row = rows[row_index]
            if column_index >= len(row):
                continue
            creative_value = row[column_index]
            creative = cell_text(creative_value)
            if not is_stock_creative(creative_value):
                continue
            cell = f"{spreadsheet_column_name(column_index)}{row_index + 1}"
            entries.append({
                "campaign": campaign,
                "campaign_aliases": [alias for alias in aliases if alias],
                "campaign_code": campaign_code,
                "creative": creative,
                "row": row_index + 1,
                "column": column_index,
                "cell": cell,
                "gid": gid
            })
    return entries


def extract_sheet_tab_colors(workbook_data):
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    colors = {}

    with zipfile.ZipFile(io.BytesIO(workbook_data)) as archive:
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            relationship.get("Id"): relationship.get("Target")
            for relationship in relationships_root.findall(f"{{{package_ns}}}Relationship")
        }

        for sheet in workbook_root.findall(f"./{{{spreadsheet_ns}}}sheets/{{{spreadsheet_ns}}}sheet"):
            sheet_name = sheet.get("name")
            relationship_id = sheet.get(f"{{{relationship_ns}}}id")
            target = relationships.get(relationship_id)
            if not sheet_name or not target:
                continue
            sheet_path = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target))
            )
            sheet_root = ET.fromstring(archive.read(sheet_path))
            tab_color = sheet_root.find(
                f"./{{{spreadsheet_ns}}}sheetPr/{{{spreadsheet_ns}}}tabColor"
            )
            rgb = tab_color.get("rgb", "") if tab_color is not None else ""
            if re.fullmatch(r"(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})", rgb):
                colors[sheet_name] = f"#{rgb[-6:]}"

    return colors


def extract_sheet_cell_colors(workbook_data):
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    indexed_colors = {
        "10": "#FF0000",
        "11": "#00FF00",
        "12": "#0000FF",
        "13": "#FFFF00",
        "14": "#FF00FF",
        "15": "#00FFFF"
    }
    cell_colors = {}

    with zipfile.ZipFile(io.BytesIO(workbook_data)) as archive:
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            relationship.get("Id"): relationship.get("Target")
            for relationship in relationships_root.findall(f"{{{package_ns}}}Relationship")
        }
        if "xl/styles.xml" not in archive.namelist():
            return {}
        styles_root = ET.fromstring(archive.read("xl/styles.xml"))
        fills = styles_root.find(f"{{{spreadsheet_ns}}}fills")
        cell_xfs = styles_root.find(f"{{{spreadsheet_ns}}}cellXfs")
        style_colors = []

        for style in cell_xfs or ():
            fill_index = int(style.get("fillId", "0"))
            colors = []
            if fills is not None and fill_index < len(fills):
                color_element = fills[fill_index].find(
                    f"./{{{spreadsheet_ns}}}patternFill/{{{spreadsheet_ns}}}fgColor"
                )
                if color_element is not None:
                    rgb = color_element.get("rgb", "")
                    if re.fullmatch(r"(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})", rgb):
                        color = f"#{rgb[-6:].upper()}"
                    else:
                        color = indexed_colors.get(color_element.get("indexed", ""))
                    if color:
                        colors.append(color)
            style_colors.append(colors)

        for sheet in workbook_root.findall(f"./{{{spreadsheet_ns}}}sheets/{{{spreadsheet_ns}}}sheet"):
            sheet_name = sheet.get("name")
            target = relationships.get(sheet.get(f"{{{relationship_ns}}}id"))
            if not sheet_name or not target:
                continue
            sheet_path = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target))
            )
            sheet_root = ET.fromstring(archive.read(sheet_path))
            colors_by_cell = {}
            for cell in sheet_root.findall(f".//{{{spreadsheet_ns}}}c"):
                style_index = int(cell.get("s", "0"))
                if style_index >= len(style_colors):
                    continue
                colors = style_colors[style_index]
                if colors:
                    colors_by_cell[cell.get("r", "")] = colors
            cell_colors[sheet_name] = colors_by_cell

    return cell_colors


def load_missing_stock():
    current_time = time.time()
    if (
        missing_stock_cache["tabs"]
        and current_time - missing_stock_cache["timestamp"] < MISSING_STOCK_CACHE_DURATION
    ):
        return missing_stock_cache

    headers = {"User-Agent": "Mozilla/5.0"}
    workbook_request = urllib.request.Request(
        f"https://docs.google.com/spreadsheets/d/{MISSING_STOCK_SHEET_ID}/export?format=xlsx",
        headers=headers
    )
    with urllib.request.urlopen(workbook_request, timeout=8) as response:
        workbook_data = response.read()
    if not workbook_data.startswith(b"PK"):
        raise RuntimeError("The linked stock spreadsheet did not return an Excel workbook.")
    workbook = CalamineWorkbook.from_filelike(io.BytesIO(workbook_data))
    tab_colors = extract_sheet_tab_colors(workbook_data)
    cell_colors = extract_sheet_cell_colors(workbook_data)

    metadata_request = urllib.request.Request(
        f"{MISSING_STOCK_SHEET_URL}?usp=sharing",
        headers=headers
    )
    with urllib.request.urlopen(metadata_request, timeout=8) as response:
        spreadsheet_html = response.read().decode("utf-8", "replace")
    gids = {
        name: gid
        for _, gid, name in SHEET_GID_PATTERN.findall(spreadsheet_html)
    }
    if any(name not in gids for name in workbook.sheet_names):
        raise RuntimeError("Could not map all stock spreadsheet tabs to their links.")

    entries_by_tab = {}
    tabs = []
    for sheet_name in workbook.sheet_names:
        rows = workbook.get_sheet_by_name(sheet_name).to_python()
        entries = parse_stock_sheet(sheet_name, rows, gids[sheet_name])
        for entry in entries:
            entry["creative_colors"] = cell_colors.get(sheet_name, {}).get(entry["cell"], [])
        entries_by_tab[sheet_name] = entries
        tabs.append({
            "name": sheet_name,
            "creative_count": len(entries),
            "tab_color": tab_colors.get(sheet_name)
        })

    missing_stock_cache.update({
        "timestamp": current_time,
        "tabs": tabs,
        "entries": entries_by_tab
    })
    return missing_stock_cache


def get_missing_stock_campaign_codes(stock):
    return sorted({
        code
        for entries in stock["entries"].values()
        for entry in entries
        for code in [(entry.get("campaign_code") or "").strip()]
        if code and code[0].isalnum()
    }, key=str.casefold)


@app.route("/api/missing-stock", methods=["GET"])
def get_missing_stock():
    try:
        stock = load_missing_stock()
        tab_name = request.args.get("tab", "").strip()
        tabs_by_name = {tab["name"]: tab for tab in stock["tabs"]}
        if request.args.get("all_tabs") == "true":
            entries = [
                {
                    **entry,
                    "stock_tab": name,
                    "stock_tab_color": tabs_by_name[name]["tab_color"]
                }
                for name, tab_entries in stock["entries"].items()
                for entry in tab_entries
            ]
            return jsonify({
                "success": True,
                "tab": "All stock tabs",
                "sheet_url": MISSING_STOCK_SHEET_URL,
                "entries": entries
            })
        if not tab_name:
            return jsonify({
                "success": True,
                "sheet_url": MISSING_STOCK_SHEET_URL,
                "tabs": stock["tabs"],
                "campaign_codes": get_missing_stock_campaign_codes(stock)
            })
        if tab_name not in stock["entries"]:
            return jsonify({"success": False, "error": "The selected stock tab was not found."}), 404
        return jsonify({
            "success": True,
            "tab": tab_name,
            "sheet_url": MISSING_STOCK_SHEET_URL,
            "entries": [
                {
                    **entry,
                    "stock_tab": tab_name,
                    "stock_tab_color": tabs_by_name[tab_name]["tab_color"]
                }
                for entry in stock["entries"][tab_name]
            ]
        })
    except Exception as error:
        print(f"Could not load missing-creative stock sheet: {error}")
        return jsonify({
            "success": False,
            "error": "Could not load the linked stock spreadsheet. Check that it is shared for viewing and try again."
        }), 503


def is_allowed_push_endpoint(endpoint):
    parsed = urlparse(endpoint)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and (
        hostname == "fcm.googleapis.com"
        or hostname.endswith(".push.apple.com")
        or hostname == "updates.push.services.mozilla.com"
        or hostname.endswith(".notify.windows.com")
    )


@app.route("/api/push/subscribe", methods=["POST"])
def save_push_subscription():
    if not push_configuration()["enabled"]:
        return jsonify({"success": False, "error": "Mobile push is not configured on the server."}), 503
    if not request.is_json:
        return jsonify({"success": False, "error": "Expected a JSON subscription."}), 400

    subscription = request.get_json()
    endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
    keys = subscription.get("keys") if isinstance(subscription, dict) else None
    if (
        not isinstance(endpoint, str)
        or len(endpoint) > 4096
        or not is_allowed_push_endpoint(endpoint)
        or not isinstance(keys, dict)
        or not isinstance(keys.get("p256dh"), str)
        or not isinstance(keys.get("auth"), str)
        or len(keys.get("p256dh", "")) > 256
        or len(keys.get("auth", "")) > 256
    ):
        return jsonify({"success": False, "error": "The push subscription is invalid."}), 400

    subscription_id = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    try:
        existing_count = int(upstash_command("HLEN", PUSH_SUBSCRIPTIONS_KEY) or 0)
        if existing_count >= 100:
            existing = upstash_command("HEXISTS", PUSH_SUBSCRIPTIONS_KEY, subscription_id)
            if not existing:
                return jsonify({
                    "success": False,
                    "error": "This app has reached its 100-device push limit."
                }), 429
        upstash_command(
            "HSET",
            PUSH_SUBSCRIPTIONS_KEY,
            subscription_id,
            json.dumps(subscription, separators=(",", ":"))
        )
    except Exception as error:
        print(f"Could not save push subscription: {error}")
        return jsonify({"success": False, "error": "Could not save this device subscription."}), 503
    return jsonify({"success": True})


@app.route("/api/push/subscribe", methods=["DELETE"])
def delete_push_subscription():
    if not push_configuration()["enabled"]:
        return jsonify({"success": False, "error": "Mobile push is not configured on the server."}), 503
    if not request.is_json:
        return jsonify({"success": False, "error": "Expected a JSON subscription endpoint."}), 400

    payload = request.get_json()
    endpoint = payload.get("endpoint") if isinstance(payload, dict) else None
    if not isinstance(endpoint, str) or not is_allowed_push_endpoint(endpoint):
        return jsonify({"success": False, "error": "The push subscription endpoint is invalid."}), 400

    subscription_id = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    try:
        upstash_command("HDEL", PUSH_SUBSCRIPTIONS_KEY, subscription_id)
    except Exception as error:
        print(f"Could not delete push subscription: {error}")
        return jsonify({"success": False, "error": "Could not remove this device subscription."}), 503
    return jsonify({"success": True})


def push_configuration():
    public_key = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subject = os.environ.get("VAPID_SUBJECT", "").strip()
    redis_url = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
    redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    enabled = bool(
        public_key and private_key and subject.startswith("mailto:")
        and redis_url and redis_token
    )
    return {
        "enabled": enabled,
        "public_key": public_key if enabled else "",
        "missing": [
            name for name, value in (
                ("VAPID_PUBLIC_KEY", public_key),
                ("VAPID_PRIVATE_KEY", private_key),
                ("VAPID_SUBJECT", subject if subject.startswith("mailto:") else ""),
                ("UPSTASH_REDIS_REST_URL", redis_url),
                ("UPSTASH_REDIS_REST_TOKEN", redis_token)
            ) if not value
        ]
    }


def get_client_local_date():
    value = request.headers.get("X-Local-Date", "").strip()
    try:
        parsed_date = date.fromisoformat(value)
        if parsed_date.isoformat() == value:
            return parsed_date
    except ValueError:
        pass
    return date.today()


def upstash_command(*arguments):
    redis_url = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
    redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if not redis_url or not redis_token:
        raise RuntimeError("Push subscription storage is not configured.")

    payload = json.dumps([list(arguments)]).encode("utf-8")
    req = urllib.request.Request(
        f"{redis_url}/pipeline",
        data=payload,
        headers={
            "Authorization": f"Bearer {redis_token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, list) or not result or result[0].get("error"):
        raise RuntimeError("Push subscription storage request failed.")
    return result[0].get("result")


def send_push_notification(subscription, title, body, tag, et_name=None, delay_seconds=0):
    from pywebpush import webpush

    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subject = os.environ.get("VAPID_SUBJECT", "").strip()
    if not subject.startswith("mailto:"):
        raise RuntimeError("VAPID_SUBJECT must be a mailto: address.")

    webpush(
        subscription_info=subscription,
        data=json.dumps({
            "title": title,
            "body": body,
            "tag": tag,
            "etName": et_name,
            "delaySeconds": delay_seconds
        }),
        vapid_private_key=private_key,
        vapid_claims={"sub": subject},
        ttl=60 * 60,
        timeout=5
    )


def deliver_stored_push(subscription_id, subscription, title, body, tag, et_name=None, delay_seconds=0):
    try:
        send_push_notification(
            subscription,
            title,
            body,
            tag,
            et_name,
            delay_seconds
        )
    except Exception as error:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in (404, 410):
            try:
                upstash_command("HDEL", PUSH_SUBSCRIPTIONS_KEY, subscription_id)
            except Exception as cleanup_error:
                print(f"Could not remove expired push subscription: {cleanup_error}")
        print(f"Push delivery failed for one subscription: {error}")


def push_sheet_changes(rows, excluded_subscription_id=None, start_date="", end_date=""):
    global push_check_cache
    config = push_configuration()
    if not config["enabled"]:
        return
    current_time = time.time()
    if current_time - push_check_cache["checked_at"] < PUSH_CHECK_INTERVAL_SECONDS:
        return

    previous_json = upstash_command("GET", PUSH_SNAPSHOT_KEY)
    current_snapshot = {
        f"{item['tab']}|||{item['row_number']}": {
            "tab": item["tab"],
            "row_number": item["row_number"],
            "et": item["et"],
            "status": item["status"],
            "date": item["date"],
            "filter_date": item["filter_date"],
            "is_done": bool(item["is_done"])
        }
        for item in rows
    }
    current_json = json.dumps(current_snapshot, ensure_ascii=False, sort_keys=True)
    if not previous_json:
        upstash_command("SET", PUSH_SNAPSHOT_KEY, current_json)
        push_check_cache = {"checked_at": current_time}
        return

    try:
        previous_snapshot = json.loads(previous_json)
    except (TypeError, ValueError):
        previous_snapshot = {}
    if previous_snapshot == current_snapshot:
        push_check_cache = {"checked_at": current_time}
        return
    upstash_command("SET", PUSH_SNAPSHOT_KEY, current_json)
    push_check_cache = {"checked_at": current_time}

    groups = {}
    for key in sorted(set(previous_snapshot) | set(current_snapshot)):
        previous = previous_snapshot.get(key)
        current = current_snapshot.get(key)
        if previous == current:
            continue
        reference = current or previous
        tab = reference["tab"]
        group = groups.setdefault(tab, {"changes": [], "completed": False})
        if current is None:
            group["changes"].append(f"Removed: {previous['et']}")
        elif previous is None:
            group["changes"].append(f"New creative: {current['et']}")
        else:
            if previous["et"] != current["et"]:
                group["changes"].append(f"Creative renamed: {previous['et']} → {current['et']}")
            if previous["status"] != current["status"]:
                group["changes"].append(f"{current['et']}: {previous['status']} → {current['status']}")
            if previous["date"] != current["date"]:
                group["changes"].append(f"{current['et']} date: {previous['date']} → {current['date']}")

    def completion_totals(snapshot):
        totals = {}
        for item in snapshot.values():
            item_date = str(item.get("filter_date") or item.get("date") or "")
            if (start_date and item_date < start_date) or (end_date and item_date > end_date):
                continue
            stats = totals.setdefault(item["tab"], {"total": 0, "done": 0})
            stats["total"] += 1
            stats["done"] += int(item["is_done"])
        return totals

    previous_totals = completion_totals(previous_snapshot)
    current_totals = completion_totals(current_snapshot)

    for tab, current in current_totals.items():
        previous = previous_totals.get(tab, {"total": 0, "done": 0})
        if current["total"] > 0 and current["done"] == current["total"] \
                and (previous["total"] == 0 or previous["done"] < previous["total"]):
            group = groups.setdefault(tab, {"changes": [], "completed": False})
            group["changes"].insert(0, f"All {current['total']} creatives are done.")
            group["completed"] = True

    if not groups:
        return

    subscriptions = upstash_command("HGETALL", PUSH_SUBSCRIPTIONS_KEY) or []
    for tab, group in sorted(groups.items(), key=lambda item: item[0].casefold()):
        change_count = len(group["changes"])
        update_label = f"{change_count} update{'s' if change_count != 1 else ''}"
        title = (
            f"✅ ET Completed: {tab} — {update_label}"
            if group["completed"]
            else f"{tab} — {update_label}"
        )
        body = "\n".join(group["changes"][:8])
        if change_count > 8:
            body += f"\n+{change_count - 8} more updates"
        for index in range(0, len(subscriptions), 2):
            if index + 1 >= len(subscriptions):
                break
            subscription_id = subscriptions[index]
            if subscription_id == excluded_subscription_id:
                continue
            try:
                subscription = json.loads(subscriptions[index + 1])
            except (TypeError, ValueError) as error:
                print(f"Stored push subscription is invalid: {error}")
                continue
            deliver_stored_push(
                subscriptions[index],
                subscription,
                title,
                body,
                f"sheet-update-{hashlib.sha256(tab.encode('utf-8')).hexdigest()[:16]}",
                tab
            )

    if any(group["completed"] for group in groups.values()):
        pending_groups = [
            (tab, stats["total"] - stats["done"])
            for tab, stats in current_totals.items()
            if stats["total"] - stats["done"] > 0
        ]
        pending_groups.sort(key=lambda item: item[0].casefold())
        pending_creatives = sum(count for _, count in pending_groups)
        if pending_groups:
            pending_title = f"📋 {len(pending_groups)} ET{'s' if len(pending_groups) != 1 else ''} still pending"
            pending_body = (
                f"Pending ETs: {len(pending_groups)}\n"
                f"Pending creatives: {pending_creatives}\n\n"
                + "\n".join(f"{tab} — {count} pending" for tab, count in pending_groups)
            )
        else:
            pending_title = "Hurry 🎉 all ETs Done"
            pending_body = "All ETs are complete. Great work!"
        for index in range(0, len(subscriptions), 2):
            if index + 1 >= len(subscriptions):
                break
            if subscriptions[index] == excluded_subscription_id:
                continue
            try:
                subscription = json.loads(subscriptions[index + 1])
            except (TypeError, ValueError) as error:
                print(f"Stored push subscription is invalid: {error}")
                continue
            deliver_stored_push(
                subscriptions[index],
                subscription,
                pending_title,
                pending_body,
                "pending-ets-summary",
                delay_seconds=5
            )

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {}

def save_history(history):
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2)
    except: pass

@app.route('/api/sync', methods=['GET'])
def sync_data():
    global cache
    try:
        current_time = time.time()
        client_today = get_client_local_date()
        if (
            cache["data"] is not None
            and cache["local_date"] == client_today.isoformat()
            and current_time - cache["timestamp"] < CACHE_DURATION
        ):
            return jsonify({
                "success": True,
                "data": cache["data"]
            })

        # FIX 1: Robust Fetching with Retries & User Agent Rotation to bypass Google IP blocks
        USER_AGENTS = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        ]
        
        excel_data = None
        fetch_error = None
        for attempt in range(3):
            try:
                headers = {
                    'User-Agent': random.choice(USER_AGENTS),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
                }
                req = urllib.request.Request(URL, headers=headers)
                # 8 second timeout to avoid Vercel's 10s hard kill limit
                with urllib.request.urlopen(req, timeout=8) as response:
                    data = response.read()
                    if data.startswith(b'<html') or data.startswith(b'<!DOCTYPE'):
                        raise Exception("Google returned a webpage (rate limited).")
                    excel_data = data
                    cache["last_raw_excel"] = data
                    break
            except Exception as e:
                fetch_error = str(e)
                time.sleep(0.5) # short backoff
        
        if not excel_data:
            if cache["last_raw_excel"]:
                excel_data = cache["last_raw_excel"]
            else:
                raise Exception(f"Failed to fetch from Google Sheets: {fetch_error}")

        wb = CalamineWorkbook.from_filelike(io.BytesIO(excel_data))
        tab_colors = extract_sheet_tab_colors(excel_data)
        cell_colors = extract_sheet_cell_colors(excel_data)
        
        history = load_history()
        history_dirty = False
        results = []

        for sheet_name in wb.sheet_names:
            if sheet_name.strip().casefold() == AUDIT_SHEET_NAME.casefold():
                continue
            ws = wb.get_sheet_by_name(sheet_name)
            sheet_rows_data = ws.to_python()

            headers_found = False
            date_col_idx = 0
            creative_col_idx = None
            status_col_idx = None
            
            last_date = None
            last_explicit_date_row = None
            sheet_rows = []

            for row_number, row in enumerate(sheet_rows_data, start=1):
                if not headers_found:
                    row_strs = [str(cell).upper().strip() if cell else "" for cell in row]
                    c_idx = next((i for i, cell in enumerate(row_strs) if 'CREATIVE' in cell), None)
                    s_idx = next((i for i, cell in enumerate(row_strs) if 'STATUS' in cell), None)

                    if c_idx is not None and s_idx is not None:
                        creative_col_idx = c_idx
                        status_col_idx = s_idx
                        headers_found = True
                    continue

                if creative_col_idx >= len(row):
                    continue

                raw_date = row[date_col_idx] if date_col_idx < len(row) else None
                has_explicit_date = raw_date is not None and str(raw_date).strip() != ""
                if has_explicit_date:
                    last_date = raw_date

                et_name = row[creative_col_idx]
                if et_name is None or str(et_name).strip() == "":
                    continue

                if has_explicit_date:
                    last_explicit_date_row = row_number
                raw_status = row[status_col_idx] if status_col_idx < len(row) else None
                sheet_rows.append((row_number, raw_date if has_explicit_date else last_date, has_explicit_date, et_name, raw_status))

            for row_number, raw_date, has_explicit_date, et_name, raw_status in sheet_rows:
                if not has_explicit_date and (last_explicit_date_row is None or row_number > last_explicit_date_row):
                    raw_date = client_today
                if raw_date is None:
                    continue
                
                tab_name_str = str(sheet_name).strip()
                et_name_str = str(et_name).strip()
                unique_key = f"{tab_name_str}|||{et_name_str}"
                
                if unique_key in history and isinstance(history[unique_key], str):
                    history[unique_key] = {
                        "display_time": history[unique_key],
                        "iso_date": datetime.now().strftime('%Y-%m-%d')
                    }
                    history_dirty = True
                    
                if unique_key not in history:
                    history[unique_key] = {
                        "display_time": datetime.now().strftime('%b %d, %I:%M %p'),
                        "iso_date": datetime.now().strftime('%Y-%m-%d')
                    }
                    history_dirty = True
                
                discovered_display = history[unique_key]["display_time"]
                
                # Date Formatting
                if isinstance(raw_date, (date, datetime)):
                    display_date_str = raw_date.strftime('%Y-%m-%d')
                    filter_date_str = display_date_str
                else:
                    display_date_str = str(raw_date).strip().split(' ')[0]
                    try:
                        filter_date_str = datetime.strptime(display_date_str, '%d/%m/%Y').strftime('%Y-%m-%d')
                    except:
                        filter_date_str = "1970-01-01"
                        
                raw_status_str = str(raw_status).strip() if raw_status is not None and str(raw_status).strip() else "PENDING/BLANK"
                is_done = is_completed_status(raw_status_str)
                
                results.append({
                    "tab": tab_name_str,
                    "row_number": row_number,
                    "date": str(display_date_str),
                    "filter_date": filter_date_str,
                    "et": et_name_str,
                    "status": raw_status_str,
                    "is_done": is_done,
                    "discovered_at": discovered_display,
                    "tab_color": tab_colors.get(tab_name_str),
                    "creative_colors": cell_colors.get(tab_name_str, {}).get(
                        f"{spreadsheet_column_name(creative_col_idx)}{row_number}", []
                    )
                })
                
        if history_dirty:
            save_history(history)

        try:
            push_endpoint = request.headers.get("X-Push-Endpoint", "").strip()
            excluded_subscription_id = (
                hashlib.sha256(push_endpoint.encode("utf-8")).hexdigest()
                if push_endpoint else None
            )
            push_sheet_changes(
                results,
                excluded_subscription_id,
                request.headers.get("X-Alert-Start-Date", "").strip(),
                request.headers.get("X-Alert-End-Date", "").strip()
            )
        except Exception as push_error:
            print(f"Could not process mobile push updates: {push_error}")
            
        cache["data"] = results
        cache["timestamp"] = time.time()
        cache["local_date"] = client_today.isoformat()

        return jsonify({
            "success": True,
            "data": results
        })
        
    except Exception as e:
        print("Error during sync:", str(e))
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/')
def serve_index():
    return send_from_directory(ROOT_DIR, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    if os.path.exists(os.path.join(ROOT_DIR, path)):
        return send_from_directory(ROOT_DIR, path)
    return jsonify({"error": "Not Found"}), 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8024))
    app.run(port=port, debug=True)
