import os
import uuid
import time
import json
import sqlite3
import urllib.parse
from datetime import datetime
from typing import Dict, Any, List

import requests
import streamlit as st

# --------------------------- database helpers (sqlite) ---------------------------- #
DB_PATH = os.path.join(os.environ.get("DATA_DIR", "."), "polls.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS polls (
                id TEXT PRIMARY KEY, poll_type TEXT NOT NULL, question TEXT NOT NULL,
                options TEXT NOT NULL, created_at INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id TEXT NOT NULL, ts INTEGER NOT NULL,
                vote_data TEXT NOT NULL, FOREIGN KEY (poll_id) REFERENCES polls (id)
            );
        ''')
        conn.commit()

def create_poll(poll_type: str, question: str, options: List[str]) -> str:
    pid = uuid.uuid4().hex[:10]
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO polls (id, poll_type, question, options, created_at, closed) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, poll_type, question, json.dumps(options), int(time.time()), 0)
        )
        conn.commit()
    return pid

def cast_vote(poll_id: str, option_index: int, poll_data: Dict) -> bool:
    if not poll_data or poll_data["closed"] or poll_data.get("poll_type") != "single": return False
    vote_data = json.dumps({"option_index": option_index})
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO votes (poll_id, ts, vote_data) VALUES (?, ?, ?)", (poll_id, int(time.time()), vote_data))
        conn.commit()
    return True

def cast_ranked_vote(poll_id: str, name: str, ranking: List[str], poll_data: Dict) -> bool:
    if not poll_data or poll_data["closed"] or poll_data.get("poll_type") != "ranked": return False
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT vote_data FROM votes WHERE poll_id = ?", (poll_id,))
        if any(json.loads(row[0]).get("name") == name for row in cursor.fetchall()): return False
    vote_data = json.dumps({"name": name, "ranking": ranking})
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO votes (poll_id, ts, vote_data) VALUES (?, ?, ?)", (poll_id, int(time.time()), vote_data))
        conn.commit()
    return True

def end_poll(poll_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute("UPDATE polls SET closed = 1 WHERE id = ?", (poll_id,))
        conn.commit()

def get_poll(poll_id: str) -> Dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        poll_row = conn.cursor().execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        if not poll_row: return None
        poll = dict(poll_row)
        poll["options"] = json.loads(poll["options"])
        return poll

def list_polls() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        polls_rows = conn.cursor().execute("SELECT * FROM polls ORDER BY created_at DESC").fetchall()
        polls = []
        for row in polls_rows:
            poll = dict(row)
            poll["options"] = json.loads(poll["options"])
            votes_rows = conn.cursor().execute("SELECT vote_data FROM votes WHERE poll_id = ?", (poll["id"],)).fetchall()
            poll["votes_log"] = [json.loads(v[0]) for v in votes_rows]
            if poll["poll_type"] == "single":
                poll["totals"] = [0] * len(poll["options"])
                for vote in poll["votes_log"]:
                    opt_idx = vote.get("option_index")
                    if isinstance(opt_idx, int) and 0 <= opt_idx < len(poll["totals"]):
                        poll["totals"][opt_idx] += 1
            polls.append(poll)
    return polls

# ----------------------------- slack helpers -------------------------------- #
EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def post_poll_to_slack(webhook_url: str, base_url: str, poll_id: str, poll_data: Dict[str, Any]):
    question, options, poll_type = poll_data["question"], poll_data["options"], poll_data.get("poll_type", "single")
    if poll_type == "single":
        lines = [f":bar_chart: *Poll:* {question}", ""]
        for i, option in enumerate(options):
            params = urllib.parse.urlencode({"poll": poll_id, "vote": i + 1})
            lines.append(f"{EMOJIS[i]} <{base_url}?{params}|{option}>")
        lines.append("\n_Click an option to vote. Results are in the dashboard._")
    else:
        params = urllib.parse.urlencode({'poll': poll_id})
        lines = [f":ballot_box_with_ballot: *Ranked Poll:* {question}", "", f"<{base_url}?{params}|Click here to rank your choices>"]
    return requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10)

# --------------------------------- UI --------------------------------------- #
init_db()
st.set_page_config(page_title="Slack Polls", page_icon="📊", layout="centered")
params = st.query_params
current_poll_id = params.get("poll")
poll_data = get_poll(current_poll_id) if current_poll_id else None

# --- VOTE LANDING PAGE ---
if current_poll_id and poll_data:
    st.title("🗳️ Slack Poll")
    st.header(f"Re: *{poll_data['question']}*")

    if poll_data["closed"]:
        st.error("This poll has been closed and is no longer accepting votes.")
    # Single Choice Vote Confirmation
    elif "vote" in params and poll_data["poll_type"] == "single":
        try:
            vote_idx = int(params.get("vote")) - 1
            if cast_vote(current_poll_id, vote_idx, poll_data):
                st.success("✅ Thank you, your vote has been recorded!")
                st.balloons()
            else:
                st.warning("⚠️ Could not record vote. It may be closed or invalid.")
        except Exception:
            st.error("❌ Invalid vote link.")
    # Ranked Choice Voting Form
    elif poll_data["poll_type"] == "ranked":
        num_options = len(poll_data["options"])
        name = st.text_input("Enter your name to vote (case-sensitive)")
        ranking = st.multiselect("Rank options in your preferred order", options=poll_data["options"], max_selections=num_options)
        if st.button("Submit My Ranking", type="primary", disabled=not (name.strip() and len(ranking) == num_options)):
            if cast_ranked_vote(current_poll_id, name.strip(), ranking, poll_data):
                st.success("✅ Thank you, your ranking has been recorded!")
            else:
                st.error("⚠️ Could not record vote. You may have already voted.")
    else:
        st.warning("This link seems to be incomplete. Please use the links from Slack.")

# --- MAIN DASHBOARD PAGE ---
else:
    st.title("📊 Slack Polls Dashboard")
    with st.sidebar:
        st.header("Configuration")
        webhook_url = st.secrets.get("SLACK_WEBHOOK_URL")
        base_url = st.secrets.get("PUBLIC_BASE_URL")
        if webhook_url and base_url: st.success("✅ Config loaded from secrets.")
        else: st.error("🚨 Config missing in secrets.toml!")
        st.markdown("---")
        st.subheader("Create a New Poll")
        if "options" not in st.session_state: st.session_state.options = ["", ""]
        question = st.text_input("Poll Question")
        poll_type = st.radio("Poll Type", ["Single Choice", "Ranked Preference"], horizontal=True)
        for i in range(len(st.session_state.options)):
            st.session_state.options[i] = st.text_input(f"Option {i + 1}", st.session_state.options[i], key=f"opt_{i}")
        c1, c2 = st.columns(2)
        if c1.button("Add Option", disabled=len(st.session_state.options) >= 10): st.session_state.options.append(""); st.rerun()
        if c2.button("Remove Last", disabled=len(st.session_state.options) <= 2): st.session_state.options.pop(); st.rerun()
        
        options = [opt.strip() for opt in st.session_state.options if opt.strip()]
        if st.button("Create & Post", type="primary", disabled=not all([webhook_url, base_url, question, len(options) >= 2])):
            pid = create_poll("ranked" if poll_type == "Ranked Preference" else "single", question, options)
            resp = post_poll_to_slack(webhook_url, base_url, pid, get_poll(pid))
            if resp.ok: st.success(f"Poll posted! ID: {pid}")
            else: st.error(f"Post failed: {resp.status_code} {resp.text}")

    st.markdown("---")
    st.subheader("Polls")
    for p in list_polls():
        with st.container(border=True):
            status = '🔒 Closed' if p['closed'] else '🟢 Open'
            st.markdown(f"**{p['question']}** (`{p['poll_type'].title()}`)\n\n`{p['id']}` | **Votes: {len(p['votes_log'])}** | Status: **{status}**")
            if p["poll_type"] == "single":
                cols = st.columns(min(len(p["options"]), 4))
                for i, option in enumerate(p["options"]):
                    cols[i % 4].metric(f'{EMOJIS[i]} {option}', p["totals"][i])
            else:
                if p["votes_log"]:
                    import pandas as pd
                    ranks = {f"Rank #{i+1}": [v["ranking"][i] for v in p["votes_log"]] for i in range(len(p["options"]))}
                    df_data = [{"Voter": v["name"], **{k: r[i] for k, r in ranks.items()}} for i, v in enumerate(p["votes_log"])]
                    st.dataframe(pd.DataFrame(df_data), use_container_width=True, hide_index=True)
            if not p["closed"]:
                if st.button("End poll", key=f"end_{p['id']}"): end_poll(p['id']); st.rerun()
