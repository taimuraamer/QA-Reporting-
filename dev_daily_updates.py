#!/usr/bin/env python3
"""
Dev Daily Update Bot
---------------------
Fetches tickets resolved today from Jira, grouped by assignee,
generates a summary using Claude, and posts to Confluence as a
dated daily update page under the configured parent folder.

Usage:
    python dev_daily_report.py                  # Run for today
    python dev_daily_report.py --dry-run        # Preview without posting
    python dev_daily_report.py --day-offset 1   # Run for yesterday
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

JIRA_BASE_URL     = os.getenv("JIRA_BASE_URL")
JIRA_EMAIL        = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN    = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEYS = [k.strip() for k in os.getenv("JIRA_PROJECT_KEYS", "").split(",") if k.strip()]

SLACK_BOT_TOKEN   = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL     = os.getenv("SLACK_CHANNEL")

CONFLUENCE_BASE_URL      = os.getenv("CONFLUENCE_BASE_URL")
CONFLUENCE_EMAIL         = os.getenv("CONFLUENCE_EMAIL")
CONFLUENCE_API_TOKEN     = os.getenv("CONFLUENCE_API_TOKEN")
CONFLUENCE_SPACE_KEY     = os.getenv("CONFLUENCE_SPACE_KEY1")
CONFLUENCE_PARENT_PAGE_ID = os.getenv("CONFLUENCE_PARENT_PAGE_ID_DAILY") or os.getenv("CONFLUENCE_PARENT_PAGE_ID1")


# ─────────────────────────────────────────────
# DATE HELPERS
# ─────────────────────────────────────────────

def get_target_date(offset: int = 0) -> str:
    """Returns ISO date string for today minus offset days."""
    return (datetime.utcnow().date() - timedelta(days=offset)).isoformat()


# ─────────────────────────────────────────────
# JIRA
# ─────────────────────────────────────────────

PRIORITY_ORDER = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}

def fetch_resolved_today(target_date: str) -> list:
    """
    Fetches all tickets that moved to Done/Ready for release/Need to test
    on the target date, across all configured projects.
    """
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}

    project_filter = " OR ".join([f'project = "{k}"' for k in JIRA_PROJECT_KEYS])

    jql = (
        f'({project_filter}) '
        f'AND statusCategory = Done '
        f'AND updated >= "{target_date}" AND updated <= "{target_date}" '
        f'ORDER BY assignee ASC, priority ASC'
    )

    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": 200,
        "fields": "summary,priority,status,assignee,issuetype,project"
    }

    resp = requests.get(url, headers=headers, auth=auth, params=params)
    resp.raise_for_status()
    issues = resp.json().get("issues", [])

    tickets = []
    for issue in issues:
        f = issue["fields"]
        assignee = f.get("assignee") or {}
        priority = f.get("priority") or {}
        status   = f.get("status") or {}
        project  = f.get("project") or {}

        tickets.append({
            "key":      issue["key"],
            "summary":  f.get("summary", ""),
            "assignee": assignee.get("displayName", "Unassigned"),
            "priority": priority.get("name", "Unknown"),
            "status":   status.get("name", "Unknown"),
            "project":  project.get("key", ""),
        })

    return tickets


def group_by_assignee(tickets: list) -> dict:
    """Groups ticket list into {assignee: [tickets]} sorted by priority."""
    grouped = {}
    for t in tickets:
        name = t["assignee"]
        grouped.setdefault(name, []).append(t)

    # Sort each assignee's tickets by priority
    for name in grouped:
        grouped[name].sort(key=lambda x: PRIORITY_ORDER.get(x["priority"], 99))

    return grouped


# ─────────────────────────────────────────────
# CLAUDE
# ─────────────────────────────────────────────

def generate_summary(grouped: dict, target_date: str) -> str:
    """Uses Claude to generate the opening narrative summary paragraph."""
    client = anthropic.Anthropic()

    total = sum(len(v) for v in grouped.values())
    dev_count = len(grouped)

    prompt = f"""
You are writing the opening summary paragraph for a Dev Daily Update page in Confluence.

Date: {target_date}
Total tickets resolved: {total}
Developers who completed work: {dev_count}

Ticket data grouped by developer:
{json.dumps(grouped, indent=2)}

Write a single short paragraph (2-3 sentences) summarising what was accomplished today.
Mention the total ticket count, note any significant initiatives or themes (e.g. dashboard work, 
bug fixes, UI improvements), and highlight 1-2 key completions.

Be factual, concise, and professional. Do NOT use bullet points. Plain text only.
Start with: "Today the team completed..."
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text.strip()


# ─────────────────────────────────────────────
# CONFLUENCE HTML BUILDER
# ─────────────────────────────────────────────

PRIORITY_COLOURS = {
    "Highest": "#FF0000",
    "High":    "#FF6600",
    "Medium":  "#FFAA00",
    "Low":     "#00AA00",
    "Lowest":  "#888888",
}

def build_confluence_body(summary: str, grouped: dict, target_date: str, jira_base: str) -> str:
    """Builds Confluence storage-format HTML matching the screenshot layout."""

    formatted_date = datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y-%m-%d")
    safe_summary = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html = f"""
<p><em>Auto-generated by Claude on {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC</em></p>
<p>{safe_summary}</p>
"""

    for assignee, tickets in sorted(grouped.items()):
        html += f"<h2>{assignee}</h2>\n<ul>\n"
        for t in tickets:
            colour  = PRIORITY_COLOURS.get(t["priority"], "#888888")
            key     = t["key"]
            summary_text = t["summary"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            ticket_url   = f"{jira_base}/browse/{key}"

            html += (
                f'<li>'
                f'<strong><span style="color:{colour};">{t["priority"]} Priority:</span></strong> '
                f'<a href="{ticket_url}">{key}</a> - {summary_text}'
                f'</li>\n'
            )
        html += "</ul>\n"

    # All resolved tickets table
    all_tickets = [t for tickets in grouped.values() for t in tickets]
    html += f"""
<h2>All Resolved Tickets ({len(all_tickets)})</h2>
<table>
  <thead>
    <tr>
      <th>Key</th>
      <th>Summary</th>
      <th>Assignee</th>
      <th>Status</th>
    </tr>
  </thead>
  <tbody>
"""
    for t in all_tickets:
        key          = t["key"]
        ticket_url   = f"{jira_base}/browse/{key}"
        summary_text = t["summary"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html += (
            f"    <tr>"
            f'<td><a href="{ticket_url}">{key}</a></td>'
            f"<td>{summary_text}</td>"
            f"<td>{t['assignee']}</td>"
            f"<td>{t['status']}</td>"
            f"</tr>\n"
        )

    html += "  </tbody>\n</table>\n"
    return html


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

def build_page_title(target_date: str) -> str:
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    return f"Dev Daily Update – {dt.strftime('%Y-%m-%d')}"

def find_existing_page(title: str):
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    params = {
        "title":    title,
        "spaceKey": CONFLUENCE_SPACE_KEY,
        "expand":   "version"
    }
    resp = requests.get(url, headers=confluence_headers(), auth=confluence_auth(), params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None

def create_page(title: str, body_html: str):
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    payload = {
        "type":      "page",
        "title":     title,
        "space":     {"key": CONFLUENCE_SPACE_KEY},
        "ancestors": [{"id": str(CONFLUENCE_PARENT_PAGE_ID)}],
        "body": {
            "storage": {
                "value":          body_html,
                "representation": "storage"
            }
        }
    }
    resp = requests.post(url, headers=confluence_headers(), auth=confluence_auth(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

def update_page(page_id: str, title: str, body_html: str, current_version: int):
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}"
    payload = {
        "id":      str(page_id),
        "type":    "page",
        "title":   title,
        "version": {"number": current_version + 1},
        "body": {
            "storage": {
                "value":          body_html,
                "representation": "storage"
            }
        }
    }
    resp = requests.put(url, headers=confluence_headers(), auth=confluence_auth(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

def post_to_confluence(title: str, body_html: str) -> str | None:
    """Creates or updates the Confluence page. Returns the page URL."""
    required = [CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN,
                CONFLUENCE_SPACE_KEY, CONFLUENCE_PARENT_PAGE_ID]
    if not all(required):
        print("❌ Confluence config incomplete — check your .env")
        return None

    try:
        existing = find_existing_page(title)
        if existing:
            result = update_page(existing["id"], title, body_html, existing["version"]["number"])
            print(f"✅ Confluence page updated: {title}")
        else:
            result = create_page(title, body_html)
            print(f"✅ Confluence page created: {title}")

        page_id  = result.get("id", "")
        page_url = f"{CONFLUENCE_BASE_URL}/wiki/spaces/{CONFLUENCE_SPACE_KEY}/pages/{page_id}"
        return page_url

    except requests.HTTPError as e:
        print(f"❌ Confluence HTTP error: {e.response.text}")
        return None
    except Exception as e:
        print(f"❌ Confluence error: {e}")
        return None


# ─────────────────────────────────────────────
# SLACK
# ─────────────────────────────────────────────

def post_to_slack(target_date: str, total: int, page_url: str):
    """Posts a brief Slack notification linking to the Confluence page."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        print("⚠️  Slack not configured — skipping notification.")
        return

    text = (
        f"*📋 Dev Daily Update — {target_date}*\n"
        f"{total} ticket(s) resolved today. "
        f"View the full update: {page_url}"
    )

    url     = "https://slack.com/api/chat.postMessage"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}
    payload = {"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True}

    resp = requests.post(url, headers=headers, json=payload)
    data = resp.json()
    if data.get("ok"):
        print(f"✅ Slack notification posted to {SLACK_CHANNEL}")
    else:
        print(f"❌ Slack error: {data.get('error')}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dev Daily Update Bot")
    parser.add_argument("--dry-run",    action="store_true", help="Preview without posting")
    parser.add_argument("--day-offset", type=int, default=0, help="0 = today, 1 = yesterday, etc.")
    parser.add_argument("--no-slack",   action="store_true", help="Skip Slack notification")
    args = parser.parse_args()

    target_date = get_target_date(args.day_offset)
    print(f"\n📅 Generating Dev Daily Update for {target_date}\n")

    print("🔍 Fetching resolved tickets from Jira...")
    tickets = fetch_resolved_today(target_date)
    print(f"   Found {len(tickets)} ticket(s)")

    if not tickets:
        print("ℹ️  No resolved tickets today — skipping report.")
        return

    grouped = group_by_assignee(tickets)

    print("🤖 Generating summary with Claude...")
    summary = generate_summary(grouped, target_date)

    title    = build_page_title(target_date)
    body_html = build_confluence_body(summary, grouped, target_date, JIRA_BASE_URL)

    print(f"\n{'─'*60}")
    print(f"Title: {title}")
    print(f"\nSummary:\n{summary}")
    print(f"\nDevelopers: {', '.join(grouped.keys())}")
    print(f"Total tickets: {len(tickets)}")
    print(f"{'─'*60}\n")

    if args.dry_run:
        print("🧪 Dry run — nothing posted.")
        return

    print("📄 Posting to Confluence...")
    page_url = post_to_confluence(title, body_html)

    if page_url and not args.no_slack:
        print("📤 Sending Slack notification...")
        post_to_slack(target_date, len(tickets), page_url)


if __name__ == "__main__":
    main()