import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import time
import pandas as pd
import io
from datetime import datetime
import os
import warnings

# Prevent openpyxl warnings
warnings.filterwarnings('ignore', category=UserWarning, module='openpyxl')

PORT = int(os.environ.get("PORT", 8024))
SHEET_ID = "128O1hciqdQ2eClfQ6O0m_klXecO6d8hsNvXSCMoXQMI"
URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=xlsx"
HISTORY_FILE = 'creative_history.json'

# Basic memory cache
cache = {
    "timestamp": 0,
    "data": None,
    "last_raw_excel": None
}
CACHE_DURATION = 15 # Increased to 15s to prevent Google from blocking the cloud IP

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

class SyncHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/sync'):
            try:
                current_time = time.time()
                if cache["data"] is not None and (current_time - cache["timestamp"] < CACHE_DURATION):
                    results = cache["data"]
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
                    
                    sheets = pd.read_excel(io.BytesIO(excel_data), sheet_name=None, engine='calamine')
                    
                    history = load_history()
                    history_dirty = False
                    results = []
                    
                    for tab_name, df in sheets.items():
                        cols = df.columns
                        date_col = cols[0] if len(cols) > 0 else None
                        creative_col = next((c for c in cols if 'CREATIVE' in str(c).upper()), None)
                        status_col = next((c for c in cols if 'STATUS' in str(c).upper()), None)
                        
                        if creative_col and status_col and date_col:
                            df[date_col] = df[date_col].ffill()
                            
                            for _, row in df.iterrows():
                                et_name = row[creative_col]
                                if pd.isna(et_name) or str(et_name).strip() == "":
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
                                raw_date = row[date_col]
                                
                                if pd.isna(raw_date):
                                    continue
                                    
                                parsed_dt = pd.to_datetime(raw_date, errors='coerce')
                                if pd.notna(parsed_dt):
                                    display_date_str = parsed_dt.strftime('%Y-%m-%d')
                                    filter_date_str = display_date_str
                                else:
                                    display_date_str = str(raw_date).strip().split(' ')[0]
                                    try:
                                        filter_date_str = datetime.strptime(display_date_str, '%d/%m/%Y').strftime('%Y-%m-%d')
                                    except:
                                        filter_date_str = "1970-01-01"
                                
                                raw_status = str(row[status_col]).strip() if pd.notna(row[status_col]) else "PENDING/BLANK"
                                status_upper = raw_status.upper()
                                
                                is_done = ("DONE" in status_upper or status_upper == "Y")
                                
                                results.append({
                                    "tab": tab_name_str,
                                    "date": str(display_date_str),
                                    "filter_date": filter_date_str,
                                    "et": et_name_str,
                                    "status": raw_status,
                                    "is_done": is_done,
                                    "discovered_at": discovered_display
                                })
                    
                    if history_dirty:
                        save_history(history)
                        
                    cache["data"] = results
                    cache["timestamp"] = current_time

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True, 
                    "data": results
                }).encode('utf-8'))
                
            except Exception as e:
                print("Error during sync:", e)
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))
        else:
            super().do_GET()

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), SyncHandler) as httpd:
        print(f"Server is running on port {PORT}!")
        httpd.serve_forever()
