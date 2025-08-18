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
# Changelog:
# - Added "Ranked Preference" poll type.
# - Users vote on ranked polls via a dedicated page in Streamlit.
# - Dashboard now shows total votes for single-choice polls.
# - Dashboard displays a table of ranked responses for ranked polls.
# - Removed default "localhost" URL to prevent errors.
###############################################################################

# ----------------------------- storage helpers ------------------------------ #

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
POLLS_PATH = os.path.join(DATA_DIR, "polls.json")

def _default_store() -> Dict[str, Any]:
    # poll_id -> {type, question, options[4], created_at, closed, totals[4], votes_log}
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
        "type": poll_type, # "single" or "ranked"
        "question": question,
        "options": options,
        "created_at": int(time.time()),
        "closed": False,
        "totals": [0, 0, 0, 0], # Only used for "single" type
        "votes_log": []
    }
    save_store(store)
    return pid

def cast_vote(poll_id: str, option_index: int) -> bool:
    store = load_store()
    poll = store["polls"].get(poll_id)
    if not poll or poll["closed"] or poll.get("type") != "single":
        return False
    if not 0 <= option_index <= 3:
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
    # Prevent duplicate votes by the same name
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

def post_poll_to_slack(webhook_url: str, base_url: str, poll_id: str, poll_data: Dict[str, Any]):
    poll_type = poll_data.get("type", "single")
    question = poll_data["question"]
    options = poll_data["options"]

    if poll_type == "single":
        vote_urls = [f"{base_url}?{urllib.parse.urlencode({'poll': poll_id, 'vote': i + 1})}" for i in range(4)]
        text_lines = [
            f":bar_chart: *Single-Choice Poll:* {question}", "",
            f"1️⃣ <{vote_urls[0]}|{options[0]}>", f"2️⃣ <{vote_urls[1]}|{options[1]}>",
            f"3️⃣ <{vote_urls[2]}|{options[2]}>", f"4️⃣ <{vote_urls[3]}|{options[3]}>", "",
            "_Click an option to vote. Results are shown in the dashboard._",
        ]
    else: # ranked
        vote_url = f"{base_url}?{urllib.parse.urlencode({'poll': poll_id})}"
        text_lines = [
            f": ballot_box_with_ballot: *Ranked Preference Poll:* {question}", "",
            f"<{vote_url}|Click here to rank your choices>", "",
            "_You will be asked to rank all 4 options in your order of preference._"
        ]
    payload = {"text": "\n".join(text_lines)}
    return requests.post(webhook_url, json=payload, timeout=10)

# --------------------------------- UI --------------------------------------- #

st.set_page_config(page_title="Slack Polls (Webhook + Streamlit)", page_icon="📊", layout="wide")
params = st.query_params
current_poll_id = params.get("poll")
poll_data = get_poll(current_poll_id) if current_poll_id else None

# --- Main Page vs. Voting Page Logic ---
if poll_data and poll_data.get("type") == "ranked" and "vote" not in params:
    # RANKED VOTING PAGE
    st.title("🗳️ Rank Your Preference")
    st.header(poll_data["question"])

    if poll_data["closed"]:
        st.error("This poll has been closed and is no longer accepting votes.")
    else:
        name = st.text_input("Enter your name to vote (case-sensitive)")
        st.write("Select the options below in your desired order of preference (1st, 2nd, 3rd, 4th).")
        ranking = st.multiselect(
            "Your Ranking",
            options=poll_data["options"],
            placeholder="Click to select your 1st choice, then 2nd, and so on...",
            key="ranking_multiselect"
        )
        
        if st.button("Submit My Ranking", type="primary", disabled=not (name.strip() and len(ranking) == 4)):
            if cast_ranked_vote(current_poll_id, name.strip(), ranking):
                st.success("✅ **Vote recorded!** Your preference has been saved. You can close this tab.")
                st.balloons()
            else:
                st.error("⚠️ **Could not record vote.** You may have already voted, or the poll is closed.")
        
        if len(ranking) != 4:
            st.warning("Please select and rank all 4 options to submit.")

else:
    # MAIN DASHBOARD PAGE
    st.title("📊 Slack Polls Dashboard")
    vote_ack = st.empty()
    if "poll" in params and "vote" in params:
        try:
            vote_idx = int(params.get("vote")) - 1
            if cast_vote(current_poll_id, vote_idx):
                vote_ack.success("✅ **Vote recorded!** Thank you. You can close this tab.")
            else:
                vote_ack.warning("⚠️ Poll not found, already closed, or is not a single-choice poll.")
        except Exception:
            vote_ack.error("❌ Could not record your vote due to an invalid link.")

    with st.sidebar:
        st.header("Settings")
        webhook_url = st.text_input("Slack Incoming Webhook URL", os.environ.get("SLACK_WEBHOOK_URL", ""), placeholder="https://hooks.slack.com/services/...")
        base_url = st.text_input("Public URL of this app", os.environ.get("PUBLIC_BASE_URL", ""), placeholder="https://your-app.streamlit.app")
        st.markdown("---")
        st.subheader("Create a New Poll")
        poll_type = st.radio("Poll Type", ["Single Choice", "Ranked Preference"], horizontal=True)
        question = st.text_input("Poll Question", placeholder="What's for lunch?")
        opts = [st.text_input(f"Option {i+1}", f"Option {chr(65+i)}") for i in range(4)]
        
        create_disabled = not all([webhook_url.strip(), base_url.strip(), question.strip()])
        if st.button("Create & Post to Slack", type="primary", disabled=create_disabled):
            options = [o.strip() for o in opts]
            poll_type_val = "ranked" if poll_type == "Ranked Preference" else "single"
            pid = create_poll(poll_type_val, question.strip(), options)
            new_poll_data = get_poll(pid)
            resp = post_poll_to_slack(webhook_url.strip(), base_url.strip(), pid, new_poll_data)
            if resp.ok:
                st.success(f"Poll posted! Poll ID: {pid}")
            else:
                st.error(f"Post to Slack failed: {resp.status_code} {resp.text}")

    st.markdown("---")
    if st.button("🔄 Refresh Data"):
        st.rerun()

    polls = load_store()["polls"]
    if not polls:
        st.info("No polls created yet. Use the sidebar to create one!")
    else:
        ordered = sorted(polls.items(), key=lambda kv: kv[1]["created_at"], reverse=True)
        for pid, p in ordered:
            with st.container(border=True):
                poll_type_str = "Ranked Preference" if p.get("type") == "ranked" else "Single Choice"
                status = '🔒 Closed' if p['closed'] else '🟢 Open'
                total_votes = sum(p["totals"]) if p.get("type") == "single" else len(p["votes_log"])
                st.markdown(f"**{p['question']}** (`{poll_type_str}`)\n\n`{pid}` | **Total Votes: {total_votes}** | Status: **{status}**")
                
                if p.get("type") == "single":
                    cols = st.columns(4)
                    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
                    for i, col in enumerate(cols):
                        col.metric(f'{emojis[i]} {p["options"][i]}', p["totals"][i])
                else: # ranked
                    if p["votes_log"]:
                        import pandas as pd
                        df_data = [{"Voter": v["name"], "1st": v["ranking"][0], "2nd": v["ranking"][1], "3rd": v["ranking"][2], "4th": v["ranking"][3]} for v in p["votes_log"]]
                        st.dataframe(pd.DataFrame(df_data), use_container_width=True, hide_index=True)
                    else:
                        st.caption("No ranked votes have been cast yet.")
                
                if not p["closed"]:
                    if st.button("End this poll", key=f"end_{pid}", type="secondary"):
                        end_poll(pid)
                        st.rerun()
