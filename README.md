# Change Request Scoring Agent

A Flask web application that scores new change requests and product suggestions against 1,000 historical Jira tickets using Claude AI. Built for the Experlogix product management team.

---

## What it does

Paste a description of a new customer request into the app. The agent will:

- **Score it 0–100** based on how similar requests have been handled historically
- **Explain the score** in 1–2 sentences, referencing patterns in the Jira history
- **Cite up to 3 similar past tickets** with their status and relevance
- **Recommend a type** — Change Request (chargeable, customer-specific) or Product Suggestion (general roadmap)

Scores are influenced by your configured **Warnings** and **OKs** — persistent policy rules you define in the sidebar that the agent applies with high weight.

---

## Project structure

```
change-scoring/
├── app.py                        # Flask backend — all routes, auth, scoring, DB
├── run.sh                        # Startup script
├── requirements.txt
├── .env                          # API keys and secret (never committed)
├── .env.example                  # Template for .env
├── data/
│   ├── JIRA-PM-Changes-Features.xml   # Jira export (1,000 tickets)
│   ├── rules.json                     # Persisted Warnings & OKs
│   └── users.db                       # SQLite user database (never committed)
├── templates/
│   ├── index.html                # Main scoring UI
│   ├── login.html                # Sign-in page
│   ├── setup.html                # First-run admin setup
│   ├── forgot_password.html      # Password reset request
│   ├── reset_password.html       # Password reset form
│   └── admin.html                # User management panel
└── static/
    ├── style.css
    ├── script.js                 # Scoring UI logic
    └── admin.js                  # Admin panel logic
```

---

## Getting started

### 1. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```
ANTHROPIC_API_KEY=sk-ant-...
SECRET_KEY=<any long random string>
```

### 3. Start the server

```bash
./run.sh
# or
python3 app.py
```

### 4. First-run setup

On first launch, no users exist. Visit:

```
http://localhost:5001/setup
```

Create the first **admin account** here. This route disables itself permanently once any user exists.

---

## Scoring

The agent uses a two-step process:

1. **Keyword pre-filter** — all 1,000 Jira tickets are scored by word overlap with the new request; the top 20 most relevant are selected
2. **Claude scoring** — those 20 tickets plus your active Warnings/OKs rules are sent to Claude, which returns a score, explanation, similar tickets, and a type recommendation

### Score bands

| Score | Meaning |
|---|---|
| 0–20 | Similar requests strongly rejected, or matches a Warning rule |
| 21–40 | Mostly rejected / unlikely to be entertained |
| 41–60 | Mixed history |
| 61–80 | Generally accepted / similar to DONE or active tickets |
| 81–100 | Highly suitable — strong precedent and/or matches OK rules |

### Jira ticket statuses

| Status | Signal |
|---|---|
| DONE | Strong positive |
| Rejected | Strong negative |
| Gut Feel / Discovery / Solution Design / In Development / Awaiting Approval | Positive |
| To Do / ROADMAP/PLANNING | Neutral/positive |

---

## User authentication

All routes are protected by login. Sessions last 8 hours.

### Routes

| Route | Purpose |
|---|---|
| `/setup` | First-run only; disabled once any user exists |
| `/login` | Email + password sign-in |
| `/logout` | Clears session |
| `/forgot-password` | Generates a 1-hour, single-use reset link shown on screen |
| `/reset-password/<token>` | User self-service password reset |
| `/admin` | Admin-only user management panel |

### Password policy

All passwords must meet:
- Minimum 10 characters
- At least one uppercase letter
- At least one lowercase letter
- At least one number
- At least one special character (e.g. `!@#$%`)

Passwords are hashed with **bcrypt at cost factor 12**.

### Security properties

- Reset tokens are stored as SHA-256 hashes — the raw token only ever exists in the reset URL, never in the database
- Reset tokens are single-use and expire after 1 hour
- Suspended users are blocked at login
- The forgot-password page never reveals whether an email address is registered
- `users.db` is excluded from version control

---

## Admin panel (`/admin`)

Accessible to admin-role users only. Provides:

| Action | Description |
|---|---|
| **Add user** | Provision a new account with a temporary password |
| **Suspend** | Block a user from logging in |
| **Activate** | Re-enable a suspended user |
| **Reset password** | Admin sets a new password directly |
| **Reset link** | Generate a 1-hour self-service reset link to share with the user |
| **Remove** | Permanently delete a user account |

> An admin cannot suspend, reset, or delete their own account.

---

## Policy rules (Warnings & OKs)

Defined in the sidebar of the main app and saved to `data/rules.json`.

- **Warnings** — requests matching these patterns score lower (e.g. *"Changes that alter core platform behaviour for all customers"*)
- **OKs** — requests matching these patterns are generally acceptable (e.g. *"REST API migrations from SOAP are standard and accepted"*)

Rules persist across server restarts and are applied by Claude with high weight during scoring.
