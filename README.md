# Accessiblity-Agent
An accessibility agent for less tech savvy users looking for assistance using web browsers. This agent features microphone input streaming and fast response times. 

backend/.env needs:
ANTHROPIC_API_KEY=...     # agent reasoning (Claude)
ASSEMBLYAI_API_KEY=...    # voice-to-text (mic button)

voice: the first mic use opens a one-time "Turn on voice" setup tab —
allow the microphone there once and it works on every website after
(the permission belongs to the extension, not to individual sites).

run backend:
cd ~/Documents/GitHub/Accessiblity-Agent/backend
source ~/.venvs/accessibility/bin/activate
uvicorn main:app --reload --port 8000

test backend:
wscat -c ws://localhost:8000/agent
    {"type":"start_task","task":"click the contact link","url":"https://example.com","title":"Test","dom_tree":[{"tag":"a","text":"Home","selector":"a.home","visible":true},{"tag":"a","text":"Contact","selector":"a.contact","visible":true}]}
    
