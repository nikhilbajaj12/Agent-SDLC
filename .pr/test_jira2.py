from dotenv import load_dotenv
load_dotenv()
from jira_tool import JiraExecutor, JiraAction

e = JiraExecutor()

# Test add_comment
r1 = e(JiraAction(command="add_comment", ticket_key="KAN-1", comment_text="Agent picked up this ticket and is working on it."))
print("ADD COMMENT:", r1.text, "| ERROR:", r1.is_error)

# Test update_status to "In Progress"
r2 = e(JiraAction(command="update_status", ticket_key="KAN-1", target_status="In Progress"))
print("UPDATE STATUS:", r2.text, "| ERROR:", r2.is_error)
