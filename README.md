# Accessiblity-Agent
An accessibility agent for less tech savvy users looking for assistance using web browsers. This agent features microphone input streaming and fast response times. 

run backend:
uvicorn main:app --reload --port 8000

test backend:
wscat -c ws://localhost:8000/agent
    {"type":"start_task","task":"click the contact link","url":"https://example.com","title":"Test","dom_tree":[{"tag":"a","text":"Home","selector":"a.home","visible":true},{"tag":"a","text":"Contact","selector":"a.contact","visible":true}]}
    
