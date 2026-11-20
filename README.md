# Personal phone screen

Screens calls to two Telnyx numbers (one fed by the cell, one by Google Voice) before they ring a softphone.

- **Allowed** callers ring straight through. No answer goes to voicemail.
- **Blocked** callers are rejected.
- **Unknown** callers hear "press 1 to be connected". Pass rings the softphone; fail goes to voicemail.
- Every call is logged with which line it came in on. Voicemail audio is saved to a private S3 bucket.
- `/admin` is a small page for the allow/block lists, the call log and voicemail playback.

Texts land in the same panel, grouped by contact, with a reply box.

Threads can be archived (they move to an Archived tab and come back with Unarchive) or deleted outright. Deleting removes every call and text with that contact from the table; voicemail and call recordings stay in S3.

The pencil button composes a new message to a number with no history. That always goes out from the cell, because Google Voice's email relay is a reply address that only exists once someone has texted you.

Contact names come from Google Contacts, synced over the People API every 12 hours and on demand from the Status screen. This needs real OAuth, since an app password reaches mail and nothing else: set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, then press Connect on the Status screen and approve once.

Publish the OAuth consent screen to **Production**. While it sits in Testing, Google expires the refresh token after seven days and the sync dies quietly every week. You will click past an "unverified app" warning, which is expected — verification only matters for putting an app in front of other people.

A sync never overwrites an allow or block you have made; it only fills in names, and files contacts it has not seen before as `name` (labelled, still screened). The CSV import on the Numbers screen still works for anything outside Google Contacts.

Text size is adjustable: A− and A+ in the ⋮ menu, 13px to 26px, remembered per device in localStorage. Everything scales with it, since no font size is pinned in pixels — except the form fields, which stay at 16px or above so phone browsers do not zoom in when you tap them.

The panel follows the screen it is on. On a phone it is a conversation list you tap into, with a thread view and a reply box pinned to the bottom; on a wide screen every contact is expanded with calls on the left and texts on the right. Rotating the phone or resizing the window switches between the two live. It is installable: open `/admin` in Chrome on Android, then "Add to Home screen", and it runs full-screen with its own icon. The inbox re-reads itself every 30 seconds while in the foreground, and pauses that while you are typing a reply.

- **Cell texts** arrive from a gateway app on the Android phone, which posts each one to `/gateway`. Replies go back out through the same app, so they send from your real cell number. Two apps are supported and the inbound handler accepts either payload shape: SimpApp's *SMS Gateway API* (on the Play Store, set `SMS_API_KEY`) and capcom6's *SMS Gateway* (sideloaded, set `SMS_GATEWAY_USER`/`SMS_GATEWAY_PASS`). The Play Store one relays through the vendor's Firebase; the sideloaded one talks straight to this Lambda.
  Neither app authenticates its webhook calls, so the shared secret rides in the URL as `?k=...`.

`/telnyx` and `/gateway` both accept both senders. A request is read as a Telnyx webhook when it carries a valid Telnyx signature, and as a phone-app post when it carries the shared secret — the path itself decides nothing. Pointing the wrong URL at the wrong place is therefore harmless, and an unauthenticated request is still rejected on either.
- **Google Voice texts** arrive by Gmail: Google Voice forwards each text to the account's email, a once-a-minute IMAP poll picks it up, and a reply is sent over SMTP to Google's relay address, which Google turns back into a text from the Google Voice number.
- **Medical-sounding texts** are flagged, and a one-tap button fills the reply box with a message pointing the sender at the office. Nothing is sent automatically.

**Transcription.** Voicemail is transcribed automatically. Answered calls are recorded and transcribed only when call recording is on, which is a toggle on the Status screen rather than a deploy setting; `RECORD_CALLS` is only the starting value for a fresh stack. Transcripts appear under the call in the panel and are included in search. Transcription runs in a second, asynchronous Lambda invocation so a long recording never holds the Telnyx webhook open.

Before turning call recording on: New Jersey is a one-party-consent state, but several states a caller may be in — Pennsylvania among them — require everyone on the line to consent. The switch is off by default for that reason.

**Outbound calls** are logged too, but only once the SIP connection has a webhook URL set (SIP Trunking → the connection → Webhooks). Without it, calls placed from the handset leave through the connection and this app never hears about them.

**Click to call** rings your own handset first and bridges you to the other party when you answer. That is deliberate rather than a browser softphone: on Android, a web page cannot switch between earpiece and speaker (`setSinkId` is not supported there), so routing the call to the SIP app keeps mute, speaker and Bluetooth native. It needs `VOICE_APP_ID`, the Voice API application's id.

Not built yet: the AI screener, scheduled/queued texts.

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
| `AWS_DEPLOY_ROLE_ARN` | Same value as in the Language-tutor repo (OIDC deploy role) |
| `TELNYX_API_KEY` | Telnyx API key |
| `TELNYX_PUBLIC_KEY` | Telnyx public key (base64 string) |
| `ADMIN_TOKEN` | A long random string; this is the admin page password |
| `RING_TARGET` | `sip:USERNAME@sip.telnyx.com` |
| `CELL_DID` | Telnyx number the cell will forward to, as `+1XXXXXXXXXX` |
| `GV_DID` | Telnyx number Google Voice will forward to, as `+1XXXXXXXXXX` |

The deploy signs in to AWS the same way the Language-tutor repo does: GitHub OIDC assuming a role, no stored keys. That role's trust policy almost certainly names only the Language-tutor repo, so add this repo to it before the first deploy. In IAM, open the role, then Trust relationships, and add `repo:wkrasner/Communications:*` alongside the existing `token.actions.githubusercontent.com:sub` entry. Without that, the deploy fails at the credentials step with "Not authorized to perform sts:AssumeRoleWithWebIdentity".

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
