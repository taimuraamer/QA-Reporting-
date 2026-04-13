"""
dev_daily_updates.py
────────────────────
Pulls Jira tickets resolved today across CC and A20 (or any configured
projects), generates a Claude narrative, then creates/updates a Confluence
page structured as:

  [Intro narrative]

  ── CC ──────────────────────────────────
  ### Person A
    • [CC badge] High Priority: CC-123 - description
  ### Person B
    • ...

  ── A20 ─────────────────────────────────
  ### Person A
    • [A20 badge] ...

  All Resolved Tickets (N)  ← table with Project column

Secrets (GitHub Actions / local .env):
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEYS
  SLACK_BOT_TOKEN, SLACK_CHANNEL
  CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
  CONFLUENCE_SPACE_KEY1, CONFLUENCE_PARENT_PAGE_ID_DAILY
  ANTHROPIC_API_KEY
"""

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone

import anthropic
import requests

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
JIRA_BASE_URL     = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL        = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN    = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT_KEYS = [k.strip() for k in os.environ.get("JIRA_PROJECT_KEYS", "CC").split(",")]

SLACK_BOT_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL     = os.environ.get("SLACK_CHANNEL", "")

CONFLUENCE_BASE_URL       = os.environ["CONFLUENCE_BASE_URL"].rstrip("/")
CONFLUENCE_EMAIL          = os.environ["CONFLUENCE_EMAIL"]
CONFLUENCE_API_TOKEN      = os.environ["CONFLUENCE_API_TOKEN"]
CONFLUENCE_SPACE_KEY      = os.environ["CONFLUENCE_SPACE_KEY1"]
CONFLUENCE_PARENT_PAGE_ID = os.environ["CONFLUENCE_PARENT_PAGE_ID_DAILY"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Priority sort order
PRIORITY_ORDER = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}

# Project badge colours (background, text) — extend if you add more projects
PROJECT_BADGE_STYLES: dict[str, tuple[str, str]] = {
    "CC":  ("#0065FF", "#FFFFFF"),   # blue
    "A20": ("#6554C0", "#FFFFFF"),   # purple
}
DEFAULT_BADGE_STYLE = ("#42526E", "#FFFFFF")   # dark grey fallback


# ─── Jira ─────────────────────────────────────────────────────────────────────

def jira_auth() -> tuple[str, str]:
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def fetch_todays_resolved_issues() -> list[dict]:
    """Return all issues with statusCategory=Done, updated today, across all projects."""
    project_clause = ", ".join(f'"{k}"' for k in JIRA_PROJECT_KEYS)
    jql = (
        f"project in ({project_clause}) "
        f"AND statusCategory = Done "
        f"AND updated >= startOfDay() "
        f"ORDER BY project ASC, priority ASC, updated DESC"
    )

    url = f"{JIRA_BASE_URL}/rest/api/3/search"
    payload = {
        "jql":        jql,
        "maxResults": 200,
        "fields":     ["summary", "status", "priority", "assignee",
                       "project", "issuetype", "resolutiondate", "updated"],
    }

    log.info("Jira JQL: %s", jql)
    resp = requests.post(
        url, json=payload, auth=jira_auth(),
        headers={"Content-Type": "application/json"}, timeout=30
    )
    resp.raise_for_status()

    issues = resp.json().get("issues", [])
    log.info("Fetched %d resolved issue(s) today.", len(issues))
    return issues


def parse_issues(issues: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """
    Returns:
        {
          "CC":  { "Person A": [ticket, ...], "Person B": [...] },
          "A20": { "Person A": [...] },
          ...
        }
    Only projects that actually have tickets are included.
    Order of projects follows JIRA_PROJECT_KEYS config.
    """
    # Initialise with configured project order (empty dicts)
    by_project: dict[str, dict[str, list[dict]]] = {k: {} for k in JIRA_PROJECT_KEYS}

    for issue in issues:
        fields   = issue["fields"]
        proj_key = (fields.get("project") or {}).get("key", "UNKNOWN")
        assignee = fields.get("assignee") or {}
        person   = assignee.get("displayName", "Unassigned")
        priority = (fields.get("priority") or {}).get("name", "Medium")
        key      = issue["key"]
        summary  = fields.get("summary", "")
        status   = (fields.get("status") or {}).get("name", "")
        url      = f"{JIRA_BASE_URL}/browse/{key}"

        # Handle projects not in the config (shouldn't happen, but be safe)
        if proj_key not in by_project:
            by_project[proj_key] = {}

        by_project[proj_key].setdefault(person, []).append({
            "key":      key,
            "summary":  summary,
            "priority": priority,
            "status":   status,
            "url":      url,
            "assignee": person,
            "project":  proj_key,
        })

    # Sort each person's tickets by priority; remove empty projects
    result: dict[str, dict[str, list[dict]]] = {}
    for proj_key in JIRA_PROJECT_KEYS:
        people = by_project.get(proj_key, {})
        if not people:
            continue
        result[proj_key] = {
            person: sorted(tickets, key=lambda t: PRIORITY_ORDER.get(t["priority"], 99))
            for person, tickets in people.items()
        }

    return result


# ─── Claude enrichment ────────────────────────────────────────────────────────

def enrich_ticket_descriptions(
    by_project: dict[str, dict[str, list[dict]]]
) -> dict[str, dict[str, list[dict]]]:
    """
    Ask Claude to write a 1-2 sentence human-readable description for every ticket.
    Adds a "description" key to each ticket dict.
    """
    all_tickets = [
        t
        for people in by_project.values()
        for tickets in people.values()
        for t in tickets
    ]
    if not all_tickets:
        return by_project

    tickets_json = json.dumps(
        [
            {
                "key":      t["key"],
                "summary":  t["summary"],
                "priority": t["priority"],
                "project":  t["project"],
            }
            for t in all_tickets
        ],
        indent=2,
    )

    prompt = f"""For each Jira ticket below, write a single short description (1-2 sentences)
suitable for a developer daily update report. Clearly explain what was done or achieved.
Be specific and use plain English. Do not restate the ticket key, project, or priority.

Tickets:
{tickets_json}

Respond ONLY with a valid JSON array:
[
  {{"key": "CC-123", "description": "Description here."}},
  ...
]"""

    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()

    descriptions: dict[str, str] = {}
    try:
        for item in json.loads(raw):
            descriptions[item["key"]] = item["description"]
    except Exception as exc:
        log.warning("Could not parse Claude ticket descriptions: %s", exc)

    # Write descriptions back into the nested structure
    for people in by_project.values():
        for tickets in people.values():
            for t in tickets:
                t["description"] = descriptions.get(t["key"], t["summary"])

    return by_project


def generate_narrative(
    by_project: dict[str, dict[str, list[dict]]],
    report_date: str,
) -> str:
    """Generate the introductory paragraph for the page."""
    lines: list[str] = []
    total = 0
    for proj, people in by_project.items():
        lines.append(f"\nProject {proj}:")
        for person, tickets in people.items():
            lines.append(f"  {person}:")
            for t in tickets:
                lines.append(f"    [{t['priority']}] {t['key']} - {t['summary']} ({t['status']})")
                total += 1

    projects_str = " and ".join(by_project.keys())

    prompt = f"""You are writing the intro paragraph for a Confluence Dev Daily Update page dated {report_date}.

The team resolved {total} Jira tickets today across projects: {projects_str}.

Breakdown:
{"".join(lines)}

Write ONE concise paragraph (3-5 sentences) that:
- Mentions both projects if both have tickets
- States the total tickets completed and any notable themes or initiatives
- Highlights the most significant work (highest-priority items)
- Reads naturally as a daily standup summary
- Plain prose only — no bullet points, no heading

Respond with only the paragraph text."""

    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ─── Confluence page HTML builder ─────────────────────────────────────────────

def priority_color(priority: str) -> str:
    return {
        "Highest": "#FF0000",
        "High":    "#FF8B00",
        "Medium":  "#0065FF",
        "Low":     "#00875A",
        "Lowest":  "#6B778C",
    }.get(priority, "#0065FF")


def project_badge_html(proj_key: str) -> str:
    """Render a small coloured inline badge for the project key."""
    bg, fg = PROJECT_BADGE_STYLES.get(proj_key, DEFAULT_BADGE_STYLE)
    return (
        f'<span style="background:{bg};color:{fg};border-radius:3px;'
        f'padding:1px 6px;font-size:11px;font-weight:bold;'
        f'margin-right:4px;">{proj_key}</span>'
    )


def build_confluence_storage(
    narrative: str,
    by_project: dict[str, dict[str, list[dict]]],
    generated_at: str,
) -> str:
    """Build Confluence Storage Format (XHTML)."""

    all_tickets = [
        t
        for people in by_project.values()
        for tickets in people.values()
        for t in tickets
    ]
    total = len(all_tickets)

    # ── Page header ──
    html = (
        f"<p><em>Auto-generated by Claude on {generated_at}</em></p>\n"
        f"<p>{narrative}</p>\n"
    )

    # ── Per-project sections ──
    for proj_key, people in by_project.items():
        proj_total = sum(len(t) for t in people.values())
        badge      = project_badge_html(proj_key)

        html += (
            f"<h1>{badge} {proj_key} "
            f"&nbsp;<small>({proj_total} ticket{'s' if proj_total != 1 else ''})</small></h1>\n"
        )

        # Per-person subsections within this project
        for person, tickets in sorted(people.items()):
            html += f"<h2>{person}</h2>\n<ul>\n"
            for t in tickets:
                p_color = priority_color(t["priority"])
                desc    = t.get("description", t["summary"])
                html += (
                    f"<li>"
                    f'<strong><span style="color:{p_color};">{t["priority"]} Priority:</span></strong> '
                    f"{project_badge_html(proj_key)}"
                    f'<a href="{t["url"]}">{t["key"]}</a> – {desc}'
                    f"</li>\n"
                )
            html += "</ul>\n"

    # ── All resolved tickets summary table ──
    html += f"<h2>All Resolved Tickets ({total})</h2>\n"
    html += (
        "<table>"
        "<colgroup>"
        '<col style="width:80px"/>'
        '<col style="width:110px"/>'
        "<col/>"
        '<col style="width:150px"/>'
        '<col style="width:150px"/>'
        "</colgroup>"
        "<tbody>"
        "<tr>"
        "<th><strong>Project</strong></th>"
        "<th><strong>Key</strong></th>"
        "<th><strong>Summary</strong></th>"
        "<th><strong>Assignee</strong></th>"
        "<th><strong>Status</strong></th>"
        "</tr>\n"
    )

    for t in all_tickets:
        html += (
            f"<tr>"
            f"<td>{project_badge_html(t['project'])}</td>"
            f'<td><a href="{t["url"]}">{t["key"]}</a></td>'
            f"<td>{t['summary']}</td>"
            f"<td>{t['assignee']}</td>"
            f"<td>{t['status']}</td>"
            f"</tr>\n"
        )

    html += "</tbody></table>\n"
    return html


# ─── Confluence API ───────────────────────────────────────────────────────────

def _confluence_headers() -> dict:
    token = base64.b64encode(
        f"{CONFLUENCE_EMAIL}:{CONFLUENCE_API_TOKEN}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def page_exists(title: str) -> tuple[bool, str | None]:
    url    = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    params = {"spaceKey": CONFLUENCE_SPACE_KEY, "title": title, "expand": "version"}
    resp   = requests.get(url, headers=_confluence_headers(), params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return (True, results[0]["id"]) if results else (False, None)


def create_confluence_page(title: str, body: str) -> str:
    url     = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    payload = {
        "type":      "page",
        "title":     title,
        "space":     {"key": CONFLUENCE_SPACE_KEY},
        "ancestors": [{"id": CONFLUENCE_PARENT_PAGE_ID}],
        "body":      {"storage": {"value": body, "representation": "storage"}},
    }
    resp     = requests.post(url, headers=_confluence_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    page_id  = resp.json()["id"]
    page_url = f"{CONFLUENCE_BASE_URL}/wiki/spaces/{CONFLUENCE_SPACE_KEY}/pages/{page_id}"
    log.info("Created Confluence page: %s", page_url)
    return page_url


def update_confluence_page(page_id: str, title: str, body: str) -> str:
    url  = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}?expand=version"
    resp = requests.get(url, headers=_confluence_headers(), timeout=30)
    resp.raise_for_status()
    version = resp.json()["version"]["number"]

    url     = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}"
    payload = {
        "type":    "page",
        "title":   title,
        "version": {"number": version + 1},
        "body":    {"storage": {"value": body, "representation": "storage"}},
    }
    resp     = requests.put(url, headers=_confluence_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    page_url = f"{CONFLUENCE_BASE_URL}/wiki/spaces/{CONFLUENCE_SPACE_KEY}/pages/{page_id}"
    log.info("Updated Confluence page: %s", page_url)
    return page_url


# ─── Slack ────────────────────────────────────────────────────────────────────

def post_slack_notification(
    page_url: str,
    report_date: str,
    by_project: dict[str, dict[str, list[dict]]],
) -> None:
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        log.info("Slack not configured — skipping.")
        return

    lines: list[str] = [f":spiral_note_pad: *Dev Daily Update — {report_date}*"]
    total = 0
    for proj, people in by_project.items():
        proj_total = sum(len(t) for t in people.values())
        total     += proj_total
        people_str = ", ".join(sorted(people.keys()))
        lines.append(
            f"  *{proj}*: {proj_total} ticket{'s' if proj_total != 1 else ''} "
            f"by {people_str}"
        )

    lines.append(f"\n*{total} total tickets resolved today*")
    lines.append(f"<{page_url}|View full report on Confluence>")

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type":  "application/json",
        },
        json={"channel": SLACK_CHANNEL, "text": "\n".join(lines)},
        timeout=15,
    )
    data = resp.json()
    if data.get("ok"):
        log.info("Slack notification sent to %s", SLACK_CHANNEL)
    else:
        log.warning("Slack error: %s", data.get("error"))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    now_utc      = datetime.now(timezone.utc)
    generated_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    report_date  = now_utc.strftime("%Y-%m-%d")
    page_title   = f"Dev Daily Update – {report_date}"

    log.info("=== %s ===", page_title)

    # 1. Fetch all resolved tickets from Jira
    issues = fetch_todays_resolved_issues()
    if not issues:
        log.warning("No resolved issues found today — skipping page creation.")
        return

    # 2. Parse into {project: {person: [tickets]}}
    by_project = parse_issues(issues)

    # 3. Enrich descriptions via Claude
    log.info("Enriching ticket descriptions via Claude...")
    by_project = enrich_ticket_descriptions(by_project)

    # 4. Generate narrative intro via Claude
    log.info("Generating narrative via Claude...")
    narrative = generate_narrative(by_project, report_date)
    log.info("Narrative: %s", narrative)

    # 5. Build Confluence storage format body
    body = build_confluence_storage(narrative, by_project, generated_at)

    # 6. Publish to Confluence (create or update)
    exists, page_id = page_exists(page_title)
    if exists and page_id:
        log.info("Page already exists (%s) — updating.", page_id)
        page_url = update_confluence_page(page_id, page_title, body)
    else:
        page_url = create_confluence_page(page_title, body)

    # 7. Slack notification
    post_slack_notification(page_url, report_date, by_project)

    log.info("Done → %s", page_url)


if __name__ == "__main__":
    main()