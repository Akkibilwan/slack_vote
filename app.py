import os
import uuid
import time
import json
import urllib.parse
from datetime import datetime
from typing import Dict, Any, List

import requests
import streamlit as st

# ----------------------------- storage helpers ------------------------------ #

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
POLLS_PATH = os.path.join(DATA_DIR, "polls.json")

def _default_store() -> Dict[str, Any]:
    return {"polls": {}}

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

def create_poll(poll_type: str, question: str, options: List[str]) -> str:
    store = load_store()
    pid = uuid.uuid4().hex[:10]
    store["polls"][pid] = {
        "type": poll_type,
        "question": question,
        "options": options,
        "created_at": int(time.time()),
        "closed": False,
        "totals": [0] * len(options),
        "votes_log": []
    }
    save_store(store)
    return pid

def cast_vote(poll_id: str, option_index: int) -> bool:
    store = load_store()
    poll = store["polls"].get(poll_id)
    if not poll or poll["closed"] or poll.get("type") != "single":
        return False
    if not 0 <= option_index < len(poll["options"]):
        return False
    poll["totals"][option_index] += 1
    poll["votes_log"].append({"ts": int(time.time()), "option_index": option_index})
    save_store(store)
    return True

def cast_ranked_vote(poll_id: str, name: str, ranking: List[str]) -> bool:
    store = load_store()
    poll = store["polls"].get(poll_id)
    if not poll or poll["closed"] or poll.get("type") != "ranked":
        return False
    if any(vote.get("name") == name for vote in poll["votes_log"]):
        return False
    poll["votes_log"].append({"ts": int(time.time()), "name": name, "ranking": ranking})
    save_store(store)
    return True

def end_poll(poll_id: str) -> bool:
    store = load_store()
    poll = store["polls"].get(poll_id)
    if not poll: return False
    poll["closed"] = True
    save_store(store)
    return True

def get_poll(poll_id: str) -> Dict[str, Any]:
    return load_store()["polls"].get(poll_id)

# ----------------------------- slack helpers -------------------------------- #
EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def post_poll_to_slack(webhook_url: str, base_url: str, poll_id: str, poll_data: Dict[str, Any]):
    poll_type = poll_data.get("type", "single")
    question = poll_data["question"]
    options = poll_data["options"]

    if poll_type == "single":
        text_lines = [f":bar_chart: *Single-Choice Poll:* {question}", ""]
        for i, option in enumerate(options):
            params = urllib.parse.urlencode({"poll": poll_id, "vote": i + 1})
            vote_url = f"{base_url}?{params}"
            text_lines.append(f"{EMOJIS[i]} <{vote_url}|{option}>")
        text_lines.append("\n_Click an option to vote. Results are shown in the dashboard._")
    else: # ranked
        vote_url = f"{base_url}?{urllib.parse.urlencode({'poll': poll_id})}"
        text_lines = [
            f":ballot_box_with_ballot: *Ranked Preference Poll:* {question}", "",
            f"<{vote_url}|Click here to rank your choices>", "",
            f"_You will be asked to rank all {len(options)} options in your order of preference._"
        ]
    payload = {"text": "\n".join(text_lines)}
    return requests.post(webhook_url, json=payload, timeout=10)

# --------------------------------- UI --------------------------------------- #

st.set_page_config(page_title="Slack Polls", page_icon="📊", layout="wide")
params = st.query_params
current_poll_id = params.get("poll")
poll_data = get_poll(current_poll_id) if current_poll_id else None

# --- Main Page vs. Voting Page Logic ---
if poll_data and poll_data.get("type") == "ranked" and "vote" not in params:
    # RANKED VOTING PAGE
    st.title("🗳️ Rank Your Preference")
    st.header(poll_data["question"])
    num_options = len(poll_data["options"])

    if poll_data["closed"]:
        st.error("This poll has been closed.")
    else:
        name = st.text_input("Enter your name to vote (case-sensitive)")
        ranking = st.multiselect(
            f"Select the options below in your desired order of preference (1st, 2nd, etc.).",
            options=poll_data["options"],
            placeholder="Click to select your 1st choice, then 2nd, and so on...",
            max_selections=num_options
        )
        if st.button("Submit My Ranking", type="primary", disabled=not (name.strip() and len(ranking) == num_options)):
            if cast_ranked_vote(current_poll_id, name.strip(), ranking):
                st.success("✅ **Vote recorded!** You can close this tab.")
            else:
                st.error("⚠️ **Could not record vote.** You may have already voted.")
        if len(ranking) != num_options:
            st.warning(f"Please select and rank all {num_options} options to submit.")
else:
    # MAIN DASHBOARD PAGE
    st.title("📊 Slack Polls Dashboard")
    vote_ack = st.empty()
    if "poll" in params and "vote" in params:
        try:
            vote_idx = int(params.get("vote")) - 1
            if cast_vote(current_poll_id, vote_idx):
                vote_ack.success("✅ **Vote recorded!** You can close this tab.")
            else:
                vote_ack.warning("⚠️ Poll not found, closed, or invalid.")
        except Exception:
            vote_ack.error("❌ Invalid vote link.")

    # --- Sidebar for settings and poll creation ---
    with st.sidebar:
        st.header("Settings")
        webhook_url = st.secrets.get("SLACK_WEBHOOK_URL")
        if webhook_url:
            st.success("✅ Slack Webhook loaded from secrets.")
        else:
            st.error("🚨 Slack Webhook not found!")
            st.caption("Add your webhook to `.streamlit/secrets.toml`.")
        
        base_url = st.text_input("Public URL of this app", placeholder="https://your-app.streamlit.app")
        st.markdown("---")

        st.subheader("Create a New Poll")
        poll_type = st.radio("Poll Type", ["Single Choice", "Ranked Preference"], horizontal=True)
        question = st.text_input("Poll Question", placeholder="What should we focus on next?")
        
        # Dynamic options management
        if "options" not in st.session_state:
            st.session_state.options = ["Feature A", "Feature B"]
        
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
        
        create_disabled = not all([webhook_url, base_url.strip(), question.strip(), len(options) >= 2])
        if st.button("Create & Post to Slack", type="primary", disabled=create_disabled):
            poll_type_val = "ranked" if poll_type == "Ranked Preference" else "single"
            pid = create_poll(poll_type_val, question.strip(), options)
            new_poll_data = get_poll(pid)
            resp = post_poll_to_slack(webhook_url.strip(), base_url.strip(), pid, new_poll_data)
            if resp.ok:
                st.success(f"Poll posted! Poll ID: {pid}")
            else:
                st.error(f"Post to Slack failed: {resp.status_code} {resp.text}")

    # --- Main dashboard display ---
    st.markdown("---")
    st.subheader("Polls")
    polls = load_store()["polls"]
    if not polls:
        st.info("No polls created yet. Use the sidebar to create one!")
    else:
        ordered = sorted(polls.items(), key=lambda kv: kv[1]["created_at"], reverse=True)
        for pid, p in ordered:
            with st.container(border=True):
                poll_type_str = "Ranked Preference" if p.get("type") == "ranked" else "Single Choice"
                status = '🔒 Closed' if p['closed'] else '🟢 Open'
                total_votes = len(p["votes_log"])
                st.markdown(f"**{p['question']}** (`{poll_type_str}`)\n\n`{pid}` | **Total Votes: {total_votes}** | Status: **{status}**")
                
                if p.get("type") == "single":
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
                    else:
                        st.caption("No ranked votes have been cast yet.")
                
                if not p["closed"]:
                    if st.button("End this poll", key=f"end_{pid}"):
                        end_poll(pid)
                        st.rerun()
