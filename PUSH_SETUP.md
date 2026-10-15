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

## Device requirements and behavior

- **Android:** Use current Chrome (or another browser supporting Web Push). HTTPS is required; installing the app from the browser menu is recommended.
- **iPhone/iPad:** Use iOS/iPadOS 16.4 or newer, open the deployed app in Safari, choose **Share → Add to Home Screen**, open the installed app, then enable Alerts. iOS Web Push is for Home Screen web apps, not ordinary Safari tabs.
- The API avoids sending a duplicate push to the browser that made the sync request; that browser receives its regular in-page desktop notification instead.
- If every dashboard instance is closed, this version cannot notice a spreadsheet edit immediately. It sends push updates after an active dashboard makes a fresh sync request. Fully independent monitoring would need a more frequent scheduled watcher or a Google Sheets edit trigger.
- Notification permission, network access, device power/background policies, and the platform push service can affect delivery time.
