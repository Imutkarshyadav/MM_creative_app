from flask import Flask, jsonify, request, send_from_directory
import urllib.request
import json
import time
import io
import gzip
from datetime import date, datetime
import os
import random

from python_calamine import CalamineWorkbook

app = Flask(__name__)

@app.after_request
def compress_json_response(response):
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    if response.mimetype != "application/json" or response.status_code >= 300 or not accepts_gzip:
        return response

    response.set_data(gzip.compress(response.get_data()))
    response.headers["Content-Encoding"] = "gzip"
    response.vary.add("Accept-Encoding")
    return response

SHEET_ID = "128O1hciqdQ2eClfQ6O0m_klXecO6d8hsNvXSCMoXQMI"
URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
HISTORY_FILE = '/tmp/creative_history.json'

cache = {
    "timestamp": 0,
    "data": None,
    "last_raw_excel": None
}
CACHE_DURATION = 5

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
            return jsonify({"success": True, "data": cache["data"]})

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
        
        for sheet_name in wb.sheet_names:
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
                    "date": str(display_date_str),
                    "filter_date": filter_date_str,
                    "et": et_name_str,
                    "status": raw_status_str,
                    "is_done": is_done,
                    "discovered_at": discovered_display
                })
                
        if history_dirty:
            save_history(history)
            
        cache["data"] = results
        cache["timestamp"] = current_time

        return jsonify({"success": True, "data": results})
        
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
