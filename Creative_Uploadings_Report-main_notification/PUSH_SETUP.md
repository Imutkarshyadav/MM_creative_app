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

## Device requirements and behavior

- **Android:** Use current Chrome (or another browser supporting Web Push). HTTPS is required; installing the app from the browser menu is recommended.
- **iPhone/iPad:** Use iOS/iPadOS 16.4 or newer, open the deployed app in Safari, choose **Share → Add to Home Screen**, open the installed app, then enable Alerts. iOS Web Push is for Home Screen web apps, not ordinary Safari tabs.
- The API avoids sending a duplicate push to the browser that made the sync request; that browser receives its regular in-page desktop notification instead.
- If every dashboard instance is closed, this version cannot notice a spreadsheet edit immediately. It sends push updates after an active dashboard makes a fresh sync request. Fully independent monitoring would need a more frequent scheduled watcher or a Google Sheets edit trigger.
- Notification permission, network access, device power/background policies, and the platform push service can affect delivery time.
