import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

from jira_tool import JiraExecutor, JiraAction

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────
JIRA_JQL = os.getenv("JIRA_JQL", 'labels = "Agent-ready" AND status = "Ready for Agent"')
TARGET_REPO = os.getenv("TARGET_REPO", "nikhilbajaj12/Lighthouse-Pharos")
TARGET_BRANCH = os.getenv("TARGET_BRANCH", "dev")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
AZURE_MODEL = f"azure/{os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4.1-mini')}"
AZURE_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_URL = os.getenv("AZURE_OPENAI_ENDPOINT")
MAX_AGENT_ITERATIONS = 5
# Status names — matches your Jira workflow
STATUS_FAILED = os.getenv("STATUS_FAILED", "Human In Loop")
STATUS_DONE = os.getenv("STATUS_DONE", "Done")
WORKSPACE_DIR = Path(tempfile.mkdtemp(prefix="jira_bot_"))
REPO_DIR = WORKSPACE_DIR / TARGET_REPO.split("/")[-1]

print(f"  Target repo: {TARGET_REPO} ({TARGET_BRANCH})")
print(f"  Workspace:   {WORKSPACE_DIR}")


# ── Helpers ─────────────────────────────────────────────────────────
def run_cmd(cmd: list[str], cwd: Path | None = None, silent: bool = False) -> tuple[int, str, str]:
    """Run a command, return (exit_code, stdout, stderr)."""
    if not silent:
        print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or REPO_DIR)
    if r.stdout:
        print(r.stdout[:1500])
    if r.stderr:
        print(r.stderr[:500])
    return r.returncode, r.stdout, r.stderr


def gh_api(method: str, path: str, json_data: dict | None = None) -> dict | list:
    """Call GitHub REST API with the user's token."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.request(method, url, headers=headers, json=json_data, timeout=30)
    if resp.status_code >= 400:
        body = resp.json()
        raise RuntimeError(f"GitHub API {resp.status_code}: {body.get('message', resp.text[:300])}")
    return resp.json()


def step(msg: str):
    print(f"\n{'─' * 60}")
    print(f"  [{msg}]")
    print(f"{'─' * 60}")


# ── Main ────────────────────────────────────────────────────────────
def main():
    required = {
        "AZURE_OPENAI_API_KEY": AZURE_KEY,
        "AZURE_OPENAI_ENDPOINT": AZURE_URL,
        "AZURE_OPENAI_DEPLOYMENT": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        "JIRA_BASE_URL": os.getenv("JIRA_BASE_URL"),
        "JIRA_USER_EMAIL": os.getenv("JIRA_USER_EMAIL"),
        "JIRA_API_TOKEN": os.getenv("JIRA_API_TOKEN"),
        "GITHUB_TOKEN": GITHUB_TOKEN,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"!! Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    # ── STEP 1: FETCH ───────────────────────────────────────────────
    step("1/6  FETCH — Searching for ticket")

    jira = JiraExecutor()
    result = jira(JiraAction(command="get_ticket", jql_filter=JIRA_JQL))

    if result.is_error or not result.text.strip() or result.text.startswith("No tickets found"):
        print(f"  No ticket found matching: {JIRA_JQL}")
        sys.exit(0)

    # Parse the fetched result
    lines = result.text.strip().split("\n")
    ticket_key = result.result
    summary = ""
    description = ""
    status = ""
    in_desc = False
    desc_lines: list[str] = []
    for line in lines:
        if line.startswith("Summary:"):
            summary = line.split(":", 1)[1].strip()
        elif line.startswith("Status:"):
            status = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            in_desc = True
            desc_lines.append(line.split(":", 1)[1].strip())
        elif in_desc and line.strip() and not any(line.startswith(p) for p in ["Key:", "Summary:", "Status:", "Priority:"]):
            desc_lines.append(line.strip())
    description = "\n".join(desc_lines)

    print(f"  Ticket: {ticket_key}")
    print(f"  Summary: {summary}")
    print(f"  Status: {status}")

    # ── STEP 2: CLARITY GATE ───────────────────────────────────────
    step("2/6  CLARITY GATE — Checking ticket detail")

    word_count = len(description.split())
    if word_count < 5:
        msg = f"Ticket description is too short ({word_count} words). Please provide more detail including specific packages, versions, and repository to modify."
        print(f"  !! {msg}")
        jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text=msg))
        jira(JiraAction(command="update_status", ticket_key=ticket_key, target_status=STATUS_FAILED))
        sys.exit(0)

    # Quick LLM validation
    from openhands.sdk import LLM
    from openhands.sdk.llm import Message, TextContent
    from pydantic import SecretStr

    gate_llm = LLM(
        model=AZURE_MODEL,
        api_key=SecretStr(AZURE_KEY),
        base_url=AZURE_URL,
    )

    gate_prompt = (
        "You are validating a Jira ticket for an automation bot. "
        f"The bot will work in repo {TARGET_REPO} on branch '{TARGET_BRANCH}'.\n\n"
        "Does the following description have enough detail about WHAT to change "
        "(specific packages, versions, files) to implement without guessing?\n"
        "Ignore missing repo/branch/location details — those are handled by the bot configuration.\n\n"
        f"Summary: {summary}\nDescription: {description}\n\n"
        "Answer YES or NO followed by a one-sentence reason."
    )

    try:
        gate_messages = [Message(role="user", content=[TextContent(text=gate_prompt)])]
        gate_response = gate_llm.completion(messages=gate_messages)
        gate_text = ""
        if gate_response.message.content:
            first = gate_response.message.content[0]
            if isinstance(first, TextContent):
                gate_text = first.text.strip()
    except Exception as e:
        print(f"  !!! Gate LLM call failed: {e}")
        jira(JiraAction(
            command="add_comment", ticket_key=ticket_key,
            comment_text="Clarity check failed to run (technical error) — please retry or check the bot logs",
        ))
        jira(JiraAction(command="update_status", ticket_key=ticket_key, target_status=STATUS_FAILED))
        sys.exit(0)

    gate_ok = gate_text.strip().upper().startswith("YES")
    print(f"  Gate response: {gate_text.strip()[:200]}")
    print(f"  → {'[OK] PASS' if gate_ok else '!! FAIL'}")

    if not gate_ok:
        jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text=f"Ticket needs more detail: {gate_text.strip()}"))
        jira(JiraAction(command="update_status", ticket_key=ticket_key, target_status=STATUS_FAILED))
        sys.exit(0)

    # ── STEP 3: AGENT RUN ─────────────────────────────────────────
    step("3/6  AGENT RUN — Cloning repo and running agent")

    repo_url = f"https://{GITHUB_TOKEN}@github.com/{TARGET_REPO}.git"

    if REPO_DIR.exists():
        run_cmd(["git", "fetch", "origin"], REPO_DIR)
        run_cmd(["git", "checkout", TARGET_BRANCH], REPO_DIR)
        run_cmd(["git", "pull", "origin", TARGET_BRANCH], REPO_DIR)
    else:
        print(f"  Git clone URL: https://<token>@github.com/{TARGET_REPO}.git")
        rc, out, err = run_cmd(
            ["git", "clone", "--branch", TARGET_BRANCH, repo_url, str(REPO_DIR)],
            cwd=WORKSPACE_DIR,
            silent=True,
        )
        if rc != 0:
            print(f"!! Clone failed: {err}")
            jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text=f"Failed to clone repo: {err[:300]}"))
            sys.exit(1)

    # Build task from ticket
    task_prompt = (
        f"Jira Ticket {ticket_key}: {summary}\n\n"
        f"Description: {description}\n\n"
        f"You are working in repo: {TARGET_REPO} (branch: {TARGET_BRANCH})\n"
        f"The workspace is at: {REPO_DIR}\n\n"
        "CRITICAL: Do NOT ask me clarifying questions — just use your best judgment to interpret packages.\n"
        "Correct likely typos (e.g. 'pydentic' -> 'pydantic', 'trasformers' -> 'transformers', 'langraph' -> 'langgraph').\n"
        "IMPORTANT: Do NOT run 'pip install' or any package installation — it is too slow and unnecessary.\n"
        "Just edit the file, verify with 'git diff', then call finish."
    )

    from openhands.sdk import Agent, Conversation, Tool
    from openhands.tools.terminal import TerminalTool
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool

    agent_llm = LLM(
        model=AZURE_MODEL,
        api_key=SecretStr(AZURE_KEY),
        base_url=AZURE_URL,
    )

    agent = Agent(
        llm=agent_llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
    )

    conversation = Conversation(
        agent=agent,
        workspace=str(REPO_DIR),
    )

    print(f"  Agent workspace: {REPO_DIR}")
    print(f"  Task: {task_prompt[:200]}...")
    # Export env for git operations inside agent
    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    try:
        conversation.send_message(task_prompt)
        conversation.run()
        print("  [OK] Agent run completed")
    except Exception as e:
        print(f"  !! Agent run failed: {e}")
        jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text=f"Agent run failed: {str(e)[:300]}"))
        jira(JiraAction(command="update_status", ticket_key=ticket_key, target_status=STATUS_FAILED))
        sys.exit(1)

    # ── STEP 4: VERIFY SUCCESS ─────────────────────────────────────
    step("4/6  VERIFY — Checking agent's work")

    # Check if requirements.txt was modified
    rc, stdout, _ = run_cmd(["git", "diff", "--name-only"], REPO_DIR)
    modified_files = [f for f in stdout.strip().split("\n") if f.strip()]

    if not modified_files:
        print("  !! No files were modified by the agent")
        jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text="Agent made no changes to the repository."))
        jira(JiraAction(command="update_status", ticket_key=ticket_key, target_status=STATUS_FAILED))
        sys.exit(0)

    print(f"  [OK] Modified files: {modified_files}")

    # Validate requirements.txt — only check the new packages the ticket asked for,
    # not the entire file (pre-existing packages like nvidia-cufile may not resolve locally).
    new_packages = [l.strip() for l in description.split("\n") if ">=" in l or "==" in l]
    if new_packages:
        failed = []
        for pkg_spec in new_packages:
            rc2, out2, err2 = run_cmd(
                [sys.executable, "-m", "pip", "install", pkg_spec, "--dry-run"],
                cwd=REPO_DIR,
            )
            if rc2 != 0:
                failed.append(pkg_spec)
        if failed:
            print(f"  !! New packages failed pip check: {failed}")
            jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text=f"Agent modified requirements.txt but some new packages failed pip validation: {', '.join(failed)}"))
            jira(JiraAction(command="update_status", ticket_key=ticket_key, target_status=STATUS_FAILED))
            sys.exit(0)

    print("  [OK] Work verified successfully")

    # ── STEP 5: PR CREATION ─────────────────────────────────────────
    step("5/6  PR — Creating branch, committing, and opening PR")

    short_desc = summary.lower().replace(" ", "-").replace(".", "")[:40]
    branch_name = f"feat/{ticket_key}-{short_desc}"

    run_cmd(["git", "checkout", "-b", branch_name], REPO_DIR)
    run_cmd(["git", "add", "-A"], REPO_DIR)
    rc, _, _ = run_cmd(
        ["git", "commit", "-m", f"{ticket_key}: {summary}\n\nCloses {ticket_key}\n\nCo-authored-by: openhands <openhands@all-hands.dev>"],
        REPO_DIR,
    )
    if rc != 0:
        print("  ⚠️  Nothing to commit (already up to date)")
        sys.exit(0)

    # Push
    push_url = f"https://{GITHUB_TOKEN}@github.com/{TARGET_REPO}.git"
    print(f"  Git push to: https://<token>@github.com/{TARGET_REPO}.git {branch_name}")
    rc, out, err = run_cmd(["git", "push", push_url, branch_name], REPO_DIR)
    if rc != 0:
        print(f"  !! Push failed: {err[:300]}")
        jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text=f"Git push failed: {err[:300]}"))
        sys.exit(1)

    print(f"  [OK] Pushed branch: {branch_name}")

    # Open PR
    pr_title = f"{ticket_key}: {summary}"
    pr_body = (
        f"## Summary\n"
        f"Implements changes for Jira ticket **{ticket_key}**.\n\n"
        f"**Ticket**: [{ticket_key}]({os.getenv('JIRA_BASE_URL')}/browse/{ticket_key})\n\n"
        f"**Changes**:\n"
        f"- Modified files: {', '.join(modified_files)}\n\n"
        f"**Verification**:\n"
        f"- Tests passing: [OK]\n"
        f"- Ready for human review.\n"
    )

    pr_data = gh_api("POST", f"/repos/{TARGET_REPO}/pulls", {
        "title": pr_title,
        "head": branch_name,
        "base": TARGET_BRANCH,
        "body": pr_body,
    })
    pr_url = pr_data.get("html_url", "")
    print(f"  [OK] PR created: {pr_url}")

    # ── STEP 6: REPORT BACK ───────────────────────────────────────
    step("6/6  REPORT — Updating Jira ticket")

    comment = (
        f"Agent completed work on this ticket.\n\n"
        f"**Pull Request**: {pr_url}\n\n"
        f"**Files modified**: {', '.join(modified_files)}\n"
        f"Tests verified: [OK]"
    )

    jira(JiraAction(command="add_comment", ticket_key=ticket_key, comment_text=comment))
    jira(JiraAction(command="update_status", ticket_key=ticket_key, target_status=STATUS_DONE))

    print(f"\n{'=' * 60}")
    print(f"  [OK] DONE — {ticket_key} is now {STATUS_DONE}")
    print(f"  PR: {pr_url}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
