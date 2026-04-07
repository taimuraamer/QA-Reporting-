#!/usr/bin/env python3
"""
QA Weekly Report Bot
---------------------
Fetches data from Jira, generates a report using Claude,
and posts it to Slack automatically.

Usage:
    python qa_report.py                  # Run with .env config
    python qa_report.py --dry-run        # Preview report without posting to Slack
    python qa_report.py --week-offset 1  # Report for last week
"""

import os
import json
import argparse
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
import anthropic

load_dotenv(override=True)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

JIRA_BASE_URL     = os.getenv("JIRA_BASE_URL")        # e.g. https://yourcompany.atlassian.net
JIRA_EMAIL        = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN    = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEYS = [k.strip() for k in os.getenv("JIRA_PROJECT_KEYS", "").split(",") if k.strip()]

SLACK_BOT_TOKEN   = os.getenv("SLACK_BOT_TOKEN")      # xoxb-...
SLACK_CHANNEL     = os.getenv("SLACK_CHANNEL")        # e.g. #qa-reports

CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL")
CONFLUENCE_EMAIL = os.getenv("CONFLUENCE_EMAIL")
CONFLUENCE_API_TOKEN = os.getenv("CONFLUENCE_API_TOKEN")
CONFLUENCE_SPACE_KEY1 = os.getenv("CONFLUENCE_SPACE_KEY1")
CONFLUENCE_PARENT_PAGE_ID1 = os.getenv("CONFLUENCE_PARENT_PAGE_ID1")


# ─────────────────────────────────────────────
# DATE HELPERS
# ─────────────────────────────────────────────

def get_week_range(offset: int = 0):
    """Returns (start, end) ISO strings for the target week (Mon–Sun)."""
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


# ─────────────────────────────────────────────
# JIRA
# ─────────────────────────────────────────────

def fetch_jira_data(week_start: str, week_end: str) -> dict:
    """Fetches bugs and issues from Jira for each configured project."""
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}

    def run_jql(jql: str) -> list:
        url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "maxResults": 100,
            "fields": "summary,priority,status,created,resolutiondate,assignee"
        }
        resp = requests.get(url, headers=headers, auth=auth, params=params)
        resp.raise_for_status()
        return resp.json().get("issues", [])

    def summarize(issues):
        by_priority = {}
        for issue in issues:
            p = issue["fields"].get("priority", {})
            name = p.get("name", "Unknown") if p else "Unknown"
            by_priority[name] = by_priority.get(name, 0) + 1
        return {"total": len(issues), "by_priority": by_priority}

    projects = {}
    for project_key in JIRA_PROJECT_KEYS:
        base = f'project = "{project_key}"'

        new_bugs = run_jql(
            f'{base} AND issuetype = Bug AND created >= "{week_start}" AND created <= "{week_end}"'
        )
        resolved_bugs = run_jql(
            f'{base} AND issuetype = Bug AND statusCategory = Done '
            f'AND resolutiondate >= "{week_start}" AND resolutiondate <= "{week_end}"'
        )
        open_bugs = run_jql(
            f'{base} AND issuetype = Bug AND statusCategory != Done'
        )

        projects[project_key] = {
            "new_bugs": summarize(new_bugs),
            "resolved_bugs": summarize(resolved_bugs),
            "open_bugs": summarize(open_bugs),
            "open_bug_list": [
                {
                    "key": i["key"],
                    "summary": i["fields"]["summary"],
                    "priority": (i["fields"].get("priority") or {}).get("name", "Unknown"),
                    "status": i["fields"]["status"]["name"],
                }
                for i in open_bugs[:10]
            ]
        }

    return projects


# ─────────────────────────────────────────────
# CLAUDE
# ─────────────────────────────────────────────

def generate_report(jira_data: dict, week_start: str, week_end: str) -> str:
    """Uses Claude to generate a polished Slack-formatted QA report."""
    client = anthropic.Anthropic()

    project_names = ", ".join(jira_data.keys())

    prompt = f"""
You are a QA lead writing a weekly quality report for your engineering team on Slack.
Use Slack markdown (bold with *text*, no #headers, use emojis).
Be concise, professional, and highlight risks clearly.

Week: {week_start} → {week_end}
Projects: {project_names}

--- JIRA DATA (per project) ---
{json.dumps(jira_data, indent=2)}

Write the Slack report with these sections:
1. 🐛 Bug Report — break down new, resolved, and top open bugs *per project*, then provide a combined total
2. 🚀 Release Readiness (based on open critical/high bugs across all projects)
3. ⚠️ Risks & Blockers (if any critical/high open bugs exist in either project)
4. ✅ Highlights
5. 📅 Next Steps

Keep the total under 500 words. Do NOT use markdown headers (#). Use *bold* for section titles.
Start with: *📋 Weekly QA Report — {week_start} to {week_end}*
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text


# ─────────────────────────────────────────────
# SLACK
# ─────────────────────────────────────────────

def post_to_slack(report_text: str) -> bool:
    """Posts the report to the configured Slack channel."""
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "channel": SLACK_CHANNEL,
        "text": report_text,
        "mrkdwn": True
    }
    resp = requests.post(url, headers=headers, json=payload)
    data = resp.json()

    if not data.get("ok"):
        print(f"❌ Slack error: {data.get('error')}")
        return False

    print(f"✅ Report posted to {SLACK_CHANNEL}")
    return True

# ─────────────────────────────────────────────
# CONFLUENCE
# ─────────────────────────────────────────────

def confluence_auth():
    return (CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN)

def confluence_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def build_confluence_title(week_start: str, week_end: str) -> str:
    return f"QA Weekly Report - {week_start} to {week_end}"

def build_confluence_body(report_text: str, jira_data: dict, week_start: str, week_end: str) -> str:
    safe_report = (
        report_text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )

    safe_json = (
        json.dumps(jira_data, indent=2)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    return f"""
    <h1>QA Weekly Report</h1>
    <p><strong>Week:</strong> {week_start} to {week_end}</p>

    <h2>Executive Summary</h2>
    <p>{safe_report}</p>

    <h2>Raw Jira Data</h2>
    <pre>{safe_json}</pre>
    """

def find_existing_confluence_page(title: str):
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    params = {
        "title": title,
        "spaceKey": CONFLUENCE_SPACE_KEY1,
        "expand": "version"
    }

    resp = requests.get(
        url,
        headers=confluence_headers(),
        auth=confluence_auth(),
        params=params,
        timeout=30
    )
    resp.raise_for_status()

    results = resp.json().get("results", [])
    return results[0] if results else None

def create_confluence_page(title: str, body_html: str):
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": CONFLUENCE_SPACE_KEY1},
        "ancestors": [{"id": str(CONFLUENCE_PARENT_PAGE_ID1)}],
        "body": {
            "storage": {
                "value": body_html,
                "representation": "storage"
            }
        }
    }

    resp = requests.post(
        url,
        headers=confluence_headers(),
        auth=confluence_auth(),
        json=payload,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()

def update_confluence_page(page_id: str, title: str, body_html: str, current_version: int):
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}"
    payload = {
        "id": str(page_id),
        "type": "page",
        "title": title,
        "version": {"number": current_version + 1},
        "body": {
            "storage": {
                "value": body_html,
                "representation": "storage"
            }
        }
    }

    resp = requests.put(
        url,
        headers=confluence_headers(),
        auth=confluence_auth(),
        json=payload,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()

def post_to_confluence(report_text: str, jira_data: dict, week_start: str, week_end: str) -> bool:
    required = [
        CONFLUENCE_BASE_URL,
        CONFLUENCE_EMAIL,
        CONFLUENCE_API_TOKEN,
        CONFLUENCE_SPACE_KEY1,
        CONFLUENCE_PARENT_PAGE_ID1,
    ]
    if not all(required):
        print("❌ Confluence configuration is incomplete.")
        return False

    title = build_confluence_title(week_start, week_end)
    body_html = build_confluence_body(report_text, jira_data, week_start, week_end)

    try:
        existing = find_existing_confluence_page(title)

        if existing:
            update_confluence_page(
                page_id=existing["id"],
                title=title,
                body_html=body_html,
                current_version=existing["version"]["number"]
            )
            print(f"✅ Confluence page updated: {title}")
        else:
            create_confluence_page(title, body_html)
            print(f"✅ Confluence page created under parent page: {title}")

        return True

    except requests.HTTPError as e:
        print(f"❌ Confluence HTTP error: {e}")
        return False
    except Exception as e:
        print(f"❌ Confluence error: {e}")
        return False

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="QA Weekly Report Bot")
    parser.add_argument("--dry-run", action="store_true", help="Print report without posting to Slack")
    parser.add_argument("--week-offset", type=int, default=0, help="0 = this week, 1 = last week, etc.")
    args = parser.parse_args()

    week_start, week_end = get_week_range(args.week_offset)
    print(f"\n📅 Generating QA report for {week_start} → {week_end}\n")

    print("🔍 Fetching Jira data...")
    jira_data = fetch_jira_data(week_start, week_end)

    print("🤖 Generating report with Claude...")
    report = generate_report(jira_data, week_start, week_end)

    print("\n" + "─" * 60)
    print(report)
    print("─" * 60 + "\n")

    if args.dry_run:
        print("🧪 Dry run — report NOT posted to Slack.")
    else:
        print("📤 Posting to Slack...")
        post_to_slack(report)
        print("Posting to Confluence...")
        post_to_confluence(report, jira_data, week_start, week_end)


if __name__ == "__main__":
    main()
