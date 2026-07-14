from dotenv import load_dotenv
load_dotenv()
from jira_tool import JiraExecutor, JiraAction

e = JiraExecutor()
r = e(JiraAction(command="get_ticket", jql_filter='labels = "Agent-ready"'))
print("=== RESULT ===")
print(r.text)
print("=== ERROR ===", r.is_error)
print("=== COMMAND ===", r.command)
