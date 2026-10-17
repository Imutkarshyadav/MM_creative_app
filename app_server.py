import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import gzip
import time
import io
from http.cookies import CookieError, SimpleCookie
from datetime import date, datetime
import os
from api.index import (
    ADMIN_COOKIE_NAME,
    ADMIN_SESSION_SECONDS,
    CalamineWorkbook,
    admin_api_request,
    extract_sheet_cell_colors,
    extract_sheet_tab_colors,
    get_admin_settings,
    get_daily_report_events,
    get_missing_stock_campaign_codes,
    is_completed_status,
    load_missing_stock,
    MISSING_STOCK_SHEET_URL,
    is_valid_admin_session,
    spreadsheet_column_name
)

PORT = int(os.environ.get("PORT", 8024))
SHEET_ID = "128O1hciqdQ2eClfQ6O0m_klXecO6d8hsNvXSCMoXQMI"
URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
HISTORY_FILE = 'creative_history.json'

# Basic memory cache
cache = {
    "timestamp": 0,
    "data": None,
    "response_body": None,
    "gzip_body": None,
    "last_raw_excel": None,
    "local_date": None
}
CACHE_DURATION = 15

def get_client_local_date(headers):
    value = headers.get("X-Local-Date", "").strip()
    try:
        parsed_date = datetime.strptime(value, '%Y-%m-%d').date()
        if parsed_date.isoformat() == value:
            return parsed_date
    except ValueError:
        pass
    return datetime.now().date()


def parse_sheet_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None

    text = str(value).strip()
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

class SyncHandler(http.server.SimpleHTTPRequestHandler):
    def send_json_response(self, status, payload):
        response = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(response)

    def handle_daily_report(self, query):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            session_cookie = cookies.get(ADMIN_COOKIE_NAME)
            settings = get_admin_settings()
            if not is_valid_admin_session(
                session_cookie.value if session_cookie else "",
                settings
            ):
                self.send_json_response(401, {
                    "success": False,
                    "error": "Admin sign-in is required."
                })
                return

            report_date = urllib.parse.parse_qs(query).get("date", [""])[0]
            try:
                parsed_date = datetime.strptime(report_date, "%Y-%m-%d").date()
            except ValueError:
                self.send_json_response(400, {
                    "success": False,
                    "error": "A valid report date is required."
                })
                return
            if parsed_date.isoformat() != report_date:
                self.send_json_response(400, {
                    "success": False,
                    "error": "A valid report date is required."
                })
                return

            report = get_daily_report_events(report_date)
            self.send_json_response(200, {"success": True, **report})
        except CookieError:
            self.send_json_response(400, {
                "success": False,
                "error": "Invalid session cookie."
            })
        except RuntimeError as error:
            print(f"Daily Report request failed: {error}")
            self.send_json_response(503, {"success": False, "error": str(error)})

    def handle_admin_api(self, method):
        payload = {}
        if method != "GET":
            if self.headers.get_content_type() != "application/json":
                self.send_error(400, "Expected a JSON request.")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 0 or content_length > 4096:
                    raise ValueError("Request body is too large.")
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                response = json.dumps({"success": False, "error": str(error)}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return
            if not isinstance(payload, dict):
                response = json.dumps({"success": False, "error": "Expected a JSON object."}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return

        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            response = json.dumps({"success": False, "error": "Invalid session cookie."}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)
            return
        session_cookie = cookies.get(ADMIN_COOKIE_NAME)
        session_token = session_cookie.value if session_cookie else ""
        try:
            status, result, token = admin_api_request(
                method,
                payload,
                session_token,
                self.client_address[0]
            )
        except RuntimeError as error:
            print(f"Admin settings request failed: {error}")
            status, result, token = 503, {"success": False, "error": str(error)}, None

        response = json.dumps(result).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Cache-Control", "no-store")
        if token == "clear":
            self.send_header(
                "Set-Cookie",
                f"{ADMIN_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            )
        elif token:
            cookie = (
                f"{ADMIN_COOKIE_NAME}={token}; Path=/; Max-Age={ADMIN_SESSION_SECONDS}; "
                "HttpOnly; SameSite=Lax"
            )
            forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
            if forwarded_proto == "https":
                cookie += "; Secure"
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        parsed_path = urllib.parse.urlsplit(self.path)
        if parsed_path.path == "/api/admin/config":
            self.handle_admin_api("GET")
            return
        if parsed_path.path == "/api/admin/daily-report":
            self.handle_daily_report(parsed_path.query)
            return

        if parsed_path.path == '/api/missing-stock':
            try:
                stock = load_missing_stock()
                tabs_by_name = {tab['name']: tab for tab in stock['tabs']}
                tab_name = urllib.parse.parse_qs(parsed_path.query).get('tab', [''])[0].strip()
                query_params = urllib.parse.parse_qs(parsed_path.query)
                if query_params.get('all_tabs', [''])[0] == 'true':
                    entries = [
                        {
                            **entry,
                            "stock_tab": name,
                            "stock_tab_color": tabs_by_name[name]['tab_color']
                        }
                        for name, tab_entries in stock['entries'].items()
                        for entry in tab_entries
                    ]
                    status_code = 200
                    payload = {
                        "success": True,
                        "tab": "All stock tabs",
                        "sheet_url": MISSING_STOCK_SHEET_URL,
                        "entries": entries
                    }
                elif tab_name and tab_name not in stock['entries']:
                    status_code = 404
                    payload = {"success": False, "error": "The selected stock tab was not found."}
                elif tab_name:
                    status_code = 200
                    payload = {
                        "success": True,
                        "tab": tab_name,
                        "sheet_url": MISSING_STOCK_SHEET_URL,
                        "entries": [
                            {
                                **entry,
                                "stock_tab": tab_name,
                                "stock_tab_color": tabs_by_name[tab_name]['tab_color']
                            }
                            for entry in stock['entries'][tab_name]
                        ]
                    }
                else:
                    status_code = 200
                    payload = {
                        "success": True,
                        "sheet_url": MISSING_STOCK_SHEET_URL,
                        "tabs": stock['tabs'],
                        "campaign_codes": get_missing_stock_campaign_codes(stock)
                    }
            except Exception as error:
                print(f"Could not load missing-creative stock sheet: {error}")
                status_code = 503
                payload = {
                    "success": False,
                    "error": "Could not load the linked stock spreadsheet. Check that it is shared for viewing and try again."
                }
            response = json.dumps(payload).encode('utf-8')
            self.send_response(status_code)
            self.send_header('Content-type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if self.path.startswith('/api/sync'):
            try:
                current_time = time.time()
                client_today = get_client_local_date(self.headers)
                if (
                    cache["data"] is not None
                    and cache["local_date"] == client_today.isoformat()
                    and current_time - cache["timestamp"] < CACHE_DURATION
                ):
                    results = cache["data"]
                    response_body = cache["response_body"]
                    gzip_body = cache["gzip_body"]
                else:
                    try:
                        # Use a realistic browser User-Agent to avoid Google blocking data center IPs
                        headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                            'Accept-Language': 'en-US,en;q=0.5'
                        }
                        req = urllib.request.Request(URL, headers=headers)
                        with urllib.request.urlopen(req) as response:
                            excel_data = response.read()
                            
                            # Check if Google returned an HTML page (like a login or rate limit warning) instead of the Excel file
                            if excel_data.startswith(b'<html') or excel_data.startswith(b'<!DOCTYPE html'):
                                raise Exception("Google returned a webpage instead of the spreadsheet. The cloud IP might be temporarily rate-limited.")
                                
                            cache["last_raw_excel"] = excel_data
                    except Exception as fetch_err:
                        print("Google fetch failed:", fetch_err)
                        if cache["last_raw_excel"] is not None:
                            excel_data = cache["last_raw_excel"]
                        else:
                            raise Exception(f"Failed to download from Google Sheets: {str(fetch_err)}")
                    
                    workbook = CalamineWorkbook.from_filelike(io.BytesIO(excel_data))
                    tab_colors = extract_sheet_tab_colors(excel_data)
                    cell_colors = extract_sheet_cell_colors(excel_data)
                    
                    history = load_history()
                    history_dirty = False
                    results = []
                    
                    for tab_name in workbook.sheet_names:
                        rows = workbook.get_sheet_by_name(tab_name).to_python()
                        if not rows:
                            continue
                        if tab_name.strip().casefold() == "daily report":
                            continue

                        headers = rows[0]
                        date_col = 0
                        creative_col = next(
                            (index for index, value in enumerate(headers) if 'CREATIVE' in str(value or '').upper()),
                            None
                        )
                        status_col = next(
                            (index for index, value in enumerate(headers) if 'STATUS' in str(value or '').upper()),
                            None
                        )
                        if creative_col is None or status_col is None:
                            continue

                        explicit_date_rows = [
                            row_number
                            for row_number, row in enumerate(rows[1:], start=2)
                            if (
                                date_col < len(row)
                                and row[date_col] is not None
                                and str(row[date_col]).strip()
                                and creative_col < len(row)
                                and row[creative_col] is not None
                                and str(row[creative_col]).strip()
                            )
                        ]
                        last_explicit_date_row = max(explicit_date_rows, default=None)
                        last_date = None

                        for row_number, row in enumerate(rows[1:], start=2):
                            raw_date = row[date_col] if date_col < len(row) else None
                            if raw_date is not None and str(raw_date).strip():
                                last_date = raw_date
                            else:
                                raw_date = last_date

                            et_name = row[creative_col] if creative_col < len(row) else None
                            if et_name is None or not str(et_name).strip():
                                continue

                            tab_name_str = str(tab_name).strip()
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
                            if last_explicit_date_row is None or row_number > last_explicit_date_row:
                                raw_date = client_today
                            if raw_date is None:
                                continue

                            parsed_date = parse_sheet_date(raw_date)
                            display_date_str = parsed_date.isoformat() if parsed_date else str(raw_date).strip().split(' ')[0]
                            filter_date_str = parsed_date.isoformat() if parsed_date else "1970-01-01"

                            raw_status = row[status_col] if status_col < len(row) else None
                            raw_status = str(raw_status).strip() if raw_status is not None else "PENDING/BLANK"
                            is_done = is_completed_status(raw_status)

                            results.append({
                                "tab": tab_name_str,
                                "row_number": row_number,
                                "date": display_date_str,
                                "filter_date": filter_date_str,
                                "et": et_name_str,
                                "status": raw_status,
                                "is_done": is_done,
                                "discovered_at": discovered_display,
                                "tab_color": tab_colors.get(tab_name_str),
                                "creative_colors": cell_colors.get(tab_name_str, {}).get(
                                    f"{spreadsheet_column_name(creative_col)}{row_number}", []
                                )
                            })
                    
                    if history_dirty:
                        save_history(history)
                        
                    cache["data"] = results
                    cache["local_date"] = client_today.isoformat()
                    cache["response_body"] = json.dumps(
                        {
                            "success": True,
                            "data": results
                        },
                        separators=(",", ":")
                    ).encode("utf-8")
                    cache["gzip_body"] = gzip.compress(
                        cache["response_body"],
                        compresslevel=5,
                        mtime=0
                    )
                    cache["timestamp"] = time.time()
                    response_body = cache["response_body"]
                    gzip_body = cache["gzip_body"]

                self.send_response(200)
                accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
                response = gzip_body if accepts_gzip else response_body
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response)))
                if accepts_gzip:
                    self.send_header('Content-Encoding', 'gzip')
                    self.send_header('Vary', 'Accept-Encoding')
                self.end_headers()
                self.wfile.write(response)
                
            except Exception as e:
                print("Error during sync:", e)
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))
        else:
            super().do_GET()

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path == "/api/admin/config":
            self.handle_admin_api("POST")
            return
        self.send_error(404, "Not found.")

    def do_PUT(self):
        if urllib.parse.urlsplit(self.path).path == "/api/admin/config":
            self.handle_admin_api("PUT")
            return
        self.send_error(404, "Not found.")

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), SyncHandler) as httpd:
        print(f"Server is running on port {PORT}!")
        httpd.serve_forever()
