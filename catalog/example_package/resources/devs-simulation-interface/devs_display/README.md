Will be added later. 

frontend: https://github.com/czyarl/devs_display
	- This is the frontend for the devs_display. It can chat and display the structure of the model. 
	- It is written in React. Must be individually installed and run. See its README for more information. 
	- To pull it, run `git submodule update --remote`. 

backend:
	- This is the backend for the devs_display. It is written in Python and uses FastAPI. 
	- To install, run `pip install fastapi uvicorn watchdog` (Seems they are installed with the HAMLET package, so you do not need to de anything. )

API:
	- See `API.md` for the current REST API and the proposed session/progress API design.
