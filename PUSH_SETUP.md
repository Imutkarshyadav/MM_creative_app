# Android and iOS push notifications

The app supports Web Push through the service worker. Push is sent when a fresh `/api/sync` request detects spreadsheet changes (push checks are throttled to once per 30 seconds per warm server instance). Keep the dashboard open in at least one browser/device to poll the sheet; Vercel Hobby cron jobs run only once per day, so this setup does not independently watch the sheet continuously when every app window is closed.

## Vercel and Upstash setup

1. Deploy this repository to Vercel over HTTPS. A Vercel Hobby account is sufficient for hosting and push API requests.
2. Create a free Upstash Redis database and copy its **REST URL** and **REST token**. The app stores device subscriptions and its last seen spreadsheet snapshot there.
3. Generate one VAPID key pair locally. Keep the private key secret and use the same pair for every deployment:

   ```powershell
   pip install py-vapid
   vapid --gen
   vapid --applicationServerKey
   ```

   `vapid --gen` creates `private_key.pem` and `public_key.pem` in the current directory. Copy the value printed by `vapid --applicationServerKey` for `VAPID_PUBLIC_KEY`. To create the DER/base64 private-key value expected by the sender, run this from that directory:

   ```powershell
   python -c "import base64; from cryptography.hazmat.primitives import serialization; key=serialization.load_pem_private_key(open('private_key.pem','rb').read(),None); print(base64.urlsafe_b64encode(key.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())).decode())"
   ```

   Do not commit or share `private_key.pem` or the generated private-key value.
4. Add these environment variables in **Vercel → Project → Settings → Environment Variables**:

   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `VAPID_PUBLIC_KEY`
   - `VAPID_PRIVATE_KEY`
   - `VAPID_SUBJECT` (for example, `mailto:you@example.com`)

5. Redeploy after saving the variables. In the deployed app, enable **Alerts** on each device and approve browser notification permission. Subscriptions are limited to 100 devices per deployment.

## Admin Access

The header's **Admin Access** button protects the app-wide Color Settings visibility toggle.

- Set `ADMIN_PASSWORD` as an environment variable; use a strong password and do not commit it or send it in chat. For local development, set it in the PowerShell process before starting the server:

  ```powershell
  $env:ADMIN_PASSWORD = "use-a-strong-secret-here"
  python app_server.py
  ```

- On Vercel, add `ADMIN_PASSWORD` under **Project → Settings → Environment Variables**, then redeploy.
- Vercel admin settings must persist across function instances, so also configure `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` as described above. Without both, the app reports an error instead of pretending a setting change was saved.
- Local admin settings are stored in `admin_settings.json`, which is excluded from Git. The admin session is held in an HttpOnly cookie and expires after four hours.
- Once signed in, use **Change admin password** in the Admin Access panel. Passwords must be at least 12 characters and are stored as salted hashes in the same persistent settings store; changing the password invalidates existing admin sessions except the session that made the change. After a password is changed, the saved hash takes precedence over `ADMIN_PASSWORD`. To reset a forgotten changed password, remove the saved `admin_password_hash` and `admin_password_salt` fields from the local `admin_settings.json` or stored Upstash settings, then restart or redeploy; the configured `ADMIN_PASSWORD` will be used again.
- Color Settings visibility is refreshed by open app pages every 30 seconds. The setting affects which users see the control; their saved color preferences are unchanged.

## Private Daily Report audit

The admin-only Daily Report reads its edit history from a separate, private spreadsheet. The Apps Script edit trigger stays attached to the **CREATIVE UPLOADINGS** source spreadsheet and writes audit rows to the destination spreadsheet ID `1iDQoBTDHsrfavBA4iWrcgrUKIt7NfWty28s2PkMvF18`. The destination must remain private; grant Editor access only to the Google account that owns/runs the Apps Script.

1. Open the **CREATIVE UPLOADINGS** spreadsheet, then **Extensions → Apps Script**. Replace the editor contents with the repository's `Code.gs` and save.
2. Run `setupCreativeChangeAudit()` from the Apps Script editor and approve the requested spreadsheet permissions. This creates the destination's **Daily Report** tab, remembers the source spreadsheet for read-only current-status reporting, and installs an edit trigger on the source spreadsheet. The app accesses the source only through this script running as its owner; do not give the app a Google account or edit access to the source sheet.
3. Run `generateAuditApiToken()` once. Copy the token from the Apps Script execution log directly into your server configuration. Do not send it in chat or commit it.
4. In Apps Script, choose **Deploy → New deployment → Web app**. Set **Execute as** to yourself and **Who has access** to **Anyone**, then deploy and copy the web app URL. The endpoint is callable publicly, but it returns audit rows only when the secret token is supplied; keep that token private and rotate it by running `generateAuditApiToken()` again if exposed.
5. Configure these server-side environment variables; never put either value in `index.html` or another client-side file:

   - `AUDIT_WEB_APP_URL` — the deployed Apps Script web app URL
   - `AUDIT_API_TOKEN` — the token from step 3

   On Vercel, add both under **Project → Settings → Environment Variables** for the **Production** environment (and Preview too if testing previews), then redeploy the project after saving them. The dashboard code and Python API changes also require a new Vercel deployment; local changes alone do not update the hosted app. For local development, set them in the same PowerShell process used to start the app:

   ```powershell
   $env:AUDIT_WEB_APP_URL = "your-deployed-web-app-url"
   $env:AUDIT_API_TOKEN = "your-generated-token"
   python app_server.py
   ```

After updating `Code.gs`, run `setupCreativeChangeAudit()` again so the source spreadsheet ID is saved, then deploy a **new version** of the web app. The admin-session-protected Daily Report focuses on the selected date and shows each Gmail account that marked creatives Done, its Done count, and the creative names. A creative is counted only if its latest recorded status is still Done; clearing Done removes it from the daily count, and marking it Done again credits the new editor. Later edits that leave the status Done do not transfer credit. Google may not provide an editor's email, in which case the report shows that the identity is unavailable. Existing Done rows cannot be attributed retroactively unless their status edit was recorded. If audit logging is not initialized, the report explains that setup is required. The open report refreshes every 30 seconds; use Refresh to fetch immediately. Regular `/api/sync` responses do not contain audit data.

Each source tab must have a header row containing columns whose names include **Creative** and **Status** (the script checks the first 25 rows). The live snapshot reports zero matching tabs if this header pair is not found; move/fix the headers or adapt the detector to the sheet's actual column names.

For Vercel, confirm the latest code is pushed to the connected Git branch, both audit variables are set for the Production environment, and a new deployment completed after the variables were saved. If the report says the Apps Script is outdated or omits `current_done`, update the Apps Script web app to a new version and verify `AUDIT_WEB_APP_URL` points to that deployment's `/exec` URL. Check the Vercel Function logs for `/api/admin/daily-report` when diagnosing server-side failures; never put the token in browser code or send it in logs or chat.

The edit trigger records edited cells, including status changes, in the audit sheet; logs are no longer automatically pruned and remain subject to Google Sheets storage limits. For multi-cell edits, each cell is recorded but the previous value is unavailable. Apps Script edit triggers do not run for changes made by another script, an API integration, or formula recalculation, though the current Done snapshot still reflects the source sheet's displayed values when the report loads. Logging begins only after the trigger is installed, so earlier editor identities and dates cannot be recovered retroactively. Google may not provide an editor email for every edit. Google Apps Script deployments configured as **Anyone** are bearer-token protected rather than Google-login protected, so treat the token like a password and rotate it immediately if it is disclosed. If your Google Workspace policy does not allow **Anyone** deployments, the server cannot call this endpoint without an additional Google-authenticated integration.

## Device requirements and behavior

- **Android:** Use current Chrome (or another browser supporting Web Push). HTTPS is required; installing the app from the browser menu is recommended.
- **iPhone/iPad:** Use iOS/iPadOS 16.4 or newer, open the deployed app in Safari, choose **Share → Add to Home Screen**, open the installed app, then enable Alerts. iOS Web Push is for Home Screen web apps, not ordinary Safari tabs.
- The API avoids sending a duplicate push to the browser that made the sync request; that browser receives its regular in-page desktop notification instead.
- If every dashboard instance is closed, this version cannot notice a spreadsheet edit immediately. It sends push updates after an active dashboard makes a fresh sync request. Fully independent monitoring would need a more frequent scheduled watcher or a Google Sheets edit trigger.
- Notification permission, network access, device power/background policies, and the platform push service can affect delivery time.
