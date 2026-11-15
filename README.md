# Personal phone screen

Screens calls to two Telnyx numbers (one fed by the cell, one by Google Voice) before they ring a softphone.

- **Allowed** callers ring straight through. No answer goes to voicemail.
- **Blocked** callers are rejected.
- **Unknown** callers hear "press 1 to be connected". Pass rings the softphone; fail goes to voicemail.
- Every call is logged with which line it came in on. Voicemail audio is saved to a private S3 bucket.
- `/admin` is a small page for the allow/block lists, the call log and voicemail playback.

Not built yet: text ingest (Google Voice via Gmail, cell via the SMS gateway app), the unified panel, voicemail transcripts, the AI screener, and the medical-text office referral.

## Setup

### 1. Telnyx
1. **SIP connection for the softphone.** Create a new credential-type SIP connection (separate from the MedTask one). Note its SIP username. The ring target is `sip:USERNAME@sip.telnyx.com`.
2. **Softphone app.** Install a push-capable SIP app on the phone and register it with that connection's username and password.
3. **Voice API application.** Create one for this project, webhook API version 2. Leave the webhook URL blank until step 3 below. Attach an outbound voice profile, since ringing the softphone is an outbound leg.
4. **Numbers.** Buy two numbers and assign both to the Voice API application.
5. Copy the account **API key** and **public key** (Account settings, Keys & Credentials).

### 2. GitHub
Create a private repo, upload these files keeping the folder structure, and add these Actions secrets:

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Same deploy credentials as the Language-tutor repo |
| `TELNYX_API_KEY` | Telnyx API key |
| `TELNYX_PUBLIC_KEY` | Telnyx public key (base64 string) |
| `ADMIN_TOKEN` | A long random string; this is the admin page password |
| `RING_TARGET` | `sip:USERNAME@sip.telnyx.com` |
| `CELL_DID` | Telnyx number the cell will forward to, as `+1XXXXXXXXXX` |
| `GV_DID` | Telnyx number Google Voice will forward to, as `+1XXXXXXXXXX` |

If the Language-tutor workflow signs in to AWS a different way (a role instead of keys), copy that step into `.github/workflows/deploy.yml`.

### 3. Deploy and connect
1. Push to `main`. The workflow runs the tests, deploys, and prints `WebhookUrl` and `AdminUrl` in the "Show URLs" step.
2. Paste `WebhookUrl` into the Telnyx Voice API application.
3. Open `AdminUrl`, sign in with the admin token, and paste your contacts into Numbers (one per line: `number, name`).

### 4. Test before forwarding anything
Call each Telnyx number directly:
- from a number on the allow list: softphone rings, shows `Cell: Name` or `GV: Name`
- from a number not on any list: press-1 prompt, then the softphone rings
- same, but don't press anything: voicemail, shows in the call log
- let an allowed call ring out: voicemail

### 5. Turn on forwarding
1. **Google Voice first.** Add the GV Telnyx number as a linked number and forward calls to it. Google may refuse to link a VoIP number; if so, leave Google Voice forwarding to the cell and those calls will arrive tagged `Cell`.
2. **Cell last**, after a few clean days. Turn on unconditional call forwarding to the cell Telnyx number with your carrier's code or app.

Never point the ring target at 609-254-2233 itself: once the cell forwards to Telnyx, that loops.

## Notes
- Unknown callers who pass the challenge are not added to the allow list automatically. Use Allow or Block on the call log row.
- Automated callers (pharmacy, reminders, bank verification calls) can't press 1 and land in voicemail.
- The Telnyx request and webhook shapes follow the current API reference, but nothing here has run against a live Telnyx account yet. Step 4 is the real test.
