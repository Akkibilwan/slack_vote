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
import pandas as pd
import openai

# --------------------------- database helpers (sqlite) ---------------------------- #
DB_PATH = os.path.join(os.environ.get("DATA_DIR", "."), "polls.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS polls (
                id TEXT PRIMARY KEY, poll_type TEXT NOT NULL, question TEXT NOT NULL,
                options TEXT NOT NULL, created_at INTEGER NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0, summary TEXT
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id TEXT NOT NULL, ts INTEGER NOT NULL,
                vote_data TEXT NOT NULL, FOREIGN KEY (poll_id) REFERENCES polls (id)
            );
        ''')
        conn.commit()

def create_poll(poll_type: str, question: str, options: Dict) -> str:
    pid = uuid.uuid4().hex[:10]
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute(
            "INSERT INTO polls (id, poll_type, question, options, created_at) VALUES (?, ?, ?, ?, ?)",
            (pid, poll_type, question, json.dumps(options), int(time.time()))
        )
        conn.commit()
    return pid

def cast_vote(poll_id: str, vote_data: Dict, poll_data: Dict) -> bool:
    if not poll_data or poll_data["closed"]: return False
    if poll_data["poll_type"] != "single":
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT vote_data FROM votes WHERE poll_id = ?", (poll_id,))
            if any(json.loads(row[0]).get("name") == vote_data.get("name") for row in cursor.fetchall()):
                return False
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute(
            "INSERT INTO votes (poll_id, ts, vote_data) VALUES (?, ?, ?)",
            (poll_id, int(time.time()), json.dumps(vote_data))
        )
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
        polls = [dict(row) for row in conn.cursor().execute("SELECT * FROM polls ORDER BY created_at DESC").fetchall()]
        for poll in polls:
            poll["options"] = json.loads(poll["options"])
            votes_rows = conn.cursor().execute("SELECT vote_data FROM votes WHERE poll_id = ?", (poll["id"],)).fetchall()
            poll["votes_log"] = [json.loads(v[0]) for v in votes_rows]
            if poll["poll_type"] == "single":
                poll["totals"] = [0] * len(poll["options"]["choices"])
                for vote in poll["votes_log"]:
                    opt_idx = vote.get("option_index")
                    if isinstance(opt_idx, int) and 0 <= opt_idx < len(poll["totals"]):
                        poll["totals"][opt_idx] += 1
    return polls

def update_summary(poll_id: str, summary: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute("UPDATE polls SET summary = ? WHERE id = ?", (summary, poll_id))
        conn.commit()

# ----------------------------- AI Summary -------------------------------- #
def generate_summary(poll_data: dict, api_key: str):
    openai.api_key = api_key
    results = ""
    # Create a string representation of the results based on poll type
    if poll_data['poll_type'] == 'single':
        results = "\n".join([f"- {opt}: {count} votes" for opt, count in zip(poll_data['options']['choices'], poll_data['totals'])])
    elif poll_data['poll_type'] == 'ranked':
        results = json.dumps([v['ranking'] for v in poll_data['votes_log']], indent=2)
    elif poll_data['poll_type'] == 'matrix':
        results = json.dumps(poll_data['votes_log'], indent=2)

    prompt = f"""
    Based ONLY on the data provided below, generate a concise, neutral, data-driven summary of the poll results.
    - Start with a single sentence stating the main outcome.
    - Use bullet points to highlight key statistics or trends.
    - Do not add any information, opinions, or predictions not present in the data.

    **Poll Question:** {poll_data['question']}
    **Poll Type:** {poll_data['poll_type']}
    **Total Votes:** {len(poll_data['votes_log'])}

    **Results Data:**
    {results}
    """
    try:
        response = openai.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.0)
        return response.choices[0].message.content
    except Exception as e:
        return f"Error generating summary: {e}"

# ----------------------------- UI & Slack Helpers -------------------------------- #
EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def post_poll_to_slack(webhook_url: str, base_url: str, poll_id: str, poll_data: Dict[str, Any]):
    question, poll_type = poll_data["question"], poll_data["poll_type"]
    params = urllib.parse.urlencode({'poll': poll_id})
    vote_url = f"{base_url}?{params}"

    if poll_type == "single":
        lines = [f":bar_chart: *Poll:* {question}", ""]
        for i, option in enumerate(poll_data["options"]["choices"]):
            vote_params = urllib.parse.urlencode({"poll": poll_id, "vote": i + 1})
            lines.append(f"{EMOJIS[i]} <{base_url}?{vote_params}|{option}>")
    else:
        title = "Ranked Poll" if poll_type == "ranked" else "Matrix Poll"
        icon = ":ballot_box_with_ballot:" if poll_type == "ranked" else ":clipboard:"
        lines = [f"{icon} *{title}:* {question}", "", f"<{vote_url}|Click Here to Respond>"]
    
    return requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10)

def render_vote_page(poll_data: dict):
    st.title("🗳️ Slack Poll")
    st.header(f"*{poll_data['question']}*")
    if poll_data["closed"]:
        st.error("This poll is closed.")
        return

    params = st.query_params
    current_poll_id = params.get("poll")
    if "vote" in params and poll_data["poll_type"] == "single":
        vote_idx = int(params.get("vote")) - 1
        if cast_vote(current_poll_id, {"option_index": vote_idx}, poll_data): st.success("✅ Thank you, your vote has been recorded!")
        else: st.warning("⚠️ Could not record vote.")
    elif poll_data["poll_type"] in ["ranked", "matrix"]:
        name = st.text_input("Enter your name to vote")
        vote_recorded = False
        if poll_data["poll_type"] == "ranked":
            num_options = len(poll_data["options"]["choices"])
            ranking = st.multiselect("Rank in preferred order", options=poll_data["options"]["choices"], max_selections=num_options)
            st.caption("Button activates when all items are ranked.")
            if st.button("Submit Ranking", disabled=not (name.strip() and len(ranking) == num_options)):
                vote_recorded = cast_vote(current_poll_id, {"name": name.strip(), "ranking": ranking}, poll_data)
        elif poll_data["poll_type"] == "matrix":
            responses = {}
            for item_idx, item in enumerate(poll_data["options"]["items"]):
                st.subheader(item)
                responses[item] = {}
                for param_idx, param in enumerate(poll_data["options"]["criteria"]):
                    criterion_label, criterion_type = param["label"], param["type"]
                    key = f"{item_idx}_{param_idx}" # Unique key
                    
                    if criterion_type == "Yes/No":
                        responses[item][criterion_label] = st.radio(criterion_label, ["Yes", "No"], key=key, horizontal=True)
                    elif criterion_type == "Scale (1-5)":
                        responses[item][criterion_label] = st.select_slider(criterion_label, options=range(1, 6), key=key)
                    elif criterion_type == "Text":
                        responses[item][criterion_label] = st.text_input(criterion_label, key=key)

            if st.button("Submit Ratings", disabled=not name.strip()):
                vote_recorded = cast_vote(current_poll_id, {"name": name.strip(), "responses": responses}, poll_data)
        
        if vote_recorded: st.success("✅ Thank you, your response has been recorded!")

# *** NEW: Helper functions for displaying results ***
def display_single_choice_results(poll):
    choices = poll["options"]["choices"]
    totals = poll["totals"]
    
    # Metrics
    cols = st.columns(min(len(choices), 4))
    for i, choice in enumerate(choices):
        cols[i % 4].metric(f'{EMOJIS[i]} {choice}', totals[i])

    # Bar Chart
    if sum(totals) > 0:
        chart_data = pd.DataFrame({"options": choices, "votes": totals}).set_index("options")
        st.bar_chart(chart_data)

def display_ranked_results(poll):
    choices = poll["options"]["choices"]
    votes = poll["votes_log"]
    num_choices = len(choices)
    
    # Borda Count weighted scores
    scores = {choice: 0 for choice in choices}
    for vote in votes:
        for i, choice in enumerate(vote["ranking"]):
            scores[choice] += (num_choices - i)
    
    sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    
    st.subheader("Weighted Scores")
    df = pd.DataFrame(sorted_scores, columns=["Option", "Score"]).set_index("Option")
    st.bar_chart(df)

    with st.expander("View individual votes"):
        st.dataframe(pd.DataFrame([{"Voter": v["name"], **{f"Rank #{i+1}": r for i, r in enumerate(v["ranking"])}} for v in votes]), use_container_width=True, hide_index=True)

def display_matrix_results(poll):
    items = poll["options"]["items"]
    criteria = poll["options"]["criteria"]
    votes = poll["votes_log"]

    summary_rows = []
    for item in items:
        row = {"Topic": item}
        for crit in criteria:
            label, type = crit["label"], crit["type"]
            responses = [v["responses"].get(item, {}).get(label) for v in votes if v["responses"].get(item)]
            
            if type == "Yes/No":
                yes_count = responses.count("Yes")
                total = responses.count("Yes") + responses.count("No")
                row[label] = f"{yes_count / total * 100:.0f}% Yes" if total > 0 else "N/A"
            elif type == "Scale (1-5)":
                numeric_responses = [r for r in responses if isinstance(r, (int, float))]
                avg_score = sum(numeric_responses) / len(numeric_responses) if numeric_responses else 0
                row[label] = f"{avg_score:.2f} avg"
            elif type == "Text":
                text_count = len([r for r in responses if r and str(r).strip()])
                row[label] = f"{text_count} response(s)"
        summary_rows.append(row)
        
    st.subheader("Aggregated Results")
    st.dataframe(pd.DataFrame(summary_rows).set_index("Topic"), use_container_width=True)

    with st.expander("View individual votes and text responses"):
        df_data = [{"Voter": vote["name"], **{f"{item} - {crit['label']}": vote["responses"].get(item, {}).get(crit['label']) for item in items for crit in criteria}} for vote in votes]
        st.dataframe(pd.DataFrame(df_data), use_container_width=True, hide_index=True)

def render_dashboard():
    st.title("📊 Slack Polls Dashboard")
    with st.sidebar:
        # Sidebar logic (unchanged)...
        st.header("Configuration")
        webhook_url = st.secrets.get("SLACK_WEBHOOK_URL")
        base_url = st.secrets.get("PUBLIC_BASE_URL")
        st.session_state.openai_api_key = st.secrets.get("OPENAI_API_KEY")
        if webhook_url and base_url: st.success("✅ Config loaded.")
        else: st.error("🚨 Config missing in secrets.toml!")
        st.markdown("---")
        st.subheader("Create a New Poll")
        poll_type = st.radio("Poll Type", ["Single Choice", "Ranked Preference", "Matrix"])
        question = st.text_input("Poll Question", key="poll_question")
        options_data = {}

        if poll_type in ["Single Choice", "Ranked Preference"]:
            if "choices" not in st.session_state: st.session_state.choices = ["", ""]
            for i in range(len(st.session_state.choices)):
                st.session_state.choices[i] = st.text_input(f"Option {i + 1}", st.session_state.choices[i], key=f"choice_{i}")
            c1, c2 = st.columns(2); c1.button("Add Option", on_click=lambda: st.session_state.choices.append("")); c2.button("Remove Last Option", on_click=lambda: st.session_state.choices.pop())
            options_data["choices"] = [c.strip() for c in st.session_state.choices if c.strip()]
        elif poll_type == "Matrix":
            if "matrix_items" not in st.session_state: st.session_state.matrix_items = ["Topic A"]
            if "matrix_criteria" not in st.session_state: st.session_state.matrix_criteria = [{"label": "Wider TAM?", "type": "Yes/No"}]
            st.write("**Topics to Rate**")
            for i in range(len(st.session_state.matrix_items)):
                st.session_state.matrix_items[i] = st.text_input(f"Topic {i+1}", st.session_state.matrix_items[i], key=f"item_{i}")
            c1, c2 = st.columns(2); c1.button("Add Topic", on_click=lambda: st.session_state.matrix_items.append("")); c2.button("Remove Last Topic", on_click=lambda: st.session_state.matrix_items.pop())
            st.write("**Criteria**")
            for i in range(len(st.session_state.matrix_criteria)):
                c1, c2 = st.columns([2, 1])
                st.session_state.matrix_criteria[i]["label"] = c1.text_input(f"Criterion {i+1}", st.session_state.matrix_criteria[i]["label"], key=f"crit_label_{i}")
                st.session_state.matrix_criteria[i]["type"] = c2.selectbox("Type", ["Yes/No", "Scale (1-5)", "Text"], key=f"crit_type_{i}", index=["Yes/No", "Scale (1-5)", "Text"].index(st.session_state.matrix_criteria[i]["type"]))
            c1, c2 = st.columns(2); c1.button("Add Criterion", on_click=lambda: st.session_state.matrix_criteria.append({"label": "", "type": "Yes/No"})); c2.button("Remove Last Criterion", on_click=lambda: st.session_state.matrix_criteria.pop())
            options_data["items"] = [i.strip() for i in st.session_state.matrix_items if i.strip()]
            options_data["criteria"] = [c for c in st.session_state.matrix_criteria if c["label"].strip()]

        if st.button("Create & Post", type="primary", disabled=not all([webhook_url, base_url, question, options_data])):
            pid = create_poll(poll_type.lower().replace(" ", "_"), question, options_data)
            post_poll_to_slack(webhook_url, base_url, pid, get_poll(pid))
            st.success(f"Poll posted!")

    if st.button("🔄 Refresh Data"): st.rerun()
    st.markdown("---")
    for p in list_polls():
        with st.container(border=True):
            status = '🔒 Closed' if p['closed'] else '🟢 Open'
            st.markdown(f"**{p['question']}** (`{p['poll_type'].replace('_', ' ').title()}`)\n\n`{p['id']}` | **Votes: {len(p['votes_log'])}** | Status: **{status}**")
            
            results_tab, summary_tab = st.tabs(["📊 Results", "✨ AI Summary"])
            with results_tab:
                if not p["votes_log"]:
                    st.info("No votes have been cast yet.")
                else:
                    # *** NEW: Call the appropriate display function ***
                    if p['poll_type'] == 'single':
                        display_single_choice_results(p)
                    elif p['poll_type'] == 'ranked':
                        display_ranked_results(p)
                    elif p['poll_type'] == 'matrix':
                        display_matrix_results(p)

                st.markdown("---")
                if not p['closed']:
                    if st.button("End poll", key=f"end_{p['id']}"): end_poll(p['id']); st.rerun()
            with summary_tab:
                if p['closed']:
                    if p.get('summary'):
                        st.markdown(p['summary'])
                    elif st.button("Generate Summary", key=f"sum_{p['id']}", disabled=not st.session_state.get('openai_api_key')):
                        with st.spinner("Generating AI summary..."):
                            summary = generate_summary(p, st.session_state.openai_api_key)
                            update_summary(p['id'], summary); st.rerun()
                else:
                    st.info("Please end the poll before generating an AI summary.")

# --------------------------------- MAIN --------------------------------------- #
init_db()
current_poll_id = st.query_params.get("poll")
poll_data = get_poll(current_poll_id) if current_poll_id else None

if poll_data:
    render_vote_page(poll_data)
else:
    render_dashboard()
