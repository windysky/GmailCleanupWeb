# [DEPRECATED] Gmail Cleanup Web App

A Flask-based web application for cleaning up your Gmail inbox. It connects to the Gmail API, groups unread emails by sender, and lets you review and batch-move unwanted messages to Trash through a simple browser interface.

**Features:**
- Groups unread inbox emails by sender (showing senders with 2+ messages)
- Bootstrap 5 UI with sender selection checkboxes and subject previews
- Persistent blocklist that remembers your selections across sessions
- Multi-step workflow: review senders, confirm selection, move to Trash
- OAuth 2.0 authentication with Google
- Messages are moved to Trash (not permanently deleted)

> **This project has been deprecated.** All functionality has been consolidated into the unified [GmailCleanup](https://github.com/windysky/GmailCleanup) repository, which now includes both CLI and Web interfaces in a single Python package.

## Migration

Use the consolidated repository instead:

```bash
git clone https://github.com/windysky/GmailCleanup.git
cd GmailCleanup
pip install -r requirements.txt

# Start the web UI (same functionality as this project)
python -m gmail_cleanup web

# Or use the interactive CLI
python -m gmail_cleanup cli
```

## Archive

This repository is no longer maintained. Please use [GmailCleanup](https://github.com/windysky/GmailCleanup) for all future development.
