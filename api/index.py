from flask import Flask, jsonify, request, send_from_directory
import urllib.request
import json
import time
import io
import hashlib
from datetime import date, datetime
import os
import random
from urllib.parse import urlparse

from python_calamine import CalamineWorkbook

app = Flask(__name__)

SHEET_ID = "128O1hciqdQ2eClfQ6O0m_klXecO6d8hsNvXSCMoXQMI"
URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
HISTORY_FILE = '/tmp/creative_history.json'

cache = {
    "timestamp": 0,
    "data": None,
    "audit_events": [],
    "audit_tracking": False,
    "last_raw_excel": None
}
CACHE_DURATION = 5

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_SHEET_NAME = "Creative Change Audit"
PUSH_SUBSCRIPTIONS_KEY = "creative-report:push:subscriptions"
PUSH_SNAPSHOT_KEY = "creative-report:push:last-snapshot"
PUSH_CHECK_INTERVAL_SECONDS = 30
push_check_cache = {"checked_at": 0}


@app.route("/api/push/config", methods=["GET"])
def get_push_config():
    return jsonify(push_configuration())


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
        if cache["data"] is not None and (current_time - cache["timestamp"] < CACHE_DURATION):
            return jsonify({
                "success": True,
                "data": cache["data"],
                "audit_events": cache["audit_events"],
                "audit_tracking": cache["audit_tracking"]
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
        
        history = load_history()
        history_dirty = False
        results = []
        audit_events = []
        audit_tracking = False

        for sheet_name in wb.sheet_names:
            ws = wb.get_sheet_by_name(sheet_name)
            sheet_rows_data = ws.to_python()

            if sheet_name.strip().casefold() == AUDIT_SHEET_NAME.casefold():
                if not sheet_rows_data:
                    continue
                audit_headers = [
                    str(cell).strip().casefold() if cell is not None else ""
                    for cell in sheet_rows_data[0]
                ]
                audit_columns = {name: index for index, name in enumerate(audit_headers)}
                required_audit_headers = {
                    "event id", "timestamp", "editor", "sheet", "et / creative",
                    "range", "column", "old value", "new value"
                }
                if required_audit_headers.issubset(audit_columns):
                    audit_tracking = True
                    for audit_row in sheet_rows_data[1:]:
                        event_id_index = audit_columns["event id"]
                        if event_id_index >= len(audit_row) or not audit_row[event_id_index]:
                            continue

                        def audit_value(column_name):
                            column_index = audit_columns[column_name]
                            if column_index >= len(audit_row):
                                return ""
                            value = audit_row[column_index]
                            if value is None:
                                return ""
                            if isinstance(value, (date, datetime)):
                                return value.isoformat()
                            return str(value).strip()

                        audit_events.append({
                            "id": audit_value("event id"),
                            "timestamp": audit_value("timestamp"),
                            "editor": audit_value("editor"),
                            "sheet": audit_value("sheet"),
                            "et": audit_value("et / creative"),
                            "range": audit_value("range"),
                            "column": audit_value("column"),
                            "old_value": audit_value("old value"),
                            "new_value": audit_value("new value")
                        })
                continue

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
                    raw_date = datetime.now()
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
                status_upper = raw_status_str.upper()
                is_done = ("DONE" in status_upper or status_upper == "Y")
                
                results.append({
                    "tab": tab_name_str,
                    "row_number": row_number,
                    "date": str(display_date_str),
                    "filter_date": filter_date_str,
                    "et": et_name_str,
                    "status": raw_status_str,
                    "is_done": is_done,
                    "discovered_at": discovered_display
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
        cache["audit_events"] = audit_events[-1000:]
        cache["audit_tracking"] = audit_tracking
        cache["timestamp"] = current_time

        return jsonify({
            "success": True,
            "data": results,
            "audit_events": cache["audit_events"],
            "audit_tracking": audit_tracking
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
