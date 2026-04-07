# 📋 QA Weekly Report Bot

Automatically fetches QA data from **Jira**, generates a report using **Claude**, and posts it to **Slack** — all from Claude Code (CLI).

---

## 🚀 Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp .env.example .env
# Edit .env with your actual API keys and URLs
```

### 3. Create a Slack Bot
1. Go to https://api.slack.com/apps → **Create New App**
2. Add the **`chat:write`** OAuth scope under *Bot Token Scopes*
3. Install the app to your workspace
4. Copy the **Bot User OAuth Token** (`xoxb-...`) into `.env`
5. Invite the bot to your channel: `/invite @YourBotName`

### 4. Get a Jira API Token
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Create a token and add it to `.env`

---

## 🛠 Usage

```bash
# Post this week's report to Slack
python qa_report.py

# Preview without posting (dry run)
python qa_report.py --dry-run

# Report for last week
python qa_report.py --week-offset 1
```

---

## ⏰ Automate Weekly (Cron)

Run every Friday at 4:00 PM:
```bash
crontab -e

# Add this line:
0 16 * * 5 cd /path/to/qa_report_bot && python qa_report.py
```

---

## 📁 File Structure

```
qa_report_bot/
├── qa_report.py       # Main script
├── requirements.txt   # Python dependencies
├── .env.example       # Config template
└── README.md
```

---

## 🔧 Customization

- **Change report sections**: Edit the `prompt` inside `generate_report()` in `qa_report.py`
- **Add more Jira filters**: Extend `fetch_jira_data()` with additional JQL queries
