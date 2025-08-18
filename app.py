import os
import uuid
import time
import json
import urllib.parse
from datetime import datetime
from typing import Dict, Any, List

import requests
import streamlit as st

###############################################################################
# Streamlit + Slack (Incoming Webhook) Poll App
#
# What this app does:
# - Lets you create a poll with 4 options
# - Posts the poll to Slack using an Incoming Webhook
# - Each option in Slack is a link back to this app (?poll=<id>&vote=1..4)
# - Votes are collected & summarized ONLY inside Streamlit
# - You can “End poll” to stop taking votes and show a final summary
#
# IMPORTANT NOTES
# - Incoming Webhooks can only POST to Slack; they cannot read reactions or user info.
# - Votes are tracked by clicks on links (not emoji reactions) so we can collect
#   results in Streamlit without extra Slack API scopes.
# - If you strictly need emoji-based voting, you’ll need a real Slack bot + Events API.
###############################################################################

# ----------------------------- storage helpers ------------------------------ #

DATA_DIR = os.environ.get("DATA_DIR", ".") # Use current dir for simplicity
os.makedirs(DATA_DIR, exist_ok=True)
POLLS_PATH = os.path.join(DATA_DIR, "polls.json")


def _default_store() -> Dict[str, Any]:
    return {
        "polls": {}  # poll_id -> {question, options[4], created_at, closed, totals[4], votes_log}
    }


def load_store() -> Dict[str, Any]:
    if not os.path.exists(POLLS_PATH):
        return _default_store()
    try:
        with open(POLLS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_store()


def save_store(store: Dict[str, Any]) -> None:
    tmp = POLLS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, POLLS_PATH)


def create_poll(question: str, options: List[str]) -> str:
    store = load_store()
    pid = uuid.uuid4().hex[:10]
    store["polls"][pid] = {
        "question": question,
        "options": options,
        "created_at": int(time.time()),
        "closed": False,
        "totals": [0, 0, 0, 0],
        "votes_log": []  # [{ts, ip, ua, option_index, name?}]
    }
    save_store(store)
    return pid


def cast_vote(poll_id: str, option_index: int) -> bool:
    store = load_store()
    poll = store["polls"].get(poll_id)
    if not poll or poll["closed"]:
        return False
    if not 0 <= option_index <= 3:
        return False

    poll["totals"][option_index] += 1
    entry = {"ts": int(time.time()), "option_index": option_index}
    poll["votes_log"].append(entry)
    save_store(store)
    return True


def end_poll(poll_id: str) -> bool:
    store = load_store()
    poll = store["polls"].get(poll_id)
    if not poll:
        return False
    poll["closed"] = True
    save_store(store)
    return True


def get_poll(poll_id: str) -> Dict[str, Any]:
    store = load_store()
    return store["polls"].get(poll_id)


def list_polls() -> Dict[str, Any]:
    store = load_store()
    return store["polls"]


# ----------------------------- slack helpers -------------------------------- #

def post_poll_to_slack(webhook_url: str, base_url: str, poll_id: str, question: str, options: List[str]) -> requests.Response:
    """
    Posts a poll message to Slack via Incoming Webhook, with 4 links that point
    back to this Streamlit app for voting.
    """
    vote_urls = []
    for i in range(4):
        params = urllib.parse.urlencode({"poll": poll_id, "vote": i + 1})
        vote_urls.append(f"{base_url}?{params}")

    text_lines = [
        f":bar_chart: *Poll:* {question}",
        "",
        f"1️⃣ <{vote_urls[0]}|{options[0]}>",
        f"2️⃣ <{vote_urls[1]}|{options[1]}>",
        f"3️⃣ <{vote_urls[2]}|{options[2]}>",
        f"4️⃣ <{vote_urls[3]}|{options[3]}>",
        "",
        "_Click an option to vote. Results are shown in the dashboard._",
    ]
    payload = {"text": "\n".join(text_lines)}
    return requests.post(webhook_url, json=payload, timeout=10)


# --------------------------------- UI --------------------------------------- #

st.set_page_config(page_title="Slack Polls (Webhook + Streamlit)", page_icon="📊", layout="wide")

st.title("📊 Slack Polls – Webhook + Streamlit")
st.caption("Create 4-option polls, post them to Slack, and view live results here.")

# Handle vote links (?poll=...&vote=1..4)
params = st.query_params
vote_ack = st.empty()
if "poll" in params and "vote" in params:
    try:
        pid = params.get("poll")
        vote_idx = int(params.get("vote")) - 1
        ok = cast_vote(pid, vote_idx)
        if ok:
            vote_ack.success("✅ Your vote has been recorded! You can close this tab now.")
        else:
            vote_ack.warning("⚠️ Poll not found or it has already been closed.")
    except Exception:
        vote_ack.error("❌ Could not record your vote due to an invalid link.")


with st.sidebar:
    st.header("Settings")
    webhook_url = st.text_input("Slack Incoming Webhook URL", os.environ.get("SLACK_WEBHOOK_URL", ""))
    base_url = st.text_input(
        "Public URL of this app",
        os.environ.get("PUBLIC_BASE_URL", "http://localhost:8501"),
        help="The public URL where this app is running. E.g., https://your-app.streamlit.app",
    )
    st.markdown("---")
    st.subheader("Create a new poll")
    question = st.text_input("Poll Question", placeholder="What's for lunch?")
    col1, col2 = st.columns(2)
    with col1:
        opt1 = st.text_input("Option 1", value="Pizza")
        opt2 = st.text_input("Option 2", value="Salad")
    with col2:
        opt3 = st.text_input("Option 3", value="Tacos")
        opt4 = st.text_input("Option 4", value="Sandwiches")

    if st.button("Create & Post to Slack", type="primary", disabled=not (webhook_url and question.strip())):
        options = [opt1.strip(), opt2.strip(), opt3.strip(), opt4.strip()]
        pid = create_poll(question.strip(), options)
        resp = post_poll_to_slack(webhook_url.strip(), base_url.strip(), pid, question.strip(), options)
        if resp.ok:
            st.success(f"Poll created and posted! Poll ID: {pid}")
            st.code(f"{base_url}?poll={pid}", language="text")
        else:
            st.error(f"Failed to post to Slack: {resp.status_code} {resp.text}")

# Main dashboard
st.markdown("## Poll Dashboard")
polls = list_polls()

def format_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

if not polls:
    st.info("No polls have been created yet. Use the sidebar to create your first one!")
else:
    # Sort by creation time desc
    ordered = sorted(polls.items(), key=lambda kv: kv[1]["created_at"], reverse=True)
    for pid, p in ordered:
        with st.container(border=True):
            status = '🔒 Closed' if p['closed'] else '🟢 Open'
            st.markdown(f"**{p['question']}**\n\n`{pid}` | Created: {format_dt(p['created_at'])} | Status: **{status}**")
            
            c1, c2, c3, c4 = st.columns(4)
            cols = [c1, c2, c3, c4]
            emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
            for i, col in enumerate(cols):
                col.metric(f'{emojis[i]} {p["options"][i]}', p["totals"][i])

            if not p["closed"]:
                if st.button("End this poll", key=f"end_{pid}", type="secondary"):
                    if end_poll(pid):
                        st.success("Poll has been closed.")
                        st.rerun()

            with st.expander("View raw vote log"):
                st.json(p["votes_log"])
