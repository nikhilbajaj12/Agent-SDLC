from dotenv import load_dotenv
load_dotenv()
from jira import JIRA
import os

j = JIRA(server=os.environ["JIRA_BASE_URL"], basic_auth=(os.environ["JIRA_USER_EMAIL"], os.environ["JIRA_API_TOKEN"]))
trans = j.transitions("KAN-1")
for t in trans:
    print(f"  {t['id']}: {t['name']}")
