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
                id TEXT PRIMARY KEY,
                poll_type TEXT NOT NULL,
                question TEXT NOT NULL,
                options TEXT NOT NULL, -- JSON array of strings
                created_at INTEGER NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                vote_data TEXT NOT NULL, -- JSON object
                FOREIGN KEY (poll_id) REFERENCES polls (id)
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
    if not poll_data or poll_data["closed"] or poll_data.get("poll_type") != "single":
        return False
    vote_data = json.dumps({"option_index": option_index})
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO votes (poll_id, ts, vote_data) VALUES (?, ?, ?)",
            (poll_id, int(time.time()), vote_data)
        )
        conn.commit()
    return True

def cast_ranked_vote(poll_id: str, name: str, ranking: List[str], poll_data: Dict) -> bool:
    if not poll_data or poll_data["closed"] or poll_data.get("poll_type") != "ranked":
        return False
    # Check for existing vote by the same name
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT vote_data FROM votes WHERE poll_id = ?", (poll_id,))
        for row in cursor.fetchall():
            if json.loads(row[0]).get("name") == name:
                return False # Duplicate vote
    
    vote_data = json.dumps({"name": name, "ranking": ranking})
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO votes (poll_id, ts, vote_data) VALUES (?, ?, ?)",
            (poll_id, int(time.time()), vote_data)
        )
        conn.commit()
    return True


def end_poll(poll_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE polls SET closed = 1 WHERE id = ?", (poll_id,))
        conn.commit()
    return True

def get_poll(poll_id: str) -> Dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM polls WHERE id = ?", (poll_id,))
        poll_row = cursor.fetchone()
        if not poll_row:
            return None
        poll = dict(poll_row)
        poll["options"] = json.loads(poll["options"])
        return poll

def list_polls() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM polls ORDER BY created_at DESC")
        polls_rows = cursor.fetchall()
        
        polls = []
        for row in polls_rows:
            poll = dict(row)
            poll["options"] = json.loads(poll["options"])
            poll["votes_log"] = []
            poll["totals"] = [0] * len(poll["options"])
            
            cursor.execute("SELECT vote_data FROM votes WHERE poll_id = ?", (poll["id"],))
            votes_rows = cursor.fetchall()
            
            for v_row in votes_rows:
                vote_data = json.loads(v_row["vote_data"])
                poll["votes_log"].append(vote_data)
                if poll["poll_type"] == "single":
                    opt_idx = vote_data.get("option_index")
                    if isinstance(opt_idx, int) and 0 <= opt_idx < len(poll["totals"]):
                        poll["totals"][opt_idx] += 1
            polls.append(poll)
    return polls


# ----------------------------- slack helpers -------------------------------- #
EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def post_poll_to_slack(webhook_url: str, base_url: str, poll_id: str, poll_data: Dict[str, Any]):
    poll_type = poll_data.get("poll_type", "single")
    question = poll_data["question"]
    options = poll_data["options"]

    if poll_type == "single":
        text_lines = [f":bar_chart: *Single-Choice Poll:* {question}", ""]
        for i, option in enumerate(options):
            params = urllib.parse.urlencode({"poll": poll_id, "vote": i + 1})
            vote_url = f"{base_url}?{params}"
            text_lines.append(f"{EMOJIS[i]} <{vote_url}|{option}>")
        text_lines.append("\n_Click an option to vote. Results are in the dashboard._")
    else: # ranked
        vote_url = f"{base_url}?{urllib.parse.urlencode({'poll': poll_id})}"
        text_lines = [
            f":ballot_box_with_ballot: *Ranked Preference Poll:* {question}", "",
            f"<{vote_url}|Click here to rank your choices>", "",
            f"_You will be asked to rank all {len(options)} options._"
        ]
    payload = {"text": "\n".join(text_lines)}
    return requests.post(webhook_url, json=payload, timeout=10)

# --------------------------------- UI --------------------------------------- #

# Initialize database on first run
init_db()

st.set_page_config(page_title="Slack Polls", page_icon="📊", layout="wide")
params = st.query_params
current_poll_id = params.get("poll")
poll_data = get_poll(current_poll_id) if current_poll_id else None

# --- Main Page vs. Voting Page Logic ---
if poll_data and poll_data.get("poll_type") == "ranked" and "vote" not in params:
    # RANKED VOTING PAGE
    st.title("🗳️ Rank Your Preference")
    st.header(poll_data["question"])
    num_options = len(poll_data["options"])
    if poll_data["closed"]:
        st.error("This poll has been closed.")
    else:
        name = st.text_input("Enter your name to vote (case-sensitive)")
        ranking = st.multiselect(
            "Select in your desired order of preference", options=poll_data["options"], max_selections=num_options
        )
        if st.button("Submit My Ranking", type="primary", disabled=not (name.strip() and len(ranking) == num_options)):
            if cast_ranked_vote(current_poll_id, name.strip(), ranking, poll_data):
                st.success("✅ **Vote recorded!** You can close this tab.")
            else:
                st.error("⚠️ **Could not record vote.** You may have already voted.")
        if len(ranking) != num_options:
            st.warning(f"Please select and rank all {num_options} options.")
else:
    # MAIN DASHBOARD PAGE
    st.title("📊 Slack Polls Dashboard")
    vote_ack = st.empty()
    if "poll" in params and "vote" in params:
        try:
            vote_idx = int(params.get("vote")) - 1
            if cast_vote(current_poll_id, vote_idx, poll_data):
                vote_ack.success("✅ **Vote recorded!** You can close this tab.")
            else:
                vote_ack.warning("⚠️ Poll not found, closed, or invalid.")
        except Exception:
            vote_ack.error("❌ Invalid vote link.")

    with st.sidebar:
        st.header("Configuration")
        webhook_url = st.secrets.get("SLACK_WEBHOOK_URL")
        base_url = st.secrets.get("PUBLIC_BASE_URL")
        
        if webhook_url and base_url:
            st.success("✅ Configuration loaded from secrets.")
        else:
            st.error("🚨 Config missing from secrets!")
            st.caption("Add `SLACK_WEBHOOK_URL` and `PUBLIC_BASE_URL` to `.streamlit/secrets.toml`.")
        st.markdown("---")

        st.subheader("Create a New Poll")
        
        # Use session state to manage form inputs
        if "question" not in st.session_state: st.session_state.question = ""
        if "options" not in st.session_state: st.session_state.options = ["Option A", "Option B"]

        st.session_state.question = st.text_input("Poll Question", value=st.session_state.question, key="question_input")
        poll_type = st.radio("Poll Type", ["Single Choice", "Ranked Preference"], horizontal=True)
        
        for i in range(len(st.session_state.options)):
            st.session_state.options[i] = st.text_input(f"Option {i + 1}", st.session_state.options[i], key=f"opt_{i}")
        
        c1, c2 = st.columns(2)
        if c1.button("Add Option", use_container_width=True, disabled=len(st.session_state.options) >= 10):
            st.session_state.options.append("")
            st.rerun()
        if c2.button("Remove Last", use_container_width=True, disabled=len(st.session_state.options) <= 2):
            st.session_state.options.pop()
            st.rerun()

        options = [opt.strip() for opt in st.session_state.options if opt.strip()]
        
        create_disabled = not all([webhook_url, base_url, st.session_state.question.strip(), len(options) >= 2])
        if st.button("Create & Post to Slack", type="primary", disabled=create_disabled):
            poll_type_val = "ranked" if poll_type == "Ranked Preference" else "single"
            pid = create_poll(poll_type_val, st.session_state.question.strip(), options)
            new_poll_data = get_poll(pid)
            resp = post_poll_to_slack(webhook_url.strip(), base_url.strip(), pid, new_poll_data)
            if resp.ok:
                st.success(f"Poll posted! ID: {pid}")
                # Reset form for next poll
                st.session_state.question = ""
                st.session_state.options = ["Option A", "Option B"]
                st.rerun()
            else:
                st.error(f"Post to Slack failed: {resp.status_code} {resp.text}")

    st.markdown("---")
    st.subheader("Polls")
    polls = list_polls()
    if not polls:
        st.info("No polls created yet. Use the sidebar to create one!")
    else:
        for p in polls:
            with st.container(border=True):
                status = '🔒 Closed' if p['closed'] else '🟢 Open'
                total_votes = len(p["votes_log"])
                st.markdown(f"**{p['question']}** (`{p['poll_type'].replace('_', ' ').title()}`)\n\n`{p['id']}` | **Votes: {total_votes}** | Status: **{status}**")
                
                if p["poll_type"] == "single":
                    num_options = len(p["options"])
                    rows = [p["options"][i:i + 4] for i in range(0, num_options, 4)]
                    start_index = 0
                    for row_options in rows:
                        cols = st.columns(len(row_options))
                        for c_idx, option in enumerate(row_options):
                            total_index = start_index + c_idx
                            with cols[c_idx]:
                                st.metric(f'{EMOJIS[total_index]} {option}', p["totals"][total_index])
                        start_index += len(row_options)
                else: # ranked
                    if p["votes_log"]:
                        import pandas as pd
                        df_data = [{"Voter": v["name"]} for v in p["votes_log"]]
                        for i in range(len(p["options"])):
                            for row_idx, vote in enumerate(p["votes_log"]):
                                df_data[row_idx][f"Rank #{i+1}"] = vote["ranking"][i]
                        st.dataframe(pd.DataFrame(df_data), use_container_width=True, hide_index=True)
                
                if not p["closed"]:
                    if st.button("End this poll", key=f"end_{p['id']}"):
                        end_poll(p['id'])
                        st.rerun()
