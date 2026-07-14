import os
from dotenv import load_dotenv

from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool

load_dotenv()

AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

llm = LLM(
    model=f"azure/{AZURE_DEPLOYMENT}",
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
)

agent = Agent(
    llm=llm,
    tools=[
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ],
)

cwd = os.getcwd()
conversation = Conversation(agent=agent, workspace=cwd)

conversation.send_message("Clone https://github.com/nikhilbajaj12/skill_set1.git, add 'pydantic' and 'langchain' to requirements.txt, commit with message 'Add pydantic and langchain to requirements.txt', and push. The GITHUB_TOKEN env var has the PAT.")
conversation.run()
print("All done!")
