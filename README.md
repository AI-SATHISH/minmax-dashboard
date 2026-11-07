# Min & Max Salary Range Dashboard

Live HR salary benchmarking dashboard pulling data directly from a Zoho Sheet
via the Zoho Sheet OAuth API. Role-based logins restrict data by Sub Band,
Location, and Costing Head.

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python server.py
```

## Deploy (Render/Railway free tier)

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn server:app --bind 0.0.0.0:$PORT`
- Set all variables from `.env.example` in the platform's Environment settings.
