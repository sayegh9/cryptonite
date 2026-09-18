with open("/home/samx/.gemini/antigravity/brain/c38f091f-452b-4049-be3d-5d7cb0d119df/task.md", "r") as f:
    text = f.read()

text = text.replace("[ ] Install `openai` package", "[x] Install `openai` package")
text = text.replace("[ ] Create the `OpportunityHunter` agent", "[x] Create the `OpportunityHunter` agent")
text = text.replace("[ ] Create `StrategistAgent`", "[x] Create `StrategistAgent`")
text = text.replace("[ ] Build a chat interface in the dashboard", "[x] Build a chat interface in the dashboard")
text = text.replace("[ ] Expose a new API endpoint", "[x] Expose a new API endpoint")
text = text.replace("[ ] Add UI capabilities to accept/reject generated code", "[x] Add UI capabilities to accept/reject generated code")
text = text.replace("[ ] Implement a dynamic strategy loader", "[x] Implement a dynamic strategy loader")
text = text.replace("[ ] Test backtesting a generated strategy", "[x] Test backtesting a generated strategy")

with open("/home/samx/.gemini/antigravity/brain/c38f091f-452b-4049-be3d-5d7cb0d119df/task.md", "w") as f:
    f.write(text)
