import os
import json
import time
import uuid
from typing import Dict, List, Set, Tuple
from email.utils import parseaddr

from flask import Flask, render_template, redirect, url_for, request, session, flash
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# ---------------- Config ----------------
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]
TOKEN_DIR = ".tokens"
WEB_TOKEN_PATH = os.path.join(TOKEN_DIR, "web_token.json")
BLOCKLIST_PATH = "senders_to_delete.txt"
OAUTH_REDIRECT_URI = "http://localhost:5000/oauth2callback"

# Server-side cache (to avoid huge cookie sessions)
CACHE_DIR = ".cache"

app = Flask(__name__)
# Use env var; falls back to a dev default if not set
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

os.makedirs(TOKEN_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ------------- Helpers --------------

def log(msg: str):
    print(f"[web] {msg}", flush=True)

def load_blocklist(path: str = BLOCKLIST_PATH) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    return set(s.lower() for s in lines)

def save_blocklist(senders: Set[str], path: str = BLOCKLIST_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Senders marked for deletion (one email per line)\n")
        for s in sorted(senders):
            f.write(s + "\n")
    log(f"Blocklist updated: {path}")

def save_credentials(creds: Credentials):
    with open(WEB_TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    log(f"Saved token to {WEB_TOKEN_PATH}")

def load_credentials() -> Credentials | None:
    if os.path.exists(WEB_TOKEN_PATH):
        try:
            with open(WEB_TOKEN_PATH, "r", encoding="utf-8") as f:
                info = json.load(f)
            return Credentials.from_authorized_user_info(info, SCOPES)
        except Exception as e:
            log(f"Failed to load creds: {e}")
            return None
    return None

def ensure_modify_scope(creds: Credentials) -> Credentials | None:
    granted = set(creds.scopes or [])
    need = "https://www.googleapis.com/auth/gmail.modify"
    if need not in granted:
        log("Token missing gmail.modify. Clearing and restarting OAuth.")
        if os.path.exists(WEB_TOKEN_PATH):
            os.remove(WEB_TOKEN_PATH)
        return None
    return creds

def get_flow() -> Flow:
    if not os.path.exists("credentials.json"):
        raise RuntimeError("Missing credentials.json in project folder.")
    flow = Flow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    return flow

def build_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)

def list_unread_inbox_message_ids(service, page_size=500) -> List[str]:
    q = "is:unread in:inbox"
    msg_ids = []
    page_token = None
    while True:
        try:
            resp = service.users().messages().list(
                userId="me",
                q=q,
                labelIds=["INBOX"],
                maxResults=page_size,
                pageToken=page_token
            ).execute()
        except HttpError as e:
            if e.resp.status in (429, 500, 503):
                time.sleep(1.5)
                continue
            raise
        msgs = resp.get("messages", [])
        msg_ids.extend(m["id"] for m in msgs)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return msg_ids

def get_message_metadata(service, msg_id):
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"]
        ).execute()
    except HttpError as e:
        log(f"Fetch fail {msg_id}: {e}")
        return None
    headers = msg.get("payload", {}).get("headers", [])
    hmap = {h["name"].lower(): h["value"] for h in headers}
    from_raw = hmap.get("from", "(unknown)")
    subject = hmap.get("subject", "(no subject)")
    date = hmap.get("date", "(no date)")
    display, email = parseaddr(from_raw)
    sender_key = (email or from_raw).lower()
    sender_label = f"{display} <{email}>" if email else from_raw
    return {
        "id": msg_id,
        "from_key": sender_key,
        "from_label": sender_label,
        "subject": subject.strip(),
        "date": date,
    }

def group_by_sender(service, msg_ids) -> Dict[str, Dict]:
    by_sender: Dict[str, Dict] = {}
    total = len(msg_ids)
    for idx, mid in enumerate(msg_ids, start=1):
        if idx == 1 or idx % 50 == 0:
            log(f"Metadata {idx}/{total}")
        meta = get_message_metadata(service, mid)
        if not meta:
            continue
        key = meta["from_key"]
        if key not in by_sender:
            by_sender[key] = {"sender": meta["from_label"], "count": 0, "messages": []}
        by_sender[key]["count"] += 1
        by_sender[key]["messages"].append(meta)
    return by_sender

def filter_min_count(by_sender: Dict[str, Dict], min_count: int = 2) -> Dict[str, Dict]:
    return {k: v for k, v in by_sender.items() if v["count"] >= min_count}

def collect_msg_ids_for_senders(by_sender: Dict[str, Dict], selected_keys: Set[str]) -> List[str]:
    out = []
    for k, data in by_sender.items():
        if k in selected_keys:
            out.extend([m["id"] for m in data["messages"]])
    return out

def trash_messages(service, msg_ids: List[str]) -> Tuple[int, List[str]]:
    """Return count_trashed, failed_ids."""
    total = len(msg_ids)
    trashed = 0
    failed = []
    for idx, mid in enumerate(msg_ids, start=1):
        if idx == 1 or idx % 50 == 0:
            log(f"Trashing {idx}/{total}")
        try:
            service.users().messages().trash(userId="me", id=mid).execute()
            trashed += 1
        except HttpError as e:
            failed.append(mid)
    return trashed, failed

# --------- Tiny server-side cache (to avoid re-fetching) ----------

def _cache_path(cache_id: str) -> str:
    return os.path.join(CACHE_DIR, f"by_sender_{cache_id}.json")

def save_cache(data: dict) -> str:
    cache_id = uuid.uuid4().hex[:16]
    with open(_cache_path(cache_id), "w", encoding="utf-8") as f:
        json.dump({"ts": int(time.time()), "data": data}, f)
    return cache_id

def load_cache(cache_id: str) -> dict | None:
    if not cache_id:
        return None
    try:
        with open(_cache_path(cache_id), "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("data")
    except Exception:
        return None

def cleanup_cache(max_age_seconds: int = 2 * 60 * 60):
    now = int(time.time())
    try:
        for name in os.listdir(CACHE_DIR):
            if not name.startswith("by_sender_") or not name.endswith(".json"):
                continue
            p = os.path.join(CACHE_DIR, name)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    blob = json.load(f)
                ts = int(blob.get("ts", 0))
                if now - ts > max_age_seconds:
                    os.remove(p)
            except Exception:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except FileNotFoundError:
        pass

# ------------- Routes ----------------

@app.route("/")
def index():
    # Check for stored credentials
    creds = load_credentials()
    if creds:
        creds = ensure_modify_scope(creds)
        if creds:
            return redirect(url_for("unread"))
    return render_template("index.html")

@app.route("/login")
def login():
    flow = get_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["state"] = state
    return redirect(auth_url)

@app.route("/oauth2callback")
def oauth2callback():
    # state = session.get("state")  # not strictly needed but kept from original shape
    flow = get_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    # Save and continue
    save_credentials(creds)
    flash("Authentication complete.", "success")
    return redirect(url_for("unread"))

@app.route("/logout")
def logout():
    if os.path.exists(WEB_TOKEN_PATH):
        os.remove(WEB_TOKEN_PATH)
    session.clear()
    flash("Signed out and local token removed.", "info")
    return redirect(url_for("index"))

@app.route("/unread")
def unread():
    creds = load_credentials()
    if not creds:
        return redirect(url_for("index"))
    creds = ensure_modify_scope(creds)
    if not creds:
        return redirect(url_for("login"))

    service = build_gmail_service(creds)
    msg_ids = list_unread_inbox_message_ids(service)
    by_sender_all = group_by_sender(service, msg_ids)
    by_sender = filter_min_count(by_sender_all, min_count=2)

    # Cache in server-side file and remember key in session for reuse
    cleanup_cache()
    cache_id = save_cache(by_sender)
    session["cache_id"] = cache_id

    # Preselect from blocklist
    blocklist = load_blocklist()
    rows = sorted(by_sender.items(), key=lambda kv: (-kv[1]["count"], kv[1]["sender"].lower()))
    return render_template("unread.html", rows=rows, blocklist=blocklist)

@app.route("/update_selection", methods=["POST"])
def update_selection():
    # Selected checkboxes named 'sender'
    selected = request.form.getlist("sender")
    selected = set(s.lower() for s in selected)

    # Persist blocklist immediately, replacing with the chosen set
    save_blocklist(selected)

    # Use cached data from unread step instead of refetching
    cache_id = session.get("cache_id")
    by_sender = load_cache(cache_id)
    if not by_sender:
        flash("Cached data expired. Please refresh the Unread page.", "warning")
        return redirect(url_for("unread"))

    # Prepare selected detail list
    selected_detail = []
    for key, data in by_sender.items():
        if key in selected:
            selected_detail.append({"key": key, "label": data["sender"], "count": data["count"]})
    selected_detail.sort(key=lambda d: (-d["count"], d["label"].lower()))

    return render_template("confirm.html", selected_detail=selected_detail)

@app.route("/delete", methods=["POST"])
def delete():
    # Final delete based on current blocklist, using cached IDs (no refetch)
    blocklist = load_blocklist()

    creds = load_credentials()
    if not creds:
        return redirect(url_for("index"))
    service = build_gmail_service(creds)

    cache_id = session.get("cache_id")
    by_sender = load_cache(cache_id)
    if not by_sender:
        flash("Cached data expired. Please refresh the Unread page.", "warning")
        return redirect(url_for("unread"))

    target_ids = collect_msg_ids_for_senders(by_sender, blocklist)
    trashed, failed = trash_messages(service, target_ids)

    # Optionally clear this cache entry
    try:
        os.remove(_cache_path(cache_id))
    except Exception:
        pass

    return render_template("result.html", trashed=trashed, failed=len(failed), failed_ids=failed)

if __name__ == "__main__":
    app.run(debug=True)
