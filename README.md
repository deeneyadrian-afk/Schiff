# Schiffe Versenken Multiplayer

Render deployment:
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`

Dateien:
- `app.py`: Flask + Spiellogik + SQLite Autosave
- `templates/index.html`: HTML
- `static/style.css`: Styles
- `static/script.js`: Frontend-Logik
